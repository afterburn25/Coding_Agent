"""Voice subsystem tests — filter classification, streaming segmentation,
presets, queue/mute/cancel, cache keys, DSP chain sanity. Engine inference
is mocked at the boundary; everything above it is exercised for real."""
from __future__ import annotations

import json
import tempfile
import time
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

    def test_no_speech_when_disabled(self):
        self.m.config.voice_enabled = False
        self.m.begin_task("t")
        self.assertEqual(self.m.feed_token("t", "Hello there."), 0)
        self.m.finish_task("t", "Hello there.")
        self.assertEqual(self.m.status()["queue"], 0)
        self.m.config.voice_enabled = True

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


if __name__ == "__main__":
    unittest.main()
