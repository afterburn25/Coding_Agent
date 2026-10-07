"""Voice subsystem tests — filter classification, streaming segmentation,
presets, queue/mute/cancel, cache keys, DSP chain sanity. Engine inference
is mocked at the boundary; everything above it is exercised for real."""
from __future__ import annotations

import json
import tempfile
import time
import types
import unittest
from pathlib import Path

from localcodeagent.voice.speech_filter import SpeechTextFilter
from localcodeagent.voice.streamer import SentenceStreamer
from localcodeagent.voice.presets import (OFFICIAL_PRESET_ID, PresetError,
                                          VoicePresetStore)
from localcodeagent.voice.types import VoicePreset
from localcodeagent.voice.cache import AudioCache

try:
    import numpy as np
    from localcodeagent.voice import dsp
    from localcodeagent.voice.manager import VoiceManager
    HAS_NUMPY = True
except ImportError:  # CI without numpy — DSP/manager tests skip cleanly
    np = None
    dsp = None
    VoiceManager = None
    HAS_NUMPY = False


# --------------------------------------------------------------------------
# speech filter

class TestSpeechFilter(unittest.TestCase):
    def setUp(self):
        self.f = SpeechTextFilter()

    def test_prose_is_spoken(self):
        out = self.f.filter("The analysis is complete. Everything looks good.")
        self.assertIn("analysis is complete", out)
        self.assertIn("looks good", out)

    def test_code_fence_not_spoken(self):
        text = ("Here's the fix:\n\n```python\ndef broken():\n"
                "    return x_y_z[0]\n```\n\nThat should resolve it.")
        out = self.f.filter(text)
        self.assertNotIn("x_y_z", out)
        self.assertIn("resolve it", out)
        self.assertIn("code", out.lower())

    def test_terminal_output_skipped(self):
        out = self.f.filter(
            "Run this:\n$ python -m pytest tests/ -v\nPASSED 42 tests")
        self.assertNotIn("pytest", out)
        self.assertNotIn("PASSED", out)

    def test_json_blob_skipped(self):
        out = self.f.filter(
            'Config:\n{"enabled": true, "port": 8081, "host": "x"}\nDone.')
        self.assertNotIn('"port"', out)
        self.assertIn("Done", out)

    def test_url_and_hash_suppressed(self):
        out = self.f.filter(
            "See https://example.com/some/really/long/path?q=1 for details.\n"
            "SHA 496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4")
        self.assertNotIn("example.com", out)
        self.assertNotIn("496dba", out)
        self.assertIn("link", out)

    def test_file_path_speakable_name(self):
        out = self.f.filter("The problem is in runtime/manager.py as expected.")
        self.assertIn("manager", out)
        self.assertNotIn("runtime/manager.py", out)

    def test_mixed_prose_code_speaks_only_prose(self):
        text = ("I found the issue in the scheduler.\n\n"
                "```bash\nrm -rf /tmp/x\n```\n\n"
                "The queue now recovers cleanly.")
        out = self.f.filter(text)
        self.assertIn("scheduler", out)
        self.assertIn("recovers cleanly", out)
        self.assertNotIn("rm -rf", out)

    def test_status_glyphs_speak_verdicts(self):
        # Diagnostic readouts say the verdict, never the glyph name.
        out = self.f.filter(
            "Self-diagnostic complete.\n"
            "✅ GPU detected\n"
            "✔️ Models verified\n"
            "❌ ComfyUI backend\n"
            "✗ Audio pipeline\n"
            "- [x] config loaded\n"
            "- [ ] queued task\n"
            "✅ memory bus\n"
            "- [ ] thermal probe\n")
        # Items that already state the result are read as-is — no
        # redundant verdict bolted on.
        self.assertIn("GPU detected", out)
        self.assertIn("Models verified", out)
        self.assertIn("config loaded", out)
        self.assertIn("queued task", out)
        self.assertNotIn("GPU detected —", out)
        self.assertNotIn("Models verified —", out)
        self.assertNotIn("config loaded —", out)
        self.assertNotIn("queued task —", out)
        # Opaque items get the verdict appended.
        self.assertIn("ComfyUI backend — failed to initialize", out)
        self.assertIn("Audio pipeline — failed to initialize", out)
        self.assertIn("memory bus — operating within normal parameters", out)
        self.assertIn("thermal probe — pending", out)
        for glyph in ("✅", "✔", "❌", "✗"):
            self.assertNotIn(glyph, out)

    def test_inline_glyph_verdict(self):
        out = self.f.filter("Backend status: ✅ and tools: ❌ done.")
        self.assertIn("passed", out)
        self.assertIn("failed", out)
        self.assertNotIn("✅", out)
        self.assertNotIn("❌", out)

    def test_inline_glyph_dropped_when_result_already_said(self):
        out = self.f.filter("The backend is healthy ✅, all good.")
        self.assertIn("healthy", out)
        self.assertNotIn("passed", out)
        self.assertNotIn("✅", out)

    def test_stack_trace_skipped(self):
        out = self.f.filter(
            "It crashed:\nTraceback (most recent call last):\n"
            '  File "app.py", line 3, in main\nDone digging.')
        self.assertNotIn("Traceback", out)
        self.assertNotIn("File", out)
        self.assertIn("Done digging", out)

    def test_long_list_runs_summarized_not_dictated(self):
        # Recipe/structured answers: prose intro is voiced, the ingredient
        # and step lists are not read aloud line by line.
        text = (
            "Heh — yeah, absolutely. Seafood gumbo is one of my favorites.\n\n"
            "**Ingredients**\n"
            "- 2 tablespoons olive oil\n"
            "- 1 large onion, diced\n"
            "- 1 lb shrimp, peeled\n"
            "- 1 lb crab meat\n\n"
            "**Steps**\n"
            "1. Sauté the aromatics for five minutes\n"
            "2. Build the roux and stir constantly\n"
            "3. Add the seafood and simmer\n"
            "4. Serve hot with rice\n\n"
            "Want me to tweak it for a gluten-free version?")
        out = self.f.filter(text)
        self.assertIn("one of my favorites", out)
        self.assertIn("gluten-free", out)
        self.assertNotIn("olive oil", out)
        self.assertNotIn("Sauté", out)
        self.assertIn("listed", out.lower())

    def test_short_lists_still_speak(self):
        out = self.f.filter(
            "Two things stand out:\n"
            "- The scheduler fix worked\n"
            "- Memory pressure is gone\n"
            "That's the story.")
        self.assertIn("scheduler fix", out)
        self.assertIn("Memory pressure", out)
        self.assertIn("the story", out)


# --------------------------------------------------------------------------
# streaming segmentation

