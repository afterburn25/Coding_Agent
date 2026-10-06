"""Mix current-main cinematic stems, mux aligned masters and extract review frames.

Requires Python 3.10+, numpy==2.3.5 and ffmpeg/ffprobe on PATH. No TTS input.
"""
import hashlib
import json
import math
import subprocess
import wave
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPLASH = ROOT / 'desktop/ChatNexus.Desktop/splash'
WORK = HERE / 'work'
REVIEW = ROOT / 'docs/review/startup-milestones'
RATE = 48000
T = json.loads((SPLASH / 'startup_caption_timeline.json').read_text(encoding='utf-8'))
M = json.loads((SPLASH / 'animation_manifest.json').read_text(encoding='utf-8'))

def run(*args):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True).stdout

def audio():
    out = np.zeros((RATE * T['duration'], 2), dtype=np.float64)
    used = []
    for e in M['events']:
        if 'sound' not in e:
            continue
        source = SPLASH / M['sounds'][e['sound']]
        used.append({'id':e['id'], 'source':source.relative_to(ROOT).as_posix(), 'sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
        with wave.open(str(source)) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (RATE, 2, 2)
            data = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').reshape(-1,2).astype(float) / 32767
        start = round(e['at'] * RATE)
        end = min(len(out),round(e.get('until',T['duration'] if e.get('loop') else e['at']+len(data)/RATE)*RATE))
        n = end-start
        data = np.tile(data,(math.ceil(n/len(data)),1))[:n].copy()
        attack = min(n,round(e.get('fadeIn',.006)*RATE))
        release = min(n,round(e.get('fadeOut',.015)*RATE))
        data[:attack] *= np.linspace(0,1,attack)[:,None]
        if end < len(out): data[-release:] *= np.linspace(1,0,release)[:,None]
        pan = e.get('pan',0)
        if pan>0:
            left=data[:,0].copy(); data[:,0]*=math.cos(pan*math.pi/2); data[:,1]+=left*math.sin(pan*math.pi/2)
        elif pan<0:
            right=data[:,1].copy(); data[:,1]*=math.cos(-pan*math.pi/2); data[:,0]+=right*math.sin(-pan*math.pi/2)
        out[start:end] += data*e.get('gain',.6)
    out *= M['defaultVolume']
    # Preserve the existing hum instead of the old standalone end fade.
    # The incoming part ends precisely one sample before the loop-start sample.
    tail = T['onlineTail']
    count = round(tail['sceneSeamDissolve']*RATE)
    start = round(tail['loopStart']*RATE)
    u = np.linspace(0,1,count,endpoint=False)
    u = u*u*(3-2*u)
    out[-count:] = out[-count:]*(1-u[:,None]) + out[start-count:start]*u[:,None]
    pcm = (out*32767).astype('<i2')
    assert np.max(np.abs(out))<1
    WORK.mkdir(exist_ok=True)
    with wave.open(str(WORK/'startup.wav'),'wb') as wav:
        wav.setnchannels(2);wav.setsampwidth(2);wav.setframerate(RATE);wav.writeframes(pcm.tobytes())
    (REVIEW/'audio-verification.json').write_text(json.dumps({'seconds':len(out)/RATE,'sampleRate':RATE,'channels':2,'peak':float(np.max(np.abs(out))),'speechInputs':[],'sourceStems':used,'loopSeamDelta':(out[start]-out[-1]).tolist(),'tailRms':float(np.sqrt(np.mean(out[-count:]**2)))},indent=2)+'\n',encoding='utf-8')
    run('ffmpeg','-y','-v','error','-i',WORK/'startup.wav','-c:a','aac','-b:a','192k',WORK/'audio.m4a')

def mux():
    targets = [('clean-silent',SPLASH/'assets/NexusCore-Startup-Glow-Only.mp4','Nexus Core Startup - Clean Runtime Master'),('reference-silent',ROOT/'docs/reference/NexusCore-Startup-Captioned-Reference.mp4','Nexus Core Startup - Captioned Reference')]
    for source,target,title in targets:
        target.parent.mkdir(parents=True,exist_ok=True)
        run('ffmpeg','-y','-v','error','-i',WORK/(source+'.mp4'),'-i',WORK/'audio.m4a','-map','0:v:0','-map','1:a:0','-c','copy','-movflags','+faststart','-metadata','title='+title,target)

def frames():
    source=ROOT/'docs/reference/NexusCore-Startup-Captioned-Reference.mp4'
    points=[(c['id'], T['finalizingHold']['at'] if c['id']=='finalizing' else c['at']+.4) for c in T['captions'] if c['id']!='online']
    points += [('online-low',24.8),('online-peak',25.5),('first',0),('last',T['duration']-1/T['fps'])]
    for name,t in points:
        frame=round(t*T['fps'])
        run('ffmpeg','-y','-v','error','-i',source,'-vf',f'select=eq(n\\,{frame})','-frames:v','1',REVIEW/(name+'.png'))
    (REVIEW/'review-frames.json').write_text(json.dumps([{'file':name+'.png','at':t,'frame':round(t*T['fps']),'source':'docs/reference/NexusCore-Startup-Captioned-Reference.mp4'} for name,t in points],indent=2)+'\n',encoding='utf-8')

if __name__=='__main__':
    audio();mux();frames()
    print('Both 30-second masters muxed with identical cinematic audio; review frames extracted from final MP4.')
