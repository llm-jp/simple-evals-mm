"""Generate site/assets/overview.svg (self-contained figure for the site and the README).

Icons: Lucide (https://lucide.dev), ISC license (site/assets/LICENSE-lucide.txt); fetched once into site/assets/icons/.
Chip widths are computed with a generous per-character estimate and wrap, so wider system fonts cannot overflow.
    uv run python scripts/build_overview_svg.py
"""
import os
import re
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "site", "assets")
ICON_DIR = os.path.join(ROOT, "icons")
ICON_NAMES = {"sampler": "server", "task": "images", "run": "chart-column"}


def lucide(name):
    os.makedirs(ICON_DIR, exist_ok=True)
    p = os.path.join(ICON_DIR, f"{name}.svg")
    if not os.path.exists(p):
        urllib.request.urlretrieve(f"https://raw.githubusercontent.com/lucide-icons/lucide/main/icons/{name}.svg", p)
    s = open(p).read()
    inner = re.sub(r"^.*?<svg[^>]*>", "", s, flags=re.S).replace("</svg>", "").strip()
    return re.sub(r"\s+", " ", inner)


W, H, PAD = 1000, 256, 20


def chips(x0, y0, labels, color, border, maxw):
    out, x, y, h = [], x0, y0, 24
    for lab in labels:
        w = int(len(lab) * 7.4 + 22)
        if x + w > x0 + maxw:
            x, y = x0, y + h + 6
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="#ffffff" stroke="{border}"/>'
                   f'<text x="{x + w / 2:.0f}" y="{y + 16}" text-anchor="middle" font-size="11.5" font-weight="600" fill="{color}">{lab}</text>')
        x += w + 6
    return "\n      ".join(out)


def card(x, y, w, h, fill, border, kcolor, icon, key, title, sub, labels):
    return f'''  <g>
    <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{fill}" stroke="{border}"/>
    <g transform="translate({x + PAD} {y + 18}) scale(1.75)" fill="none" stroke="{kcolor}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{lucide(ICON_NAMES[icon])}</g>
    <text x="{x + PAD + 58}" y="{y + 34}" font-size="11" font-weight="700" letter-spacing="0.12em" fill="{kcolor}">{key}</text>
    <text x="{x + PAD + 58}" y="{y + 58}" font-size="19" font-weight="700" fill="#111111" letter-spacing="-0.01em">{title}</text>
    <text x="{x + PAD}" y="{y + 90}" font-size="12.5" fill="#5a5955">{sub}</text>
      {chips(x + PAD, y + 106, labels, kcolor, border, w - 2 * PAD)}
  </g>'''


def main():
    cw, gap, y, h = 296, 36, 66, 178
    x1 = 12; x2 = x1 + cw + gap; x3 = x2 + cw + gap
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="simple-evals-mm: a Sampler (any model behind one interface) times a Task (30+ Japanese and English vision and text benchmarks) gives a Run: reproducible results with the frozen config, answers, score, cost and output tokens">',
           '  <!-- Icons: Lucide (https://lucide.dev), ISC License, Copyright (c) Lucide Icons and Contributors. Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies. THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE. -->',
           '  <style>text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, "Helvetica Neue", Arial, sans-serif; } .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }</style>',
           f'  <rect width="{W}" height="{H}" fill="#ffffff"/>',
           '  <text class="mono" x="12" y="28" font-size="18" font-weight="700" fill="#111111">simple-evals-mm</text>',
           '  <text x="12" y="50" font-size="12.5" fill="#6f6e69">A lightweight, extensible evaluation framework for vision-language models</text>',
           card(x1, y, cw, h, "#eef4ff", "#cfdcf5", "#2a5db0", "sampler", "SAMPLER", "Any model", "API and local models behind one interface", ["API", "OpenRouter", "SGLang", "Hugging Face"]),
           card(x2, y, cw, h, "#f3eeff", "#ded1f7", "#6a46c2", "task", "TASK", "30+ tasks · JA / EN", "Vision and text benchmarks", ["VQA", "Document", "Chart", "OCR", "Math", "Text-only"]),
           card(x3, y, W - x3 - 12, h, "#fff7ee", "#f3d8b8", "#b35a12", "run", "RUN", "Reproducible results", "One run = one frozen configuration", ["config", "answers", "score", "cost", "output tokens"])]
    cy = y + h / 2
    svg.append(f'  <text x="{x1 + cw + gap / 2:.0f}" y="{cy + 9:.0f}" text-anchor="middle" font-size="26" fill="#b3b2ad">×</text>')
    ax = x2 + cw + 6
    svg.append(f'  <line x1="{ax}" y1="{cy:.0f}" x2="{ax + gap - 14}" y2="{cy:.0f}" stroke="#b3b2ad" stroke-width="1.8"/>'
               f'<path d="M{ax + gap - 19} {cy - 5:.0f} L{ax + gap - 12} {cy:.0f} L{ax + gap - 19} {cy + 5:.0f}" fill="none" stroke="#b3b2ad" stroke-width="1.8"/>')
    svg.append('</svg>')
    out = os.path.join(ROOT, "overview.svg")
    open(out, "w").write("\n".join(svg) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
