"""בוט חדשות תעופה - שלב א': שאיבה -> ניסוח בעברית -> אישור בטלגרם.

מצבים:
  python bot.py run    # מעבד לחיצות על כפתורים, ואז שואב חדשות ושולח הצעות
  python bot.py test   # שולח הודעת בדיקה עם כפתורים
  python bot.py sample # כמו run, אבל בודק 72 שעות אחורה ומתעלם ממה שכבר נראה

סודות נדרשים (משתני סביבה): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

import imgcard

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
TOPICS_FILE = os.environ.get("TOPICS_FILE", "topics.json")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "12"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "70"))
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "3"))
PENDING_TTL_DAYS = 3


import threading

STATE_LOCK = threading.RLock()


def ttl_hours(urgency):
    """תוקף הצעה לפי דחיפות: חדשה דחופה מתיישנת מהר, ידע יכול לחכות."""
    u = int(urgency or 3)
    return 3 if u >= 4 else 12 if u == 3 else PENDING_TTL_DAYS * 24
UA = "Mozilla/5.0 (compatible; news-bot/1.0)"


# ---------------------------------------------------------------- state
DECISIONS_DIR = "decisions"


def apply_decision_files(st):
    """מיישם החלטות מיידיות (קבצים קטנים שנכתבים בהרצת ה-webhook) על המצב הראשי, בלי התנגשויות בגיט."""
    if not os.path.isdir(DECISIONS_DIR):
        return
    for fn in sorted(os.listdir(DECISIONS_DIR)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(DECISIONS_DIR, fn)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        st["pending"].pop(d.get("pid"), None)
        item = d.get("item")
        if item and d.get("action") == "q":
            st["queue"].append(item)
        elif item and d.get("action") in ("a", "n"):
            if not any(x.get("id") == item.get("id") for x in st["approved"]):
                st["approved"].append(item)
        try:
            os.remove(path)
        except OSError:
            pass


def load_state(apply_decisions=True):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    st.setdefault("seen", [])
    st.setdefault("pending", {})
    st.setdefault("approved", [])
    st.setdefault("queue", [])
    st.setdefault("slots_done", {})
    st.setdefault("offset", 0)
    st.setdefault("daily", {"date": "", "count": {}})
    if apply_decisions:
        apply_decision_files(st)
    legacy = {"mideast": "security", "world": "politics"}
    for coll in (st["pending"].values(), st["approved"]):
        for it in coll:
            if isinstance(it, dict) and it.get("topic") in legacy:
                it["topic"] = legacy[it["topic"]]
    return st


def save_state(st):
    st["seen"] = st["seen"][-3000:]
    st["approved"] = st["approved"][-300:]
    now = time.time()
    st["pending"] = {k: v for k, v in st["pending"].items() if v.get("ts", 0) > now - ttl_hours(v.get("urgency")) * 3600}
    keep = {os.path.basename(x["image"]) for x in list(st["pending"].values()) + st["approved"] + st["queue"] if x.get("image")}
    if os.path.isdir("images"):
        for fn in os.listdir("images"):
            if fn.endswith(".jpg") and fn not in keep:
                try:
                    os.remove(os.path.join("images", fn))
                except OSError:
                    pass
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------- telegram
def _token():
    t = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not t:
        sys.exit("חסר TELEGRAM_BOT_TOKEN")
    return t


def _chat_id():
    c = os.environ.get("TELEGRAM_CHAT_ID")
    if not c:
        sys.exit("חסר TELEGRAM_CHAT_ID")
    return str(c).strip()


def tg(method, **payload):
    """קריאה ל-API של טלגרם. לא מדפיסים את ה-URL כדי לא לחשוף את הטוקן."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_token()}/{method}", json=payload, timeout=30
        )
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"telegram {method} failed: {type(e).__name__}") from None
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method} error: {data.get('description')}")
    return data["result"]


def tg_photo(path, caption):
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{_token()}/sendPhoto",
                data={"chat_id": _chat_id(), "caption": caption[:1000]},
                files={"photo": f},
                timeout=60,
            )
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"telegram sendPhoto failed: {type(e).__name__}") from None
    if not data.get("ok"):
        raise RuntimeError(f"telegram sendPhoto error: {data.get('description')}")


def keyboard(pid):
    return {
        "inline_keyboard": [
            [
                {"text": "🚀 העלה עכשיו", "callback_data": f"n:{pid}"},
                {"text": "➕ הוסף לתור", "callback_data": f"q:{pid}"},
            ],
            [{"text": "❌ דחה", "callback_data": f"r:{pid}"}],
        ]
    }


def _bar(v):
    return "●" * v + "○" * (5 - v)



def when_line(cands, idxs):
    """שורת זמני פרסום ראשון: שעה בישראל ובת כמה הידיעה, לפי המקור הוותיק ביותר."""
    from zoneinfo import ZoneInfo

    rows = []
    for i in idxs:
        c = cands[i]
        if not c.get("ts"):
            continue
        try:
            d = datetime.fromisoformat(c["ts"])
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        rows.append((d, c["source"]))
    if not rows:
        return "🕒 זמן פרסום לא ידוע"
    rows.sort()
    d, src = rows[0]
    mins = max(0, int((datetime.now(timezone.utc) - d).total_seconds() // 60))
    age = f"לפני {mins} דק'" if mins < 90 else f"לפני {mins // 60} שע'"
    stamp = d.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%H:%M %d/%m")
    fresh = "🟢" if mins < 60 else ("🟠" if mins < 180 else "🔴")
    return f"🕒 פורסם לראשונה: {stamp} ({age}) · {src} {fresh}"


def check_line(text, n_sources, topic):
    """בדיקה אוטומטית לידיעות רגישות: מקורות, ייחוס, נפגעים ופרטים מבצעיים."""
    if topic not in ("security", "politics"):
        return ""
    ok = lambda b, y, n: ("✅ " + y) if b else ("⚠️ " + n)  # noqa: E731
    return "🔍 בדיקה: " + " · ".join([
        ok(n_sources >= 2, "2+ מקורות", "מקור יחיד"),
        ok("לפי" in text or "אמר" in text or "מסר" in text, "יש ייחוס", "אין ייחוס"),
        ok(not re.search(r"\d+ הרוגים|נהרגו \d+|שם ההרוג|קואורדינט", text), "בלי פרטים מסוכנים", "בדוק נפגעים/פרטים"),
    ])


AUTO_TOPICS = {"facts", "aviation", "politics"}
AUTO_DAILY_CAP = 4
SENSITIVE = re.compile(r"הרוג|נהרג|נהרגו|פצוע|נפצע|נפצעו|התרסק|התרסקות|קורבנ|מת\b|מתו|אסון|תאונה|חטיפ|פיגוע|מלחמ|ירי|טיל")


def auto_ok(st, topic, p, n_sources):
    """אישור אוטומטי לתור: רק ידע ותעופה, חומרה נמוכה, בלי נפגעים/ביטחון, ובתעופה לפחות 2 מקורות."""
    if topic not in AUTO_TOPICS or p.get("severity") != "low" or p.get("israel"):
        return False
    if SENSITIVE.search(p["text"]):
        return False
    if topic == "aviation" and n_sources < 2:
        return False
    if topic == "politics" and (n_sources < 3 or st["daily"].get("auto_politics", 0) >= 2):
        return False  # פוליטיקה: רק עם 3 מקורות ומעלה, עד 2 ביום
    return st["daily"].get("auto", 0) < AUTO_DAILY_CAP


FAST_TOPICS = {"security", "aviation", "abroad", "politics"}
FAST_DAILY_CAP = 15


def FAST_OK(st, topic, p):
    """מתפרצת (דחיפות 4+) עולה מיד לפייסבוק כדיווח ראשוני. תקרה יומית נגד ספאם/חסימה."""
    return topic in FAST_TOPICS and int(p.get("urgency") or 3) >= 4 and st["daily"].get("fast", 0) < FAST_DAILY_CAP and not load_settings().get("no_fast")


def send_proposal(pid, text, links, severity, n_sources, israel=False, topic="aviation", urgency=3, special=3, auto=False, when=""):
    sev = {"high": "🔴 חומרה גבוהה", "medium": "🟠 חומרה בינונית"}.get(severity, "🟢 חומרה נמוכה")
    verified = f"{n_sources} מקורות" if n_sources > 1 else "⚠️ מקור יחיד"
    flag = " · 🇮🇱 קשר ישראלי" if israel else ""
    tp = load_topics().get(topic, {})
    scores = f"⚡ דחיפות {_bar(urgency)} {urgency}/5 · ⭐ חשיבות וייחודיות {_bar(special)} {special}/5"
    rec = "🚀 מומלץ להעלות עכשיו" if urgency >= 4 else ("➕ מומלץ להוסיף לתור" if special >= 3 else "🗑 שקול לדחות")
    head = "🤖 אושר אוטומטית ונכנס לתור (אפשר לבטל)" if auto else "הצעה לפוסט"
    if urgency >= 5:
        head = "🚨 מתפרצת · " + head
    msg = f"{tp.get('emoji', '📰')} {head} · {tp.get('label', '')}\n{scores}\n{sev} · {verified}{flag}\n{when}\n\n{text}\n\n" + "\n".join(f"🔗 {u}" for u in links) + f"\n\n{when}\n{check_line(text, n_sources, topic)}\n{scores}\n{rec}"
    res = tg(
        "sendMessage",
        chat_id=_chat_id(),
        text=msg[:4000],
        reply_markup=({"inline_keyboard": [[{"text": "🛑 בטל פרסום", "callback_data": f"r:{pid}"}]]} if auto else keyboard(pid)),
        link_preview_options={"is_disabled": True},
    )
    return res["message_id"]


def apply_decision(st, decision):
    """מיישם החלטה מהפורמט "a:<pid>" (אשר) או "r:<pid>" (דחה). משמש את מצב ה-webhook המיידי."""
    action, _, pid = (decision or "").partition(":")
    item = st["pending"].pop(pid, None)
    if item is None and action == "r":  # ביטול פריט שאושר אוטומטית ועדיין בתור
        before = len(st["queue"])
        st["queue"] = [x for x in st["queue"] if not (x.get("id") == pid and not x.get("published"))]
        if len(st["queue"]) < before:
            print("decision: auto-approved item cancelled:", pid)
            return True
    if item is None:
        print("decision: item already handled or expired:", pid)
        return False
    if action == "q":
        st["queue"].append(
            {
                "id": pid,
                "text": item["text"],
                "links": item["links"],
                "severity": item.get("severity"),
                "topic": item.get("topic"),
                "urgency": item.get("urgency"),
                "image": item.get("image"),
                "credit": item.get("credit"),
                "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "published": False,
            }
        )
    elif action in ("a", "n"):
        st["approved"].append(
            {
                "id": pid,
                "text": item["text"],
                "links": item["links"],
                "severity": item.get("severity"),
                "image": item.get("image"),
                "credit": item.get("credit"),
                "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "published": False,
            }
        )
    print("decision applied:", action, pid)
    return True


WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or "https://mer5.shalmonraw.workers.dev"


def ensure_webhook():
    """מחבר מחדש את ה-webhook לטלגרם אם הוא נעלם. פועל רק אם הוגדר WEBHOOK_SECRET ב-GitHub."""
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        return
    try:
        if webhook_active():
            return
        tg("setWebhook", url=WEBHOOK_URL, secret_token=secret, allowed_updates=["callback_query"])
        print("webhook was missing: restored")
    except Exception as e:  # noqa: BLE001
        print("warn: ensure_webhook", type(e).__name__, str(e)[:120])


def webhook_active():
    try:
        return bool(tg("getWebhookInfo").get("url"))
    except Exception as e:
        print("warn: getWebhookInfo", e)
        return False


def process_updates(st):
    """מטפל בלחיצות על כפתורי אשר/דחה שהצטברו מאז הריצה הקודמת (כשאין webhook)."""
    if webhook_active():
        print("poll: skipped, webhook is active (decisions arrive via workflow input)")
        return
    updates = tg(
        "getUpdates", offset=st["offset"], timeout=0, allowed_updates=["callback_query"]
    )
    handled = 0
    for u in updates:
        st["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq:
            continue
        if str(cq["from"]["id"]) != _chat_id():
            continue  # רק אתה יכול לאשר
        action, _, pid = (cq.get("data") or "").partition(":")
        item = st["pending"].pop(pid, None)
        msg = cq.get("message") or {}
        try:
            if item is None:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="הפריט כבר טופל או פג תוקף")
            else:
                ok = action == "a"
                if ok:
                    st["approved"].append(
                        {
                            "id": pid,
                            "text": item["text"],
                            "links": item["links"],
                            "severity": item.get("severity"),
                            "image": item.get("image"),
                            "credit": item.get("credit"),
                            "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "published": False,
                        }
                    )
                handled += 1
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="אושר ✅" if ok else "נדחה ❌")
            if msg.get("message_id"):
                status = "— " + ("אושר ✅" if action == "a" and item else "נדחה ❌" if item else "טופל")
                tg(
                    "editMessageText",
                    chat_id=_chat_id(),
                    message_id=msg["message_id"],
                    text=((msg.get("text") or "") + "\n\n" + status)[:4000],
                    link_preview_options={"is_disabled": True},
                )
        except RuntimeError as e:
            print("warn:", e)
    print(f"poll: {len(updates)} updates, {handled} handled")


# -------------------------------------------------------------- sources
def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def feed_image(e):
    """כתובת תמונה מתוך פריט ה-RSS, אם קיימת."""
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []) or []:
        if str(l.get("type", "")).startswith("image") and l.get("href"):
            return l["href"]
    m = re.search(r'<img[^>]+src="([^"]+)"', e.get("summary", "") or "")
    return m.group(1) if m else ""


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)


