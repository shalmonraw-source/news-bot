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
PAGE_TAG = os.environ.get("PAGE_TAG", "MER5")
ACCENT = (230, 57, 70)
UA = "news-bot/1.0 (aviation news page; contact via GitHub repo)"
OK_LICENSE = re.compile(r"^(cc[ -]by(?!-n)|cc[ -]by-sa|cc0|public domain|pd)", re.I)
BAD_TITLE = re.compile(r"(diagram|logo|map|schematic|cockpit|interior|cabin|seat|crash|wreck|accident|svg|drawing|livery sketch)", re.I)
BAD_NEWS = re.compile(r"(dead|killed|victim|corpse|wounded|injur|blood|explosion|bomb|rocket|missile|strike|destroy|ruin|rubble|riot|soldier|troops|fighter|militia|hostage|funeral|refugee|prison|torture|execution|massacre|portrait|politician|minister|president|diagram|logo|map|svg|drawing)", re.I)

USE_RAQM = features.check("raqm")
try:
    from bidi.algorithm import get_display  # noqa: F401
except Exception:  # noqa: BLE001
    get_display = None


G = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
# שם -> (כתובת, האם גופן משתנה)
FONTS = {
    "Heebo": (G + "heebo/Heebo%5Bwght%5D.ttf", True),
    "Assistant": (G + "assistant/Assistant%5Bwght%5D.ttf", True),
    "Rubik": (G + "rubik/Rubik%5Bwght%5D.ttf", True),
    "Frank Ruhl Libre": (G + "frankruhllibre/FrankRuhlLibre%5Bwght%5D.ttf", True),
    "Secular One": (G + "secularone/SecularOne-Regular.ttf", False),
    "Suez One": (G + "suezone/SuezOne-Regular.ttf", False),
    "Alef": (G + "alef/Alef-Bold.ttf", False),
    "Miriam Libre": (G + "miriamlibre/MiriamLibre%5Bwght%5D.ttf", True),
    "Noto Sans Hebrew": (G + "notosanshebrew/NotoSansHebrew%5Bwdth%2Cwght%5D.ttf", True),
}
_CURRENT = {"name": None}


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def ensure_font(name, folder="fonts"):
    """מוריד את הגופן אם חסר ומחזיר נתיב, או None אם נכשל."""
    if name not in FONTS:
        return None
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, _slug(name) + ".ttf")
    if not os.path.exists(path):
        r = requests.get(FONTS[name][0], headers={"User-Agent": UA}, timeout=40)
        if r.status_code != 200 or len(r.content) < 5000:
            return None
        with open(path, "wb") as f:
            f.write(r.content)
    return path


def use_font(name, folder="fonts"):
    try:
        path = ensure_font(name, folder)
    except Exception:  # noqa: BLE001
        path = None
    _CURRENT["name"] = name if path else None
    _CURRENT["path"] = path
    return bool(path)


def _font(size, path=None, variable=None):
    path = path or _CURRENT.get("path") or (FONT_BOLD if os.path.exists(FONT_BOLD) else FONT_FALLBACK)
    f = ImageFont.truetype(path, size)
    name = _CURRENT.get("name")
    is_var = FONTS[name][1] if (name in FONTS and path == _CURRENT.get("path")) else False
    if variable is not None:
        is_var = variable
    if is_var:
        try:
            axes = f.get_variation_axes()
            vals = [700 if a.get("name") in (b"Weight", "Weight") or i == 0 else a.get("default", a["minimum"]) for i, a in enumerate(axes)]
            f.set_variation_by_axes(vals)
        except Exception:  # noqa: BLE001
            pass
    return f


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