class TestSentenceStreamer(unittest.TestCase):
    def test_sentences_in_order(self):
        s = SentenceStreamer()
        got = []
        got += s.feed("First sentence.")
        got += s.feed(" Second one here.")
        got += s.feed(" Third…")
        got += s.flush()
        self.assertEqual(got, ["First sentence.", "Second one here.", "Third…"])

    def test_never_emits_half_words(self):
        s = SentenceStreamer()
        self.assertEqual(s.feed("The analy"), [])
        self.assertEqual(s.feed("sis is don"), [])
        self.assertEqual(s.feed("e. Next part"), ["The analysis is done."])

    def test_holds_code_until_fence_closes(self):
        s = SentenceStreamer()
        out = s.feed("Prose first.\n```python\nx = 1\n```\nThen prose again.")
        joined = " ".join(out + s.flush())
        self.assertNotIn("x = 1", joined)
        self.assertIn("Prose first.", joined)

    def test_no_duplicates_after_flush(self):
        s = SentenceStreamer()
        s.feed("Hello world.")
        s.flush()
        self.assertEqual(s.flush(), [])

    def test_long_sentence_splits_into_bounded_segments(self):
        # One monolithic sentence becomes one monolithic TTS job whose
        # synthesis latency shows up as dead air — it must be split.
        s = SentenceStreamer()
        words = " ".join(f"word{i}" for i in range(160))  # ~800 chars
        out = s.feed(words + ".")
        out += s.flush()
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(p) <= s.max_clause for p in out))
        self.assertEqual(" ".join(out).replace(" .", "."), words + ".")

    def test_run_on_pending_buffer_eventually_emits(self):
        # No clause break at all: the word-wrap fallback still bounds the
        # pending buffer instead of waiting forever for punctuation.
        s = SentenceStreamer()
        words = " ".join(f"item{i}" for i in range(120))
        out = s.feed(words)
        self.assertGreater(len(out), 0)
        self.assertTrue(all(len(p) <= s.max_clause for p in out))

    def test_streamer_skips_long_list_runs(self):
        s = SentenceStreamer()
        got = s.feed(
            "Here is the recipe.\n\n"
            "- 2 tablespoons olive oil\n"
            "- 1 onion, diced\n"
            "- 1 lb shrimp\n"
            "- 1 lb crab\n\n"
            "Enjoy it.\n")
        got += s.flush()
        joined = " ".join(got)
        self.assertIn("Here is the recipe.", joined)
        self.assertIn("Enjoy it.", joined)
        self.assertNotIn("olive oil", joined)
        self.assertIn("listed below", joined)

    def test_streamer_speaks_short_lists(self):
        s = SentenceStreamer()
        got = s.feed(
            "Two findings.\n"
            "- the scheduler recovered\n"
            "- the cache stayed warm\n"
            "Done.\n")
        got += s.flush()
        joined = " ".join(got)
        self.assertIn("scheduler recovered", joined)
        self.assertIn("cache stayed warm", joined)

    def test_clause_breaks_stay_under_limit(self):
        s = SentenceStreamer()
        long_clause = ", ".join("clause" + str(i) * 8 for i in range(40))
        out = s.feed("Intro short. " + long_clause + " trailing words")
        out += s.flush()
        self.assertTrue(all(len(p) <= s.max_clause for p in out))
        self.assertGreater(len(out), 2)


# --------------------------------------------------------------------------
# presets

class TestPresets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VoicePresetStore(Path(self.tmp.name) / "presets")

    def tearDown(self):
        self.tmp.cleanup()

    def test_official_preset_installed(self):
        p = self.store.get(OFFICIAL_PRESET_ID)
        self.assertIsNotNone(p)
        self.assertTrue(p.official)
        self.assertEqual(p.base_voice, "bf_isabella")
        self.assertEqual(p.engine, "kokoro")
        self.assertEqual(p.language, "en-GB")

    def test_preset_roundtrip(self):
        p = self.store.get(OFFICIAL_PRESET_ID)
        clone = VoicePreset.from_dict(p.as_dict())
        self.assertEqual(clone.as_dict(), p.as_dict())

    def test_metadata_fields(self):
        p = self.store.get(OFFICIAL_PRESET_ID)
        for f in ("provenance", "version", "cadence", "energy", "warmth",
                  "formality", "emotion_range", "pronunciation_overrides"):
            self.assertIn(f, p.as_dict())
        self.assertIn("kokoro:bf_isabella", p.provenance)
        self.assertIsInstance(p.pronunciation_overrides, dict)

    def test_metadata_survives_roundtrip_and_unknown_keys_tolerated(self):
        raw = VoicePreset(id="x", name="x").as_dict()
        raw["cadence"] = "brisk"
        raw["pronunciation_overrides"] = {"UI": "you eye"}
        raw["future_unknown_field"] = 42  # forward-compat tolerance
        p = VoicePreset.from_dict(raw)
        self.assertEqual(p.cadence, "brisk")
        self.assertEqual(p.pronunciation_overrides.get("UI"), "you eye")

    def test_official_refreshes_on_boot(self):
        # Officials are read-only source of truth — a stale/modified
        # installed copy must be overwritten by the shipped JSON.
        dest = self.store._official_path(OFFICIAL_PRESET_ID)
        dest.write_text('{"id": "stale"}', encoding="utf-8")
        VoicePresetStore(Path(self.tmp.name) / "presets")
        self.assertIn("kokoro", dest.read_text(encoding="utf-8"))

    def test_official_preset_protected(self):
        p = self.store.get(OFFICIAL_PRESET_ID)
        p.name = "Hacked"
        with self.assertRaises(PresetError):
            self.store.save(p)
        with self.assertRaises(PresetError):
            self.store.delete(OFFICIAL_PRESET_ID)

    def test_save_as_and_duplicate(self):
        p = self.store.get(OFFICIAL_PRESET_ID)
        custom = self.store.save_as(p, new_name="Warm Assistant")
        self.assertNotEqual(custom.id, p.id)
        self.assertFalse(custom.official)
        self.assertEqual(self.store.get(custom.id).name, "Warm Assistant")
        dup = self.store.duplicate(custom.id)
        self.assertNotEqual(dup.id, custom.id)

    def test_rename_delete_custom(self):
        p = self.store.save_as(self.store.get(OFFICIAL_PRESET_ID),
                               new_name="X")
        self.store.rename(p.id, "Renamed Voice")
        self.assertEqual(self.store.get(p.id).name, "Renamed Voice")
        self.assertTrue(self.store.delete(p.id))
        self.assertIsNone(self.store.get(p.id))

    def test_import_export(self):
        src = self.store.get(OFFICIAL_PRESET_ID)
        text = self.store.export_json(src.id)
        p = self.store.import_json(text)
        self.assertNotEqual(p.id, src.id)
        self.assertEqual(p.base_voice, "bf_isabella")

    def test_import_rejects_invalid(self):
        with self.assertRaises(PresetError):
            self.store.import_json("{not json")
        with self.assertRaises(PresetError):
            self.store.import_json('"just a string"')

    def test_corrupt_preset_quarantined(self):
        bad = self.store.dir / "broken.json"
        bad.write_text("{corrupt", encoding="utf-8")
        self.assertIsNone(self.store.get("broken"))
        self.assertTrue(list(self.store.dir.glob("broken.json.corrupt-*")))

    def test_other_base_voices(self):
        p = VoicePreset(id="emma-warm", name="Warm Emma",
                        base_voice="bf_emma", pitch_semitones=-1.5,
                        synthetic=0.3)
        self.store.save(p)
        self.assertEqual(self.store.get("emma-warm").base_voice, "bf_emma")

    def test_official_preset_safe_ranges(self):
        official = (Path(__file__).parent.parent / "localcodeagent" / "voice" /
                    "official" / f"{OFFICIAL_PRESET_ID}.json")
        p = VoicePreset.from_dict(json.loads(official.read_text()))
        self.assertTrue(0.0 <= p.synthetic <= 1.0)
        self.assertTrue(-12 <= p.pitch_semitones <= 12)
        self.assertTrue(0.5 <= p.tempo <= 2.0)
        self.assertTrue(0.7 <= p.limiter_ceiling <= 0.99)
        for lay in (p.neural, p.glass, p.micro):
            self.assertLess(lay.mix, 0.5)


# --------------------------------------------------------------------------
# cache

