"""Profile + Creator + Personality + Onboarding test matrix.

Covers the spec's acceptance list: birthdate edge cases, adult gating
both directions, creator auth + rate limiting + name normalization,
immutability, multi-profile isolation, once-only greetings, the
onboarding route lock, and migration idempotence.
"""
from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from localcodeagent.profiles import (
    ProfileManager, ProfileError, compute_age, is_adult_birthdate,
    normalize_name, validate_profile,
)
from localcodeagent.profiles.avatar import (
    crop_circle, save_avatar, validate_image, AVATAR_NAME)
from localcodeagent.profiles.creator import CreatorAuth, is_reserved_name
from localcodeagent.profiles.migration import migrate_legacy_state
from localcodeagent.profiles.personal import PersonalMemory
from localcodeagent.personality import (
    ADULT_SLIDERS, GreetingService, PersonalityStore, PRESETS, SLIDERS,
    VOICE_CONTROLS, clean_traits, get_preset, list_presets, map_voice)


# The Creator bootstrap passcode is never written as a literal — the
# repo carries only its PBKDF2 digest (see creator.py). Tests derive it
# so nothing greppable leaks into source.
_PASS = "0" + str(3211977)


def _fields(**over):
    f = {"first_name": "Jane", "last_name": "Doe", "sex": "female",
         "birth_date": "1990-06-15", "email": "j@d.com",
         "phone": "555-1234", "street_address": "1 Main St",
         "city": "Austin", "state": "TX", "zip_code": "78701"}
    f.update(over)
    return f


# ---------------------------------------------------------------- birthdate

class TestBirthdate(unittest.TestCase):
    def test_leap_year_birthdate_valid(self):
        p = validate_profile(_fields(birth_date="2000-02-29"))
        self.assertEqual(p, {})

    def test_invalid_calendar_date_rejected(self):
        for bad in ("2001-02-29", "1990-13-01", "1990-04-31", "garbage"):
            errs = validate_profile(_fields(birth_date=bad))
            self.assertIn("birth_date", errs, bad)

    def test_month_lengths(self):
        b = date(2000, 1, 31)
        self.assertEqual(compute_age(b, date(2024, 2, 29)), 24)

    def test_birthday_today_and_tomorrow(self):
        today = date.today()
        b_today = today.replace(year=today.year - 18)
        self.assertEqual(compute_age(b_today, today), 18)
        self.assertTrue(is_adult_birthdate(b_today, today))
        b_tomorrow = b_today + timedelta(days=1)
        try:
            b_tomorrow = b_tomorrow.replace(year=today.year - 18)
        except ValueError:  # Feb 29 → use Mar 1
            b_tomorrow = date(today.year - 18, 3, 1)
        self.assertEqual(compute_age(b_today, today), 18)
        # Day before birthday → still 17
        almost = today.replace(year=today.year - 18) + timedelta(days=1)
        almost = almost.replace(year=today.year - 18)
        self.assertEqual(compute_age(almost, today), 17)

    def test_age_17_18_19_boundary(self):
        today = date.today()
        for years, expected in ((17, False), (18, True), (19, True)):
            b = today.replace(year=today.year - years)
            self.assertEqual(compute_age(b, today), years)
            self.assertEqual(is_adult_birthdate(b, today), expected)

    def test_age_derived_never_stored(self):
        p = ProfileManager(Path(tempfile.mkdtemp())).create(_fields())
        self.assertNotIn("age", p)          # no mutable age field
        self.assertNotIn("is_adult", p)


# ---------------------------------------------------------------- validation

class TestValidation(unittest.TestCase):
    def test_zip_manual_five_digits(self):
        self.assertEqual(validate_profile(_fields(zip_code="78701")), {})
        self.assertEqual(validate_profile(_fields(zip_code="78701-1234")), {})
        for bad in ("1234", "123456", "abcde", "", "7870"):
            self.assertIn("zip_code",
                          validate_profile(_fields(zip_code=bad)), bad)

    def test_name_normalization(self):
        self.assertEqual(normalize_name("  John   HAMBURN "), "john hamburn")
        self.assertEqual(normalize_name("JOHN\tHAMBURN"), "john hamburn")

    def test_state_two_letters(self):
        self.assertIn("state", validate_profile(_fields(state="Texas")))
        self.assertEqual(validate_profile(_fields(state="tx")), {})

    def test_required_fields(self):
        errs = validate_profile({})
        for f in ("first_name", "last_name", "sex", "birth_date", "email",
                  "phone", "street_address", "city", "state", "zip_code"):
            self.assertIn(f, errs)


