"""A product shoot with ground truth for Client pick (photosort/cull.py, docs/client-pick.md).

What a real product day looks like after the shutter: every product in several colours, every colour from several
angles, every angle shot a few times over (retakes), and the frames that have to go: the product out of focus while
the backdrop is sharp, camera shake, the product run off the edge of the frame, the product blown out or crushed to
black. Thirty drawn products by default (the seven of make_product_set.py plus twenty-three more), half of them on a
textured surface whose grain stays sharp, which is the case a whole-frame focus check gets wrong.

Writes <out>/<product>/<colour>_<view>_<take>[_<defect>].jpg and <out>/truth.csv with columns
rel, product, colour, view, take, defect, severity. defect is "" for a clean frame. A clean retake differs from its
siblings by a few pixels of framing, a quarter degree, a hundredth of a stop and up to half a pixel of softness, the
way a real retake does. Nothing random is unseeded.

    python scripts/make_cull_set.py <out_dir> [--products 30] [--seed 2] [--workers 6]

--products 30 --seed 2 is the 3,500-photo benchmark (about 3,500 files); --products 8 --seed 1 is the calibration set
the thresholds in cull.py were read off. The two never share a seed.
"""
from __future__ import annotations
import argparse, csv, sys
from multiprocessing import Pool
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_product_set as mps          # noqa: E402  the seven calibrated products, rgb(), shade(), view()

W, H = mps.W, mps.H
HUES = dict(mps.HUES, black=(0.0, 0.0, 0.16))     # a black SKU: the colour crushed blacks happen to
DARK = ("navy", "black")
OLD = ("bottle", "flask", "mug", "sneaker", "headphones", "backpack", "watch")
NEW = ("vase", "lamp", "chair", "candle", "phone", "sunglasses", "cap", "giftbox", "perfume", "lipstick", "can",
       "kettle", "helmet", "umbrella", "teapot", "wineglass", "tshirt", "handbag", "speaker", "camera", "book", "boot",
       "plantpot")
KINDS = OLD + NEW
# views beyond make_product_set's four: tilted the other way, and a close crop
# six angles a product table actually shoots: straight on, turned, three-quarter, from above, a detail close-up that
# fills the frame on purpose, and a wide shot with room round the product
VIEWS = ("front", "turned", "threequarter", "top", "detail", "wide")
DEFECTS = ("soft", "motion", "cut", "blown", "crushed")
SEVERITY = {"soft": (2.5, 4.0, 7.0), "motion": (10, 18, 30), "cut": (0.2, 0.32, 0.45), "blown": (0.6, 1.2), "crushed": (1.0, 2.0)}