class TestAudioCache(unittest.TestCase):
    def test_key_changes_with_preset(self):
        k1 = AudioCache.key("hello", "kokoro", "v1", "bf_isabella", "p1", 1.0)
        k2 = AudioCache.key("hello", "kokoro", "v1", "bf_isabella", "p2", 1.0)
        k3 = AudioCache.key("hello", "kokoro", "v1", "bf_emma", "p1", 1.0)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_eviction(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = AudioCache(Path(tmp), max_bytes=400)
            for i in range(6):
                c.put(f"k{i}", b"x" * 100)
            self.assertLessEqual(c.stats()["bytes"], 400)
            self.assertLessEqual(c.stats()["entries"], 4)


# --------------------------------------------------------------------------
# manager: queue / mute / cancel (engine mocked)

class _FakeEngine:
    name = "kokoro"
    version = "test"
    sample_rate = 24000

    def __init__(self):
        self.calls = 0

    def synthesize(self, text, *, voice, speed=1.0, lang="en-us"):
        self.calls += 1
        n = int(0.25 * self.sample_rate)
        t = np.linspace(0, 0.25, n)
        return (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), self.sample_rate

    def voices(self):
        return [{"id": "bf_isabella"}]

    def status(self):
        return {"name": "kokoro", "loaded": True}

    def load(self):
        pass


class _Cfg:
    voice_enabled = True
    voice_muted = False
    voice_engine = "kokoro"
    voice_preset_id = OFFICIAL_PRESET_ID
    voice_mode = "responses"
    voice_speed = 1.0
    voice_volume = 1.0

    def save(self):
        pass


@unittest.skipUnless(HAS_NUMPY, "numpy required for DSP/manager tests")
class TestVoiceManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = VoiceManager(_Cfg(), preset_dir=Path(self.tmp.name) / "presets",
                              cache_dir=Path(self.tmp.name) / "cache")
        self.m._engines["kokoro"] = _FakeEngine()

    def tearDown(self):
        self.tmp.cleanup()

    def test_speak_text_produces_segment(self):
        out = self.m.speak_text("Hello, this is a test.")
        self.assertTrue(out["ok"])
        self.assertTrue(self.m.segment_path(out["segment_id"]).exists())
        self.assertGreater(out["seconds"], 0)

    def test_tts_failure_surfaces_only_to_voice(self):
        class Bad:
            name, version = "kokoro", "x"
            def synthesize(self, *a, **k):
                raise RuntimeError("boom")
        self.m._engines["kokoro"] = Bad()
        with self.assertRaises(Exception) as ctx:
            self.m.speak_text("hi")
        self.assertIn("boom", str(ctx.exception))

    def test_pronunciation_overrides_apply_at_synthesis(self):
        """Per-preset token rewrites reach the engine — 'UI' in the
        official preset must arrive as its spoken expansion."""
        captured = []

        class Capture:
            name, version, sample_rate = "kokoro", "x", 24000
            def synthesize(self, text, *, voice, speed=1.0, lang="en-us"):
                captured.append(text)
                return np.zeros(1200, dtype=np.float32), 24000
            def voices(self): return [{"id": "bf_isabella"}]
            def status(self): return {"name": "kokoro", "loaded": True}

        self.m._engines["kokoro"] = Capture()
        self.m.speak_text("Check the UI now.")
        self.assertTrue(captured)
        self.assertIn("you eye", captured[0])
        self.assertNotIn(" UI ", captured[0])

    def test_mute_stops_and_clears_queue(self):
        self.m.enqueue("t1", "first")
        self.m.enqueue("t1", "second")
        self.m.set_muted(True)
        self.assertTrue(self.m.muted())
        self.assertEqual(self.m.status()["queue"], 0)

    def test_new_task_suppresses_old_speech(self):
        self.m.begin_task("t1")
        self.m.enqueue("t1", "old response audio")
        self.m.begin_task("t2")
        self.assertEqual(self.m.status()["queue"], 0)

    def test_muted_enqueue_is_noop(self):
        self.m.set_muted(True)
        self.assertIsNone(self.m.enqueue("t", "text"))
        self.m.set_muted(False)

    def test_enqueue_vocalize_false_preserves_text(self):
        # Persona notice lead-ins like "Oof — …" must survive verbatim:
        # re-resolving them could strip the lead-in as a vocalization.
        job = self.m.enqueue("n1", "Oof — that one fought back.",
                             vocalize=False)
        self.assertEqual(job.text, "Oof — that one fought back.")

    def test_repeat_last_requeues_spoken_text(self):
        self.assertFalse(self.m.repeat_last())  # nothing spoken yet
        published = []
        self.m._publish = lambda kind, payload: published.append(payload)
        self.m.begin_task("t-rep")
        self.m.finish_task("t-rep", "The thing I said.")
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(p.get("event") == "segment" for p in published):
                break
            time.sleep(0.05)
        self.assertTrue(self.m.repeat_last())
        deadline = time.time() + 15
        while time.time() < deadline:
            segs = [p for p in published if p.get("event") == "segment"]
            if len(segs) >= 2:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(
            len([p for p in published if p.get("event") == "segment"]),
            2)

    def test_repeat_last_replays_recorded_segments(self):
        """🔊/repeat must re-serve the exact clips, not re-synthesize —
        the original carried the persona delivery plan (pace/tone) that
        a flat speak_text rebuild would lose."""
        published = []
        self.m._publish = lambda kind, payload: published.append(payload)
        self.m.begin_task("t-rx")
        self.m.finish_task("t-rx", "Same voice, same pace.")
        orig = []
        deadline = time.time() + 15
        while time.time() < deadline:
            orig = [p for p in published if p.get("event") == "segment"]
            if orig:
                break
            time.sleep(0.05)
        self.assertTrue(orig)
        self.assertEqual(self.m.segments_for_task("t-rx"),
                         [p["segment_id"] for p in orig])
        published.clear()
        self.assertTrue(self.m.repeat_last())
        replayed = [p for p in published if p.get("event") == "segment"]
        self.assertEqual([p["segment_id"] for p in replayed],
                         [p["segment_id"] for p in orig])

    def test_segments_for_task_evicted_and_unknown(self):
        self.assertEqual(self.m.segments_for_task("nope"), [])
        seg_path = Path(self.m.cache.dir) / "gone.wav"
        seg_path.write_bytes(b"x")
        sid = self.m._register_segment(seg_path, "t-old")
        self.assertEqual(self.m.segments_for_task("t-old"), [sid])
        self.m.segments.pop(sid)  # simulate bounded-segment eviction
        self.assertEqual(self.m.segments_for_task("t-old"), [])

    def test_queue_synthesizes_and_publishes(self):
        published = []
        self.m._publish = lambda kind, payload: published.append(payload)
        self.m.begin_task("t9")
        self.m.feed_token("t9", "This sentence should be spoken.")
        self.m.finish_task("t9", "This sentence should be spoken.")
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(p.get("event") == "segment" for p in published):
                break
            time.sleep(0.05)
        segs = [p for p in published if p.get("event") == "segment"]
        self.assertTrue(segs, published)
        self.assertEqual(segs[0]["task_id"], "t9")
        self.assertEqual(segs[0]["seq"], 0)
        self.assertTrue(self.m.segment_path(segs[0]["segment_id"]).exists())

    def test_finish_without_tokens_speaks_final_text(self):
        """Responses with no streamed tokens (local/memory/instant answers)
        must still be spoken via the finish_task fallback."""
        published = []
        self.m._publish = lambda kind, payload: published.append(payload)
        self.m.begin_task("t-local")
        self.m.finish_task("t-local", "This answer came from memory.")
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(p.get("event") == "segment" for p in published):
                break
            time.sleep(0.05)
        segs = [p for p in published if p.get("event") == "segment"]
        self.assertTrue(segs, published)
        self.assertEqual(segs[0]["task_id"], "t-local")

    def test_finish_flushes_unterminated_tail(self):
        """Text after the last sentence terminator must not be dropped."""
        published = []
        self.m._publish = lambda kind, payload: published.append(payload)
        self.m.begin_task("t-tail")
        self.m.feed_token("t-tail", "Spoken now. trailing fragment")
        self.m.finish_task("t-tail", "Spoken now. trailing fragment")
        deadline = time.time() + 15
        while time.time() < deadline:
            if len([p for p in published if p.get("event") == "segment"]) >= 2:
                break
            time.sleep(0.05)
        segs = [p for p in published if p.get("event") == "segment"]
        self.assertEqual([s["seq"] for s in segs], [0, 1])

    def test_unterminated_reply_not_double_spoken(self):
        """A reply that emitted zero parts during feed is spoken by the
        flush() tail — the final_text fallback must not speak it again."""
        published = []
        self.m._publish = lambda kind, payload: published.append(payload)
        self.m.begin_task("t-echo")
        self.m.feed_token("t-echo", "Sure thing")  # no terminator → 0 emitted
        self.m.finish_task("t-echo", "Sure thing")
        deadline = time.time() + 15
        while time.time() < deadline:
            if len([p for p in published if p.get("event") == "segment"]) >= 2:
                break
            time.sleep(0.05)
        segs = [p for p in published if p.get("event") == "segment"]
        self.assertEqual(len(segs), 1, segs)

    # -- speech-genome delivery plan -----------------------------------------

    def test_delivery_plan_maps_energy_warmth_emphasis(self):
        """Energy/warmth/emphasis become bounded pitch/gain deltas."""
        preset = self.m.current_preset()
        base = preset.as_dict()
        hot = self.m._delivery_preset(
            preset, {"energy": 1.0, "warmth": 0.0, "emphasis_level": 1.0})
        self.assertGreater(hot.output_gain_db, base["output_gain_db"])
        self.assertGreater(hot.pitch_semitones, base["pitch_semitones"])
        calm = self.m._delivery_preset(
            preset, {"energy": 0.0, "warmth": 1.0, "emphasis_level": 0.0})
        self.assertLess(calm.output_gain_db, base["output_gain_db"])
        self.assertLess(calm.pitch_semitones, base["pitch_semitones"])
        # Bounds: extreme raw inputs clamp, never runaway deltas.
        wild = self.m._delivery_preset(
            preset, {"energy": 99.0, "warmth": -4.0,
                     "emphasis_level": 99.0})
        self.assertLessEqual(
            wild.output_gain_db - base["output_gain_db"], 3.0)
        self.assertLessEqual(
            wild.pitch_semitones - base["pitch_semitones"], 1.0)

    def test_delivery_plan_neutral_and_malformed_leave_preset(self):
        """Neutral-0.5, missing, or garbage values keep the saved voice."""
        preset = self.m.current_preset()
        self.assertIs(self.m._delivery_preset(preset, {}), preset)
        self.assertIs(self.m._delivery_preset(preset, None), preset)
        for junk in ({"energy": 0.5, "warmth": 0.5, "emphasis_level": 0.0},
                     {"energy": "loud"}, {"energy": None}):
            self.assertIs(self.m._delivery_preset(preset, junk), preset)

    def test_finish_task_threads_delivery_into_jobs(self):
        """The plan from the response lane lands on the queued job."""
        job = self.m.enqueue("t-del", "spoken words",
                             delivery={"energy": 0.9})
        self.assertIsNotNone(job)
        self.assertEqual(job.delivery["energy"], 0.9)

    def test_delivery_plan_reaches_synthesis_preset(self):
        """speak_text(delivery=...) reshapes the preset handed to DSP —
        proves the genome characteristics ride real engine controls."""
        captured = []
        orig = dsp.process
        def spy(audio, sr, preset):
            captured.append(preset)
            return orig(audio, sr, preset)
        dsp.process = spy
        try:
            self.m.speak_text("Feel the shift.",
                              delivery={"energy": 1.0, "warmth": 0.0})
        finally:
            dsp.process = orig
        self.assertTrue(captured)
        base = self.m.current_preset()
        self.assertGreater(captured[0].output_gain_db,
                           base.output_gain_db)
        self.assertGreater(captured[0].pitch_semitones,
                           base.pitch_semitones)

    def test_no_speech_when_disabled(self):
        self.m.config.voice_enabled = False
        self.m.begin_task("t")
        self.assertEqual(self.m.feed_token("t", "Hello there."), 0)
        self.m.finish_task("t", "Hello there.")
        self.assertEqual(self.m.status()["queue"], 0)
        self.m.config.voice_enabled = True

    def test_speak_greeting_returns_segment_and_publishes(self):
        """Synchronous greeting path — the caller needs the URL in hand;
        relying on the ephemeral bus segment alone let boot-time
        greetings fire before the page's voice subscription attached."""
        published = []
        self.m._publish = lambda kind, payload: published.append(payload)
        out = self.m.speak_greeting("greet-p1", "Welcome back.")
        self.assertTrue(out["ok"])
        self.assertTrue(out["url"].endswith(out["segment_id"]))
        self.assertTrue(self.m.segment_path(out["segment_id"]).exists())
        segs = [p for p in published if p.get("event") == "segment"]
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["segment_id"], out["segment_id"])
        self.assertEqual(segs[0]["task_id"], "greet-p1")
        # Same segment id on both paths → client-side dedupe is a noop.
        self.assertGreater(self.m._greeting_hold_until, 0)

    def test_speak_greeting_publish_false_silences_bus(self):
        """The startup prefetch asks for the wav without the bus segment —
        publishing it early would play over splash narration. The page
        holds the returned URL itself until the host's transition gate."""
        published = []
        self.m._publish = lambda kind, payload: published.append(payload)
        out = self.m.speak_greeting("greet-p1", "Welcome back.",
                                    publish=False)
        self.assertTrue(out["ok"])
        self.assertTrue(out["url"].endswith(out["segment_id"]))
        self.assertTrue(self.m.segment_path(out["segment_id"]).exists())
        self.assertFalse(
            [p for p in published if p.get("event") == "segment"])

    def test_speak_greeting_respects_mute(self):
        self.m.set_muted(True)
        self.assertIsNone(self.m.speak_greeting("greet-p1", "hi"))

    def test_voice_tool_registration(self):
        from localcodeagent.voice.tools import register_voice_tools
        from localcodeagent.tools.base import ToolRegistry
        reg = ToolRegistry({"audio.generate": "allow", "audio.read": "allow",
                            "audio.manage": "allow"})
        register_voice_tools(reg, self.m)
        names = [s.name for s in reg._tools.values()]
        for expected in ("voice_synthesize", "voice_list", "voice_preview",
                         "voice_preset_list", "voice_preset_get",
                         "voice_preset_save", "voice_preset_duplicate",
                         "voice_preset_delete", "voice_preset_import",
                         "voice_preset_export", "voice_status"):
            self.assertIn(expected, names)
        perm, _eff = reg.permission_for("voice_synthesize")
        self.assertEqual(perm, "audio.generate")
        perm, _eff = reg.permission_for("voice_preset_save")
        self.assertEqual(perm, "audio.manage")


