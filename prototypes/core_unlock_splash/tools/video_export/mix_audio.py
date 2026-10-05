import json
import math
import wave
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
RATE = 48000
m = json.loads((ROOT / 'animation_manifest.json').read_text(encoding='utf-8'))
stems = {}
for key, filename in m['sounds'].items():
    with wave.open(str(ROOT / filename)) as wav:
        assert wav.getframerate() == RATE and wav.getnchannels() == 2 and wav.getsampwidth() == 2
        stems[key] = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').reshape(-1, 2).astype(float) / 32767

def render(events, seconds):
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
            left = data[:, 0].copy()
            data[:, 0] *= math.cos(pan * math.pi / 2)
            data[:, 1] += left * math.sin(pan * math.pi / 2)
        elif pan < 0:
            right = data[:, 1].copy()
            data[:, 1] *= math.cos(-pan * math.pi / 2)
            data[:, 0] += right * math.sin(-pan * math.pi / 2)
        out[start:end] += data * e.get('gain', .6)
    return out

def write(name, signal):
    signal[-RATE:] *= np.linspace(1, 0, RATE)[:, None]
    signal *= m['defaultVolume']
    assert np.max(np.abs(signal)) < 1
    with wave.open(str(OUT / name), 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes((signal * 32767).astype('<i2').tobytes())
    return {'seconds': len(signal) / RATE, 'peak': float(np.max(np.abs(signal)))}

if __name__ == '__main__':
    normal = render(m['events'], 17)
    report = {'normal': write('Startup.wav', normal)}
    (OUT / 'audio-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
