from __future__ import annotations

import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOICE_DIR = ROOT / "localcodeagent" / "voice" / "chatterbox_voices" / "isabella"
PRESET_PATH = ROOT / "localcodeagent" / "voice" / "official" / "nexus-isabella-chatterbox.json"
GOLDEN = ROOT / "docs" / "reference" / "audio" / "isabella-v7-approved-golden-2s.mp3"
GOLDEN_META = ROOT / "docs" / "reference" / "audio" / "isabella-v7-approved-golden.json"

try:
    import numpy as np
    from localcodeagent.voice import dsp, loudness
    from localcodeagent.voice.manager import VoiceManager
    from localcodeagent.voice.types import VoicePreset
    HAS_NUMPY = True
except ImportError:
    np = None
    dsp = None
    loudness = None
    VoiceManager = None
    VoicePreset = None
    HAS_NUMPY = False


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _preset():
    return VoicePreset.from_dict(_load(PRESET_PATH))


def test_isabella_chatterbox_uses_dry_reference_source() -> None:
    meta = _load(VOICE_DIR / "voice.json")
    assert meta["reference"] == "reference-source.mp3"
    assert meta["reference_source"] == "reference-source.mp3"
    assert "dry" in meta["provenance"].lower() or "unprocessed" in meta["provenance"].lower()
    ref = VOICE_DIR / meta["reference"]
    assert ref.exists()
    assert ref.stat().st_size > 100_000
    assert not (VOICE_DIR / "reference.wav").exists(), (
        "Do not condition Chatterbox from the old processed V6 reference"
    )


def test_isabella_v7_applies_v6_character_once_after_dry_clone() -> None:
    p = _load(PRESET_PATH)
    assert p["target_signature"] == "approved-v7-v6-character-louder"
    assert float(p["synthetic"]) == 0.8
    assert p["neural"]["enabled"] is True
    assert p["glass"]["enabled"] is True
    assert p["micro"]["enabled"] is True
    # Original V6 character is allowed once post-generation. Barrel prevention
    # now comes from the DRY conditioning reference and centered output rather
    # than deleting the character the user approved.
    total_parallel_mix = (
        float(p["neural"]["mix"])
        + float(p["glass"]["mix"])
        + float(p["micro"]["mix"])
    )
    assert total_parallel_mix <= 0.40
    assert p["stereo_width"] == 0.0
    assert p["ambience_ms"] == 0.0


def test_isabella_v7_preserves_approved_tonal_shape() -> None:
    p = _load(PRESET_PATH)
    assert 1.1 <= float(p["pitch_semitones"]) <= 1.4
    assert 0.95 <= float(p["tempo"]) <= 1.01
    low_cut = [b for b in p["eq"] if 180 <= float(b["freq_hz"]) <= 300 and float(b["gain_db"]) <= -2.5]
    presence = [b for b in p["eq"] if 3000 <= float(b["freq_hz"]) <= 4200 and float(b["gain_db"]) >= 5.0]
    air = [b for b in p["eq"] if 6500 <= float(b["freq_hz"]) <= 8500 and float(b["gain_db"]) >= 2.5]
    assert low_cut and presence and air


def test_isabella_chatterbox_is_intentionally_louder_but_limited() -> None:
    p = _load(PRESET_PATH)
    assert p["normalize_loudness"] is True
    assert -13.0 <= float(p["loudness_target_lufs"]) <= -12.0
    assert p["limiter_enabled"] is True
    assert 0.88 <= float(p["limiter_ceiling"]) <= 0.90


# ---------------------------------------------------------------------------
# golden reference discipline — the approved V7 target is an A/B acoustic
# target, never Chatterbox conditioning audio.

def test_golden_reference_exists_and_matches_metadata() -> None:
    assert GOLDEN.is_file() and GOLDEN.stat().st_size > 5_000
    meta = _load(GOLDEN_META)
    assert meta["approved_by_user"] is True
    assert meta["target_signature"] == "approved-v7-v6-character-louder"
    digest = hashlib.sha256(GOLDEN.read_bytes()).hexdigest()
    assert digest == meta["excerpt"]["sha256"]


