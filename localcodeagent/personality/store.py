"""PersonalityStore — per-profile personality.json under the profile dir.

Layout::

    <profile_dir>/personality.json
        {"schema_version": 1,
         "active": "preset:nerdy" | "custom:<uuid>",
         "strength": 0-100,
         "mood": "" | one of schema.MOODS,
         "customs": [{"personality_id", "name", "base_preset",
                      "traits", "voice"}]}

The store enforces the slider whitelist and adult gating on every
mutation — the caller passes ``is_adult`` derived from the immutable
birthdate; a crafted payload can never activate adult content for a
minor's profile.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text
from ..profiles.model import ProfileError
from . import schema
from .presets import get_preset, list_presets

SCHEMA_VERSION = 1
DEFAULT_ACTIVE = "preset:default-nexus"
_MAX_NAME = 60
_MAX_CUSTOMS = 200


def _blank() -> dict:
    return {"schema_version": SCHEMA_VERSION, "active": DEFAULT_ACTIVE,
            "strength": schema.DEFAULT_STRENGTH, "mood": "",
            "customs": []}


class PersonalityStore:
    def __init__(self, profile_dir: Path) -> None:
        self.path = Path(profile_dir) / "personality.json"

    # -- IO ---------------------------------------------------------------

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("schema_version", SCHEMA_VERSION)
                raw.setdefault("active", DEFAULT_ACTIVE)
                raw["strength"] = schema.clean_strength(raw.get("strength"))
                raw["mood"] = schema.clean_mood(raw.get("mood"))
                raw.setdefault("customs", [])
                return raw
        except Exception:
            pass
        return _blank()

    def _save(self, st: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(st, indent=2,
                                               ensure_ascii=False))

    def _custom(self, st: dict, cid: str) -> dict | None:
        for c in st.get("customs") or []:
            if c.get("personality_id") == cid:
                return c
        return None

    # -- queries ----------------------------------------------------------

    def available(self, *, is_adult: bool) -> dict:
        """Everything the UI needs: visible presets, customs, sliders,
        voice controls, active record, strength, mood."""
        st = self._load()
        return {
            "active": self.resolve_active(is_adult=is_adult),
            "strength": st["strength"],
            "mood": st["mood"],
            "presets": list_presets(include_adult=is_adult),
            "customs": [self._public_custom(c) for c in st["customs"]],
            "sliders": schema.SLIDERS,
            "voice_controls": schema.VOICE_CONTROLS,
            "moods": schema.MOODS,
            "categories": schema.CATEGORY_LABELS,
            "is_adult": is_adult,
        }

    def _public_custom(self, c: dict) -> dict:
        return {k: c[k] for k in
                ("personality_id", "name", "base_preset", "traits",
                 "voice") if k in c}

    def resolve_active(self, *, is_adult: bool) -> dict:
        """The effective personality — preset or custom — with
        adult content stripped when the profile isn't eligible."""
        st = self._load()
        active = str(st.get("active") or DEFAULT_ACTIVE)
        kind, _, pid = active.partition(":")
        if kind == "custom":
            c = self._custom(st, pid)
            if c is not None:
                base = get_preset(c.get("base_preset") or "") or {}
                adult_blocked = bool(base.get("adult_only")) and not is_adult
                return self._effective(
                    name=c.get("name") or "Custom",
                    base_preset=c.get("base_preset") or "",
                    traits=schema.clean_traits(c.get("traits"),
                                               is_adult=is_adult),
                    voice=schema.clean_voice(c.get("voice")),
                    is_custom=True, personality_id=pid,
                    greeting_style=(
                        "default" if adult_blocked else str(
                            base.get("greeting_style") or "default")),
                    address=(
                        "" if adult_blocked else str(
                            c.get("address") or base.get("address") or "")),
                    pitch_bias=float(
                        base.get("pitch_bias") or 0.0),
                    is_adult=is_adult)
            active = DEFAULT_ACTIVE       # dangling → fall back
        pid = active.split(":", 1)[-1] if ":" in active else active
        p = get_preset(pid) or get_preset("default-nexus")
        traits = schema.clean_traits(p["traits"], is_adult=is_adult)
        adult_blocked = bool(p.get("adult_only")) and not is_adult
        return self._effective(
            name=p["name"], base_preset=p["id"], traits=traits,
            voice=schema.clean_voice(p.get("voice")),
            is_custom=False, personality_id=f"preset:{p['id']}",
            greeting_style=("default" if adult_blocked
                            else p.get("greeting_style") or "default"),
            address=("" if adult_blocked
                     else str(p.get("address") or "")),
            pitch_bias=float(p.get("pitch_bias") or 0.0),
            is_adult=is_adult)

    def _effective(self, *, is_adult: bool, **kw) -> dict:
        kw["strength"] = self._load()["strength"]
        kw["mood"] = self._load()["mood"]
        kw["adult_gated"] = not is_adult
        return kw

    # -- mutations ----------------------------------------------------------

    def set_active(self, target: str, *, is_adult: bool) -> dict:
        """Activate 'preset:<id>' or 'custom:<uuid>'."""
        st = self._load()
        kind, _, pid = str(target).partition(":")
        if kind == "preset":
            p = get_preset(pid)
            if p is None:
                raise ProfileError("no such preset")
            if p.get("adult_only") and not is_adult:
                raise ProfileError("adult personality requires 18+")
        elif kind == "custom":
            if self._custom(st, pid) is None:
                raise ProfileError("no such custom personality")
        else:
            raise ProfileError("target must be preset:<id> or custom:<id>")
        st["active"] = f"{kind}:{pid}"
        self._save(st)
        return self.resolve_active(is_adult=is_adult)

    def set_strength(self, raw: Any) -> int:
        st = self._load()
        st["strength"] = schema.clean_strength(raw)
        self._save(st)
        return st["strength"]

    def set_mood(self, raw: Any) -> str:
        st = self._load()
        st["mood"] = schema.clean_mood(raw)
        self._save(st)
        return st["mood"]

    # -- custom personalities -------------------------------------------------

    def _clean_name(self, raw: Any) -> str:
        name = " ".join(str(raw or "").split())[:_MAX_NAME]
        if not name:
            raise ProfileError("name required")
        return name

    def create_custom(self, *, is_adult: bool, name: str | None = None,
                      base_preset: str | None = None,
                      traits: dict | None = None,
                      voice: dict | None = None) -> dict:
        """Save a custom personality — optionally duplicated from a
        preset ('Custom based on <preset>')."""
        st = self._load()
        if len(st["customs"]) >= _MAX_CUSTOMS:
            raise ProfileError("custom personality limit reached")
        base = get_preset(str(base_preset or ""))
        if base and base.get("adult_only") and not is_adult:
            raise ProfileError("adult personality requires 18+")
        if name is None:
            name = (f"Custom based on {base['name']}" if base
                    else "Custom")
        merged_traits = dict(base["traits"]) if base else {}
        merged_traits.update(traits or {})
        merged_voice = dict(base.get("voice") or {}) if base else {}
        merged_voice.update(voice or {})
        custom = {
            "personality_id": uuid.uuid4().hex,
            "name": self._clean_name(name),
            "base_preset": base["id"] if base else "",
            "created_at": time.time(),
            "traits": schema.clean_traits(merged_traits,
                                          is_adult=is_adult),
            "voice": schema.clean_voice(merged_voice),
        }
        st["customs"].append(custom)
        self._save(st)
        return self._public_custom(custom)

    def patch_custom(self, cid: str, *, is_adult: bool,
                     name: Any = None, traits: dict | None = None,
                     voice: dict | None = None) -> dict:
        st = self._load()
        c = self._custom(st, cid)
        if c is None:
            raise ProfileError("no such custom personality")
        if name is not None:
            c["name"] = self._clean_name(name)
        if traits is not None:
            merged = dict(c.get("traits") or {})
            merged.update(traits)
            c["traits"] = schema.clean_traits(merged, is_adult=is_adult)
        if voice is not None:
            merged = dict(c.get("voice") or {})
            merged.update(voice)
            c["voice"] = schema.clean_voice(merged)
        self._save(st)
        return self._public_custom(c)

    def delete_custom(self, cid: str) -> bool:
        st = self._load()
        if self._custom(st, cid) is None:
            return False
        st["customs"] = [c for c in st["customs"]
                         if c.get("personality_id") != cid]
        if st.get("active") == f"custom:{cid}":
            st["active"] = DEFAULT_ACTIVE
        self._save(st)
        return True
