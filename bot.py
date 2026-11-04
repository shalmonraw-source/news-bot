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
    msg = f"✈️ הצעה לפוסט\n{sev} · {verified}{flag}\n\n{text}\n\n" + "\n".join(f"🔗 {u}" for u in links)
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
SYSTEM = """אתה עורך ראשי של עמוד חדשות תעופה פופולרי בעברית, לקהל ישראלי רחב (לא רק חובבי תעופה).
תקבל רשימת ידיעות (כותרת, תקציר, מקור). המטרה: פוסטים שאנשים ירצו לקרוא, לשתף ולעקוב אחריהם.

מה לבחור (רק אם יש בזה משהו באמת יוצא דופן):
- תקריות ואירועים דרמטיים או מוזרים: נחיתות חירום, תקלות חריגות, כמעט-התנגשויות, מקרים נדירים, שיבושים גדולים.
- מקרים עם "וואו": שיאים, סיפורים אנושיים, פרטים מפתיעים, ציוד חריג.
- קשר ישראלי חזק: אל על, ישראייר, ארקיע, נתב"ג, ישראלים שנתקעו, קווים לישראל, סגירות מרחב אווירי באזור. אירוע כזה מקבל עדיפות.
מה לדלג עליו תמיד: הזמנות מטוסים, דוחות כספיים, מינויים, מגעים עסקיים, פתיחת קווים שגרתית, כתבות דעה ורשימות,
פרסומות, ופעילות צבאית או ביטחונית עם פרטי מיקום או מבצעים. עדיף להחזיר רשימה ריקה מאשר פוסט בינוני.

סגנון הכתיבה (קריטי):
- עברית רהוטה וטבעית, כמו עיתונאי אנושי ושנון. לא תרגום מילולי מאנגלית, לא סגנון של בוט או הודעה לעיתונות.
- שורה ראשונה: כותרת קצרה וחדה שמסקרנת בלי לשקר (פרט קונקרטי ולא סיסמה). אחריה 2-4 משפטים קצרים בקצב טוב.
- פתח בדבר המעניין ביותר. הוסף פרט אחד מפתיע או הקשר שמסביר למה זה חשוב.
- אפשר לסיים במשפט שמשאיר את הקורא עם מחשבה או מתח, אבל רק אם הוא נשען על עובדה מהידיעה.
- בלי "דווח כי" בכל משפט: השתמש בו רק פעם אחת כשהאירוע לא מאושר. בלי קלישאות ("בעולם התעופה", "הערב", "שוב").
- עד 450 תווים. אימוג'י אחד לכל היותר, רק בכותרת ורק אם זה מוסיף. בלי האשטגים ובלי קישורים בטקסט.

דיוק (לא מתפשרים): להסתמך אך ורק על מה שכתוב בידיעות. לא להוסיף פרטים, מספרים, סיבות או נפגעים שלא נכתבו.
אם כמה ידיעות עוסקות באותו אירוע, אחד לפוסט אחד וציין את כל האינדקסים ב-idxs.
severity: high לאירוע חירום או סכנה, medium לתקלה או שיבוש משמעותי, low לשאר. israel: true אם יש קשר ישראלי ישיר.

החזר JSON בלבד, בלי טקסט נוסף:
{"posts":[{"idxs":[0,3],"text":"...","severity":"low","israel":false}]}
אם אין שום דבר ראוי: {"posts":[]}"""


def ask_claude(cands):
    import anthropic

    lines = []
    for i, c in enumerate(cands):
        lines.append(f'[{i}] ({c["source"]}) {c["title"]}\n{c["summary"]}')
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=SYSTEM,
        messages=[{"role": "user", "content": "\n\n".join(lines)}],
    )
    return parse_posts("".join(b.text for b in resp.content if b.type == "text"), len(cands))


def parse_posts(raw, n):
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("לא התקבל JSON")
    data = json.loads(raw[a : b + 1])
    posts = []
    for p in data.get("posts", []):
        idxs = [i for i in p.get("idxs", []) if isinstance(i, int) and 0 <= i < n]
        text = (p.get("text") or "").strip()
        if idxs and text:
            sev = p.get("severity") if p.get("severity") in ("low", "medium", "high") else "low"
            posts.append({"idxs": sorted(set(idxs)), "text": text, "severity": sev, "israel": bool(p.get("israel"))})
    return posts


def fetch_and_propose(st):
    cands = fetch_candidates(st)
    print(f"candidates: {len(cands)}")
    if not cands:
        return
    posts = ask_claude(cands)  # אם נכשל, לא מסמנים כנראה, ננסה שוב בריצה הבאה
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
