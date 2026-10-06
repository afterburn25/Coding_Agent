"""Validate delivered movies, current-main preservation and decoded-frame seams."""
import hashlib
import json
import re
import subprocess
from pathlib import Path
import numpy as np
from media import ROOT, SPLASH, WORK, REVIEW, T, run

BASE = '2e29fd8edbda980e7be8c007a607675c3b1b969d'
RUNTIME = SPLASH/'assets/NexusCore-Startup-Glow-Only.mp4'
REFERENCE = ROOT/'docs/reference/NexusCore-Startup-Captioned-Reference.mp4'

def baseline(name):
    return subprocess.run(['git','show',f'{BASE}:{name}'],cwd=ROOT,check=True,capture_output=True).stdout

def probe(path):
    return json.loads(run('ffprobe','-v','error','-show_streams','-show_format','-of','json',path))

def frame(path, n):
    raw=run('ffmpeg','-v','error','-i',path,'-vf',f'select=eq(n\\,{n})','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1')
    return np.frombuffer(raw,dtype=np.uint8).reshape(720,1280,3).astype(float)

def ssim(a,b,frames=None):
    command=['ffmpeg','-hide_banner','-i',a,'-i',b,'-filter_complex','[0:v]crop=1280:590:0:0[a];[1:v]crop=1280:590:0:0[b];[a][b]ssim=shortest=1','-an']
    if frames:command+=['-frames:v',str(frames)]
    command+=['-f','null','-']
    log=subprocess.run([str(x) for x in command],capture_output=True,check=True).stderr.decode()
    return float(re.findall(r'All:([\d.]+)',log)[-1])

