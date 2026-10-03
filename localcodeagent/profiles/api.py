"""Profile/personality/onboarding API dispatcher.

Owns every ``/api/profiles*``, ``/api/onboarding/*``,
``/api/creator/*``, ``/api/personalities`` and ``/api/postal/*`` route
so server.py stays a thin delegate. All enforcement (immutability,
creator auth, adult gating, ZIP validation) lives in the backing
services — this layer only parses and serializes.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..fsutil import atomic_write_text
from ..personality import (
    GreetingService, PersonalityStore, list_presets,
)
from ..personality import SLIDERS, VOICE_CONTROLS, MOODS, CATEGORY_LABELS
from . import avatar as avatar_svc
from .migration import migrate_legacy_state
from .model import (
    ProfileError, compute_age, public_profile,
)
from .personal import PersonalMemory
from .postal import provider as _postal_provider

_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_MAX_DATA_URL = 17_000_000


def _decode_data_url(raw: str) -> bytes:
    if not isinstance(raw, str) or not raw.startswith("data:"):
        raise ProfileError("avatar must be a data URL")
    if len(raw) > _MAX_DATA_URL:
        raise ProfileError("avatar image too large")
    try:
        return base64.b64decode(raw.split(",", 1)[1], validate=True)
    except Exception as exc:
        raise ProfileError("invalid avatar data URL") from exc


def _is_adult(profile: dict | None) -> bool:
    """Adult eligibility derives only from the immutable birthdate."""
    if not profile:
        return False
    try:
        from datetime import date as d
        return compute_age(d.fromisoformat(
            str(profile.get("birth_date") or ""))) >= 18
    except (TypeError, ValueError):
        return False


class ProfileAPI:
    """Stateless dispatcher — AppState owns the ProfileManager."""

    def __init__(self, state: Any) -> None:
        self.state = state

    # -- helpers ---------------------------------------------------------

    @property
    def mgr(self):
        return self.state.profiles

    def _pid(self, path: str, prefix: str, suffix: str = "") -> str:
        """Extract the profile id from /api/profiles/<id>[/<suffix>]."""
        rest = path[len(prefix):]
        if suffix:
            rest = rest[:-len(suffix)]
        pid = rest.strip("/").split("/", 1)[0]
        if not _ID_RE.match(pid):
            raise ProfileError("invalid profile id")
        return pid

    def _personality(self, profile_id: str) -> PersonalityStore:
        return PersonalityStore(self.mgr.profile_dir(profile_id))

    def _memory(self, profile_id: str) -> PersonalMemory:
        return PersonalMemory(self.mgr.profile_dir(profile_id))

    def _greetings(self, profile_id: str) -> GreetingService:
        return GreetingService(self.mgr.profile_dir(profile_id))

    def _err(self, h, exc: Exception) -> bool:
        if isinstance(exc, ProfileError):
            h._json({"ok": False, "error": str(exc)}, 400)
        else:
            h._json({"ok": False,
                     "error": f"{type(exc).__name__}: {exc}"}, 500)
        return True

    # -- routes that must pass the onboarding gate -------------------------

    def onboarding_status(self) -> dict:
        profiles = self.mgr.list_profiles()
        return {
            "required": self.mgr.onboarding_required,
            "profiles": profiles,
            "active": self.mgr.active_id(),
            "welcome_played": self.mgr.onboarding_welcome_played(),
        }

    # -- GET ------------------------------------------------------------

    def handle_get(self, h, path: str, query: dict) -> bool:
        if path == "/api/onboarding/status":
            h._json(self.onboarding_status())
            return True
        if path == "/api/profiles":
            h._json({"profiles": self.mgr.list_profiles()})
            return True
        if path == "/api/profiles/active":
            p = self.mgr.active()
            h._json({"profile": public_profile(p) if p else None,
                     "onboarding_required": self.mgr.onboarding_required})
            return True
        if path == "/api/personalities":
            active = self.mgr.active()
            h._json({
                "presets": list_presets(include_adult=_is_adult(active)),
                "sliders": SLIDERS, "voice_controls": VOICE_CONTROLS,
                "moods": MOODS, "categories": CATEGORY_LABELS,
                "is_adult": _is_adult(active),
            })
            return True
        if path == "/api/postal/states":
            h._json({"states": _postal_provider().states()})
            return True
        if path == "/api/postal/cities":
            st = str((query.get("state") or [""])[0]).upper()
            h._json({"state": st, "cities": _postal_provider().cities(st)})
            return True
        if path == "/api/creator/status":
            h._json({"enrolled": self.mgr.creator_auth.path.is_file(),
                     "locked_until": self.mgr.creator_auth.locked_until()})
            return True
        if path.startswith("/api/profiles/"):
            return self._profile_get(h, path)
        return False

    def _profile_get(self, h, path: str) -> bool:
        try:
            if path.endswith("/avatar"):
                pid = self._pid(path, "/api/profiles/", "/avatar")
                return self._serve_avatar(h, pid)
            if path.endswith("/personality"):
                pid = self._pid(path, "/api/profiles/", "/personality")
                p = self.mgr.get(pid)
                if p is None:
                    h._json({"error": "no such profile"}, 404)
                    return True
                h._json(self._personality(pid).available(
                    is_adult=_is_adult(p)))
                return True
            if path.endswith("/greeting"):
                pid = self._pid(path, "/api/profiles/", "/greeting")
                return self._greeting(h, pid)
            if path.endswith("/memory"):
                pid = self._pid(path, "/api/profiles/", "/memory")
                if self.mgr.get(pid) is None:
                    h._json({"error": "no such profile"}, 404)
                    return True
                h._json({"memories": self._memory(pid).list()})
                return True
            if path.endswith("/settings"):
                pid = self._pid(path, "/api/profiles/", "/settings")
                p = self.mgr.get(pid)
                if p is None:
                    h._json({"error": "no such profile"}, 404)
                    return True
                h._json({"settings": dict(p.get("settings") or {})})
                return True
            # /api/profiles/<id>
            pid = self._pid(path, "/api/profiles/")
            p = self.mgr.get(pid)
            if p is None:
                h._json({"error": "no such profile"}, 404)
            else:
                h._json({"profile": public_profile(p)})
            return True
        except ProfileError as exc:
            h._json({"error": str(exc)}, 400)
            return True

    def _serve_avatar(self, h, pid: str) -> bool:
        p = self.mgr.get(pid)
        f = self.mgr.profile_dir(pid) / "avatar.webp"
        if p is None or not f.is_file():
            h.send_error(404)
            return True
        data = f.read_bytes()
        h.send_response(200)
        h.send_header("Content-Type", "image/webp")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(data)
        return True

    def _greeting(self, h, pid: str) -> bool:
        p = self.mgr.get(pid)
        if p is None:
            h._json({"error": "no such profile"}, 404)
            return True
        personality = self._personality(pid).resolve_active(
            is_adult=_is_adult(p))
        health = "ok"
        try:
            health = "ok" if self.state.health.overall() == "healthy" \
                else "degraded"
        except Exception:
            pass
        g = self._greetings(pid).greeting(
            p, personality, is_adult=_is_adult(p), health=health)
        if g["kind"] == "intro":
            self.mgr.mark_intro_completed(pid)
        h._json(g)
        return True

    # -- POST / PATCH -----------------------------------------------------

    def handle_post(self, h, path: str, body: dict) -> bool:
        try:
            if path == "/api/profiles":
                return self._create(h, body)
            if path == "/api/profiles/switch":
                return self._switch(h, body)
            if path == "/api/creator/verify":
                return self._verify(h, body)
            if path.startswith("/api/profiles/"):
                return self._profile_post(h, path, body)
        except ProfileError as exc:
            return self._err(h, exc)
        except Exception as exc:
            return self._err(h, exc)
        return False

    def _create(self, h, body: dict) -> bool:
        fields = {k: body.get(k) for k in (
            "first_name", "last_name", "sex", "birth_date", "email",
            "phone", "street_address", "city", "state", "zip_code",
            "greeting_preference")}
        first = self.mgr.onboarding_required
        profile = self.mgr.create(
            fields, creator_passcode=body.get("creator_passcode"))
        # Avatar is required by the spec but accepted as a data URL with
        # optional crop — validated/cropped server-side.
        raw_avatar = body.get("avatar")
        if isinstance(raw_avatar, dict) and raw_avatar.get("data_url"):
            rel = avatar_svc.save_avatar(
                self.mgr.profile_dir(profile["profile_id"], create=True),
                _decode_data_url(str(raw_avatar["data_url"])),
                raw_avatar.get("crop"))
            profile = self.mgr.set_avatar(profile["profile_id"], rel)
        if first:
            migrate_legacy_state(
                self.mgr.root, profile["profile_id"],
                runtime_root=self.state.runtime.base_dir)
        g = self._greetings(profile["profile_id"]).greeting(
            profile,
            self._personality(profile["profile_id"]).resolve_active(
                is_adult=_is_adult(profile)),
            is_adult=_is_adult(profile))
        self.mgr.mark_intro_completed(profile["profile_id"])
        h._json({"ok": True, "profile": public_profile(profile),
                 "greeting": g,
                 "unlocked": not self.mgr.onboarding_required})
        return True

    def _switch(self, h, body: dict) -> bool:
        p = self.mgr.switch(str(body.get("profile_id") or ""))
        g = self._greetings(p["profile_id"]).greeting(
            p,
            self._personality(p["profile_id"]).resolve_active(
                is_adult=_is_adult(p)),
            is_adult=_is_adult(p))
        if g["kind"] == "intro":
            self.mgr.mark_intro_completed(p["profile_id"])
        h._json({"ok": True, "profile": public_profile(p),
                 "greeting": g})
        return True

    def _verify(self, h, body: dict) -> bool:
        result = self.mgr.creator_auth.verify(str(body.get("passcode") or ""))
        # Neutral response — never echo credential details.
        h._json(result)
        return True

    def _profile_post(self, h, path: str, body: dict) -> bool:
        if path.endswith("/patch"):
            pid = self._pid(path, "/api/profiles/", "/patch")
            p = self.mgr.patch(pid, dict(body.get("fields") or body))
            h._json({"ok": True, "profile": public_profile(p)})
            return True
        if path.endswith("/avatar"):
            pid = self._pid(path, "/api/profiles/", "/avatar")
            raw = body.get("data_url") or body.get("avatar")
            rel = avatar_svc.save_avatar(
                self.mgr.profile_dir(pid, create=True),
                _decode_data_url(str(raw or "")),
                body.get("crop"))
            p = self.mgr.set_avatar(pid, rel)
            h._json({"ok": True, "profile": public_profile(p)})
            return True
        if path.endswith("/personality"):
            pid = self._pid(path, "/api/profiles/", "/personality")
            return self._personality_post(h, pid, body)
        if path.endswith("/creator"):
            pid = self._pid(path, "/api/profiles/", "/creator")
            p = self.mgr.update_creator_settings(
                pid, dict(body.get("fields") or {}),
                passcode=str(body.get("passcode") or ""))
            h._json({"ok": True, "profile": public_profile(p)})
            return True
        if path.endswith("/memory/forget"):
            pid = self._pid(path, "/api/profiles/", "/memory/forget")
            ok = self._memory(pid).forget(str(body.get("id") or ""))
            h._json({"ok": ok}, 404 if not ok else 200)
            return True
        if path.endswith("/memory"):
            pid = self._pid(path, "/api/profiles/", "/memory")
            if self.mgr.get(pid) is None:
                h._json({"error": "no such profile"}, 404)
                return True
            try:
                entry = self._memory(pid).remember(
                    body.get("text"), kind=str(body.get("kind") or "note"))
            except ValueError as exc:
                h._json({"error": str(exc)}, 400)
                return True
            h._json({"ok": True, "memory": entry})
            return True
        if path.endswith("/settings"):
            pid = self._pid(path, "/api/profiles/", "/settings")
            p = self.mgr.get(pid)
            if p is None:
                h._json({"error": "no such profile"}, 404)
                return True
            incoming = body.get("settings")
            if not isinstance(incoming, dict):
                raise ProfileError("settings must be an object")
            merged = dict(p.get("settings") or {})
            merged.update(incoming)
            p = self.mgr.patch(pid, {"settings": merged})
            h._json({"ok": True, "profile": public_profile(p)})
            return True
        if path.endswith("/voice"):
            pid = self._pid(path, "/api/profiles/", "/voice")
            return self._voice_post(h, pid, body)
        return False

    def _personality_post(self, h, pid: str, body: dict) -> bool:
        p = self.mgr.get(pid)
        if p is None:
            h._json({"error": "no such profile"}, 404)
            return True
        adult = _is_adult(p)
        store = self._personality(pid)
        action = str(body.get("action") or "")
        if action == "set_active":
            h._json({"ok": True, "active": store.set_active(
                str(body.get("target") or ""), is_adult=adult)})
        elif action == "set_strength":
            h._json({"ok": True,
                     "strength": store.set_strength(body.get("strength"))})
        elif action == "set_mood":
            h._json({"ok": True, "mood": store.set_mood(body.get("mood"))})
        elif action == "create_custom":
            h._json({"ok": True, "custom": store.create_custom(
                is_adult=adult, name=body.get("name"),
                base_preset=body.get("base_preset"),
                traits=body.get("traits"), voice=body.get("voice"))})
        elif action == "patch_custom":
            h._json({"ok": True, "custom": store.patch_custom(
                str(body.get("personality_id") or ""), is_adult=adult,
                name=body.get("name"), traits=body.get("traits"),
                voice=body.get("voice"))})
        elif action == "delete_custom":
            ok = store.delete_custom(str(body.get("personality_id") or ""))
            h._json({"ok": ok}, 404 if not ok else 200)
        elif action == "reset":
            store.set_active("preset:default-nexus", is_adult=adult)
            store.set_strength(70)
            h._json({"ok": True,
                     "active": store.resolve_active(is_adult=adult)})
        elif action == "preview":
            # Personality-aware sample text + voice params — no TTS call.
            active = store.resolve_active(is_adult=adult)
            g = GreetingService(
                self.mgr.profile_dir(pid)).greeting(
                p, active, is_adult=adult)
            h._json({"ok": True, "preview": g["text"],
                     "voice": g["voice"], "personality": active})
        else:
            h._json({"error": "unknown personality action"}, 400)
        return True

    def _voice_post(self, h, pid: str, body: dict) -> bool:
        """Profile-scoped voice selection — stored in voice.json inside
        the profile dir (not the global voice preset store)."""
        p = self.mgr.get(pid)
        if p is None:
            h._json({"error": "no such profile"}, 404)
            return True
        vpath = self.mgr.profile_dir(pid, create=True) / "voice.json"
        try:
            cur = json.loads(vpath.read_text(encoding="utf-8"))
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}
        for key in ("preset_id", "speed", "prosody"):
            if key in body:
                cur[key] = body[key]
        atomic_write_text(vpath, json.dumps(cur, indent=2))
        h._json({"ok": True, "voice": cur})
        return True

    # -- onboarding gate ----------------------------------------------------

    #: API prefixes that must stay reachable while onboarding_required.
    SAFE_PREFIXES = (
        "/api/onboarding", "/api/profiles", "/api/creator",
        "/api/postal", "/api/personalities", "/api/health",
        "/api/status", "/api/time",
    )

    @classmethod
    def allowed_while_locked(cls, path: str) -> bool:
        """While onboarding_required, only onboarding APIs + static files
        pass; every other API returns 403."""
        if not path.startswith("/api/"):
            return True                     # static assets/pages
        return any(path.startswith(p) for p in cls.SAFE_PREFIXES)