def draw_new(kind: str, hue) -> tuple[Image.Image, Image.Image]:
    """(colour layer, mask) of one of the twenty-three extra products, drawn in the 900 px frame like mps.draw."""
    col = Image.new("RGB", (W, H), (0, 0, 0)); m = Image.new("L", (W, H), 0)
    d, dm = ImageDraw.Draw(col), ImageDraw.Draw(m)
    body = mps.rgb(*hue)
    dark = tuple(int(c * 0.55) for c in body)
    ink = (28, 28, 30); white = (244, 242, 236); metal = (190, 190, 196)
    def both(fn, box, *args, **kw):          # args: a start and end angle for arc, chord and pieslice, then the fill
        *extra, fill = args
        getattr(d, fn)(box, *extra, fill=fill, **kw); getattr(dm, fn)(box, *extra, fill=255, **kw)
    if kind == "vase":
        both("ellipse", (300, 380, 600, 780), body); both("rectangle", (390, 220, 510, 420), body)
        both("ellipse", (360, 190, 540, 250), dark); shade(col, (300, 190, 600, 780))
    elif kind == "lamp":
        both("polygon", [(300, 380), (600, 380), (530, 180), (370, 180)], body)
        both("rectangle", (440, 380, 460, 720), metal); both("ellipse", (330, 700, 570, 780), ink)
    elif kind == "chair":
        both("rectangle", (300, 200, 340, 780), dark); both("rectangle", (560, 470, 600, 780), dark)
        both("rectangle", (300, 450, 620, 510), body); both("rectangle", (300, 200, 560, 330), body)
        both("rectangle", (380, 510, 410, 780), dark)
    elif kind == "candle":
        both("rectangle", (360, 360, 540, 780), body); both("ellipse", (360, 330, 540, 390), dark)
        both("ellipse", (435, 250, 465, 330), (250, 200, 80)); d.line((450, 330, 450, 360), fill=ink, width=4)
    elif kind == "phone":
        both("rounded_rectangle", (330, 140, 570, 760), ink, radius=36)
        d.rounded_rectangle((345, 175, 555, 725), fill=body, radius=18); d.ellipse((440, 740, 460, 752), fill=metal)
    elif kind == "sunglasses":
        both("ellipse", (170, 380, 420, 560), body); both("ellipse", (480, 380, 730, 560), body)
        both("rectangle", (410, 420, 490, 440), ink); both("line", (170, 430, 110, 400), ink, width=14)
        both("line", (730, 430, 790, 400), ink, width=14)
        d.ellipse((200, 405, 390, 535), fill=dark); d.ellipse((510, 405, 700, 535), fill=dark)
    elif kind == "cap":
        both("pieslice", (230, 300, 670, 700), 180, 360, body); both("ellipse", (560, 470, 820, 540), dark)
        d.ellipse((435, 290, 465, 320), fill=ink); dm.ellipse((435, 290, 465, 320), fill=255)
    elif kind == "giftbox":
        both("rectangle", (270, 380, 630, 760), body); both("rectangle", (250, 320, 650, 400), dark)
        d.rectangle((430, 320, 470, 760), fill=white)
        both("ellipse", (360, 250, 450, 330), white); both("ellipse", (450, 250, 540, 330), white)
    elif kind == "perfume":
        both("rounded_rectangle", (300, 400, 600, 760), body, radius=20); both("rectangle", (410, 330, 490, 410), metal)
        both("rounded_rectangle", (380, 230, 520, 340), ink, radius=10); d.rectangle((360, 520, 540, 600), fill=white)
    elif kind == "lipstick":
        both("rectangle", (390, 430, 510, 750), ink); both("rectangle", (400, 310, 500, 440), metal)
        both("polygon", [(410, 310), (490, 310), (490, 210), (430, 170), (410, 210)], body)
    elif kind == "can":
        both("rectangle", (330, 260, 570, 760), body); both("ellipse", (330, 230, 570, 290), metal)
        both("ellipse", (330, 730, 570, 790), metal); d.rectangle((330, 430, 570, 560), fill=white)
        d.text((380, 480), "FIZZ", fill=ink); shade(col, (330, 230, 570, 790))
    elif kind == "kettle":
        both("ellipse", (260, 340, 640, 780), body); both("polygon", [(620, 500), (760, 380), (780, 400), (640, 560)], body)
        both("arc", (330, 220, 570, 460), 180, 360, ink, width=24); both("ellipse", (420, 320, 480, 360), ink)
    elif kind == "helmet":
        both("chord", (220, 250, 680, 710), 180, 360, body); both("rectangle", (220, 470, 680, 540), dark)
        d.rectangle((300, 420, 600, 470), fill=ink)
    elif kind == "umbrella":
        both("pieslice", (150, 200, 750, 640), 180, 360, body); both("rectangle", (440, 420, 460, 760), ink)
        both("arc", (380, 720, 460, 800), 0, 180, ink, width=18)
        for x in (300, 450, 600):
            d.line((450, 205, x, 420), fill=dark, width=5)
    elif kind == "teapot":
        both("ellipse", (290, 380, 610, 720), body); both("polygon", [(300, 520), (180, 420), (170, 445), (300, 600)], body)
        both("arc", (560, 440, 700, 640), 270, 90, body, width=26); both("ellipse", (400, 330, 500, 400), dark)
    elif kind == "wineglass":
        both("chord", (330, 200, 570, 520), 0, 180, body); both("rectangle", (330, 200, 570, 360), body)
        both("rectangle", (440, 500, 460, 720), metal); both("ellipse", (360, 700, 540, 750), metal)
    elif kind == "tshirt":
        both("polygon", [(300, 220), (380, 200), (450, 250), (520, 200), (600, 220), (720, 330), (650, 410), (600, 370),
                         (600, 760), (300, 760), (300, 370), (250, 410), (180, 330)], body)
        d.arc((390, 180, 510, 280), 0, 180, fill=dark, width=10)
    elif kind == "handbag":
        both("polygon", [(240, 420), (660, 420), (700, 760), (200, 760)], body)
        both("arc", (330, 230, 570, 520), 180, 360, ink, width=22); d.rectangle((420, 470, 480, 520), fill=metal)
    elif kind == "speaker":
        both("rounded_rectangle", (320, 180, 580, 780), body, radius=30)
        for cy, r in ((330, 70), (600, 110)):
            d.ellipse((450 - r, cy - r, 450 + r, cy + r), fill=ink); d.ellipse((450 - r // 3, cy - r // 3, 450 + r // 3, cy + r // 3), fill=dark)
    elif kind == "camera":
        both("rounded_rectangle", (200, 360, 700, 680), body, radius=30); both("rectangle", (260, 320, 360, 370), ink)
        both("ellipse", (360, 400, 560, 600), ink); d.ellipse((400, 440, 520, 560), fill=(60, 70, 90))
    elif kind == "book":
        both("rectangle", (290, 200, 610, 760), body); d.rectangle((290, 200, 320, 760), fill=dark)
        d.rectangle((360, 300, 560, 360), fill=white); d.text((380, 320), "NOTES", fill=ink)
    elif kind == "boot":
        both("polygon", [(330, 200), (500, 200), (500, 580), (720, 640), (720, 720), (320, 720)], body)
        both("rectangle", (310, 710, 730, 760), ink); d.line((340, 260, 480, 260), fill=dark, width=10)
    elif kind == "plantpot":
        both("polygon", [(320, 520), (580, 520), (550, 780), (350, 780)], body)
        for k, (x, y) in enumerate(((380, 260), (450, 200), (520, 270), (420, 330), (500, 340))):
            both("ellipse", (x - 45, y - 80, x + 45, y + 80), (40 + 10 * k, 120, 60))
        both("rectangle", (440, 300, 460, 520), (50, 110, 50))
    else:
        raise ValueError(kind)
    return col, m


def shade(im, box):
    mps.shade(im, box)


def draw(kind: str, hue) -> tuple[Image.Image, Image.Image]:
    return mps.draw(kind, hue) if kind in OLD else draw_new(kind, hue)


def backdrop(seed: int, textured: bool, rng) -> Image.Image:
    """make_product_set's seamless sweep, or a linen tabletop whose fine grain is sharp in every frame."""
    bg = mps.backdrop(seed)
    if not textured:
        return bg
    a = np.asarray(bg).astype(np.float32)
    grain = rng.normal(0, 7, (H, W))
    weave = 6 * np.sin(np.arange(W)[None, :] * 1.9) * np.sin(np.arange(H)[:, None] * 2.3)
    a += (grain + weave)[..., None]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def layer_view(col, m, v):
    """One angle, applied to the product layer and its mask alone, so the backdrop never rotates or crops."""
    box = m.getbbox() or (0, 0, W, H)
    def one(im):
        if v == "turned":
            return im.rotate(25, resample=Image.BICUBIC)
        if v == "threequarter":
            sq = im.resize((int(W * 0.65), H), Image.BICUBIC)
            out = Image.new(im.mode, (W, H)); out.paste(sq, ((W - sq.width) // 2, 0))
            return out.rotate(-8, resample=Image.BICUBIC)
        if v == "top":                                  # seen from above: squashed top to bottom, sitting lower
            sq = im.resize((W, int(H * 0.55)), Image.BICUBIC)
            out = Image.new(im.mode, (W, H)); out.paste(sq, (0, int(H * 0.3)))
            return out
        if v == "detail":                               # the top of the product, 2.2 times in: it fills the frame
            x0, y0, x1, y1 = box
            side = int(max(x1 - x0, y1 - y0) / 2.2)
            cx, cy = (x0 + x1) // 2, y0 + int(0.3 * (y1 - y0))
            return im.crop((cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2)).resize((W, H), Image.BICUBIC)
        if v == "wide":                                 # stepped back: the product at 0.6, low in the frame
            sm = im.resize((int(W * 0.6), int(H * 0.6)), Image.BICUBIC)
            out = Image.new(im.mode, (W, H)); out.paste(sm, (int(W * 0.2), int(H * 0.32)))
            return out
        return im
    return one(col), one(m.convert("RGB")).convert("L")


def jitter(col, m, rng):
    """A retake: the product a few pixels over and a fraction of a degree round."""
    dx, dy, rot = int(rng.integers(-6, 7)), int(rng.integers(-6, 7)), float(rng.uniform(-0.4, 0.4))
    def f(im):
        return im.rotate(rot, resample=Image.BICUBIC, translate=(dx, dy))
    return f(col), f(m)


def shift_off(col, m, frac: float, side: str):
    """Move the product so frac of its height or width leaves the frame by side."""
    box = m.getbbox()
    if box is None:
        return col, m
    x0, y0, x1, y1 = box
    if side == "left":
        dx, dy = -(x0 + int(frac * (x1 - x0))), 0
    elif side == "right":
        dx, dy = (W - x1) + int(frac * (x1 - x0)), 0
    elif side == "top":
        dx, dy = 0, -(y0 + int(frac * (y1 - y0)))
    else:
        dx, dy = 0, (H - y1) + int(frac * (y1 - y0))
    return col.rotate(0, translate=(dx, dy)), m.rotate(0, translate=(dx, dy))


def exposure(im: Image.Image, stops: float) -> Image.Image:
    a = np.asarray(im).astype(np.float32) / 255.0
    lin = np.where(a > 0.04045, ((a + 0.055) / 1.055) ** 2.4, a / 12.92) * (2.0 ** stops)
    out = np.where(lin > 0.0031308, 1.055 * np.clip(lin, 0, None) ** (1 / 2.4) - 0.055, 12.92 * lin)
    return Image.fromarray((out.clip(0, 1) * 255 + 0.5).astype(np.uint8))


def motion(im: Image.Image, length: int, angle: float) -> Image.Image:
    import cv2
    k = np.zeros((length, length), np.float32)
    c = (length - 1) / 2
    for t in np.linspace(-c, c, length * 3):
        x, y = int(round(c + t * np.cos(angle))), int(round(c + t * np.sin(angle)))
        k[y, x] = 1
    k /= k.sum()
    return Image.fromarray(cv2.filter2D(np.asarray(im), -1, k, borderType=cv2.BORDER_REFLECT))


def gloss(col, m, rng):
    """A specular highlight on a glossy product: a small clipped streak, as a clean frame of a real bottle has."""
    box = m.getbbox()
    if box is None:
        return col
    x0, y0, x1, y1 = box
    cx = x0 + (x1 - x0) * 0.3; cy = y0 + (y1 - y0) * 0.4
    hl = Image.new("L", (W, H), 0)
    ImageDraw.Draw(hl).ellipse((cx - 10, cy - 50, cx + 10, cy + 50), fill=255)
    hl = Image.composite(hl, Image.new("L", (W, H), 0), m).filter(ImageFilter.GaussianBlur(2))
    return Image.composite(Image.new("RGB", (W, H), (255, 255, 255)), col, hl)


def render(job: dict) -> tuple:
    rng = np.random.default_rng(job["seed"])
    kind, cname, v = job["kind"], job["colour"], job["view"]
    col, m = draw(kind, HUES[cname])
    if job["glossy"]:
        col = gloss(col, m, rng)
    col, m = layer_view(col, m, v)
    col, m = jitter(col, m, rng)
    bg = backdrop(job["bg_seed"], job["textured"], rng)
    defect, sev = job["defect"], job["severity"]
    edge = 1.2 + float(rng.uniform(0, 0.5))           # the clean retakes are not identically sharp
    if defect == "cut":
        col, m = shift_off(col, m, sev, job["side"])
    if defect == "soft":
        edge = sev                                     # the lens focused behind the product: the backdrop stays sharp
        col = col.filter(ImageFilter.GaussianBlur(sev))
    elif edge > 1.2:
        col = col.filter(ImageFilter.GaussianBlur(edge - 1.2))
    im = Image.composite(col, bg, m.filter(ImageFilter.GaussianBlur(edge)))
    im = exposure(im, float(rng.uniform(-0.03, 0.03)))
    if defect == "motion":
        im = motion(im, int(sev), float(rng.uniform(0, np.pi)))
    elif defect == "blown":
        # the product, not just the sweep, pushed past white: stops enough to clip its brightest channel plus sev
        body = max(mps.rgb(*HUES[cname])) / 255.0
        lin = ((body + 0.055) / 1.055) ** 2.4
        im = exposure(im, float(np.log2(1.0 / lin)) + sev)
    elif defect == "crushed":
        # a dark product under a heavy curve: the black point set at the product's own median, so half of it and more
        # goes to pure black while the sweep only darkens. sev 1 or 2 pushes it a little further.
        px = np.asarray(im)[np.asarray(m) > 128].max(1)
        bp = float(np.percentile(px, 50 + 10 * sev)) + 2 if px.size else 40.0
        a = np.asarray(im).astype(np.float32)
        im = Image.fromarray(((a - bp) / (255 - bp) * 255).clip(0, 255).astype(np.uint8))
    out = Path(job["path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, quality=88)
    return job["rel"], kind, cname, v, job["take"], defect, sev if defect else ""


def plan(out: Path, products: int, seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    kinds = list(KINDS)
    rng.shuffle(kinds)
    kinds = kinds[:products]
    jobs = []
    for k, kind in enumerate(kinds):
        ncol = int(rng.integers(2, 6))                  # 2 to 5 colours
        nview = int(rng.integers(5, 7))                 # 5 or 6 angles
        colours = list(rng.choice(list(HUES), ncol, replace=False))
        views = list(VIEWS[:nview])
        textured = bool(k % 2)
        glossy = bool(rng.random() < 0.5)
        for cname in colours:
            for v in views:
                takes = int(rng.integers(2, 8))         # 2 to 7 clean takes of every angle
                ndef = int(rng.poisson(1.6))
                # crushed blacks only happen to a dark product; a detail that fills the frame cannot be cut off
                allowed = [d for d in DEFECTS if (d != "crushed" or cname in DARK) and (d != "cut" or v != "detail")]
                items = [("", 0.0)] * takes + [(str(rng.choice(allowed)), 0.0) for _ in range(ndef)]
                for t, (defect, _) in enumerate(items):
                    sev = float(rng.choice(SEVERITY[defect])) if defect else 0.0
                    rel = f"{kind}/{cname}_{v}_{t}{'_' + defect if defect else ''}.jpg"
                    jobs.append(dict(kind=kind, colour=str(cname), view=v, take=t, defect=defect, severity=sev,
                                     side=str(rng.choice(["left", "right", "top", "bottom"])), textured=textured,
                                     glossy=glossy, bg_seed=k, seed=int(rng.integers(1 << 30)), rel=rel,
                                     path=str(out / rel)))
    return jobs


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out"); ap.add_argument("--products", type=int, default=30); ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    jobs = plan(out, a.products, a.seed)
    with Pool(a.workers) as pool:
        rows = pool.map(render, jobs, chunksize=8)
    with open(out / "truth.csv", "w", newline="") as f:
        csv.writer(f).writerows([("rel", "product", "colour", "view", "take", "defect", "severity"), *rows])
    print(f"{len(rows)} files in {out}")


if __name__ == "__main__":
    main()
