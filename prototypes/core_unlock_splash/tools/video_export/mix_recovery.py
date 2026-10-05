import json
import subprocess
import wave
import numpy as np
from mix_audio import OUT, ROOT, RATE, m, stems, render, write

config = json.loads((OUT / 'recovery.json').read_text(encoding='utf-8'))

def retime(key, tempo):
    target = OUT / ('recovery_' + key + '.wav')
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', str(ROOT / m['sounds'][key]), '-af', f'atempo={tempo}',
                    '-ar', str(RATE), '-ac', '2', '-c:a', 'pcm_s16le', str(target)],
                   check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    with wave.open(str(target)) as source:
        stems['recovery_' + key] = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').reshape(-1, 2).astype(float) / 32767

retime('iris_open', .6)
retime('core_charge', 4 / 3.6)
events = [
    {'at': 0, 'sound': 'emergency_idle_hum', 'gain': .26, 'loop': True, 'until': 14.4, 'fadeIn': .08, 'fadeOut': 1.8},
    {'at': 4.4, 'sound': 'authorization', 'gain': .2},
    {'at': 6.6, 'sound': 'authorization', 'gain': .16},
    {'at': 8.8, 'sound': 'ambient_hum', 'gain': .42, 'loop': True, 'until': 21.6, 'fadeIn': 1.6, 'fadeOut': .5},
    {'at': 8.8, 'sound': 'security_activate', 'gain': .3},
    {'at': 9.9, 'sound': 'tumbler_clicks', 'gain': .7},
    {'at': 10.4, 'sound': 'lock_turn', 'gain': .8},
    {'at': 11.2, 'sound': 'pin_01', 'gain': .7, 'pan': 0},
    {'at': 11.38, 'sound': 'pin_02', 'gain': .7, 'pan': .22},
    {'at': 11.56, 'sound': 'pin_03', 'gain': .7, 'pan': 0},
    {'at': 11.74, 'sound': 'pin_04', 'gain': .7, 'pan': -.22},
    {'at': 12, 'sound': 'ring_rotation', 'gain': .82},
    {'at': 13.5, 'sound': 'recovery_iris_open', 'gain': .72},
    {'at': 15, 'sound': 'recovery_core_charge', 'gain': .95},
    {'at': 17.7, 'sound': 'energy_swell', 'gain': .5},
    {'at': 18.465, 'sound': 'charged_hum', 'gain': .76, 'loop': True, 'until': 21.7, 'fadeIn': .5, 'fadeOut': .4},
    {'at': 20.963636, 'sound': 'core_online', 'gain': .78},
    {'at': 21.4, 'sound': 'stable_hum', 'gain': .48, 'loop': True, 'fadeIn': .4},
]
signal = render(events, config['seconds'])
report = write('Recovery.wav', signal)
report['events'] = events
(OUT / 'recovery-audio-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({'seconds': report['seconds'], 'peak': report['peak'], 'audioEvents': len(events)}))
