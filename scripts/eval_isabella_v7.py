#!/usr/bin/env python3
"""Isabella V7 voice evaluator — measures live Chatterbox output against the
user-approved golden target.

Pipeline under test (unchanged from production):

    dry bf_isabella  ->  Chatterbox Turbo  ->  V7 Nexus DSP once
                      ->  loudness normalize -> limiter  ->  stereo WAV

Two modes:

  --wav FILE            measure an existing WAV (e.g. a segment captured from
                        the live install) against the golden target.
  --synthesize          generate the standard dogfood scripts through the REAL
                        VoiceManager._synthesize path (Chatterbox worker +
                        official preset + dsp.process) and measure each.

The golden reference (docs/reference/audio/isabella-v7-approved-golden-2s.mp3)
is an ACOUSTIC target only — it is never used as conditioning audio.

Usage:
    python scripts/eval_isabella_v7.py --synthesize --install-root D:\\Nexus_Core
    python scripts/eval_isabella_v7.py --wav segment.wav
    python scripts/eval_isabella_v7.py --synthesize --text "Custom line." --json

Exit code 0 when every measured utterance passes, 2 otherwise.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from localcodeagent.voice import loudness  # noqa: E402
from localcodeagent.voice.types import VoicePreset  # noqa: E402

GOLDEN = ROOT / "docs" / "reference" / "audio" / "isabella-v7-approved-golden-2s.mp3"
PRESET = ROOT / "localcodeagent" / "voice" / "official" / "nexus-isabella-chatterbox.json"

SCRIPTS = {
    "technical": (
        "No GitHub connection or access configured. I don't have a public "
        "repo or branches to show. Nexus Core is my personal workstation, "
        "not a shared project. If you'd like, I can help set up a local "
        "repo or sync one with your own GitHub account. Just say the word."),
    "conversational": (
        "Of course. Give me a moment and I'll check that for you. If "
        "something isn't working, I'll tell you exactly what failed "
        "instead of pretending it worked."),
    "excited": (
        "Excellent! Core systems are online, the models are ready, and "
        "everything is responding normally."),
    "concerned": (
        "I've detected a startup problem. I'm checking the affected "
        "component now and I'll report exactly what I find."),
    "longform": (
        "Here's what I found. The backend restarted cleanly after the "
        "deploy, and the health check passed on the first probe. The "
        "voice worker is warm now, so the next response should begin "
        "speaking almost immediately. If anything drifts from the "
        "approved target, the evaluation harness will flag the band "
        "that moved instead of guessing."),
}


# ---------------------------------------------------------------------------
# decode

def _ffmpeg() -> str | None:
    import shutil
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def decode(path: Path) -> tuple[np.ndarray, int]:
    """Any audio -> float32 (n, ch). WAV via stdlib; anything else via
    ffmpeg's s16le pipe (one decode, no temp files)."""
    path = Path(path)
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as w:
            sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            raw = w.readframes(n)
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return pcm.reshape(-1, ch), sr
    exe = _ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg required to decode " + path.suffix)
    # Normalize every non-WAV input to the engine's native rate — the
    # golden target compares against 24 kHz production output, so the
    # same band/LUFS windows apply to both sides of the A/B.
    proc = subprocess.run(
        [exe, "-hide_banner", "-y", "-i", str(path),
         "-ar", "24000", "-f", "s16le", "-acodec", "pcm_s16le",
         "-ac", "2", "-"],
        capture_output=True, timeout=60)
    sr = 24000
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg decode failed: "
                           + (proc.stderr or b"")[-200:].decode("utf-8", "replace"))
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm.reshape(-1, 2), sr


def _mono(pcm: np.ndarray) -> np.ndarray:
    return pcm.mean(axis=1) if pcm.ndim > 1 else pcm


# ---------------------------------------------------------------------------
# measurement

BANDS = {
    "sub_0_120": (0, 120),
    "body_120_500": (120, 500),
    "lowmid_500_1500": (500, 1500),
    "presence_1500_3400": (1500, 3400),
    "clarity_3400_5000": (3400, 5000),
    "air_6000_10000": (6000, 10000),
}