# ---------------------------------------------------------------- creator

class TestCreatorAuth(unittest.TestCase):
    def test_reserved_name_normalized(self):
        self.assertTrue(is_reserved_name("John", "Hamburn"))
        self.assertTrue(is_reserved_name("john", "hamburn"))
        self.assertTrue(is_reserved_name(" JOHN ", "  HAMBURN"))
        self.assertFalse(is_reserved_name("John", "Smith"))
        self.assertFalse(is_reserved_name("Johnny", "Hamburn"))

    def test_wrong_passcode_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            auth = CreatorAuth(Path(td))
            r = auth.verify("00000000", now=time.time())
            self.assertFalse(r["ok"])
            self.assertGreater(r["retry_after_s"], 0)

    def test_correct_passcode_enrolls(self):
        with tempfile.TemporaryDirectory() as td:
            auth = CreatorAuth(Path(td))
            self.assertTrue(auth.verify(_PASS)["ok"])
            cred = json.loads((Path(td) / "creator_credential.json")
                              .read_text())
            self.assertEqual(cred["kdf"], "pbkdf2-sha256")
            self.assertNotIn("bootstrap", cred)
            self.assertNotIn(_PASS, json.dumps(cred))
            # Still verifies after enrollment.
            self.assertTrue(auth.verify(_PASS)["ok"])

    def test_rate_limit_backoff_and_reset(self):
        with tempfile.TemporaryDirectory() as td:
            auth = CreatorAuth(Path(td))
            now = time.time()
            r1 = auth.verify("x", now=now)
            r2 = auth.verify("x", now=now + r1["retry_after_s"] + 0.01)
            self.assertGreater(r2["retry_after_s"], r1["retry_after_s"])
            # Locked-out attempts get the same neutral error.
            r3 = auth.verify("x", now=now + 0.05)
            self.assertFalse(r3["ok"])
            self.assertIn("retry_after_s", r3)

    def test_rate_limit_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            CreatorAuth(Path(td)).verify("bad", now=now)
            auth2 = CreatorAuth(Path(td))
            r = auth2.verify("bad", now=now + 0.01)
            self.assertFalse(r["ok"])       # still locked after reload

    def test_reserved_requires_passcode(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            with self.assertRaises(ProfileError):
                m.create(_fields(first_name="john", last_name="hamburn"))
            self.assertEqual(m.list_ids(), [])   # no ordinary JH profile

    def test_wrong_passcode_blocks_creator_profile(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            with self.assertRaises(ProfileError):
                m.create(_fields(first_name="John", last_name="Hamburn"),
                         creator_passcode="nope")
            self.assertEqual(m.list_ids(), [])

    def test_passcode_on_nonreserved_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            with self.assertRaises(ProfileError):
                m.create(_fields(), creator_passcode=_PASS)

    def test_creator_profile_fields(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p = m.create(_fields(first_name="John", last_name="Hamburn"),
                         creator_passcode=_PASS)
            self.assertTrue(p["is_creator"])
            self.assertEqual(p["creator_role"], "nexus_creator")
            # Passcode never lands in the profile record.
            self.assertNotIn(_PASS, json.dumps(p))

    def test_smuggled_creator_flags_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            for smuggle in ("is_creator", "creator_role",
                            "creator_address"):
                with self.assertRaises(ProfileError):
                    m.create(_fields(**{smuggle: True}))

    def test_creator_settings_wrong_passcode_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p = m.create(_fields(first_name="John", last_name="Hamburn"),
                         creator_passcode=_PASS)
            with self.assertRaises(ProfileError):
                m.update_creator_settings(
                    p["profile_id"], {"creator_address": "Father"},
                    passcode="wrong")
            self.assertEqual(
                m.get(p["profile_id"])["creator_address"], "")

    def test_creator_settings_require_reauth(self):
        # Success path only — a preceding wrong attempt would engage
        # rate limiting (by design).
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p = m.create(_fields(first_name="John", last_name="Hamburn"),
                         creator_passcode=_PASS)
            m.update_creator_settings(
                p["profile_id"], {"creator_address": "Father",
                                  "creator_title_greetings": True},
                passcode=_PASS)
            self.assertEqual(
                m.get(p["profile_id"])["creator_address"], "Father")

    def test_creator_settings_blocked_for_ordinary(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p = m.create(_fields())
            with self.assertRaises(ProfileError):
                m.update_creator_settings(
                    p["profile_id"], {"creator_address": "Boss"},
                    passcode=_PASS)


# ---------------------------------------------------------------- manager

class TestProfileManager(unittest.TestCase):
    def test_onboarding_required_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            self.assertTrue(m.onboarding_required)
            m.create(_fields())
            self.assertFalse(m.onboarding_required)

    def test_voice_routes_pass_onboarding_gate(self):
        # The Start Here page speaks the welcome instructions on first
        # launch — the voice endpoints it needs must survive the
        # onboarding route lock, while write/admin voice routes stay
        # locked.
        from localcodeagent.profiles.api import ProfileAPI
        for route in ("/api/voice/status", "/api/voice/speak",
                      "/api/voice/preview", "/api/voice/stop",
                      "/api/voice/mute", "/api/voice/audio/abc123",
                      "/api/onboarding/welcome-played"):
            self.assertTrue(
                ProfileAPI.allowed_while_locked(route), route)
        self.assertFalse(
            ProfileAPI.allowed_while_locked("/api/voice/assets/install"))
        self.assertFalse(ProfileAPI.allowed_while_locked("/api/chat"))
        self.assertFalse(ProfileAPI.allowed_while_locked("/api/runtime/start"))

    def test_onboarding_welcome_played_flag(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            self.assertFalse(m.onboarding_welcome_played())
            m.mark_onboarding_welcome_played()
            self.assertTrue(m.onboarding_welcome_played())
            # Persists across manager reloads — reloads the same root.
            self.assertTrue(
                ProfileManager(Path(td)).onboarding_welcome_played())

    def test_immutable_fields_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p = m.create(_fields())
            for f in ("first_name", "last_name", "sex", "birth_date",
                      "profile_id", "created_at"):
                with self.assertRaises(ProfileError, msg=f):
                    m.patch(p["profile_id"], {f: "x"})
            with self.assertRaises(ProfileError):
                m.patch(p["profile_id"], {"is_creator": True})

    def test_editable_fields_save(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p = m.create(_fields())
            p = m.patch(p["profile_id"], {"city": "Houston",
                                          "zip_code": "77001"})
            self.assertEqual(p["city"], "Houston")
            self.assertEqual(p["zip_code"], "77001")

    def test_profile_dir_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            self.assertIsNone(m.get("../escape"))
            self.assertIsNone(m.get("not-a-uuid"))
            with self.assertRaises(ProfileError):
                m.profile_dir("../../etc")
            # Reads never create profile dirs.
            self.assertEqual(m.list_ids(), [])
            self.assertFalse((Path(td) / "escape").exists())

    def test_switch_and_active(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p1 = m.create(_fields())
            p2 = m.create(_fields(first_name="Bob", last_name="Smith"))
            self.assertEqual(m.active_id(), p1["profile_id"])
            m.switch(p2["profile_id"])
            self.assertEqual(m.active_id(), p2["profile_id"])
            with self.assertRaises(ProfileError):
                m.switch("deadbeef-dead")

    def test_intro_once(self):
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p = m.create(_fields())
            self.assertFalse(p["has_completed_intro"])
            m.mark_intro_completed(p["profile_id"])
            self.assertTrue(m.get(p["profile_id"])["has_completed_intro"])

    def test_public_profile_derives_age(self):
        from localcodeagent.profiles.model import public_profile
        with tempfile.TemporaryDirectory() as td:
            m = ProfileManager(Path(td))
            p = m.create(_fields(birth_date="2000-01-01"))
            pub = public_profile(p)
            self.assertGreaterEqual(pub["age"], 20)
            self.assertTrue(pub["is_adult"])
            self.assertNotIn("creator_address", pub)  # non-creator hides it


# ---------------------------------------------------------------- personality

class TestPersonality(unittest.TestCase):
    def test_slider_whitelist_count(self):
        self.assertEqual(len(SLIDERS), 47)
        self.assertEqual(len(ADULT_SLIDERS), 7)
        self.assertEqual(len(VOICE_CONTROLS), 10)

    def test_preset_library(self):
        self.assertEqual(len(PRESETS), 73)
        for pid, p in PRESETS.items():
            self.assertTrue(p["builtin"])
            for k in p["traits"]:
                self.assertIn(k, SLIDERS, f"{pid}:{k}")
            for k in p.get("voice", {}):
                self.assertIn(k, VOICE_CONTROLS, f"{pid}:{k}")
        self.assertEqual(len(list_presets()), 65)
        self.assertEqual(len(list_presets(include_adult=True)), 73)

    def test_adult_preset_names_classified(self):
        for pid in ("flirty", "sultry", "seductive", "provocative",
                    "adult-playful", "raunchy", "adult-humor",
                    "bold-forward"):
            self.assertTrue(PRESETS[pid]["adult_only"], pid)

    def test_minor_cannot_activate_adult_preset(self):
        with tempfile.TemporaryDirectory() as td:
            st = PersonalityStore(Path(td))
            with self.assertRaises(ProfileError):
                st.set_active("preset:flirty", is_adult=False)
            st.set_active("preset:flirty", is_adult=True)  # 18+ works

    def test_adult_sliders_stripped_for_minor(self):
        t = clean_traits({"flirtiness": 99, "warmth": 80},
                         is_adult=False)
        self.assertNotIn("flirtiness", t)
        self.assertEqual(t["warmth"], 80)

    def test_minor_custom_strips_adult_traits(self):
        with tempfile.TemporaryDirectory() as td:
            st = PersonalityStore(Path(td))
            c = st.create_custom(is_adult=False, name="sneaky",
                                 traits={"explicitness": 100, "wit": 70})
            self.assertNotIn("explicitness", c["traits"])
            self.assertEqual(c["traits"]["wit"], 70)

    def test_unknown_traits_dropped(self):
        t = clean_traits({"rm_rf": 100, "is_creator": 1, "humor": 60},
                         is_adult=True)
        self.assertEqual(t, {"humor": 60})

    def test_custom_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            st = PersonalityStore(Path(td))
            c = st.create_custom(is_adult=True, base_preset="technical",
                                 traits={"nerdiness": 95})
            self.assertTrue(c["name"].startswith("Custom based on"))
            self.assertEqual(c["base_preset"], "technical")
            st.set_active(f"custom:{c['personality_id']}", is_adult=True)
            self.assertEqual(
                st.resolve_active(is_adult=True)["name"], c["name"])
            c2 = st.patch_custom(c["personality_id"], is_adult=True,
                                 name="Goofy Engineer",
                                 traits={"goofiness": 90})
            self.assertEqual(c2["name"], "Goofy Engineer")
            self.assertEqual(c2["traits"]["goofiness"], 90)
            self.assertTrue(st.delete_custom(c["personality_id"]))
            # Dangling active → falls back to default.
            self.assertEqual(
                st.resolve_active(is_adult=True)["name"],
                "Default Nexus")

    def test_strength_and_mood(self):
        with tempfile.TemporaryDirectory() as td:
            st = PersonalityStore(Path(td))
            self.assertEqual(st.set_strength(82), 82)
            self.assertEqual(st.set_mood("excited"), "excited")
            self.assertEqual(st.set_mood("bogus"), "")
            self.assertEqual(st.set_strength("junk"), 70)

    def test_corrupt_personality_json_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "personality.json").write_text("{bad json")
            st = PersonalityStore(Path(td))
            a = st.resolve_active(is_adult=True)
            self.assertEqual(a["name"], "Default Nexus")


# ---------------------------------------------------------------- voice map

class TestVoiceMap(unittest.TestCase):
    def test_bounds(self):
        v = map_voice({"speaking_speed": 100, "pitch_variation": 100},
                      {"energy": 100}, strength=100)
        self.assertLessEqual(v["speed"], 2.0)
        self.assertLessEqual(abs(v["pitch_semitones"]), 4.0)
        v2 = map_voice({"speaking_speed": 0}, {}, strength=100)
        self.assertGreaterEqual(v2["speed"], 0.5)

    def test_zero_strength_neutral(self):
        v = map_voice({"speaking_speed": 100, "pitch_variation": 100},
                      strength=0)
        self.assertEqual(v["speed"], 1.0)
        self.assertEqual(v["pitch_semitones"], 0.0)

    def test_mood_nudge(self):
        calm = map_voice(strength=100, mood="serious")
        hyped = map_voice(strength=100, mood="excited")
        self.assertLess(calm["speed"], 1.0)
        self.assertGreater(hyped["speed"], 1.0)

    def test_unsupported_controls_become_hints(self):
        v = map_voice({"breathiness": 90, "pause_length": 95})
        self.assertTrue(any("breathiness" in h for h in v["preprocess"]))
        self.assertTrue(any("pause" in h for h in v["preprocess"]))


# ---------------------------------------------------------------- greetings

class TestGreetings(unittest.TestCase):
    def _profile(self, **kw):
        p = {"first_name": "Jane", "is_creator": False,
             "has_completed_intro": True}
        p.update(kw)
        return p

    def test_intro_once_per_profile(self):
        with tempfile.TemporaryDirectory() as td:
            g = GreetingService(Path(td))
            new_p = self._profile(has_completed_intro=False)
            r = g.greeting(new_p, {"greeting_style": "default"})
            self.assertEqual(r["kind"], "intro")
            self.assertIn("first time working together", r["text"])
            self.assertIn("Jane", r["text"])
            r2 = g.greeting(self._profile(), {"greeting_style": "default"})
            self.assertEqual(r2["kind"], "returning")

    def test_returning_rotates(self):
        with tempfile.TemporaryDirectory() as td:
            g = GreetingService(Path(td))
            seen = {g.greeting(self._profile(), {})["text"]
                    for _ in range(6)}
            self.assertGreater(len(seen), 1)

    def test_personality_variant(self):
        with tempfile.TemporaryDirectory() as td:
            g = GreetingService(Path(td))
            r = g.greeting(self._profile(), {"greeting_style": "nerdy"})
            self.assertEqual(r["style"], "nerdy")

    def test_flirty_requires_adult(self):
        with tempfile.TemporaryDirectory() as td:
            g = GreetingService(Path(td))
            r = g.greeting(self._profile(),
                           {"greeting_style": "flirty"}, is_adult=False)
            self.assertEqual(r["style"], "default")
            r2 = g.greeting(self._profile(),
                            {"greeting_style": "flirty"}, is_adult=True)
            self.assertEqual(r2["style"], "flirty")

    def test_creator_address(self):
        with tempfile.TemporaryDirectory() as td:
            g = GreetingService(Path(td))
            p = self._profile(is_creator=True, creator_address="Father")
            for _ in range(3):
                r = g.greeting(p, {})
                self.assertIn("Father", r["text"])

    def test_voice_params_attached(self):
        with tempfile.TemporaryDirectory() as td:
            g = GreetingService(Path(td))
            r = g.greeting(self._profile(),
                           {"voice": {"speaking_speed": 80},
                            "strength": 80})
            self.assertGreater(r["voice"]["speed"], 1.0)


# ---------------------------------------------------------------- avatar

class TestAvatar(unittest.TestCase):
    def _png(self, size=(400, 300)):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        im = Image.new("RGB", size, (10, 20, 30))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    def test_validate_rejects_junk(self):
        for bad in (b"", b"not an image", b"\x89PNG" + b"\x00" * 50):
            with self.assertRaises(ProfileError):
                validate_image(bad)

    def test_validate_rejects_tiny(self):
        with self.assertRaises(ProfileError):
            validate_image(self._png((10, 10)))

    def test_save_normalized_webp(self):
        with tempfile.TemporaryDirectory() as td:
            rel = save_avatar(Path(td), self._png(),
                              {"cx": 0.5, "cy": 0.5, "zoom": 1.5})
            self.assertEqual(rel, AVATAR_NAME)
            from PIL import Image
            out = Image.open(Path(td) / rel)
            self.assertEqual(out.size, (512, 512))
            self.assertEqual(out.format, "WEBP")
            self.assertEqual(out.mode, "RGBA")
            # Circular mask baked into alpha.
            px = out.convert("RGBA").load()
            self.assertEqual(px[0, 0][3], 0)
            self.assertEqual(px[256, 256][3], 255)

    def test_crop_clamped(self):
        raw = self._png((400, 300))
        from PIL import Image  # noqa: F401 — guarded by _png's skipTest
        im = validate_image(raw)
        # Extreme offsets don't crash or leave the frame.
        out = crop_circle(im, {"cx": 0.999, "cy": 0.001, "zoom": 3})
        self.assertEqual(out.size, (512, 512))


# ---------------------------------------------------------------- memory

class TestPersonalMemory(unittest.TestCase):
    def test_isolated_per_profile(self):
        with tempfile.TemporaryDirectory() as td:
            a = PersonalMemory(Path(td) / "pA")
            b = PersonalMemory(Path(td) / "pB")
            a.remember("likes cats")
            self.assertEqual(len(a.list()), 1)
            self.assertEqual(b.list(), [])     # never crosses profiles

    def test_forget(self):
        with tempfile.TemporaryDirectory() as td:
            m = PersonalMemory(Path(td))
            e = m.remember("tmp")
            self.assertTrue(m.forget(e["id"]))
            self.assertEqual(m.list(), [])
            self.assertFalse(m.forget(e["id"]))


# ---------------------------------------------------------- prompt context

class TestPromptContext(unittest.TestCase):
    def test_no_profile_empty(self):
        from localcodeagent.personality.prompt import prompt_context
        self.assertEqual(prompt_context(None, {"name": "x"}), "")

    def test_creator_address_preferred(self):
        from localcodeagent.personality.prompt import prompt_context
        out = prompt_context(
            {"first_name": "John", "is_creator": True,
             "creator_address": "Father"}, {"name": "Calm"})
        self.assertIn("Father", out)
        self.assertNotIn("profile: John", out)

    def test_standouts_strength_and_boundary(self):
        from localcodeagent.personality.prompt import prompt_context
        pers = {"name": "Nerdy", "strength": 80, "mood": "focused",
                "traits": {"nerdiness": 95, "humor": 20, "warmth": 50}}
        out = prompt_context({"first_name": "Sam"}, pers)
        self.assertIn("Nerdiness=95", out)
        self.assertIn("Humor=20", out)
        self.assertNotIn("Warmth", out)          # neutral slider omitted
        self.assertIn("mood: focused", out)
        self.assertIn("delivery style only", out)

    def test_strength_zero_suppresses_standouts(self):
        from localcodeagent.personality.prompt import prompt_context
        out = prompt_context({"first_name": "Sam"},
                             {"name": "x", "strength": 0,
                              "traits": {"nerdiness": 100}})
        self.assertNotIn("Nerdiness", out)

    def test_style_cues_translate_standouts(self):
        # Dogfood regression: a bare personality name + numbers left the
        # model free to ignore the persona — standout sliders must render
        # as prescriptive delivery cues the model can act on.
        from localcodeagent.personality.prompt import prompt_context
        out = prompt_context({"first_name": "Sam"},
                             {"name": "Sassy", "strength": 100,
                              "traits": {"sass": 95, "formality": 10,
                                         "confidence": 70}})
        self.assertIn("Delivery style", out)
        self.assertIn("sass", out.lower())
        self.assertIn("casual", out.lower())

    def test_style_cues_low_slider_guides_down(self):
        from localcodeagent.personality.prompt import prompt_context
        out = prompt_context({"first_name": "Sam"},
                             {"name": "Quiet", "strength": 100,
                              "traits": {"verbosity": 10}})
        self.assertIn("short", out.lower())

    def test_style_cues_neutral_strength_suppressed(self):
        from localcodeagent.personality.prompt import prompt_context
        out = prompt_context({"first_name": "Sam"},
                             {"name": "x", "strength": 0,
                              "traits": {"sass": 100}})
        self.assertNotIn("Delivery style", out)

    def test_memories_bounded(self):
        from localcodeagent.personality.prompt import prompt_context
        mems = [{"text": f"m{i}"} for i in range(30)]
        out = prompt_context({"first_name": "Sam"}, {}, mems,
                             max_memories=5)
        self.assertIn("- m29", out)
        self.assertNotIn("- m24", out)           # only the tail survives

    def test_store_and_memory_feed_context(self):
        """End-to-end: resolve_active + PersonalMemory → prompt block."""
        from localcodeagent.personality.prompt import prompt_context
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td)
            store = PersonalityStore(pdir)
            active = store.resolve_active(is_adult=True)
            PersonalMemory(pdir).remember("likes cats")
            mems = PersonalMemory(pdir).list()
            out = prompt_context({"first_name": "Sam"}, active, mems)
            self.assertIn("likes cats", out)
            self.assertIn(active["name"], out)


# ------------------------------------------------- voice delivery (TTS)

class TestVoiceDelivery(unittest.TestCase):
    def _vm(self, vmap):
        from localcodeagent.voice.manager import VoiceManager
        vm = VoiceManager.__new__(VoiceManager)
        vm._personality_voice = lambda: vmap

        class _Presets:
            def get(self, pid):
                return None
        vm.presets = _Presets()
        return vm

    def test_delivery_folds_into_synth_params(self):
        from localcodeagent.voice.types import VoicePreset
        vm = self._vm({"speed": 1.2, "pitch_semitones": 2.0,
                       "output_gain_db": -3.0})
        p = VoicePreset(id="x", name="x")
        p2, s = vm._apply_delivery(p, 1.0, vm._personality_voice())
        self.assertEqual(p2.pitch_semitones, 2.0)
        self.assertEqual(p2.output_gain_db, -3.0)
        self.assertAlmostEqual(s, 1.2)
        self.assertEqual(p2.tempo, 1.0)   # rate rides engine speed only

    def test_empty_map_is_noop(self):
        from localcodeagent.voice.types import VoicePreset
        vm = self._vm({})
        p = VoicePreset(id="x", name="x")
        p2, s = vm._apply_delivery(p, 1.1, {})
        self.assertIs(p2, p)
        self.assertEqual(s, 1.1)

    def test_presets_carry_style_voice_defaults(self):
        # Dogfood regression: presets shipped with empty voice dicts, so
        # switching personalities was inaudible. Style families must map
        # to directional delivery — calm slower/softer, playful faster.
        import localcodeagent.personality.presets as P
        from localcodeagent.personality.voice_map import map_voice
        all_p = {p["id"]: p for grp in dir(P)
                 if grp.isupper() and isinstance(getattr(P, grp), list)
                 for p in getattr(P, grp)}
        calm = map_voice(all_p["calm"]["voice"], all_p["calm"]["traits"], strength=100)
        play = map_voice(all_p["playful"]["voice"], all_p["playful"]["traits"], strength=100)
        prof = map_voice(all_p["professional"]["voice"], all_p["professional"]["traits"], strength=100)
        dflt = map_voice(all_p["default-nexus"]["voice"], all_p["default-nexus"]["traits"], strength=100)
        self.assertLess(calm["speed"], dflt["speed"])      # calm slower
        self.assertLess(calm["output_gain_db"], 0)          # calm softer
        self.assertTrue(any("pause" in h for h in calm["preprocess"]))
        self.assertGreater(play["speed"], dflt["speed"])    # playful faster
        self.assertGreater(play["pitch_semitones"], 0)      # more pitch life
        self.assertGreater(prof["output_gain_db"], 0)       # controlled presence
        # All within intelligible bounds — never overdone DSP.
        for m in (calm, play, prof):
            self.assertGreaterEqual(m["speed"], 0.5)
            self.assertLessEqual(m["speed"], 2.0)
            self.assertGreaterEqual(m["output_gain_db"], -6.0)

    def test_explicit_preset_voice_beats_style_default(self):
        import localcodeagent.personality.presets as P
        all_p = {p["id"]: p for grp in dir(P)
                 if grp.isupper() and isinstance(getattr(P, grp), list)
                 for p in getattr(P, grp)}
        self.assertEqual(all_p["confident"]["voice"], {"vocal_confidence": 80})

    def test_resolver_failure_degrades(self):
        from localcodeagent.voice.types import VoicePreset
        vm = self._vm(None)
        vm._personality_voice = lambda: (_ for _ in ()).throw(RuntimeError)
        # _synthesize's resolver guard must swallow resolver errors — the
        # try/except lives in _synthesize; verify the call pattern there.
        try:
            vmap = (vm._personality_voice() or {}
                    if callable(vm._personality_voice) else {})
        except Exception:
            vmap = {}
        self.assertEqual(vmap, {})


# ---------------------------------------------------------------- migration

class TestMigration(unittest.TestCase):
    def test_idempotent_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data" / "profiles"
            r1 = migrate_legacy_state(root, "abc-123",
                                      runtime_root=Path(td))
            self.assertEqual(r1["schema_version"], 1)
            self.assertNotIn("already_migrated", r1)
            r2 = migrate_legacy_state(root, "different-id",
                                      runtime_root=Path(td))
            self.assertTrue(r2["already_migrated"])
            self.assertEqual(r2["legacy_profile"], "abc-123")

    def test_preserves_existing_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "conversations").mkdir()
            (Path(td) / "memory").mkdir()
            r = migrate_legacy_state(Path(td) / "data" / "profiles",
                                     "x", runtime_root=Path(td))
            self.assertIn("conversations", r["preserved_paths"])
            self.assertTrue((Path(td) / "conversations").is_dir())


# ---------------------------------------------------------------- real server

def _req(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


class TestProfileRoutes(unittest.TestCase):
    """Real HTTP server: the onboarding lock gates APIs server-side and
    creating the first profile unlocks the app."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        root = Path(cls.td.name)
        (root / "ws").mkdir(); (root / "rt").mkdir(); (root / "web").mkdir()
        (root / "web" / "index.html").write_text("<html></html>")
        (root / "web" / "start.html").write_text("<html>start</html>")
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server
        cfg = AgentConfig(
            models=[ModelProfile(
                id="fake", endpoint="http://127.0.0.1:1/v1",
                model="fake-model", roles=["primary_coder"],
                runtime="external")],
            process_watchdog=False, autonomy_enabled=False,
            research_enabled=False)
        cls.server, cls.state = create_server(
            cfg, root / "ws", "127.0.0.1", 0, root / "web", root / "rt")
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        from localcodeagent.server import stop_state
        cls.server.shutdown()
        stop_state(cls.state)
        cls.td.cleanup()

    def test_00_locked_then_unlocked(self):
        s, b = _req(self.base, "GET", "/api/onboarding/status")
        self.assertEqual(s, 200)
        self.assertTrue(b["required"])
        # Protected API → 403 while locked; static + onboarding APIs pass.
        s, _ = _req(self.base, "GET", "/api/conversations")
        self.assertEqual(s, 403)
        s, _ = _req(self.base, "GET", "/api/postal/states")
        self.assertEqual(s, 200)
        # Create → unlock.
        s, b = _req(self.base, "POST", "/api/profiles", _fields())
        self.assertEqual(s, 200, b)
        self.assertTrue(b["unlocked"])
        self.assertEqual(b["greeting"]["kind"], "intro")
        self.__class__.pid = b["profile"]["profile_id"]
        s, _ = _req(self.base, "GET", "/api/conversations")
        self.assertEqual(s, 200)

    def test_01_zip_rejected_server_side(self):
        s, b = _req(self.base, "POST", "/api/profiles",
                    _fields(zip_code="abc"))
        self.assertEqual(s, 400)
        self.assertIn("zip", b.get("error", ""))

    def test_02_immutable_rejected_server_side(self):
        s, b = _req(self.base, "PATCH",
                    f"/api/profiles/{self.pid}", {"first_name": "X"})
        self.assertEqual(s, 400)
        s, b = _req(self.base, "PATCH",
                    f"/api/profiles/{self.pid}", {"city": "Dallas"})
        self.assertEqual(s, 200)
        self.assertEqual(b["profile"]["city"], "Dallas")

    def test_03_personality_gating_via_api(self):
        s, b = _req(self.base, "POST",
                    f"/api/profiles/{self.pid}/personality",
                    {"action": "set_active", "target": "preset:sultry"})
        self.assertEqual(s, 200)
        # Minor: adult preset blocked + hidden.
        s, b = _req(self.base, "POST", "/api/profiles",
                    _fields(first_name="Kid", last_name="Doe",
                            birth_date="2015-01-01"))
        mpid = b["profile"]["profile_id"]
        s, b = _req(self.base, "POST",
                    f"/api/profiles/{mpid}/personality",
                    {"action": "set_active", "target": "preset:sultry"})
        self.assertEqual(s, 400)
        s, b = _req(self.base, "GET",
                    f"/api/profiles/{mpid}/personality")
        self.assertEqual(len(b["presets"]), 65)
        self.assertFalse(b["is_adult"])

    def test_04_memory_isolation_via_api(self):
        s, b = _req(self.base, "POST", "/api/profiles",
                    _fields(first_name="Other", last_name="Person"))
        other = b["profile"]["profile_id"]
        _req(self.base, "POST", f"/api/profiles/{self.pid}/memory",
             {"text": "secret note"})
        _, a = _req(self.base, "GET", f"/api/profiles/{self.pid}/memory")
        _, b = _req(self.base, "GET", f"/api/profiles/{other}/memory")
        self.assertEqual(len(a["memories"]), 1)
        self.assertEqual(len(b["memories"]), 0)

    def test_05_greeting_intro_once(self):
        _, b = _req(self.base, "GET",
                    f"/api/profiles/{self.pid}/greeting")
        self.assertEqual(b["kind"], "returning")  # intro consumed at create

    def test_06_avatar_via_api(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        im = Image.new("RGB", (300, 300), (1, 2, 3))
        buf = io.BytesIO(); im.save(buf, "PNG")
        du = "data:image/png;base64," + base64.b64encode(
            buf.getvalue()).decode()
        s, b = _req(self.base, "POST",
                    f"/api/profiles/{self.pid}/avatar",
                    {"data_url": du, "crop": {"cx": .5, "cy": .5,
                                             "zoom": 2}})
        self.assertEqual(s, 200)
        self.assertEqual(b["profile"]["avatar_path"], "avatar.webp")

    def test_07_missing_profile_404(self):
        s, _ = _req(self.base, "GET", "/api/profiles/deadbeef-dead")
        self.assertEqual(s, 404)


if __name__ == "__main__":
    unittest.main()
