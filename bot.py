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
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "40"))
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "3"))
PENDING_TTL_DAYS = 3
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
    cutoff = time.time() - PENDING_TTL_DAYS * 86400
    st["pending"] = {k: v for k, v in st["pending"].items() if v.get("ts", 0) > cutoff}
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


def send_proposal(pid, text, links, severity, n_sources, israel=False, topic="aviation", urgency=3, special=3):
    sev = {"high": "🔴 חומרה גבוהה", "medium": "🟠 חומרה בינונית"}.get(severity, "🟢 חומרה נמוכה")
    verified = f"{n_sources} מקורות" if n_sources > 1 else "⚠️ מקור יחיד"
    flag = " · 🇮🇱 קשר ישראלי" if israel else ""
    tp = load_topics().get(topic, {})
    scores = f"⚡ דחיפות {_bar(urgency)} {urgency}/5 · ⭐ חשיבות וייחודיות {_bar(special)} {special}/5"
    rec = "🚀 מומלץ להעלות עכשיו" if urgency >= 4 else ("➕ מומלץ להוסיף לתור" if special >= 3 else "🗑 שקול לדחות")
    msg = f"{tp.get('emoji', '📰')} הצעה לפוסט · {tp.get('label', '')}\n{scores}\n{sev} · {verified}{flag}\n\n{text}\n\n" + "\n".join(f"🔗 {u}" for u in links) + f"\n\n{scores}\n{rec}"
    res = tg(
        "sendMessage",
        chat_id=_chat_id(),
        text=msg[:4000],
        reply_markup=keyboard(pid),
        link_preview_options={"is_disabled": True},
    )
    return res["message_id"]


def apply_decision(st, decision):
    """מיישם החלטה מהפורמט "a:<pid>" (אשר) או "r:<pid>" (דחה). משמש את מצב ה-webhook המיידי."""
    action, _, pid = (decision or "").partition(":")
    item = st["pending"].pop(pid, None)
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


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)


def load_topics():
    with open(TOPICS_FILE, encoding="utf-8") as f:
        return json.load(f)


def fetch_candidates(st, topic):
    sources = load_topics()[topic]["sources"]
    sample = os.environ.get("SAMPLE") == "1"
    seen = set() if sample else set(st["seen"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72 if sample else MAX_AGE_HOURS)
    out, failed = [], []
    in_run = set()
    for src in sources:
        try:
            r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=25)
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

אם כמה ידיעות עוסקות באותו אירוע, אחד אותן ל-pick אחד (כל האינדקסים ב-idxs).
severity: high לאירוע חירום או סכנה, medium לתקלה או שיבוש משמעותי, low לשאר. israel: true אם יש קשר ישראלי ישיר.
urgency (1-5): כמה דחוף להעלות עכשיו (5 = חדשה מתפרצת שמתיישנת בדקות, 3 = בתוך שעות, 1 = יכולה לחכות ימים). special (1-5): כמה הידיעה ייחודית וחשובה (5 = בלעדית או משנה תמונה, 3 = טובה, 1 = שגרתית).
החזר JSON בלבד: {"picks":[{"idxs":[0,3],"severity":"low","urgency":3,"special":3,"israel":false}]}   ואם אין: {"picks":[]}"""

WRITE_SYSTEM = """אתה כתב תעופה בכיר בערוץ חדשות ישראלי מוביל, שכותב גם לעמוד הפייסבוק של הערוץ.
הקורא צריך להרגיש שכתב שטח מנוסה רשם את זה: מקצועי, מדויק, רגוע, בטוח בעצמו, ובכל זאת כזה שאי אפשר להפסיק לקרוא.
הפוסט לא נשמע כמו בינה מלאכותית, לא כמו סיפור ספרותי ולא כמו הודעה לעיתונות.

כלי מקצועי אחד לשימוש כשהחומר מצדיק (לא בכוח): "לשם השוואה". מספר או מרחק גדול נעשה מוחשי כשמשווים אותו למשהו מוכר לקורא הישראלי (מרחק מתל אביב, גודל של מגרש כדורגל, זמן נסיעה). ההשוואה חייבת להיות מדויקת, ואם אין לך נתון בטוח אל תמציא אותה. בפתיח אפשר לפתוח בשאלה או בפרט מפתיע אחד שגורם לקורא להמשיך, בתנאי שהוא מופיע בחומר.
מבנה (האורך נקבע לפי החומר, בין 300 ל-1000 תווים. אל תרחיב בכוח ואל תמלא רווחים: חומר דל מקבל פוסט קצר וממוקד. כל פסקה נכנסת רק אם יש לה תוכן אמיתי):
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