def _band_shares(x: np.ndarray, sr: int) -> dict[str, float]:
    spec = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    total = float(spec.sum()) + 1e-12
    return {k: float(spec[(freqs >= lo) & (freqs < hi)].sum() / total)
            for k, (lo, hi) in BANDS.items()}


def _centroid(x: np.ndarray, sr: int) -> float:
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    return float((spec * freqs).sum() / max(spec.sum(), 1e-12))


def _echo_energy(x: np.ndarray, sr: int,
                 lo_ms: float = 4.0, hi_ms: float = 80.0) -> float:
    """Peak normalized autocorrelation at echo-lag delays — a comb/echo
    signature shows up as a strong second peak well after lag 0."""
    x = x - x.mean()
    n = min(x.size, int(sr * 4))
    x = x[:n]
    ac = np.correlate(x, x, mode="full")[n - 1:]
    ac /= max(ac[0], 1e-12)
    lo, hi = int(sr * lo_ms / 1000), int(sr * hi_ms / 1000)
    return float(ac[lo:hi].max()) if hi < ac.size else 0.0


def _noise_floor_db(x: np.ndarray, sr: int) -> float:
    frame = max(1, int(sr * 0.02))
    n = x.size // frame
    if n < 4:
        return -np.inf
    rms = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
    return float(20 * np.log10(np.percentile(rms, 10) + 1e-9))


def measure(pcm: np.ndarray, sr: int) -> dict:
    pcm = np.asarray(pcm, dtype=np.float32)
    mono = _mono(pcm)
    peak = float(np.abs(pcm).max()) if pcm.size else 0.0
    rms = float(np.sqrt((mono ** 2).mean())) if mono.size else 0.0
    clipped = float(np.mean(np.abs(pcm) >= 0.999)) if pcm.size else 0.0
    out = {
        "seconds": round(mono.size / sr, 3),
        "sample_rate": sr,
        "lufs": round(loudness.integrated_lufs(pcm, sr), 2),
        "rms_dbfs": round(20 * np.log10(rms + 1e-9), 2),
        "peak_dbfs": round(20 * np.log10(peak + 1e-9), 2),
        "clip_fraction": round(clipped, 6),
        "centroid_hz": round(_centroid(mono, sr), 1),
        "noise_floor_db": round(_noise_floor_db(mono, sr), 1),
        "echo_lag_corr": round(_echo_energy(mono, sr), 3),
    }
    out.update({k: round(v, 4) for k, v in _band_shares(mono, sr).items()})
    if pcm.ndim > 1 and pcm.shape[1] >= 2:
        L, R = pcm[:, 0], pcm[:, 1]
        denom = float(np.sqrt((L ** 2).sum() * (R ** 2).sum())) + 1e-12
        corr = float((L * R).sum() / denom)
        side = float(((L - R) ** 2).sum())
        mid = float(((L + R) ** 2).sum()) + 1e-12
        out["stereo_corr"] = round(corr, 4)
        out["side_mid_ratio"] = round(side / mid, 5)
    else:
        out["stereo_corr"] = 1.0
        out["side_mid_ratio"] = 0.0
    return out


# ---------------------------------------------------------------------------
# barrel regression — measurable proxies for the user's "in a barrel" report:
# low-mid buildup, swallowed presence, side-channel/room energy, comb echo.

def barrel_flags(m: dict) -> list[str]:
    flags = []
    if m["side_mid_ratio"] > 0.02:
        flags.append(f"side-channel energy {m['side_mid_ratio']:.4f} "
                     "(stereo width must stay centered)")
    if abs(1.0 - m["stereo_corr"]) > 0.02:
        flags.append(f"stereo correlation {m['stereo_corr']:.3f} "
                     "(phase decorrelation)")
    if m["echo_lag_corr"] > 0.55:
        flags.append(f"echo-lag correlation {m['echo_lag_corr']:.2f} "
                     "(delayed/room energy)")
    lowmid = m["body_120_500"] + m["lowmid_500_1500"]
    high = m["presence_1500_3400"] + m["clarity_3400_5000"] + m["air_6000_10000"]
    if high > 0 and lowmid / high > 8.0:
        flags.append(f"low-mid dominance {lowmid / high:.1f}x over presence+air")
    if m["clip_fraction"] > 0.001:
        flags.append(f"clipping {m['clip_fraction']:.4f}")
    return flags


