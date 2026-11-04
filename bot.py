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

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SOURCES_FILE = os.environ.get("SOURCES_FILE", "sources.json")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "12"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "40"))
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "3"))
DAILY_CAP = int(os.environ.get("DAILY_CAP", "8"))
PENDING_TTL_DAYS = 3
UA = "Mozilla/5.0 (compatible; news-bot/1.0)"


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    st.setdefault("seen", [])
    st.setdefault("pending", {})
    st.setdefault("approved", [])
    st.setdefault("offset", 0)
    st.setdefault("daily", {"date": "", "count": 0})
    return st


def save_state(st):
    st["seen"] = st["seen"][-3000:]
    st["approved"] = st["approved"][-300:]
    cutoff = time.time() - PENDING_TTL_DAYS * 86400
    st["pending"] = {k: v for k, v in st["pending"].items() if v.get("ts", 0) > cutoff}
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


def keyboard(pid):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ אשר", "callback_data": f"a:{pid}"},
                {"text": "❌ דחה", "callback_data": f"r:{pid}"},
            ]
        ]
    }


def send_proposal(pid, text, links, severity, n_sources, israel=False):
    sev = {"high": "🔴 חומרה גבוהה", "medium": "🟠 חומרה בינונית"}.get(severity, "🟢 חומרה נמוכה")
    verified = f"{n_sources} מקורות" if n_sources > 1 else "⚠️ מקור יחיד"
    flag = " · 🇮🇱 קשר ישראלי" if israel else ""
    msg = f"✈️ הצעה לפוסט\n{sev} · {verified}{flag}\n\n{text}\n\n" + "\n".join(f"🔗 {u}" for u in links) + "\n\n⏳ הלחיצה נרשמת בתוך עד 10 דקות"
    res = tg(
        "sendMessage",
        chat_id=_chat_id(),
        text=msg[:4000],
        reply_markup=keyboard(pid),
        link_preview_options={"is_disabled": True},
    )
    return res["message_id"]


def process_updates(st):
    """מטפל בלחיצות על כפתורי אשר/דחה שהצטברו מאז הריצה הקודמת."""
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


def fetch_candidates(st):
    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)
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
החזר JSON בלבד: {"picks":[{"idxs":[0,3],"severity":"low","israel":false}]}   ואם אין: {"picks":[]}"""

WRITE_SYSTEM = """אתה כתב תעופה ותיק בעיתון ישראלי גדול, שכותב לעמוד פייסבוק עם מאות אלפי עוקבים.
תקבל חומר גלם באנגלית או בעברית על אירוע אחד. כתוב פוסט עשיר ומובן, כזה שאדם שלא מבין בתעופה יסיים אותו ויגיד "לא ידעתי את זה".

מבנה (כ-700-1000 תווים):
1. כותרת של 5-10 מילים עם פרט קונקרטי ומפתיע. לא סיסמה. אימוג'י אחד לכל היותר.
2. הסיפור: מה קרה, איפה, מתי, למי, ומה עשו. כל הפרטים החשובים מהחומר: מספרים, שעות, סוג המטוס, החברה, המסלול, התוצאה.
3. "מה זה אומר": פסקה של 2-3 משפטים שמסבירה בשפה פשוטה את המונחים והמנגנון (למשל למה מטוס נחת דווקא שם, מה זה אומר קוד חירום, איך נראית פרוצדורה כזו), ומה המסקנה לנוסעים או לענף.
4. שורה אחרונה שמגדילה את הראש: זווית רחבה, השוואה, או שאלה שגורמת לקורא לחשוב. היא נשענת על עובדות ולא על ספקולציה.

הבחנה חשובה: עובדות על האירוע מגיעות רק מהחומר. הסברים כלליים על איך התעופה עובדת (נהלים, מונחים, טכנולוגיה מבוססת)
מותר להוסיף מהידע הכללי שלך, כל עוד הם מוסכמים ולא מכילים מספרים או טענות ספציפיות על האירוע. אל תנחש סיבות לתקלה או לתאונה.

שפה: עברית טבעית ורהוטה, בגובה העיניים, משפטים של עד 20 מילים, פעלים פעילים. אסור תרגום מילולי מאנגלית וסגנון של הודעה לעיתונות.
מונחים: מנחת, המראה, נחיתה, נחיתת חירום, תא הטייס, צוות הדיילים, מגדל הפיקוח, מסלול, מטוס נוסעים, מסוק.
בלי קלישאות ("בעולם התעופה", "הערב", "שוב"), בלי "דווח כי" יותר מפעם אחת, בלי האשטגים וקישורים, ובלי סעיפים ממוספרים בטקסט עצמו.
אירוע שלא אושר מנוסח בזהירות ("לפי הדיווחים"). מספרים ושמות בדיוק כפי שבחומר.

דוגמה לסגנון בלבד (העובדות בדויות, אל תשתמש בהן):
נחיתת חירום בלי גלגל קדמי

טייסת של מטוס נוסעים עם 96 נוסעים דיווחה למגדל הפיקוח על תקלה במנגנון ההנחתה זמן קצר אחרי ההמראה. היא חגה מעל המסלול שעה שלמה כדי לשרוף דלק, ואז נחתה על שני הגלגלים הראשיים בלבד. האף החליק על המסלול, והנוסעים יצאו במגלשות חירום. כולם בריאים.

