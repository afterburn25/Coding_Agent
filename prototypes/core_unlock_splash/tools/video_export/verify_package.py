"""Check distribution integrity; --media also decodes/probes all four MP4s."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--media', action='store_true', help='Require ffmpeg and ffprobe for actual decode verification')
args = parser.parse_args()
manifest = json.loads((ROOT / 'sequence_manifest.json').read_text(encoding='utf-8'))
assert manifest['schemaVersion'] == 1
assert {clip['id'] for clip in manifest['clips']} == {'startup', 'error', 'recovery', 'recovery-failed'}
assert len(manifest['clips']) == 4
reports = []
for clip in manifest['clips']:
    path = (ROOT / clip['file']).resolve()
    assert path.is_relative_to(ROOT.resolve()) and path.is_file()
    data = path.read_bytes()
    assert data[4:8] == b'ftyp', f'{path.name} is not an MP4'
    assert len(data) == clip['bytes']
    assert hashlib.sha256(data).hexdigest() == clip['sha256']
    if clip['id'] == 'startup':
        assert clip['sha256'] == 'bcc3ecc6e9032c95b1c0244d79071b81663743dd64a415151b18e26ee4b63955', 'Approved startup must remain unchanged'
    if clip.get('sourceConfig'):
        config = json.loads((ROOT / clip['sourceConfig']).read_text(encoding='utf-8'))
        assert config['seconds'] == clip['seconds']
        assert all(message['text'] and '\ufffd' not in message['text'] for message in config['messages'])
        assert all(a['at'] < b['at'] for a, b in zip(config['messages'], config['messages'][1:]))
    if args.media:
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))
        visual = next(stream for stream in info['streams'] if stream['codec_type'] == 'video')
        sound = next(stream for stream in info['streams'] if stream['codec_type'] == 'audio')
        assert (visual['codec_name'], visual['width'], visual['height'], visual['r_frame_rate']) == ('h264', 1280, 720, '30/1')
        assert (sound['codec_name'], sound['sample_rate'], sound['channels']) == ('aac', '48000', 2)
        assert float(info['format']['duration']) == clip['seconds']
        assert int(visual['nb_frames']) == clip['seconds'] * 30
        subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'], check=True)
    reports.append({'id': clip['id'], 'seconds': clip['seconds'], 'hashVerified': True, 'decoded': args.media})
print(json.dumps(reports, indent=2))
