"""Preset-driven DSP chain for voice shaping.

Everything runs on float32 mono PCM at the engine's native rate; the only
lossy step anywhere is an optional MP3 encode at export time. Pure numpy —
no scipy/ffmpeg in the realtime path.

Chain: pitch+tempo (resample + phase-vocoder) → EQ → exciter → compressor
→ parallel synthetic layers (neural / glass / micro, each derived from the
shaped signal) → stereo decorrelation → limiter. The master `synthetic`
slider scales layer mixes and exciter — the dry voice is never removed.
"""
from __future__ import annotations

import numpy as np

from .types import VoicePreset

# Bump when the DSP chain's audible output changes so cached segments
# (keyed on engine/preset, not on post-processing) invalidate instead of
# serving audio synthesized under the old pipeline.
DSP_VERSION = "2"


# ---------------------------------------------------------------------------
# primitives

def _resample(x: np.ndarray, new_len: int) -> np.ndarray:
    """FFT resample — clean for small factors, no librosa dependency."""
    new_len = max(16, int(round(new_len)))
    if new_len == x.size:
        return x.astype(np.float32, copy=False)
    X = np.fft.rfft(x)
    n_out = new_len // 2 + 1
    Y = np.zeros(n_out, dtype=np.complex64)
    copy = min(X.size, n_out)
    Y[:copy] = X[:copy]
    if new_len % 2 == 0 and X.size > n_out - 1 and n_out > 1:
        # Nyquist bin must be real for even lengths.
        if n_out - 1 < X.size:
            Y[n_out - 1] = X[n_out - 1].real + 0j
    y = np.fft.irfft(Y, n=new_len) * (new_len / x.size)
    return y.astype(np.float32)


