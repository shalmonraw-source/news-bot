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



# ------------------------------------------------------------------ logo

_FIVE = [(-24, -46), (24, -46), (24, -29), (-7, -29), (-7, -14), (6, -14), (24, 4), (24, 30), (8, 46),
         (-24, 46), (-24, 29), (3, 29), (7, 25), (7, 9), (3, 5), (-24, 5)]
# הקצה השמאלי של הגזע והקו התחתון על אותו קו (x=-24); הצורה כולה ממורכזת סביב (0,0)


def _five(d, cx, cy, h, col):
    """ספרה 5 גאומטרית בעיצוב מותאם: פינות חתוכות וקווים אחידים, ללא גופן. h = גובה הספרה."""
    k = h / 92.0
    d.polygon([(cx + x * k, cy + y * k) for x, y in _FIVE], fill=col)


def _wordmark(d, x, y_mid, cap_h, text_col, accent, font):
    """MER בגופן + ה-5 המעוצב, מיושרים לגובה אותיות הקידמה."""
    d.text((x, y_mid), "MER", font=font, fill=text_col, anchor="lm")
    wm = d.textlength("MER", font=font)
    bb = d.textbbox((x, y_mid), "M", font=font, anchor="lm")
    cap_h = bb[3] - bb[1]
    k = cap_h / 92.0
    _five(d, x + wm + 8 + 26 * k, (bb[1] + bb[3]) / 2, cap_h, accent)
    return wm + 6 + 52 * k


def logo_mark(size, rgb=(255, 255, 255), alpha=255):
    """סמל MER5: כוונת רדאר עם הספרה 5 במרכז. מצויר בהגדלה פי 4 ומוקטן לחלקות."""
    S = 4
    n = size * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    col = tuple(rgb) + (255,)
    import math
    c, r, wd = n / 2, n * 0.385, n * 0.075
    gap = 13
    for a0 in (0, 90, 180, 270):  # ארבע קשתות זהות, כל אחת כפוליגון מחושב, סימטריה מדויקת
        angs = [a0 + gap + (90 - 2 * gap) * k / 60 for k in range(61)]
        outer = [(c + r * math.cos(math.radians(a)), c + r * math.sin(math.radians(a))) for a in angs]
        inner = [(c + (r - wd) * math.cos(math.radians(a)), c + (r - wd) * math.sin(math.radians(a))) for a in reversed(angs)]
        d.polygon(outer + inner, fill=col)
    hw = n * 0.03  # חצי רוחב סימון כוונת
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):  # סימונים בארבעת הכיוונים
        a, b = r - n * 0.09, r + n * 0.07
        px, py = -dy, dx
        d.polygon([(c + dx * a + px * hw, c + dy * a + py * hw), (c + dx * b + px * hw, c + dy * b + py * hw),
                   (c + dx * b - px * hw, c + dy * b - py * hw), (c + dx * a - px * hw, c + dy * a - py * hw)], fill=col)
    _five(d, c, c, n * 0.40, col)
    im = im.resize((size, size), Image.LANCZOS)
    if alpha < 255:
        a = im.getchannel("A").point(lambda v: v * alpha // 255)
        im.putalpha(a)
    return im


def logo_lockup(height=160, mark_rgb=(255, 255, 255), text_rgb=(255, 255, 255), accent=(61, 139, 255), tagline=True, bg=None):
    """סמל + MER5 (החמש בצבע הדגש) + שורת משנה באנגלית."""
    m = logo_mark(height, mark_rgb)
    f = ImageFont.truetype(FONT_BOLD if os.path.exists(FONT_BOLD) else FONT_FALLBACK, int(height * 0.62))
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    wmer = probe.textlength("MER", font=f)
    w5 = probe.textlength("5", font=f)
    tf = ImageFont.truetype(FONT_BOLD if os.path.exists(FONT_BOLD) else FONT_FALLBACK, max(10, int(height * 0.15)))
    tag = "MIDDLE EAST REPORTS"
    sp = height * 0.02
    tw = sum(probe.textlength(ch, font=tf) + sp for ch in tag)
    gap = int(height * 0.14)
    total_w = int(height + gap + max(wmer + w5 + 12, tw if tagline else 0))
    out = Image.new("RGBA", (total_w + 8, height), bg or (0, 0, 0, 0))
    out.alpha_composite(m, (0, 0))
    d = ImageDraw.Draw(out)
    ty = height * (0.40 if tagline else 0.5)
    _wordmark(d, height + gap, ty, height * 0.44, tuple(text_rgb) + (255,), tuple(accent) + (255,), f)
    if tagline:
        x = height + gap
        for ch in tag:
            d.text((x, height * 0.80), ch, font=tf, fill=(150, 156, 166, 255), anchor="lm")
            x += probe.textlength(ch, font=tf) + sp
    return out


def _watermark(canvas, x, y, size=44, shadow=False):
    """סימן מים עדין: הסמל בלבד, קטן ושקוף בפינת התמונה."""
    mark = logo_mark(size, (255, 255, 255), 120)
    canvas.paste(mark, (x, y), mark)

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


BG_TOP, BG_BOT, PANEL = (22, 22, 23), (11, 11, 12), (27, 27, 29)
WHITE, MUTED = (244, 246, 248), (150, 156, 166)


def _pattern(accent, S=2):
    """רקע דק: רשת נקודות, טבעות מכ"ם וקווים אלכסוניים. מצויר בהגדלה פי 2 ומוקטן לחלקות."""
    w, h = W * S, H * S
    bg = Image.new("RGBA", (w, h))
    px = ImageDraw.Draw(bg)
    for y in range(0, h, 4):  # גרדיאנט אנכי
        t = y / h
        px.rectangle([0, y, w, y + 4], fill=tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)) + (255,))
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for x in range(36 * S, w, 44 * S):  # רשת נקודות
        for y in range(36 * S, h, 44 * S):
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 255, 255, 16))
    cx, cy = int(w * 0.95), int(h * 0.12)  # טבעות מכ"ם
    for r in range(260 * S, 1500 * S, 170 * S):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=accent + (17,), width=2 * S)
    for k in range(-6, 12):  # אלכסונים דקים מאוד
        x0 = k * 260 * S
        d.line([(x0, h), (x0 + 1500 * S, h - 866 * S)], fill=(255, 255, 255, 5), width=S)
    return Image.alpha_composite(bg, ov).convert("RGB").resize((W, H), Image.LANCZOS)