# --------------------------------------------------------------------------
# DSP sanity

@unittest.skipUnless(HAS_NUMPY, "numpy required for DSP tests")
class TestDSP(unittest.TestCase):
    def test_chain_bounds_and_shape(self):
        sr = 24000
        t = np.linspace(0, 1.0, sr)
        x = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        p = VoicePreset(id="t", name="t", pitch_semitones=1.25, tempo=0.98,
                        synthetic=0.8)
        out = dsp.process(x, sr, p)
        self.assertEqual(out.ndim, 2)
        self.assertEqual(out.shape[1], 2)
        self.assertLessEqual(np.abs(out).max(), p.limiter_ceiling + 1e-6)
        self.assertAlmostEqual(out.shape[0] / sr, 1.0 / 0.98, delta=0.15)

    def test_synthetic_zero_near_raw(self):
        sr = 24000
        t = np.linspace(0, 0.5, sr // 2)
        x = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        p = VoicePreset(id="t", name="t", pitch_semitones=0.0, tempo=1.0,
                        synthetic=0.0)
        out = dsp.process_mono(x, sr, p)
        corr = np.corrcoef(out[: x.size], x)[0, 1]
        self.assertGreater(corr, 0.95)

    def test_synthetic_differs(self):
        sr = 24000
        t = np.linspace(0, 1.0, sr)
        x = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        p = VoicePreset(id="t", name="t", synthetic=0.8, pitch_semitones=1.25)
        raw = dsp.process_mono(x, sr, VoicePreset(id="r", name="r", synthetic=0.0))
        proc = dsp.process_mono(x, sr, p)
        self.assertFalse(np.allclose(raw, proc, atol=1e-3))

    def test_mono_compatibility(self):
        sr = 24000
        t = np.linspace(0, 1.0, sr)
        x = (0.3 * np.sin(2 * np.pi * 200 * t) +
             0.1 * np.sin(2 * np.pi * 3000 * t)).astype(np.float32)
        p = VoicePreset(id="t", name="t", synthetic=0.8, stereo_width=1.5)
        stereo = dsp.process(x, sr, p)
        mono = stereo.mean(axis=1)
        self.assertGreater(mono.std(), 0.001)
        self.assertLessEqual(np.abs(mono).max(), 1.0)

    def test_wav_bytes_roundtrip(self):
        import io
        import wave
        sr = 24000
        x = np.zeros((sr // 10, 2), dtype=np.float32)
        data = dsp.wav_bytes(x, sr)
        with wave.open(io.BytesIO(data), "rb") as w:
            self.assertEqual(w.getnchannels(), 2)
            self.assertEqual(w.getframerate(), sr)

    def test_trim_tail_artifact_reattaches_isolated_blip(self):
        # Speech + silence + a short stray-consonant island at the end —
        # the Kokoro detached-phoneme artifact heard as a trailing
        # "t"/"d" after a beat. The island is spliced back onto the word,
        # not cut — it may be the word's real final consonant.
        sr = 24000
        rng = np.random.default_rng(0)
        t = np.arange(sr) / sr
        speech = (0.3 * np.sin(2 * np.pi * 150 * t)
                  + 0.05 * rng.standard_normal(sr))
        gap = np.zeros(int(sr * 0.10))
        blip = 0.2 * np.sin(2 * np.pi * 800 * np.arange(int(sr * 0.06)) / sr)
        tail = np.zeros(int(sr * 0.05))
        x = np.concatenate([speech, gap, blip, tail]).astype(np.float32)
        y = dsp.trim_tail_artifact(x, sr)
        # Gap removed (~speech + pad + blip + tail), island kept.
        self.assertLessEqual(y.size, int(sr * 1.15))
        self.assertGreaterEqual(y.size, int(sr * 1.0))
        # The island now attaches to the speech — no deep gap remains
        # before the final voiced run.
        frame = int(sr * 0.01)
        env = np.sqrt((y[: y.size // frame * frame]
                       .reshape(-1, frame) ** 2).mean(axis=1))
        peak = float(env.max())
        voiced = env > peak * 10 ** (-42 / 20)
        last = len(voiced) - 1 - int(voiced[::-1].argmax())
        run_start = last
        while run_start > 0 and voiced[run_start - 1]:
            run_start -= 1
        run_peak = float(env[run_start:last + 1].max())
        g = run_start
        while g > 0 and env[g - 1] <= run_peak * 10 ** (-10 / 20):
            g -= 1
        self.assertLess((run_start - g) * 10, 40)

    def test_trim_tail_artifact_leaves_speech_alone(self):
        sr = 24000
        n = int(sr * 1.5)
        t = np.arange(n) / sr
        # Natural decaying tail — no silence gap + isolated blip.
        x = (0.3 * np.sin(2 * np.pi * 150 * t)
             * np.linspace(1.0, 0.05, n)).astype(np.float32)
        y = dsp.trim_tail_artifact(x, sr)
        self.assertEqual(y.size, x.size)
        # A long voiced tail is speech, not a blip.
        speech = 0.3 * np.sin(2 * np.pi * 150 * np.arange(sr) / sr)
        voiced_tail = 0.25 * np.sin(2 * np.pi * 200
                                    * np.arange(int(sr * 0.3)) / sr)
        x2 = np.concatenate([speech, voiced_tail]).astype(np.float32)
        self.assertEqual(dsp.trim_tail_artifact(x2, sr).size, x2.size)

    def test_trim_tail_artifact_shaves_noise_bed(self):
        # Kokoro leaves a breathy noise bed decaying for hundreds of ms
        # after the real last phoneme — audible as a trailing hiss.
        # Everything after the last -22 dB run is sub-consonant level,
        # so a long residue is shaved to a short decay.
        sr = 24000
        rng = np.random.default_rng(0)
        speech = 0.3 * np.sin(2 * np.pi * 150 * np.arange(sr) / sr)
        hiss = (0.006 * rng.standard_normal(int(sr * 0.4))
                * np.linspace(1.0, 0.3, int(sr * 0.4)))
        tail = np.zeros(int(sr * 0.3))
        x = np.concatenate([speech, hiss.astype(np.float32), tail])
        y = dsp.trim_tail_artifact(x, sr)
        # Noise bed + dead air gone; kept ~speech + short decay.
        self.assertLessEqual(y.size, int(sr * 1.15))
        self.assertGreaterEqual(y.size, int(sr * 1.0))

    def test_trim_tail_artifact_keeps_natural_release(self):
        # A short natural release decay (< noise_tail_ms) after the last
        # strong run is speech, not a noise bed — leave it alone.
        sr = 24000
        rng = np.random.default_rng(0)
        speech = 0.3 * np.sin(2 * np.pi * 150 * np.arange(sr) / sr)
        release = (0.01 * rng.standard_normal(int(sr * 0.08))
                   * np.linspace(1.0, 0.2, int(sr * 0.08)))
        x = np.concatenate([speech, release.astype(np.float32)])
        y = dsp.trim_tail_artifact(x, sr)
        self.assertEqual(y.size, x.size)


# --------------------------------------------------------------------------
# speech-to-text configuration/provisioning contract

class TestSpeechToTextConfig(unittest.TestCase):
    def test_stt_settings_load_and_voice_extra_is_pinned(self):
        from localcodeagent.config import AgentConfig, load_config

        defaults = AgentConfig()
        self.assertEqual(defaults.stt_backend, "auto")
        self.assertEqual(defaults.stt_model, "base")
        self.assertEqual(defaults.vosk_model_path, "")
        self.assertFalse(defaults.stt_auto_submit)

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(json.dumps({
                "stt_backend": "faster-whisper",
                "stt_model": "small",
                "vosk_model_path": "models/vosk/en-us",
                "stt_auto_submit": True,
            }), encoding="utf-8")
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.stt_backend, "faster-whisper")
            self.assertEqual(cfg.stt_model, "small")
            self.assertEqual(cfg.vosk_model_path, "models/vosk/en-us")
            self.assertTrue(cfg.stt_auto_submit)

        import tomllib
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))[
            "project"]["optional-dependencies"]
        self.assertIn("sounddevice==0.5.6", extras["voice-stt"])
        self.assertIn("faster-whisper==1.2.1", extras["voice-stt"])
        self.assertIn("av==18.1.0", extras["voice-stt"])

    def test_server_stt_backend_selection_and_relative_vosk_path(self):
        from unittest import mock
        from localcodeagent.config import AgentConfig
        from localcodeagent.server import AppState
        from localcodeagent.voice import stt as stt_module

        state = object.__new__(AppState)
        state._stt_tried = False
        state._stt_engine = None
        with tempfile.TemporaryDirectory() as td:
            state.runtime_root = Path(td)
            state.config = AgentConfig(
                stt_backend="vosk",
                vosk_model_path="models/vosk/en-us")
            seen = []

            class FakeVosk:
                name = "vosk"
                streaming = True

                def __init__(self, model_path):
                    seen.append(Path(model_path))

                def available(self):
                    return True

            with mock.patch.object(stt_module, "VoskEngine", FakeVosk):
                eng = AppState.stt_engine(state)
            self.assertIsInstance(eng, FakeVosk)
            self.assertEqual(seen, [state.runtime_root / "models/vosk/en-us"])

            state._stt_tried = False
            state._stt_engine = None
            state.config.stt_backend = "off"
            self.assertIsNone(AppState.stt_engine(state))


# --------------------------------------------------------------------------
# loudness (BS.1770 gain stage)

@unittest.skipUnless(HAS_NUMPY, "numpy required")
class TestLoudness(unittest.TestCase):
    def _sine(self, amp=0.1, secs=1.0, sr=24000, freq=220.0):
        t = np.arange(int(sr * secs)) / sr
        return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32), sr

    def test_integrated_lufs_sane_for_sine(self):
        from localcodeagent.voice import loudness
        x, sr = self._sine(0.1)
        lufs = loudness.integrated_lufs(x, sr)
        # 0.1 sine ≈ -23 dBFS RMS → K-weighted lands near there, not inf.
        self.assertTrue(np.isfinite(lufs))
        self.assertGreater(lufs, -30.0)
        self.assertLess(lufs, -10.0)

    def test_silence_is_negative_infinite(self):
        from localcodeagent.voice import loudness
        x, sr = self._sine(0.0)
        self.assertEqual(loudness.integrated_lufs(x, sr), -np.inf)

    def test_normalize_hits_target(self):
        from localcodeagent.voice import loudness
        x, sr = self._sine(0.05)
        y, gain = loudness.normalize_lufs(x, sr, -17.0)
        post = loudness.integrated_lufs(y, sr)
        self.assertAlmostEqual(post, -17.0, delta=0.5)
        self.assertGreater(gain, 0.0)

    def test_normalize_caps_gain(self):
        from localcodeagent.voice import loudness
        x, sr = self._sine(0.001)
        _, gain = loudness.normalize_lufs(x, sr, -17.0, max_gain_db=12.0)
        self.assertEqual(gain, 12.0)

    def test_normalize_silence_passthrough(self):
        from localcodeagent.voice import loudness
        x, sr = self._sine(0.0)
        y, gain = loudness.normalize_lufs(x, sr, -17.0)
        self.assertEqual(gain, 0.0)
        self.assertTrue(np.array_equal(x, y))

    def test_stereo_measurement(self):
        from localcodeagent.voice import loudness
        x, sr = self._sine(0.1)
        st = np.stack([x, x], axis=1)
        lufs = loudness.integrated_lufs(st, sr)
        self.assertTrue(np.isfinite(lufs))

    def test_short_clip_no_crash(self):
        from localcodeagent.voice import loudness
        x, sr = self._sine(0.1, secs=0.05)
        loudness.integrated_lufs(x, sr)   # must not raise
        loudness.normalize_lufs(x, sr, -17.0)

    def test_process_respects_normalize_flag(self):
        from localcodeagent.voice import loudness
        x, sr = self._sine(0.05)
        p = VoicePreset(id="t", name="t", synthetic=0.0,
                        normalize_loudness=True,
                        loudness_target_lufs=-17.0)
        out = dsp.process(x, sr, p)
        lufs = loudness.integrated_lufs(out, sr)
        self.assertAlmostEqual(lufs, -17.0, delta=1.5)
        self.assertLessEqual(np.abs(out).max(), p.limiter_ceiling + 1e-6)

    def test_process_normalize_off_by_default(self):
        x, sr = self._sine(0.05)
        p = VoicePreset(id="t", name="t", synthetic=0.0)
        out = dsp.process(x, sr, p)
        raw_peak = np.abs(x).max()
        self.assertAlmostEqual(np.abs(out).max(), raw_peak, delta=0.05)

    def test_limiter_flag_gates_invocation(self):
        x, sr = self._sine(0.5)
        calls = []
        orig = dsp.limiter
        dsp.limiter = lambda *a, **k: (calls.append(1), a[0])[1]
        try:
            p = VoicePreset(id="t", name="t", synthetic=0.0,
                            limiter_enabled=True)
            dsp.process(x, sr, p)
            self.assertEqual(len(calls), 2)   # L + R
            calls.clear()
            p2 = VoicePreset(id="t2", name="t2", synthetic=0.0,
                             limiter_enabled=False)
            dsp.process(x, sr, p2)
            self.assertEqual(calls, [])
        finally:
            dsp.limiter = orig


