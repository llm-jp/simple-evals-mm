"""Generate site/assets/og-image.png (1200x630), the link-preview image (Open Graph / Twitter card) of the site.

Title and description on the left; on the right a small score-versus-cost scatter of the configurations in
site/data.js (points only, no labels, so the image stays valid as results change).
    uv run python scripts/build_og_image.py
"""
import json
import math
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FONT_BOLD = os.environ.get("OG_FONT_BOLD", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc")
FONT_REG = os.environ.get("OG_FONT_REGULAR", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc")
W, H = 1200, 630
BG, TEXT, MUTED, GRID, ACCENT = (252, 252, 251), (11, 11, 11), (98, 97, 93), (230, 229, 224), (42, 120, 214)


def font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def load_points() -> tuple[list[tuple[float, float]], int, int]:
    s = open(os.path.join(ROOT, "site", "data.js")).read()
    d = json.loads(s[s.index("{"): s.rstrip().rstrip(";").rindex("}") + 1])
    pts = []
    for m in d.get("models", []):
        for lvl in ("low", "max"):
            b = ((m.get(lvl) or {}).get("benchmarks") or {}).get("Overall") or {}
            if b.get("score") is not None and b.get("cost"):
                pts.append((b["cost"], b["score"]))
    return pts, len(d.get("models", [])), len(d.get("tasks", []))


def main():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    logo_path = os.path.join(ROOT, "site", "assets", "llm-jp-logo.png")
    x0 = 72
    if os.path.exists(logo_path):
        logo = Image.open(logo_path).convert("RGBA")
        logo.thumbnail((220, 64))
        img.paste(logo, (x0, 72), logo)
    d.text((x0, 168), "simple-evals-mm", font=font(FONT_BOLD, 64), fill=TEXT)
    d.text((x0, 252), "Leaderboard", font=font(FONT_BOLD, 40), fill=ACCENT)
    pts, n_models, n_tasks = load_points()
    for i, line in enumerate(["Vision-language models on Japanese", "and English benchmarks"]):
        d.text((x0, 322 + i * 40), line, font=font(FONT_REG, 30), fill=TEXT)
    for i, line in enumerate(["Score, cost and output tokens per question,", "with the provider and inference settings of every run"]):
        d.text((x0, 422 + i * 34), line, font=font(FONT_REG, 23), fill=MUTED)
    if n_models:
        d.text((x0, 540), f"{n_models} models · {n_tasks} tasks · llm-jp.github.io/simple-evals-mm", font=font(FONT_REG, 22), fill=MUTED)

    # score vs cost scatter (log cost)
    px0, py0, px1, py1 = 700, 110, 1130, 520
    d.rounded_rectangle((px0 - 20, py0 - 30, px1 + 20, py1 + 40), radius=18, outline=GRID, width=2, fill=(255, 255, 255))
    if pts:
        cs = [math.log10(c) for c, _ in pts]
        ss = [s for _, s in pts]
        clo, chi = min(cs) - 0.15, max(cs) + 0.15
        slo, shi = min(ss) - 3, max(ss) + 3
        sx = lambda c: px0 + (math.log10(c) - clo) / (chi - clo) * (px1 - px0)  # noqa: E731
        sy = lambda s: py1 - (s - slo) / (shi - slo) * (py1 - py0)  # noqa: E731
        for k in range(1, 4):
            y = py0 + k * (py1 - py0) / 4
            d.line((px0, y, px1, y), fill=GRID, width=1)
        for c, s in pts:
            r = 9
            d.ellipse((sx(c) - r, sy(s) - r, sx(c) + r, sy(s) + r), fill=ACCENT, outline=(255, 255, 255), width=2)
        small = font(FONT_REG, 18)
        d.text((px0, py1 + 12), "cost per question (log) →", font=small, fill=MUTED)
        d.text((px0, py0 - 26), "↑ score", font=small, fill=MUTED)
    out = os.path.join(ROOT, "site", "assets", "og-image.png")
    img.save(out, optimize=True)
    print(f"wrote {out} ({len(pts)} configurations)")


if __name__ == "__main__":
    main()