def loudness_flags(m: dict, target: float = -12.5) -> list[str]:
    flags = []
    if not np.isfinite(m["lufs"]):
        flags.append("no measurable loudness")
    elif abs(m["lufs"] - target) > 1.5:
        flags.append(f"loudness {m['lufs']:.1f} LUFS off target "
                     f"{target:.1f} (tolerance 1.5)")
    if m["peak_dbfs"] > -0.5:
        flags.append(f"peak {m['peak_dbfs']:.1f} dBFS above limiter ceiling")
    return flags


# ---------------------------------------------------------------------------
# real-pipeline synthesis

class EvalPipeline:
    """One VoiceManager + one Chatterbox worker for the whole run —
    matches production, where the worker persists across utterances."""

    def __init__(self, install_root: Path, work: Path) -> None:
        import types as _t
        from localcodeagent.voice.manager import VoiceManager

        self.events: list[dict] = []
        cfg = _t.SimpleNamespace(
            voice_enabled=True, voice_muted=False,
            voice_engine="chatterbox",
            voice_preset_id="nexus-isabella-chatterbox",
            voice_mode="responses", voice_speed=1.0, voice_volume=1.0,
            voice_chatterbox_synth_timeout_s=240.0,
            voice_chatterbox_runtime_dir="runtime/voice/chatterbox",
            voice_chatterbox_device="auto",
            voice_chatterbox_min_free_vram_mb=3200.0,
            voice_chatterbox_dtype="bf16",
            voice_normalize_loudness=True, voice_target_lufs=-14.0,
            voice_limiter_enabled=True,
            save=lambda: None)
        asset_dir = install_root / "models" / "voice"
        self.vm = VoiceManager(
            cfg, preset_dir=work / "presets", cache_dir=work / "cache",
            asset_dir=asset_dir,
            publish=lambda _kind, payload: self.events.append(payload))
        preset = self.vm.presets.get("nexus-isabella-chatterbox")
        if preset is None:
            raise RuntimeError("official preset nexus-isabella-chatterbox "
                               "did not seed into the preset store")
        if str(preset.engine) != "chatterbox":
            raise RuntimeError(f"preset engine drifted: {preset.engine}")
        self.preset = preset

    def close(self) -> None:
        try:
            self.vm.engine("chatterbox").unload()
        except Exception:
            pass

    def synthesize(self, text: str, work: Path) -> tuple[Path, str, dict]:
        """Generate `text` through the actual production path:
        VoiceManager._synthesize -> ChatterboxEngine (worker) ->
        dsp.process -> cache wav. Returns (wav, engine_used, status)."""
        mark = len(self.events)
        import time as _time
        t0 = _time.monotonic()
        _pcm, _sr, seg = self.vm._synthesize(
            text, self.preset, 1.0, apply_personality=False)
        wall = _time.monotonic() - t0
        new_events = self.events[mark:]
        engine_used = "chatterbox"
        if any(e.get("event") == "engine_fallback" for e in new_events):
            engine_used = "kokoro-fallback"
        eng = self.vm._engines.get("chatterbox")
        st = dict(getattr(eng, "_last_status", {}) or {})
        st["wall_s"] = round(wall, 2)
        st["device"] = getattr(eng, "_device", "")
        work.mkdir(parents=True, exist_ok=True)
        out = work / "synth.wav"
        out.write_bytes(Path(seg).read_bytes())
        return out, engine_used, st


# ---------------------------------------------------------------------------