def test_golden_reference_is_never_conditioning_source() -> None:
    meta = _load(VOICE_DIR / "voice.json")
    ref = (VOICE_DIR / meta["reference"]).resolve()
    assert ref != GOLDEN.resolve()
    # The golden file must not live inside any voice directory — it can
    # never be picked up as a user-installed clone voice either.
    rel = GOLDEN.resolve().relative_to(ROOT)
    assert "chatterbox_voices" not in rel.parts
    assert "voices" not in rel.parts


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_prepare_voice_sends_dry_reference_to_worker() -> None:
    """The worker must receive reference-source.mp3 (dry bf_isabella),
    resolved inside the shipped isabella voice dir — not a processed wav,
    not the golden target."""
    from localcodeagent.voice.chatterbox import ChatterboxEngine
    with tempfile.TemporaryDirectory() as tmp:
        eng = ChatterboxEngine(asset_dir=Path(tmp) / "a",
                               runtime_dir=Path(tmp) / "r")
        sent: dict = {}
        eng._request = lambda payload, timeout, **kw: (
            sent.update(payload) or {"ok": True})
        eng._prepare("isabella")
    assert sent["cmd"] == "prepare_voice"
    ref = Path(sent["reference"])
    assert ref.name == "reference-source.mp3"
    assert ref.parent == VOICE_DIR
    assert ref.is_file()


# ---------------------------------------------------------------------------
# conditioning cache — stale processed-reference conditionals must be
# impossible to serve: the cache filename binds the reference bytes.

def test_conditioning_cache_key_binds_reference_bytes() -> None:
    from localcodeagent.voice import chatterbox_worker as w
    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "ref-a.wav"
        b = Path(tmp) / "ref-b.wav"
        a.write_bytes(b"dry-reference" * 64)
        b.write_bytes(b"processed-reference" * 64)
        pa = w._conds_cache_path(str(a), 0.5, True)
        pb = w._conds_cache_path(str(b), 0.5, True)
        assert pa is not None and pb is not None
        assert pa != pb
        # Params that change the conditioning also change the key.
        assert w._conds_cache_path(str(a), 0.5, True) != \
            w._conds_cache_path(str(a), 0.9, True)
        assert w._conds_cache_path(str(a), 0.5, True) != \
            w._conds_cache_path(str(a), 0.5, False)


def test_conditioning_cache_evicts_stale_keys_on_save() -> None:
    """When the reference changes, the previous key's conds-*.pt must not
    survive alongside the new one."""
    from localcodeagent.voice import chatterbox_worker as w
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref.wav"
        ref.write_bytes(b"new-dry-bytes" * 64)
        stale = Path(tmp) / "conds-oldprocessedref-e0.5-n1-fp32-749d1c1a.pt"
        stale.write_bytes(b"old")
        cache = w._conds_cache_path(str(ref), 0.5, True)
        assert cache is not None

        class _Conds:
            t3 = None
            def save(self, path):
                Path(path).write_bytes(b"fresh")

        class _TTS:
            conds = None
            calls = 0
            def prepare_conditionals(self, *a, **k):
                self.calls += 1
                self.conds = _Conds()

        tts = _TTS()
        w._apply_conditionals(tts, str(ref), 0.5, True)
        assert tts.calls == 1
        assert cache.exists()
        assert not stale.exists(), "stale conditioning survived a ref change"


# ---------------------------------------------------------------------------
# speech-cache contract — V7 audio must not be served from pre-V7 keys.

