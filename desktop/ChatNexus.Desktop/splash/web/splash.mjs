import { sample, clamp, validateManifest } from './timeline.mjs';
import { SplashController, RECOVERY_STATES, RECOVERY_STAGES, RECOVERY_STAGE_TIMES, RECOVERY_FAILED_STAGES } from './controller.mjs';
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
// Displayed fill eases toward the real value — the bar sweeps smoothly
// rather than snapping when the host's progress jumps.
let shownFill = 0;
let lastDraw = -Infinity, lastRevision = 0, lastPanel = false;
let previousHeld = false, completePosted = false, lastPhaseId = '';
// Rendered scene clips are the primary surface once decoded; the DOM
// layers beneath stay live as the fallback if playback never starts.
const bootvid = $('bootvid'), errvid = $('errvid'), recvid = $('recvid'), failvid = $('failvid');
let videoMode = false;
// A sequence clip that owns the whole surface right now: 'error' (fault
// continuation), 'recovery' (user-requested attempt), 'recovery-failed'.
// While set, the DOM recovery panel stays hidden — the clip ends contained
// and THEN the real controls reappear.
let overlay = null;
// The last recovery milestone the HOST confirmed. The recovery clip may not
// cross RECOVERY_STAGE_TIMES[i] until confirmedStage >= i — a caption can
// never claim a milestone ahead of the real attempt.
let confirmedStage = -1;
// One in-flight user-requested attempt at a time — duplicate Retry clicks
// coalesce (button disabled here, single-flight guard on the host).
let attemptInFlight = false;
let recoveryAttempt = 0;
let failCaptionTimer = null;

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

function retryButton() {
  return document.querySelector('[data-recovery-action="retry"]');
}

function syncRetryButton() {
  const b = retryButton();
  if (b) b.disabled = attemptInFlight;
}

