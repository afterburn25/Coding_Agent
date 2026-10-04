import { Timeline, sample, clamp, validateManifest } from './timeline.mjs';
import { Renderer } from './renderer.mjs';
import { AudioEngine } from './audio.mjs';

const $ = id => document.getElementById(id);
const setText = (id, text) => { const el = $(id); if (el.textContent !== text) el.textContent = text; };
const params = new URLSearchParams(location.search);
const host = message => window.chrome?.webview?.postMessage(message);
let clock, renderer, audio, manifest, raf, failed = false, reduced = false;
let lastDraw = -Infinity, lastUi = -Infinity, previousHeld = false;
let draws = [], intervals = [], lastFrame = null, externalProgress = null, visibilityPaused = false;

function fail(error) {
  if (failed) return;
  failed = true; cancelAnimationFrame(raf); audio?.stop();
  $('scene').hidden = true; $('status').textContent = 'NEXUS CORE';
  $('detail').textContent = 'STATIC PREVIEW · ANIMATION UNAVAILABLE';
  $('health').textContent = `Static fallback: ${error.message}`;
  for (const id of ['play', 'replay', 'scrub', 'phase', 'speed', 'gate', 'release']) $(id).disabled = true;
  host({ type: 'prototype-fatal', reason: error.message });
}

function paint() {
  const started = performance.now();
  const state = sample(manifest, clock.time, reduced, clock.ambientTime); renderer.render(state);
  $('shade').style.opacity = (.75 - .48 * state.charge).toFixed(2);
  const label = state.phase.label.toLowerCase();
  if ($('scene').getAttribute('aria-label') !== label) $('scene').setAttribute('aria-label', label);
  setText('status', externalProgress?.primary ?? state.phase.label);
  setText('detail', externalProgress?.secondary ?? (clock.held ? (state.charge === 1 ? 'FULL POWER SUSTAINED · AWAITING READINESS' : 'AWAITING STARTUP SIGNAL · SAFE HOLD') : state.online ? 'CONTAINMENT RELEASED · ENERGY STABLE' : state.phase.id === 'charged_hold' ? 'FULL POWER SUSTAINED · READINESS GATE ARMED' : 'CONTAINMENT PROTOCOL · NOMINAL PREVIEW'));
  $('fill').style.transform = `scaleX(${(externalProgress?.value ?? clamp(clock.time / manifest.duration)).toFixed(3)})`;
  draws.push(performance.now() - started); if (draws.length > 600) draws.shift();
}

function updateUi(now) {
  if (now - lastUi < 125) return;
  lastUi = now;
  $('time').textContent = `${Math.min(clock.time, manifest.duration).toFixed(2)} / ${manifest.duration.toFixed(2)} s`;
  $('scrub').value = Math.min(clock.time, manifest.duration);
  $('play').textContent = clock.playing ? 'Pause' : clock.time > 0 ? 'Resume' : 'Play sequence';
  $('phase').value = sample(manifest, clock.time).phase.id;
  $('health').textContent = audio.disabled ? 'Audio disabled · visuals active' : audio.warnings.length ? `Audio: ${audio.warnings.length} unavailable stem(s)` : clock.held ? 'Holding for readiness' : audio.context ? 'Local stereo stems · 45% recommended' : 'Press Play to enable sound';
  if (intervals.length > 10) $('metrics').textContent = `${Math.round(1000 / percentile(intervals, .5))} fps · ${percentile(draws, .95).toFixed(1)} ms draw`;
  audio.measure();
}

function frame(now) {
  if (failed) return;
  try {
    // requestAnimationFrame follows the display clock. Cap at 60 Hz on fast monitors.
    if (now - lastDraw >= 1000 / 60 - .75) {
      if (lastFrame !== null && clock.playing) { intervals.push(now - lastFrame); if (intervals.length > 600) intervals.shift(); }
      lastFrame = now; lastDraw = now;
      if (clock.playing) paint();
      if (previousHeld !== clock.held) { previousHeld = clock.held; void audio.sync(clock); }
      updateUi(now);
    }
    raf = requestAnimationFrame(frame);
  } catch (error) { fail(error); }
}
function percentile(values, p) { const sorted = [...values].sort((a, b) => a - b); return sorted[Math.floor((sorted.length - 1) * p)] ?? 0; }
function changed() { previousHeld = clock.held; lastFrame = null; paint(); lastUi = -Infinity; updateUi(performance.now()); void audio.sync(clock); }