def story_material(cands, idxs):
    parts = []
    for i in idxs:
        c = cands[i]
        art = fetch_article(c["link"])
        parts.append(f'מקור: {c["source"]}\nכותרת: {c["title"]}\nתקציר: {c["summary"]}\n' + (f"גוף הכתבה: {art}" if art else ""))
    return "\n\n---\n\n".join(parts)


SAFETY_NEWS = """כללי בטיחות ואמינות (מחייבים):
- כל טענה עובדתית מיוחסת למקור מזוהה ("לפי רויטרס", "לפי משרד הבריאות בעזה", "לפי צה"ל", "לפי אל ג'זירה"). אל תאמץ טענה של צד בעימות כעובדה.
- אין פרטים מבצעיים: מיקום או תנועת כוחות ישראליים, מועדי תקיפה, מיקומי אתרים רגישים, קואורדינטות. תנועת כוחות זרים מותרת רק אם דווחה בכמה כלי תקשורת מרכזיים, עם ייחוס, ובלי מיקום מדויק.
- אין תיאורים גרפיים של אלימות או גוויות, אין שמות של הרוגים או פצועים, אין סרטונים או תמונות לא מאומתים, ואין הסתה, השמצה או שפה מבזה כלפי קבוצה.
- מספרי נפגעים: תמיד עם מקורם, ובלי חישובים או הערכות משלך.
- אל תנחש כוונות או תוצאות. מה שלא ידוע נכתב במפורש כלא ידוע."""

SELECT_MIDEAST = """אתה עורך ראשי של MER5, עמוד חדשות ישראלי. מדור "ביטחון וטרור": ביטחון וטרור בעולם ובפרט במזרח התיכון והעולם הערבי. כולל מלחמות, פיגועים, סיכול טרור והתפתחויות צבאיות. תקבל רשימת ידיעות (כותרת, תקציר, מקור, באנגלית, עברית או ערבית).
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
{"card_title":"...","subject":"...","queries":["...","...","..."]}
- subject: משפט קצר באנגלית: מה בדיוק צריך להיות בתמונה כדי שיהיה קשר ברור לידיעה (המקום, הכלי, המבנה או הארגון המרכזי בפוסט).
- queries: עד 3 חיפושים באנגלית ב-Wikimedia Commons, מהספציפי לכללי, כולם על הישות המרכזית של הידיעה (שם המקום, המבנה, סוג הספינה/הכלי, המוסד). אם אין צילום ספציפי מתאים, השאר רשימה קצרה: עדיף בלי תמונה מאשר תמונה לא קשורה.
- card_title: כותרת חדה לכרטיס, עד 75 תווים בעברית, בלי נקודה בסוף, בלי עובדות שלא נאמרו בפוסט. מספרים חייבים להיות זהים לפוסט. כתיב מדויק לגמרי, גרשיים תקינים (צה״ל, נתב״ג).
- הצילום חייב להראות את הישות המרכזית של הידיעה (עיר, ציון דרך, בניין, נמל, סוג ספינה או כלי רכב), לא תמונת "אווירה" כללית.
  לעולם לא אנשים סובלים, חיילים בפעולה, נשק בשימוש, הרס, פצועים או פוליטיקאים."""

SELECT_FACTS = """אתה עורך ראשי של MER5, מדור "ידיעות מהעולם": עובדות, תגליות וסיפורים מעניינים באמת מהעולם: מדע, היסטוריה, טבע, טכנולוגיה, תרבויות ותופעות חריגות. תקבל רשימת כתבות.
בחר רק סיפור שגורם לקורא לומר "לא ידעתי את זה": פרט מפתיע, מספר שמעורר תדהמה, גילוי חדש או סיפור היסטורי עם תפנית. הסיפור חייב להיות מבוסס על המקור ולא על שמועה או על טענה בלי הוכחה.
דלג על: חדשות פוליטיות וביטחוניות, סלבס, ספורט, פרסומות, רשימות כלליות ("10 דברים ש..."), וסיפורים שכבר ידועים לכולם. אחד כתבות על אותו נושא. severity תמיד low, israel: true רק אם יש קשר ישיר לישראל.
עדיף רשימה ריקה. urgency (1-5): כמה דחוף להעלות עכשיו (5 = חדשה מתפרצת שמתיישנת בדקות, 3 = בתוך שעות, 1 = יכולה לחכות ימים). special (1-5): כמה הידיעה ייחודית וחשובה (5 = בלעדית או משנה תמונה, 3 = טובה, 1 = שגרתית).
החזר JSON בלבד: {"picks":[{"idxs":[0],"severity":"low","urgency":3,"special":3,"israel":false}]}   ואם אין: {"picks":[]}"""

