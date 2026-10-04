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
        })
    return rows
