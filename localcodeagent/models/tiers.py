from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Central text-model tier catalog — the single source of truth for the
# runtime planner, the role ladder, and the installer's compile-time
# #define blocks (kept in sync by scripts/sync_model_catalog.py).
CATALOG_PATH = Path(__file__).resolve().parent / "model_tiers.json"

EXECUTION_GPU_NATIVE = "gpu_native"
EXECUTION_HYBRID = "hybrid_gpu_cpu"
EXECUTION_CPU_ONLY = "cpu_only"
EXECUTION_REMOTE = "remote"

EXECUTION_LABELS = {
    EXECUTION_GPU_NATIVE: "GPU-native",
    EXECUTION_HYBRID: "Hybrid GPU + CPU",
    EXECUTION_CPU_ONLY: "CPU-only",
    EXECUTION_REMOTE: "Remote",
}

# Canonical escalation ladder: role name → tier. Smallest sufficient model
# wins; higher tiers only when the task justifies them.
ROLE_LADDER: tuple[tuple[str, int], ...] = (
    ("utility", 1),
    ("lightweight_reasoner", 2),
    ("primary_coder", 3),
    ("deep_reasoner", 4),
)

# Legacy role names that still resolve to ladder roles for compatibility.
ROLE_ALIASES: dict[str, str] = {
    "fast_coder": "primary_coder",
    "general_assistant": "lightweight_reasoner",
    "light_coder": "lightweight_reasoner",
    "light_debugger": "lightweight_reasoner",
    "primary_reasoner": "primary_coder",
    "deep_coder": "deep_reasoner",
    "intent_classifier": "utility",
    "router_helper": "utility",
    "fast_general": "utility",
}


@dataclass(frozen=True, slots=True)
class ModelTier:
    id: str
    config_model_id: str
    tier: int
    display_name: str
    quantization: str
    filename: str
    url: str
    source_repo: str
    source_type: str
    sha256: str
    file_size: int
    license: str
    roles: tuple[str, ...]
    role_aliases: tuple[str, ...]
    ladder_role: str
    ladder_label: str
    context_window: int
    target_context: int
    keep_warm: bool
    minimum_vram_native_gb: float
    recommended_vram_native_gb: float
    minimum_ram_native_gb: float
    minimum_vram_hybrid_gb: float
    minimum_ram_hybrid_gb: float
    recommended_ram_hybrid_gb: float
    minimum_disk_gb: float
    cpu_only_min_ram_gb: float
    allow_hybrid: bool
    allow_cpu_only: bool
    execution_modes: tuple[str, ...]
    priority: int
    installer_default: bool
    description: str
    hybrid_note: str = ""

    @property
    def file_size_gb(self) -> float:
        return round(self.file_size / (1024 ** 3), 2)


@dataclass(slots=True)
class HardwareProfile:
    """Normalized install-time hardware facts. Dedicated VRAM only — shared
    Windows GPU memory is tracked separately and never counts as VRAM."""
    gpu_name: str = ""
    gpu_vendor: str = ""
    dedicated_vram_gb: float = 0.0
    shared_gpu_memory_gb: float = 0.0
    gpu_count: int = 1
    gpu_vram_gb_list: list[float] = field(default_factory=list)
    total_ram_gb: float = 0.0
    available_ram_gb: float = 0.0
    cpu_logical_cores: int = 0
    cpu_name: str = ""
    disk_free_gb: float = 0.0
    gpu_backend: str = "cpu"          # vulkan | cuda | cpu (llama.cpp surface)
    pagefile_gb: float = 0.0          # recorded for diagnostics, never usable

    @property
    def max_dedicated_vram_gb(self) -> float:
        """Largest single GPU's dedicated VRAM. Multi-GPU VRAM is never
        summed — model splitting is not assumed."""
        if self.gpu_vram_gb_list:
            return round(max(self.gpu_vram_gb_list), 2)
        return self.dedicated_vram_gb


