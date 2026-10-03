"""Avatar pipeline — validate, circular-crop, normalize to WebP.

The client cropper sends ``{cx, cy, zoom}`` as fractions of the source
image's short side (all optional — defaults center-fit): ``cx``/``cy``
are the crop-circle center in 0..1 image space, ``zoom`` ≥ 1 shrinks
the crop square. The result is a 512×512 WebP with the circle masked
into the alpha channel, written as ``avatar.webp`` in the profile dir.
The uploaded original is never modified.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from .model import ProfileError

try:
    from PIL import Image, ImageDraw, UnidentifiedImageError
    _HAS_PIL = True
except ImportError:  # Pillow optional at import time; required at runtime
    _HAS_PIL = False
    UnidentifiedImageError = Exception

MAX_INPUT_BYTES = 25 * 1024 * 1024      # 25 MB — photos, not video
MIN_DIMENSION = 32                       # px — anything smaller is junk
OUT_SIZE = 512
AVATAR_NAME = "avatar.webp"


def _require_pil() -> None:
    if not _HAS_PIL:
        raise ProfileError("image support unavailable (Pillow missing)")


def validate_image(data: bytes) -> "Image.Image":
    """Decode + verify the upload is a real image. Returns an RGB image."""
    _require_pil()
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ProfileError("avatar image required")
    if len(data) > MAX_INPUT_BYTES:
        raise ProfileError("avatar image too large (max 25 MB)")
    try:
        im = Image.open(io.BytesIO(bytes(data)))
        im.verify()                      # full decode check, not sniffing
        im = Image.open(io.BytesIO(bytes(data)))  # verify() leaves it broken
        im.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ProfileError("not a valid image file") from exc
    w, h = im.size
    if w < MIN_DIMENSION or h < MIN_DIMENSION:
        raise ProfileError(
            f"image too small (min {MIN_DIMENSION}×{MIN_DIMENSION})")
    return im.convert("RGB")


def _clamp(v: Any, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return (lo + hi) / 2.0


def crop_circle(im: "Image.Image", crop: dict[str, Any] | None) -> "Image.Image":
    """Apply the client crop to a square image, resize to OUT_SIZE."""
    w, h = im.size
    crop = crop or {}
    zoom = _clamp(crop.get("zoom", 1), 1.0, 20.0)
    side = min(w, h) / zoom
    # Fraction-of-image center, clamped so the crop stays inside.
    half = side / 2.0
    cx = _clamp(crop.get("cx", 0.5), half / w, 1.0 - half / w) * w
    cy = _clamp(crop.get("cy", 0.5), half / h, 1.0 - half / h) * h
    box = (int(round(cx - half)), int(round(cy - half)),
           int(round(cx + half)), int(round(cy + half)))
    box = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
    if box[2] - box[0] < MIN_DIMENSION or box[3] - box[1] < MIN_DIMENSION:
        raise ProfileError("crop region too small")
    return im.crop(box).resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)


def _mask_circle(im: "Image.Image") -> "Image.Image":
    """Bake the circle into the alpha channel (transparent corners)."""
    mask = Image.new("L", (OUT_SIZE, OUT_SIZE), 0)
    ImageDraw.Draw(mask).ellipse(
        (0, 0, OUT_SIZE - 1, OUT_SIZE - 1), fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


def save_avatar(profile_dir: Path, image_bytes: bytes,
                crop: dict[str, Any] | None = None) -> str:
    """Validate → crop → circular-mask → write avatar.webp.

    Returns the profile-relative path for ``profile.avatar_path``.
    """
    im = validate_image(image_bytes)
    squared = crop_circle(im, crop)
    out = _mask_circle(squared)
    buf = io.BytesIO()
    out.save(buf, format="WEBP", quality=88, method=6)
    dest = Path(profile_dir) / AVATAR_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(buf.getvalue())
    return AVATAR_NAME