def _report(name: str, m: dict, golden: dict | None) -> dict:
    flags = barrel_flags(m) + loudness_flags(m)
    row = {"name": name, "metrics": m, "flags": flags,
           "ok": not flags}
    print(f"\n=== {name} ===")
    print(f"  {m['seconds']:.2f}s  {m['lufs']:.1f} LUFS  "
          f"rms {m['rms_dbfs']:.1f}  peak {m['peak_dbfs']:.1f} dBFS  "
          f"clip {m['clip_fraction']:.4f}")
    print(f"  centroid {m['centroid_hz']:.0f} Hz  "
          f"corr {m['stereo_corr']:.3f}  side/mid {m['side_mid_ratio']:.4f}  "
          f"echo {m['echo_lag_corr']:.2f}  floor {m['noise_floor_db']:.0f} dB")
    bands = "  ".join(f"{k.split('_')[0]} {m[k]:.3f}"
                      for k in BANDS)
    print(f"  bands: {bands}")
    if golden is not None:
        dl = m["lufs"] - golden["lufs"]
        dc = m["centroid_hz"] - golden["centroid_hz"]
        print(f"  vs golden: LUFS {dl:+.1f}  centroid {dc:+.0f} Hz")
    if flags:
        for f in flags:
            print(f"  FLAG: {f}")
    else:
        print("  ok")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install-root", default=r"D:\Nexus_Core",
                    help="install root providing models/voice + runtime/voice")
    ap.add_argument("--golden", default=str(GOLDEN))
    ap.add_argument("--wav", action="append", default=[],
                    help="measure an existing wav/mp3 (repeatable)")
    ap.add_argument("--synthesize", action="store_true",
                    help="generate the standard scripts through the real "
                         "Chatterbox pipeline and measure them")
    ap.add_argument("--text", help="single custom text for --synthesize")
    ap.add_argument("--keep", action="store_true",
                    help="keep synthesized wavs under the work dir")
    ap.add_argument("--out", default="",
                    help="directory for kept wavs (default: tempdir)")
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON")
    args = ap.parse_args()

    golden = None
    if Path(args.golden).exists():
        gpcm, gsr = decode(Path(args.golden))
        golden = measure(gpcm, gsr)
    else:
        print(f"golden reference missing: {args.golden}", file=sys.stderr)

    rows = []
    work = Path(args.out) if args.out else None
    tmp_ctx = None
    if args.synthesize:
        if work is None:
            tmp_ctx = tempfile.TemporaryDirectory(prefix="isabella-v7-")
            work = Path(tmp_ctx.name)
        work.mkdir(parents=True, exist_ok=True)
        pipeline = EvalPipeline(Path(args.install_root), work)
        scripts = {"custom": args.text} if args.text else SCRIPTS
        try:
            for name, text in scripts.items():
                print(f"synthesizing [{name}] ...", flush=True)
                try:
                    wav_path, engine_used, st = pipeline.synthesize(
                        text, work / name)
                    if args.keep:
                        kept = work / f"{name}.wav"
                        kept.write_bytes(wav_path.read_bytes())
                        print(f"  kept: {kept}  [{engine_used} "
                              f"{st.get('device','')} wall {st.get('wall_s')}s"
                              f" gen {st.get('gen_s')}s rtf {st.get('rtf')}]")
                    pcm, sr = decode(wav_path)
                    row = _report(name, measure(pcm, sr), golden)
                    row["engine"] = engine_used
                    row["synth"] = st
                    if engine_used != "chatterbox":
                        row["flags"].append(
                            f"rendered by {engine_used} — Chatterbox path "
                            "was not exercised")
                        row["ok"] = False
                    rows.append(row)
                except Exception as exc:
                    print(f"  SYNTH FAILED: {exc}")
                    rows.append({"name": name, "ok": False,
                                 "flags": [f"synthesis failed: {exc}"],
                                 "metrics": {}})
        finally:
            try:
                eng = pipeline.vm._engines.get("chatterbox")
                if eng is not None:
                    st = eng.status()
                    print(f"\nengine: device={st.get('device')} "
                          f"load={st.get('load_time_s')}s "
                          f"calls={st.get('synth_calls')} "
                          f"rtf={st.get('rtf')} "
                          f"vram={st.get('vram_alloc_mb')}MB")
            except Exception:
                pass
            pipeline.close()
    for w in args.wav:
        pcm, sr = decode(Path(w))
        rows.append(_report(Path(w).name, measure(pcm, sr), golden))
    if not rows and golden is not None:
        rows.append(_report("golden", golden, None))

    if args.json:
        print(json.dumps({"golden": golden, "results": rows}, indent=2))
    n_bad = sum(1 for r in rows if not r["ok"])
    print(f"\n{len(rows) - n_bad}/{len(rows)} measured utterances pass")
    if tmp_ctx is not None:
        tmp_ctx.cleanup()
    return 2 if n_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
