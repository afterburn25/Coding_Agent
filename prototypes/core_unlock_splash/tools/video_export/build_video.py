"""Rebuild one explicitly selected review video, using only repository assets."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CLIPS = {
    'startup': ('--startup', 'mix_audio.py', 'Startup', 'NexusCore-Startup-Glow-Only.mp4'),
    'error': (None, 'mix_error_continuation.py', 'Error-Continuation', 'NexusCore-Error-Red-Continuation.mp4'),
    'recovery': ('--recovery', 'mix_recovery.py', 'Recovery', 'NexusCore-Recovery.mp4'),
    'recovery-failed': ('--recovery-failed', 'mix_recovery_failed.py', 'Recovery-Failed', 'NexusCore-Recovery-Failed.mp4'),
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--clip', choices=CLIPS, required=True)
parser.add_argument('--mux-only', action='store_true', help='Use the existing local silent video and mixed audio')
args = parser.parse_args()
flag, mixer, stem, filename = CLIPS[args.clip]
if not args.mux_only:
    subprocess.run([sys.executable, str(HERE / mixer)], check=True)
    subprocess.run(['node', str(HERE / 'export-error-continuation.cjs'), *([flag] if flag else [])], check=True)
destination = ROOT / 'review/videos' / filename
subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                '-i', str(HERE / (stem + '-silent.mp4')), '-i', str(HERE / (stem + '.wav')),
                '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                '-metadata', f'title=Nexus Core {args.clip}', '-movflags', '+faststart', '-shortest', str(destination)], check=True)
manifest_path = ROOT / 'sequence_manifest.json'
if manifest_path.exists():
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    item = next(clip for clip in manifest['clips'] if clip['id'] == args.clip)
    item['sha256'] = hashlib.sha256(destination.read_bytes()).hexdigest()
    item['bytes'] = destination.stat().st_size
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(destination)
