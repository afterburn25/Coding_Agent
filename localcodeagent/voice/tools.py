"""Voice tool registration — exposes speech + presets through ToolRegistry
with audio.* permissions. Handlers return JSON strings for the model."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..tools.base import ToolSpec
from .engine import VoiceEngineError
from .types import VoicePreset


def register_voice_tools(registry, manager) -> None:
    def ok(payload: dict[str, Any]) -> str:
        return json.dumps({"ok": True, **payload})

    def err(exc: Exception) -> str:
        return json.dumps({"ok": False, "error": str(exc)})

    def _synthesize(args: dict[str, Any]) -> str:
        try:
            out = manager.speak_text(
                str(args.get("text", "")),
                preset_id=args.get("preset_id") or None,
                speed=float(args.get("speed_override") or 1.0),
                auto_filter=bool(args.get("auto_filter", True)),
            )
            dest = args.get("output_path")
            if dest:
                fmt = str(args.get("output_format", "wav"))
                path = manager.export_segment(out["segment_id"], fmt,
                                              Path(dest))
                out["exported_to"] = str(path)
            return ok(out)
        except Exception as exc:
            return err(exc)

    registry.register(ToolSpec(
        name="voice_synthesize",
        description="Synthesize speech locally with the configured voice "
                    "preset. Returns a playable audio segment URL and "
                    "optionally exports WAV/MP3 to output_path.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to speak"},
                "preset_id": {"type": "string"},
                "output_format": {"type": "string", "enum": ["wav", "mp3"]},
                "output_path": {"type": "string"},
                "auto_filter": {"type": "boolean",
                                "description": "Skip code/logs/URLs (default true)"},
                "speed_override": {"type": "number"},
            },
            "required": ["text"],
        },
        permission="audio.generate",
        handler=_synthesize,
        category="voice",
        display_name="Voice Synthesize",
        capabilities=["tts", "speech", "audio.generate"],
    ))

    def _list_voices(args: dict[str, Any]) -> str:
        try:
            eng = manager.engine()
            return ok({"engine": eng.name, "voices": eng.voices()})
        except Exception as exc:
            return err(exc)

    registry.register(ToolSpec(
        name="voice_list",
        description="List installed base voices for the active TTS engine.",
        parameters={"type": "object", "properties": {}},
        permission="audio.read",
        handler=_list_voices,
        category="voice", display_name="List Voices",
        capabilities=["tts", "audio.read"],
    ))

    def _preview(args: dict[str, Any]) -> str:
        try:
            from .types import PREVIEW_PHRASE
            text = str(args.get("text") or PREVIEW_PHRASE)
            out = manager.speak_text(text, preset_id=args.get("preset_id") or None,
                                     auto_filter=False)
            return ok(out)
        except Exception as exc:
            return err(exc)

    registry.register(ToolSpec(
        name="voice_preview",
        description="Preview a voice preset with a short phrase (no filter).",
        parameters={
            "type": "object",
            "properties": {
                "preset_id": {"type": "string"},
                "text": {"type": "string"},
            },
        },
        permission="audio.generate",
        handler=_preview,
        category="voice", display_name="Voice Preview",
        capabilities=["tts", "audio.generate"],
    ))

    registry.register(ToolSpec(
        name="voice_preset_list",
        description="List saved voice presets (official + custom).",
        parameters={"type": "object", "properties": {}},
        permission="audio.read",
        handler=lambda args: ok({"presets": [p.as_dict() for p in manager.presets.list()]}),
        category="voice", display_name="List Voice Presets",
        capabilities=["audio.read"],
    ))

    def _preset_get(args):
        p = manager.presets.get(str(args.get("preset_id", "")))
        return ok({"preset": p.as_dict()}) if p else json.dumps(
            {"ok": False, "error": "preset not found"})

    registry.register(ToolSpec(
        name="voice_preset_get",
        description="Fetch one voice preset by id.",
        parameters={
            "type": "object",
            "properties": {"preset_id": {"type": "string"}},
            "required": ["preset_id"],
        },
        permission="audio.read",
        handler=_preset_get,
        category="voice", display_name="Get Voice Preset",
        capabilities=["audio.read"],
    ))

    def _preset_save(args):
        try:
            raw = args.get("preset") or {}
            if not isinstance(raw, dict):
                raise VoiceEngineError("'preset' must be an object")
            p = VoicePreset.from_dict(raw)
            saved = manager.presets.save(p)
            return ok({"preset": saved.as_dict()})
        except Exception as exc:
            return err(exc)

    registry.register(ToolSpec(
        name="voice_preset_save",
        description="Create or update a custom voice preset (official "
                    "presets are protected and cannot be overwritten).",
        parameters={
            "type": "object",
            "properties": {"preset": {"type": "object"}},
            "required": ["preset"],
        },
        permission="audio.manage",
        handler=_preset_save,
        category="voice", display_name="Save Voice Preset",
        capabilities=["audio.manage"],
    ))

    def _preset_duplicate(args):
        try:
            p = manager.presets.duplicate(str(args.get("preset_id", "")))
            if p is None:
                return json.dumps({"ok": False, "error": "preset not found"})
            return ok({"preset": p.as_dict()})
        except Exception as exc:
            return err(exc)

    registry.register(ToolSpec(
        name="voice_preset_duplicate",
        description="Duplicate a voice preset under a new id.",
        parameters={
            "type": "object",
            "properties": {"preset_id": {"type": "string"},
                           "name": {"type": "string"}},
            "required": ["preset_id"],
        },
        permission="audio.manage",
        handler=_preset_duplicate,
        category="voice", display_name="Duplicate Voice Preset",
        capabilities=["audio.manage"],
    ))

    def _preset_delete(args):
        try:
            removed = manager.presets.delete(str(args.get("preset_id", "")))
            return ok({"deleted": bool(removed)})
        except Exception as exc:
            return err(exc)

    registry.register(ToolSpec(
        name="voice_preset_delete",
        description="Delete a custom voice preset (official presets cannot "
                    "be deleted).",
        parameters={
            "type": "object",
            "properties": {"preset_id": {"type": "string"}},
            "required": ["preset_id"],
        },
        permission="audio.manage",
        handler=_preset_delete,
        category="voice", display_name="Delete Voice Preset",
        capabilities=["audio.manage"],
    ))

    def _preset_import(args):
        try:
            p = manager.presets.import_json(str(args.get("json", "")))
            return ok({"preset": p.as_dict()})
        except Exception as exc:
            return err(exc)

    registry.register(ToolSpec(
        name="voice_preset_import",
        description="Import a voice preset from JSON text.",
        parameters={
            "type": "object",
            "properties": {"json": {"type": "string"}},
            "required": ["json"],
        },
        permission="audio.manage",
        handler=_preset_import,
        category="voice", display_name="Import Voice Preset",
        capabilities=["audio.manage"],
    ))

    def _preset_export(args):
        try:
            text = manager.presets.export_json(str(args.get("preset_id", "")))
            if text is None:
                return json.dumps({"ok": False, "error": "preset not found"})
            return ok({"json": text})
        except Exception as exc:
            return err(exc)

    registry.register(ToolSpec(
        name="voice_preset_export",
        description="Export a voice preset as JSON text.",
        parameters={
            "type": "object",
            "properties": {"preset_id": {"type": "string"}},
            "required": ["preset_id"],
        },
        permission="audio.read",
        handler=_preset_export,
        category="voice", display_name="Export Voice Preset",
        capabilities=["audio.read"],
    ))

    registry.register(ToolSpec(
        name="voice_status",
        description="Voice engine status: preset, queue, cache, performance.",
        parameters={"type": "object", "properties": {}},
        permission="audio.read",
        handler=lambda args: ok(manager.status()),
        category="voice", display_name="Voice Status",
        capabilities=["audio.read"],
    ))