@dataclass(slots=True)
class TierDecision:
    tier: ModelTier
    selected: bool
    execution: str            # gpu_native | hybrid_gpu_cpu | cpu_only | unsupported
    execution_label: str
    reason: str
    supported: bool           # selectable in advanced mode (vs blocked)
    near_native: bool = False # native but below recommended VRAM

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.tier.id,
            "config_model_id": self.tier.config_model_id,
            "tier": self.tier.tier,
            "display_name": self.tier.display_name,
            "quantization": self.tier.quantization,
            "filename": self.tier.filename,
            "url": self.tier.url,
            "sha256": self.tier.sha256,
            "file_size": self.tier.file_size,
            "file_size_gb": self.tier.file_size_gb,
            "source_repo": self.tier.source_repo,
            "roles": list(self.tier.roles),
            "ladder_label": self.tier.ladder_label,
            "selected": self.selected,
            "supported": self.supported,
            "execution": self.execution,
            "execution_label": self.execution_label,
            "near_native": self.near_native,
            "reason": self.reason,
            "installer_default": self.tier.installer_default,
        }


@dataclass(slots=True)
class ModelPlan:
    hardware: HardwareProfile
    decisions: list[TierDecision]
    download_bytes: int
    disk_free_gb: float
    disk_ok: bool
    summary_lines: list[str]

    @property
    def selected(self) -> list[TierDecision]:
        return [d for d in self.decisions if d.selected]

    def as_dict(self) -> dict[str, Any]:
        return {
            "hardware": {
                "gpu_name": self.hardware.gpu_name,
                "gpu_vendor": self.hardware.gpu_vendor,
                "dedicated_vram_gb": self.hardware.dedicated_vram_gb,
                "shared_gpu_memory_gb": self.hardware.shared_gpu_memory_gb,
                "gpu_count": self.hardware.gpu_count,
                "gpus_vram_gb": list(self.hardware.gpu_vram_gb_list),
                "total_ram_gb": self.hardware.total_ram_gb,
                "available_ram_gb": self.hardware.available_ram_gb,
                "cpu_logical_cores": self.hardware.cpu_logical_cores,
                "cpu_name": self.hardware.cpu_name,
                "disk_free_gb": self.hardware.disk_free_gb,
                "gpu_backend": self.hardware.gpu_backend,
            },
            "tiers": [d.as_dict() for d in self.decisions],
            "selected_ids": [d.tier.id for d in self.selected],
            "download_bytes": self.download_bytes,
            "download_gb": round(self.download_bytes / (1024 ** 3), 2),
            "disk_free_gb": self.disk_free_gb,
            "disk_ok": self.disk_ok,
        }


_catalog_cache: tuple[list[ModelTier], dict[str, Any]] | None = None


def _load_catalog() -> tuple[list[ModelTier], dict[str, Any]]:
    global _catalog_cache
    if _catalog_cache is None:
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        tiers = [
            ModelTier(
                **{k: v for k, v in row.items() if k in ModelTier.__dataclass_fields__}
                | {k: tuple(v) for k, v in row.items()
                   if k in {"roles", "role_aliases", "execution_modes"}}
            )
            for row in raw["tiers"]
        ]
        _catalog_cache = (tiers, raw)
    return _catalog_cache


def model_tiers() -> list[ModelTier]:
    return _load_catalog()[0]


def catalog_meta() -> dict[str, Any]:
    return _load_catalog()[1]


def tier_by_id(tier_id: str) -> ModelTier | None:
    return next((t for t in model_tiers() if t.id == tier_id), None)


def usable_ram_gb(profile: HardwareProfile) -> float:
    """RAM available to models after the system reserve. Pagefile never
    counts — a model that needs swapping is not supported."""
    meta = catalog_meta()["ram_reserve"]
    reserve = max(float(meta["absolute_gb"]),
                  profile.total_ram_gb * float(meta["fraction"]))
    return max(0.0, profile.total_ram_gb - reserve)


