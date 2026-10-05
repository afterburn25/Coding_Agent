import { sample, clamp, validateManifest } from './timeline.mjs';
import { SplashController, RECOVERY_STATES } from './controller.mjs';
import { Renderer } from './renderer.mjs';
import { AudioEngine } from './audio.mjs';
import { VoiceChannel } from './voice.mjs';

// Production splash runtime. This page is pure presentation: real startup
// state arrives from the host via postMessage (progress, gates, faults,
// recovery states); nothing here can claim readiness or drive recovery.
const $ = id => document.getElementById(id);
const setText = (id, text) => { const el = $(id); if (el.textContent !== text) el.textContent = text; };
const params = new URLSearchParams(location.search);
const host = message => window.chrome?.webview?.postMessage(message);

let clock, renderer, audio, voice, manifest, raf;
let failed = false, reduced = false, externalProgress = null;
let lastDraw = -Infinity, lastRevision = 0, lastPanel = false;
let previousHeld = false, completePosted = false, lastPhaseId = '';

function fail(error) {
  if (failed) return;
  failed = true;
  cancelAnimationFrame(raf);
  audio?.stop();
  void voice?.dispose();
  // The host owns recovery — this page just reports the renderer is dead.
  host({ type: 'splash-error', reason: String(error?.message ?? error), startupFault: Boolean(clock?.activeFault) });
}

// recoveryWatchdogMs: if the host never escalates the recovery state
// past ANALYZING, the honest terminal state is "needs a human" — the
// panel's actions are what remain. Any real recovery-state lands first
// and the guard below makes the timer a no-op.
let recoveryWatchdog = null;
function armRecoveryWatchdog() {
  clearTimeout(recoveryWatchdog);
  const ms = Number(manifest?.failure?.recoveryWatchdogMs) || 6000;
  recoveryWatchdog = setTimeout(() => {
    if (clock && clock.recoveryState === 'RECOVERY_ANALYZING') {
      clock.setRecoveryState('HUMAN_INTERVENTION_REQUIRED');
      changed(.16);
    }
  }, ms);
}

function updateRecovery(state) {
  const visible = (state.panel ?? 0) > 0;
  $('recovery').hidden = !visible;
  $('recovery').style.opacity = String(state.panel ?? 0);
  $('recovery').inert = !visible;
  if (!visible) { lastPanel = false; return; }
  if (state.panel >= .99 && !lastPanel) host({ type: 'recovery-visible' });
  const mode = clock.recoveryState;
  setText('recovery-title', mode === 'SAFE_MODE' ? 'Nexus Core Safe Mode' : 'Nexus Core startup failure');
  setText('recovery-state', clock.mode === 'repair' ? 'REPAIR COMPLETE · REAUTHORIZING' : RECOVERY_STATES[mode][0]);
  setText('recovery-message', clock.diagnostics.message);
  setText('recovery-description', RECOVERY_STATES[mode][1]);
  setText('recovery-attempt', clock.attempt ? `RECOVERY ATTEMPT ${clock.attempt.attempt} OF ${clock.attempt.total}` : '');
  $('recovery-attempt').hidden = !clock.attempt;
  setText('error-details', clock.diagnostics.detail || 'No additional details supplied.');
  if (!lastPanel) $('recovery-title').focus({ preventScroll: true });
  lastPanel = true;
}

