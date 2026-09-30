from __future__ import annotations

import json
from typing import Any

from ..image.manager import ImageManager
from ..image.types import ImageRequest
from .base import ToolRegistry, ToolSpec


def _schema(extra: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    props={
        "prompt":{"type":"string"}, "negative_prompt":{"type":"string"},
        "source_image":{"type":"string"}, "reference_images":{"type":"array","items":{"type":"string"}},
        "mask_path":{"type":"string"}, "subject_profile":{"type":"string"},
        "model_override":{"type":"string","default":"auto"}, "quality":{"type":"string","default":"balanced"},
        "width":{"type":"integer","minimum":64}, "height":{"type":"integer","minimum":64},
        "count":{"type":"integer","minimum":1,"maximum":8}, "seed":{"type":"integer"},
        "real_person":{"type":"boolean","description":"True only when source/reference depicts an identifiable real person."},
    }
    if extra: props.update(extra)
    return {"type":"object","properties":props,"required":required or ["prompt"]}


def register_image_tools(registry: ToolRegistry, manager: ImageManager) -> None:
    def queue(operation: str):
        def handler(args: dict[str, Any]) -> str:
            fields={k:v for k,v in args.items() if k in ImageRequest.__dataclass_fields__}
            fields["operation"]=operation
            request=ImageRequest(**fields)
            job=manager.create_job(request, real_person=bool(args.get("real_person",False)))
            return json.dumps({"ok":True,"job":job.as_dict()}, ensure_ascii=False)
        return handler

    specs=[
        ("generate_image","Generate one or more local images from a conversational prompt. Image model is routed automatically.","text_to_image",_schema()),
        ("edit_image","Edit a supplied local image/reference using a conversational instruction while preserving requested identity/details.","edit_image",_schema(required=["prompt","source_image"])),
        ("inpaint_image","Replace or repair a masked/local region of an image.","inpaint",_schema(required=["prompt","source_image","mask_path"])),
        ("outpaint_image","Extend an existing image/canvas beyond its current borders.","outpaint",_schema(extra={"outpaint":{"type":"object"}},required=["prompt","source_image"])),
        ("remove_background","Remove or replace the background of an image.","remove_background",_schema(required=["prompt","source_image"])),
        ("upscale_image","Upscale and optionally refine a local image.","upscale",_schema(required=["prompt","source_image"])),
        ("create_image_variations","Create new variations using a source image or subject profile.","variation",_schema()),
    ]
    for name,desc,op,params in specs:
        registry.register(ToolSpec(name,desc,params,"image.generate",queue(op)))

    registry.register(ToolSpec(
        "list_image_models","List configured and discovered local image models and backend status.",
        {"type":"object","properties":{}},"image.read",
        lambda args: json.dumps(manager.summary(), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "load_subject_profile","Load a reusable image subject/character profile including references and assigned LoRAs.",
        {"type":"object","properties":{"profile_id":{"type":"string"}},"required":["profile_id"]},"image.read",
        lambda args: json.dumps(manager.profiles.get(str(args["profile_id"])), ensure_ascii=False),
    ))
