"""Best-guess ComfyUI sampling params plus lightweight outcome learning.

Two layers:

1. Heuristics — every unset request field gets a sensible value based on
   the model family, the operation, the requested quality tier, and cues
   in the prompt itself ("subtle change" -> lower denoise).
2. Learning — thumbs feedback on image-bearing assistant messages records
   the effective param set as a win or loss per (family, operation) key.
   Future guesses prefer the params that earned wins and deliberately
   explore after repeated losses instead of repeating a bad recipe.

Explicit user controls ("cfg 4", "denoise 0.6", sampler/scheduler names,
tool args) always win — the advisor only fills what was left unset.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_bytes
from .types import ImageModelProfile, ImageRequest

_FIELDS = ("steps", "guidance", "denoise_strength", "sampler_name", "scheduler")

_SUBTLE_RE = re.compile(
    r"\b(subtle|small|slight|minor|tiny|lightly|keep most|preserve|gentle)\b",
    re.I)
_DRAMATIC_RE = re.compile(
    r"\b(dramatic|complet|transform|totally|drastic|entirely|fully|radical)\b",
    re.I)
_DETAIL_RE = re.compile(
    r"\b(more detail|sharper|crisper|higher quality|hi-?res|intricate|"
    r"more realistic|finer details?)\b", re.I)
_WASHED_RE = re.compile(
    r"\b(overcooked|over-?saturated|burnt|harsh|too saturated|washed out|"
    r"too intense|melted)\b", re.I)


def _family_defaults(profile: ImageModelProfile) -> dict[str, Any]:
    family = (profile.family or "").lower()
    if "stable-diffusion" in family:
        defaults = {"guidance": 6.5, "sampler_name": "dpmpp_2m", "scheduler": "karras",
                    "steps_fast": 18, "steps_balanced": 25, "steps_high": 35}
    elif "qwen" in family:
        defaults = {"guidance": 4.0, "sampler_name": "euler", "scheduler": "simple",
                    "steps_fast": 16, "steps_balanced": 25, "steps_high": 45}
    elif "flux" in family and "klein" in family:
        defaults = {"guidance": 1.0, "sampler_name": "euler", "scheduler": "simple",
                    "steps_fast": 4, "steps_balanced": 8, "steps_high": 12}
    elif "z-image" in family or "turbo" in family:
        defaults = {"guidance": 1.0, "sampler_name": "euler", "scheduler": "simple",
                    "steps_fast": 4, "steps_balanced": 8, "steps_high": 12}
    else:
        defaults = {"guidance": 4.0, "sampler_name": "euler", "scheduler": "simple",
                    "steps_fast": 16, "steps_balanced": 25, "steps_high": 40}
    # Fleet/managed models ship their own verified sampling defaults
    # (Juggernaut ≠ RealVis ≠ CyberRealistic) — they override the family
    # baseline but never explicit request values or learned outcomes.
    model_defaults = ((profile.metadata or {}).get("sampling") or {})
    if model_defaults.get("steps"):
        steps = int(model_defaults["steps"])
        defaults["steps_balanced"] = steps
        defaults["steps_high"] = steps + 10
        defaults["steps_fast"] = max(8, steps - 12)
    if model_defaults.get("guidance") is not None:
        defaults["guidance"] = float(model_defaults["guidance"])
    if model_defaults.get("sampler"):
        defaults["sampler_name"] = str(model_defaults["sampler"])
    if model_defaults.get("scheduler"):
        defaults["scheduler"] = str(model_defaults["scheduler"])
    return defaults


class SamplingAdvisor:
    """Fills unset sampling fields on an ImageRequest and learns from
    recorded outcomes so guesses improve instead of staying static."""

    def __init__(self, stats_path: Path) -> None:
        self.stats_path = Path(stats_path)
        self._lock = threading.RLock()
        self._stats: dict[str, Any] = {}
        try:
            raw = json.loads(self.stats_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._stats = raw
        except Exception:
            self._stats = {}

    # -- learning -----------------------------------------------------

    @staticmethod
    def _key(profile: ImageModelProfile, operation: str,
             backend: str = "comfyui") -> str:
        """Learning is keyed per backend — recipes that won on ComfyUI must
        not leak into InvokeAI guesses (different sampler vocabulary and
        CFG behavior). Managed fleet models additionally key per model
        (``model_scope``) so feedback on Juggernaut doesn't drift RealVis.
        The legacy ``family|op`` key is retained for unscoped models on
        ComfyUI so previously learned stats keep working."""
        scope = str(((profile.metadata or {}).get("sampling") or {})
                    .get("model_scope") or "")
        family = scope or (profile.family or "unknown").lower()
        base = f"{family}|{operation}"
        return base if backend == "comfyui" else f"{backend}|{base}"

    def record_outcome(self, profile: ImageModelProfile, operation: str,
                       request_params: dict, rating: str,
                       backend: str = "comfyui") -> None:
        """Persist a thumbs outcome against the job's effective params."""
        rating = (rating or "").lower()
        if rating not in {"up", "better", "down", "worse"}:
            return
        params = {k: request_params.get(k) for k in _FIELDS
                  if request_params.get(k) not in (None, "")}
        if not params:
            return
        key = self._key(profile, operation or "auto", backend)
        with self._lock:
            row = self._stats.setdefault(
                key, {"wins": 0, "losses": 0, "best": None, "avoid": []})
            if rating in {"up", "better"}:
                row["wins"] = int(row.get("wins", 0)) + 1
                row["best"] = params
                row["avoid"] = [a for a in (row.get("avoid") or []) if a != params]
            else:
                row["losses"] = int(row.get("losses", 0)) + 1
                if row.get("best") == params:
                    row["best"] = None
                avoid = row.setdefault("avoid", [])
                if params not in avoid:
                    avoid.append(params)
                    del avoid[:-4]
            self._save()

    def _save(self) -> None:
        try:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(
                self.stats_path,
                json.dumps(self._stats, indent=1).encode("utf-8"))
        except Exception:
            pass

    # -- guessing -----------------------------------------------------

    def apply(self, request: ImageRequest, profile: ImageModelProfile,
              operation: str, *, backend: str = "comfyui") -> dict[str, str]:
        """Fill unset sampling fields. Returns reason notes for routing."""
        notes: list[str] = []
        defaults = _family_defaults(profile)
        quality = (request.quality or "balanced").lower()
        learned = self._learned_params(profile, operation, backend)

        prompt_text = request.prompt or ""
        if request.steps is None:
            if quality in {"preview", "fast", "draft"}:
                guess = defaults["steps_fast"]
            elif quality in {"high", "max", "quality"} \
                    or _DETAIL_RE.search(prompt_text):
                guess = defaults["steps_high"]
                if _DETAIL_RE.search(prompt_text):
                    notes.append("detail cue -> more steps")
            else:
                guess = defaults["steps_balanced"]
            request.steps = int(learned.get("steps", guess))
            if "steps" in learned:
                notes.append("learned step count")
        if request.guidance is None:
            base_cfg = float(learned.get("guidance", defaults["guidance"]))
            if _WASHED_RE.search(prompt_text):
                base_cfg = max(1.0, base_cfg - 1.0)
                notes.append("overcooked cue -> lower cfg")
            request.guidance = base_cfg
            if "guidance" in learned:
                notes.append("learned cfg")
        if request.denoise_strength is None:
            # Conditioned-edit architectures (Qwen Image) resample fully —
            # denoise only shifts when the prompt asks for restraint.
            if _SUBTLE_RE.search(prompt_text):
                request.denoise_strength = 0.6
                notes.append("subtle edit -> lower denoise")
            elif _DRAMATIC_RE.search(prompt_text):
                request.denoise_strength = 1.0
            else:
                request.denoise_strength = float(
                    learned.get("denoise_strength", 1.0))
        if not request.sampler_name:
            request.sampler_name = str(
                learned.get("sampler_name", defaults["sampler_name"]))
        if not request.scheduler:
            request.scheduler = str(
                learned.get("scheduler", defaults["scheduler"]))
        return notes

    def _learned_params(self, profile: ImageModelProfile, operation: str,
                        backend: str = "comfyui") -> dict[str, Any]:
        with self._lock:
            row = self._stats.get(self._key(profile, operation, backend)) or {}
            best = row.get("best")
            if isinstance(best, dict) and best:
                return dict(best)
            # More losses than wins -> bounded exploration: nudge steps up
            # and cfg down slightly rather than repeat a rejected recipe.
            if int(row.get("losses", 0)) >= 2 \
                    and int(row.get("losses", 0)) > int(row.get("wins", 0)):
                avoid = row.get("avoid") or [{}]
                last = dict(avoid[-1]) if avoid else {}
                out = {}
                if isinstance(last.get("steps"), int):
                    out["steps"] = min(60, int(last["steps"]) + 10)
                if isinstance(last.get("guidance"), (int, float)):
                    out["guidance"] = max(1.0, float(last["guidance"]) - 0.5)
                return out
        return {}