# --------------------------------------------------------------------------
# chatterbox vocalization adapter

class TestChatterboxAdapter(unittest.TestCase):
    def setUp(self):
        from localcodeagent.voice.vocalizations import (
            ChatterboxVocalizationAdapter, Vocalization)
        self.V = Vocalization
        self.adapter = ChatterboxVocalizationAdapter()

    def test_gesture_styles_render_native_tags(self):
        for style, tag in (("chuckle", "[chuckle]"), ("laugh", "[laugh]"),
                           ("sigh", "[sigh]"), ("gasp", "[gasp]"),
                           ("groan", "[groan]"), ("sniff", "[sniff]"),
                           ("throat_clear", "[clear throat]"),
                           ("shush", "[shush]")):
            voc = self.V(category="amusement", style=style,
                         intensity=0.5, token=style)
            self.assertEqual(self.adapter.render(voc), tag, style)

    def test_verbal_fillers_stay_text(self):
        voc = self.V(category="thinking", style="hmm",
                     intensity=0.3, token="hmm")
        out = self.adapter.render(voc)
        self.assertEqual(out, "hmm…")
        self.assertNotIn("[", out)

    def test_unsupported_tag_falls_back_to_text(self):
        from localcodeagent.voice.vocalizations import (
            ChatterboxVocalizationAdapter)
        ad = ChatterboxVocalizationAdapter(supported={"laugh"})
        voc = self.V(category="sigh", style="sigh",
                     intensity=0.5, token="sigh")
        out = ad.render(voc)
        self.assertIsNotNone(out)
        self.assertNotEqual(out, "[sigh]")
        # And a supported one still renders as a tag.
        voc2 = self.V(category="amusement", style="laugh",
                      intensity=0.5, token="laugh")
        self.assertEqual(ad.render(voc2), "[laugh]")

    def test_adapter_registry(self):
        from localcodeagent.voice.vocalizations import (
            ChatterboxVocalizationAdapter, KokoroVocalizationAdapter,
            adapter_for)
        self.assertIsInstance(adapter_for("chatterbox"),
                              ChatterboxVocalizationAdapter)
        self.assertIsInstance(adapter_for("kokoro"),
                              KokoroVocalizationAdapter)
        self.assertIsInstance(adapter_for("unknown-engine"),
                              KokoroVocalizationAdapter)