def _brackets(d, box, color, ln=54, wd=7):
    x0, y0, x1, y1 = box
    for (x, y, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.line([(x, y), (x + sx * ln, y)], fill=color, width=wd)
        d.line([(x, y), (x, y + sy * ln)], fill=color, width=wd)


def render_card(title, out_path, photo=None, credit=None, color=(61, 139, 255), label="", en_label=""):
    """כרטיס MER5: רקע אפור כהה עם פטרן, מסגרת צילום עם סימני פינה בצבע הנושא, כותרת גדולה למטה."""
    accent = tuple(color)
    canvas = _pattern(accent)
    d = ImageDraw.Draw(canvas)
    m = 56
    ph_box = (m, 150, W - m, 780)

    # --- שורת מותג עליונה
    canvas.paste(logo_mark(64, accent), (m, 56), logo_mark(64, accent))
    bf = _font(46)
    _wordmark(d, m + 80, 88, 40, WHITE, accent, bf)
    tf = _font(30)
    tag = label or PAGE_TAG
    tw = _width(d, tag, tf)
    d.rectangle([0, 0, W, 10], fill=accent)
    d.rounded_rectangle([W - m - tw - 44, 62, W - m, 62 + 52], radius=8, fill=accent)
    d.text((W - m - 22, 88), _shape(tag), font=tf, fill=(12, 12, 13), anchor="rm", **_kw())

    # --- אזור התמונה
    pw, phh = ph_box[2] - ph_box[0], ph_box[3] - ph_box[1]
    if photo is not None:
        im = _cover(photo, pw, phh)
        im = ImageEnhance.Color(im).enhance(0.82)
        im = ImageEnhance.Contrast(im).enhance(1.06)
        im = ImageEnhance.Brightness(im).enhance(0.9)
        tint = Image.new("RGB", im.size, accent)
        im = Image.blend(im, tint, 0.07)
        fade = Image.new("RGBA", im.size, (0, 0, 0, 0))
        fd = ImageDraw.Draw(fade)
        for y in range(phh // 2, phh):
            fd.line([(0, y), (pw, y)], fill=(8, 9, 11, int(170 * ((y - phh // 2) / (phh // 2)) ** 1.6)))
        im = Image.alpha_composite(im.convert("RGBA"), fade).convert("RGB")
    else:
        im = Image.new("RGB", (pw, phh), PANEL)
        pd = ImageDraw.Draw(im)
        for r in range(120, 1000, 120):
            pd.ellipse([pw // 2 - r, phh // 2 - r, pw // 2 + r, phh // 2 + r], outline=tuple(int(c * 0.35 + 30 * 0.65) for c in accent), width=2)
        wf = _font(150)
        pd.text((pw // 2, phh - 70), en_label or "MER5", font=wf, fill=(44, 48, 56), anchor="mm")
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw, phh], radius=22, fill=255)
    canvas.paste(im, (ph_box[0], ph_box[1]), mask)
    if photo is not None:
        _watermark(canvas, ph_box[2] - 30 - 44, ph_box[1] + 28)
    else:
        big = logo_mark(330, accent, 40)
        canvas.paste(big, (ph_box[0] + pw // 2 - 165, ph_box[1] + phh // 2 - 165), big)
    d = ImageDraw.Draw(canvas)
    _brackets(d, (ph_box[0] - 10, ph_box[1] - 10, ph_box[2] + 10, ph_box[3] + 10), accent)

    # --- כותרת
    margin, max_w = m + 8, W - 2 * (m + 8)
    scratch = ImageDraw.Draw(Image.new("RGB", (W, 10)))
    for size in range(80, 47, -4):
        f = _font(size)
        lines = _wrap(scratch, title, f, max_w)
        if len(lines) <= 4:
            break
    lh = int(size * 1.26)
    y = 830
    d.rectangle([W - margin - 120, y, W - margin, y + 8], fill=accent)
    y += 34
    for ln in lines:
        d.text((W - margin, y), _shape(ln), font=f, fill=WHITE, anchor="ra", **_kw())
        y += lh

    # --- תחתית
    d.line([(m, H - 84), (W - m, H - 84)], fill=(60, 65, 74), width=2)
    sf = _font(24)
    if photo is not None:
        d.text((W - m, H - 46), _shape("צילום ארכיון"), font=sf, fill=MUTED, anchor="rm", **_kw())
        if credit:
            cf = ImageFont.truetype(FONT_FALLBACK if os.path.exists(FONT_FALLBACK) else FONT_BOLD, 19)
            d.text((m, H - 46), credit[:78], font=cf, fill=MUTED, anchor="lm")
    else:
        d.text((m, H - 46), "MER5 · חדשות מהמזרח התיכון והעולם", font=sf, fill=MUTED, anchor="lm") if False else None
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    canvas.save(out_path, "JPEG", quality=92)
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
