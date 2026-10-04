"""Worker roles, resource estimates, and task classification.

A worker is a *logical* execution unit — not a permanently resident model
process. Each admitted task carries a ResourceEstimate that the manager
reserves before launch; the estimate comes from the role profile, optionally
refined by the per-hardware cost learner.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


# Canonical worker roles — Specialist Brains in brain/specialists.py supply
# the cognitive side; this table supplies the *execution* resource profile.
ROLES = (
    "coding", "reasoning", "research", "diagnostics", "build_test",
    "tool", "image", "self_repair", "reviewer", "integrator",
    "maintenance", "planning",
)

# Priority bands (lower = sooner). Interactive work always outranks
# background autonomy; aging lifts long-waiting entries.
PRIORITY = {
    "interactive": 0,
    "user_foreground": 10,
    "critical_repair": 20,
    "user_queued": 30,
    "mission": 50,
    "background_research": 60,
    "optimization": 70,
    "maintenance": 80,
}


@dataclass(slots=True)
class ResourceEstimate:
    """What one worker is expected to consume while running."""
    cpu_cores: float = 0.5          # fraction of logical CPUs (0..N)
    ram_mb: int = 512
    vram_mb: int = 0
    gpu: float = 0.0                # 0..1 share of GPU compute
    io: float = 0.2                 # 0..1 disk/IO weight
    model_tier: str = ""            # "", "4B", "8B", "14B", "30B"
    model_slot: int = 0             # inference slots needed (usually 1 if tier set)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Default per-role profiles — deliberately conservative; the cost learner
# replaces them with measured values as telemetry accumulates.
ROLE_PROFILES: dict[str, ResourceEstimate] = {
    # 14B coding workhorse — biggest normal workload
    "coding":      ResourceEstimate(cpu_cores=2.0, ram_mb=6144, vram_mb=6144,
                                    gpu=0.5, io=0.4, model_tier="14B",
                                    model_slot=1),
    "reasoning":   ResourceEstimate(cpu_cores=1.5, ram_mb=4096, vram_mb=4096,
                                    gpu=0.35, io=0.1, model_tier="14B",
                                    model_slot=1),
    # research rides the 8B tier
    "research":    ResourceEstimate(cpu_cores=1.0, ram_mb=3072, vram_mb=3072,
                                    gpu=0.25, io=0.3, model_tier="8B",
                                    model_slot=1),
    "diagnostics": ResourceEstimate(cpu_cores=0.5, ram_mb=1024, vram_mb=1024,
                                    gpu=0.1, io=0.3, model_tier="8B",
                                    model_slot=1),
    # builds/tests are CPU+IO heavy, no model slot needed
    "build_test":  ResourceEstimate(cpu_cores=4.0, ram_mb=4096, vram_mb=0,
                                    gpu=0.0, io=0.8),
    "tool":        ResourceEstimate(cpu_cores=1.0, ram_mb=1024, vram_mb=0,
                                    gpu=0.0, io=0.5),
    # ComfyUI — VRAM-heavy, model_slot=0 (it is its own engine)
    "image":       ResourceEstimate(cpu_cores=1.5, ram_mb=6144, vram_mb=8192,
                                    gpu=0.8, io=0.4),
    "self_repair": ResourceEstimate(cpu_cores=2.0, ram_mb=6144, vram_mb=6144,
                                    gpu=0.5, io=0.4, model_tier="14B",
                                    model_slot=1),
    # reviewer prefers the deep tier when it fits; escalations only
    "reviewer":    ResourceEstimate(cpu_cores=2.0, ram_mb=8192, vram_mb=9216,
                                    gpu=0.6, io=0.2, model_tier="30B",
                                    model_slot=1),
    "integrator":  ResourceEstimate(cpu_cores=1.0, ram_mb=2048, vram_mb=1024,
                                    gpu=0.1, io=0.6, model_tier="8B",
                                    model_slot=1),
    "maintenance": ResourceEstimate(cpu_cores=1.0, ram_mb=1024, vram_mb=0,
                                    gpu=0.0, io=0.4),
    "planning":    ResourceEstimate(cpu_cores=1.0, ram_mb=3072, vram_mb=3072,
                                    gpu=0.25, io=0.1, model_tier="8B",
                                    model_slot=1),
    # catch-all for unclassified work
    "generic":     ResourceEstimate(cpu_cores=1.0, ram_mb=2048, vram_mb=1024,
                                    gpu=0.1, io=0.3),
}


# Task-kind → default role when the planner didn't name one.
KIND_ROLE = {
    "agent": "coding",
    "verify": "build_test",
    "research": "research",
    "internal": "tool",
    "job": "tool",
    "wait": "tool",
    "integrate": "integrator",
    "review": "reviewer",
}

# Keywords that steer classification toward a role — the LLM may assist with
# semantics but this deterministic pass makes the first classification.
_ROLE_HINTS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(p, re.I), r) for p, r in (
        (r"\b(test|pytest|unittest|build|compile|lint|typecheck)\b", "build_test"),
        (r"\b(image|picture|render|comfy|diffusion|photo|wallpaper)\b", "image"),
        (r"\b(research|investigate|look up|search|find out|summari[sz]e)\b", "research"),
        (r"\b(review|audit|verify|check the)\b", "reviewer"),
        (r"\b(merge|integrate|combine|conflict)\b", "integrator"),
        (r"\b(repair|fix (itself|nexus|the bug)|self.?repair)\b", "self_repair"),
        (r"\b(plan|decompose|break down|architect)\b", "planning"),
        (r"\b(clean|maintain|prune|vacuum|tidy)\b", "maintenance"),
        (r"\b(diagnos|debug|trace|why did|inspect)\b", "diagnostics"),
        (r"\b(implement|write|code|create|refactor|add|patch|build me)\b", "coding"),
    ))


def classify_role(text: str, *, kind: str = "", hinted: str = "") -> str:
    """Deterministic first-pass role classification. ``hinted`` wins when it
    is a known role (planner/LLM suggestion), else keyword match, else the
    task-kind default."""
    if hinted in ROLE_PROFILES:
        return hinted
    t = (text or "")[:4000]
    for pattern, role in _ROLE_HINTS:
        if pattern.search(t):
            return role
    return KIND_ROLE.get(kind or "", "generic")


def estimate_for(role: str, *, overrides: dict | None = None,
                 learned: ResourceEstimate | None = None) -> ResourceEstimate:
    """Resource estimate for a role — learned profile beats the static
    default; explicit overrides beat both."""
    base = learned or ROLE_PROFILES.get(role) or ROLE_PROFILES["generic"]
    est = ResourceEstimate(**asdict(base))
    for k, v in (overrides or {}).items():
        if hasattr(est, k):
            setattr(est, k, v)
    return est