# --------------------------------------------------------------------------
# chatterbox engine (worker mocked / paths only — no torch in tests)

@unittest.skipUnless(HAS_NUMPY, "numpy required")
class TestChatterboxEngine(unittest.TestCase):
    def setUp(self):
        from localcodeagent.voice.chatterbox import ChatterboxEngine
        self.tmp = tempfile.TemporaryDirectory()
        self.assets = Path(self.tmp.name) / "models" / "voice"
        self.runtime = Path(self.tmp.name) / "runtime"
        self.eng = ChatterboxEngine(asset_dir=self.assets,
                                    runtime_dir=self.runtime)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unavailable_without_runtime_and_model(self):
        self.assertFalse(self.eng.available())

    def test_unavailable_runtime_only(self):
        model = self.assets / "chatterbox"
        model.mkdir(parents=True)
        for f in ("ve.safetensors", "t3_turbo_v1.safetensors",
                  "s3gen_meanflow.safetensors", "conds.pt",
                  "tokenizer_config.json", "vocab.json", "merges.txt"):
            (model / f).write_bytes(b"x")
        self.assertFalse(self.eng.available())

    def test_load_raises_when_unavailable(self):
        from localcodeagent.voice.engine import VoiceEngineError
        with self.assertRaises(VoiceEngineError):
            self.eng.load()

    def test_official_isabella_voice_listed(self):
        voices = {v["id"]: v for v in self.eng.voices()}
        self.assertIn("isabella", voices)
        self.assertTrue(voices["isabella"]["installed"])
        self.assertTrue(voices["isabella"]["official"])

    def test_voice_meta_reads_profile(self):
        meta = self.eng.voice_meta("isabella")
        self.assertEqual(meta["engine"], "chatterbox")
        self.assertEqual(meta["reference"], "reference.wav")

    def test_status_shape(self):
        st = self.eng.status()
        self.assertEqual(st["name"], "chatterbox")
        self.assertFalse(st["loaded"])
        self.assertFalse(st["available"])
        self.assertIn("model", st)

    def _wav(self, seconds: float, sr: int = 24000,
             amp: float = 0.3) -> Path:
        import wave as _wave
        t = np.arange(int(seconds * sr), dtype=np.float32) / sr
        pcm = (amp * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
        p = Path(self.tmp.name) / f"src-{seconds}-{amp}.wav"
        with _wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        return p

    def test_import_voice_registers_valid_clip(self):
        out = self.eng.import_voice("My Voice!", self._wav(6.0),
                                    name="My Voice")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["voice"]["id"], "my-voice")
        vdir = self.eng.voices_dir / "my-voice"
        self.assertTrue((vdir / "reference.wav").is_file())
        meta = json.loads((vdir / "voice.json").read_text())
        self.assertEqual(meta["name"], "My Voice")
        self.assertEqual(meta["reference_sha256"],
                         out["voice"]["reference_sha256"])
        # imported voice shows up in the listing, shadowing-capable
        voices = {v["id"]: v for v in self.eng.voices()}
        self.assertTrue(voices["my-voice"]["installed"])
        self.assertFalse(voices["my-voice"]["official"])

    def test_import_voice_rejects_short_clip(self):
        out = self.eng.import_voice("tiny", self._wav(2.0))
        self.assertFalse(out["ok"])
        self.assertIn("validation", out["error"])
        self.assertFalse((self.eng.voices_dir / "tiny").exists())

    def test_import_voice_refuses_clobber_without_overwrite(self):
        self.assertTrue(
            self.eng.import_voice("dup", self._wav(6.0))["ok"])
        out = self.eng.import_voice("dup", self._wav(6.0))
        self.assertFalse(out["ok"])
        self.assertTrue(self.eng.import_voice(
            "dup", self._wav(6.0), overwrite=True)["ok"])

    def test_import_voice_bad_inputs(self):
        self.assertFalse(self.eng.import_voice("!!!", self._wav(6.0))["ok"])
        self.assertFalse(self.eng.import_voice(
            "x", Path(self.tmp.name) / "missing.wav")["ok"])
        self.assertFalse(self.eng.import_voice("x", "")["ok"])

    def test_status_poll_does_not_extend_idle_lease(self):
        """Introspection must not bump _last_used — otherwise any UI
        status poll resets the idle-unload timer and pins ~2 GB of VRAM
        forever (observed: status() ran a worker request that refreshed
        the lease on every call)."""
        calls = []
        eng = self.eng
        eng._loaded = True
        eng._proc = types.SimpleNamespace(poll=lambda: None)
        eng._last_used = 1000.0

        def rec(payload, timeout, **kw):
            calls.append((payload.get("cmd"), kw.get("touch", False)))
            return {"ok": True, "device": "cuda", "sr": 24000}
        eng._request = rec

        eng.status()
        self.assertIn(("status", False), calls)
        self.assertEqual(eng._last_used, 1000.0)

    def test_manager_falls_back_to_kokoro(self):
        """A chatterbox preset whose engine fails must still speak —
        the kokoro engine renders it and the event is published."""
        from localcodeagent.voice.engine import VoiceEngineError
        published = []
        m = VoiceManager(_Cfg(), preset_dir=Path(self.tmp.name) / "pres",
                         cache_dir=Path(self.tmp.name) / "cache",
                         publish=lambda k, p: published.append(p))

        class DeadChatterbox:
            name, version, sample_rate = "chatterbox", "x", 24000
            def synthesize(self, *a, **k):
                raise VoiceEngineError("worker not installed")
            def voices(self): return []
            def status(self): return {"name": "chatterbox", "loaded": False}

        m._engines["chatterbox"] = DeadChatterbox()
        m._engines["kokoro"] = _FakeEngine()
        preset = VoicePreset(id="cb", name="cb", engine="chatterbox",
                             base_voice="isabella")
        m.presets.save(preset)
        pcm, sr, path = m._synthesize("hello", preset, 1.0)
        self.assertGreater(pcm.size, 0)
        self.assertTrue(any(e.get("event") == "engine_fallback"
                            for e in published))

    def test_fallback_maps_unknown_voice_to_kokoro_voice(self):
        """The chatterbox preset's voice id isn't a Kokoro voice — the
        fallback must map to a real Kokoro voice (bf_isabella, the
        approved reference's own source) instead of passing the foreign
        id through and failing a second time."""
        from localcodeagent.voice.engine import VoiceEngineError
        m = VoiceManager(_Cfg(), preset_dir=Path(self.tmp.name) / "pres",
                         cache_dir=Path(self.tmp.name) / "cache",
                         publish=lambda k, p: None)

        class DeadChatterbox:
            name, version, sample_rate = "chatterbox", "x", 24000
            def synthesize(self, *a, **k):
                raise VoiceEngineError("worker not installed")
            def voices(self): return []
            def status(self): return {"name": "chatterbox", "loaded": False}

        seen = {}

        class KokoroSpy(_FakeEngine):
            def synthesize(self, text, *, voice=None, speed=1.0, lang="en"):
                seen["voice"] = voice
                return super().synthesize(text, voice=voice, speed=speed,
                                        lang=lang)
            def voices(self):
                return [{"id": "af_heart"}, {"id": "bf_isabella"}]

        m._engines["chatterbox"] = DeadChatterbox()
        m._engines["kokoro"] = KokoroSpy()
        preset = VoicePreset(id="cb", name="cb", engine="chatterbox",
                             base_voice="isabella")
        m.presets.save(preset)
        pcm, sr, path = m._synthesize("hello", preset, 1.0)
        self.assertGreater(pcm.size, 0)
        self.assertEqual(seen["voice"], "bf_isabella")

    def test_cache_keys_separate_engines(self):
        """Same text under kokoro vs chatterbox must never share a cache
        entry — the engine name + version fold into the key."""
        from localcodeagent.voice.cache import AudioCache
        k1 = AudioCache.key("hello", "kokoro", "v1|dsp1", "bf_isabella",
                            "ph", 1.0)
        k2 = AudioCache.key("hello", "chatterbox", "v1|dsp1", "isabella",
                            "ph", 1.0)
        k3 = AudioCache.key("hello", "chatterbox", "v2|dsp1", "isabella",
                            "ph", 1.0)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k2, k3)


