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
    GreetingService, PersonalityStore, clean_traits, clean_voice,
    list_presets,
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

    def _dynamics(self, profile_id: str) -> "PersonaDynamics":
        from ..personality.dynamics import PersonaDynamics
        return PersonaDynamics(self.mgr.profile_dir(profile_id))

    def _persona_effective(self, pid: str, p: dict,
                           user_text: str = "") -> dict:
        """Advanced/debug view of the compiled effective persona —
        presentation metadata only, never private memory."""
        from ..personality.dynamics import PersonaDynamics
        from ..personality.effective import (
            compile_effective, debug_view)
        adult = _is_adult(p)
        store = self._personality(pid)
        active = store.resolve_active(is_adult=adult)
        dyn = PersonaDynamics(self.mgr.profile_dir(pid))
        card = compile_effective(
            active, user_text=user_text,
            relationship=dyn.relationship(),
            mood=dyn.effective_mood(
                manual_mood=str(active.get("mood") or "")),
            overlay=dyn.overlay(), modifiers=dyn.modifiers(),
            mode=dyn.mode(), is_adult=adult)
        view = debug_view(card)
        view["recent_openers"] = dyn.recent_phrases()["openers"][-4:]
        view["recent_closers"] = dyn.recent_phrases()["closers"][-4:]
        return {"ok": True, "effective": view}

    # -- speech genome preview (Preview Lab backend) ----------------------

    def _speech_preview(self, pid: str, p: dict, query: dict) -> dict:
        """Render a battery of speech acts through a persona's genome —
        the Preview Lab surface (§ UI). Query params: ``target``
        ('preset:<id>' / 'custom:<id>' / 'active'), ``register``,
        ``seriousness`` (0-3), ``turns`` (renders per act, 1-4)."""
        from ..context.realize import (
            PersonaRenderer, RenderContext, SemanticResponse)
        from ..personality.dynamics import PersonaDynamics
        from ..personality.genome import derive_genome, genome_summary
        adult = _is_adult(p)
        store = self._personality(pid)
        target = str((query.get("target") or ["active"])[0])
        personality = store.resolve(target, is_adult=adult)
        if personality is None:
            return {"ok": False, "error": "no such persona"}
        genome = derive_genome(personality)
        dyn = PersonaDynamics(self.mgr.profile_dir(pid))
        rel = dyn.relationship()
        register = str((query.get("register") or ["casual"])[0])
        try:
            seriousness = max(0, min(3, int(
                (query.get("seriousness") or ["0"])[0])))
        except (TypeError, ValueError):
            seriousness = 0
        try:
            turns = max(1, min(4, int((query.get("turns") or ["2"])[0])))
        except (TypeError, ValueError):
            turns = 2

        ctx = RenderContext(
            mood=str(dyn.effective_mood(
                manual_mood=str(personality.get("mood") or ""))
                or "relaxed"),
            seriousness=seriousness, register=register,
            relationship_stage=str(rel.get("stage") or "new"),
            familiarity=float(rel.get("familiarity") or 0.0),
            address=str(dyn.state().get("address") or ""))

        # One semantic per act — the same WHAT across personas is what
        # makes the HOW differences legible.
        samples = {
            "greet": SemanticResponse(
                semantic_id="pv_greet", speech_act="greet"),
            "answer": SemanticResponse(
                semantic_id="pv_answer", speech_act="answer",
                facts=["The answer is 42.",
                       "Douglas Adams documented it in 1979."],
                exact_spans=["42", "1979"]),
            "report_success": SemanticResponse(
                semantic_id="pv_success", speech_act="report_success",
                facts=["The build finished cleanly.",
                       "All 12 tests passed in 0.8s."],
                actions_completed=["ran the build", "ran the test suite"],
                exact_spans=["12 tests", "0.8s"]),
            "report_failure": SemanticResponse(
                semantic_id="pv_failure", speech_act="report_failure",
                facts=["The migration failed at step 3.",
                       "The error was a duplicate key on users.id."],
                exact_spans=["step 3", "users.id"],
                next_steps=["inspect the migration log"]),
            "disagree": SemanticResponse(
                semantic_id="pv_disagree", speech_act="disagree",
                facts=["Dropping the index would slow every lookup.",
                       "A partial index covers the hot path instead."],
                confidence="likely",
                exact_spans=["users.id"]),
            "warn": SemanticResponse(
                semantic_id="pv_warn", speech_act="warn",
                facts=["This deletes the local history permanently."],
                warnings=["This cannot be undone."],
                exact_spans=["permanently"]),
            "admit_uncertainty": SemanticResponse(
                semantic_id="pv_uncertain",
                speech_act="admit_uncertainty",
                confidence="uncertain",
                facts=["The remote may still be syncing."],
                uncertainty=["The last fetch timestamp is stale."]),
            "farewell": SemanticResponse(
                semantic_id="pv_farewell", speech_act="farewell"),
        }

        import random
        renderer = PersonaRenderer(rng=random.Random())
        renders: dict[str, list] = {}
        for act, sem in samples.items():
            rows = []
            for _ in range(turns):
                out = renderer.render_semantic(sem, genome, ctx)
                rows.append(out.as_dict())
            renders[act] = rows

        return {
            "ok": True,
            "persona": {"name": personality.get("name"),
                        "id": personality.get("personality_id"),
                        "base_preset": personality.get("base_preset")},
            "register": register, "seriousness": seriousness,
            "relationship": rel,
            "genome_summary": genome_summary(genome),
            "renders": renders,
        }

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
            return self._profile_get(h, path, query)
        return False

    def _profile_get(self, h, path: str, query: dict | None = None) -> bool:
        query = query or {}
        try:
            if path.endswith("/avatar"):
                pid = self._pid(path, "/api/profiles/", "/avatar")
                return self._serve_avatar(h, pid)
            if path.endswith("/personality/speech-preview"):
                pid = self._pid(path, "/api/profiles/",
                                "/personality/speech-preview")
                p = self.mgr.get(pid)
                if p is None:
                    h._json({"error": "no such profile"}, 404)
                    return True
                h._json(self._speech_preview(pid, p, query))
                return True
            if path.endswith("/personality/effective"):
                pid = self._pid(path, "/api/profiles/",
                                "/personality/effective")
                p = self.mgr.get(pid)
                if p is None:
                    h._json({"error": "no such profile"}, 404)
                    return True
                h._json(self._persona_effective(
                    pid, p, str((query.get("text") or [""])[0])))
                return True
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
            if path.endswith("/farewell"):
                pid = self._pid(path, "/api/profiles/", "/farewell")
                return self._farewell(h, pid)
            if path.endswith("/memory"):
                pid = self._pid(path, "/api/profiles/", "/memory")
                if self.mgr.get(pid) is None:
                    h._json({"error": "no such profile"}, 404)
                    return True
                h._json({"memories": self._memory(pid).list()})
                return True
            if path.endswith("/voice"):
                pid = self._pid(path, "/api/profiles/", "/voice")
                if self.mgr.get(pid) is None:
                    h._json({"error": "no such profile"}, 404)
                    return True
                h._json({"voice": self._voice_state(pid)})
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
            # Probe fresh — at greeting time the periodic tick may not have
            # run yet, leaving every component reading "starting". Treat
            # "stopped" and "starting" as fine: models and ComfyUI idle or
            # warm up lazily by design, so neither is worth announcing.
            states = {
                name: self.state.health.check(name)
                for name in list(self.state.health.components)
            }
            if any(s in {"degraded", "overloaded", "hung", "crashed",
                         "restarting"} for s in states.values()):
                health = "degraded"
        except Exception:
            pass
        g = self._greetings(pid).greeting(
            p, personality, is_adult=_is_adult(p), health=health)
        # Report a dirty previous session once — she explains the crash and
        # what recovery found rather than greeting like nothing happened.
        prior = getattr(self.state, "prior_session_abnormal", None)
        if prior and not getattr(self.state, "_crash_greeting_given", False):
            self.state._crash_greeting_given = True
            g["text"] += (
                " (Also — my last session ended without a clean shutdown, "
                "so a crash or forced close. I ran recovery checks on "
                "startup; there's a crash report in notifications if you "
                "want the details.)")
        if g["kind"] == "intro":
            self.mgr.mark_intro_completed(pid)
        # Speak the greeting once per process per profile — mute and
        # voice-disabled states drop it silently. The response carries
        # the audio URL so the page can play it deterministically; the
        # bus segment alone raced the page's voice event subscription.
        try:
            spoken = self.state.speak_greeting(pid, str(g.get("text") or ""))
        except Exception:
            spoken = None
        if spoken and spoken.get("url"):
            g["voice_url"] = spoken["url"]
            g["voice_segment_id"] = spoken.get("segment_id") or ""
        h._json(g)
        return True

    def _farewell(self, h, pid: str) -> bool:
        """GET /api/profiles/{pid}/farewell — persona-based goodbye for
        the desktop shutdown sequence. Same render + synchronous voice
        contract as /greeting; the host plays the wav itself and waits
        for real playback completion before exiting."""
        p = self.mgr.get(pid)
        if p is None:
            h._json({"error": "no such profile"}, 404)
            return True
        personality = self._personality(pid).resolve_active(
            is_adult=_is_adult(p))
        f = self._greetings(pid).farewell(
            p, personality, is_adult=_is_adult(p))
        try:
            spoken = self.state.speak_farewell(pid, str(f.get("text") or ""))
        except Exception:
            spoken = None
        if spoken and spoken.get("url"):
            f["voice_url"] = spoken["url"]
            f["voice_segment_id"] = spoken.get("segment_id") or ""
        h._json(f)
        return True

    # -- POST / PATCH -----------------------------------------------------

    def handle_post(self, h, path: str, body: dict) -> bool:
        try:
            if path == "/api/profiles":
                return self._create(h, body)
            if path == "/api/profiles/switch":
                return self._switch(h, body)
            if path == "/api/onboarding/welcome-played":
                self.mgr.mark_onboarding_welcome_played()
                h._json({"ok": True})
                return True
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
        elif action == "set_vocalizations":
            h._json({"ok": True, "vocalizations": store.set_vocalizations(
                body.get("level"))})
        elif action == "create_custom":
            h._json({"ok": True, "custom": store.create_custom(
                is_adult=adult, name=body.get("name"),
                base_preset=body.get("base_preset"),
                traits=body.get("traits"), voice=body.get("voice"),
                speech_genome=body.get("speech_genome"))})
        elif action == "patch_custom":
            h._json({"ok": True, "custom": store.patch_custom(
                str(body.get("personality_id") or ""), is_adult=adult,
                name=body.get("name"), traits=body.get("traits"),
                voice=body.get("voice"),
                speech_genome=body.get("speech_genome"))})
        elif action == "delete_custom":
            ok = store.delete_custom(str(body.get("personality_id") or ""))
            h._json({"ok": ok}, 404 if not ok else 200)
        elif action == "reset":
            store.set_active("preset:default-nexus", is_adult=adult)
            store.set_strength(70)
            dyn = self._dynamics(pid)
            dyn.clear_modifiers()
            dyn.reset_overlay()
            dyn.set_mode("")
            h._json({"ok": True,
                     "active": store.resolve_active(is_adult=adult)})
        elif action == "adjust_overlay":
            ov = self._dynamics(pid).adjust_overlay(
                body.get("trait_offsets"),
                note=str(body.get("note") or ""))
            h._json({"ok": True, "overlay": ov})
        elif action == "reset_overlay":
            self._dynamics(pid).reset_overlay()
            h._json({"ok": True})
        elif action == "add_modifier":
            mod = self._dynamics(pid).add_modifier(
                trait_offsets=body.get("trait_offsets"),
                note=str(body.get("note") or ""),
                ttl_seconds=float(body.get("ttl_seconds") or 0.0),
                scope=str(body.get("scope") or "conversation"),
                mode=str(body.get("mode") or ""))
            h._json({"ok": True, "modifier": mod})
        elif action == "clear_modifiers":
            n = self._dynamics(pid).clear_modifiers(
                scope=str(body.get("scope") or ""))
            h._json({"ok": True, "cleared": n})
        elif action == "set_mode":
            mode = self._dynamics(pid).set_mode(str(body.get("mode") or ""))
            h._json({"ok": True, "mode": mode})
        elif action == "coherence":
            from ..personality.customize import coherence_check
            h._json({"ok": True, **coherence_check(
                body.get("traits"), body.get("voice"))})
        elif action == "blend":
            from ..personality.customize import create_blend
            specs = body.get("blend")
            if not isinstance(specs, list):
                raise ProfileError("blend must be a list of "
                                   "{preset, weight}")
            custom = create_blend(
                store, specs, name=body.get("name"), is_adult=adult)
            h._json({"ok": True, "custom": custom})
        elif action == "export":
            from ..personality.customize import export_custom
            h._json({"ok": True, "package": export_custom(
                store, str(body.get("personality_id") or ""))})
        elif action == "import":
            from ..personality.customize import import_custom
            h._json({"ok": True, "custom": import_custom(
                store, body.get("package"), is_adult=adult)})
        elif action == "set_address":
            addr = self._dynamics(pid).set_address(
                str(body.get("address") or ""))
            h._json({"ok": True, "address": addr})
        elif action == "reset_adaptations":
            self._dynamics(pid).reset_continuity()
            h._json({"ok": True})
        elif action == "adaptations":
            # Inspect learned adaptations — counts/stances only, never
            # raw private memory.
            dyn = self._dynamics(pid)
            h._json({"ok": True,
                     "adaptations": {
                         "overlay": dyn.overlay(),
                         "modifiers": dyn.modifiers(),
                         "mode": dyn.mode(),
                         "continuity": dyn.continuity_view(),
                         "relationship": dyn.relationship(),
                         "metrics": dyn.metrics()}})
        elif action == "metrics":
            from ..personality.introspect import behavior_report
            h._json({"ok": True,
                     **behavior_report(self._dynamics(pid).metrics())})
        elif action == "compare":
            from ..personality.introspect import compare_personas
            dyn = self._dynamics(pid)
            ids = body.get("personas")
            if not isinstance(ids, list) or not ids:
                raise ProfileError(
                    "compare needs 'personas': [preset-id, ...]")
            h._json({"ok": True, "comparison": compare_personas(
                store, [str(x) for x in ids],
                user_text=str(body.get("text") or ""),
                is_adult=adult, dyn_state=dyn.state(),
                relationship=dyn.relationship())})
        elif action == "describe":
            from ..personality.introspect import describe_persona
            dyn = self._dynamics(pid)
            active = store.resolve_active(is_adult=adult)
            eff = self._persona_effective(pid, p)
            h._json({"ok": True,
                     "description": describe_persona(
                         eff.get("effective") or {}, active)})
        elif action == "similarity":
            from ..personality.introspect import (
                persona_similarity, similarity_audit)
            a, b = str(body.get("a") or ""), str(body.get("b") or "")
            if a and b:
                h._json({"ok": True, **persona_similarity(a, b)})
            else:
                h._json({"ok": True,
                         "flagged": similarity_audit()})
        elif action == "preview":
            # Personality-aware sample text + voice params — no TTS call.
            active = store.resolve_active(is_adult=adult)
            # Optional unsaved edits preview — whitelisted + gated.
            if isinstance(body.get("traits"), dict):
                active["traits"] = clean_traits(
                    body["traits"], is_adult=adult)
            if isinstance(body.get("voice"), dict):
                active["voice"] = clean_voice(body["voice"])
            # Preview always renders the returning-greeting style —
            # never the once-per-profile intro.
            preview_profile = dict(p, has_completed_intro=True)
            g = GreetingService(
                self.mgr.profile_dir(pid)).greeting(
                preview_profile, active, is_adult=adult)
            h._json({"ok": True, "preview": g["text"],
                     "voice": g["voice"], "personality": active})
        else:
            h._json({"error": "unknown personality action"}, 400)
        return True

    def _voice_state(self, pid: str) -> dict:
        try:
            cur = json.loads(
                (self.mgr.profile_dir(pid) / "voice.json")
                .read_text(encoding="utf-8"))
            return cur if isinstance(cur, dict) else {}
        except Exception:
            return {}

    def _voice_post(self, h, pid: str, body: dict) -> bool:
        """Profile-scoped voice selection — stored in voice.json inside
        the profile dir (not the global voice preset store)."""
        p = self.mgr.get(pid)
        if p is None:
            h._json({"error": "no such profile"}, 404)
            return True
        vpath = self.mgr.profile_dir(pid, create=True) / "voice.json"
        cur = self._voice_state(pid)
        for key in ("preset_id", "speed", "prosody", "gain_db"):
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
        # The Start Here page speaks the welcome instructions — voice
        # read/playback routes must pass the onboarding gate for that.
        "/api/voice/status", "/api/voice/speak", "/api/voice/stop",
        "/api/voice/mute", "/api/voice/preview", "/api/voice/audio",
    )

    @classmethod
    def allowed_while_locked(cls, path: str) -> bool:
        """While onboarding_required, only onboarding APIs + static files
        pass; every other API returns 403."""
        if not path.startswith("/api/"):
            return True                     # static assets/pages
        return any(path.startswith(p) for p in cls.SAFE_PREFIXES)