def load_topics():
    with open(TOPICS_FILE, encoding="utf-8") as f:
        return json.load(f)



def fetch_telegram(src):
    """קורא ערוץ טלגרם ציבורי דרך תצוגת t.me/s/ ומחזיר רשימת פריטים [(id, text, iso_time, image)]."""
    r = requests.get(src["url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    items = []
    for blk in re.split(r'(?=<div class="tgme_widget_message_wrap)', r.text)[1:]:
        post = re.search(r'data-post="([^"]+)"', blk)
        txt = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', blk, re.S)
        tm = re.search(r'<time[^>]+datetime="([^"]+)"', blk)
        img = re.search(r"tgme_widget_message_photo_wrap[^>]+background-image:url\('([^']+)'\)", blk)
        if not post or not txt:
            continue
        text = strip_html(re.sub(r"<br\s*/?>", "\n", txt.group(1)))
        if len(text) < 40:
            continue
        items.append((post.group(1), text, tm.group(1) if tm else "", img.group(1) if img else ""))
    return items


def fetch_candidates(st, topic):
    sources = load_topics()[topic]["sources"]
    sample = os.environ.get("SAMPLE") == "1"
    seen = set() if sample else set(st["seen"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72 if sample else MAX_AGE_HOURS)
    out, failed = [], []
    in_run = set()
    for src in sources:
        if src.get("type") == "telegram":
            try:
                for post, text, iso, img in fetch_telegram(src):
                    link = f"https://t.me/{post}"
                    h = hashlib.sha1(link.encode()).hexdigest()[:12]
                    if h in seen or h in in_run:
                        continue
                    in_run.add(h)
                    try:
                        ts = datetime.fromisoformat(iso) if iso else None
                    except ValueError:
                        ts = None
                    if ts and ts < cutoff:
                        continue
                    out.append({"h": h, "source": src["name"], "title": text.split("\n")[0][:140], "summary": text[:500], "link": link,
                                "image": img, "ts": ts.astimezone(timezone.utc).isoformat(timespec="minutes") if ts else ""})
            except Exception as e:  # noqa: BLE001
                failed.append(f'{src["name"]} ({type(e).__name__})')
            continue
        try:
            r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=25)
            if r.status_code >= 400:  # חלק מהאתרים חוסמים בוטים: ניסיון חוזר עם דפדפן
                r = requests.get(src["url"], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}, timeout=25)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # noqa: BLE001
            failed.append(f'{src["name"]} ({type(e).__name__})')
            continue
        for e in feed.entries:
            link = e.get("link")
            if not link:
                continue
            h = hashlib.sha1(link.encode()).hexdigest()[:12]
            if h in seen or h in in_run:
                continue
            in_run.add(h)
            ts = entry_time(e)
            if ts and ts < cutoff:
                continue
            out.append(
                {
                    "h": h,
                    "source": src["name"],
                    "title": strip_html(e.get("title")),
                    "summary": strip_html(e.get("summary"))[:500],
                    "link": link,
                    "image": feed_image(e),
                    "ts": ts.isoformat(timespec="minutes") if ts else "",
                }
            )
    if failed:
        print("feeds failed:", ", ".join(failed))
    out.sort(key=lambda c: c["ts"], reverse=True)
    return out[:MAX_CANDIDATES]


# --------------------------------------------------------------- claude
WRITER_MODEL = os.environ.get("WRITER_MODEL", "claude-opus-5-5")

SELECT_SYSTEM = """אתה עורך ראשי של עמוד חדשות תעופה פופולרי בעברית, לקהל ישראלי רחב (לא רק חובבי תעופה).
תקבל רשימת ידיעות (כותרת, תקציר, מקור). בחר רק סיפורים שאנשים ירצו לקרוא, לשתף ולעקוב אחריהם.

מה לבחור (רק אם יש בזה משהו באמת יוצא דופן):
- תקריות ואירועים דרמטיים או מוזרים: נחיתות חירום, תקלות חריגות, כמעט-התנגשויות, מקרים נדירים, שיבושים גדולים.
- מקרים עם "וואו": שיאים, סיפורים אנושיים, פרטים מפתיעים, ציוד חריג.
- קשר ישראלי חזק: אל על, ישראייר, ארקיע, נתב"ג, ישראלים שנתקעו, קווים לישראל, סגירות מרחב אווירי באזור. קודם כל.
מה לדלג עליו תמיד: הזמנות מטוסים, דוחות כספיים, מינויים, מגעים עסקיים, פתיחת קווים שגרתית, כתבות דעה ורשימות,
פרסומות, ופעילות צבאית או ביטחונית עם פרטי מיקום או מבצעים. עדיף רשימה ריקה מסיפור בינוני.

עדיפות עליונה לסיפורים מרתקים: סיפור אנושי, תקרית יוצאת דופן שהסתיימה טוב, מקרה נדיר או מוזר, פרט שגורם לקורא לומר "וואו" או לשתף. בנוסף חובה לכלול כל עדכון קריטי: תאונה, נחיתת חירום, סגירת מרחב אווירי, תקלה חמורה, גם אם הסיפור יבש.

אם כמה ידיעות עוסקות באותו אירוע, אחד אותן ל-pick אחד (כל האינדקסים ב-idxs).
severity: high לאירוע חירום או סכנה, medium לתקלה או שיבוש משמעותי, low לשאר. israel: true אם יש קשר ישראלי ישיר.
urgency (1-5): כמה דחוף להעלות עכשיו (5 = חדשה מתפרצת שמתיישנת בדקות, 3 = בתוך שעות, 1 = יכולה לחכות ימים). special (1-5): כמה הידיעה ייחודית וחשובה (5 = בלעדית או משנה תמונה, 3 = טובה, 1 = שגרתית).
החזר JSON בלבד: {"picks":[{"idxs":[0,3],"severity":"low","urgency":3,"special":3,"israel":false}]}   ואם אין: {"picks":[]}"""

WRITE_SYSTEM = """אתה כתב תעופה בכיר בערוץ חדשות ישראלי מוביל, שכותב גם לעמוד הפייסבוק של הערוץ.
הקורא צריך להרגיש שכתב שטח מנוסה רשם את זה: מקצועי, מדויק, רגוע, בטוח בעצמו, ובכל זאת כזה שאי אפשר להפסיק לקרוא.
הפוסט לא נשמע כמו בינה מלאכותית, לא כמו סיפור ספרותי ולא כמו הודעה לעיתונות.

כלי מקצועי אחד לשימוש כשהחומר מצדיק (לא בכוח): "לשם השוואה". מספר או מרחק גדול נעשה מוחשי כשמשווים אותו למשהו מוכר לקורא הישראלי (מרחק מתל אביב, גודל של מגרש כדורגל, זמן נסיעה). ההשוואה חייבת להיות מדויקת, ואם אין לך נתון בטוח אל תמציא אותה. בפתיח אפשר לפתוח בשאלה או בפרט מפתיע אחד שגורם לקורא להמשיך, בתנאי שהוא מופיע בחומר.
איזון באורך לפי סוג הידיעה: ידיעות קלות ואהובות על הקהל (צרכנות, תיירות, מקומות, שיאים, סיפורים אנושיים, טרנדים, "וואו") נכתבות קצרות וקלילות, 250-500 תווים, 2-3 פסקאות קצרות, בלי חפירה, בלי הקדמות ובלי הסברים מיותרים: פרט מרכזי, הקשר אחד, משפט סיום. ידיעות רציניות או מורכבות (ביטחון, פוליטיקה, תקריות עם נפגעים, החלטות שמשפיעות על אנשים) מקבלות אורך כמה שצריך, עד 1000 תווים, כשכל משפט נושא מידע חדש.
מבנה (האורך נקבע לפי החומר וסוג הידיעה, בין 250 ל-1000 תווים. אל תרחיב בכוח ואל תמלא רווחים: חומר דל מקבל פוסט קצר וממוקד. כל פסקה נכנסת רק אם יש לה תוכן אמיתי):
1. כותרת חדשותית ומדויקת של 6-12 מילים: מה קרה ואיפה, עם הפרט החריג ביותר. בלי סיסמאות ובלי "דרמה". אימוג'י אחד לכל היותר.
2. פסקת פתיחה: משפט פתיחה קצר ותמונתי אחד, שנוצר מפרט קונקרטי מהחומר (רגע, מקום או מצב) ומושך את הקורא פנימה. אחריו מה, מי, איפה ומתי, ומה התוצאה, בסדר של חשיבות. השעות, המספרים, סוג המטוס, החברה, המסלול והנפגעים בדיוק כפי שבחומר.
3. פסקת פרטים והקשר: מה ידוע על הנסיבות, מי חוקר, מה עוד דווח, ופרט אנושי אחד או שניים אם הם בחומר (מי היה על הסיפון, מה עשו, איך הסתיים).
4. פסקת "מה זה אומר" (רק אם יש מה להסביר, אחרת דלג עליה): הסבר פשוט וברור של המנגנון או הרקע, והמסקנה למי שטס או לענף. מה שהופך את הידיעה להבנה.
5. שורה אחרונה: משפט סיום אחד עם נימה סיפורית עדינה, שנשען על עובדה ומשאיר תחושה או מחשבה (תמונה אחרונה, פרט אנושי או תובנה חדה). לא מוסר השכל, לא שאלה לקורא, לא סיכום.

סגנון:
- כמו מהדורת חדשות טובה: משפטים ברורים של עד 20 מילים, פעלים פעילים, עברית טבעית שנכתבה במקור ולא תורגמה. הסיפוריות מינימלית ורק בפתיחה ובסיום, וכל השאר דיווח מקצועי.
- דקדוק מדויק: התאמת מין ומספר (שני מסוקים, שתי טיסות, שלושה הרוגים, חמש נוסעות), זמנים עקביים לאורך הטקסט, סמיכות נכונה, מילות יחס נכונות (התנגש ב-, נחת ב-, התרסק ליד), ידיעה תקינה ובלי מבנה משפט שמזכיר אנגלית.
- הקורא חכם ומתעניין. אל תדבר אליו בגוף שני ואל תבקש ממנו לדמיין.
- מותר פרט אנושי כשהוא בחומר. אסור להמציא רגשות, ציטוטים, מחשבות או סצנות. בתקרית עם נפגעים: טון מכבד ומאופק.
- חשוב: דברים שגורמים לטקסט להישמע כמו צ'אט בוט אסורים לחלוטין: "תארו לעצמכם", "מאחורי כל מספר", "בעולם התעופה", "הערב", "שוב",
  "לא סתם", "זה לא רק X אלא Y", שלשות של שמות תואר, התפעלות ("מדהים", "מטלטל"), פסקת סיכום, מוסר השכל, שאלה רטורית בסוף, וקף מיותר או נקודותיים דרמטיות.
- בלי האשטגים, קישורים, או "נעדכן". "דווח כי" מותר פעם אחת לכל היותר, וכשהאירוע לא מאושר בלבד.
- מונחים: מנחת, המראה, נחיתה, נחיתת חירום, תא הטייס, צוות הדיילים, מגדל הפיקוח, מסלול, מטוס נוסעים, מסוק.

הוק וסיפוריות בטוב טעם: בנושאים לא מפחידים ולא מלחיצים מותר סיפוריות קלה יותר ופתיח שתופס תשומת לב (פרט מפתיע, שאלה חדה או תמונה קונקרטית מהחומר). בדיווחים רציניים או עם נפגעים: הוק קטן ומאופק בלבד. אף פעם לא בגרוזה, בלי המצאות, ובלי לפגוע בדיוק.

דיוק: עובדות על האירוע רק מהחומר. הסברים כלליים על איך התעופה עובדת מותר מהידע הכללי, אם הם מוסכמים ובלי מספרים או טענות ספציפיות על האירוע.
אין לציין שמות של הרוגים או פצועים (רק גיל, לאום ותפקיד). שמות חברות, מקומות ובעלי תפקידים רשמיים מותרים. אם שם או מספר מופיע בכתיב שונה בין מקורות, השמט אותו.
אל תנחש סיבות לתקלה או לתאונה. מספרים ושמות בדיוק כפי שבחומר. אירוע שלא אושר מנוסח בזהירות.

דוגמה לסגנון בלבד (העובדות בדויות, אל תשתמש בהן):
נחיתת חירום בסלובניה: מטוס נוסעים חג שעה לפני שנחת בלי גלגל אף

מטוס נוסעים של חברה אזורית נחת בבוקר יום רביעי בנמל התעופה של ליובליאנה בלי גלגל הנחיתה הקדמי. 96 הנוסעים וארבעת אנשי הצוות פונו במגלשות חירום, ואיש מהם לא נפגע.

הטייסת דיווחה למגדל על תקלה במנגנון ההנחתה כעשר דקות אחרי ההמראה. המטוס חג מעל המסלול כשעה כדי לשרוף דלק, בזמן שכוחות הכיבוי והחילוץ התייצבו לאורכו. הרשות הלאומית לחקירת תאונות פתחה בבדיקה.

מה זה אומר? מטוס עם פחות דלק קל יותר, נוחת במהירות נמוכה יותר, ובחיכוך עם המסלול קטן הסיכון להתלקחות. זו פרוצדורה מתורגלת בסימולטור, והיא חלק מהכשרת הטייסים הקבועה.

נחיתה על שני הגלגלים הראשיים בלבד נחשבת מהתרחישים הנדירים אך הנלמדים ביותר בהכשרת טייסים.

החזר JSON בלבד: {"text":"..."}"""

REVIEW_SYSTEM = """אתה עורך לשון ובודק עובדות. תקבל חומר גלם ואת הטיוטה של פוסט בעברית.
1. בדוק כל עובדה, מספר, שם ושעה בטיוטה מול החומר. פרט שלא מופיע בחומר חייב להימחק.
2. תקן עברית ובדוק שההסברים הכלליים נכונים ומקובלים (אחרת מחק אותם): ניסוח מתורגם, מילים לא מדויקות, תחביר מגושם, פעלים סבילים מיותרים, ובעיות במינוח תעופתי.
3. שמור על הטון של כתב שטח בכיר: מקצועי, מדויק, ברור ומרתק. הסר כל ניסוח שנשמע כמו בוט או כמו ספרות (פנייה לקורא, שאלות רטוריות, מוסר השכל, דרמטיזציה). אל תוסיף תוכן כדי להאריך, ואל תקצר פרטים חשובים. הסברים כלליים על איך התעופה עובדת מותרים, אבל פרטים ספציפיים על האירוע חייבים להיות מהחומר. עד 1100 תווים.
4. בדיקת כתיב קפדנית, מילה אחר מילה: כתיב מלא, שגיאות הקלדה, מילים כפולות או חסרות, אותיות דומות (ו/ז, ח/כ, ס/שׂ), הטיות, גרשיים וגרש תקינים (צה״ל, נתב״ג) ושמות פרטיים. תקן כל שגיאה והוסף אותה ל-issues.
החזר JSON בלבד: {"text":"הנוסח הסופי","issues":["תיאור קצר של כל תיקון עובדתי שנעשה"]}"""


HEBREW_SYSTEM = """אתה עורך לשון בכיר בעיתון ישראלי מוביל. תקבל פוסט חדשותי בעברית. שפר אך ורק את העברית, בלי לשנות עובדות, מספרים, שמות או משמעות.
בדיקת כתיב היא חלק מהמשימה: כתיב מלא לפי כללי האקדמיה ללשון העברית, גרשיים וגרש תקינים (צה״ל, נתב״ג, ג׳ורג׳) ולא מרכאות ישרות, הטיות ושמות פרטיים תקינים, ובלי שגיאות הקלדה. תקן כל שגיאה שנמצאה.
בדוק בקפדנות:
1. התאמת מין ומספר בין שם עצם, שם תואר, פועל ומספרים (שני מסוקים, שתי נוסעות, שלושה הרוגים, 11 איש, שנים-עשר, וכו').
2. עקביות זמנים, ובניין פועל נכון.
3. סמיכות, ידיעה, ומילות יחס (התנגש ב-, נחת ב-, התרסק ליד, חזר ל-).
4. סדר מילים טבעי בעברית, ובלי מבנים שמזכירים תרגום מאנגלית ("נעשה שימוש", "על ידי" מיותר, "ביחס ל", "בעקבות כך ש").
5. פיסוק, כתיב (מלא/חסר לפי הנוהג בעיתונות), וסימני גרשיים במקומות הנכונים.
6. קצב ורהיטות: משפט ארוך מדי יפוצל, משפט יבש מדי יקבל פועל חי. שמור על הפתיחה והסיום הסיפוריים העדינים.
החזר JSON בלבד: {"text":"הנוסח הסופי"}"""


def claude_text(system, user, model, max_tokens=3000):
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def claude_json(system, user, model, max_tokens=3000):
    raw = claude_text(system, user, model, max_tokens)
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b < 0:
        m = re.search(r'"text"\s*:\s*"(.*)', raw, re.S)  # JSON שנחתך באמצע: מחלצים את הטקסט
        if m and len(m.group(1)) > 150:
            txt = m.group(1).rstrip("`} \n").rstrip('"').replace("\\n", "\n").replace('\\"', '"')
            print("truncated JSON recovered, chars:", len(txt))
            return {"text": txt}
        print("no JSON in reply, start of reply:", raw[:200].replace("\n", " "))
        return {"_raw": raw}
    try:
        return json.loads(raw[a : b + 1])
    except json.JSONDecodeError:
        print("bad JSON in reply, start of reply:", raw[:200].replace("\n", " "))
        return {"_raw": raw}


def fetch_article(url):
    """מנסה להביא את גוף הכתבה. מחזיר מחרוזת ריקה אם לא הצליח (למשל קישורי Google News)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        body = re.sub(r"(?is)<(script|style|nav|footer|header|aside).*?</\1>", " ", r.text)
        paras = [strip_html(x) for x in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)]
        text = " ".join(p for p in paras if len(p) > 60)
        return text[:4000] if len(text) > 300 else ""
    except Exception:  # noqa: BLE001
        return ""


def parse_picks(data, n):
    picks = []
    for p in data.get("picks", []):
        idxs = [i for i in p.get("idxs", []) if isinstance(i, int) and 0 <= i < n]
        if idxs:
            sev = p.get("severity") if p.get("severity") in ("low", "medium", "high") else "low"
            def score(k):
                v = p.get(k)
                return v if isinstance(v, int) and 1 <= v <= 5 else 3
            picks.append({"idxs": sorted(set(idxs)), "severity": sev, "urgency": score("urgency"), "special": score("special"), "israel": bool(p.get("israel"))})
    return picks



_GN_CACHE = {}


def resolve_google_news(link):
    """מפענח קישור news.google.com לכתובת הכתבה האמיתית. אם נכשל, מחזיר את הקישור המקורי."""
    if "news.google.com" not in link:
        return link
    if link in _GN_CACHE:
        return _GN_CACHE[link]
    out = link
    try:
        import base64
        from urllib.parse import quote, urlparse

        gid = urlparse(link).path.rstrip("/").split("/")[-1]
        try:  # פורמט ישן: הכתובת מקודדת בתוך המזהה
            raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
            m = re.search(rb"https?://[\x21-\x7e]+", raw)
            if m and raw.startswith(b"\x08\x13\x22"):
                out = m.group(0).decode()
        except Exception:  # noqa: BLE001
            pass
        if out == link:
            page = requests.get(f"https://news.google.com/articles/{gid}?hl=en-US&gl=US&ceid=US:en", headers={"User-Agent": UA}, timeout=20).text
            sg = re.search(r'data-n-a-sg="([^"]+)"', page)
            ts = re.search(r'data-n-a-ts="([^"]+)"', page)
            if sg and ts:
                inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, ts.group(1), sg.group(1)])
                body = "f.req=" + quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
                r = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body, timeout=20,
                                  headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
                arr = json.loads(r.text.split("\n\n", 1)[1])
                url = json.loads(arr[0][2])[1]
                if url.startswith("http"):
                    out = url
    except Exception as e:  # noqa: BLE001
        print("gnews resolve failed:", type(e).__name__, str(e)[:80])
    _GN_CACHE[link] = out
    return out


def story_material(cands, idxs):
    parts = []
    for i in idxs:
        c = cands[i]
        c["link_real"] = resolve_google_news(c["link"])
        art = fetch_article(c["link_real"])
        c["has_body"] = bool(art)
        print("material:", c["source"], "| resolved" if c["link_real"] != c["link"] else "| NOT resolved", "| body" if art else "| no body")
        parts.append(f'מקור: {c["source"]}\nכותרת: {c["title"]}\nתקציר: {c["summary"]}\n' + (f"גוף הכתבה: {art}" if art else ""))
    return "\n\n---\n\n".join(parts)


SAFETY_NEWS = """כללי בטיחות ואמינות (מחייבים):
- כל טענה עובדתית מיוחסת למקור מזוהה ("לפי רויטרס", "לפי משרד הבריאות בעזה", "לפי צה"ל", "לפי אל ג'זירה"). אל תאמץ טענה של צד בעימות כעובדה.
- אין פרטים מבצעיים: מיקום או תנועת כוחות ישראליים, מועדי תקיפה, מיקומי אתרים רגישים, קואורדינטות. תנועת כוחות זרים מותרת רק אם דווחה בכמה כלי תקשורת מרכזיים, עם ייחוס, ובלי מיקום מדויק.
- אין תיאורים גרפיים של אלימות או גוויות, אין שמות של הרוגים או פצועים, אין סרטונים או תמונות לא מאומתים, ואין הסתה, השמצה או שפה מבזה כלפי קבוצה.
- מספרי נפגעים: תמיד עם מקורם, ובלי חישובים או הערכות משלך.
- אל תנחש כוונות או תוצאות. מה שלא ידוע נכתב במפורש כלא ידוע."""

SELECT_MIDEAST = """אתה עורך ראשי של MER5, עמוד חדשות ישראלי. מדור "ביטחון, ישראל והמזרח התיכון": עדיפות ראשונה לכל מה שקורה בתוך ישראל ובסביבתה (פיגועים, ירי, אזעקות, תקריות משטרה וביטחון, תאונות ואסונות, שריפות, פשיעה חריגה, אירועים שמעניינים את הציבור הרחב), ואחר כך מזרח תיכון והעולם הערבי, מלחמות, סיכול טרור והתפתחויות צבאיות. תקריות בתוך ישראל נבחרות גם כשהן קטנות יחסית, כל עוד הן מעניינות את הציבור וזוכות לתשומת לב (מקור אחד מספיק לדיווח ראשוני). תקבל רשימת ידיעות (כותרת, תקציר, מקור, באנגלית, עברית או ערבית).
בחר רק ידיעות בעלות משקל: התפתחויות במלחמות ובעימותים באזור, החלטות ממשלות, הסכמים ומשברים, מהלכים צבאיים שדווחו בכמה מקורות מרכזיים, אנרגיה וכלכלה באזור,
וגם ידיעות מהתקשורת הערבית שהקהל הישראלי לא רואה ("מה אומרים בעולם הערבי") כשהן מעניינות באמת.
דלג על: שמועות ורשתות חברתיות ללא מקור מזוהה, תעמולה, ידיעות קטנות או חוזרות, ספורט, סלבס, ופרטים מבצעיים של כוחות ישראליים.
אם כמה ידיעות עוסקות באותו אירוע אחד אותן (כל האינדקסים ב-idxs). עדיפות לאירוע שמופיע ביותר ממקור אחד.
severity: high להסלמה או אירוע גדול, medium להתפתחות משמעותית, low לשאר. israel: true אם יש קשר ישיר לישראל.
עדיף רשימה ריקה מפוסט בינוני. urgency (1-5): כמה דחוף להעלות עכשיו (5 = חדשה מתפרצת שמתיישנת בדקות, 3 = בתוך שעות, 1 = יכולה לחכות ימים). special (1-5): כמה הידיעה ייחודית וחשובה (5 = בלעדית או משנה תמונה, 3 = טובה, 1 = שגרתית).
החזר JSON בלבד: {"picks":[{"idxs":[0,3],"severity":"low","urgency":3,"special":3,"israel":false}]}   ואם אין: {"picks":[]}"""

SELECT_WORLD = """אתה עורך ראשי של MER5, עמוד חדשות ישראלי. מדור "פוליטיקה": תקבל ידיעות פוליטיות מהעולם. בחר רק סיפורים בעלי משקל:
בחירות ותוצאותיהן, משברים בממשלות וקואליציות, החלטות של מנהיגים ומעצמות, פסגות, סנקציות והסכמים, הפוליטיקה בישראל, בארה"ב ובעולם הערבי, ומהלכים שמשפיעים על המזרח התיכון.
דלג על: רכילות פוליטית, כתבות דעה, סקרים קטנים, פוליטיקה מקומית זעירה, סלבס וספורט. אחד ידיעות על אותו אירוע. severity לפי חשיבות. israel: true אם יש קשר ישיר לישראל.
שמור על ניטרליות: דווח על עמדות כעמדות של מי שאמר אותן, בלי הטיה לאף צד. עדיף רשימה ריקה. urgency (1-5): כמה דחוף להעלות עכשיו (5 = חדשה מתפרצת שמתיישנת בדקות, 3 = בתוך שעות, 1 = יכולה לחכות ימים). special (1-5): כמה הידיעה ייחודית וחשובה (5 = בלעדית או משנה תמונה, 3 = טובה, 1 = שגרתית).
החזר JSON בלבד: {"picks":[{"idxs":[0],"severity":"low","urgency":3,"special":3,"israel":false}]}   ואם אין: {"picks":[]}"""

_base, _tail = WRITE_SYSTEM.rsplit("החזר JSON בלבד", 1)
WRITE_NEWS = (
    _base.replace("אתה כתב תעופה בכיר בערוץ חדשות ישראלי מוביל", "אתה כתב בכיר בערוץ חדשות ישראלי מוביל")
    .replace("דוגמה לסגנון בלבד", "הדוגמה הבאה מתחום התעופה, אבל הסגנון, המבנה והטון זהים לכל נושא. דוגמה לסגנון בלבד")
    + SAFETY_NEWS
    + "\n\nהחזר JSON בלבד"
    + _tail
)

IMAGE_NEWS = """אתה עורך תמונות בעמוד חדשות. תקבל פוסט בעברית. החזר JSON בלבד:
{"card_title":"...","highlight":["..."],"subject":"...","queries":["...","...","..."]}
- highlight: עד 3 מילים מתוך card_title בדיוק כפי שהן כתובות שהן עיקר הידיעה (מקום, מספר או הפועל המרכזי), לצביעה בצבע הנושא.
- subject: משפט קצר באנגלית: מה בדיוק צריך להיות בתמונה כדי שיהיה קשר ברור לידיעה (המקום, הכלי, המבנה או הארגון המרכזי בפוסט).
- queries: עד 3 חיפושים באנגלית ב-Wikimedia Commons, מהספציפי לכללי, כולם על הישות המרכזית של הידיעה (שם המקום, המבנה, סוג הספינה/הכלי, המוסד). אם אין צילום ספציפי מתאים, השאר רשימה קצרה: עדיף בלי תמונה מאשר תמונה לא קשורה.
- card_title: כותרת חדה לכרטיס, עד 75 תווים בעברית, בלי אימוג'י, בלי נקודה בסוף, בלי עובדות שלא נאמרו בפוסט. מספרים חייבים להיות זהים לפוסט. כתיב מדויק לגמרי, גרשיים תקינים (צה״ל, נתב״ג).
- הצילום חייב להראות את הישות המרכזית של הידיעה (עיר, ציון דרך, בניין, נמל, סוג ספינה או כלי רכב), לא תמונת "אווירה" כללית.
  לעולם לא אנשים סובלים, חיילים בפעולה, נשק בשימוש, הרס, פצועים או פוליטיקאים."""

SELECT_FACTS = """אתה עורך ראשי של MER5, מדור "ידיעות מהעולם": עובדות, תגליות וסיפורים מעניינים באמת מהעולם: מדע, היסטוריה, טבע, טכנולוגיה, תרבויות ותופעות חריגות. תקבל רשימת כתבות.
בחר רק סיפור שגורם לקורא לומר "לא ידעתי את זה": פרט מפתיע, מספר שמעורר תדהמה, גילוי חדש או סיפור היסטורי עם תפנית. הסיפור חייב להיות מבוסס על המקור ולא על שמועה או על טענה בלי הוכחה.
דלג על: חדשות פוליטיות וביטחוניות, סלבס, ספורט, פרסומות, רשימות כלליות ("10 דברים ש..."), וסיפורים שכבר ידועים לכולם. אחד כתבות על אותו נושא. severity תמיד low, israel: true רק אם יש קשר ישיר לישראל.
עדיף רשימה ריקה. urgency (1-5): כמה דחוף להעלות עכשיו (5 = חדשה מתפרצת שמתיישנת בדקות, 3 = בתוך שעות, 1 = יכולה לחכות ימים). special (1-5): כמה הידיעה ייחודית וחשובה (5 = בלעדית או משנה תמונה, 3 = טובה, 1 = שגרתית).
החזר JSON בלבד: {"picks":[{"idxs":[0],"severity":"low","urgency":3,"special":3,"israel":false}]}   ואם אין: {"picks":[]}"""

WRITE_FACTS = WRITE_NEWS.replace("אתה כתב בכיר בערוץ חדשות ישראלי מוביל", "אתה כותב בכיר במדור ידע ותגליות של ערוץ חדשות ישראלי מוביל") + """

מדור עובדות: הפתיח הוא הקרס. פתח בשאלה או בפרט מפתיע אחד מהחומר. הסיפור מסופר בקצב טוב, עם "לשם השוואה" מדויקת כשיש נתון בטוח. דיוק עובדתי מעל הכול: כל מספר, שנה ושם מהמקור בלבד."""

SELECT_ABROAD = """אתה עורך ראשי של MER5, מדור "ישראלים בעולם": ידיעות שמעניינות ישראלים שמטיילים, גרים או עושים עסקים בחו"ל. תקבל רשימת ידיעות.
בחר: שינויי ויזה וכניסה, אזהרות מסע, יחס מקומי לתיירים ישראלים, אירועים שמערבים ישראלים, בטיחות במוקדי תיירות (תאילנד, הודו, יוון, טורקיה, דרום אמריקה ועוד), טיסות וקווים לישראל, פעילות קהילות ישראליות ויהודיות בחו"ל.
דלג על: פוליטיקה פנימית, ספורט, סלבס, עבירות פליליות של פרט בלי נפגעים, וידיעה בלי פרט שמשנה משהו לישראלי שמתכנן נסיעה.
severity: high רק אם יש סכנה ממשית לישראלים. israel תמיד true.
urgency (1-5): כמה דחוף להעלות עכשיו (5 = אזהרה או שינוי שנוגעים למטיילים עכשיו). special (1-5): כמה הידיעה ייחודית וחשובה.
עדיף רשימה ריקה. החזר JSON בלבד: {"picks":[{"idxs":[0],"severity":"low","urgency":3,"special":3,"israel":true}]}   ואם אין: {"picks":[]}"""

SELECT_BY = {"abroad": SELECT_ABROAD, "facts": SELECT_FACTS, "aviation": SELECT_SYSTEM, "security": SELECT_MIDEAST, "politics": SELECT_WORLD}
WRITE_ABROAD = WRITE_NEWS + """

מדור ישראלים בעולם: השורה הראשונה של הפוסט פותחת באימוג'י הדגל של המדינה שבה מדובר (למשל 🇹🇭), ואחריו הכותרת. הפסקאות קצרות מאוד (1-3 משפטים). כל טענה על יחס או החלטה מיוחסת למי שאמר אותה. ציין בסוף מה המשמעות המעשית למי שמתכנן נסיעה, רק אם היא נובעת מהחומר."""
WRITE_BY = {"abroad": WRITE_ABROAD, "facts": WRITE_FACTS, "aviation": WRITE_SYSTEM, "security": WRITE_NEWS, "politics": WRITE_NEWS}
REVIEW_NEWS = REVIEW_SYSTEM + """
4. בדוק במיוחד ייחוס: כל טענה על אירוע, מספר נפגעים או כוונה של צד חייבת להיות מיוחסת. טענה בלי ייחוס מקור בחומר: הוסף ייחוס או הסר אותה.
5. הסר כל פרט מבצעי (מיקום או תנועת כוחות ישראליים, מועדי תקיפה), שמות של הרוגים ופצועים, ותיאור גרפי. החומר שהוסר מופיע ברשימת issues."""


def clean_hebrew(t):
    """ניקוי טיפוגרפי דטרמיניסטי אחרי כל בדיקות ה-AI: גרשיים, רווחים, מילים כפולות."""
    t = re.sub(r'(?<=[\u05d0-\u05ea])"(?=[\u05d0-\u05ea])', "\u05f4", t)
    t = re.sub(r"(?<=[\u05d0-\u05ea])'(?=[\s,.:;!?]|$)", "\u05f3", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" +([,.:;!?])", r"\1", t)
    t = re.sub(r"(?<![\w])([\u05d0-\u05ea]{2,})\s+\1(?![\w])", r"\1", t)  # מילה כפולה
    return t.strip()


def _words(s):
    return set(w for w in re.findall(r"[\u05d0-\u05eaA-Za-z0-9]{3,}", s or ""))


def recent_sent(st, hours=48):
    cut = time.time() - hours * 3600
    return [x["line"] for x in st.get("sent_log", []) if x["ts"] > cut] + recent_headlines(st)


def is_duplicate(st, text):
    """True אם הפוסט עוסק בדיוק באותו אירוע כמו הצעה שנשלחה ב-48 השעות האחרונות (חפיפת מילים גבוהה)."""
    w = _words(text.split("\n")[0])
    for line in recent_sent(st):
        lw = _words(line)
        if len(lw) >= 4 and len(w) >= 4 and len(w & lw) / min(len(w), len(lw)) >= 0.4:
            return True
    return False


DEDUP_SYSTEM = """אתה בודק כפילויות בעמוד חדשות. תקבל פוסט חדש ורשימת כותרות של פוסטים שכבר נשלחו. החזר JSON בלבד: {"duplicate":true|false}.
duplicate=true אם הפוסט החדש עוסק באותו אירוע או באותה ידיעה כמו אחת הכותרות, גם בניסוח שונה או ממקור אחר. false רק אם זה אירוע אחר או התפתחות מהותית חדשה."""


def llm_duplicate(st, text):
    prev = list(dict.fromkeys(recent_sent(st, 36)))[-60:]
    if not prev:
        return False
    try:
        r = claude_json(DEDUP_SYSTEM, "פוסט חדש:\n" + text[:600] + "\n\nכבר נשלחו:\n" + "\n".join("- " + x for x in prev), MODEL, 300)
        return bool(r.get("duplicate"))
    except Exception as e:  # noqa: BLE001
        print("dedup llm failed:", str(e)[:100])
        return False


def build_posts(cands, topic="aviation", st=None):
    lines = [f'[{i}] ({c["source"]}) {c["title"]}\n{c["summary"]}' for i, c in enumerate(cands)]
    sent = recent_sent(st) if st else []
    extra = ("\n\nידיעות שכבר נשלחו ב-48 השעות האחרונות. אל תבחר שוב אירוע זהה, גם אם הכתבות שונות, אלא רק אם יש התפתחות מהותית חדשה:\n" + "\n".join("- " + s for s in sent[-40:])) if sent else ""
    picks = parse_picks(claude_json(SELECT_BY[topic], "\n\n".join(lines) + extra, MODEL, 2000), len(cands))
    review = REVIEW_SYSTEM if topic == "aviation" else REVIEW_NEWS
    posts = []
    for pk in picks[:MAX_POSTS_PER_RUN]:
        try:
            material = story_material(cands, pk["idxs"])
            if not any(cands[i].get("has_body") for i in pk["idxs"]) and max(len(cands[i]["summary"]) for i in pk["idxs"]) < 160:
                print("skip thin story (no article body, short summary):", cands[pk["idxs"][0]]["title"][:60])
                continue
            w = claude_json(WRITE_BY[topic], material, WRITER_MODEL, 6000)
            draft = (w.get("text") or "").strip()
            if not draft and len(w.get("_raw", "")) > 150 and "{" not in w["_raw"][:5]:
                draft = w["_raw"].strip()  # המודל החזיר טקסט חופשי
            if not draft:
                continue
            rev = claude_json(review, f"חומר גלם:\n{material}\n\nטיוטה:\n{draft}", WRITER_MODEL, 6000)
            text = (rev.get("text") or draft).strip()
            if rev.get("issues"):
                print("review fixes:", rev["issues"])
            pol = claude_json(HEBREW_SYSTEM, text, WRITER_MODEL, 6000)
            text = (pol.get("text") or text).strip()
            posts.append({**pk, "text": clean_hebrew(text).replace("\n", "\n\n", 1) if "\n\n" not in clean_hebrew(text) else clean_hebrew(text)})
        except Exception as e:  # noqa: BLE001
            print("story failed:", type(e).__name__, str(e)[:200])
    return posts


IMAGE_AVIATION = """אתה עורך תמונות בעמוד חדשות תעופה. תקבל פוסט בעברית. החזר JSON בלבד:
{"card_title":"...","highlight":["..."],"subject":"...","queries":["...","...","..."]}
- highlight: עד 3 מילים מתוך card_title בדיוק כפי שהן כתובות שהן עיקר הידיעה (מקום, מספר או הפועל המרכזי), לצביעה בצבע הנושא.
- subject: משפט קצר באנגלית: מה בדיוק צריך להיות בתמונה (סוג המטוס, החברה או שדה התעופה המרכזיים בפוסט).
- queries: עד 3 חיפושים באנגלית ב-Wikimedia Commons, מהספציפי לכללי (למשל "LOT Polish Airlines Boeing 737 MAX", "Boeing 737 airliner"), תמיד על סוג המטוס/החברה/שדה התעופה בפוסט. אם אין מידע ספציפי, עדיף רשימה קצרה מתמונה לא קשורה.
- card_title: כותרת חדה לתמונה, עד 75 תווים בעברית, בלי אימוג'י, בלי נקודה בסוף, שונה מעט מכותרת הפוסט ולא מוסיפה עובדות שלא נאמרו בפוסט. מספרים חייבים להיות זהים לפוסט. כתיב מדויק לגמרי, גרשיים תקינים (צה״ל, נתב״ג).
- אם הפוסט אינו מזכיר סוג מטוס או חברה מסוימים, השתמש בשאילתה כללית על סוג הכלי.
"""


GENERIC_Q = {"security": "Middle East landscape", "aviation": "airliner at airport", "politics": "parliament building", "facts": "nature landscape", "abroad": "airport departures hall"}


def judge_photo(img, info, subject, post):
    """בדיקה חזותית: האם התמונה מראה את מה שהידיעה עוסקת בו. מחזיר True/False."""
    import anthropic, base64, io as _io

    small = img.copy()
    small.thumbnail((800, 800))
    buf = _io.BytesIO()
    small.save(buf, "JPEG", quality=80)
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system='אתה עורך תמונות. נתון פוסט חדשותי ותמונה מועמדת. החזר JSON בלבד: {"ok":true|false,"why":"..."}. ok=true רק אם התמונה מראה בבירור את הישות המרכזית של הפוסט (אותו סוג כלי, מקום או מבנה) וראויה לפרסום כרקע לידיעה. תמונה כללית, היסטורית, של דבר אחר, עם טקסט/שלט בולט לא קשור, או באיכות גרועה: false.',
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}},
            {"type": "text", "text": f"נושא נדרש: {subject}\nכותרת הקובץ: {info}\nהפוסט:\n{post[:700]}"},
        ]}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        v = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return False
    print("photo judge:", v.get("ok"), str(v.get("why"))[:100])
    return bool(v.get("ok"))


