"""Generate site/assets/og-image.png (1200x630), the link-preview image (Open Graph / Twitter card) of the site:
a header (LLM-jp logo, title, counts) above the site's own Overall chart (score vs API cost per question), rendered
from site/index.html#thumb=1 (larger chart labels) in the light theme with headless Chromium.

Needs Playwright with Chromium, which the project itself does not depend on:
    uv run --with playwright python -m playwright install chromium
    uv run --with playwright python scripts/build_og_image.py
"""
import io
import json
import os
import pathlib

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
FONT_BOLD = os.environ.get("OG_FONT_BOLD", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc")
FONT_REG = os.environ.get("OG_FONT_REGULAR", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc")
W, H, HEADER = 1200, 630, 104
BG, TEXT, MUTED, BORDER = (255, 255, 255), (11, 11, 11), (98, 97, 93), (230, 229, 224)


def font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def chart_png() -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1180, "height": 900}, device_scale_factor=2, color_scheme="light")
        page.goto((SITE / "index.html").as_uri() + "#thumb=1")
        page.wait_for_selector("svg.chart circle")
        page.wait_for_timeout(500)
        png = page.locator("svg.chart").screenshot()
        browser.close()
    return png


def counts() -> tuple[int, int]:
    s = (SITE / "data.js").read_text()
    d = json.loads(s[s.index("{"): s.rstrip().rstrip(";").rindex("}") + 1])
    return len(d.get("models", [])), len(d.get("tasks", []))


def main():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    x = 48
    logo_path = SITE / "assets" / "llm-jp-logo.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        logo.thumbnail((200, 52))
        img.paste(logo, (x, (HEADER - logo.height) // 2), logo)
        x += logo.width + 22
        d.line((x, 30, x, HEADER - 30), fill=BORDER, width=2)
        x += 22
    d.text((x, 16), "simple-evals-mm leaderboard", font=font(FONT_BOLD, 38), fill=TEXT)
    n_models, n_tasks = counts()
    d.text((x, 64), f"Overall score vs API cost per question  ·  {n_models} models  ·  {n_tasks} tasks  ·  llm-jp.github.io/simple-evals-mm",
           font=font(FONT_REG, 19), fill=MUTED)
    d.line((0, HEADER, W, HEADER), fill=BORDER, width=2)

    chart = Image.open(io.BytesIO(chart_png())).convert("RGB")
    box_w, box_h = W - 40, H - HEADER - 16
    chart.thumbnail((box_w, box_h), Image.LANCZOS)
    img.paste(chart, ((W - chart.width) // 2, HEADER + 8 + (box_h - chart.height) // 2))
    out = SITE / "assets" / "og-image.png"
    img.save(out, optimize=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
