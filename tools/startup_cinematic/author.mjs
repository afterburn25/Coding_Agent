// Export-only adapter: import the CURRENT MAIN production renderer and its
// mechanical timeline unchanged. Never load splash.mjs or drive the host.
import {Renderer} from '../../desktop/ChatNexus.Desktop/splash/web/renderer.mjs';
import {sample, smooth, validateManifest} from '../../desktop/ChatNexus.Desktop/splash/web/timeline.mjs';
const base = '../../desktop/ChatNexus.Desktop/splash/';
const [timeline, manifest] = await Promise.all([
  fetch(base + 'startup_caption_timeline.json').then(r => r.json()),
  fetch(base + 'animation_manifest.json').then(r => r.json()).then(validateManifest)
]);
await document.fonts.ready;
await document.querySelector('#chamber').decode();
const canvas = document.querySelector('#scene');
const renderer = new Renderer(canvas);
const incomingCanvas = document.createElement('canvas');
const incoming = new Renderer(incomingCanvas);
const blendCanvas = document.createElement('canvas');
blendCanvas.width = 1024; blendCanvas.height = 576;
const tail = timeline.onlineTail;
const color = (a, b, u) => 'rgb(' + a.map((v, i) => Math.round(v + (b[i] - v) * u)).join(',') + ')';
const primary = [174,223,255], secondary = [118,144,174];
const green = [89,255,160], greenDim = [61,215,127];

function caption(el, entry, t, dissolve) {
  const p = el.querySelector('.primary'), s = el.querySelector('.secondary');
  p.textContent = entry.primary; s.textContent = entry.secondary;
  el.style.opacity = dissolve;
  const online = entry.id === 'online';
  const phase = Math.max(0, t - tail.start);
  const pulse = (1 - Math.cos(2 * Math.PI * phase / tail.pulsePeriod)) / 2;
  const tint = online ? smooth(phase / timeline.captionTransition) : 0;
  p.style.color = color(primary, green, tint);
  s.style.color = color(secondary, greenDim, tint);
  p.style.opacity = online ? .45 + .55 * pulse : 1;
  s.style.opacity = online ? .75 + .25 * pulse : 1;
  p.style.textShadow = online ? `0 0 ${4 + 22 * pulse}px rgba(80,255,160,${.18 + .82 * pulse}),0 0 ${12 + 24 * pulse}px rgba(80,255,160,${.08 + .25 * pulse})` : 'none';
  s.style.textShadow = online ? `0 0 ${2 + 6 * pulse}px rgba(61,215,127,${.12 + .2 * pulse})` : 'none';
  return {primary:p.textContent,secondary:s.textContent,color:p.style.color,secondaryColor:s.style.color,opacity:Number(p.style.opacity),secondaryOpacity:Number(s.style.opacity),fits:p.scrollWidth <= el.clientWidth && s.scrollWidth <= el.clientWidth};
}

window.author = {
  timeline,
  render(t, clean = false) {
    // t=duration is deliberately available for mathematical endpoint checks,
    // but the movie contains exactly frames 0..899 at 30 fps.
    const state = sample(manifest, t, false, t);
    renderer.render(state);
    let seam = 0;
    const seamStart = tail.loopEnd - tail.sceneSeamDissolve;
    if (t >= seamStart) {
      seam = smooth((t - seamStart) / tail.sceneSeamDissolve);
      const incomingTime = tail.loopStart + t - tail.loopEnd;
      incoming.render(sample(manifest, incomingTime, false, incomingTime));
      // Add weighted premultiplied layers instead of source-over, which would
      // double translucent mist/glow at the seam. Geometry is never scaled.
      const b = blendCanvas.getContext('2d'), c = canvas.getContext('2d');
      b.clearRect(0, 0, 1024, 576); b.save();
      b.globalAlpha = 1 - seam; b.drawImage(canvas, 0, 0);
      b.globalCompositeOperation = 'lighter'; b.globalAlpha = seam;
      b.drawImage(incomingCanvas, 0, 0); b.restore();
      c.clearRect(0, 0, 1024, 576); c.drawImage(blendCanvas, 0, 0);
    }
    document.querySelector('#shade').style.opacity = Math.min(1, .75 - .75 * state.charge).toFixed(2);
    const index = timeline.captions.findLastIndex(c => c.at <= t);
    const entry = timeline.captions[index], next = timeline.captions[index + 1];
    const u = next ? smooth((t - entry.at) / (next.at - entry.at)) : 0;
    let progress = next ? entry.progressGate + (next.progressGate - entry.progressGate) * u : 1;
    // Leave a visible gap throughout the pause-safe hold. Only the last six
    // frames leading into ONLINE close it; all earlier hold frames stay at 98%.
    if (entry.id === 'finalizing') progress = .98 + .02 * smooth((t - (tail.start - timeline.captionTransition)) / timeline.captionTransition);
    document.querySelector('#fill').style.transform = `scaleX(${progress})`;
    const dissolve = index === 0 ? 1 : smooth((t - entry.at) / timeline.captionTransition);
    caption(document.querySelector('#previous'), timeline.captions[Math.max(0,index - 1)], t, 1 - dissolve);
    const text = caption(document.querySelector('#current'), entry, t, dissolve);
    document.body.classList.toggle('clean', clean);
    return {t,id:entry.id,progress,dissolve,seam,...text,charge:state.charge,iris:state.iris,rings:state.rings,cylinder:state.cylinder,orbit:state.orbit};
  },
  clean(value) { document.body.classList.toggle('clean', value); }
};
window.author.render(0);
