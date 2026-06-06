"""Generate the PWA icons (icon-192.png, icon-512.png).

Draws a simple dark tile with an upward green trend line — no font/emoji
dependency, so it renders identically everywhere. Run from this directory:

    python make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

BG = (15, 20, 25)        # --bg
PANEL = (26, 32, 41)     # --panel
GREEN = (46, 204, 113)   # --green
ACCENT = (74, 163, 255)  # --accent


def make_icon(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    # Rounded panel inset.
    pad = int(size * 0.10)
    radius = int(size * 0.18)
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius, fill=PANEL)

    # An upward "candlestick-ish" zigzag trend line.
    pts_norm = [(0.20, 0.70), (0.38, 0.55), (0.52, 0.62), (0.68, 0.38), (0.82, 0.28)]
    pts = [(int(x * size), int(y * size)) for x, y in pts_norm]
    line_w = max(2, int(size * 0.035))
    d.line(pts, fill=GREEN, width=line_w, joint="curve")

    # Dots at each vertex.
    r = max(2, int(size * 0.025))
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=ACCENT)

    return img


def main() -> None:
    here = Path(__file__).parent
    for s in (192, 512):
        make_icon(s).save(here / f"icon-{s}.png")
        print(f"wrote icon-{s}.png")


if __name__ == "__main__":
    main()