def classify_tier(tier: ModelTier, profile: HardwareProfile) -> tuple[str, str, bool]:
    """Return (execution, reason, near_native) for one tier on this hardware.

    Uses dedicated VRAM only for GPU qualification; shared memory and
    pagefile never count. Hybrid eligibility is judged here on hardware
    merits; the +1 tier-distance cap is applied by plan_model_stack.
    """
    vram = profile.max_dedicated_vram_gb
    usable_ram = usable_ram_gb(profile)

    if vram >= tier.minimum_vram_native_gb and usable_ram >= tier.minimum_ram_native_gb:
        near = vram < tier.recommended_vram_native_gb
        return (EXECUTION_GPU_NATIVE,
                f"{vram:.0f} GB dedicated VRAM meets native requirement "
                f"({tier.minimum_vram_native_gb} GB)", near)
    if (tier.allow_hybrid and tier.minimum_vram_hybrid_gb > 0
            and vram >= tier.minimum_vram_hybrid_gb
            and usable_ram >= tier.minimum_ram_hybrid_gb):
        return (EXECUTION_HYBRID,
                f"hybrid: {vram:.0f} GB VRAM offloads part of the model; "
                f"{usable_ram:.0f} GB usable RAM holds the rest", False)
    if (tier.allow_cpu_only and tier.cpu_only_min_ram_gb > 0
            and usable_ram >= tier.cpu_only_min_ram_gb):
        return (EXECUTION_CPU_ONLY,
                "no usable dedicated GPU VRAM; CPU-only inference will be slow", False)
    return ("unsupported", "", False)