def main():
    report={'baselineCommit':BASE,'exports':[]}
    for path in [RUNTIME,REFERENCE]:
        p=probe(path);v=next(s for s in p['streams'] if s['codec_type']=='video');a=next(s for s in p['streams'] if s['codec_type']=='audio')
        assert (v['codec_name'],v['pix_fmt'],v['width'],v['height'],v['avg_frame_rate'],int(v['nb_frames']))==('h264','yuv420p',1280,720,'30/1',900)
        assert float(v['duration'])==30 and float(p['format']['duration'])==30
        assert (a['codec_name'],a['sample_rate'],a['channels'])==('aac','48000',2)
        run('ffmpeg','-v','error','-xerror','-i',path,'-f','null','-')
        report['exports'].append({'file':path.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size,'duration':30,'fps':30,'frames':900,'width':1280,'height':720,'video':'H.264 High / yuv420p','audio':'AAC LC / stereo / 48000 Hz','fullDecodeErrors':0})
    audio_hashes=[hashlib.sha256(run('ffmpeg','-v','error','-i',p,'-map','0:a:0','-c','copy','-f','adts','pipe:1')).hexdigest() for p in [RUNTIME,REFERENCE]]
    assert audio_hashes[0]==audio_hashes[1]
    report['audioStreamsIdentical']=True
    report['sceneAlignmentSsimAll900Frames']=ssim(RUNTIME,REFERENCE)
    # Independent H.264 encodes allocate quantizers differently when captions
    # are present. Source pixels are checked exactly before encoding; this
    # decoded comparison permits only minor lossy-codec noise.
    assert report['sceneAlignmentSsimAll900Frames']>.995
    old=WORK/'baseline-main.mp4';old.write_bytes(baseline(RUNTIME.relative_to(ROOT).as_posix()))
    report['preservedMainSceneSsimFirst510Frames']=ssim(RUNTIME,old,510)
    # Different GOP/keyframe placement from the 17-second source creates
    # small temporal compression differences; the scene itself is unchanged.
    assert report['preservedMainSceneSsimFirst510Frames']>.99
    # All production source, error and recovery media must remain byte identical.
    paths=subprocess.run(['git','ls-tree','-r','--name-only',BASE,'desktop/ChatNexus.Desktop'],cwd=ROOT,check=True,capture_output=True,text=True).stdout.splitlines()
    unchanged=[]
    for name in paths:
        if name==RUNTIME.relative_to(ROOT).as_posix():continue
        # Git's configured clean filters account for Windows CRLF checkout;
        # comparing raw git-show text to the working file would falsely fail.
        current=subprocess.run(['git','hash-object','--path='+name,name],cwd=ROOT,check=True,capture_output=True,text=True).stdout.strip()
        original=subprocess.run(['git','rev-parse',f'{BASE}:{name}'],cwd=ROOT,check=True,capture_output=True,text=True).stdout.strip()
        if current!=original:raise AssertionError('Unrequested production change: '+name)
        unchanged.append(name)
    report['unchangedProductionFiles']=unchanged
    # Verify original SFX mix against the original movie before its old end fade.
    def pcm(p):
        return np.frombuffer(run('ffmpeg','-v','error','-i',p,'-t','15.5','-map','0:a:0','-f','f32le','pipe:1'),dtype='<f4').astype(float)
    new_audio,old_audio=pcm(RUNTIME),pcm(old)
    report['preservedAudioCorrelationFirst15_5Seconds']=float(np.corrcoef(new_audio,old_audio)[0,1])
    assert report['preservedAudioCorrelationFirst15_5Seconds']>.995
    # Actual decoded MP4 frames: allow at most one 8-bit level of mean seam
    # error. The seekable IDR at loop-start has different compression history
    # from the final P/B frame, unlike two adjacent P/B frames. Exact source
    # endpoint equality is independently enforced before encoding in export.cjs.
    for path,key in [(RUNTIME,'clean'),(REFERENCE,'reference')]:
        a,b,c,d=(frame(path,n) for n in [732,733,898,899])
        ordinary=(np.mean(np.abs(a-b))+np.mean(np.abs(c-d)))/2
        seam=float(np.mean(np.abs(d-a)))
        report[key+'Loop']={'firstFrame':732,'lastFrame':899,'lengthFrames':168,'pulseCycles':4,'seamMeanPixelDelta':seam,'adjacentMeanPixelDelta':float(ordinary)}
        assert seam < 1.0
    # Manifest boundaries are the actual first frame of each caption in the
    # deterministic render, not post-hoc guessed seek locations.
    authored=json.loads((WORK/'frames.json').read_text())
    for c in T['captions']:
        assert next(i for i,f in enumerate(authored) if f['id']==c['id'])==round(c['at']*30)
    assert all(f['progress']<1 for f in authored if f['t']<T['onlineTail']['start'])
    online=next(c for c in T['captions'] if c['id']=='online')
    for i in range(380,858):
        assert abs(authored[i]['opacity']-authored[i+42]['opacity'])<1e-10
    low=authored[744];peak=authored[765]
    assert abs(low['opacity']-.45)<1e-10 and abs(peak['opacity']-1)<1e-10
    assert low['color']=='rgb(89, 255, 160)' and low['secondaryColor']=='rgb(61, 215, 127)'
    assert authored[348]['id']=='finalizing' and authored[348]['color']=='rgb(174, 223, 255)'
    report['measuredPulsePeriodFrames']=42
    report['measuredPulsePeriodSeconds']=1.4
    # Measure the finished, lossy-encoded green caption too, independently of
    # the render-state formula. The lower readout crop excludes the blue core.
    raw=run('ffmpeg','-v','error','-i',REFERENCE,'-vf','crop=480:24:400:648','-an','-f','rawvideo','-pix_fmt','rgb24','pipe:1')
    pixels=np.frombuffer(raw,dtype=np.uint8).reshape(900,24,480,3).astype(float)
    green=np.maximum(0,pixels[:,:,:,1]-np.maximum(pixels[:,:,:,0],pixels[:,:,:,2])).mean(axis=(1,2))
    pulse=green[732:900]
    correlations={lag:float(np.corrcoef(pulse[:-lag],pulse[lag:])[0,1]) for lag in range(30,55)}
    period=max(correlations,key=correlations.get)
    assert period==42 and correlations[period]>.995
    assert green[765]>green[744]*1.7
    report['decodedCaptionPulse']={'periodFrames':period,'periodSeconds':period/30,'repeatCorrelation':correlations[period],'lowGreenSignal':float(green[744]),'peakGreenSignal':float(green[765])}
    report['finalizingHoldFrame']=348
    report['onlineBoundaryFrame']=round(online['at']*30)
    (REVIEW/'media-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='unchangedProductionFiles'},indent=2))

if __name__=='__main__':main()
