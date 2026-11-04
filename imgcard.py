"""יצירת תמונת פוסט: צילום ארכיון בר-רישיון מ-Wikimedia Commons + כרטיס חדשות ממותג, או כרטיס טיפוגרפי כשאין צילום."""
import html
import io
import os
import re

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, features

W, H = 1080, 1350
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_BOLD = os.path.join(HERE, "fonts", "FreeSansBold.ttf")
FONT_FALLBACK = os.path.join(HERE, "fonts", "DejaVuSans-Bold.ttf")
PAGE_TAG = os.environ.get("PAGE_TAG", "חדשות תעופה")
ACCENT = (230, 57, 70)
UA = "news-bot/1.0 (aviation news page; contact via GitHub repo)"
OK_LICENSE = re.compile(r"^(cc[ -]by(?!-n)|cc[ -]by-sa|cc0|public domain|pd)", re.I)
BAD_TITLE = re.compile(r"(diagram|logo|map|schematic|cockpit|interior|cabin|seat|crash|wreck|accident|svg|drawing|livery sketch)", re.I)

USE_RAQM = features.check("raqm")
try:
    from bidi.algorithm import get_display  # noqa: F401
except Exception:  # noqa: BLE001
    get_display = None


def _font(size):
    path = FONT_BOLD if os.path.exists(FONT_BOLD) else FONT_FALLBACK
    return ImageFont.truetype(path, size)


def _shape(text):
    """מחזיר טקסט מוכן לציור ב-RTL לפי המנוע הזמין."""
    if USE_RAQM or get_display is None:
        return text
    return get_display(text)


def _kw():
    return {"direction": "rtl", "language": "he"} if USE_RAQM else {}


def _width(draw, text, font):
    return draw.textlength(_shape(text), font=font, **_kw())


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if _width(draw, trial, font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------- commons
AVIATION = re.compile(r"(aircraft|airplane|aeroplane|airliner|helicopter|boeing|airbus|embraer|atr|bombardier|cessna|jet|airline|airways|airport|aviation|avion|cargo|flight)", re.I)
STOP = {"the", "and", "for", "with", "from", "airlines", "aircraft"}


def _relevance(page, query):
    """ציון התאמה: חייב להיות קשור לתעופה וחייב להכיל לפחות חצי ממילות החיפוש הייחודיות."""
    info = (page.get("imageinfo") or [{}])[0]
    desc = _strip(info.get("extmetadata", {}).get("ImageDescription", {}).get("value"))
    cats = " ".join(c.get("title", "") for c in page.get("categories", []))
    hay = f'{page.get("title", "")} {desc} {cats}'.lower()
    if not AVIATION.search(hay):
        return 0
    toks = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) >= 3 and t not in STOP]
    if not toks:
        return 1
    hits = sum(1 for t in toks if t in hay)
    return hits if hits >= max(1, (len(toks) + 1) // 2) else 0

def _strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def commons_photo(query):
    """מחזיר (PIL.Image, credit_text) או None. רק רישיונות פתוחים עם שיוך."""
    if not query:
        return None
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 20,
        "prop": "imageinfo|categories", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600, "cllimit": "max",
    }
    r = requests.get("https://commons.wikimedia.org/w/api.php", params=params, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    pages = sorted((r.json().get("query", {}).get("pages", {})).values(), key=lambda p: p.get("index", 99))
    scored = []
    for p in pages:
        scored.append((_relevance(p, query), p.get("index", 99), p))
    scored = [x for x in scored if x[0] > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    for _, _, p in scored:
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        lic = _strip(meta.get("LicenseShortName", {}).get("value"))
        if info.get("mime") != "image/jpeg" or not OK_LICENSE.match(lic):
            continue
        if BAD_TITLE.search(p.get("title", "")) or info.get("width", 0) < 1200:
            continue
        if _strip(meta.get("Restrictions", {}).get("value")):
            continue
        url = info.get("thumburl") or info.get("url")
        try:
            img = Image.open(io.BytesIO(requests.get(url, headers={"User-Agent": UA}, timeout=40).content)).convert("RGB")
        except Exception:  # noqa: BLE001
            continue
        artist = _strip(meta.get("Artist", {}).get("value")) or "Unknown author"
        credit = f"{artist[:60]} / Wikimedia Commons / {lic}"
        return img, credit
    return None


# ----------------------------------------------------------------- render
def _cover(img, w, h):
    s = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def _gradient_bg():
    bg = Image.new("RGB", (W, H))
    px = bg.load()
    for y in range(H):
        t = y / H
        c = (int(12 + 10 * t), int(22 + 14 * t), int(40 + 22 * t))
        for x in range(W):
            px[x, y] = c
    return bg


def render_card(title, out_path, photo=None, credit=None):
    if photo is not None:
        base = ImageEnhance.Brightness(_cover(photo, W, H)).enhance(0.85)
    else:
        base = _gradient_bg()
    base = base.convert("RGBA")
    # שכבת הכהיה מדורגת מלמטה לקריאות הכותרת
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    start = int(H * 0.30)
    for y in range(start, H):
        a = int(235 * ((y - start) / (H - start)) ** 1.5)
        od.line([(0, y), (W, y)], fill=(5, 10, 20, a))
    base = Image.alpha_composite(base, ov)
    d = ImageDraw.Draw(base)

    margin, max_w = 70, W - 140
    # כותרת: מקטינים גופן עד שנכנסת ב-5 שורות לכל היותר
    for size in range(86, 47, -4):
        f = _font(size)
        lines = _wrap(d, title, f, max_w)
        if len(lines) <= 5:
            break
    lh = int(size * 1.28)
    block_h = lh * len(lines)
    bottom = H - (150 if photo is not None else 130)
    y = bottom - block_h
    d.rectangle([W - margin - 110, y - 36, W - margin, y - 28], fill=ACCENT)  # פס הדגשה
    for ln in lines:
        d.text((W - margin, y), _shape(ln), font=f, fill=(255, 255, 255), anchor="ra", **_kw())
        y += lh

    # תג עליון
    tf = _font(40)
    tw = _width(d, PAGE_TAG, tf)
    d.rounded_rectangle([W - margin - tw - 56, 62, W - margin, 62 + 74], radius=14, fill=ACCENT)
    d.text((W - margin - 28, 62 + 37), _shape(PAGE_TAG), font=tf, fill=(255, 255, 255), anchor="rm", **_kw())

    # שורת תחתית
    sf = _font(28)
    if photo is not None:
        d.text((W - margin, H - 92), _shape("צילום ארכיון"), font=sf, fill=(220, 225, 235), anchor="ra", **_kw())
        if credit:
            cf = ImageFont.truetype(FONT_FALLBACK if os.path.exists(FONT_FALLBACK) else FONT_BOLD, 21)
            d.text((margin, H - 58), credit[:95], font=cf, fill=(190, 198, 212), anchor="la")
    out = base.convert("RGB")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    out.save(out_path, "JPEG", quality=90)
    return out_path
