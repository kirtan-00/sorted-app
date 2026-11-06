"""A product-shoot calibration set for products.find_products (docs/shoot-modes.md).

Eight products on a seamless backdrop: seven drawn here (bottle, flask, mug, sneaker, headphones, backpack, watch) and the one
real product photo the landing page carries (site/img/product.webp, Pexels, credits in site/credits.html). Each comes
in six colours (the body recoloured, the way a client's red and navy SKUs differ; the real photo by a hue shift, three colours) and four views (as
shot, turned 20 degrees, mirrored and cropped in, squashed to a three-quarter view), so the truth for every file is
(product, colour, view). Writes <out>/<product>/<colour>_<view>.jpg and <out>/truth.csv. Nothing random is unseeded.

    python scripts/make_product_set.py <out_dir>
"""
from __future__ import annotations
import csv, colorsys, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 900, 900
# the product's body as (hue, saturation, value). Orange sits next to red and navy next to blue on purpose: the variant
# split has to tell neighbours apart, not just primaries.
HUES = {"red": (0.0, .75, .8), "yellow": (0.13, .75, .8), "green": (0.33, .75, .8), "blue": (0.6, .75, .8),
        "orange": (0.07, .8, .85), "navy": (0.62, .7, .35)}
VIEWS = ("front", "turned", "mirrored", "threequarter")

def backdrop(seed: int) -> Image.Image:
    y = np.linspace(0, 1, H)[:, None]
    g = (232 - 30 * y) * np.ones((1, W))
    rng = np.random.default_rng(seed)
    g = g + rng.normal(0, 1.5, g.shape)
    im = Image.fromarray(np.clip(np.stack([g, g, g * 0.985], -1), 0, 255).astype(np.uint8))
    sh = Image.new("L", (W, H), 0); ImageDraw.Draw(sh).ellipse((200, 760, 700, 830), fill=90)
    sh = sh.filter(ImageFilter.GaussianBlur(25))
    return Image.composite(Image.new("RGB", (W, H), (120, 120, 118)), im, sh)

def rgb(h: float, s=0.75, v=0.8) -> tuple[int, int, int]:
    return tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h % 1, s, v))

def shade(im: Image.Image, box) -> None:
    """A cylindrical light falloff across box: lighter left of centre, darker at the edges."""
    x0, y0, x1, y1 = box
    a = np.asarray(im).astype(np.float32)
    xs = np.linspace(-1, 1, x1 - x0)
    f = (1.15 - 0.5 * (xs - (-0.3)) ** 2).clip(0.55, 1.2)
    a[y0:y1, x0:x1] *= f[None, :, None]
    im.paste(Image.fromarray(a.clip(0, 255).astype(np.uint8)))

def draw(kind: str, hue: float) -> tuple[Image.Image, Image.Image]:
    """(colour layer, mask) of one product in one colour."""
    col = Image.new("RGB", (W, H), (0, 0, 0)); m = Image.new("L", (W, H), 0)
    d, dm = ImageDraw.Draw(col), ImageDraw.Draw(m)
    body = rgb(*hue)
    def both(fn, box, fill, **kw):
        getattr(d, fn)(box, fill=fill, **kw); getattr(dm, fn)(box, fill=255, **kw)
    if kind == "flask":            # a second bottle SKU: squat, wide neck, no label, the near neighbour of "bottle"
        both("rounded_rectangle", (290, 420, 610, 790), body, radius=110)
        both("rectangle", (390, 330, 510, 430), body)
        both("rounded_rectangle", (370, 280, 530, 340), (200, 200, 205), radius=10)
        shade(col, (290, 280, 610, 790))
    elif kind == "bottle":
        both("rounded_rectangle", (330, 300, 570, 790), body, radius=60)
        both("rectangle", (410, 190, 490, 310), body)
        both("rounded_rectangle", (395, 140, 505, 200), (30, 30, 30), radius=12)
        d.rectangle((330, 470, 570, 600), fill=(245, 242, 235)); d.text((380, 520), "AQUA 750", fill=(40, 40, 40))
        shade(col, (330, 140, 570, 790))
    elif kind == "mug":
        both("ellipse", (560, 400, 720, 640), body); d.ellipse((600, 450, 680, 590), fill=(0, 0, 0)); dm.ellipse((600, 450, 680, 590), fill=0)
        both("rounded_rectangle", (260, 330, 620, 770), body, radius=40)
        d.ellipse((260, 300, 620, 370), fill=tuple(int(c * 0.6) for c in body)); dm.ellipse((260, 300, 620, 370), fill=255)
        shade(col, (260, 300, 620, 770))
    elif kind == "sneaker":
        both("polygon", [(140, 640), (180, 470), (330, 420), (470, 520), (700, 560), (790, 640)], body)
        both("rounded_rectangle", (120, 630, 810, 700), (240, 240, 236), radius=30)
        for i in range(5):
            d.line((300 + 30 * i, 450 + 8 * i, 330 + 30 * i, 500 + 8 * i), fill=(250, 250, 250), width=8)
        d.ellipse((560, 590, 610, 620), fill=(250, 250, 250))
    elif kind == "headphones":
        d.arc((250, 200, 650, 640), 190, 350, fill=(35, 35, 35), width=40); dm.arc((250, 200, 650, 640), 190, 350, fill=255, width=40)
        both("rounded_rectangle", (220, 440, 340, 640), body, radius=45)
        both("rounded_rectangle", (560, 440, 680, 640), body, radius=45)
        shade(col, (220, 440, 340, 640)); shade(col, (560, 440, 680, 640))
    elif kind == "backpack":
        both("rounded_rectangle", (280, 230, 620, 780), body, radius=90)
        d.rounded_rectangle((340, 520, 560, 740), fill=tuple(int(c * 0.8) for c in body), radius=40)
        d.line((350, 520, 550, 520), fill=(30, 30, 30), width=6)
        d.arc((390, 150, 510, 280), 180, 360, fill=(30, 30, 30), width=18); dm.arc((390, 150, 510, 280), 180, 360, fill=255, width=18)
        shade(col, (280, 230, 620, 780))
    elif kind == "watch":
        both("rounded_rectangle", (395, 120, 505, 800), body, radius=30)
        both("ellipse", (300, 310, 600, 610), (200, 200, 205))
        d.ellipse((325, 335, 575, 585), fill=(20, 22, 30))
        d.line((450, 460, 450, 380), fill=(240, 240, 240), width=6); d.line((450, 460, 510, 480), fill=(240, 240, 240), width=5)
    return col, m

