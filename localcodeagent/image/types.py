from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ImageModelProfile:
    id: str
    family: str
    backend: str = "comfyui"
    model_path: str = ""
    workflow: str = ""
    capabilities: list[str] = field(default_factory=lambda: ["text_to_image"])
    priority: int = 50
    enabled: bool = True
    quantization: str = ""
    estimated_vram_gb: float = 0.0
    estimated_ram_gb: float = 0.0
    max_reference_images: int = 0
    supports_transparency: bool = False
    speed_tier: str = "balanced"
    quality_tier: str = "balanced"
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ImageRequest:
    prompt: str
    operation: str = "auto"
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    count: int = 1
    seed: int | None = None
    quality: str = "balanced"
    reference_images: list[str] = field(default_factory=list)
    mask_path: str = ""
    source_image: str = ""
    subject_profile: str = ""
    model_override: str = "auto"
    loras: list[dict[str, Any]] = field(default_factory=list)
    image_strength: float | None = None
    denoise_strength: float | None = None
    outpaint: dict[str, int] = field(default_factory=dict)
    transparent_background: bool = False
    upscale: bool = False
    refine_details: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ImageRoutingDecision:
    model_id: str
    operation: str
    workflow: str
    reasons: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ImageJob:
    id: str
    request: dict[str, Any]
    state: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    model_id: str = ""
    operation: str = ""
    workflow: str = ""
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    outputs: list[str] = field(default_factory=list)
    error: str = ""
    routing_reasons: list[str] = field(default_factory=list)
    vram_before_gb: float = 0.0
    vram_after_gb: float = 0.0
    backend_job_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
