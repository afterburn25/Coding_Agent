"""Original containment sound design; preserves all existing normal-startup stems.

Run after generate_audio.py when rebuilding the full sound library. Requires NumPy.
No recordings or third-party samples. See failure_mix_report.json for interruption
mixes at multiple normal timeline positions, including the outgoing 160 ms fade.
"""
import hashlib
import json
import math
import wave
import numpy as np
from generate_audio import ROOT, RATE, TAU, time, tone, sweep, noise, envelope, stereo, normalize, clack, place, write_wav


def read(name):
    with wave.open(str(ROOT / 'audio' / f'{name}.wav')) as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype='<i2').reshape(-1, 2).astype(float) / 32767


def render(events, stems, seconds):
    out = np.zeros((round(seconds * RATE), 2))
    for e in events:
        if 'sound' not in e:
            continue
        data = stems[e['sound']]
        start = round(e['at'] * RATE)
        end = min(len(out), round(e.get('until', seconds if e.get('loop') else e['at'] + len(data) / RATE) * RATE))
        n = end - start
        if n <= 0:
            continue
        data = np.tile(data, (math.ceil(n / len(data)), 1))[:n].copy()
        attack = min(n, round(e.get('fadeIn', .006) * RATE))
        release = min(n, round(e.get('fadeOut', .015) * RATE))
        data[:attack] *= np.linspace(0, 1, attack)[:, None]
        if end < len(out):
            data[-release:] *= np.linspace(1, 0, release)[:, None]
        pan = e.get('pan', 0)
        if pan > 0:
            left = data[:, 0].copy(); data[:, 0] *= math.cos(pan * math.pi / 2); data[:, 1] += left * math.sin(pan * math.pi / 2)
        elif pan < 0:
            right = data[:, 1].copy(); data[:, 1] *= math.cos(-pan * math.pi / 2); data[:, 0] += right * math.sin(-pan * math.pi / 2)
        out[start:end] += data * e.get('gain', .6)
    return out