@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_v7_preset_cache_key_distinct_from_legacy() -> None:
    from localcodeagent.voice.cache import AudioCache
    p = _preset()
    legacy = VoicePreset.from_dict({
        **p.as_dict(), "stereo_width": 1.5, "synthetic": 0.45,
        "loudness_target_lufs": -14.0})
    assert AudioCache.preset_hash(p.to_json()) != \
        AudioCache.preset_hash(legacy.to_json())
    k_new = AudioCache.key("hi", "chatterbox", f"v|dsp{dsp.DSP_VERSION}",
                           "isabella", AudioCache.preset_hash(p.to_json()), 1.0)
    k_old = AudioCache.key("hi", "chatterbox", f"v|dsp{dsp.DSP_VERSION}",
                           "isabella",
                           AudioCache.preset_hash(legacy.to_json()), 1.0)
    assert k_new != k_old


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_synthesize_applies_dsp_exactly_once() -> None:
    """A second dsp.process pass would re-shift pitch/EQ/layers — the
    production path must call it once per utterance."""
    m = VoiceManager(types.SimpleNamespace(
        voice_enabled=True, voice_muted=False, voice_engine="chatterbox",
        voice_preset_id="", voice_mode="responses", voice_speed=1.0,
        voice_volume=1.0, voice_normalize_loudness=True,
        voice_target_lufs=-14.0, voice_limiter_enabled=True,
        voice_chatterbox_quality_retries=0,
        save=lambda: None),
        preset_dir=Path(tempfile.mkdtemp()) / "p",
        cache_dir=Path(tempfile.mkdtemp()) / "c")

    class _FakeChatterbox:
        name, version, sample_rate = "chatterbox", "x", 24000
        def synthesize(self, text, *, voice=None, speed=1.0, lang="en"):
            t = np.linspace(0, 0.5, 12000, dtype=np.float32)
            return (0.1 * np.sin(2 * np.pi * 220 * t)), 24000
        def voices(self): return []
        def status(self): return {"name": "chatterbox"}

    m._engines["chatterbox"] = _FakeChatterbox()
    calls = []
    real = dsp.process
    dsp.process = lambda x, sr, preset, **kw: (calls.append(1), real(x, sr, preset))[1]
    try:
        m._synthesize("hello there", _preset(), 1.0,
                      apply_personality=False)
    finally:
        dsp.process = real
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# barrel regression — the user's "sounds like she's in a barrel" complaint,
# as measurable DSP-level proxies on the shipped preset.