async function boot() {
  $('chamber').addEventListener('error', () => { $('missing-art').hidden = false; $('chamber').hidden = true; });
  if ($('chamber').complete && !$('chamber').naturalWidth) { $('missing-art').hidden = false; $('chamber').hidden = true; }
  const response = await fetch('../animation_manifest.json');
  if (!response.ok) throw new Error('Timeline asset missing');
  manifest = validateManifest(await response.json());
  clock = new Timeline(manifest);
  $('scrub').max = manifest.duration;
  reduced = params.has('reduced') || matchMedia('(prefers-reduced-motion: reduce)').matches;
  $('reduced').checked = reduced;
  renderer = new Renderer($('scene'));
  audio = new AudioEngine(manifest, { disabled: params.has('silent') });
  for (const p of manifest.phases) { const option = new Option(p.id.replaceAll('_', ' '), p.id); $('phase').add(option); }
  $('play').onclick = async () => {
    if (clock.playing) clock.pause();
    else { await audio.initialize(); clock.play(); }
    changed();
  };
  $('replay').onclick = async () => { await audio.initialize(); clock.replay(); changed(); };
  $('mute').onclick = () => { audio.setMuted(!audio.muted); $('mute').textContent = audio.muted ? 'Unmute' : 'Mute'; $('mute').setAttribute('aria-pressed', String(audio.muted)); };
  $('volume').oninput = () => audio.setVolume(Number($('volume').value) / 100);
  $('speed').onchange = () => { clock.setSpeed(Number($('speed').value)); changed(); };
  $('scrub').oninput = () => { clock.pause(); clock.seek(Number($('scrub').value)); changed(); };
  $('phase').onchange = () => { clock.pause(); clock.seek(manifest.phases.find(p => p.id === $('phase').value).at); changed(); };
  $('reduced').onchange = () => { reduced = $('reduced').checked; paint(); };
  $('gate').onchange = () => {
    clock.pause(); clock.seek(0);
    for (const gate of manifest.gates) clock.setGate(gate.id, true);
    if ($('gate').value) clock.setGate($('gate').value, false);
    $('release').disabled = !$('gate').value; changed();
  };
  $('release').onclick = () => { if ($('gate').value) clock.setGate($('gate').value, true); $('release').disabled = true; changed(); };
  document.addEventListener('visibilitychange', () => {
    // Background preview should not compete with model work or skip the reveal.
    if (document.hidden) { visibilityPaused = clock.playing; clock.pause(); changed(); }
    else if (visibilityPaused) { visibilityPaused = false; clock.play(); changed(); }
  });
  window.addEventListener('pagehide', () => { cancelAnimationFrame(raf); void audio.dispose(); });
  window.preview = {
    // Integration API. Caller supplies real progress separately from animation time.
    setGate(id, released) { clock.setGate(id, released); changed(); },
    setProgress(value, primary, secondary = '') {
      if (!Number.isFinite(value)) throw new Error('Invalid progress');
      externalProgress = { value: clamp(value), primary: String(primary), secondary: String(secondary) }; paint();
    },
    setAudioEnabled(enabled) { if (!enabled) { audio.disabled = true; audio.stop(); } else { audio.disabled = false; void audio.sync(clock); } },
    setVolume(value) { audio.setVolume(value); },
    setReducedMotion(value) { reduced = Boolean(value); $('reduced').checked = reduced; paint(); },
    play() { clock.play(); changed(); },
    pause() { clock.pause(); changed(); },
    replay() { clock.replay(); changed(); },
    dispose() { clock.pause(); cancelAnimationFrame(raf); void audio.dispose(); },
    capture(t, minimal = false) {
      clock.pause(); clock.seek(t); reduced = minimal; document.body.classList.add('capture');
      paint(); audio.stop(); return { t: clock.time, phase: sample(manifest, clock.time).phase.id };
    },
    async verifyPlayback() {
      const checks = {};
      await audio.initialize();
      clock.replay(); changed();
      await delay(320); clock.pause(); changed(); const paused = clock.time;
      await delay(100); checks.pauseFreezesTime = clock.time === paused;
      window.preview.setProgress(.37, 'LOADING · MODELS', 'Preparing the local model');
      checks.realProgressIndependent = clock.time === paused && $('status').textContent === 'LOADING · MODELS' && $('fill').style.transform.includes('0.37');
      externalProgress = null; paint();
      clock.play(); changed(); await delay(100);
      audio.setMuted(true); await delay(100); checks.muteSilencesOutput = audio.measure() < .00001;
      audio.setMuted(false);
      clock.setSpeed(.5); checks.speedControl = clock.speed === .5; clock.setSpeed(1);
      const readyAt = manifest.gates.find(g => g.id === 'ready').at;
      clock.seek(0); clock.setGate('ready', false); clock.seek(readyAt - .15); clock.play(); changed();
      await delay(350); checks.readinessGateHolds = clock.held && clock.time < readyAt;
      await delay(2100);
      checks.chargedHumSustains = audio.disabled || (audio.scheduled.some(e => e.id === 'charged_hold_hum') && audio.measure() > .001);
      checks.chargedVisualSustains = sample(manifest, clock.time).charge === 1 && !sample(manifest, clock.time).online;
      clock.setGate('ready', true); changed(); await delay(150); checks.releaseContinues = clock.time > readyAt;
      clock.pause(); clock.replay(); changed(); checks.replayResets = clock.time < .01;
      draws = []; intervals = []; audio.peak = 0;
      await delay((manifest.duration + .6) * 1000);
      checks.reachedOnline = sample(manifest, clock.time).online;
      checks.audioOutput = audio.disabled ? audio.context === null : audio.peak > .001;
      checks.allStemsDecoded = audio.disabled || audio.buffers.size === Object.keys(manifest.sounds).length;
      checks.noAudioWarnings = audio.warnings.length === 0;
      checks.reducedMotion = sample(manifest, manifest.duration, true).particleCount === 0 && sample(manifest, manifest.duration, true).pulse === 0;
      const report = { checks, passed: Object.values(checks).every(Boolean), decodedStems: audio.buffers.size, audioPeak: audio.peak, audioState: audio.context?.state ?? 'disabled', warnings: audio.warnings,
        frames: intervals.length, frameMedianMs: percentile(intervals, .5), frameP95Ms: percentile(intervals, .95), drawMedianMs: percentile(draws, .5), drawP95Ms: percentile(draws, .95),
        dimensions: [$('scene').width, $('scene').height], userAgent: navigator.userAgent };
      clock.pause(); changed(); host({ type: 'verification-result', report }); return report;
    }
  };
  paint(); raf = requestAnimationFrame(frame); host({ type: 'prototype-ready' });
  if (params.has('autoplay')) { await audio.initialize(); clock.play(); changed(); }
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
window.addEventListener('error', e => fail(e.error ?? new Error(e.message)));
window.addEventListener('unhandledrejection', e => fail(e.reason instanceof Error ? e.reason : new Error(String(e.reason))));
boot().catch(fail);