def _relevance(page, query, aviation=True):
    """ציון התאמה: חייב להיות קשור לתעופה וחייב להכיל לפחות חצי ממילות החיפוש הייחודיות."""
    info = (page.get("imageinfo") or [{}])[0]
    desc = _strip(info.get("extmetadata", {}).get("ImageDescription", {}).get("value"))
    cats = " ".join(c.get("title", "") for c in page.get("categories", []))
    hay = f'{page.get("title", "")} {desc} {cats}'.lower()
    if aviation and not AVIATION.search(hay):
        return 0
    if not aviation and BAD_NEWS.search(hay):
        return 0
    toks = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) >= 3 and t not in STOP]
    if not toks:
        return 1
    hits = sum(1 for t in toks if t in hay)
    return hits if hits >= max(1, (len(toks) + 1) // 2) else 0

def _strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def commons_photo(query, aviation=True):
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
        scored.append((_relevance(p, query, aviation), p.get("index", 99), p))
    scored = [x for x in scored if x[0] > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    for _, _, p in scored:
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        lic = _strip(meta.get("LicenseShortName", {}).get("value"))
        if info.get("mime") != "image/jpeg" or not OK_LICENSE.match(lic):
            continue
        if (aviation and BAD_TITLE.search(p.get("title", ""))) or info.get("width", 0) < 1200:
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


def render_card(title, out_path, photo=None, credit=None, color=(180, 28, 36), label=""):
    """כרטיס חדשות: בלוק כותרת צבעוני למעלה, צילום מתחתיו (או רקע כהה כשאין צילום)."""
    margin, max_w = 64, W - 128
    scratch = ImageDraw.Draw(Image.new("RGB", (W, 10)))
    for size in range(84, 47, -4):
        f = _font(size)
        lines = _wrap(scratch, title, f, max_w)
        if len(lines) <= 4:
            break
    lh = int(size * 1.25)
    chip_h = 56
    banner_h = 34 + chip_h + 26 + lh * len(lines) + 40
    color = tuple(color)

    canvas = Image.new("RGB", (W, H), (10, 18, 32))
    if photo is not None:
        ph = ImageEnhance.Brightness(_cover(photo, W, H - banner_h)).enhance(0.95)
        canvas.paste(ph, (0, banner_h))
    else:
        bg = _gradient_bg().crop((0, banner_h, W, H))
        canvas.paste(bg, (0, banner_h))
    d = ImageDraw.Draw(canvas)
    d.rectangle([0, 0, W, banner_h], fill=color)
    d.rectangle([0, banner_h, W, banner_h + 8], fill=tuple(max(0, c - 55) for c in color))

    # תג: MER5 · קטגוריה
    tf = _font(34)
    tag = PAGE_TAG + ("  |  " + label if label else "")
    tw = _width(d, tag, tf)
    d.rounded_rectangle([W - margin - tw - 44, 34, W - margin, 34 + chip_h], radius=12, fill=(255, 255, 255))
    d.text((W - margin - 22, 34 + chip_h // 2), _shape(tag), font=tf, fill=color, anchor="rm", **_kw())

    y = 34 + chip_h + 26
    for ln in lines:
        d.text((W - margin, y), _shape(ln), font=f, fill=(255, 255, 255), anchor="ra", **_kw())
        y += lh

    if photo is not None:
        strip = Image.new("RGBA", (W, 74), (0, 0, 0, 150))
        canvas.paste(strip, (0, H - 74), strip)
        sf = _font(26)
        d = ImageDraw.Draw(canvas)
        d.text((W - margin, H - 37), _shape("צילום ארכיון"), font=sf, fill=(235, 238, 245), anchor="rm", **_kw())
        if credit:
            cf = ImageFont.truetype(FONT_FALLBACK if os.path.exists(FONT_FALLBACK) else FONT_BOLD, 20)
            d.text((margin - 20, H - 37), credit[:80], font=cf, fill=(205, 212, 225), anchor="lm")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    canvas.save(out_path, "JPEG", quality=90)
    return out_path


def font_sheet(out_path, folder="fonts_cache"):
    """דף השוואה של כל הגופנים עם אותה כותרת. מחזיר רשימת (מספר, שם) שנטענו בהצלחה."""
    title = "תייר נהרג בהתנגשות שני מסוקים מעל מנחת בגובה 4,200 מטר בנפאל"
    cell_w, cell_h, cols = 540, 675, 2
    done = []
    cells = []
    for name in FONTS:
        if not use_font(name, folder):
            continue
        p = f"/tmp/_font_{_slug(name)}.jpg"
        render_card(title, p)
        im = Image.open(p).resize((cell_w, cell_h), Image.LANCZOS)
        d = ImageDraw.Draw(im)
        n = len(done) + 1
        d.rectangle([0, 0, 190, 42], fill=(0, 0, 0))
        d.text((10, 8), f"{n}. {name}", font=ImageFont.truetype(FONT_FALLBACK if os.path.exists(FONT_FALLBACK) else FONT_BOLD, 22), fill=(255, 255, 255))
        cells.append(im)
        done.append((n, name))
    rows = (len(cells) + cols - 1) // cols
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), (0, 0, 0))
    for i, im in enumerate(cells):
        sheet.paste(im, ((i % cols) * cell_w, (i // cols) * cell_h))
    sheet.save(out_path, "JPEG", quality=88)
    _CURRENT.update({"name": None, "path": None})
    return done
