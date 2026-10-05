import copy
import json
import numpy as np
from mix_audio import OUT, RATE, m, stems, render, write

config = json.loads((OUT / 'error-continuation.json').read_text(encoding='utf-8'))
lead = config['onlineLeadSeconds']
extension = config['extraInstabilitySeconds']
seconds = config['seconds']
endpoint = config['startupSeconds'] - 1 / config['fps']

# Continue only the existing online hum; never replay the startup mechanics.
normal = render(m['events'], endpoint + seconds)
start = round(endpoint * RATE)
signal = normal[start:start + round(seconds * RATE)].copy()
fault_start = round(lead * RATE)
crossfade = round(.16 * RATE)
signal[fault_start:fault_start + crossfade] *= np.linspace(1, 0, crossfade)[:, None]
signal[fault_start + crossfade:] = 0
# The standalone file starts gently after the unchanged startup clip's fade.
attack = round(.08 * RATE)
signal[:attack] *= np.linspace(0, 1, attack)[:, None]

events = copy.deepcopy(m['failure']['events'])
for event in events:
    if event['at'] >= 2.8:
        event['at'] += extension
    if event['id'] == 'instability_start':
        event['until'] += extension

# Extend the sustained interior of the instability stem with soft overlaps,
# preserving the original pitch and the natural lead into the power-down.
source = stems['core_instability']
body = source[round(.5 * RATE):round(2.8 * RATE)]
overlap = round(.16 * RATE)
extended = source[:round(2.8 * RATE)].copy()
required = round((3.5 + extension) * RATE)
while len(extended) < required:
    fade = np.linspace(0, 1, overlap)[:, None]
    joined = extended[-overlap:] * (1 - fade) + body[:overlap] * fade
    extended = np.concatenate((extended[:-overlap], joined, body[overlap:]))
stems['core_instability'] = extended[:required]
failure = render(events, seconds - lead)
signal[fault_start:fault_start + len(failure)] += failure
report = write('Error-Continuation.wav', signal)
report.update({'faultAt': lead, 'extraInstabilitySeconds': extension,
               'startupReplayed': False,
               'soundEvents': [{'at': event['at'] + lead, 'sound': event['sound']}
                               for event in events if 'sound' in event]})
(OUT / 'error-continuation-audio-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
