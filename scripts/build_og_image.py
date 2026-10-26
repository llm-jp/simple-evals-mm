"""Generate site/assets/og-image.png (1200x630), the link-preview image (Open Graph / Twitter card) of the site: a
brand card in the style of DeepSWE's -- the LLM-jp logo above "simple-evals-mm" and "LLM-jp", over a soft blur of the
logo's colours, all inside the central square so that cropped previews (Slack, square thumbnails) keep them whole. It does not depend on the results, so it only needs regenerating when the design changes.

    uv run python scripts/build_og_image.py
"""
import os
import pathlib
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
FONT_BOLD = os.environ.get("OG_FONT_BOLD", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc")
FONT_REG = os.environ.get("OG_FONT_REGULAR", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc")
W, H = 1200, 630
LOGO_COLOURS = [(233, 132, 159), (169, 155, 230), (127, 178, 236), (111, 211, 176), (240, 176, 122)]  # pink, lavender, blue, mint, orange
SPOTS = [(150, 120), (1050, 90), (300, 560), (950, 560), (620, 40), (80, 380), (1150, 380), (640, 620)]


def font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def background() -> Image.Image:
    rnd = random.Random(7)  # fixed: the same image on every run
    img = Image.new("RGB", (W, H), (20, 20, 26))
    d = ImageDraw.Draw(img)
    for k, (x, y) in enumerate(SPOTS):
        r = rnd.randint(230, 330)
        d.ellipse((x - r, y - r, x + r, y + r), fill=LOGO_COLOURS[k % len(LOGO_COLOURS)])
    img = img.filter(ImageFilter.GaussianBlur(110))
    img = Image.blend(img, Image.new("RGB", (W, H), (14, 14, 20)), 0.42)  # darken so white text reads
    noise = Image.effect_noise((W, H), 30).convert("RGB")
    return Image.blend(img, noise, 0.07)  # a little film grain


def main():
    img = background()
    d = ImageDraw.Draw(img)
    title, sub = font(FONT_BOLD, 62), font(FONT_REG, 26)
    logo = Image.open(SITE / "assets" / "llm-jp-logo.png").convert("RGBA")
    logo.thumbnail((120, 120))
    plate = Image.new("RGBA", (logo.width + 28, logo.height + 28), (0, 0, 0, 0))  # light plate: the colour logo on a dark card
    ImageDraw.Draw(plate).rounded_rectangle((0, 0, plate.width - 1, plate.height - 1), 24, fill=(255, 255, 255, 235))
    plate.paste(logo, (14, 14), logo)
    # stacked and centred inside the middle 630 x 630 square: previews that crop the card (Slack, square thumbnails)
    # keep the whole logo and name
    top = (H - (plate.height + 34 + 62 + 30 + 26)) / 2
    img.paste(plate, (int((W - plate.width) / 2), int(top)), plate)
    d.text((W / 2, top + plate.height + 34), "simple-evals-mm", font=title, fill=(250, 250, 248), anchor="mt")
    d.text((W / 2, top + plate.height + 34 + 62 + 30), "LLM-jp", font=sub, fill=(232, 232, 228), anchor="mt")
    out = SITE / "assets" / "og-image.png"
    img.save(out, optimize=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
