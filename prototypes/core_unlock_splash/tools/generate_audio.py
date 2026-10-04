"""Original deterministic sound design. Requires NumPy only when regenerating assets.

No recordings, external samples, pretrained models, or franchise material are used.
48 kHz stereo PCM16. Seeds, oscillator frequencies and envelopes are all below.
"""
from pathlib import Path
import hashlib
import json
import math
import wave
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RATE = 48000
TAU = 2 * math.pi


def time(seconds):
    return np.arange(round(seconds * RATE), dtype=np.float64) / RATE


def tone(t, frequency, phase=0):
    return np.sin(TAU * frequency * t + phase)


def sweep(t, start, end, duration):
    return np.sin(TAU * (start * t + .5 * (end - start) / duration * t * t))


def envelope(t, attack=.04, release=.1):
    duration = (len(t) - 1) / RATE
    return np.minimum(1, t / attack) ** 2 * np.minimum(1, np.maximum(0, duration - t) / release) ** 2


def noise(t, seed, low, high):
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(len(t)))
    f = np.fft.rfftfreq(len(t), 1 / RATE)
    spectrum *= (1 - np.exp(-(f / low) ** 4)) * np.exp(-(f / high) ** 4)
    out = np.fft.irfft(spectrum, n=len(t))
    return out / max(1e-10, np.max(np.abs(out)))


def impact(t, start, base, decay=.06):
    u = np.maximum(0, t - start)
    modes = tone(u, base) + .45 * tone(u, base * 2.71) + .22 * tone(u, base * 4.13)
    return np.where(t >= start, modes * np.exp(-u / decay) * np.minimum(1, u / .0015), 0)


def stereo(signal, side, width=.12):
    return np.column_stack((signal + side * width, signal - side * width))


def clack(seed, size=1):
    """A contact impulse, case resonance, and spring return; never a chirp/beep.

    Broadband metal contact lasts ~2 ms, the low case knock ~9 ms. Short,
    inharmonic resonances decay before they can read as musical notes.
    """
    u = time(.07 * size)
    onset = 1 - np.exp(-u / .00012)
    snap = noise(u, seed, 1000 / size, 14000) * np.exp(-u / (.0013 * size))
    body = noise(u, seed + 1, 180 / size, 2400 / size) * np.exp(-u / (.0065 * size))
    case = (.33 * tone(u, 1700 / size) + .19 * tone(u, 3271 / size) + .1 * tone(u, 5813 / size)) * np.exp(-u / (.0019 * size))
    release_time = .008 * size
    r = np.maximum(0, u - release_time)
    spring = np.where(u > release_time, noise(u, seed + 2, 600 / size, 6800 / size) * np.exp(-r / (.0025 * size)), 0)
    return onset * (.85 * snap + .9 * body + case) + .38 * spring


def place(destination, source, seconds, gain=1):
    start = round(seconds * RATE)
    count = min(len(source), len(destination) - start)
    if count > 0:
        destination[start:start + count] += source[:count] * gain


def normalize(data, peak):
    # DC subtraction, then fixed sensible peak. Global mix is checked again below.
    data = data - data.mean(axis=0)
    return data * peak / max(1e-12, np.max(np.abs(data)))


def write_wav(path, data):
    assert np.max(np.abs(data)) < 1, path
    pcm = np.round(data * 32767).astype('<i2')
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(2); f.setsampwidth(2); f.setframerate(RATE); f.writeframes(pcm.tobytes())


