"""Erzeugt die App-Icons (benötigt Pillow): python tools/make_icons.py"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "solmate_os" / "static"


def icon(size: int) -> Image.Image:
    s = 4  # Supersampling für glatte Kanten
    n = size * s
    img = Image.new("RGB", (n, n))
    d = ImageDraw.Draw(img)
    for y in range(n):  # Verlauf Nachtblau → Blau
        t = y / n
        d.line([(0, y), (n, y)], fill=(int(18 + 20 * t), int(40 + 70 * t), int(90 + 110 * t)))
    cx, cy, r = n / 2, n * 0.47, n * 0.17
    sun = (255, 196, 46)
    for i in range(12):
        a = math.pi * 2 * i / 12
        x1, y1 = cx + math.cos(a) * r * 1.35, cy + math.sin(a) * r * 1.35
        x2, y2 = cx + math.cos(a) * r * 1.9, cy + math.sin(a) * r * 1.9
        d.line([(x1, y1), (x2, y2)], fill=sun, width=int(n * 0.035))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=sun)
    # Blitz
    bolt = [(0.53, 0.33), (0.43, 0.50), (0.50, 0.50), (0.46, 0.63), (0.58, 0.45), (0.51, 0.45), (0.55, 0.33)]
    d.polygon([(x * n, y * n) for x, y in bolt], fill=(25, 60, 130))
    # Akku-Balken unten
    bw, bh, by = n * 0.46, n * 0.09, n * 0.78
    d.rounded_rectangle([cx - bw / 2, by, cx + bw / 2, by + bh], radius=n * 0.02, outline=(230, 236, 245), width=int(n * 0.012))
    d.rounded_rectangle([cx - bw / 2 + n * 0.02, by + n * 0.02, cx - bw / 2 + bw * 0.72, by + bh - n * 0.02], radius=n * 0.01, fill=(60, 196, 136))
    return img.resize((size, size), Image.LANCZOS)


for size in (180, 192, 512):
    icon(size).save(OUT / f"icon-{size}.png", optimize=True)
    print("geschrieben:", OUT / f"icon-{size}.png")
