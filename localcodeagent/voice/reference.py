"""Reference-audio validation and preparation for voice cloning.

Chatterbox Turbo rejects references under ~5 s and silently degrades on
clipped, noisy, or silence-dominated clips. ``validate_reference``
returns a structured report instead of letting the model assert at
generation time; ``prepare_reference`` renders a validated clip into a
canonical form (mono, target sample rate, loudness-normalized, peak-safe)
under a cache dir so originals are never mutated.
"""
from __future__ import annotations

import hashlib
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MIN_DURATION_S = 5.0        # turbo's own assert is >5 s
CLIP_FRAC_LIMIT = 0.001     # >0.1% samples at full scale = clipped
SILENCE_FRAC_LIMIT = 0.60   # >60% silent frames = mostly dead air
MIN_RMS_DBFS = -40.0        # quieter than this is effectively unusable
TARGET_SR = 24000


@dataclass
class ReferenceReport:
    path: str = ""
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    peak: float = 0.0
    rms_dbfs: float = 0.0
    clip_frac: float = 0.0
    silence_frac: float = 0.0
    lufs: float | None = None
    sha256: str = ""

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "path", "ok", "errors", "warnings", "duration_s",
            "sample_rate", "channels", "peak", "rms_dbfs", "clip_frac",
            "silence_frac", "lufs", "sha256")}


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        pcm = np.frombuffer(raw, dtype=np.int32).astype(np.float32) \
            / 2147483648.0
    elif width == 1:
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
               - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width: {width}")
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(pcm), sr


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _silence_frac(x: np.ndarray, sr: int, frame_s: float = 0.025,
                  floor_dbfs: float = -45.0) -> float:
    f = max(1, int(sr * frame_s))
    n = x.size // f
    if n < 2:
        return 0.0
    rms = np.sqrt((x[: n * f].reshape(n, f) ** 2).mean(axis=1))
    return float((rms < 10.0 ** (floor_dbfs / 20.0)).mean())


def validate_reference(path: Path) -> ReferenceReport:
    """Structured validation — errors block use, warnings degrade."""
    rep = ReferenceReport(path=str(path))
    p = Path(path)
    if not p.exists():
        rep.errors.append("file not found")
        return rep
    if p.suffix.lower() not in {".wav", ".wave"}:
        # Non-WAV sources are decodable (mp3/flac/ogg) but only inside
        # the runtime venv (librosa) — validate happens post-decode.
        rep.errors.append(
            f"unsupported container {p.suffix!r} — convert to WAV first "
            "(the provisioning pipeline decodes mp3/flac via librosa)")
        return rep
    try:
        pcm, sr = _read_wav(p)
    except (wave.Error, EOFError, ValueError, OSError) as exc:
        rep.errors.append(f"corrupt or unreadable WAV: {exc}")
        return rep
    rep.sample_rate = sr
    rep.channels = 1
    rep.duration_s = round(pcm.size / sr, 3)
    rep.sha256 = _sha256(p)
    if pcm.size == 0:
        rep.errors.append("empty audio")
        return rep
    rep.peak = float(np.abs(pcm).max())
    rms = float(np.sqrt((pcm ** 2).mean()))
    rep.rms_dbfs = round(20.0 * np.log10(max(rms, 1e-12)), 1)
    rep.clip_frac = float((np.abs(pcm) >= 0.999).mean())
    rep.silence_frac = _silence_frac(pcm, sr)
    try:
        from .loudness import integrated_lufs
        rep.lufs = round(integrated_lufs(pcm, sr), 1)
    except Exception:
        pass
    if rep.duration_s < MIN_DURATION_S:
        rep.errors.append(
            f"reference too short: {rep.duration_s:.1f}s "
            f"(need >= {MIN_DURATION_S:.0f}s)")
    if rep.clip_frac > CLIP_FRAC_LIMIT:
        rep.errors.append(
            f"reference is clipped ({rep.clip_frac * 100:.2f}% samples "
            "at full scale)")
    if rep.silence_frac > SILENCE_FRAC_LIMIT:
        rep.errors.append(
            f"reference is mostly silence ({rep.silence_frac * 100:.0f}%)")
    if rep.rms_dbfs < MIN_RMS_DBFS:
        rep.warnings.append(
            f"reference is very quiet ({rep.rms_dbfs:.0f} dBFS RMS)")
    if sr < 16000:
        rep.warnings.append(f"low sample rate ({sr} Hz) — cloning "
                            "quality will suffer")
    rep.ok = not rep.errors
    return rep


def prepare_reference(src: Path, cache_dir: Path, *,
                      target_sr: int = TARGET_SR) -> tuple[Path, ReferenceReport]:
    """Validate → canonicalize → cache. Returns (prepared_path, report).

    The source file is never modified. The prepared copy lands under
    ``cache_dir`` keyed on content hash + target sr, so rerunning on the
    same source is free and a changed source never serves stale output.
    """
    from . import dsp
    from .loudness import normalize_lufs

    rep = validate_reference(src)
    if not rep.ok:
        raise ValueError("invalid reference: " + "; ".join(rep.errors))
    key_src = rep.sha256 or _sha256(Path(src))
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"ref-{key_src[:24]}-{target_sr}.wav"
    if dest.exists():
        return dest, rep

    pcm, sr = _read_wav(Path(src))
    if sr != target_sr:
        pcm = dsp._resample(pcm, int(round(pcm.size * target_sr / sr)))
        sr = target_sr
    pcm, _ = normalize_lufs(pcm, sr, -23.0, max_gain_db=12.0)
    pcm = np.clip(pcm, -0.95, 0.95).astype(np.float32)
    from .dsp import wav_bytes
    import tempfile
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(wav_bytes(pcm[:, None], sr))
    tmp.replace(dest)
    return dest, rep