WRITE_FACTS = WRITE_NEWS.replace("אתה כתב בכיר בערוץ חדשות ישראלי מוביל", "אתה כותב בכיר במדור ידע ותגליות של ערוץ חדשות ישראלי מוביל") + """

מדור עובדות: הפתיח הוא הקרס. פתח בשאלה או בפרט מפתיע אחד מהחומר. הסיפור מסופר בקצב טוב, עם "לשם השוואה" מדויקת כשיש נתון בטוח. דיוק עובדתי מעל הכול: כל מספר, שנה ושם מהמקור בלבד."""

SELECT_BY = {"facts": SELECT_FACTS, "aviation": SELECT_SYSTEM, "security": SELECT_MIDEAST, "politics": SELECT_WORLD}
WRITE_BY = {"facts": WRITE_FACTS, "aviation": WRITE_SYSTEM, "security": WRITE_NEWS, "politics": WRITE_NEWS}
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


def build_posts(cands, topic="aviation"):
    lines = [f'[{i}] ({c["source"]}) {c["title"]}\n{c["summary"]}' for i, c in enumerate(cands)]
    picks = parse_picks(claude_json(SELECT_BY[topic], "\n\n".join(lines), MODEL, 2000), len(cands))
    review = REVIEW_SYSTEM if topic == "aviation" else REVIEW_NEWS
    posts = []
    for pk in picks[:MAX_POSTS_PER_RUN]:
        try:
            material = story_material(cands, pk["idxs"])
            w = claude_json(WRITE_BY[topic], material, WRITER_MODEL)
            draft = (w.get("text") or "").strip()
            if not draft and len(w.get("_raw", "")) > 150 and "{" not in w["_raw"][:5]:
                draft = w["_raw"].strip()  # המודל החזיר טקסט חופשי
            if not draft:
                continue
            rev = claude_json(review, f"חומר גלם:\n{material}\n\nטיוטה:\n{draft}", WRITER_MODEL)
            text = (rev.get("text") or draft).strip()
            if rev.get("issues"):
                print("review fixes:", rev["issues"])
            pol = claude_json(HEBREW_SYSTEM, text, WRITER_MODEL)
            text = (pol.get("text") or text).strip()
            posts.append({**pk, "text": clean_hebrew(text).replace("\n", "\n\n", 1) if "\n\n" not in clean_hebrew(text) else clean_hebrew(text)})
        except Exception as e:  # noqa: BLE001
            print("story failed:", type(e).__name__, str(e)[:200])
    return posts


IMAGE_AVIATION = """אתה עורך תמונות בעמוד חדשות תעופה. תקבל פוסט בעברית. החזר JSON בלבד:
{"card_title":"...","subject":"...","queries":["...","...","..."]}
- subject: משפט קצר באנגלית: מה בדיוק צריך להיות בתמונה (סוג המטוס, החברה או שדה התעופה המרכזיים בפוסט).
- queries: עד 3 חיפושים באנגלית ב-Wikimedia Commons, מהספציפי לכללי (למשל "LOT Polish Airlines Boeing 737 MAX", "Boeing 737 airliner"), תמיד על סוג המטוס/החברה/שדה התעופה בפוסט. אם אין מידע ספציפי, עדיף רשימה קצרה מתמונה לא קשורה.
- card_title: כותרת חדה לתמונה, עד 75 תווים בעברית, בלי נקודה בסוף, שונה מעט מכותרת הפוסט ולא מוסיפה עובדות שלא נאמרו בפוסט. מספרים חייבים להיות זהים לפוסט. כתיב מדויק לגמרי, גרשיים תקינים (צה״ל, נתב״ג).
- אם הפוסט אינו מזכיר סוג מטוס או חברה מסוימים, השתמש בשאילתה כללית על סוג הכלי.
"""


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


def make_image(pid, text, topic="aviation"):
    """מחזיר (נתיב תמונה, קרדיט או None). צילום רק אם נבדק שהוא קשור לידיעה, אחרת כרטיס טיפוגרפי נקי."""
    tp = load_topics()[topic]
    meta = claude_json(IMAGE_AVIATION if topic == "aviation" else IMAGE_NEWS, text, MODEL, 700)
    title = (meta.get("card_title") or text.split("\n", 1)[0]).strip()
    subject = meta.get("subject") or title
    queries = [q for q in (meta.get("queries") or []) if isinstance(q, str)][:3]
    photo, credit = None, None
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
    path = imgcard.render_card(title, os.path.join("images", f"{pid}.jpg"), photo, credit, tp["color"], tp["label"], tp.get("en", ""))
    return path, credit


