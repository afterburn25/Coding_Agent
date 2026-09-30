from __future__ import annotations

from .types import ImageModelProfile, ImageRequest, ImageRoutingDecision


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
        if "remove background" in text or "transparent background" in text:
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

    def choose(self, request: ImageRequest) -> ImageRoutingDecision:
        operation, reasons = self.infer_operation(request)
        required = self.required_capabilities(operation, request)

        if request.model_override and request.model_override != "auto":
            matches = [m for m in self.models if m.id == request.model_override]
            if not matches:
                raise KeyError(f"Unknown image model override: {request.model_override}")
            chosen = matches[0]
            reasons.append("manual image-model override")
        else:
            candidates = []
            for m in self.models:
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
                raise RuntimeError(f"No enabled image model supports: {', '.join(required)}")
            fitting = [c for c in candidates if c[0]]
            pool = fitting or candidates
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

    def get_profile(self, model_id: str) -> ImageModelProfile:
        for model in self.models:
            if model.id == model_id:
                return model
        raise KeyError(model_id)