_STATE_REF = {}


def st_no_source():
    return _STATE_REF.get("st", {}).get("no_source_images_until", 0) > time.time()


def make_image(pid, text, topic="aviation", overlay=False, sources=()):
    """מחזיר (נתיב תמונה, קרדיט או None). צילום רק אם נבדק שהוא קשור לידיעה, אחרת כרטיס טיפוגרפי נקי."""
    tp = load_topics()[topic]
    meta = claude_json(IMAGE_AVIATION if topic == "aviation" else IMAGE_NEWS, text, MODEL, 700)
    title = (meta.get("card_title") or text.split("\n", 1)[0]).strip()
    subject = meta.get("subject") or title
    queries = [q for q in (meta.get("queries") or []) if isinstance(q, str)][:3]
    photo, credit = None, None
    typo_only = topic == "explain" or bool(re.search(r"הרוג|נהרג|נפצע|פצועים|מת |נרצח|גופות|חלל", text.split("\n\n")[0] + title))
    if typo_only:  # פוסטי מערכת וידיעות רגישות: כרטיס טיפוגרפי עם הפטרנים, בלי צילום
        queries, sources = [], ()
    for q in queries:
        try:
            cands = imgcard.commons_candidates(q, aviation=(topic == "aviation"), limit=3)
        except Exception as e:  # noqa: BLE001
            print("commons failed:", type(e).__name__)
            continue
        for img, cr, info in cands:
            try:
                if judge_photo(img, info, subject, text):
                    photo, credit = img, cr
                    break
            except Exception as e:  # noqa: BLE001
                print("judge failed:", type(e).__name__)
        if photo:
            break
    if photo is None and (load_settings().get("use_source_images", True) is False or st_no_source()):
        sources = ()
    if photo is None:  # אין צילום חופשי מאומת: תמונה מהמקור של הידיעה עצמה (עם קרדיט למקור)
        for url, src_name, link in sources:
            got = imgcard.source_image(url, link)
            print("source image:", src_name, "ok" if got else "none")
            if got:
                from urllib.parse import urlparse
                host = urlparse(link).netloc.replace("www.", "") or src_name
                photo, credit = got, f"תמונה: {host}"
                break
    blur = False
    if photo is None and not typo_only:  # עדיין אין תמונה: תמונה כללית של הנושא, מטושטשת, כדי שתמיד יהיה צילום אמיתי
        generic = [q for q in (queries + [GENERIC_Q.get(topic, "city skyline")]) if q]
        for q in generic[-2:]:
            try:
                c = imgcard.commons_candidates(q, aviation=False, limit=1)
            except Exception as e:  # noqa: BLE001
                print("generic commons failed:", type(e).__name__)
                continue
            if c:
                photo, credit, blur = c[0][0], c[0][1], True
                print("generic blurred photo used for:", q)
                break
    path = imgcard.render_card(title, os.path.join("images", f"{pid}.jpg"), photo, credit, tp["color"], tp["label"], tp.get("en", ""), overlay=(overlay or blur) and not typo_only, highlight=meta.get("highlight") or [], blur=blur)
    return path, credit


