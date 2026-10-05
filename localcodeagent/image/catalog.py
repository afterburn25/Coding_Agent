from __future__ import annotations

from pathlib import Path


IMAGE_EXTENSIONS = {".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin"}


def discover_image_models(root: Path) -> list[dict]:
    rows: list[dict] = []
    if not root.exists():
        return rows
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS):
        name = path.name.lower()
        family = "unknown"
        if "qwen" in name or "qwen" in str(path.parent).lower():
            family = "qwen-image"
        elif "flux" in name or "flux" in str(path.parent).lower():
            family = "flux"
        elif "sdxl" in name:
            family = "stable-diffusion-xl"
        elif "stable" in name or "sd" in name:
            family = "stable-diffusion"
        elif "esrgan" in name or "upscal" in name or "upscaler" in str(path.parent).lower():
            family = "upscaler"
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append({
            "name": path.name,
            "path": str(path.resolve()),
            "family": family,
            "size_gb": round(size / (1024 ** 3), 2),
            "extension": path.suffix.lower(),
            **classify_model(path.name, family=family),
        })
    return rows


_ADULT_MARKERS = ("nsfw", "porn", "xxx", "adult", "hentai", "lewd",
                  "uncensored", "unfiltered", "r18")
_PHOTOREAL_MARKERS = ("realism", "realistic", "realvis", "photon",
                      "juggernaut", "epicrealism", "cyberrealistic",
                      "absolutebeauty", "film", "photo")
_ILLUSTRATION_MARKERS = ("anime", "animagine", "pony", "waifu", "anything",
                         "illustration", "cartoon", "comic", "art")
_INPAINT_MARKERS = ("inpaint", "inpaint")
_UPSCALE_MARKERS = ("esrgan", "upscal", "swinir", "ultrasharp", "4x", "remacri")
_BACKGROUND_MARKERS = ("rembg", "birefnet", "isnet", "u2net", "rmbg")
_LORA_MARKERS = ("lora", "-lora", "_lora")


def classify_model(name: str, *, family: str = "", notes: str = "",
                   model_type: str = "") -> dict[str, str]:
    """Classify an image model's capability class + restriction status.

    Pure descriptive metadata — it informs routing/model browsing and
    NEVER relaxes policy gates. Unknown markers surface as honest
    "general"/"unknown_capability" rather than an invented label."""
    text = f"{name} {family} {notes}".lower()
    mtype = (model_type or "").lower()

    if mtype in {"lora", "lycoris"} or any(m in text for m in _LORA_MARKERS):
        cls = "lora"
    elif any(m in text for m in _BACKGROUND_MARKERS):
        cls = "background_removal"
    elif any(m in text for m in _UPSCALE_MARKERS) or mtype == "spandrel":
        cls = "upscale"
    elif any(m in text for m in _INPAINT_MARKERS):
        cls = "inpainting"
    elif any(m in text for m in _PHOTOREAL_MARKERS):
        cls = "photoreal"
    elif any(m in text for m in _ILLUSTRATION_MARKERS):
        cls = "illustration"
    else:
        cls = "general"

    if any(m in text for m in _ADULT_MARKERS):
        restriction = "adult_capable"
        if "unfiltered" in text or "uncensored" in text:
            restriction = "local_unfiltered_model"
    else:
        restriction = "unknown_capability"

    return {"capability_class": cls, "restriction_status": restriction}
