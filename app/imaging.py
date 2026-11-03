"""Image analysis, background removal and canvas fitting.

Analysis works on fractions of the canvas, so a downscaled copy gives the same
answer as the original. Processing always runs at full resolution, because
downscaling loses thin dark details and light edges.
"""
import io

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = 120_000_000

_SESSION = None


def session():
    """Load the segmentation model once. isnet-general-use is the model that
    worked reliably in earlier Bisque jobs (birefnet had load problems)."""
    global _SESSION
    if _SESSION is None:
        from rembg import new_session
        _SESSION = new_session("isnet-general-use")
    return _SESSION


def hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.int16)


def load(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    im.load()
    return im


def has_alpha(im: Image.Image) -> bool:
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        a = np.asarray(im.convert("RGBA"))[:, :, 3]
        return int(a.min()) < 250
    return False


def border_stats(rgb: np.ndarray, frac=0.02):
    """Background colour and how plain the border is.

    Each side is judged on its own, skipping the outer 0.5% where thin edge
    lines live. A packshot whose product touches one edge still counts as
    plain; a model or lifestyle shot fails on most sides. Returns the median
    colour of the plain sides and the uniformity of the third-best side.
    """
    h, w, _ = rgb.shape
    i = max(1, int(min(h, w) * 0.005))
    b = max(2, int(min(h, w) * frac))
    sides = [rgb[i:i + b, i:w - i], rgb[h - i - b:h - i, i:w - i],
             rgb[i:h - i, i:i + b], rgb[i:h - i, w - i - b:w - i]]
    sides = [s.reshape(-1, 3).astype(np.int16) for s in sides]
    allpx = np.concatenate(sides)
    med = np.median(allpx, axis=0).astype(np.int16)
    scores = [float((np.abs(s - med).sum(1) < 24).mean()) for s in sides]
    plain = [s for s, sc in zip(sides, scores) if sc >= 0.9]
    if plain:
        med = np.median(np.concatenate(plain), axis=0).astype(np.int16)
        scores = [float((np.abs(s - med).sum(1) < 24).mean()) for s in sides]
    return med, sorted(scores, reverse=True)[2]


def bbox(mask):
    ys = np.where(mask.any(1))[0]
    xs = np.where(mask.any(0))[0]
    if len(ys) == 0:
        return None
    return int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1


def _plain_mask(rgb, bg, thresh=40):
    diff = np.abs(rgb.astype(np.int16) - bg).sum(2)
    return ndimage.binary_opening(diff > thresh, iterations=2)


_FACES = None


def _faces(im: Image.Image) -> int:
    """Count frontal or profile faces. Used to recognise model shots."""
    global _FACES
    import cv2
    if _FACES is None:
        _FACES = [cv2.CascadeClassifier(cv2.data.haarcascades + n)
                  for n in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")]
    g = im.convert("L")
    g.thumbnail((900, 900))
    arr = np.asarray(g)
    side = int(min(arr.shape) * 0.06)
    return sum(len(c.detectMultiScale(arr, scaleFactor=1.1, minNeighbors=6, minSize=(side, side))) for c in _FACES)


def _model_reason(im, mask, box):
    """A model shot shows a face, or a body cut off by the top or bottom edge.
    Flatlays and packshots float free of those edges."""
    h, w = mask.shape
    x0, y0, x1, y1 = box
    if _faces(im):
        return "Model shot (face found)"
    band = max(1, int(h * 0.005))
    top = mask[:band].any(0).mean()
    bottom = mask[-band:].any(0).mean()
    if bottom > 0.12 or top > 0.08:
        return "Model shot (body cut off at the edge)"
    return None


def photo_result(reason, W, H):
    return {"category": "photo", "status": "skip", "reasons": [reason], "metrics": {"w": W, "h": H}}


def analyze(data: bytes, spec: dict, orig_w: int = None, orig_h: int = None, force: str = None) -> dict:
    """Classify an image and check it against the grid standard.

    force="photo" marks it as a model shot; force="flatlay" skips model detection.
    """
    im = load(data)
    w, h = im.size
    W, H = orig_w or w, orig_h or h
    if force == "photo":
        return photo_result("Marked as model shot", W, H)
    if has_alpha(im):
        a = np.asarray(im.convert("RGBA"))[:, :, 3]
        mask = ndimage.binary_opening(a > 8, iterations=1)
        category, bg = "cutout", None
    else:
        rgb = np.asarray(im.convert("RGB"))
        bg, uniform = border_stats(rgb)
        if uniform < spec["uniform_min"] and force != "flatlay":
            return photo_result("Model or lifestyle shot (background is not plain)", W, H)
        mask = _plain_mask(rgb, bg)
        category = "plain"

    box = bbox(mask)
    if box is None:
        return {"category": category, "status": "skip", "reasons": ["No product found in image"], "metrics": {"w": W, "h": H}}
    x0, y0, x1, y1 = box
    touches = sum([x0 <= w * 0.005, y0 <= h * 0.005, x1 >= w * 0.995, y1 >= h * 0.995])
    if touches >= 3:
        return {"category": "detail", "status": "skip",
                "reasons": ["Detail shot (fills the frame)"], "metrics": {"w": W, "h": H}}
    if force != "flatlay":
        reason = _model_reason(im, mask, box)
        if reason:
            return photo_result(reason, W, H)

    cw, ch, bh = spec["canvas_w"], spec["canvas_h"], spec["box_h"]
    pw, ph = (x1 - x0) / w, (y1 - y0) / h          # product size as share of image
    aspect = ((x1 - x0) * W / w) / max(1, (y1 - y0) * H / h)   # real width / height of the product
    target_h = min(bh, width_limit(spec) / aspect)  # height the product should have on the standard canvas
    fill = (ph * ch) / target_h                     # 1.0 = exactly the standard height
    dx, dy = (x0 + x1) / 2 / w - 0.5, (y0 + y1) / 2 / h - 0.5

    reasons = []
    if abs(W / H - cw / ch) > 0.01:
        reasons.append(f"Canvas {W}×{H}, standard is {cw}×{ch}")
    elif W < cw * 0.6:
        reasons.append(f"Low resolution ({W}×{H})")
    size_word = "height" if spec.get("fit_mode") == "height" else "size"
    tol = spec["scale_tol"]
    if fill < 1 - tol:
        reasons.append(f"Product too small ({fill:.0%} of standard {size_word})")
    elif fill > 1 + tol:
        reasons.append(f"Product too large ({fill:.0%} of standard {size_word})")
    vis = min(W, H * spec["card_ratio_w"] / spec["card_ratio_h"])
    left, right = x0 / w * W, x1 / w * W
    if left < (W - vis) / 2 or right > (W + vis) / 2:
        reasons.append("Cut off at the sides in the grid")
    if abs(dx) > spec["center_tol"]:
        reasons.append("Not centred horizontally")
    if abs(dy) > spec["center_tol"]:
        reasons.append("Not centred vertically")
    if category == "plain" and np.abs(bg - hex_rgb(spec["card_bg"])).sum() > spec["bg_tol"]:
        reasons.append("Background is not transparent")

    return {
        "category": category,
        "status": "off" if reasons else "ok",
        "reasons": reasons,
        "metrics": {"w": W, "h": H, "fill": round(fill, 3), "dx": round(dx, 3), "dy": round(dy, 3),
                    "pw": round(pw, 3), "ph": round(ph, 3)},
    }


def visible_width(spec):
    """Width of the canvas that is visible in a grid card. The shop theme scales the
    image to the card's height and crops the sides, so a square canvas in a portrait
    card only shows its middle part."""
    return min(spec["canvas_w"], spec["canvas_h"] * spec["card_ratio_w"] / spec["card_ratio_h"])


def width_limit(spec):
    """Widest a product may be: max width (height mode) or fit box width, but never
    wider than the visible part of the card minus a 4% margin on each side."""
    lim = (spec.get("max_w") or spec["canvas_w"]) if spec.get("fit_mode") == "height" else spec["box_w"]
    return min(lim, visible_width(spec) * 0.92)


def cutout(im: Image.Image) -> Image.Image:
    """Remove a plain background at full resolution.

    Segmentation model as the base, combined with a strict colour-difference
    cut against the measured background colour. Connected parts that touch the
    product core are kept wherever they are (thin straps, legs, cords); soft
    shadows stay out because the colour threshold is strict. Holes fully
    enclosed by product are filled so white logos and stitching stay opaque.
    """
    from rembg import remove

    rgb_img = im.convert("RGB")
    rgb = np.asarray(rgb_img)
    bg, _ = border_stats(rgb)
    m = remove(rgb_img, session=session(), only_mask=True).convert("L")
    if m.size != rgb_img.size:
        m = m.resize(rgb_img.size, Image.BILINEAR)
    m = np.asarray(m, dtype=np.float32) / 255.0

    diff = np.abs(rgb.astype(np.int16) - bg).sum(2).astype(np.float32)
    cd_hard = diff > 45
    cd_soft = np.clip((diff - 15) / 30.0, 0, 1)
    core = m > 0.85

    cand = (m > 0.5) | cd_hard
    lab, n = ndimage.label(cand)
    if n == 0:
        raise ValueError("No product found")
    idx = np.arange(1, n + 1)
    hits = ndimage.sum(core, lab, index=idx)
    keep = idx[hits >= 50]
    if len(keep) == 0:
        keep = [idx[int(np.argmax(ndimage.sum(cand, lab, index=idx)))]]
    solid = ndimage.binary_fill_holes(np.isin(lab, keep))

    ring = ndimage.binary_dilation(solid, iterations=2) & ~solid
    alpha = np.where(solid, np.maximum(m, cd_soft), np.where(ring, m, 0.0))
    alpha[ndimage.binary_erosion(solid, iterations=3)] = 1.0
    alpha = np.clip(alpha, 0, 1)

    # Remove background colour that bleeds into semi-transparent edge pixels.
    out = rgb.astype(np.float32)
    edge = (alpha > 0.02) & (alpha < 0.98)
    a = alpha[..., None]
    fg = (out - (1 - a) * bg.astype(np.float32)) / np.maximum(a, 1e-3)
    out[edge] = np.clip(fg[edge], 0, 255)

    rgba = np.dstack([out.astype(np.uint8), (alpha * 255).round().astype(np.uint8)])
    return Image.fromarray(rgba, "RGBA")


def fit(rgba: Image.Image, spec: dict):
    """Crop to the visible product, scale it until it touches the fit box, centre it."""
    warnings = []
    a = np.asarray(rgba)[:, :, 3]
    box = bbox(ndimage.binary_opening(a > 8, iterations=1))
    if box is None:
        raise ValueError("Image is fully transparent")
    prod = rgba.crop(box)
    pw, ph = prod.size
    cw, ch, bh = spec["canvas_w"], spec["canvas_h"], spec["box_h"]
    limit = width_limit(spec)
    scale = min(limit / pw, bh / ph)
    if spec.get("fit_mode") == "height" and limit / pw < bh / ph:
        warnings.append(f"Wide product: kept {round(ph * scale)} px tall so it isn't cut off in the grid")
    if scale > 1.15:
        warnings.append(f"Upscaled {scale:.0%}: source is small, check sharpness")
    size = (max(1, round(pw * scale)), max(1, round(ph * scale)))
    # Resize premultiplied to avoid dark or light fringes.
    prod = prod.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    canvas.paste(prod, (round((cw - size[0]) / 2), round((ch - size[1]) / 2)), prod)
    return canvas, warnings


def process(data: bytes, spec: dict):
    """Full pipeline. Returns (png_bytes, preview_png_bytes, warnings)."""
    im = load(data)
    rgba = im.convert("RGBA") if has_alpha(im) else cutout(im)
    canvas, warnings = fit(rgba, spec)
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    pv = canvas.copy()
    pv.thumbnail((700, 700))
    pbuf = io.BytesIO()
    pv.save(pbuf, "PNG")
    return buf.getvalue(), pbuf.getvalue(), warnings