@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_isabella_v7_does_not_regress_to_barrel_sound() -> None:
    p = _preset()
    sr = 24000
    t = np.arange(int(sr * 2.0), dtype=np.float32) / sr
    # Deterministic speech-like carrier: harmonic stack with a slow
    # syllable envelope and a presence/air component the barrel defect
    # would swallow.
    sig = (0.30 * np.sin(2 * np.pi * 220 * t)
           + 0.15 * np.sin(2 * np.pi * 440 * t)
           + 0.08 * np.sin(2 * np.pi * 660 * t)
           + 0.05 * np.sin(2 * np.pi * 3300 * t)
           + 0.03 * np.sin(2 * np.pi * 6600 * t))
    sig = (sig * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    out = dsp.process(sig, sr, p)

    # Centered output — the V6 stereo-widening path is part of what
    # read as "room/tunnel". L and R must be identical.
    np.testing.assert_allclose(out[:, 0], out[:, 1], atol=1e-5)
    # Limiter respected, no clipping.
    peak = float(np.abs(out).max())
    assert peak <= p.limiter_ceiling + 0.02
    assert float(np.mean(np.abs(out) >= 0.999)) < 0.001
    # Loudness lands near the approved -12.5 LUFS target.
    lufs = loudness.integrated_lufs(out, sr)
    assert -14.0 <= lufs <= -11.0, f"integrated loudness {lufs:.1f} LUFS"
    # Presence/air survives the low-mid cut — the barrel signature was a
    # hollow low-mid blob with swallowed highs.
    spec = np.abs(np.fft.rfft(out[:, 0])) ** 2
    freqs = np.fft.rfftfreq(out.shape[0], 1.0 / sr)
    body = float(spec[(freqs >= 120) & (freqs < 600)].sum())
    clarity = float(spec[(freqs >= 3000) & (freqs < 10000)].sum())
    assert clarity > 0.0
    assert body / clarity < 60.0, (
        f"low-mid dominance {body / clarity:.1f}x — barrel signature")


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_double_dsp_pass_is_measurably_different() -> None:
    """Documents the detector contract: if a code path ever runs the
    preset DSP twice, the acoustic signature moves far enough for the
    eval harness to catch it."""
    p = _preset()
    sr = 24000
    t = np.arange(int(sr * 1.5), dtype=np.float32) / sr
    sig = (0.2 * np.sin(2 * np.pi * 220 * t)
           + 0.05 * np.sin(2 * np.pi * 3400 * t)).astype(np.float32)
    once = dsp.process(sig, sr, p)
    twice = dsp.process(dsp.process_mono(sig, sr, p), sr, p)

    def peak_hz(x):
        s = np.abs(np.fft.rfft(x))
        f = np.fft.rfftfreq(x.size, 1.0 / sr)
        return float(f[int(np.argmax(s))])

    # A second pass re-applies the +1.25 st pitch shift, so the dominant
    # peak must move by roughly a semitone (~7%) — far above bin noise.
    ratio = peak_hz(twice[:, 0]) / max(peak_hz(once[:, 0]), 1.0)
    assert abs(ratio - 1.0) > 0.03, (
        f"double DSP pass barely detectable (peak ratio {ratio:.3f}) — "
        "eval contract broken")


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_warm_engine_targets_active_preset_engine() -> None:
    """Regression: _warm_on_first_speech used to warm the config-default
    engine — with a Chatterbox preset selected it loaded Kokoro for no
    benefit while the real engine still cold-started."""
    cfg = types.SimpleNamespace(
        voice_enabled=True, voice_muted=False, voice_engine="kokoro",
        voice_preset_id="cb-v7", voice_mode="responses", voice_speed=1.0,
        voice_volume=1.0, save=lambda: None)
    m = VoiceManager(cfg, preset_dir=Path(tempfile.mkdtemp()) / "p",
                     cache_dir=Path(tempfile.mkdtemp()) / "c")
    m.presets.save(VoicePreset(id="cb-v7", name="cb", engine="chatterbox",
                               base_voice="isabella"))
    loaded = []

    class _SpyChatterbox:
        name, version, sample_rate = "chatterbox", "x", 24000
        def load(self): loaded.append("chatterbox")
        def status(self): return {"name": "chatterbox"}

    class _SpyKokoro:
        name, version, sample_rate = "kokoro", "x", 24000
        def load(self): loaded.append("kokoro")
        def status(self): return {"name": "kokoro"}

    m._engines["chatterbox"] = _SpyChatterbox()
    m._engines["kokoro"] = _SpyKokoro()
    m._warm_engine()
    assert loaded == ["chatterbox"]


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_startup_sig_changes_with_preset_content() -> None:
    """StartupNarrator salts its per-line wav cache with _startup_sig_raw.
    Preset ids are stable across tuning changes — the sig must embed the
    preset content hash or a clip rendered under an old recipe replays
    forever (the pre-V7 'online' clip kept matching the post-V7 sig)."""
    cfg = types.SimpleNamespace(
        voice_enabled=True, voice_muted=False, voice_engine="chatterbox",
        voice_preset_id="cb-v7", voice_mode="responses", voice_speed=1.0,
        voice_volume=1.0, save=lambda: None)
    m = VoiceManager(cfg, preset_dir=Path(tempfile.mkdtemp()) / "p",
                     cache_dir=Path(tempfile.mkdtemp()) / "c")
    m.presets.save(VoicePreset(id="cb-v7", name="cb", engine="chatterbox",
                               base_voice="isabella", stereo_width=1.5,
                               loudness_target_lufs=-14.0))
    eng = types.SimpleNamespace(name="chatterbox", version="v1")
    sig_old = m._startup_sig_raw(eng)
    m.presets.save(VoicePreset(id="cb-v7", name="cb", engine="chatterbox",
                               base_voice="isabella", stereo_width=0.0,
                               loudness_target_lufs=-12.5))
    sig_new = m._startup_sig_raw(eng)
    assert sig_old != sig_new, "same-id preset retune must re-salt narrator cache"
    # id/identity fields stay in the sig for readability of engine.json
    assert "cb-v7" in sig_new and "chatterbox" in sig_new


def test_v7_preset_keeps_air_band_for_anti_barrel_tone() -> None:
    """The 2026-10-07 live diagnosis: 'barrel' perception was TONAL — the
    dry-ref generation is ~1000 Hz darker than the approved golden
    (centroid 4391 Hz, air band 0.129). The air/presence EQ is the guard:
    removing it re-darkens the voice into the hollow sound without
    touching any echo path."""
    p = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    air = [b for b in p["eq"]
           if b["freq_hz"] >= 9000 and b["gain_db"] >= 4.0]
    assert air, "V7 preset must keep a >=9 kHz air band (golden-matched)"
    assert p["exciter"] >= 0.5, "exciter below 0.5 loses the golden sheen"


# ---------------------------------------------------------------------------
# stochastic-draw quality gate — live-measured bad draws (short replies
# rendering dark + reverberant, e.g. 0.86 s @ 2690 Hz / echo 0.68 vs
# golden 4391 Hz / 0.30) must be re-generated, not cached.

def _gate_manager(retries):
    cfg = types.SimpleNamespace(
        voice_enabled=True, voice_muted=False, voice_engine="chatterbox",
        voice_preset_id="", voice_mode="responses", voice_speed=1.0,
        voice_volume=1.0, voice_normalize_loudness=True,
        voice_target_lufs=-14.0, voice_limiter_enabled=True,
        voice_chatterbox_quality_retries=retries,
        save=lambda: None)
    m = VoiceManager(cfg, preset_dir=Path(tempfile.mkdtemp()) / "p",
                     cache_dir=Path(tempfile.mkdtemp()) / "c",
                     publish=lambda k, p: _gate_manager.events.append((k, p)))
    return m


_gate_manager.events = []


class _DrawChatterbox:
    """Serves queued wavs then repeats the last — a stochastic engine."""
    name, version, sample_rate = "chatterbox", "x", 24000
    def __init__(self, wavs):
        self.wavs, self.calls = list(wavs), 0
    def synthesize(self, text, *, voice=None, speed=1.0, lang="en"):
        self.calls += 1
        w = self.wavs.pop(0) if len(self.wavs) > 1 else self.wavs[0]
        return w, 24000
    def voices(self): return []
    def status(self): return {"name": "chatterbox"}


def _dark_draw(n=24000):
    # 200 Hz pure tone → centroid ~200 Hz, pitch autocorr inside the
    # 4–80 ms echo window → both gate metrics fail.
    t = np.linspace(0, 1.0, n, dtype=np.float32)
    return 0.1 * np.sin(2 * np.pi * 200 * t)


def _clean_draw(n=24000):
    # Broadband noise → high centroid, no echo peak → gate passes.
    rng = np.random.default_rng(7)
    return (0.05 * rng.standard_normal(n)).astype(np.float32)


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_quality_gate_redraws_bad_draws() -> None:
    _gate_manager.events.clear()
    m = _gate_manager(retries=2)
    eng = _DrawChatterbox([_dark_draw(), _clean_draw()])
    m._engines["chatterbox"] = eng
    m._synthesize("short reply", _preset(), 1.0, apply_personality=False)
    assert eng.calls == 2, "a dark/reverberant draw must trigger a redraw"
    assert any(p.get("event") == "quality_redraw"
               for _, p in _gate_manager.events)


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_quality_gate_accepts_good_draw_first_try() -> None:
    m = _gate_manager(retries=2)
    eng = _DrawChatterbox([_clean_draw()])
    m._engines["chatterbox"] = eng
    m._synthesize("short reply", _preset(), 1.0, apply_personality=False)
    assert eng.calls == 1, "a passing draw must not waste a retry"


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_quality_gate_zero_retries_disables() -> None:
    m = _gate_manager(retries=0)
    eng = _DrawChatterbox([_dark_draw()])
    m._engines["chatterbox"] = eng
    m._synthesize("short reply", _preset(), 1.0, apply_personality=False)
    assert eng.calls == 1


@unittest.skipUnless(HAS_NUMPY, "numpy required")
def test_quality_gate_skips_tag_segments() -> None:
    """Paralinguistic-tag text renders non-speech sounds that legitimately
    fail the speech band (a live [laugh] measured 1591 Hz / echo 0.91) —
    gating them would burn retries on good audio."""
    m = _gate_manager(retries=2)
    eng = _DrawChatterbox([_dark_draw()])
    m._engines["chatterbox"] = eng
    m._synthesize("[laugh] that was funny", _preset(), 1.0,
                apply_personality=False)
    assert eng.calls == 1