function updateRecovery(state) {
  // A sequence clip owns the surface — the intervention panel appears when
  // the clip ends contained, never mid-animation.
  const visible = (state.panel ?? 0) > 0 && overlay === null;
  $('recovery').hidden = !visible;
  $('recovery').style.opacity = String(state.panel ?? 0);
  $('recovery').inert = !visible;
  if (!visible) { lastPanel = false; return; }
  if (state.panel >= .99 && !lastPanel) host({ type: 'recovery-visible' });
  const mode = clock.recoveryState;
  setText('recovery-title', mode === 'SAFE_MODE' ? 'Nexus Core Safe Mode' : 'Nexus Core startup failure');
  // During a user-requested attempt the stage line shows the last
  // host-confirmed milestone — held, never predicted.
  const stageLabel = attemptInFlight && confirmedStage >= 0 ? RECOVERY_STAGES[confirmedStage] : null;
  setText('recovery-state', stageLabel
    ?? (clock.mode === 'repair' ? 'REPAIR COMPLETE · REAUTHORIZING'
      : mode === 'HUMAN_INTERVENTION_REQUIRED' ? 'USER INTERVENTION · REQUIRED'
      : RECOVERY_STATES[mode][0]));
  setText('recovery-message', clock.diagnostics.message);
  setText('recovery-description', RECOVERY_STATES[mode][1]);
  setText('recovery-attempt', attemptInFlight ? `RECOVERY ATTEMPT ${recoveryAttempt}` : (clock.attempt ? `RECOVERY ATTEMPT ${clock.attempt.attempt} OF ${clock.attempt.total}` : ''));
  $('recovery-attempt').hidden = !(attemptInFlight || clock.attempt);
  setText('error-details', clock.diagnostics.detail || 'No additional details supplied.');
  if (!lastPanel) $('recovery-title').focus({ preventScroll: true });
  lastPanel = true;
  syncRetryButton();
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
  const fillTarget = normal ? externalProgress?.value ?? clamp(clock.time / manifest.duration) : clock.progressAtFault;
  shownFill += (fillTarget - shownFill) * .09;   // ~60Hz ease; no snap on host updates
  if (Math.abs(fillTarget - shownFill) < .003) shownFill = fillTarget;
  $('fill').style.transform = `scaleX(${shownFill.toFixed(3)})`;
  // The clip's baked bar must track REAL progress: rate-correct playback
  // so the scene never outruns the backend — it slows to a near-hold when
  // progress stalls and speeds up (max 2.5x) to catch up. When progress
  // completes, the remaining tail plays out fast into the online glow.
  if (videoMode && !clock.activeFault && bootvid) {
    const dur = bootvid.duration || 17;
    const target = Math.min(fillTarget, .985) * dur;
    const drift = target - bootvid.currentTime;
    if (drift <= -.6) { if (!bootvid.paused) bootvid.pause(); }
    else {
      if (bootvid.paused && !bootvid.ended) void bootvid.play().catch(() => {});
      const rate = clamp(.35, 1 + drift * .9, 2.5);
      if (!bootvid.paused && Math.abs(bootvid.playbackRate - rate) > .04) bootvid.playbackRate = rate;
    }
  }
  $('stage').classList.toggle('fault', Boolean(state.fault));
  // STABILITY THRESHOLD · RECOVERING fades the bar/status back toward the
  // normal palette (mirrors the clip's ~1.8s red→cyan transition).
  const recovering = attemptInFlight && confirmedStage >= 5;
  $('status').classList.toggle('recovering', recovering);
  $('detail').classList.toggle('recovering', recovering);
  $('fill').classList.toggle('recovering', recovering);
  // Core-online state: green pulsating status — the timeline's stable-online
  // point, the host's canonical CORE SYSTEMS · ONLINE label, or the final
  // confirmed recovery stage, whichever applies. Never on a fault surface:
  // a frozen ONLINE label must not stay green through containment.
  const online = (attemptInFlight && confirmedStage >= 8)
    || (!clock.activeFault && overlay !== 'recovery-failed' && (externalProgress
      ? externalProgress.primary === 'CORE SYSTEMS · ONLINE'
      : state.online));
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
      // Recovery milestone gate: the clip may not cross a stage's caption
      // until the host confirmed that stage. Held frames keep the hum via
      // the fault-mode audio loop — a stalled attempt looks stalled, never
      // falsely ahead.
      if (overlay === 'recovery' && recvid && !recvid.paused && !recvid.ended
          && confirmedStage + 1 < RECOVERY_STAGE_TIMES.length
          && recvid.currentTime >= RECOVERY_STAGE_TIMES[confirmedStage + 1] - .1) {
        recvid.pause();
      }
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

// The error continuation clip branches from the fully powered startup frame —
// it only matches reality when the fault landed at/after full power. An early
// fault falls back to the DOM containment animation so partial geometry and
// partial progress are the actual state, not a jumped-to final frame.
function useErrorClip(progress) {
  return !reduced && videoMode && errvid
    && (bootvid?.ended || clock.held || progress >= .9);
}

function triggerFault(details = {}) {
  const progress = externalProgress?.value ?? clamp(clock.time / manifest.duration);
  const started = clock.triggerFault(details, reduced, progress);
  host({ type: 'startup-fault', message: clock.diagnostics.message });
  // Fault is a new surface truth — any stale green online styling clears.
  $('status').classList.remove('online');
  $('detail').classList.remove('online');
  if (started && useErrorClip(progress)) {
    overlay = 'error';
    errvid.hidden = false;
    errvid.loop = false;
    errvid.currentTime = 0;
    // Clip ends contained — hold the last frame, then hand the surface
    // back so the intervention panel appears over the contained core.
    errvid.onended = () => { overlay = null; changed(.16); };
    errvid.onerror = () => { overlay = null; errvid.hidden = true; changed(.16); };
    void errvid.offsetWidth;           // flush style so the fade runs
    errvid.classList.add('live');
    void errvid.play().catch(() => { overlay = null; errvid.hidden = true; changed(.16); });
    if (bootvid) setTimeout(() => bootvid.pause(), 400);
  } else if (started && videoMode && bootvid) {
    // Early/mid fault while the clip ruled the surface: hand back to the
    // canvas so containment plays from the real partial geometry.
    videoMode = false;
    document.body.classList.remove('video-mode');
    bootvid.classList.remove('live');
    bootvid.pause();
  }
  if (started) changed(.16); else if (!failed) paint();
  return started;
}

// --- User-requested recovery attempt -------------------------------------
// The host owns the real attempt; this page owns the honest presentation of
// it. The recovery clip gates on confirmed stages; the DOM fallback cycles
// the same stage labels on the intervention panel.

// hold=true keeps the clip's last frame owning the surface after 'ended' —
// used by the recovery clip whose online-green tail runs while the host
// finishes its own readiness/voice gates before dismissing the splash.
function playClip(vid, name, onEnded, hold = false) {
  overlay = name;
  vid.hidden = false;
  vid.loop = false;
  vid.currentTime = 0;
  vid.onended = () => { if (overlay === name && !hold) { overlay = null; changed(.16); } onEnded?.(); };
  vid.onerror = () => { if (overlay === name) { overlay = null; } vid.hidden = true; changed(.16); };
  void vid.offsetWidth;
  vid.classList.add('live');
  return vid.play().then(() => true).catch(() => {
    if (overlay === name) overlay = null;
    vid.hidden = true; changed(.16); return false;
  });
}

// The successful green tail — real audio cue once the host confirms ONLINE.
function recoveryOnlineAudio() {
  try {
    audio.stop(.18);
    if (audio.disabled) return;
    void audio.initialize().then(ok => {
      if (!ok) return;
      void audio.context.resume();
      audio.schedule({ id: 'online_pulse', sound: 'core_online', at: 0, gain: .78 }, 0, 1, Infinity);
      audio.schedule({ id: 'stable_online', sound: 'stable_hum', at: 0, gain: .48, loop: true, fadeIn: .3 }, 0, 1, Infinity);
    });
  } catch { /* cues are decorative — the stage labels carry the truth */ }
}

function beginRecovery(msg = {}) {
  if (attemptInFlight || !clock.activeFault) return;   // one attempt at a time
  attemptInFlight = true;
  recoveryAttempt = Number.isInteger(msg.attempt) ? msg.attempt : recoveryAttempt + 1;
  confirmedStage = 0;                                  // containment engaged IS stage 0
  clearTimeout(recoveryWatchdog);                      // attempt supersedes the guard
  clearTimeout(failCaptionTimer);                      // a pending fail-caption chain must not clear this attempt
  clock.setRecoveryState('REPAIR_ATTEMPT', { attempt: recoveryAttempt, total: recoveryAttempt });
  syncRetryButton();
  changed(.16);
  // Video path when the clip can actually play; otherwise the DOM panel
  // stays up and narrates the same confirmed stages.
  if (!reduced && recvid) {
    void playClip(recvid, 'recovery',
      () => { /* success tail holds last frame; host dismisses */ }, /* hold */ true);
  }
  host({ type: 'recovery-attempt-started', attempt: recoveryAttempt });
}

function confirmStage(stage) {
  if (!attemptInFlight || !Number.isFinite(stage)) return;
  const next = Math.min(Math.trunc(stage), RECOVERY_STAGES.length - 1);
  if (next <= confirmedStage) return;
  confirmedStage = next;
  if (overlay === 'recovery' && recvid?.paused && !recvid.ended) {
    void recvid.play().catch(() => {});
  }
  if (confirmedStage === RECOVERY_STAGES.length - 1) recoveryOnlineAudio();
  changed(.16);
}

function recoveryFailed(msg = {}) {
  if (!clock.activeFault) return;
  const wasRecovering = overlay === 'recovery';
  attemptInFlight = false;
  confirmedStage = -1;
  if (msg.message !== undefined) clock.diagnostics.message = String(msg.message).slice(0, 500);
  clock.setRecoveryState('HUMAN_INTERVENTION_REQUIRED');
  syncRetryButton();
  if (wasRecovering && recvid) {
    recvid.pause(); recvid.hidden = true; recvid.classList.remove('live');
  }
  if (overlay === 'recovery-failed') { changed(.16); return; }  // already told — no replay
  if (wasRecovering && !reduced && failvid) {
    // Interrupted-recovery clip: branches from the contained recovery state,
    // never from a successful green ending.
    void playClip(failvid, 'recovery-failed', () => showIntervention());
  } else {
    // DOM fallback: pace the four failure captions over the contained end
    // state, then intervention.
    clock.showRecoveryImmediately();
    showFailedCaptions(0);
  }
  changed(.16);
}

function showFailedCaptions(i) {
  clearTimeout(failCaptionTimer);
  if (i < RECOVERY_FAILED_STAGES.length - 1) {
    setText('recovery-state', RECOVERY_FAILED_STAGES[i]);
    $('recovery').hidden = false; $('recovery').inert = false;
    $('recovery').style.opacity = '1';
    syncRetryButton();
    failCaptionTimer = setTimeout(() => showFailedCaptions(i + 1), 2700);
  } else showIntervention();
}

function showIntervention() {
  overlay = null;
  attemptInFlight = false;
  // Guarantee the contained end-state — the panel renders even if the real
  // fault timeline had not finished playing out when the attempt failed.
  clock.showRecoveryImmediately();
  syncRetryButton();
  if (!failed) paint();
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
      case 'recovery-begin': beginRecovery(msg); break;
      case 'recovery-stage': confirmStage(msg.stage); break;
      case 'recovery-failed': recoveryFailed(msg); break;
      case 'action-feedback': setText('action-feedback', String(msg.text ?? '')); break;
      case 'set-volume': audio.setVolume(clamp(msg.value ?? audio.volume)); break;
      case 'set-audio':
        if (!msg.enabled) { audio.disabled = true; audio.stop(); }
        else { audio.disabled = false; void audio.sync(clock); }
        break;
      case 'set-reduced': reduced = Boolean(msg.value); paint(); break;
      case 'complete-sequence': {
        // Host reached readiness — converge the remaining tail onto the
        // online state so the sequence ends WITH the truth instead of
        // trailing it at natural speed.
        if (clock.mode === 'normal' && !clock.held) {
          const onlineAt = manifest.events.find(e => e.id === 'stable_online')?.at ?? manifest.duration;
          const remaining = onlineAt - clock.time;
          if (remaining > .8) clock.setSpeed(clamp(remaining / 2.5, 1, 1.75));
        }
        break;
      }
      case 'play-voice': void playVoice(msg.id, msg.b64, msg.duck); break;
      case 'stop-voice': voice.stop(typeof msg.fade === 'number' ? msg.fade : .18); break;
      case 'dispose': clock.pause(); cancelAnimationFrame(raf); clearTimeout(failCaptionTimer); bootvid?.pause(); errvid?.pause(); recvid?.pause(); failvid?.pause(); void audio.dispose(); void voice.dispose(); break;
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
    beginRecovery,
    confirmStage,
    recoveryFailed,
    recovery() { return { overlay, confirmedStage, attemptInFlight, attempt: recoveryAttempt }; },
  };
  // The rendered scene clip takes over as the surface the moment its first
  // frame decodes — the DOM beneath already shows the same artwork, so the
  // takeover is a fade over identical pixels, not a surface swap.
  if (bootvid) {
    const activateVideo = () => {
      if (videoMode) return;
      videoMode = true;
      document.body.classList.add('video-mode');
      bootvid.classList.add('live');
      void bootvid.play().catch(() => {});
    };
    if (bootvid.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) activateVideo();
    else {
      bootvid.addEventListener('loadeddata', activateVideo, { once: true });
      bootvid.addEventListener('error', () => bootvid.classList.remove('live'), { once: true });
    }
  }
  clock.play();
  paint();
  raf = requestAnimationFrame(frame);
  host({ type: 'splash-ready' });
  void audio.initialize().then(() => changed(.16));
}

window.addEventListener('error', e => fail(e.error ?? new Error(e.message)));
window.addEventListener('unhandledrejection', e => fail(e.reason instanceof Error ? e.reason : new Error(String(e.reason))));
boot().catch(fail);
