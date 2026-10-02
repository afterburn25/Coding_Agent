"""Voice preset store — official built-ins + user presets on disk.

Official presets ship in the package (data/voice_presets/_official/) and are
copied — never overwritten — into the user store. User presets are versioned
JSON with a schema_version; unknown fields are ignored so newer presets don't
break older builds. Saves are atomic (tmp+replace), corrupt files quarantined.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from ..fsutil import replace_with_retry

from .types import VoicePreset

OFFICIAL_PRESET_ID = "nexus-synthetic-isabella"
_OFFICIAL_DIR = Path(__file__).resolve().parent / "official"


class PresetError(RuntimeError):
    pass


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or f"voice-{int(time.time())}"


class VoicePresetStore:
    def __init__(self, preset_dir: Path) -> None:
        self.dir = Path(preset_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._install_official()

    # -- official seeding -------------------------------------------------
    def _install_official(self) -> None:
        for src in _OFFICIAL_DIR.glob("*.json"):
            dest = self.dir / f"{src.stem}.official.json"
            if not dest.exists():
                try:
                    shutil.copyfile(src, dest)
                except OSError:
                    pass

    # -- IO -----------------------------------------------------------------
    def _path(self, preset_id: str) -> Path:
        return self.dir / f"{preset_id}.json"

    def _official_path(self, preset_id: str) -> Path:
        return self.dir / f"{preset_id}.official.json"

    def _load_file(self, path: Path) -> VoicePreset | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            try:
                path.rename(path.with_name(
                    f"{path.name}.corrupt-{int(time.time())}"))
            except OSError:
                pass
            return None
        except OSError:
            return None
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        try:
            p = VoicePreset.from_dict(raw)
        except (TypeError, ValueError):
            return None
        p.official = path.name.endswith(".official.json")
        return p

    def list(self) -> list[VoicePreset]:
        seen: dict[str, VoicePreset] = {}
        # Official first so a same-id user copy never overrides it.
        for path in sorted(self.dir.glob("*.official.json")):
            p = self._load_file(path)
            if p:
                seen[p.id] = p
        for path in sorted(self.dir.glob("*.json")):
            if path.name.endswith(".official.json") or ".corrupt-" in path.name:
                continue
            p = self._load_file(path)
            if p and p.id not in seen:
                seen[p.id] = p
        return list(seen.values())

    def get(self, preset_id: str) -> VoicePreset | None:
        for p in self.list():
            if p.id == preset_id:
                return p
        return None

    def save(self, preset: VoicePreset, *, allow_official: bool = False) -> VoicePreset:
        if not preset.id:
            preset.id = _slug(preset.name)
        existing = self.get(preset.id)
        if existing and existing.official and not allow_official:
            raise PresetError(
                f"{preset.id} is an official preset — save under a new name")
        preset.official = False
        path = self._path(preset.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(preset.to_json(), encoding="utf-8")
        replace_with_retry(tmp, path)
        return preset

    def save_as(self, preset: VoicePreset, new_id: str | None = None,
                new_name: str | None = None) -> VoicePreset:
        p = VoicePreset.from_dict(preset.as_dict())
        p.id = _slug(new_id or new_name or (p.name + " copy"))
        p.name = new_name or (p.name + " copy")
        p.official = False
        while self.get(p.id):
            p.id = f"{p.id}-{int(time.time()) % 10000}"
        return self.save(p)

    def rename(self, preset_id: str, new_name: str) -> VoicePreset | None:
        p = self.get(preset_id)
        if p is None:
            return None
        if p.official:
            raise PresetError("official presets cannot be renamed — duplicate instead")
        p.name = new_name.strip() or p.name
        return self.save(p)

    def duplicate(self, preset_id: str) -> VoicePreset | None:
        p = self.get(preset_id)
        if p is None:
            return None
        return self.save_as(p)

    def delete(self, preset_id: str) -> bool:
        p = self.get(preset_id)
        if p is None:
            return False
        if p.official:
            raise PresetError("official presets cannot be deleted")
        self._path(preset_id).unlink(missing_ok=True)
        return True

    def export_json(self, preset_id: str) -> str | None:
        p = self.get(preset_id)
        if p is None:
            return None
        return p.to_json()

    def import_json(self, text: str, *, overwrite: bool = False) -> VoicePreset:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PresetError(f"invalid preset JSON: {exc}") from exc
        if not isinstance(raw, dict) or not raw.get("engine"):
            raise PresetError("preset must be a JSON object with an 'engine' field")
        raw.pop("official", None)
        p = VoicePreset.from_dict(raw)
        if not p.id:
            p.id = _slug(p.name or "imported-voice")
        if self.get(p.id) and not overwrite:
            p.id = f"{p.id}-imported"
            while self.get(p.id):
                p.id = f"{p.id}-{int(time.time()) % 10000}"
        return self.save(p, allow_official=False)