# --------------------------------------------------------------------------
# reference audio validation

@unittest.skipUnless(HAS_NUMPY, "numpy required")
class TestReferenceValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _wav(self, name, secs, amp=0.3, sr=24000, clip=False):
        import wave
        t = np.arange(int(sr * secs)) / sr
        x = np.sin(2 * np.pi * 220 * t) * amp
        if clip:
            x = np.clip(x * 5.0, -1.0, 1.0)
        path = self.dir / name
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((x * 32767).astype(np.int16).tobytes())
        return path

    def test_good_reference_passes(self):
        from localcodeagent.voice.reference import validate_reference
        rep = validate_reference(self._wav("ok.wav", 8.0))
        self.assertTrue(rep.ok, rep.errors)
        self.assertAlmostEqual(rep.duration_s, 8.0, delta=0.05)

    def test_short_reference_rejected(self):
        from localcodeagent.voice.reference import validate_reference
        rep = validate_reference(self._wav("short.wav", 2.0))
        self.assertFalse(rep.ok)
        self.assertTrue(any("short" in e for e in rep.errors))

    def test_clipped_reference_rejected(self):
        from localcodeagent.voice.reference import validate_reference
        rep = validate_reference(self._wav("clip.wav", 8.0, clip=True))
        self.assertFalse(rep.ok)
        self.assertTrue(any("clip" in e for e in rep.errors))

    def test_silent_reference_rejected(self):
        from localcodeagent.voice.reference import validate_reference
        rep = validate_reference(self._wav("sil.wav", 8.0, amp=0.0))
        self.assertFalse(rep.ok)

    def test_missing_file_reported(self):
        from localcodeagent.voice.reference import validate_reference
        rep = validate_reference(self.dir / "nope.wav")
        self.assertFalse(rep.ok)
        self.assertTrue(any("not found" in e for e in rep.errors))

    def test_non_wav_container_rejected(self):
        from localcodeagent.voice.reference import validate_reference
        p = self.dir / "ref.mp3"
        p.write_bytes(b"not audio")
        rep = validate_reference(p)
        self.assertFalse(rep.ok)
        self.assertTrue(any("container" in e for e in rep.errors))

    def test_prepare_reference_caches_and_preserves_original(self):
        from localcodeagent.voice.reference import (
            prepare_reference, validate_reference)
        src = self._wav("src.wav", 8.0, sr=48000)
        before = src.read_bytes()
        dest, rep = prepare_reference(src, self.dir / "refcache")
        self.assertTrue(dest.exists())
        self.assertEqual(src.read_bytes(), before)
        import wave
        with wave.open(str(dest), "rb") as w:
            self.assertEqual(w.getframerate(), 24000)
        # Idempotent — second call reuses the same cached file.
        dest2, _ = prepare_reference(src, self.dir / "refcache")
        self.assertEqual(dest, dest2)