def fetch_and_propose(st, topic):
    tp = load_topics()[topic]
    cands = fetch_candidates(st, topic)
    print(f"[{topic}] candidates: {len(cands)}")
    if not cands:
        return
    posts = build_posts(cands, topic, st)  # אם נכשל, לא מסמנים כנראה, ננסה שוב בריצה הבאה
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st["daily"].get("date") != today or not isinstance(st["daily"].get("count"), dict):
        st["daily"] = {"date": today, "count": {}}
    sample = os.environ.get("SAMPLE") == "1"
    sent = 0
    for p in posts:
        if sent >= MAX_POSTS_PER_RUN or (not sample and st["daily"]["count"].get(topic, 0) >= tp.get("daily_cap", 4)):
            break
        with STATE_LOCK:
            dup = is_duplicate(st, p["text"]) or llm_duplicate(st, p["text"])
            if not dup:  # רושמים מיד, כדי שנושא אחר שרץ במקביל לא ישלח אותו אירוע
                st.setdefault("sent_log", []).append({"ts": time.time(), "line": p["text"].split("\n")[0][:160]})
                st["sent_log"] = st["sent_log"][-200:]
        if dup:
            print("skip duplicate story:", p["text"][:60].replace("\n", " "))
            continue
        links0 = [cands[i]["link"] for i in p["idxs"]]
        links = [cands[i].get("link_real") or cands[i]["link"] for i in p["idxs"]]
        n_sources = len({cands[i]["source"] for i in p["idxs"]})
        pid = hashlib.sha1("".join(links0).encode()).hexdigest()[:10]
        img_path, credit = None, None
        try:
            img_path, credit = make_image(pid, p["text"], topic, overlay=True, sources=[(cands[i].get("image", ""), cands[i]["source"], cands[i].get("link_real") or cands[i]["link"]) for i in p["idxs"]])
            tg_photo(img_path, "🖼 תמונה לפוסט" + (f"\n📷 {credit}" if credit else "\nכרטיס טיפוגרפי, בלי צילום"))
        except Exception as e:  # noqa: BLE001
            print("image failed:", type(e).__name__, str(e)[:150])
            img_path = None
        with STATE_LOCK:
            fast = (not sample) and FAST_OK(st, topic, p)
            if fast:
                st["daily"]["fast"] = st["daily"].get("fast", 0) + 1
                p["text"] = p["text"].rstrip() + "\n\n⏳ דיווח ראשוני. הפרטים בבדיקה ויעודכנו."
            auto = (not sample) and not fast and auto_ok(st, topic, p, n_sources)
            if auto:
                st["daily"]["auto"] = st["daily"].get("auto", 0) + 1
                if topic == "politics":
                    st["daily"]["auto_politics"] = st["daily"].get("auto_politics", 0) + 1
        send_proposal(pid, p["text"], links, p["severity"], n_sources, p.get("israel", False), topic, p.get("urgency", 3), p.get("special", 3), auto, when_line(cands, p["idxs"]))
        if fast:
            tg("sendMessage", chat_id=_chat_id(), text="🚨 מתפרצת: פורסמה אוטומטית בפייסבוק כדיווח ראשוני (בבדיקה). אפשר למחוק בפייסבוק אם צריך.")
            with STATE_LOCK:
                st["approved"].append({"id": pid, "text": p["text"], "links": links, "severity": p["severity"], "image": img_path, "credit": credit, "topic": topic, "urgency": p.get("urgency", 3),
                                       "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "published": False, "fast": True})
                if not sample:
                    st["daily"]["count"][topic] = st["daily"]["count"].get(topic, 0) + 1
                checkpoint(st)
                publish_approved(st)
                checkpoint(st)
            sent += 1
            continue
        with STATE_LOCK:
            if auto:
                st["queue"].append({"id": pid, "text": p["text"], "links": links, "severity": p["severity"], "image": img_path, "credit": credit, "topic": topic, "urgency": p.get("urgency", 3),
                                    "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "published": False, "auto": True})
            else:
                st["pending"][pid] = {
                    "text": p["text"],
                    "links": links,
                    "severity": p["severity"],
                    "topic": topic,
                    "image": img_path,
                    "credit": credit,
                    "urgency": p.get("urgency", 3),
                    "ts": time.time(),
                }
            if not sample:
                st["daily"]["count"][topic] = st["daily"]["count"].get(topic, 0) + 1
            checkpoint(st)  # נשמר מיד אחרי כל הצעה, כך שלחיצה בטלגרם תמיד תמצא אותה
        sent += 1
    with STATE_LOCK:
        st["seen"].extend(c["h"] for c in cands)
    print(f"[{topic}] proposals sent: {sent}")


# ------------------------------------------------------------- facebook
FB_VERSION = os.environ.get("FB_API_VERSION", "v23.0")


def page_token(page, token):
    """אם הוגדר טוקן משתמש, מחליף אותו אוטומטית בטוקן של העמוד (חייבים טוקן עמוד כדי לפרסם)."""
    try:
        r = requests.get(f"https://graph.facebook.com/{FB_VERSION}/{page}", params={"fields": "access_token", "access_token": token}, timeout=40).json()
        return r.get("access_token") or token
    except Exception:  # noqa: BLE001
        return token



def mirror_to_channel(item):
    """משכפל פוסט שפורסם לערוץ הטלגרם הציבורי (settings.json: tg_channel, למשל "@mer5news"). הבוט חייב להיות מנהל בערוץ."""
    ch = load_settings().get("tg_channel")
    if not ch:
        return
    try:
        text = item["text"] + ("\n\n" + "\n".join(item.get("links", [])[:2]) if item.get("links") else "")
        if item.get("image") and os.path.exists(item["image"]):
            with open(item["image"], "rb") as f:
                requests.post(f"https://api.telegram.org/bot{_token()}/sendPhoto", data={"chat_id": ch}, files={"photo": f}, timeout=60)
        requests.post(f"https://api.telegram.org/bot{_token()}/sendMessage", data={"chat_id": ch, "text": text[:4000], "disable_web_page_preview": "true"}, timeout=30)
    except Exception as e:  # noqa: BLE001
        print("channel mirror failed:", type(e).__name__)


def publish_approved(st):
    """מפרסם לעמוד פייסבוק פוסטים שאושרו. פועל רק אם הוגדרו FB_PAGE_ID ו-FB_PAGE_TOKEN."""
    page, token = os.environ.get("FB_PAGE_ID"), os.environ.get("FB_PAGE_TOKEN")
    if not page or not token:
        return
    if load_settings().get("pause_publish") or st.get("paused_until", 0) > time.time():
        return  # הפרסום מושהה (ידנית או אוטומטית אחרי חסימה)
    token = page_token(page, token)
    for item in st["approved"]:
        if item.get("published") or item.get("fails", 0) >= 2:
            continue
        cr = item.get("credit") or ""
        caption = item["text"] + (("\n\n📷 " + (cr if cr.startswith("תמונה:") else "צילום ארכיון: " + cr)) if cr else "")
        try:
            base = f"https://graph.facebook.com/{FB_VERSION}/{page}"
            if item.get("image") and os.path.exists(item["image"]):
                with open(item["image"], "rb") as f:
                    r = requests.post(base + "/photos", data={"message": caption, "access_token": token}, files={"source": f}, timeout=90)
            else:
                r = requests.post(base + "/feed", data={"message": caption, "access_token": token}, timeout=60)
            data = r.json()
            fb_id = data.get("post_id") or data.get("id")
            if not fb_id:
                err = data.get("error") or {}
                if err.get("code") in (368, 4, 17, 32, 613, 190, 200, 10) and err.get("code") != 200:
                    st["paused_until"] = time.time() + 6 * 3600
                    tg("sendMessage", chat_id=_chat_id(), text=f"🚨 פייסבוק חסם או הגביל פרסום (קוד {err.get('code')}): {str(err.get('message', ''))[:200]}\nהפרסום הושהה ל-6 שעות. בדוק ב-Meta Business Suite > Account Quality.")
                raise RuntimeError(str(err.get("message", "unknown"))[:150])
            # מקורות בתגובה הראשונה, כדי לא לפגוע בחשיפה
            try:
                q = ""
                try:
                    qj = claude_json('כתוב שאלה אחת קצרה (עד 12 מילים) בעברית שמזמינה את הקוראים להגיב על הידיעה, בלי להטות לצד פוליטי ובלי נושא כואב אם יש נפגעים (אז החזר ריק). החזר JSON: {"q":"..."}', item["text"][:600], MODEL, 150)
                    q = (qj.get("q") or "").strip()
                except Exception:  # noqa: BLE001
                    q = ""
                body = ("מקורות:\n" + "\n".join(item["links"])) if item.get("links") else ""
                body = (q + "\n\n" + body).strip()
                if body:
                    requests.post(f"https://graph.facebook.com/{FB_VERSION}/{fb_id}/comments", data={"message": body, "access_token": token}, timeout=40)
            except Exception:  # noqa: BLE001
                pass
            item["published"] = True
            mirror_to_channel(item)
            item["fb_id"] = fb_id
            item["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            tg("sendMessage", chat_id=_chat_id(), text=f"📣 פורסם בפייסבוק\n{item['text'].splitlines()[0]}")
        except Exception as e:  # noqa: BLE001
            print("publish failed:", type(e).__name__, str(e)[:150])
            item["fails"] = item.get("fails", 0) + 1
            tg("sendMessage", chat_id=_chat_id(), text="⚠️ הפרסום לפייסבוק נכשל: " + (str(e)[:150] if isinstance(e, RuntimeError) else type(e).__name__))



def fb_health(st):
    """בדיקת בריאות העמוד כל 30 דקות: תקינות הטוקן, פוסטים שהוסרו, ומעקב עוקבים. התראה בטלגרם כשיש בעיה."""
    page, token = os.environ.get("FB_PAGE_ID"), os.environ.get("FB_PAGE_TOKEN")
    if not page or not token or time.time() - st.get("health_ts", 0) < 1800:
        return
    st["health_ts"] = time.time()
    try:
        token = page_token(page, token)
        base = f"https://graph.facebook.com/{FB_VERSION}"
        r = requests.get(f"{base}/{page}", params={"fields": "name,followers_count,fan_count,is_published", "access_token": token}, timeout=30).json()
        if r.get("error"):
            tg("sendMessage", chat_id=_chat_id(), text=f"🚨 בעיית גישה לעמוד בפייסבוק (קוד {r['error'].get('code')}): {str(r['error'].get('message', ''))[:200]}")
            return
        if r.get("is_published") is False:
            tg("sendMessage", chat_id=_chat_id(), text="🚨 העמוד מסומן כלא מפורסם בפייסבוק!")
        st.setdefault("followers", []).append({"ts": int(time.time()), "n": r.get("followers_count") or r.get("fan_count") or 0})
        st["followers"] = st["followers"][-600:]
        removed = []
        for item in st["approved"][-15:]:
            fid = item.get("fb_id")
            if not fid or not item.get("published") or item.get("removed"):
                continue
            age = time.time() - datetime.fromisoformat(item.get("published_at", "2000-01-01T00:00:00+00:00")).timestamp()
            if age > 3 * 86400:
                continue
            q = requests.get(f"{base}/{fid}", params={"fields": "id,is_hidden,is_published", "access_token": token}, timeout=30).json()
            e = q.get("error") or {}
            if e.get("code") == 100 and e.get("error_subcode") in (33, None) or q.get("is_published") is False or q.get("is_hidden"):
                item["removed"] = True
                removed.append(item)
        if removed:
            st.setdefault("removals", []).extend(int(time.time()) for _ in removed)
            names = "\n".join("• " + x["text"].splitlines()[0][:80] + (f" ({x['credit']})" if x.get("credit") else "") for x in removed)
            tg("sendMessage", chat_id=_chat_id(), text=f"⚠️ פוסטים שהוסרו או הוסתרו בפייסבוק ({len(removed)}):\n{names}\nייתכן דיווח זכויות יוצרים או הפרת תקנון.")
        recent = [x for x in st.get("removals", []) if time.time() - x < 86400]
        st["removals"] = recent
        if len(recent) >= 2 and not st.get("no_source_images_until", 0) > time.time():
            st["no_source_images_until"] = time.time() + 3 * 86400
            tg("sendMessage", chat_id=_chat_id(), text="🛡 שני פוסטים הוסרו ב-24 שעות: כיביתי אוטומטית תמונות מהמקור ל-3 ימים (נשארים צילום חופשי, תמונה כללית מטושטשת וכרטיס).")
    except Exception as e:  # noqa: BLE001
        print("health check failed:", type(e).__name__, str(e)[:120])



# ------------------------------------------------------------ daily jobs
EXPLAIN_SYSTEM = """אתה כותב מדור ההסברה של MER5, עמוד חדשות ישראלי. תקבל כותרות מהיממה האחרונה בתחומי העמוד (תעופה, ביטחון, פוליטיקה, ידיעות מהעולם, ישראלים בחו"ל).
בחר נושא אחד שהקהל שומע עליו ומתבלבל או שיש סביבו אי-הבנות, וכתוב פוסט הסברה: "מה זה אומר" / "איך זה עובד" / "מה באמת קורה". אם אין כותרת מתאימה, בחר נושא ידע מוכר ומעניין בתחומים האלה.
כללים: פתיח שתופס תשומת לב (שאלה או פרט מפתיע), 3-5 פסקאות קצרות, 450-800 תווים, עברית טבעית ורהוטה. רק ידע מבוסס ומוסכם. בלי מספרים וסטטיסטיקות אלא אם אתה בטוח בהם לגמרי. בנושא פוליטי או ביטחוני: הסבר מנגנונים והקשר, לא עמדות, ובלי פרטים מבצעיים. בלי האשטגים, קישורים או שאלה לקורא בסוף. שורה אחרונה: תובנה אחת ברורה.
החזר JSON בלבד: {"text":"...","topic":"aviation|security|politics|facts|abroad"}"""

FOLLOW_SYSTEM = """אתה כותב ב-MER5 - מהשטח, עמוד חדשות ישראלי (תעופה, ביטחון, פוליטיקה, ידיעות מהעולם, ישראלים בחו"ל). כתוב פוסט קצר ונעים (150-300 תווים) שמזמין לעקוב אחרי העמוד כדי לקבל את הידיעות בזמן. בטון חם ומקצועי, בלי הבטחות שאי אפשר לקיים, בלי מספרים בדויים, בלי האשטגים. גוון את הניסוח.
החזר JSON בלבד: {"text":"..."}"""


def recent_headlines(st):
    out = []
    for it in list(st["approved"][-20:]) + list(st["pending"].values()) + list(st["queue"]):
        line = (it.get("text") or "").split("\n")[0].strip()
        if line and line != "test":
            out.append(line)
    return out[-25:]


def propose_custom(st, topic, text, urgency=2, special=3):
    """שולח הצעה מותאמת (הסברה, עוקבים) לאישור, עם תמונה, כמו כל הצעה אחרת."""
    pid = hashlib.sha1(text.encode()).hexdigest()[:10]
    img_path, credit = None, None
    try:
        img_path, credit = make_image(pid, text, topic, overlay=True)
        tg_photo(img_path, "🖼 תמונה לפוסט" + (f"\n📷 {credit}" if credit else "\nכרטיס טיפוגרפי"))
    except Exception as e:  # noqa: BLE001
        print("custom image failed:", type(e).__name__, str(e)[:120])
    send_proposal(pid, text, [], "low", 1, False, topic, urgency, special, False, "🕒 פוסט מערכת (לא ידיעה)")
    with STATE_LOCK:
        st["pending"][pid] = {"text": text, "links": [], "severity": "low", "topic": topic, "image": img_path, "credit": credit, "urgency": urgency, "ts": time.time()}
        checkpoint(st)  # נשמר מיד, כדי שלחיצה בטלגרם תמצא את ההצעה


def daily_report(st):
    now = time.time()
    pub = [x for x in st["approved"] if x.get("published_at") and now - datetime.fromisoformat(x["published_at"]).timestamp() < 86400]
    fol = st.get("followers") or []
    delta = ""
    if len(fol) >= 2:
        day_ago = [f for f in fol if now - f["ts"] <= 90000]
        if day_ago:
            d = fol[-1]["n"] - day_ago[0]["n"]
            delta = f"\n👥 עוקבים: {fol[-1]['n']} ({'+' if d >= 0 else ''}{d} ב-24 שעות)"
    elif fol:
        delta = f"\n👥 עוקבים: {fol[-1]['n']}"
    by = {}
    for x in pub:
        by[x.get("topic") or "?"] = by.get(x.get("topic") or "?", 0) + 1
    removed = len([x for x in st.get("removals", []) if now - x < 86400])
    msg = (f"📊 דוח יומי · MER5\n📣 פורסמו ב-24 שעות: {len(pub)}\n🗂 בתור: {len(st['queue'])} · ממתינים לאישור: {len(st['pending'])}" + delta
           + (f"\n⚠️ הוסרו: {removed}" if removed else "") + "\n\nמסקנה: " + ("פורסמו מעט פוסטים. כדאי לאשר יותר לתור." if len(pub) < 5 else "קצב הפרסום תקין."))
    tg("sendMessage", chat_id=_chat_id(), text=msg)



def publish_reel(st):
    """בוחר את הפוסט הטרי ביותר שפורסם ולא הפך לרילס, בונה סרטון ומעלה לפייסבוק כרילס."""
    page, token = os.environ.get("FB_PAGE_ID"), os.environ.get("FB_PAGE_TOKEN")
    if not page or not token or load_settings().get("pause_publish") or st.get("paused_until", 0) > time.time():
        return
    now = time.time()
    cand = None
    for it in reversed(st["approved"]):
        if it.get("published") and it.get("image") and os.path.exists(it["image"]) and not it.get("reeled") and it.get("published_at"):
            if now - datetime.fromisoformat(it["published_at"]).timestamp() < 24 * 3600:
                cand = it
                break
    if not cand:
        tg("sendMessage", chat_id=_chat_id(), text="🎬 אין כרגע פוסט טרי להפוך לרילס.")
        return
    cand["reeled"] = True  # מסמנים לפני, כדי שכשל לא יגרום להעלאה כפולה
    import reels

    out = f"/tmp/reel_{cand['id']}.mp4"
    reels.make_reel(cand["image"], out)
    tok = page_token(page, token)
    base = f"https://graph.facebook.com/{FB_VERSION}/{page}/video_reels"
    s = requests.post(base, data={"upload_phase": "start", "access_token": tok}, timeout=60).json()
    if not s.get("video_id"):
        raise RuntimeError("reel start: " + str((s.get("error") or {}).get("message", s))[:200])
    size = os.path.getsize(out)
    with open(out, "rb") as f:
        u = requests.post(s["upload_url"], headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)}, data=f.read(), timeout=180)
    if u.status_code >= 300:
        raise RuntimeError("reel upload: " + u.text[:200])
    head = cand["text"].split("\n")[0]
    desc = head + "\n\n" + "\n".join(cand["text"].split("\n")[1:3]).strip()
    fin = requests.post(base, data={"upload_phase": "finish", "video_id": s["video_id"], "video_state": "PUBLISHED", "description": desc[:900], "access_token": tok}, timeout=90).json()
    try:
        os.remove(out)
    except OSError:
        pass
    if not fin.get("success", False) and not fin.get("post_id") and not fin.get("id"):
        raise RuntimeError("reel finish: " + str((fin.get("error") or {}).get("message", fin))[:200])
    tg("sendMessage", chat_id=_chat_id(), text=f"🎬 רילס עלה לפייסבוק\n{head}")



DIGEST_SYSTEM = """אתה עורך ב-MER5 - מהשטח. תקבל את כותרות הפוסטים שפורסמו היום. כתוב פוסט סיכום ערב: "הידיעות החשובות של היום" עם 4-5 נקודות קצרות (שורה אחת לכל ידיעה, עם אימוג'י מתאים בתחילת השורה), פתיח של משפט אחד וסיום של משפט אחד. רק מה שמופיע ברשימה, בלי להוסיף עובדות. עברית טבעית ורהוטה.
החזר JSON בלבד: {"text":"..."}"""


def todays_published(st):
    now = time.time()
    return [x for x in st["approved"] if x.get("published_at") and now - datetime.fromisoformat(x["published_at"]).timestamp() < 20 * 3600 and x.get("topic") != "explain"]


def links_post_text():
    """פוסט יומי עם קישורים לכל הפלטפורמות, מתוך settings.json: links = {"שם": "כתובת"}."""
    links = load_settings().get("links") or {}
    if not links:
        return ""
    return "מעדכנים אתכם בזמן אמת, בכל מקום:\n" + "\n".join(f"{name}: {url}" for name, url in links.items()) + "\n\nעקבו כדי לא לפספס."


def daily_jobs(st):
    """משימות יומיות לפי שעון ישראל: דוח בוקר, 2 פוסטי הסברה ופוסט הזמנה לעקוב. כל אחת פעם ביום."""
    cfg = load_settings()
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(cfg.get("tz", "Asia/Jerusalem")))
    except Exception:  # noqa: BLE001
        return
    today = now.strftime("%Y-%m-%d")
    st["jobs"] = {today: st.get("jobs", {}).get(today, [])}
    done = st["jobs"][today]
    mins = now.hour * 60 + now.minute
    plan = [("report", "08:00"), ("reel1", "09:30"), ("explain1", "10:30"), ("follow", "13:00"), ("reel2", "14:00"), ("explain2", "18:30"), ("reel3", "20:00"), ("digest", "21:30"), ("links", "19:30")]
    for name, hhmm in plan:
        start = int(hhmm[:2]) * 60 + int(hhmm[3:])
        force = os.environ.get("FORCE_JOBS") == "1"
        if not force and (name in done or not (0 <= mins - start < 120)):
            continue
        if not force:
            done.append(name)  # מסמנים לפני, כדי שכשל לא יגרום לשליחה כפולה
        try:
            if name == "report":
                daily_report(st)
            elif name.startswith("reel"):
                publish_reel(st)
            elif name.startswith("explain"):
                w = claude_json(EXPLAIN_SYSTEM, "כותרות מהיממה:\n" + "\n".join(recent_headlines(st)), WRITER_MODEL, 4000)
                text = (w.get("text") or "").strip()
                if text:
                    pol = claude_json(HEBREW_SYSTEM, text, WRITER_MODEL, 4000)
                    propose_custom(st, "explain", clean_hebrew((pol.get("text") or text).strip()), urgency=1, special=3)
            elif name == "digest":
                heads = [x["text"].split("\n")[0] for x in todays_published(st)]
                if len(heads) >= 3:
                    w = claude_json(DIGEST_SYSTEM, "\n".join(heads), WRITER_MODEL, 2000)
                    if (w.get("text") or "").strip():
                        propose_custom(st, "explain", clean_hebrew(w["text"].strip()), urgency=2, special=3)
            elif name == "links":
                txt = links_post_text()
                if txt:
                    propose_custom(st, "explain", txt, urgency=1, special=2)
            elif name == "follow":
                w = claude_json(FOLLOW_SYSTEM, "כתוב פוסט הזמנה לעקוב להיום.", WRITER_MODEL, 1500)
                text = (w.get("text") or "").strip()
                if text:
                    propose_custom(st, "explain", clean_hebrew(text), urgency=1, special=2)
        except Exception as e:  # noqa: BLE001
            print("daily job failed:", name, type(e).__name__, str(e)[:200])
            if name.startswith("reel"):
                tg("sendMessage", chat_id=_chat_id(), text="⚠️ רילס נכשל: " + str(e)[:200])



def queue_ttl_hours(it):
    """כמה זמן ידיעה בתור נשארת רלוונטית: מתפרצת 6 שעות, רגילה 18 שעות, ידע והסברה 72 שעות."""
    if it.get("topic") in ("facts", "explain"):
        return 72
    u = int(it.get("urgency") or 3)
    return 6 if u >= 4 else 18 if u == 3 else 36


def expire_queue(st):
    keep, dropped = [], []
    now = time.time()
    for it in st["queue"]:
        try:
            age = now - datetime.fromisoformat(it.get("queued_at", "")).timestamp()
        except ValueError:
            age = 0
        (dropped if age > queue_ttl_hours(it) * 3600 else keep).append(it)
    if dropped:
        st["queue"] = keep
        names = "\n".join("• " + x["text"].split("\n")[0][:70] for x in dropped[:8])
        tg("sendMessage", chat_id=_chat_id(), text=f"🗑 הוסרו מהתור ({len(dropped)}) כי כבר לא רלוונטיות:\n{names}")


def publish_queue(st):
    """מפרסם פוסט אחד מהתור בכל חלון זמן מוגדר (settings.json: slots, tz). חלון פעיל עד 60 דקות אחרי השעה."""
    cfg = load_settings()
    slots = cfg.get("slots") or ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(cfg.get("tz", "Asia/Jerusalem")))
    except Exception:  # noqa: BLE001
        now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    st["slots_done"] = {today: st["slots_done"].get(today, [])}
    expire_queue(st)
    if not st["queue"]:
        return
    mins = now.hour * 60 + now.minute
    due = [s for s in slots if 0 <= mins - (int(s[:2]) * 60 + int(s[3:])) < 60 and s not in st["slots_done"][today]]
    if not due:
        return
    item = st["queue"].pop(0)
    item["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    st["approved"].append(item)
    for s in due:
        st["slots_done"][today].append(s)
    publish_approved(st)


# ----------------------------------------------------------------- main
def checkpoint(st):
    """שומר ומעלה את המצב לגיט מיד (רק ב-Actions), כדי שהצעות שנשלחו לטלגרם לא יאבדו אם ההרצה נעצרת."""
    save_state(st)
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    import subprocess
    try:
        run = lambda *a: subprocess.run(a, check=False, capture_output=True, timeout=120)  # noqa: E731
        run("git", "config", "user.name", "news-bot")
        run("git", "config", "user.email", "news-bot@users.noreply.github.com")
        run("git", "add", "-A", "state.json", "images", "decisions")
        if run("git", "diff", "--staged", "--quiet").returncode != 0:
            run("git", "commit", "-m", "checkpoint state")
            for _ in range(3):  # בהתנגשות או דחייה, גרסת המצב שלנו גוברת וננסה שוב
                if run("git", "pull", "--rebase", "-X", "theirs").returncode != 0:
                    run("git", "rebase", "--abort")
                if run("git", "push").returncode == 0:
                    break
                time.sleep(2)
            else:
                print("warn: checkpoint push failed after retries")
    except Exception as e:  # noqa: BLE001
        print("warn: checkpoint", type(e).__name__)


def load_settings():
    try:
        with open("settings.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "webhookinfo":
        info = tg("getWebhookInfo")
        safe = {k: info.get(k) for k in ("url", "pending_update_count", "last_error_date", "last_error_message", "allowed_updates", "has_custom_certificate")}
        print("webhook:", safe)
        tg("sendMessage", chat_id=_chat_id(), text="🔧 webhook: " + json.dumps(safe, ensure_ascii=False)[:900])
        return
    if mode == "fbcheck":
        page, token = os.environ.get("FB_PAGE_ID"), os.environ.get("FB_PAGE_TOKEN")
        if not page or not token:
            tg("sendMessage", chat_id=_chat_id(), text="⚠️ FB_PAGE_ID או FB_PAGE_TOKEN לא מוגדרים")
            return
        r = requests.get(f"https://graph.facebook.com/{FB_VERSION}/{page}", params={"fields": "name,id,fan_count", "access_token": token}, timeout=40).json()
        if r.get("error"):
            msg = "❌ חיבור פייסבוק נכשל: " + str(r["error"].get("message", ""))[:200]
        else:
            msg = "✅ חיבור פייסבוק תקין: " + str(r.get("name")) + " (" + str(r.get("id")) + ")"
        print(msg)
        tg("sendMessage", chat_id=_chat_id(), text=msg)
        return
    if mode == "fonts":
        done = imgcard.font_sheet("/tmp/fonts.jpg")
        tg_photo("/tmp/fonts.jpg", "🔤 גופנים לבחירה:\n" + "\n".join(f"{n}. {name}" for n, name in done) + "\n\nכתוב לי את המספר או השם.")
        return
    font = os.environ.get("FONT_NAME") or load_settings().get("font")
    if font and not imgcard.use_font(font):
        print("font unavailable, using default:", font)
    ensure_webhook()
    st = load_state(apply_decisions=(mode != "decide"))
    if mode == "test":
        pid = "test" + str(int(time.time()))[-6:]
        send_proposal(pid, "בדיקת מערכת\nאם אתה רואה את ההודעה הזו והכפתורים, החיבור לטלגרם עובד.", ["https://github.com"], "low", 2)
        st["pending"][pid] = {"text": "test", "links": [], "severity": "low", "ts": time.time()}
    elif mode == "decide":
        decision = os.environ.get("DECISION", "")
        action, _, pid = decision.partition(":")
        already = any(fn.endswith(f"_{pid}.json") for fn in (os.listdir(DECISIONS_DIR) if os.path.isdir(DECISIONS_DIR) else []))
        if already:
            print("decision: already decided:", pid)
            return
        known = pid in st["pending"]
        before_a, before_q = len(st["approved"]), len(st["queue"])
        apply_decision(st, decision)
        item = None
        if len(st["approved"]) > before_a:
            publish_approved(st)
            item = st["approved"][-1]
        elif len(st["queue"]) > before_q:
            item = st["queue"][-1]
        if not known and action != "x":
            tg("sendMessage", chat_id=_chat_id(), text="⚠️ ההחלטה לא נקלטה: ההצעה כבר לא בזיכרון הבוט (נשלחה לפני עדכון או פגה). חכה להצעה חדשה.")
        os.makedirs(DECISIONS_DIR, exist_ok=True)
        with open(os.path.join(DECISIONS_DIR, f"{int(time.time())}_{pid}.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "action": action, "item": item}, f, ensure_ascii=False)
        return
    elif mode == "reel":  # בדיקה ידנית של רילס אחד
        try:
            publish_reel(st)
        except Exception as e:  # noqa: BLE001
            print("reel failed:", type(e).__name__, str(e)[:300])
            tg("sendMessage", chat_id=_chat_id(), text="⚠️ רילס נכשל: " + str(e)[:300])
        checkpoint(st)
        save_state(st)
        return
    elif mode == "jobs":  # בדיקה ידנית: מריץ את המשימות היומיות מיד
        os.environ["FORCE_JOBS"] = "1"
        _STATE_REF["st"] = st
        try:
            daily_jobs(st)
            checkpoint(st)
        finally:
            save_state(st)
        return
    elif mode in ("run", "sample"):
        if mode == "sample":
            os.environ["SAMPLE"] = "1"  # מתעלם מ"נראו כבר" ובודק 72 שעות אחורה
        _STATE_REF["st"] = st
        started = time.time()
        loops = 0
        try:
            while True:
                loops += 1
                apply_decision_files(st)
                process_updates(st)
                publish_approved(st)
                publish_queue(st)
                fb_health(st)
                daily_jobs(st)
                from concurrent.futures import ThreadPoolExecutor

                topics = [tp for tp in load_topics() if not os.environ.get("ONLY_TOPIC") or tp == os.environ["ONLY_TOPIC"]]

                def one(topic):
                    t0 = time.time()
                    try:
                        fetch_and_propose(st, topic)
                    except Exception as e:  # noqa: BLE001
                        print(f"[{topic}] failed:", type(e).__name__, str(e)[:200])
                    print(f"[{topic}] took {int(time.time() - t0)}s")

                with ThreadPoolExecutor(max_workers=len(topics) or 1) as ex:
                    list(ex.map(one, topics))  # הנושאים רצים במקביל
                with STATE_LOCK:
                    checkpoint(st)
                # סריקה חוזרת באותה ריצה כדי שלא יהיו פערים בין ריצות (עד כ-5 דקות לריצה)
                if True:  # ריצה אחת לכל הפעלה, כדי לחסוך דקות GitHub Actions
                    break
                time.sleep(30)
        finally:
            save_state(st)
        return
    else:
        sys.exit("מצב לא מוכר")
    save_state(st)


if __name__ == "__main__":
    main()