למה הטייסים שרפו דלק לפני הנחיתה? מטוס קל יותר נוחת לאט יותר, והסיכוי לשריפה בחיכוך עם המסלול קטן. זו פרוצדורה סטנדרטית, והיא מתורגלת בסימולטור שוב ושוב.

הנחיתה הזו נראית מפחידה, אבל היא בדיוק הסיבה שטייסים מקבלים הכשרה כל חצי שנה. מתי בפעם האחרונה חשבתם על מה קורה אחרי שהמטוס נוחת?

החזר JSON בלבד: {"text":"..."}"""

REVIEW_SYSTEM = """אתה עורך לשון ובודק עובדות. תקבל חומר גלם ואת הטיוטה של פוסט בעברית.
1. בדוק כל עובדה, מספר, שם ושעה בטיוטה מול החומר. פרט שלא מופיע בחומר חייב להימחק.
2. תקן עברית ובדוק שההסברים הכלליים נכונים ומקובלים (אחרת מחק אותם): ניסוח מתורגם, מילים לא מדויקות, תחביר מגושם, פעלים סבילים מיותרים, ובעיות במינוח תעופתי.
3. שמור על המבנה, העומק והסקרנות, ואל תקצר את הפוסט. הסברים כלליים על איך התעופה עובדת מותרים, אבל פרטים ספציפיים על האירוע חייבים להיות מהחומר. עד 1100 תווים.
החזר JSON בלבד: {"text":"הנוסח הסופי","issues":["תיאור קצר של כל תיקון עובדתי שנעשה"]}"""


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
            picks.append({"idxs": sorted(set(idxs)), "severity": sev, "israel": bool(p.get("israel"))})
    return picks


def story_material(cands, idxs):
    parts = []
    for i in idxs:
        c = cands[i]
        art = fetch_article(c["link"])
        parts.append(f'מקור: {c["source"]}\nכותרת: {c["title"]}\nתקציר: {c["summary"]}\n' + (f"גוף הכתבה: {art}" if art else ""))
    return "\n\n---\n\n".join(parts)


def build_posts(cands):
    lines = [f'[{i}] ({c["source"]}) {c["title"]}\n{c["summary"]}' for i, c in enumerate(cands)]
    picks = parse_picks(claude_json(SELECT_SYSTEM, "\n\n".join(lines), MODEL, 2000), len(cands))
    posts = []
    for pk in picks[:MAX_POSTS_PER_RUN]:
        try:
            material = story_material(cands, pk["idxs"])
            w = claude_json(WRITE_SYSTEM, material, WRITER_MODEL)
            draft = (w.get("text") or "").strip()
            if not draft and len(w.get("_raw", "")) > 150 and "{" not in w["_raw"][:5]:
                draft = w["_raw"].strip()  # המודל החזיר טקסט חופשי
            if not draft:
                continue
            rev = claude_json(REVIEW_SYSTEM, f"חומר גלם:\n{material}\n\nטיוטה:\n{draft}", WRITER_MODEL)
            text = (rev.get("text") or draft).strip()
            if rev.get("issues"):
                print("review fixes:", rev["issues"])
            posts.append({**pk, "text": text})
        except Exception as e:  # noqa: BLE001
            print("story failed:", type(e).__name__, str(e)[:200])
    return posts


def fetch_and_propose(st):
    cands = fetch_candidates(st)
    print(f"candidates: {len(cands)}")
    if not cands:
        return
    posts = build_posts(cands)  # אם נכשל, לא מסמנים כנראה, ננסה שוב בריצה הבאה
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st["daily"]["date"] != today:
        st["daily"] = {"date": today, "count": 0}
    sent = 0
    for p in posts:
        if sent >= MAX_POSTS_PER_RUN or st["daily"]["count"] >= DAILY_CAP:
            break
        links = [cands[i]["link"] for i in p["idxs"]]
        n_sources = len({cands[i]["source"] for i in p["idxs"]})
        pid = hashlib.sha1("".join(links).encode()).hexdigest()[:10]
        send_proposal(pid, p["text"], links, p["severity"], n_sources, p.get("israel", False))
        st["pending"][pid] = {
            "text": p["text"],
            "links": links,
            "severity": p["severity"],
            "ts": time.time(),
        }
        sent += 1
        st["daily"]["count"] += 1
    st["seen"].extend(c["h"] for c in cands)
    print(f"proposals sent: {sent}")


# ----------------------------------------------------------------- main
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    st = load_state()
    if mode == "test":
        pid = "test" + str(int(time.time()))[-6:]
        send_proposal(pid, "בדיקת מערכת\nאם אתה רואה את ההודעה הזו והכפתורים, החיבור לטלגרם עובד.", ["https://github.com"], "low", 2)
        st["pending"][pid] = {"text": "test", "links": [], "severity": "low", "ts": time.time()}
    elif mode in ("run", "sample"):
        if mode == "sample":
            os.environ["SAMPLE"] = "1"  # מתעלם מ"נראו כבר" ובודק 72 שעות אחורה
        process_updates(st)
        try:
            fetch_and_propose(st)
        finally:
            save_state(st)
        return
    else:
        sys.exit("מצב לא מוכר")
    save_state(st)


if __name__ == "__main__":
    main()