def _stft(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    pad = n_fft
    xp = np.concatenate([np.zeros(pad, np.float32), x, np.zeros(pad, np.float32)])
    n_frames = 1 + max(0, (xp.size - n_fft) // hop)
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    win = np.hanning(n_fft).astype(np.float32)
    frames = xp[idx] * win
    return np.fft.rfft(frames, axis=1).T  # (bins, frames)


def _istft(D: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    win = np.hanning(n_fft).astype(np.float32)
    frames = np.fft.irfft(D.T, n=n_fft, axis=1) * win
    n = n_fft + hop * (frames.shape[0] - 1)
    out = np.zeros(n, np.float32)
    norm = np.zeros(n, np.float32)
    w2 = win ** 2
    for i, fr in enumerate(frames):
        s = i * hop
        out[s:s + n_fft] += fr
        norm[s:s + n_fft] += w2
    nz = norm > 1e-8
    out[nz] /= norm[nz]
    pad = n_fft
    return out[pad: n - pad] if n > 2 * pad else out


def _phase_vocoder(D: np.ndarray, rate: float, hop: int) -> np.ndarray:
    """Time-stretch magnitude/phase domain. rate<1 lengthens."""
    n_bins, n_cols = D.shape
    time_steps = np.arange(0, n_cols, rate, dtype=np.float64)
    phase_adv = np.linspace(0, np.pi * hop, n_bins)
    out = np.zeros((n_bins, len(time_steps)), dtype=np.complex64)
    phase_acc = np.angle(D[:, 0])
    for t, step in enumerate(time_steps):
        i = int(step)
        if i + 1 >= n_cols:
            i = n_cols - 2
            frac = 0.0
        else:
            frac = step - i
        col_a, col_b = D[:, i], D[:, i + 1]
        mag = (1.0 - frac) * np.abs(col_a) + frac * np.abs(col_b)
        dphi = np.angle(col_b) - np.angle(col_a) - phase_adv
        dphi -= 2.0 * np.pi * np.round(dphi / (2.0 * np.pi))
        phase_acc = phase_acc + phase_adv + dphi
        out[:, t] = mag * np.exp(1j * phase_acc.astype(np.float64))
    return out


def _time_stretch(x: np.ndarray, out_len: int) -> np.ndarray:
    """Stretch x to out_len samples at unchanged pitch via phase vocoder."""
    if abs(out_len - x.size) < 64:
        return _resample(x, out_len)
    rate = x.size / float(out_len)  # source columns advanced per output frame
    n_fft, hop = 2048, 512
    D = _stft(x, n_fft, hop)
    S = _phase_vocoder(D, rate, hop)
    y = _istft(S, n_fft, hop)
    if y.size != out_len:
        y = _resample(y, out_len)
    return y.astype(np.float32)


def pitch_tempo(x: np.ndarray, sr: int, semitones: float, tempo: float,
                formant_preserve: float = 0.0) -> np.ndarray:
    """Shift pitch by semitones and scale duration by 1/tempo.

    resample by pf changes pitch+duration; phase-vocoder restores duration
    to len/tempo while keeping the new pitch. formant_preserve pulls the
    spectral envelope back toward the original (0 = free, 1 = locked).
    """
    pf = 2.0 ** (float(semitones) / 12.0)
    tempo = float(np.clip(tempo, 0.5, 2.0))
    if abs(pf - 1.0) < 1e-4 and abs(tempo - 1.0) < 1e-4:
        return x.astype(np.float32, copy=True)
    orig = x
    rs = _resample(x, x.size / pf)          # pitch shift, duration /pf
    out_len = int(round(x.size / tempo))
    y = _time_stretch(rs, out_len)
    if formant_preserve > 0.0:
        y = _formant_pull(orig, y, formant_preserve)
    return y


def _spectral_envelope(x: np.ndarray, keep: int = 48) -> np.ndarray:
    X = np.fft.rfft(x)
    cep = np.fft.irfft(np.log(np.abs(X) + 1e-8))
    cep[keep: cep.size - keep] = 0.0
    env = np.exp(np.fft.rfft(cep).real)
    return env


def _formant_pull(orig: np.ndarray, shifted: np.ndarray, amount: float) -> np.ndarray:
    """Blend the shifted signal's spectral envelope back toward the original."""
    ref = _resample(orig, shifted.size)
    env_o = _spectral_envelope(ref)
    env_s = _spectral_envelope(shifted)
    ratio = np.clip(env_o / (env_s + 1e-6), 0.25, 4.0)
    gain = ratio ** float(np.clip(amount, 0.0, 1.0))
    Y = np.fft.rfft(shifted) * gain
    return np.fft.irfft(Y, n=shifted.size).astype(np.float32)


def bandpass(x: np.ndarray, sr: int, hpf_hz: float = 0.0, lpf_hz: float = 0.0,
             edge_hz: float = 120.0) -> np.ndarray:
    """Smooth FFT-domain band-limit (raised-cosine edges, no ringing)."""
    if hpf_hz <= 0 and lpf_hz <= 0:
        return x.astype(np.float32, copy=False)
    X = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    gain = np.ones(freqs.size, np.float64)
    if hpf_hz > 0:
        gain *= np.clip((freqs - (hpf_hz - edge_hz)) / max(edge_hz * 2, 1.0), 0.0, 1.0)
        gain *= 0.5 - 0.5 * np.cos(np.pi * np.clip(freqs / max(hpf_hz, 1.0), 0.0, 1.0))
    if lpf_hz > 0:
        t = np.clip((freqs - lpf_hz) / max(edge_hz * 2, 1.0), 0.0, 1.0)
        gain *= 0.5 + 0.5 * np.cos(np.pi * t)
    return np.fft.irfft(X * gain, n=x.size).astype(np.float32)


def peaking_eq(x: np.ndarray, sr: int, bands) -> np.ndarray:
    """FFT-domain peaking EQ — each band (freq_hz, gain_db, q) applies a
    raised-cosine log-frequency bump, wide enough to sound natural."""
    if not bands:
        return x.astype(np.float32, copy=False)
    X = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    lf = np.log2(np.maximum(freqs, 20.0))
    gain_db = np.zeros(freqs.size, np.float64)
    for b in bands:
        f0 = max(float(b.freq_hz), 20.0)
        half_oct = max(0.08, 0.5 / max(float(b.q), 0.2))
        d = (lf - np.log2(f0)) / half_oct
        shape = np.exp(-0.5 * d * d)  # gaussian bump in log-f
        gain_db += float(b.gain_db) * shape
    X *= 10.0 ** (gain_db / 20.0)
    return np.fft.irfft(X, n=x.size).astype(np.float32)


def exciter(x: np.ndarray, sr: int, amount: float) -> np.ndarray:
    """Harmonic exciter: saturate a high band and mix a little back in."""
    amount = float(np.clip(amount, 0.0, 1.0))
    if amount <= 0.0:
        return x
    hi = bandpass(x, sr, hpf_hz=4200.0)
    drive = 1.0 + 6.0 * amount
    sat = np.tanh(hi * drive) / np.tanh(drive)
    return (x + sat * (0.35 * amount)).astype(np.float32)


def compressor(x: np.ndarray, sr: int, spec) -> np.ndarray:
    """Control-rate RMS compressor: fast attack, short release, soft knee."""
    ratio = max(float(spec.ratio), 1.0)
    if ratio <= 1.01:
        return x
    hop = 64
    n_frames = x.size // hop
    if n_frames < 4:
        return x
    frames = x[: n_frames * hop].reshape(n_frames, hop)
    env = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    env_db = 20.0 * np.log10(env + 1e-9)
    thr = float(spec.threshold_db)
    over = np.maximum(env_db - thr, 0.0)
    # Soft knee of 6 dB around threshold.
    knee = 6.0
    below = np.clip((env_db - (thr - knee / 2)) / knee, 0.0, 1.0)
    over = np.maximum(over, below * below * knee * 0.5)
    target_db = -over * (1.0 - 1.0 / ratio)
    att = np.exp(-1.0 / max(1.0, spec.attack_ms * 0.001 * sr / hop))
    rel = np.exp(-1.0 / max(1.0, spec.release_ms * 0.001 * sr / hop))
    gain_db = np.empty(n_frames, np.float64)
    g = 0.0
    for i in range(n_frames):
        target = target_db[i]
        g = target + (g - target) * (att if target < g else rel)
        gain_db[i] = g
    gain = 10.0 ** ((gain_db + float(spec.makeup_db)) / 20.0)
    gain_up = np.repeat(gain, hop)
    out = x[: n_frames * hop] * gain_up
    if x.size > n_frames * hop:
        out = np.concatenate([out, x[n_frames * hop:]])
    return out.astype(np.float32)


def bitcrush(x: np.ndarray, bits: float, wet: float, anti_alias: float,
             sr_reduction: float, sr: int) -> np.ndarray:
    """Controlled bit-depth + sample-rate reduction texture."""
    bits = float(np.clip(bits, 2.0, 24.0))
    levels = 2.0 ** (bits - 1.0)
    crushed = np.round(np.clip(x, -1.0, 1.0) * levels) / levels
    k = max(1, int(round(sr_reduction)))
    if k > 1:
        idx = (np.arange(x.size) // k) * k
        crushed = crushed[np.minimum(idx, x.size - 1)]
    if anti_alias > 0.0 and k > 1:
        # Post-decimation lowpass scaled by anti_alias (0 = raw aliasing).
        cutoff = (sr / (2.0 * k)) * (0.5 + 0.5 * (1.0 - float(np.clip(anti_alias, 0, 1))))
        crushed = bandpass(crushed, sr, lpf_hz=cutoff, edge_hz=200.0)
    wet = float(np.clip(wet, 0.0, 1.0))
    return ((1.0 - wet) * x + wet * crushed).astype(np.float32)


def modulated_delay(x: np.ndarray, sr: int, delay_ms: float, decay: float,
                    rate_hz: float) -> np.ndarray:
    """Short LFO-modulated delay — a subtle phase/chorus texture."""
    n = x.size
    t = np.arange(n, dtype=np.float64)
    center = delay_ms * 0.001 * sr
    depth = min(center * 0.6, 0.6 * 0.001 * sr + center * 0.25)
    d = center + depth * np.sin(2.0 * np.pi * float(rate_hz) * t / sr)
    idx = t - d
    base = np.arange(n, dtype=np.float64)
    delayed = np.interp(idx, base, x).astype(np.float32)
    out = x + float(decay) * delayed
    # one regeneration for the tiny feedback tail
    delayed2 = np.interp(idx - d, base, x).astype(np.float32)
    out += (float(decay) ** 2) * delayed2
    return out.astype(np.float32)


def amplitude_mod(x: np.ndarray, sr: int, rate_hz: float, depth: float) -> np.ndarray:
    depth = float(np.clip(depth, 0.0, 0.95))
    t = np.arange(x.size, dtype=np.float64)
    lfo = (1.0 - depth) + depth * np.sin(2.0 * np.pi * float(rate_hz) * t / sr)
    return (x * lfo).astype(np.float32)


def short_echo(x: np.ndarray, sr: int, delay_ms: float, level: float) -> np.ndarray:
    d = max(1, int(delay_ms * 0.001 * sr))
    out = x.copy()
    if x.size > d:
        out[d:] += float(level) * x[:-d]
    return out


def limiter(x: np.ndarray, sr: int, ceiling: float = 0.89) -> np.ndarray:
    """Lookahead-free peak limiter: per-frame peak target with fast gain
    smoothing — no pumping, no clipping."""
    hop = 64
    n_frames = x.size // hop
    if n_frames < 2:
        return np.clip(x, -ceiling, ceiling).astype(np.float32)
    frames = np.abs(x[: n_frames * hop]).reshape(n_frames, hop)
    peak = frames.max(axis=1)
    target = np.minimum(1.0, float(ceiling) / np.maximum(peak, 1e-6))
    smooth = np.empty(n_frames, np.float64)
    g = 1.0
    a_down = np.exp(-1.0 / (0.001 * sr / hop))   # ~1 ms attack
    a_up = np.exp(-1.0 / (0.050 * sr / hop))     # ~50 ms release
    for i in range(n_frames):
        tgt = target[i]
        g = tgt + (g - tgt) * (a_down if tgt < g else a_up)
        smooth[i] = g
    gain = np.repeat(smooth, hop)
    out = x[: n_frames * hop] * gain
    if x.size > n_frames * hop:
        tail = x[n_frames * hop:]
        out = np.concatenate([out, tail * min(g, 1.0)])
    return np.clip(out, -float(ceiling), float(ceiling)).astype(np.float32)


def trim_tail_artifact(x: np.ndarray, sr: int, *,
                       floor_db: float = -42.0, blip_ms: float = 140.0,
                       blip_min_ms: float = 15.0, gap_drop_db: float = 10.0,
                       min_gap_ms: float = 18.0, max_gap_ms: float = 300.0,
                       pad_ms: float = 8.0,
                       end_slack_ms: float = 350.0,
                       noise_tail_ms: float = 140.0,
                       decay_ms: float = 90.0) -> np.ndarray:
    """Reattach a detached final phoneme — the Kokoro boundary artifact
    heard as a trailing "t"/"d" after a beat of silence at the end of an
    utterance. The island is a real final consonant emitted late: speech
    decays, a deep dip follows, then a short loud burst sits stranded
    before the clip ends. Cutting the island could eat a real consonant,
    so we splice the gap out instead — the phoneme lands back on the word
    and reads as a normal ending.

    Detection is dip-relative, not floor-absolute: Kokoro's inter-phoneme
    silence carries breath/noise around -30..-40 dB, above any absolute
    floor, so the gap is measured against the island's own level.
    Ordinary tails (long final run, no dip, island far from the end)
    pass through untouched.

    A second artifact is handled first: the breathy noise bed Kokoro
    leaves decaying for hundreds of ms after the real last phoneme —
    audible as a trailing hiss. Everything after the last -22 dB run is
    below the level any real consonant reaches, so a residue longer than
    ``noise_tail_ms`` is pure artifact and gets shaved to a short decay.
    Syllable-length detached islands are deliberately NOT cut: real
    quiet final words are DSP-indistinguishable from stray syllables,
    and eating a real word is worse than leaving an artifact."""
    if x.ndim != 1 or x.size < int(sr * 0.3):
        return x
    frame = max(1, int(sr * 0.01))                    # 10 ms frames
    n = x.size // frame
    if n < 8:
        return x
    env = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
    peak = float(env.max())
    if peak <= 1e-6:
        return x
    voiced = env > peak * (10.0 ** (floor_db / 20.0))
    if not voiced.any():
        return x
    last = n - 1 - int(voiced[::-1].argmax())          # last voiced frame
    # Segment the island at a higher level — the dip separating it from
    # speech carries Kokoro breath noise around -30..-40 dB, above the
    # absolute floor, so the island never forms its own -42 dB run.
    # Find the last "strong" run (>= -22 dB), then extend it down to the
    # last voiced frame so a decaying release tail counts as one island.
    strong = env > peak * (10.0 ** (-22.0 / 20.0))
    if not strong.any():
        return x
    last_strong = n - 1 - int(strong[::-1].argmax())
    # Noise tail: Kokoro lets a breathy noise bed decay for hundreds of ms
    # after the real last phoneme — audible as a trailing hiss/mumble.
    # Everything after the last strong run is below -22 dB (no real
    # consonant lives down there), so a long residue is pure artifact:
    # shave it to a short natural decay, then judge what remains.
    if (last - last_strong) * 10 > noise_tail_ms:
        keep = last_strong * frame + int(sr * decay_ms / 1000.0)
        keep = min(keep, x.size)
        tail_len = min(keep - 1, int(sr * 0.03))
        trimmed = x[:keep].copy()
        if tail_len > 1:
            trimmed[-tail_len:] *= np.linspace(1.0, 0.0, tail_len)
        x = trimmed
        n = x.size // frame
        if n < 8:
            return x
        env = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
        peak = float(env.max())
        if peak <= 1e-6:
            return x
        voiced = env > peak * (10.0 ** (floor_db / 20.0))
        if not voiced.any():
            return x
        last = n - 1 - int(voiced[::-1].argmax())
        strong = env > peak * (10.0 ** (-22.0 / 20.0))
        if not strong.any():
            return x
        last_strong = n - 1 - int(strong[::-1].argmax())
    if (n - 1 - last) * 10 > end_slack_ms:
        return x                                       # island not at the end
    run_start = last_strong
    while run_start > 0 and strong[run_start - 1]:
        run_start -= 1
    run_ms = (last - run_start + 1) * 10
    if not blip_min_ms <= run_ms <= blip_ms:
        return x                                       # real speech tail
    # Measure the dip before it — contiguous frames below the island's
    # own level. A genuine word-final plosive attaches to the vowel; a
    # detached artifact sits behind a real gap.
    run_peak = float(env[run_start:last + 1].max())
    gap_floor = run_peak * (10.0 ** (-gap_drop_db / 20.0))
    gap_start = run_start
    while gap_start > 0 and env[gap_start - 1] <= gap_floor:
        gap_start -= 1
    gap_ms_meas = (run_start - gap_start) * 10
    if not min_gap_ms <= gap_ms_meas <= max_gap_ms or gap_start == 0:
        return x                                       # attached or too far
    join = gap_start * frame + int(sr * pad_ms / 1000.0)
    pre = x[:join].copy()
    # Island character decides the remedy. A noise burst (plosive
    # release — high ZCR, high spectral centroid) is a real consonant
    # emitted late: reattach it. A voiced fragment (vowel-ish, low
    # ZCR) is a stray syllable — a dangling "ha"/"heh": cut it.
    island = x[run_start * frame:(last + 1) * frame]
    if island.size > 2:
        zcr = float(np.mean(island[:-1] * island[1:] < 0.0))
        spec = np.abs(np.fft.rfft(island * np.hanning(island.size)))
        freqs = np.fft.rfftfreq(island.size, 1.0 / sr)
        centroid = float((spec * freqs).sum() / max(spec.sum(), 1e-9))
        if zcr < 0.12 and centroid < 3200.0:
            fade = min(pre.size, int(sr * 0.006))
            if fade > 1:
                pre[-fade:] *= np.linspace(1.0, 0.0, fade)
            return pre
    # Splice: drop the gap, keep the island, fade both joints.
    post = x[run_start * frame:].copy()
    fade = min(int(sr * 0.004), pre.size, post.size)
    if fade > 1:
        pre[-fade:] *= np.linspace(1.0, 0.0, fade)
        post[:fade] *= np.linspace(0.0, 1.0, fade)
    return np.concatenate([pre, post])


def to_stereo_decorrelated(main: np.ndarray, layers: list[np.ndarray], sr: int,
                           width: float) -> np.ndarray:
    """Main stays centered; each parallel layer gets alternating micro-delay
    between L/R for modest decorrelated width. width scales the side image."""
    n = main.size
    left = main.astype(np.float64)
    right = main.astype(np.float64)
    base = np.arange(n, dtype=np.float64)
    for i, layer in enumerate(layers):
        if layer.size != n:
            layer = _resample(layer, n)
        shift_ms = 0.4 + 0.25 * i  # alternating side delay
        d = shift_ms * 0.001 * sr
        t = np.arange(n, dtype=np.float64)
        if i % 2 == 0:
            l_r = np.interp(t - d, base, layer)
            left += layer
            right += l_r
        else:
            l_l = np.interp(t - d, base, layer)
            right += layer
            left += l_l
    width = float(np.clip(width, 0.5, 2.0))
    mid = (left + right) * 0.5
    side = (left - right) * 0.5 * width
    L = mid + side
    R = mid - side
    return np.stack([L, R], axis=1).astype(np.float32)


# ---------------------------------------------------------------------------
# preset processing

def process_mono(x: np.ndarray, sr: int, preset: VoicePreset,
                 synthetic: float | None = None) -> np.ndarray:
    """Run the preset's full mono chain (shaping + layers mixed in).

    `synthetic` (0..1) scales layer mixes and exciter — at 0 the output is
    nearly the raw engine voice (gentle safety EQ/limiter still applies).
    """
    s = float(preset.synthetic if synthetic is None else synthetic)
    s = float(np.clip(s, 0.0, 1.0))
    x = np.asarray(x, dtype=np.float32)

    # Shaped main vocal -----------------------------------------------------
    main = pitch_tempo(x, sr, preset.pitch_semitones, preset.tempo,
                       formant_preserve=preset.formant_preserve)
    # EQ scales gently with synthetic so 0% is closer to raw.
    if preset.eq:
        scaled = [type(b)(freq_hz=b.freq_hz, gain_db=b.gain_db * (0.35 + 0.65 * s),
                          q=b.q) for b in preset.eq]
        main = peaking_eq(main, sr, scaled)
    if preset.exciter > 0.0:
        main = exciter(main, sr, preset.exciter * (0.25 + 0.75 * s))
    if preset.compression.ratio > 1.01:
        main = compressor(main, sr, preset.compression)
    if preset.highpass_hz > 0 or preset.lowpass_hz > 0:
        main = bandpass(main, sr, preset.highpass_hz, preset.lowpass_hz)
    if preset.ambience_ms > 0:
        main = short_echo(main, sr, preset.ambience_ms, 0.18)

    # Parallel synthetic layers — always derived from the shaped mono signal.
    if s <= 0.001:
        return main

    parts: list[np.ndarray] = []

    if preset.neural.enabled and preset.neural.mix > 0:
        lay = bandpass(main, sr, preset.neural.hpf_hz, preset.neural.lpf_hz)
        lay = bitcrush(lay, preset.neural.bit_depth, preset.neural.wet,
                       preset.neural.anti_alias, preset.neural.sample_reduction, sr)
        lay = modulated_delay(lay, sr, preset.neural.delay_ms,
                              preset.neural.decay, preset.neural.mod_rate_hz)
        lay = amplitude_mod(lay, sr, preset.neural.am_rate_hz, preset.neural.am_depth)
        parts.append(lay * (preset.neural.mix * s))

    if preset.glass.enabled and preset.glass.mix > 0:
        lay = bandpass(main, sr, preset.glass.hpf_hz, preset.glass.lpf_hz)
        lay = pitch_tempo(lay, sr, 12.0 * np.log2(max(preset.glass.pitch_factor, 0.01)),
                          1.0, formant_preserve=preset.glass.formant_preserve)
        lay = short_echo(lay, sr, preset.glass.delay_ms, preset.glass.echo)
        parts.append(lay * (preset.glass.mix * s))

    if preset.micro.enabled and preset.micro.mix > 0:
        lay = bandpass(main, sr, preset.micro.hpf_hz, preset.micro.lpf_hz)
        lay = pitch_tempo(lay, sr, 12.0 * np.log2(max(preset.micro.pitch_factor, 0.01)),
                          1.0, formant_preserve=preset.micro.formant_preserve)
        lay = bitcrush(lay, preset.micro.bit_depth, preset.micro.wet, 0.5, 1.0, sr)
        lay = short_echo(lay, sr, preset.micro.delay_ms, 0.20)
        parts.append(lay * (preset.micro.mix * s))

    mixed = main.copy()
    for p in parts:
        mixed += p
    return mixed.astype(np.float32)


def process(x: np.ndarray, sr: int, preset: VoicePreset,
            synthetic: float | None = None) -> np.ndarray:
    """Full chain → stereo, gain, limiter. Returns (n, 2) float32."""
    main = process_mono(x, sr, preset, synthetic)
    # Re-run layer busses for stereo decorrelation: cheaper approximation is
    # re-deriving layers here via the same mono parts — to keep it simple we
    # decorrelate the *difference* between mixed and main.
    if preset.stereo_width and preset.stereo_width > 0.0 and (
            (preset.synthetic if synthetic is None else synthetic) > 0.001):
        # layers signal = mixed - main is embedded; rebuild decorrelated stereo
        # by mixing main center + side-decorrelated residual.
        # process_mono already mixed; recompute residual via second pass is
        # wasteful — instead process_mono returns mono mix; build stereo by
        # decorrelating a high-band copy for width.
        s = float(preset.synthetic if synthetic is None else synthetic)
        residual_band = bandpass(main, sr, hpf_hz=1500.0, lpf_hz=10000.0)
        stereo = to_stereo_decorrelated(main, [residual_band * 0.30 * s], sr,
                                        preset.stereo_width)
    else:
        stereo = np.stack([main, main], axis=1).astype(np.float32)
    gain = 10.0 ** (float(preset.output_gain_db) / 20.0)
    if gain != 1.0:
        stereo = stereo * gain
    if getattr(preset, "normalize_loudness", False):
        from . import loudness as _loud
        stereo, _ = _loud.normalize_lufs(
            stereo, sr,
            float(getattr(preset, "loudness_target_lufs", -14.0)),
            max_gain_db=15.0)
    if getattr(preset, "limiter_enabled", True):
        stereo[:, 0] = limiter(stereo[:, 0], sr, preset.limiter_ceiling)
        stereo[:, 1] = limiter(stereo[:, 1], sr, preset.limiter_ceiling)
    return stereo.astype(np.float32)


# ---------------------------------------------------------------------------
# quality metrics — used by the stochastic-draw gate in VoiceManager and
# mirrored by scripts/eval_isabella_v7.py (keep the math in sync).

def spectral_centroid_hz(x: np.ndarray, sr: int) -> float:
    x = np.asarray(x, dtype=np.float32)
    if x.size < 32:
        return 0.0
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    return float((spec * freqs).sum() / max(spec.sum(), 1e-12))


def band_share_db(x: np.ndarray, sr: int,
                  lo_hz: float, hi_hz: float) -> float:
    """Energy share of a band relative to the whole spectrum, in dB.
    Used by the draw gate to reject boomy renders — the golden's
    100–200 Hz share is ≈ −21 dB; bad draws sit ~6–14 dB higher."""
    x = np.asarray(x, dtype=np.float32)
    if x.size < 32:
        return -120.0
    spec = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    band = spec[(freqs >= lo_hz) & (freqs < hi_hz)].sum()
    return float(10.0 * np.log10(band / max(spec.sum(), 1e-12) + 1e-12))


def echo_lag_corr(x: np.ndarray, sr: int,
                  lo_ms: float = 4.0, hi_ms: float = 80.0) -> float:
    """Peak normalized autocorrelation at echo-lag delays — room/comb
    energy shows up as a strong second peak well after lag 0."""
    x = np.asarray(x, dtype=np.float32) - np.mean(x)
    n = min(x.size, int(sr * 4))
    x = x[:n]
    if n < int(sr * hi_ms / 1000):
        return 0.0
    ac = np.correlate(x, x, mode="full")[n - 1:]
    ac /= max(ac[0], 1e-12)
    lo, hi = int(sr * lo_ms / 1000), int(sr * hi_ms / 1000)
    return float(ac[lo:hi].max()) if hi < ac.size else 0.0


def wav_bytes(pcm: np.ndarray, sr: int) -> bytes:
    """float PCM → 16-bit PCM WAV bytes (stdlib, no dependency)."""
    import io
    import wave
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    clipped = np.clip(pcm, -1.0, 1.0)
    i16 = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(i16.tobytes())
    return buf.getvalue()