def build():
    stems = {}
    t = time(3.5)
    # Bounded frequency modulation: the extended hold never runs away in pitch.
    phase = TAU * 52 * t + (2.6 / 3.1) * np.sin(TAU * 3.1 * t)
    unstable = (.64 * np.sin(phase) + .29 * np.sin(2 * phase + .2 * tone(t, 4.3))
                + .18 * tone(t, 209) + .10 * tone(t, 414)) * (.86 + .14 * tone(t, 5.7))
    stems['core_instability'] = normalize(stereo(unstable * envelope(t, .08, .14), tone(t, 313) * envelope(t), .13), .47)
    t = time(.36); warning = np.zeros_like(t)
    for at in [.01, .18]:
        u = np.maximum(0, t - at)
        warning += np.where((t >= at) & (u < .10), (tone(u, 540) + .22 * tone(u, 810)) * np.sin(np.pi * np.minimum(u / .10, 1)) ** 2, 0)
    stems['failure_warning'] = normalize(stereo(warning, np.zeros_like(t)), .24)
    t = time(2.95)
    crackle = noise(t, 2201, 1200, 5500) * (.2 + .8 * np.maximum(0, tone(t, 11.3)) ** 12)
    electric = (.42 * tone(t, 177) * tone(t, 4.7) + .12 * crackle) * envelope(t, .08, .2)
    stems['electrical_instability'] = normalize(stereo(electric, noise(t, 2202, 800, 3000) * envelope(t), .035), .20)
    t = time(.95); decay = envelope(t, .025, .27)
    down = (.68 * sweep(t, 52, 24, .95) + .26 * sweep(t, 208, 64, .95) + .09 * sweep(t, 832, 145, .95)) * decay
    stems['emergency_powerdown'] = normalize(stereo(down, sweep(t, 312, 80, .95) * decay, .17 * (1 - t / .95)), .46)
    t = time(.70); velocity = np.sin(np.pi * np.minimum(1, t / .6)) ** .7
    plates = (.42 * noise(t, 2301, 90, 1600) + .12 * noise(t, 2302, 700, 6200)
              + .18 * sweep(t, 88, 55, .70)) * velocity
    place(plates, clack(2305, 1.7), .04, .55)
    place(plates, clack(2310, 2.2), .56, .8)
    stems['emergency_iris_close'] = normalize(stereo(plates * envelope(t, .008, .04), noise(t, 2315, 150, 900) * velocity, .1), .45)
    # Pin carriage starts first; the dry clunk lands at the 130 ms visual end stop.
    for i in range(4):
        t = time(.29); signal = .055 * noise(t, 2400 + i, 180, 1400) * envelope(t, .01, .17)
        place(signal, clack(2420 + i * 7, 1.85 + i * .07), .13)
        stems[f'failure_pin_{i + 1:02d}'] = normalize(stereo(signal, np.zeros_like(t)), .43)
    t = time(.41); lock = .11 * noise(t, 2501, 130, 2100) * np.sin(np.pi * np.minimum(1, t / .22))
    for i, at in enumerate([.015, .053, .09, .12]):
        place(lock, clack(2510 + i * 3, .75), at, .26)
    place(lock, clack(2530, 2.3), .22, .9)
    stems['failure_ring_lock'] = normalize(stereo(lock * envelope(t, .005, .04), np.zeros_like(t)), .44)
    t = time(.55)
    thump = (.75 * sweep(t, 65, 37, .55) * np.exp(-t / .09) + .07 * noise(t, 2601, 110, 650) * np.exp(-t / .04)) * envelope(t, .008, .1)
    stems['fault_contained'] = normalize(stereo(thump, np.zeros_like(t)), .40)
    t = time(4)
    idle = .7 * tone(t, 36) + .21 * tone(t, 72) + .055 * tone(t, 144)
    stems['emergency_idle_hum'] = normalize(stereo(idle, tone(t, 108), .045), .13)

    manifest = json.loads((ROOT / 'animation_manifest.json').read_text())
    audio_dir = ROOT / 'audio'
    report = json.loads((audio_dir / 'mix_report.json').read_text())
    for name, data in stems.items():
        path = audio_dir / f'{name}.wav'; write_wav(path, data)
        report['stems'][name] = {'seconds': len(data) / RATE, 'peak_dbfs': float(20 * np.log10(np.max(np.abs(data)))),
                               'rms_dbfs': float(20 * np.log10(np.sqrt(np.mean(data ** 2)))), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    all_stems = {s: read(s) for s in manifest['sounds']}
    failure = render(manifest['failure']['events'], all_stems, 9)
    peaks = {}
    for at in [0, 1.9, 2.8, 3.4, 5.15, 7.8, 9.6, 11.79, 12.3]:
        n = round(at * RATE); tail = round(.16 * RATE)
        mix = render([e for e in manifest['events'] if e['at'] <= at], all_stems, at + 9)
        mix[n:n + tail] *= np.linspace(1, 0, tail)[:, None]; mix[n + tail:] = 0
        mix[n:n + len(failure)] += failure
        peaks[str(at)] = float(np.max(np.abs(mix)))
        if at == 7.8:
            mix[-RATE:] *= np.linspace(1, 0, RATE)[:, None]
            write_wav(audio_dir / 'mixed_failure_preview.wav', mix * manifest['defaultVolume'])
    assert max(peaks.values()) < .86, peaks
    assert max(peaks.values()) <= report['mix_peak_at_volume_1'] * 1.05, 'Failure is louder than normal startup'
    report['failure_standalone_peak'] = float(np.max(np.abs(failure)))
    (audio_dir / 'mix_report.json').write_text(json.dumps(report, indent=2) + '\n')
    measurements = {'sample_rate': RATE, 'outgoing_fade_seconds': .16, 'failure_seconds': manifest['failure']['duration'],
                    'failure_peak_at_volume_1': float(np.max(np.abs(failure))), 'interruption_peaks_at_volume_1': peaks,
                    'note': 'Conservative mix plays all containment stems at full event gain, even when closed parts would suppress them at runtime.'}
    (audio_dir / 'failure_mix_report.json').write_text(json.dumps(measurements, indent=2) + '\n')
    print(json.dumps(measurements, indent=2))


if __name__ == '__main__':
    build()
