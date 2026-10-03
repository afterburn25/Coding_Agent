"""ProfileManager — durable per-UUID profile directories + onboarding gate.

Layout::

    data/profiles/
        index.json            {"active": "<uuid>"}
        state.json            {"onboarding_welcome_played": bool}
        <uuid>/
            profile.json      identity record (see model.py)
            avatar.webp       normalized avatar (original untouched)
            settings.json     profile-scoped preferences
            personality.json  active personality + customs
            voice.json        voice preset + prosody
            memory/           profile-scoped personal memory

``onboarding_required`` is a real state: while no profile exists the app
is locked to Start Here — enforced server-side by route gating and
client-side by the shared navigation guard, not CSS.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..fsutil import atomic_write_text
from .creator import CreatorAuth, creator_fields, is_reserved_name
from .model import (
    EDITABLE_FIELDS, PROTECTED_FIELDS, ProfileError, new_profile,
    parse_birth_date, public_profile, validate_profile,
)

# Profile dirs are UUID-named — anything else is a bogus id (or worse,
# an attempted traversal like "../../etc"); never touch the filesystem
# for it.
_SAFE_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class ProfileManager:
    def __init__(self, root: Path,
                 on_switch: Callable[[dict], None] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.creator_auth = CreatorAuth(self.root)
        self._on_switch = on_switch
        self._index_path = self.root / "index.json"
        self._state_path = self.root / "state.json"

    # -- directories --------------------------------------------------------

    def profile_dir(self, profile_id: str, *, create: bool = False) -> Path:
        pid = str(profile_id)
        if not _SAFE_ID.match(pid):
            raise ProfileError("invalid profile id")
        d = self.root / pid
        if create:
            (d / "memory").mkdir(parents=True, exist_ok=True)
        return d

    # -- state --------------------------------------------------------------

    def _index(self) -> dict:
        return _load_json(self._index_path, {"active": ""}) or {}

    def _save_index(self, idx: dict) -> None:
        atomic_write_text(self._index_path, json.dumps(idx, indent=2))

    def _state(self) -> dict:
        return _load_json(self._state_path, {}) or {}

    def _save_state(self, st: dict) -> None:
        atomic_write_text(self._state_path, json.dumps(st, indent=2))

    @property
    def onboarding_required(self) -> bool:
        return not self.list_ids()

    def onboarding_welcome_played(self) -> bool:
        return bool(self._state().get("onboarding_welcome_played"))

    def mark_onboarding_welcome_played(self) -> None:
        st = self._state()
        st["onboarding_welcome_played"] = True
        self._save_state(st)

    # -- CRUD -----------------------------------------------------------------

    def list_ids(self) -> list[str]:
        return [d.name for d in self.root.iterdir()
                if d.is_dir() and _SAFE_ID.match(d.name)
                and (d / "profile.json").is_file()]

    def list_profiles(self) -> list[dict]:
        out = []
        for pid in self.list_ids():
            p = self.get(pid)
            if p is not None:
                out.append(public_profile(p))
        out.sort(key=lambda r: r.get("created_at") or 0)
        return out

    def get(self, profile_id: str) -> dict | None:
        try:
            p = _load_json(
                self.profile_dir(profile_id) / "profile.json", None)
        except ProfileError:
            return None
        return p if isinstance(p, dict) else None

    def active_id(self) -> str:
        aid = str(self._index().get("active") or "")
        if aid and self.get(aid) is not None:
            return aid
        ids = self.list_ids()
        return ids[0] if ids else ""

    def active(self) -> dict | None:
        aid = self.active_id()
        return self.get(aid) if aid else None

    def create(self, fields: dict[str, Any], *,
               creator_passcode: str | None = None) -> dict:
        """Create a profile. The reserved Creator name cannot become an
        ordinary profile — it requires passcode verification; any other
        name with a passcode is rejected (no orphan creator creds)."""
        first = str(fields.get("first_name") or "")
        last = str(fields.get("last_name") or "")
        reserved = is_reserved_name(first, last)
        # Crafted payloads can't self-elect protected fields.
        smuggled = [f for f in fields if f in PROTECTED_FIELDS]
        if smuggled:
            raise ProfileError(
                "protected fields are not client-settable")
        if reserved:
            if creator_passcode is None:
                raise ProfileError("creator_verification_required")
            result = self.creator_auth.verify(creator_passcode)
            if not result.get("ok"):
                # Neutral error — never reveal credential details.
                raise ProfileError(
                    "creator authentication failed"
                    + (f" — retry in {result['retry_after_s']:.0f}s"
                       if result.get("retry_after_s") else ""))
        elif creator_passcode is not None:
            raise ProfileError(
                "passcode is only valid for the reserved profile")
        first = not self.list_ids()
        profile = new_profile(fields)
        if reserved:
            profile.update(creator_fields(True))
        self._write(profile)
        if first:
            # The first-created profile becomes active — recorded
            # explicitly in the index, never inferred from dir order.
            idx = self._index()
            idx["active"] = profile["profile_id"]
            self._save_index(idx)
        return profile

    def _write(self, profile: dict) -> None:
        profile["updated_at"] = time.time()
        atomic_write_text(
            self.profile_dir(profile["profile_id"], create=True)
            / "profile.json",
            json.dumps(profile, indent=2, ensure_ascii=False))

    def patch(self, profile_id: str, fields: dict[str, Any]) -> dict:
        """Editable fields only — identity and protected fields are
        rejected here regardless of what the UI shows."""
        p = self.get(profile_id)
        if p is None:
            raise ProfileError("no such profile")
        bad = [f for f in fields
               if f in PROTECTED_FIELDS or f not in EDITABLE_FIELDS]
        immutable_hit = [f for f in fields
                         if f in {"first_name", "last_name", "sex",
                                  "birth_date", "profile_id",
                                  "created_at"}]
        if immutable_hit:
            raise ProfileError(
                "immutable fields cannot be changed: "
                + ", ".join(sorted(immutable_hit)))
        if bad:
            raise ProfileError(
                "fields not editable: " + ", ".join(sorted(bad)))
        errors = validate_profile(fields, require_all=False)
        if errors:
            raise ProfileError("; ".join(
                f"{k}: {v}" for k, v in errors.items()))
        for f in EDITABLE_FIELDS & fields.keys():
            p[f] = fields[f]
        self._write(p)
        return p

    def set_avatar(self, profile_id: str, rel_path: str) -> dict:
        p = self.get(profile_id)
        if p is None:
            raise ProfileError("no such profile")
        p["avatar_path"] = rel_path
        self._write(p)
        return p

    # -- switching ------------------------------------------------------------

    def switch(self, profile_id: str) -> dict:
        p = self.get(profile_id)
        if p is None:
            raise ProfileError("no such profile")
        idx = self._index()
        idx["active"] = profile_id
        self._save_index(idx)
        if self._on_switch:
            self._on_switch(p)
        return p

    def mark_intro_completed(self, profile_id: str) -> None:
        p = self.get(profile_id)
        if p is not None and not p.get("has_completed_intro"):
            p["has_completed_intro"] = True
            self._write(p)

    # -- creator settings -----------------------------------------------------

    def update_creator_settings(
            self, profile_id: str, fields: dict[str, Any],
            *, passcode: str) -> dict:
        """Creator-only settings require re-authentication — the passcode
        is verified fresh; it is never read back from stored state."""
        p = self.get(profile_id)
        if p is None:
            raise ProfileError("no such profile")
        if not p.get("is_creator"):
            raise ProfileError("creator-only settings")
        result = self.creator_auth.verify(passcode)
        if not result.get("ok"):
            raise ProfileError("creator authentication failed")
        allowed = {"creator_address", "creator_title_greetings",
                   "creator_title_conversation",
                   "creator_title_notifications"}
        bad = [f for f in fields if f not in allowed]
        if bad:
            raise ProfileError("fields not editable: "
                               + ", ".join(sorted(bad)))
        addr = str(fields.get("creator_address",
                              p.get("creator_address") or ""))
        if addr and len(addr) > 40:
            raise ProfileError("creator_address too long")
        for f in allowed & fields.keys():
            p[f] = fields[f] if f != "creator_address" else addr.strip()
        self._write(p)
        return p

    def preferred_address(self, profile: dict | None) -> str:
        """What Nexus calls this user — creator_address for Creators who
        chose one, otherwise first name."""
        if not profile:
            return ""
        if profile.get("is_creator"):
            if profile.get("creator_title_greetings", True):
                return str(profile.get("creator_address") or "Father")
        return str(profile.get("first_name") or "")