def build():
    stems = {}
    t = time(4)
    # Periodic oscillators and periodic FFT noise yield sample-continuous loops.
    ambient = .64 * tone(t, 40) + .26 * tone(t, 80) + .09 * tone(t, 120) + .045 * noise(t, 101, 80, 480)
    stems['ambient_hum'] = normalize(stereo(ambient, tone(t, 60) * .3, .1), .17)
    stable = .6 * tone(t, 44) + .28 * tone(t, 88) + .12 * tone(t, 132) + .08 * tone(t, 220)
    stems['stable_hum'] = normalize(stereo(stable, tone(t, 176) * (.4 + .2 * tone(t, .5)), .24), .28)
    # Full-power loading plateau. All frequencies and modulation complete integer
    # cycles in four seconds so an arbitrarily long readiness wait has no seam.
    full = (.67 * tone(t, 52) + .32 * tone(t, 104) + .26 * tone(t, 208)
            + .13 * tone(t, 416) + .085 * tone(t, 832)) * (.9 + .1 * tone(t, 5.5))
    stems['charged_hum'] = normalize(stereo(full, tone(t, 312), .21), .6)

    t = time(.85); signal = np.zeros_like(t)
    for i, at in enumerate([.02, .19, .37, .56]):
        u = np.maximum(t - at, 0)
        signal += np.where(t >= at, .23 * tone(u, 700 + i * 145) * np.exp(-u / .035), 0)
        signal += .28 * impact(t, at, 330 + i * 40, .018)
    stems['security_activate'] = normalize(stereo(signal * envelope(t, .006, .08), noise(t, 111, 1300, 4000), .01), .25)
    t = time(.55)
    auth = (tone(t, 392) + .45 * tone(t, 588) + .2 * tone(t, 784)) * np.sin(np.pi * np.minimum(1, t / .4)) ** 2
    stems['authorization'] = normalize(stereo(auth * envelope(t), tone(t, 784) * envelope(t), .06), .26)

    # Dry metal pin/tumbler contacts: no sine chirps or long resonant electronic tones.
    t = time(1.3); tumblers = np.zeros_like(t)
    for i, at in enumerate([.015, .13, .26, .38, .49, .585, .67, .75, .825, .895, .96, 1.025, 1.09, 1.155, 1.225]):
        place(tumblers, clack(700 + i * 5, .82 + (i % 4) * .11), at, .75 + (i % 3) * .12)
    tumblers *= envelope(t, .003, .018)
    stems['tumbler_clicks'] = normalize(stereo(tumblers, tumblers * tone(t, .75), .07), .55)

    t = time(.8)
    motion = np.sin(np.pi * t / .8) ** 1.4
    # Bearing drag, gear teeth and a heavy end stop. No high-tech motor melody.
    lock = (.2 * noise(t, 202, 65, 600) + .085 * noise(t, 203, 450, 3600)) * motion
    for at in [.07, .16, .29, .41, .53, .64]:
        place(lock, clack(round(at * 1000) + 900, 1.1), at, .2)
    place(lock, clack(1101, 2.1), .705, 1)
    place(lock, clack(1109, .75), .765, .18)
    stems['lock_turn'] = normalize(stereo(lock * envelope(t, .02, .012), noise(t, 207, 100, 650) * motion, .06), .46)
    for i in range(4):
        t = time(.28)
        pin = noise(t, 301 + i, 300, 4300) * np.exp(-t / .016) * .13
        place(pin, clack(1201 + i * 9, .8), .009, .45)
        place(pin, clack(1204 + i * 9, 1.8 + i * .1), .13, 1)
        place(pin, clack(1207 + i * 9, .65), .19, .2)
        stems[f'pin_{i + 1:02}'] = normalize(stereo(pin * envelope(t, .002, .03), np.zeros_like(t)), .39)

    t = time(.8); u = t / .8; motion = 6 * u * (1 - u)
    ring = np.zeros_like(t); ring_side = np.zeros_like(t)
    # Match the exact 72/60/48-tooth rings, signed angles, and smoothstep easing
    # in renderer/timeline. Every tooth contact follows angular velocity, including
    # the slowdown into alignment. Add bearing rumble and sliding metal friction.
    for gear, (teeth, angle, pan) in enumerate([(72, .6, -.13), (60, -.85, .13), (48, 1.1, -.06)]):
        position = u * u * (3 - 2 * u) * teeth * abs(angle) / TAU
        contacts = np.flatnonzero(np.diff(np.floor(position), prepend=0) > 0)
        layer = np.zeros_like(t)
        for j, index in enumerate(contacts):
            place(layer, clack(1400 + gear * 100 + j * 4, .65 + gear * .12), index / RATE, .33)
        friction = (.12 * noise(t, 1600 + gear, 90, 1300) + .04 * noise(t, 1610 + gear, 800, 6300)) * motion
        layer += friction
        ring += layer; ring_side += layer * pan
    place(ring, clack(1701, 1.65), .735, .8)
    stems['ring_rotation'] = normalize(stereo(ring * envelope(t, .012, .012), ring_side * envelope(t, .012, .012), 1), .48)
    t = time(.95); motion = np.sin(np.pi * np.minimum(1, t / .9))
    iris = (.48 * sweep(t, 64, 115, .95) + .22 * sweep(t, 210, 320, .95) + .32 * noise(t, 501, 180, 1900)) * motion
    iris += .21 * impact(t, .04, 140, .08) + .34 * impact(t, .84, 110, .07)
    stems['iris_open'] = normalize(stereo(iris * envelope(t, .02, .05), noise(t, 502, 300, 1000) * motion, .13), .43)

    t = time(4.75); charge = np.minimum(1, t / 4)
    base = .67 * sweep(t, 38, 52, 4.75) + .32 * sweep(t, 76, 104, 4.75)
    harmonics = .26 * sweep(t, 152, 208, 4.75) + .13 * sweep(t, 304, 416, 4.75) + .085 * sweep(t, 608, 832, 4.75)
    reactor = (base + harmonics * charge) * (.9 + .1 * tone(t, 5.5)) * (.32 + .68 * charge) * envelope(t, .18, .65)
    side = sweep(t, 228, 312, 4.75) * envelope(t, .3, .65)
    stems['core_charge'] = normalize(stereo(reactor, side, .03 + .18 * charge), .6)
    t = time(2); env = envelope(t, .9, .7)
    shimmer = (.3 * sweep(t, 440, 660, 2) + .12 * sweep(t, 880, 1320, 2) + .055 * noise(t, 601, 2500, 6500)) * env
    stems['energy_swell'] = normalize(stereo(shimmer, tone(t, 660) * env, .1), .27)
    t = time(.9)
    pulse = (.7 * sweep(t, 72, 40, .9) * np.exp(-t / .19) + .19 * tone(t, 528) * np.exp(-t / .26) + .09 * tone(t, 792) * np.exp(-t / .2)) * envelope(t, .025, .12)
    stems['core_online'] = normalize(stereo(pulse, tone(t, 1056) * np.exp(-t / .18) * envelope(t), .035), .48)

    manifest = json.loads((ROOT / 'animation_manifest.json').read_text())
    preview_seconds = 17
    mix = np.zeros((preview_seconds * RATE, 2), dtype=np.float64)
    for event in manifest['events']:
        if 'sound' not in event:
            continue
        data = stems[event['sound']]
        start = round(event['at'] * RATE)
        end = min(len(mix), round(event.get('until', preview_seconds if event.get('loop') else event['at'] + len(data) / RATE) * RATE))
        count = end - start
        data = np.tile(data, (math.ceil(count / len(data)), 1))[:count].copy()
        fade_in = min(count, round(event.get('fadeIn', .006) * RATE))
        fade_out = min(count, round(event.get('fadeOut', .015) * RATE))
        data[:fade_in] *= np.linspace(0, 1, fade_in)[:, None]
        if end < len(mix): data[-fade_out:] *= np.linspace(1, 0, fade_out)[:, None]
        # Match Web Audio's stereo panner for these narrow stereo sources.
        p = event.get('pan', 0)
        if p > 0:
            left = data[:, 0].copy(); data[:, 0] *= math.cos(p * math.pi / 2); data[:, 1] += left * math.sin(p * math.pi / 2)
        elif p < 0:
            right = data[:, 1].copy(); data[:, 1] *= math.cos(-p * math.pi / 2); data[:, 0] += right * math.sin(-p * math.pi / 2)
        mix[start:end] += data * event.get('gain', .6)
    scale = min(1, .85 / np.max(np.abs(mix)))
    mix *= scale
    # Preview tails off for distribution; runtime stable hum loops until disposal.
    mix[-RATE:] *= np.linspace(1, 0, RATE)[:, None]
    audio_dir = ROOT / 'audio'; audio_dir.mkdir(exist_ok=True)
    report = {'sample_rate': RATE, 'channels': 2, 'bits': 16, 'seed_policy': 'Fixed integer seeds; original oscillator/noise synthesis', 'global_scale': float(scale), 'recommended_volume': manifest['defaultVolume'], 'mix_peak_at_volume_1': float(np.max(np.abs(mix))), 'mix_peak_default_dbfs': float(20 * np.log10(np.max(np.abs(mix)) * manifest['defaultVolume'])), 'stems': {}}
    for name, data in stems.items():
        data *= scale; path = audio_dir / f'{name}.wav'; write_wav(path, data)
        report['stems'][name] = {'seconds': len(data) / RATE, 'peak_dbfs': float(20 * np.log10(np.max(np.abs(data)))), 'rms_dbfs': float(20 * np.log10(np.sqrt(np.mean(data ** 2)))), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    write_wav(audio_dir / 'mixed_preview.wav', mix * manifest['defaultVolume'])
    (audio_dir / 'mix_report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'stems'}, indent=2))


if __name__ == '__main__':
    build()