def plan_model_stack(
    profile: HardwareProfile,
    *,
    tiers: list[ModelTier] | None = None,
    extra_allowed_tiers: set[int] | None = None,
) -> ModelPlan:
    """Decide which tiers this machine should install and how each executes.

    Rules (docs/HARDWARE_MODEL_TIERS.md):
      - dedicated VRAM only for GPU-native qualification
      - all qualifying tiers install (largest supported + every lower tier)
      - at most ONE hybrid tier above the highest native tier
      - usable RAM = total − reserve; pagefile never counts
      - low disk blocks selection
    """
    tiers = tiers or model_tiers()
    meta = catalog_meta()
    max_hybrid_distance = int(meta["hybrid_policy"]["max_tier_distance"])
    vram = profile.max_dedicated_vram_gb
    usable_ram = usable_ram_gb(profile)

    # Pass 1: classify each tier on hardware merits alone.
    native_max_tier = 0
    preliminary: dict[int, tuple[str, str, bool]] = {}
    for t in tiers:
        execution, reason, near = classify_tier(t, profile)
        preliminary[t.tier] = (execution, reason, near)
        if execution == EXECUTION_GPU_NATIVE:
            native_max_tier = max(native_max_tier, t.tier)

    hybrid_ceiling = native_max_tier + max_hybrid_distance
    decisions: list[TierDecision] = []
    for t in sorted(tiers, key=lambda x: x.tier):
        execution, reason, near = preliminary[t.tier]

        if execution == EXECUTION_HYBRID and t.tier > hybrid_ceiling:
            execution = "unsupported"
            reason = (f"tier {t.tier} exceeds hybrid limit (max tier "
                      f"{hybrid_ceiling} on this hardware; system RAM cannot "
                      f"raise capability by more than one tier)")
        elif execution == "unsupported":
            if usable_ram < t.minimum_ram_native_gb:
                reason = (f"needs {t.minimum_vram_native_gb:.0f} GB VRAM and "
                          f"{t.minimum_ram_native_gb:.0f} GB usable RAM "
                          f"(has {vram:.0f} GB / {usable_ram:.0f} GB usable)")
            elif t.allow_hybrid and t.minimum_vram_hybrid_gb > 0 and vram >= t.minimum_vram_hybrid_gb:
                reason = (f"insufficient system RAM for hybrid offload "
                          f"(needs {t.minimum_ram_hybrid_gb:.0f} GB usable, "
                          f"has {usable_ram:.0f} GB)")
            elif t.allow_hybrid and t.minimum_vram_hybrid_gb > 0:
                reason = (f"insufficient dedicated VRAM for useful GPU offload "
                          f"(needs {t.minimum_vram_hybrid_gb:.0f} GB)")
            else:
                reason = (f"needs {t.minimum_vram_native_gb:.0f} GB dedicated VRAM "
                          f"(has {vram:.0f} GB)")

        supported = execution in {EXECUTION_GPU_NATIVE, EXECUTION_HYBRID, EXECUTION_CPU_ONLY}
        if not supported and not t.allow_hybrid and not t.allow_cpu_only:
            supported = False
        decisions.append(TierDecision(
            tier=t,
            selected=bool(t.installer_default and supported),
            execution=execution,
            execution_label=EXECUTION_LABELS.get(execution, "Unsupported"),
            reason=reason,
            supported=supported,
            near_native=near,
        ))

    # Manual overrides (advanced selection) can raise supported-but-not-default
    # or explicitly allowed unsupported tiers.
    if extra_allowed_tiers:
        for d in decisions:
            if d.tier.tier in extra_allowed_tiers and not d.selected:
                d.selected = True
                if not d.supported:
                    d.supported = True
                    d.execution = EXECUTION_HYBRID if d.tier.allow_hybrid else EXECUTION_CPU_ONLY
                    d.execution_label = EXECUTION_LABELS[d.execution] + " (unsupported override)"
                    d.reason = "manually selected beyond automatic recommendation"

    download_bytes = sum(d.tier.file_size for d in decisions if d.selected)
    disk_free = profile.disk_free_gb
    disk_ok = disk_free <= 0 or disk_free >= (download_bytes / (1024 ** 3)) + 5.0
    if not disk_ok:
        for d in decisions:
            if d.selected:
                d.selected = False
                d.supported = False
                d.execution = "unsupported"
                d.execution_label = "Unsupported"
                d.reason = (f"insufficient free disk ({disk_free:.0f} GB free, "
                            f"need ~{download_bytes / (1024 ** 3):.0f} GB + margin)")
        download_bytes = 0

    lines = []
    if profile.gpu_name:
        lines.append(f"GPU: {profile.gpu_name}")
    lines.append(f"Dedicated VRAM: {profile.dedicated_vram_gb:.0f} GB")
    if profile.shared_gpu_memory_gb:
        lines.append(f"Shared GPU memory: {profile.shared_gpu_memory_gb:.0f} GB (not counted as VRAM)")
    lines.append(f"System RAM: {profile.total_ram_gb:.0f} GB")
    if profile.cpu_logical_cores:
        lines.append(f"CPU: {profile.cpu_logical_cores} logical cores")
    lines.append(f"Disk free: {profile.disk_free_gb:.0f} GB")
    lines.append(f"Backend: {profile.gpu_backend}")
    return ModelPlan(
        hardware=profile,
        decisions=decisions,
        download_bytes=download_bytes,
        disk_free_gb=disk_free,
        disk_ok=disk_ok,
        summary_lines=lines,
    )


def ladder_role_for_tier(tier_number: int) -> str:
    for role, num in ROLE_LADDER:
        if num == tier_number:
            return role
    return "utility"


def canonical_role(role: str) -> str:
    """Map legacy/extra role names onto the canonical ladder roles."""
    r = str(role or "").strip()
    if r in {r0 for r0, _ in ROLE_LADDER}:
        return r
    return ROLE_ALIASES.get(r, r)


def tier_for_role(role: str) -> int:
    canonical = canonical_role(role)
    for r, num in ROLE_LADDER:
        if r == canonical:
            return num
    return 3  # unknown roles default to the primary workhorse tier