def recolour(im: Image.Image, shift: float) -> Image.Image:
    """Rotate the hue of the saturated pixels only (the product, not a white backdrop or a grey shadow)."""
    hsv = np.asarray(im.convert("HSV")).astype(np.float32)
    sat = hsv[..., 1] > 50
    hsv[..., 0] = np.where(sat, (hsv[..., 0] + shift * 255) % 255, hsv[..., 0])
    return Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGB")

def view(im: Image.Image, v: str, bg) -> Image.Image:
    if v == "turned":
        return im.rotate(20, resample=Image.BICUBIC, fillcolor=bg)
    if v == "mirrored":
        w, h = im.size
        return im.transpose(Image.FLIP_LEFT_RIGHT).crop((int(w * .1), int(h * .08), int(w * .92), int(h * .9))).resize((w, h), Image.BICUBIC)
    if v == "threequarter":
        w, h = im.size
        sq = im.resize((int(w * 0.72), h), Image.BICUBIC)
        out = Image.new("RGB", (w, h), bg); out.paste(sq, ((w - sq.width) // 2, 0))
        return out.rotate(-8, resample=Image.BICUBIC, fillcolor=bg)
    return im

def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    kinds = ("bottle", "flask", "mug", "sneaker", "headphones", "backpack", "watch")
    for k, kind in enumerate(kinds):
        for cname, hue in HUES.items():
            bg = backdrop(k)
            col, m = draw(kind, hue)
            base = Image.composite(col, bg, m.filter(ImageFilter.GaussianBlur(1.2)))
            for v in VIEWS:
                p = out / kind / f"{cname}_{v}.jpg"; p.parent.mkdir(exist_ok=True)
                view(base, v, (225, 225, 222)).save(p, quality=90)
                rows.append((str(p.relative_to(out)), kind, cname, v))
    real = Path(__file__).resolve().parents[1] / "site" / "img" / "product.webp"
    if real.is_file():
        src = Image.open(real).convert("RGB"); src.thumbnail((W, H))
        for cname, shift in (("as shot", 0.0), ("shift 1", 0.33), ("shift 2", 0.66)):
            base = recolour(src, shift)
            for v in VIEWS:
                p = out / "tubes" / f"{cname.replace(' ', '')}_{v}.jpg"; p.parent.mkdir(exist_ok=True)
                view(base, v, (225, 225, 222)).save(p, quality=90)
                rows.append((str(p.relative_to(out)), "tubes", cname, v))
    with open(out / "truth.csv", "w", newline="") as f:
        csv.writer(f).writerows([("rel", "product", "colour", "view"), *rows])
    print(f"{len(rows)} files in {out}")

if __name__ == "__main__":
    main(Path(sys.argv[1]))
