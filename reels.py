"""בניית רילס אנכי (1080x1920) מכרטיס הידיעה: רקע מטושטש, כרטיס במרכז, זום איטי ואודיו אווירתי מסונתז (ללא זכויות יוצרים)."""
import os
import subprocess

from PIL import Image, ImageEnhance, ImageFilter

RW, RH = 1080, 1920
DUR = 9

# אודיו מסתורי מסונתז: דרון נמוך, הרמוניה מתנדנדת ופעימה איטית, עם כניסה ויציאה הדרגתיות
AUDIO = (
    "aevalsrc="
    "'0.22*sin(2*PI*55*t)+0.14*sin(2*PI*82.5*t+0.8*sin(2*PI*0.17*t))+0.09*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*0.4*t))"
    "+0.05*sin(2*PI*329.6*t)*pow(max(0,sin(2*PI*0.25*t)),3)|"
    "0.22*sin(2*PI*55.4*t)+0.14*sin(2*PI*83*t+0.8*sin(2*PI*0.19*t))+0.09*sin(2*PI*110.5*t)*(0.6+0.4*sin(2*PI*0.37*t))"
    "+0.05*sin(2*PI*349.2*t)*pow(max(0,sin(2*PI*0.22*t)),3)':s=44100:d=%d" % DUR
)


def make_frame(card_path, out_path):
    card = Image.open(card_path).convert("RGB")
    bg = card.copy()
    bg = bg.resize((int(RH * card.width / card.height), RH)) if card.width / card.height < RW / RH else bg.resize((RW, int(RW * card.height / card.width)))
    left, top = (bg.width - RW) // 2, (bg.height - RH) // 2
    bg = bg.crop((left, top, left + RW, top + RH)).filter(ImageFilter.GaussianBlur(40))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    w = RW
    h = int(card.height * w / card.width)
    bg.paste(card.resize((w, h)), (0, (RH - h) // 2))
    bg.save(out_path, "PNG")
    return out_path


def make_reel(card_path, out_path):
    """מחזיר out_path אם הצליח. דורש ffmpeg."""
    frame = out_path.replace(".mp4", "_frame.png")
    make_frame(card_path, frame)
    frames = DUR * 25
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", frame,
        "-f", "lavfi", "-i", AUDIO,
        "-vf", f"zoompan=z='min(zoom+0.0007,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={RW}x{RH}:fps=25,format=yuv420p",
        "-af", f"afade=t=in:st=0:d=1.5,afade=t=out:st={DUR - 1.5}:d=1.5,volume=0.8",
        "-t", str(DUR), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-shortest", out_path,
    ]
    subprocess.run(cmd, check=True, timeout=240)
    try:
        os.remove(frame)
    except OSError:
        pass
    return out_path
