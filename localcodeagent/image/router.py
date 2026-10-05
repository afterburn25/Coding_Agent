from __future__ import annotations

import re

from .types import ImageModelProfile, ImageRequest, ImageRoutingDecision
from .fleet import classify_request_traits, score_fleet_model, FLEET_BY_ID

# ComfyUI KSampler vocabulary — friendly spellings users actually type mapped
# to canonical sampler_name values.
_SAMPLER_ALIASES = {
    "euler": "euler", "euler a": "euler_ancestral",
    "euler ancestral": "euler_ancestral", "euler cfg": "euler_cfg_pp",
    "heun": "heun", "heunpp2": "heunpp2", "heunpp 2": "heunpp2",
    "dpm2": "dpm_2", "dpm 2": "dpm_2", "dpm2 a": "dpm_2_ancestral",
    "dpm 2 a": "dpm_2_ancestral", "dpm fast": "dpm_fast",
    "dpm adaptive": "dpm_adaptive", "lms": "lms",
    "dpm++ 2s a": "dpmpp_2s_ancestral", "dpmpp 2s a": "dpmpp_2s_ancestral",
    "dpmpp sde": "dpmpp_sde", "dpm++ sde": "dpmpp_sde",
    "dpmpp 2m": "dpmpp_2m", "dpm++ 2m": "dpmpp_2m",
    "dpmpp 2m sde": "dpmpp_2m_sde", "dpm++ 2m sde": "dpmpp_2m_sde",
    "dpmpp 3m sde": "dpmpp_3m_sde", "dpm++ 3m sde": "dpmpp_3m_sde",
    "ddim": "ddim", "ddpm": "ddpm", "lcm": "lcm",
    "uni pc": "uni_pc", "uni_pc": "uni_pc", "restart": "restart",
    "edm sde": "edm_sde", "seeds 2": "seeds_2", "seeds 3": "seeds_3",
    "er sde": "er_sde", "res multistep": "res_multistep",
}
_SCHEDULER_ALIASES = {
    "normal": "normal", "karras": "karras", "exponential": "exponential",
    "simple": "simple", "beta": "beta", "sgm uniform": "sgm_uniform",
    "sgm_uniform": "sgm_uniform", "ddim uniform": "ddim_uniform",
    "ddim_uniform": "ddim_uniform", "linear quadratic": "linear_quadratic",
    "linear_quadratic": "linear_quadratic", "kl optimal": "kl_optimal",
    "kl_optimal": "kl_optimal", "bong tangent": "bong_tangent",
    "bong_tangent": "bong_tangent",
}
# Longest-first so "dpmpp 2m sde" wins over "dpmpp 2m".
_SAMPLER_NAMES = sorted(_SAMPLER_ALIASES, key=len, reverse=True)
_SCHEDULER_NAMES = sorted(_SCHEDULER_ALIASES, key=len, reverse=True)

_NUMBER = r"(\d+(?:\.\d+)?)"


def parse_sampling_controls(text: str) -> tuple[str, dict]:
    """Pull ComfyUI sampler controls out of a natural-language image request.

    Understands: "cfg 4", "guidance=6.5", "30 steps", "denoise 0.6",
    "denoise 60%", "seed 42", "sampler euler a", "with dpmpp 2m sde",
    "karras scheduler", "scheduler: exponential".

    Returns (cleaned_text, controls) — matched spans are removed from the
    prompt text so they don't pollute the conditioning string."""
    controls: dict = {}
    cleaned = text or ""

    def _take(pattern: str, apply) -> None:
        nonlocal cleaned
        m = re.search(pattern, cleaned, re.I)
        if m:
            apply(m)
            cleaned = (cleaned[:m.start()] + " " + cleaned[m.end():])

    _take(rf"\b(?:cfg(?:\s+scale)?|guidance(?:\s+scale)?)\s*(?:of|=|:)?\s*{_NUMBER}\b",
          lambda m: controls.__setitem__("guidance", float(m.group(1))))
    _take(rf"\bdenoise(?:\s+strength)?\s*(?:of|=|:)?\s*{_NUMBER}\s*%",
          lambda m: controls.__setitem__("denoise_strength",
                                         max(0.0, min(1.0, float(m.group(1)) / 100.0))))
    if "denoise_strength" not in controls:
        _take(rf"\bdenoise(?:\s+strength)?\s*(?:of|=|:)?\s*{_NUMBER}\b",
              lambda m: controls.__setitem__("denoise_strength",
                                             max(0.0, min(1.0, float(m.group(1))))))
    _take(rf"\b(\d+)\s*(?:sampling\s+)?steps\b|\bsteps\s*(?:of|=|:)?\s*(\d+)\b",
          lambda m: controls.__setitem__("steps",
                                         int(m.group(1) or m.group(2))))
    _take(r"\bseed\s*(?:of|=|:)?\s*(\d+)\b",
          lambda m: controls.__setitem__("seed", int(m.group(1))))

    sampler_alt = "|".join(re.escape(n) for n in _SAMPLER_NAMES)
    _take(rf"\b(?:sampler(?:_name)?|use|with)\s*[ :=]*\s*({sampler_alt})\s*(?:sampler)?\b",
          lambda m: controls.__setitem__("sampler_name",
                                         _SAMPLER_ALIASES[m.group(1).lower()]))
    _take(rf"\b({sampler_alt})\s+sampler\b",
          lambda m: controls.__setitem__("sampler_name",
                                         _SAMPLER_ALIASES[m.group(1).lower()]))

    sched_alt = "|".join(re.escape(n) for n in _SCHEDULER_NAMES)
    _take(rf"\b(?:scheduler|use|with)\s*[ :=]*\s*({sched_alt})\s*(?:scheduler)?\b",
          lambda m: controls.__setitem__("scheduler",
                                         _SCHEDULER_ALIASES[m.group(1).lower()]))
    _take(rf"\b({sched_alt})\s+scheduler\b",
          lambda m: controls.__setitem__("scheduler",
                                         _SCHEDULER_ALIASES[m.group(1).lower()]))

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;")
    # Dangling connectors left where a control span was pulled out —
    # "make this transparent with denoise 0.6" → "make this transparent with".
    cleaned = re.sub(r"\s*(?:\bwith\b|\buse\b|\busing\b|\bat\b|\band\b)\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(?:with|use|using)\s+", "", cleaned, flags=re.I).strip(" ,;")
    return cleaned, controls