function paint() {
  const state = clock.sample(reduced);
  renderer.render(state);
  $('shade').style.opacity = Math.min(1, .75 - .75 * state.charge + (state.fault ? .18 * state.warning : 0)).toFixed(2);
  const label = state.phase.label.toLowerCase();
  if ($('scene').getAttribute('aria-label') !== label) $('scene').setAttribute('aria-label', label);
  const normal = clock.mode === 'normal';
  setText('status', normal ? externalProgress?.primary ?? state.phase.label : state.phase.label);
  setText('detail', normal
    ? externalProgress?.secondary ?? (clock.held
        ? (state.charge === 1 ? 'FULL POWER SUSTAINED · AWAITING READINESS' : 'AWAITING STARTUP SIGNAL · SAFE HOLD')
        : state.online ? 'CONTAINMENT RELEASED · ENERGY STABLE' : state.phase.id === 'charged_hold' ? 'FULL POWER SUSTAINED · READINESS GATE ARMED' : 'CONTAINMENT PROTOCOL · NOMINAL')
    : state.contained ? 'CORE SECURED · RECOVERY CONTROLS AVAILABLE' : 'AUTOMATIC CONTAINMENT · RECOVERY REMAINS AVAILABLE');
  $('fill').style.transform = `scaleX(${(normal ? externalProgress?.value ?? clamp(clock.time / manifest.duration) : clock.progressAtFault).toFixed(3)})`;
  $('stage').classList.toggle('fault', Boolean(state.fault));
  // Core-online state: green pulsating status — the timeline's stable-online
  // point OR the host's canonical CORE SYSTEMS · ONLINE label, whichever
  // reaches first.
  const online = externalProgress
    ? externalProgress.primary === 'CORE SYSTEMS · ONLINE'
    : state.online;
  $('status').classList.toggle('online', online);
  $('detail').classList.toggle('online', online);
  updateRecovery(state);
  return state;
}

function frame(now) {
  if (failed) return;
  try {
    // 60 Hz while animating; 15 Hz once contained — recovery keeps the GPU.
    const fps = clock.activeFault && clock.time >= clock.duration ? 15 : 60;
    if (now - lastDraw >= 1000 / fps - .75) {
      clock.update(reduced);
      if (clock.revision !== lastRevision) { lastRevision = clock.revision; void audio.sync(clock, { fade: .16 }); }
      if (previousHeld !== clock.held) { previousHeld = clock.held; void audio.sync(clock); }
      lastDraw = now;
      if (clock.playing) {
        const state = paint();
        if (state.phase.id !== lastPhaseId) { lastPhaseId = state.phase.id; host({ type: 'phase', id: state.phase.id }); }
        if (!completePosted && clock.mode === 'normal' && state.online) { completePosted = true; host({ type: 'sequence-complete' }); }
      }
    }
    raf = requestAnimationFrame(frame);
  } catch (error) { fail(error); }
}

function changed(fade = .008) {
  if (failed) { if (clock?.activeFault) updateRecovery({ panel: 1, contained: true }); return; }
  try { previousHeld = clock.held; lastRevision = clock.revision; paint(); void audio.sync(clock, { fade }); }
  catch (error) { fail(error); }
}

function triggerFault(details = {}) {
  const started = clock.triggerFault(details, reduced, externalProgress?.value ?? clamp(clock.time / manifest.duration));
  host({ type: 'startup-fault', message: clock.diagnostics.message });
  if (started) changed(.16); else if (!failed) paint();
  return started;
}

// b64 → playback through the voice channel; host is told whether audio
// actually started (durable "user heard it" signal) and when it ends.
async function playVoice(id, b64, duckLevel) {
  try {
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0)).buffer;
    const res = await voice.play(bytes, { duckLevel: duckLevel ?? .6 });
    host({ type: 'voice-result', id, started: res.started, seconds: res.seconds ?? 0 });
    if (res.started && res.done) res.done.then(() => host({ type: 'voice-ended', id }));
    else host({ type: 'voice-ended', id });
  } catch (error) {
    host({ type: 'voice-result', id, started: false, seconds: 0, error: String(error?.message ?? error) });
  }
}