# --------------------------------------------------------------------------
# chatterbox assets / provisioning

class TestChatterboxAssets(unittest.TestCase):
    def test_model_status_reports_missing(self):
        from localcodeagent.voice.chatterbox_assets import model_status
        with tempfile.TemporaryDirectory() as td:
            st = model_status(Path(td))
            self.assertEqual(len(st), 9)
            self.assertFalse(any(e["present"] for e in st.values()))

    def test_model_status_detects_present_unverified(self):
        from localcodeagent.voice.chatterbox_assets import (
            model_ready, model_status)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "conds.pt").write_bytes(b"fake")
            st = model_status(d)
            self.assertTrue(st["conds.pt"]["present"])
            self.assertFalse(st["conds.pt"]["verified"])
            self.assertFalse(model_ready(d))

    def test_runtime_status_missing(self):
        from localcodeagent.voice.chatterbox_runtime import runtime_status
        with tempfile.TemporaryDirectory() as td:
            st = runtime_status(Path(td))
            self.assertFalse(st["python_present"])
            self.assertFalse(st["verified"])

    def test_provisioning_plan_includes_chatterbox(self):
        from localcodeagent.config import AgentConfig
        from localcodeagent.provisioning import ProvisioningManager
        with tempfile.TemporaryDirectory() as td:
            pm = ProvisioningManager(Path(td), AgentConfig())
            kinds = {it.id: it.kind for it in pm._items.values()}
            self.assertEqual(kinds.get("chatterbox-runtime"),
                             "chatterbox_runtime")
            self.assertEqual(kinds.get("chatterbox-model"),
                             "chatterbox_model")


# --------------------------------------------------------------------------
# chatterbox config fields

class TestChatterboxConfig(unittest.TestCase):
    def test_defaults(self):
        from localcodeagent.config import AgentConfig
        c = AgentConfig()
        self.assertEqual(c.voice_chatterbox_runtime_dir,
                         "runtime/voice/chatterbox")
        self.assertEqual(c.voice_chatterbox_device, "auto")
        self.assertTrue(c.voice_normalize_loudness)
        self.assertTrue(c.voice_limiter_enabled)
        self.assertAlmostEqual(c.voice_target_lufs, -14.0)

    def test_parse_and_clamp(self):
        from localcodeagent.config import load_config
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({
                "voice_chatterbox_runtime_dir": "custom/rt",
                "voice_chatterbox_device": "bogus",
                "voice_chatterbox_min_free_vram_mb": 5000,
                "voice_target_lufs": -99.0,
                "voice_normalize_loudness": False,
            }), encoding="utf-8")
            c = load_config(p)
            self.assertEqual(c.voice_chatterbox_runtime_dir, "custom/rt")
            self.assertEqual(c.voice_chatterbox_device, "auto")  # clamped
            self.assertEqual(c.voice_chatterbox_min_free_vram_mb, 5000.0)
            self.assertEqual(c.voice_target_lufs, -40.0)  # clamped
            self.assertFalse(c.voice_normalize_loudness)


# --------------------------------------------------------------------------
# worker protocol (integration — needs the real runtime; skips without it)

@unittest.skipUnless(HAS_NUMPY, "numpy required")
class TestChatterboxWorkerProtocol(unittest.TestCase):
    """End-to-end JSONL check against the real isolated runtime when it
    exists on this machine — skipped in CI where it won't."""

    def setUp(self):
        from localcodeagent.voice.chatterbox import (
            ChatterboxEngine, _venv_python)
        self.py = _venv_python(Path("runtime/voice/chatterbox"))
        if not self.py.exists():
            self.skipTest("isolated voice runtime not installed")
        self.eng = ChatterboxEngine(
            asset_dir=Path("models/voice"),
            runtime_dir=Path("runtime/voice/chatterbox"))

    def tearDown(self):
        try:
            self.eng.unload()
        except Exception:
            pass

    def test_ping_and_capabilities(self):
        resp = self.eng._request({"cmd": "ping"}, timeout=15)
        self.assertTrue(resp["ok"])
        # capabilities before model load — all tags false, no crash.
        caps = self.eng._request({"cmd": "capabilities"}, timeout=15)
        self.assertTrue(caps["ok"])
        self.assertIn("tags", caps)

    def test_load_and_status(self):
        if not self.eng.available():
            self.skipTest("chatterbox model not provisioned")
        self.eng.load()
        st = self.eng.status()
        self.assertTrue(st["loaded"])
        self.assertIn(st["device"], ("cuda", "cpu"))
        self.assertIn("laugh", st["supported_tags"])


# --------------------------------------------------------------------------
# idle unload covers every registered engine (regression: only kokoro was
# checked — a resident chatterbox worker held ~3 GB VRAM indefinitely)

class TestVoiceIdleUnload(unittest.TestCase):
    """server._unload_idle_voice_engine must release GPU engines too."""

    def _server(self, engines, idle_s=600.0):
        from localcodeagent.server import AppState
        srv = AppState.__new__(AppState)
        srv.config = types.SimpleNamespace(
            voice_idle_unload_seconds=idle_s)
        srv.voice = types.SimpleNamespace(_engines=engines)
        published = []
        srv._voice_publish = lambda p: published.append(p)
        srv._published = published
        return srv

    def _fake_worker_engine(self, *, device="cuda", last_used=None):
        class _Proc:
            def poll(self):
                return None
        eng = types.SimpleNamespace(
            _proc=_Proc(), _loaded=True, _device=device,
            min_free_vram_mb=3200.0,
            _last_used=(time.time() if last_used is None else last_used),
            unload=lambda: setattr(eng, "unloaded", True))
        eng.unloaded = False
        return eng

    def test_chatterbox_idle_unloads(self):
        eng = self._fake_worker_engine(
            last_used=time.time() - 9999)
        srv = self._server({"chatterbox": eng}, idle_s=600.0)
        srv._unload_idle_voice_engine()
        self.assertTrue(eng.unloaded)

    def test_chatterbox_active_stays_loaded(self):
        eng = self._fake_worker_engine()  # just used
        srv = self._server({"chatterbox": eng}, idle_s=600.0)
        srv._unload_idle_voice_engine()
        self.assertFalse(eng.unloaded)

    def test_kokoro_still_unloads(self):
        eng = types.SimpleNamespace(
            _model=object(), _loaded_at=0.0, _last_used=0.0,
            unload=lambda: setattr(eng, "unloaded", True))
        eng.unloaded = False
        srv = self._server({"kokoro": eng}, idle_s=600.0)
        srv._unload_idle_voice_engine()
        self.assertTrue(eng.unloaded)

    def test_vram_pressure_unloads_gpu_engine(self):
        eng = self._fake_worker_engine()  # actively used, but VRAM tight
        srv = self._server({"chatterbox": eng}, idle_s=600.0)
        srv.runtime = types.SimpleNamespace(
            fresh_hardware=lambda: types.SimpleNamespace(
                gpus=[types.SimpleNamespace(free_vram_mb=900)]))
        srv._unload_idle_voice_engine()
        self.assertTrue(eng.unloaded)

    def test_gpu_idle_floor_beats_cpu_timeout(self):
        # A cuda engine idles out on the GPU leash (default 120 s) long
        # before the 600 s CPU-engine window.
        eng = self._fake_worker_engine(
            last_used=time.time() - 200)
        srv = self._server({"chatterbox": eng}, idle_s=600.0)
        srv._unload_idle_voice_engine()
        self.assertTrue(eng.unloaded)

    def test_gpu_engine_inside_floor_stays(self):
        eng = self._fake_worker_engine(
            last_used=time.time() - 60)
        srv = self._server({"chatterbox": eng}, idle_s=600.0)
        srv._unload_idle_voice_engine()
        self.assertFalse(eng.unloaded)


if __name__ == "__main__":
    unittest.main()
