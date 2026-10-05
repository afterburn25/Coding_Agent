import json
from mix_audio import OUT, render, write

config = json.loads((OUT / 'recovery-failed.json').read_text(encoding='utf-8'))
events = [
    {'at': 0, 'sound': 'emergency_idle_hum', 'gain': .26, 'loop': True, 'fadeIn': .08},
    {'at': .5, 'sound': 'failure_warning', 'gain': .35},
    {'at': 3.2, 'sound': 'failure_warning', 'gain': .2},
    {'at': 5.9, 'sound': 'fault_contained', 'gain': .34},
]
report = write('Recovery-Failed.wav', render(events, config['seconds']))
report['events'] = events
(OUT / 'recovery-failed-audio-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