function handleHost(msg) {
  if (!msg || typeof msg !== 'object' || failed && msg.type !== 'dispose') return;
  try {
    switch (msg.type) {
      case 'set-progress':
        if (!Number.isFinite(msg.value)) return;
        externalProgress = { value: clamp(msg.value), primary: String(msg.primary ?? ''), secondary: String(msg.secondary ?? '') };
        paint();
        break;
      case 'set-gate': clock.setGate(msg.id, Boolean(msg.released)); changed(); break;
      case 'trigger-fault': triggerFault({ message: msg.message, detail: msg.detail }); armRecoveryWatchdog(); break;
      case 'recovery-state': clock.setRecoveryState(msg.state, { attempt: msg.attempt, total: msg.total, message: msg.message }); changed(.16); break;
      case 'repair-success': clock.repairSuccess(reduced); changed(.16); break;
      case 'show-recovery': clock.showRecoveryImmediately(); armRecoveryWatchdog(); changed(.16); break;
      case 'action-feedback': setText('action-feedback', String(msg.text ?? '')); break;
      case 'set-volume': audio.setVolume(clamp(msg.value ?? audio.volume)); break;
      case 'set-audio':
        if (!msg.enabled) { audio.disabled = true; audio.stop(); }
        else { audio.disabled = false; void audio.sync(clock); }
        break;
      case 'set-reduced': reduced = Boolean(msg.value); paint(); break;
      case 'play-voice': void playVoice(msg.id, msg.b64, msg.duck); break;
      case 'stop-voice': voice.stop(typeof msg.fade === 'number' ? msg.fade : .18); break;
      case 'dispose': clock.pause(); cancelAnimationFrame(raf); void audio.dispose(); void voice.dispose(); break;
    }
  } catch (error) { fail(error); }
}

async function boot() {
  const response = await fetch('../animation_manifest.json');
  if (!response.ok) throw new Error('Timeline asset missing');
  manifest = validateManifest(await response.json());
  clock = new SplashController(manifest);
  // Presentation starts sealed — every gate waits for a real startup
  // milestone from the host; the timeline can never outrun reality.
  for (const gate of manifest.gates) clock.setGate(gate.id, false);
  reduced = params.has('reduced') || matchMedia('(prefers-reduced-motion: reduce)').matches;
  renderer = new Renderer($('scene'));
  audio = new AudioEngine(manifest, { disabled: params.has('silent') });
  voice = new VoiceChannel(audio);
  if (params.has('volume')) audio.setVolume(clamp(Number(params.get('volume')) || manifest.defaultVolume));
  // Host-seeded progress — the bar continues from the position the
  // warm-up frame already showed; without it the first paint falls back
  // to clock.time/duration and the loader visibly restarts at zero.
  if (params.has('p')) {
    externalProgress = { value: clamp(Number(params.get('p')) || 0),
      primary: params.get('t1') || undefined, secondary: params.get('t2') || undefined };
  }
  for (const button of document.querySelectorAll('[data-recovery-action]'))
    button.onclick = () => host({ type: 'recovery-action', action: button.dataset.recoveryAction });
  window.chrome?.webview?.addEventListener('message', e => handleHost(e.data));
  document.addEventListener('visibilitychange', () => { /* host owns pause policy */ });
  window.addEventListener('pagehide', () => { cancelAnimationFrame(raf); void audio.dispose(); void voice.dispose(); });
  // Integration surface for the host and for verification runs.
  window.preview = {
    state() {
      const s = clock.sample(reduced);
      return { t: clock.time, phase: s.phase.id, held: clock.held, mode: clock.mode,
        online: s.online, fault: Boolean(s.fault), contained: Boolean(s.contained),
        recoveryState: clock.recoveryState, progress: externalProgress?.value ?? null };
    },
    triggerFault,
  };
  clock.play();
  paint();
  raf = requestAnimationFrame(frame);
  host({ type: 'splash-ready' });
  void audio.initialize().then(() => changed(.16));
}

window.addEventListener('error', e => fail(e.error ?? new Error(e.message)));
window.addEventListener('unhandledrejection', e => fail(e.reason instanceof Error ? e.reason : new Error(String(e.reason))));
boot().catch(fail);