def fetch_and_propose(st, topic):
    tp = load_topics()[topic]
    cands = fetch_candidates(st, topic)
    print(f"[{topic}] candidates: {len(cands)}")
    if not cands:
        return
    posts = build_posts(cands, topic)  # אם נכשל, לא מסמנים כנראה, ננסה שוב בריצה הבאה
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st["daily"].get("date") != today or not isinstance(st["daily"].get("count"), dict):
        st["daily"] = {"date": today, "count": {}}
    sample = os.environ.get("SAMPLE") == "1"
    sent = 0
    for p in posts:
        if sent >= MAX_POSTS_PER_RUN or (not sample and st["daily"]["count"].get(topic, 0) >= tp.get("daily_cap", 4)):
            break
        links = [cands[i]["link"] for i in p["idxs"]]
        n_sources = len({cands[i]["source"] for i in p["idxs"]})
        pid = hashlib.sha1("".join(links).encode()).hexdigest()[:10]
        img_path, credit = None, None
        try:
            img_path, credit = make_image(pid, p["text"], topic)
            tg_photo(img_path, "🖼 תמונה לפוסט" + (f"\n📷 {credit}" if credit else "\nכרטיס טיפוגרפי, בלי צילום"))
        except Exception as e:  # noqa: BLE001
            print("image failed:", type(e).__name__, str(e)[:150])
            img_path = None
        send_proposal(pid, p["text"], links, p["severity"], n_sources, p.get("israel", False), topic, p.get("urgency", 3), p.get("special", 3))
        st["pending"][pid] = {
            "text": p["text"],
            "links": links,
            "severity": p["severity"],
            "topic": topic,
            "image": img_path,
            "credit": credit,
            "ts": time.time(),
        }
        sent += 1
        if not sample:
            st["daily"]["count"][topic] = st["daily"]["count"].get(topic, 0) + 1
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


def publish_approved(st):
    """מפרסם לעמוד פייסבוק פוסטים שאושרו. פועל רק אם הוגדרו FB_PAGE_ID ו-FB_PAGE_TOKEN."""
    page, token = os.environ.get("FB_PAGE_ID"), os.environ.get("FB_PAGE_TOKEN")
    if not page or not token:
        return
    token = page_token(page, token)
    for item in st["approved"]:
        if item.get("published") or item.get("fails", 0) >= 2:
            continue
        caption = item["text"] + ("\n\n📷 צילום ארכיון: " + item["credit"] if item.get("credit") else "")
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
                raise RuntimeError(str((data.get("error") or {}).get("message", "unknown"))[:150])
            # מקורות בתגובה הראשונה, כדי לא לפגוע בחשיפה
            try:
                requests.post(f"https://graph.facebook.com/{FB_VERSION}/{fb_id}/comments", data={"message": "מקורות:\n" + "\n".join(item["links"]), "access_token": token}, timeout=40)
            except Exception:  # noqa: BLE001
                pass
            item["published"] = True
            item["fb_id"] = fb_id
            item["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            tg("sendMessage", chat_id=_chat_id(), text=f"📣 פורסם בפייסבוק\n{item['text'].splitlines()[0]}")
        except Exception as e:  # noqa: BLE001
            print("publish failed:", type(e).__name__, str(e)[:150])
            item["fails"] = item.get("fails", 0) + 1
            tg("sendMessage", chat_id=_chat_id(), text="⚠️ הפרסום לפייסבוק נכשל: " + (str(e)[:150] if isinstance(e, RuntimeError) else type(e).__name__))


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
            run("git", "pull", "--rebase")
            run("git", "push")
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
    elif mode in ("run", "sample"):
        if mode == "sample":
            os.environ["SAMPLE"] = "1"  # מתעלם מ"נראו כבר" ובודק 72 שעות אחורה
        process_updates(st)
        try:
            publish_approved(st)
            publish_queue(st)
            for topic in load_topics():
                if os.environ.get("ONLY_TOPIC") and topic != os.environ["ONLY_TOPIC"]:
                    continue
                t0 = time.time()
                try:
                    fetch_and_propose(st, topic)
                except Exception as e:  # noqa: BLE001
                    print(f"[{topic}] failed:", type(e).__name__, str(e)[:200])
                print(f"[{topic}] took {int(time.time() - t0)}s")
                checkpoint(st)
        finally:
            save_state(st)
        return
    else:
        sys.exit("מצב לא מוכר")
    save_state(st)


if __name__ == "__main__":
    main()