class ImageRouter:
    """Routes conversational image requests to installed/configured image models."""

    def __init__(self, models: list[ImageModelProfile], resource_fit=None) -> None:
        self.models = [m for m in models if m.enabled]
        self.resource_fit = resource_fit

    @staticmethod
    def infer_operation(request: ImageRequest) -> tuple[str, list[str]]:
        if request.operation and request.operation != "auto":
            return request.operation, ["explicit image operation"]
        text = request.prompt.lower()
        if request.mask_path or "inpaint" in text or "remove the person" in text or "remove person" in text:
            return "inpaint", ["mask/local replacement intent detected"]
        if request.outpaint or "outpaint" in text or "extend this image" in text or "extend the image" in text:
            return "outpaint", ["canvas extension intent detected"]
        bg_phrases = (
            "remove background", "remove the background", "remove its background",
            "remove my background", "background removal", "transparent background",
            "erase the background", "erase background", "delete the background",
            "delete background", "get rid of the background", "no background",
            "cut out", "cutout", "knock out", "isolate the subject",
            "isolate the person", "isolate her", "isolate him",
        )
        if (any(p in text for p in bg_phrases)
                or re.search(r"\bcut\s+(?:\w{1,12}\s+)?out\b", text)
                or ("transparent" in text and (request.source_image or request.reference_images))
                or (request.transparent_background and (request.source_image or request.reference_images))):
            return "remove_background", ["background removal intent detected"]
        if "upscale" in text or "higher resolution" in text:
            return "upscale", ["upscale intent detected"]
        if request.source_image or request.reference_images:
            return "edit_image", ["source/reference image supplied"]
        if "variation" in text or "more pictures of" in text or "additional images" in text:
            return "variation", ["variation/character continuation intent detected"]
        return "text_to_image", ["text generation request"]

    @staticmethod
    def required_capabilities(operation: str, request: ImageRequest) -> list[str]:
        mapping = {
            "text_to_image": ["text_to_image"],
            "edit_image": ["image_edit"],
            "inpaint": ["inpaint"],
            "outpaint": ["outpaint"],
            "remove_background": ["background_removal"],
            "upscale": ["upscale"],
            "variation": ["image_edit"],
        }
        caps = list(mapping.get(operation, [operation]))
        if len(request.reference_images) > 1:
            caps.append("multi_reference")
        if request.transparent_background:
            caps.append("transparency")
        return caps

    def choose(self, request: ImageRequest,
               backend: str | None = None) -> ImageRoutingDecision:
        operation, reasons = self.infer_operation(request)
        required = self.required_capabilities(operation, request)

        pool_models = self.models
        if backend:
            pool_models = [m for m in pool_models
                           if (m.backend or "comfyui") == backend]
            reasons.append(f"backend pool: {backend}")

        if request.model_override and request.model_override != "auto":
            matches = [m for m in pool_models if m.id == request.model_override]
            if not matches:
                # Fleet ids resolve to whichever installed backend row the
                # fleet spec matched (e.g. "juggernaut-xl-v9" →
                # "invokeai:<uuid>").
                matches = [m for m in pool_models
                           if (m.metadata or {}).get("fleet_id")
                           == request.model_override]
            if not matches:
                matches = [m for m in self.models
                           if m.id == request.model_override
                           or (m.metadata or {}).get("fleet_id")
                           == request.model_override]
                if matches and backend and (matches[0].backend or "comfyui") != backend:
                    raise RuntimeError(
                        f"Model override '{request.model_override}' runs on "
                        f"'{matches[0].backend or 'comfyui'}', not '{backend}' — "
                        "use Auto or pick a model on the selected backend")
            if not matches:
                raise KeyError(f"Unknown image model override: {request.model_override}")
            chosen = matches[0]
            reasons.append("manual image-model override")
        else:
            fleet_pick = self._fleet_pick(pool_models, request, operation, reasons)
            if fleet_pick is not None:
                chosen = fleet_pick
                workflow = chosen.workflow_for(operation)
                if request.quality in {"preview", "fast", "draft"}:
                    reasons.append("speed-focused image request")
                elif request.quality in {"high", "max", "quality"}:
                    reasons.append("quality-focused image request")
                if request.reference_images:
                    reasons.append(f"{len(request.reference_images)} reference image(s)")
                return ImageRoutingDecision(
                    model_id=chosen.id,
                    operation=operation,
                    workflow=workflow,
                    reasons=reasons,
                    required_capabilities=required,
                )
            candidates = []
            for m in pool_models:
                caps = set(m.capabilities)
                if not all(cap in caps for cap in required if cap not in {"multi_reference", "transparency"}):
                    continue
                if "multi_reference" in required and m.max_reference_images < len(request.reference_images):
                    continue
                if "transparency" in required and not m.supports_transparency:
                    continue
                fits, resource_score, resource_reason = (True, 0, "")
                if self.resource_fit:
                    fits, resource_score, resource_reason = self.resource_fit(m)
                quality_score = 0
                if request.quality in {"high", "max", "quality"}:
                    quality_score += 20 if m.quality_tier == "high" else 0
                elif request.quality in {"preview", "fast", "draft"}:
                    quality_score += 20 if m.speed_tier == "fast" else 0
                candidates.append((fits, resource_score + quality_score + m.priority, m, resource_reason))
            if not candidates:
                scope = f" on backend '{backend}'" if backend else ""
                raise RuntimeError(f"No enabled image model{scope} supports: {', '.join(required)}")
            fitting = [c for c in candidates if c[0]]
            pool = fitting or candidates
            # Fast/draft requests go to a fast-tier model when one can serve
            # the operation — a higher-priority quality model must never steal
            # a preview the user asked to be quick.
            if request.quality in {"preview", "fast", "draft"}:
                fast = [c for c in pool if c[2].speed_tier == "fast"]
                if fast:
                    pool = fast
            pool.sort(key=lambda item: (-item[1], item[2].id))
            fits, _, chosen, resource_reason = pool[0]
            if resource_reason:
                reasons.append(f"{chosen.id}: {resource_reason}")
            if not fits:
                reasons.append("no configured image model fully fits current resources; using best available fallback")

        if request.quality in {"preview", "fast", "draft"}:
            reasons.append("speed-focused image request")
        elif request.quality in {"high", "max", "quality"}:
            reasons.append("quality-focused image request")
        if request.reference_images:
            reasons.append(f"{len(request.reference_images)} reference image(s)")
        return ImageRoutingDecision(
            model_id=chosen.id,
            operation=operation,
            workflow=chosen.workflow_for(operation),
            reasons=reasons,
            required_capabilities=required,
        )

    # Fleet-routable operations — SDXL main models only; upscale/background
    # removal still go through the capability pipeline below.
    _FLEET_OPS = {"text_to_image", "edit_image", "inpaint", "variation"}

    def _fleet_pick(self, pool: list[ImageModelProfile],
                    request: ImageRequest, operation: str,
                    reasons: list[str]) -> ImageModelProfile | None:
        """Capability-based model selection across the photoreal fleet.

        Scores fleet-tagged profiles against classified request traits.
        Returns None (generic routing) when no fleet model is installed or
        the request carries no photoreal signal — never invents a pick."""
        fleet_models = [m for m in pool if (m.metadata or {}).get("fleet_id")]
        if not fleet_models or operation not in self._FLEET_OPS:
            return None
        info = classify_request_traits(request.prompt)
        traits = dict(info["traits"])
        if info["photoreal"]:
            traits["photoreal"] = max(traits.get("photoreal", 0), 2)
        if not traits:
            return None
        scored: list[tuple[int, ImageModelProfile]] = []
        for m in fleet_models:
            spec = FLEET_BY_ID.get(str(m.metadata.get("fleet_id")))
            if spec is None:
                continue
            score = score_fleet_model(spec, traits)
            if score > 0:
                scored.append((score + m.priority, m))
        if not scored:
            return None
        scored.sort(key=lambda r: (-r[0], r[1].id))
        for score, model in scored:
            fits, _rs, res_reason = (True, 0, "")
            if self.resource_fit:
                fits, _rs, res_reason = self.resource_fit(model)
            if not fits:
                reasons.append(
                    f"fleet: {model.id} skipped — {res_reason or 'resource fit'}")
                continue
            label = model.metadata.get("fleet_display") or model.id
            role = model.metadata.get("fleet_role") or "photoreal"
            top = ", ".join(t for t, _ in sorted(
                traits.items(), key=lambda kv: -kv[1])[:3])
            reasons.append(
                f"fleet routing: {label} ({role}) — request traits: {top}")
            return model
        return None

    def get_profile(self, model_id: str) -> ImageModelProfile:
        for model in self.models:
            if model.id == model_id:
                return model
        raise KeyError(model_id)
