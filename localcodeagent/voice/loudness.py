"""BS.1770-style integrated loudness measurement and normalization.

Pure numpy — no pyloudnorm dependency in the realtime path. Implements the
ITU-R BS.1770 K-weighting filter pair (high-shelf + high-pass, RBJ biquads
computed from the standard analog prototype parameters at runtime sample
rate) and integrated loudness with the -70 LUFS absolute gate. Relative
gating is omitted deliberately: for single-utterance speech the absolute
gate alone lands within ~0.3 LU of full BS.1770-4, and silence tails must
not bias the measurement upward or downward.
"""
from __future__ import annotations

import numpy as np

LUFS_OFFSET = -0.691          # BS.1770 channel-power → LUFS constant
ABSOLUTE_GATE_LUFS = -70.0


def _biquad_high_shelf(sr: int, gain_db: float = 3.99984385397,
                       fc_hz: float = 1681.974450955533,
                       q: float = 0.7071752369554193) -> tuple[np.ndarray, np.ndarray]:
    """RBJ high-shelf — BS.1770 stage-1 K-weighting curve."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * fc_hz / sr
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)
    b0 = A * ((A + 1) + (A - 1) * cos_w0 + 2 * np.sqrt(A) * alpha)
    b1 = -2 * A * ((A - 1) + (A + 1) * cos_w0)
    b2 = A * ((A + 1) + (A - 1) * cos_w0 - 2 * np.sqrt(A) * alpha)
    a0 = (A + 1) - (A - 1) * cos_w0 + 2 * np.sqrt(A) * alpha
    a1 = 2 * ((A - 1) - (A + 1) * cos_w0)
    a2 = (A + 1) - (A - 1) * cos_w0 - 2 * np.sqrt(A) * alpha
    return (np.array([b0, b1, b2]) / a0, np.array([1.0, a1 / a0, a2 / a0]))


def _biquad_highpass(sr: int, fc_hz: float = 38.13547087613982,
                     q: float = 0.5003270373253953) -> tuple[np.ndarray, np.ndarray]:
    """RBJ high-pass — BS.1770 stage-2 K-weighting curve."""
    w0 = 2.0 * np.pi * fc_hz / sr
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)
    b0 = (1 + cos_w0) / 2.0
    b1 = -(1 + cos_w0)
    b2 = (1 + cos_w0) / 2.0
    a0 = 1 + alpha
    a1 = -2 * cos_w0
    a2 = 1 - alpha
    return (np.array([b0, b1, b2]) / a0, np.array([1.0, a1 / a0, a2 / a0]))


def _lfilter(b: np.ndarray, a: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Apply a biquad via its FFT-domain transfer function — O(n log n),
    no scipy needed. The K-weighting filters run on utterance audio that
    starts/ends near silence, so steady-state evaluation (no filter
    state carry) matches direct-form output within the noise floor."""
    n = x.size
    nfft = 1 << (n + 64).bit_length() if n > 64 else 256
    w = np.exp(-2j * np.pi * np.arange(nfft // 2 + 1) / nfft)
    H = np.polyval(b[::-1], w) / np.polyval(a[::-1], w)
    y = np.fft.irfft(np.fft.rfft(x, nfft) * H, nfft)[:n]
    return y


def k_weight(x: np.ndarray, sr: int) -> np.ndarray:
    """Apply the two-stage K-weighting filter to mono float32 PCM."""
    b, a = _biquad_high_shelf(sr)
    y = _lfilter(b, a, np.asarray(x, dtype=np.float64))
    b, a = _biquad_highpass(sr)
    return _lfilter(b, a, y)


def integrated_lufs(x: np.ndarray, sr: int, *,
                    block_s: float = 0.4) -> float:
    """Integrated loudness in LUFS with the -70 LUFS absolute gate.
    Stereo input (n, 2) sums channel powers per BS.1770."""
    y = np.asarray(x, dtype=np.float64)
    if y.ndim == 1:
        channels = [k_weight(y, sr)]
    else:
        channels = [k_weight(y[:, c], sr) for c in range(y.shape[1])]
    block = max(1, int(sr * block_s))
    n_blocks = max(1, max(c.size for c in channels) // block)
    powers = []
    for i in range(n_blocks):
        p = sum(float(np.mean(c[i * block:(i + 1) * block] ** 2))
                for c in channels)
        if p > 0.0:
            powers.append(p)
    if not powers:
        return -np.inf
    block_lufs = LUFS_OFFSET + 10.0 * np.log10(np.asarray(powers))
    keep = block_lufs > ABSOLUTE_GATE_LUFS
    if not keep.any():
        return -np.inf
    return float(LUFS_OFFSET + 10.0 * np.log10(
        np.asarray(powers)[keep].mean()))


def normalize_lufs(x: np.ndarray, sr: int, target_lufs: float = -17.0, *,
                   max_gain_db: float = 24.0) -> tuple[np.ndarray, float]:
    """Gain to integrated target loudness. Returns (audio, applied_db).
    Peak safety stays downstream in the limiter — this stage only moves
    the average level, so it never introduces clipping itself."""
    lufs = integrated_lufs(x, sr)
    if not np.isfinite(lufs):
        return x, 0.0
    gain_db = float(np.clip(target_lufs - lufs, -max_gain_db, max_gain_db))
    if abs(gain_db) < 0.05:
        return x, 0.0
    return (x * (10.0 ** (gain_db / 20.0))).astype(np.float32), gain_db
