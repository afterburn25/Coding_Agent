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

let clock, renderer, audio, voice, manifest, clipTimeline, raf;
let failed = false, reduced = false, externalProgress = null;
// Displayed fill eases toward the real value — the bar sweeps smoothly
// rather than snapping when the host's progress jumps.
let shownFill = 0;
let lastDraw = -Infinity, lastRevision = 0, lastPanel = false;
let previousHeld = false, completePosted = false, lastPhaseId = '';
// Rendered scene clips are the primary surface once decoded; the DOM
// layers beneath stay live as the fallback if playback never starts.
const bootvid = $('bootvid'), bootvid2 = $('bootvid2'), errvid = $('errvid'), recvid = $('recvid'), failvid = $('failvid');
// Two stacked copies of the boot clip ping-pong across the online-tail
// loop: the outgoing clip finishes under the incoming one's opacity fade,
// so the seam never shows a hard cut.
const bootclips = [bootvid, bootvid2].filter(Boolean);
let bootIdx = 0;
let videoMode = false;
// A sequence clip that owns the whole surface right now: 'error' (fault
// continuation), 'recovery' (user-requested attempt), 'recovery-failed'.
// While set, the DOM recovery panel stays hidden — the clip ends contained
// and THEN the real controls reappear.
let overlay = null;
// The last recovery milestone the HOST confirmed. The clip free-runs at its
// authored pace — only the final ONLINE boundary holds for confirmation, the
// one claim that may never precede the real attempt's outcome.
let confirmedStage = -1;
// Stages whose caption the clip has actually crossed — voice is requested
// at the visual boundary, not when the host confirms, so narration and
// footage stay in sync.
let shownStage = -1;
// Narration beats as clip-time triggers — the numbers come from the
// authored footage: iris opens ≈14.2s, power-up arc begins ≈16s, the green
// CORE SYSTEMS · ONLINE caption appears at 21.4s (past the honesty gate's
// hold point, so it cannot fire before the backend is confirmed back).
const RECOVERY_NARRATION = [
  { at: .1,   stage: 0 },   // clip start — "Attempting to reinitialize the core."
  { at: 12.0, stage: 6 },   // ends just as the iris opens (~14.2) — "Releasing containment."
  { at: 15.4, stage: 7 },   // core surges bright ~15.8 — "…Powering the core."
  { at: 21.35, stage: 8 },  // green caption — "The core is back online."
];
let narratedUpTo = -1;
// The boot clip's authored online tail (startup_caption_timeline.json →
// onlineTail) loops until the host dismisses the splash — the surface and
// its baked hum stay alive while the app finishes opening instead of
// freezing on 'ended'. The swap crossfades so any residual discontinuity
// hides in the blend. All times come from the timeline file — never
// duplicated as constants here.
const BOOT_LOOP_XFADE_MS = 400, MAX_CATCHUP = 1.75;
// caption gates sorted by progressGate: [{id, gate, at}] — progress is REAL
// (host StartupProgress), `at` is the authored clip time of that caption.
let clipGates = [];
let onlineGate = 1.0, onlineAt = 0;
// The authored finalizing hold frame — the clip may not pass it while
// progress is still below the online gate.
let holdAt = Infinity;
let bootLoopStart = 0, bootLoopEnd = Infinity;
let bootTailReached = false, bootSwapping = false, bootMutedIncoming = null;
let lastClipCaption = -1;

// Real displayed progress → authored clip time. Between caption gates the
// mapping interpolates inside the segment so the footage paces reality; a
// gate is a hard boundary the clip may approach but never cross ahead of
// the real milestone. Returns null once released (progress ≥ online gate)
// — the clip then catches up to the online boundary and free-runs the tail.
function progressToAuthoredTime(p) {
  if (!clipGates.length || p >= onlineGate) return null;
  if (p <= clipGates[0].gate) return clipGates[0].at;
  for (let i = 0; i < clipGates.length - 1; i++) {
    const a = clipGates[i], b = clipGates[i + 1];
    if (p < b.gate) {
      return a.at + (p - a.gate) / (b.gate - a.gate) * (b.at - a.at);
    }
  }
  return onlineAt;
}

// The displayed bar value bounds the clip too: while the bar is parked at
// the finalizing ceiling (~99.6%) the footage pins to the authored hold
// frame — the online caption cannot appear before true completion.
function clipTargetFor(progress) {
  const t = progressToAuthoredTime(progress);
  return t === null ? null : Math.min(t, holdAt);
}

// One diagnostics post per caption the clip actually enters — the host log
// then records real progress, authored clip time, hold and catch-up state
// for every transition without per-frame spam.
function reportClipCaption(v) {
  if (!clipGates.length || !v) return;
  let idx = -1;
  for (let i = 0; i < clipGates.length; i++) {
    if (v.currentTime >= clipGates[i].at - .02) idx = i;
  }
  if (idx > lastClipCaption) {
    lastClipCaption = idx;
    host({ type: 'caption-view', id: clipGates[idx].id, at: clipGates[idx].at,
           clipTime: +v.currentTime.toFixed(2), held: v.paused,
           rate: +v.playbackRate.toFixed(2), progress: externalProgress?.value ?? null });
  }
}

// Hand the online tail to the idle copy of the clip: it starts at the loop
// point and fades in over the outgoing clip, which keeps its baked audio
// until the swap settles — picture and hum both cross the seam cleanly.
function startBootSwap() {
  const cur = bootclips[bootIdx], nxt = bootclips[1 - bootIdx];
  bootTailReached = true; bootSwapping = true; bootMutedIncoming = nxt;
  // Timeline-less fallback: loop the last ~4s (the settled tail of either
  // master) rather than restarting the whole cinematic at 0.
  nxt.currentTime = bootLoopStart > 0 ? bootLoopStart : Math.max(0, cur.duration - 4);
  nxt.playbackRate = 1;   // release-path catch-up must not leak into the loop
  nxt.hidden = false;
  nxt.style.zIndex = 3; cur.style.zIndex = 2;
  void nxt.play().catch(() => {});
  void nxt.offsetWidth;                 // flush so the fade transition runs
  nxt.classList.add('live');
  bootIdx = 1 - bootIdx;
  setTimeout(() => {
    cur.pause(); cur.hidden = true; cur.classList.remove('live');
    bootMutedIncoming = null; applyMediaAudio(); bootSwapping = false;
  }, BOOT_LOOP_XFADE_MS);
}
// One in-flight user-requested attempt at a time — duplicate Retry clicks
// coalesce (button disabled here, single-flight guard on the host).
let attemptInFlight = false;
let recoveryAttempt = 0;
let failCaptionTimer = null;
// Media audio ownership: the sequence clips carry baked, picture-synced
// audio (produced by tools/video_export/mix_*.py). While a clip audibly
// plays it owns the mix and the stem engine must stay silent — otherwise
// the DOM-clock stems double/drift against the rate-corrected footage.
// When a clip pauses (progress hold, milestone gate) the stem engine's
// sustained hum comes back so a stall never goes mute.
let mediaWasLive = false;
let mediaDuck = 1;
let mediaHandoffT = null;

function clipOf(name) {
  return name === 'error' ? errvid : name === 'recovery' ? recvid
    : name === 'recovery-failed' ? failvid : bootvid;
}
function activeMedia() {
  return overlay ? clipOf(overlay) : (videoMode ? bootclips[bootIdx] : null);
}
function mediaOwnsMix() {
  const v = activeMedia();
  return Boolean(v && !v.paused && !v.ended);
}
function applyMediaAudio() {
  for (const v of [...bootclips, errvid, recvid, failvid]) {
    if (!v) continue;
    // An incoming loop clip stays muted until the swap completes — the
    // outgoing clip keeps its audio through the crossfade.
    v.muted = audio.disabled || v === bootMutedIncoming;
    v.volume = clamp(audio.volume * mediaDuck);
  }
}
function syncMediaAudio() {
  const live = mediaOwnsMix();
  applyMediaAudio();
  if (live === mediaWasLive) return;
  if (live) {
    // Clip is audibly playing — the stem engine hands the mix over.
    clearTimeout(mediaHandoffT);
    mediaWasLive = true;
    audio.stop(.12);
  } else {
    // Debounce the hand-back: rate-correction micro-pauses must not churn
    // the stem scheduler, or stems burst on top of the clip's own track.
    // Only a SUSTAINED pause (hold/gate) brings the hum back.
    clearTimeout(mediaHandoffT);
    mediaHandoffT = setTimeout(() => {
      if (mediaOwnsMix()) return;
      mediaWasLive = false;
      // Coverage for pauses the normal sync would leave silent:
      // a recovery clip held at a milestone gate gets a quiet ambient hum;
      // an ended clip still awaiting real readiness sustains the online hum;
      // a contained fault stays dead — its idle hum decays on its own.
      if (overlay === 'recovery') {
        void audio.initialize().then(ok => ok && audio.context.resume().then(() =>
          audio.schedule({ id: 'recovery_hold_hum', sound: 'ambient_hum', at: 0, gain: .16, loop: true, fadeIn: .5 }, 0, 1, Infinity)));
      } else if (!clock.playing && !clock.activeFault) {
        void audio.initialize().then(ok => ok && audio.context.resume().then(() =>
          audio.schedule({ id: 'sustain_online_hum', sound: 'stable_hum', at: 0, gain: .42, loop: true, fadeIn: .6 }, 0, 1, Infinity)));
      } else void audio.sync(clock, { fade: .12 });
    }, 300);
  }
}

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
  // The clip's playback position is bound to REAL progress through the
  // authored caption timeline: piecewise progress→clip-time mapping, hard
  // pause at a future caption boundary until its milestone lands, bounded
  // ≤1.75x catch-up when reality jumps ahead. A stalled backend parks the
  // picture (and its baked audio → stem hum sustains) rather than letting
  // the scene claim progress reality hasn't made. Once the host reports
  // true completion the clip is released across the online boundary and
  // free-runs into the authored looping tail until dismissal.
  if (videoMode && !clock.activeFault && bootclips.length) {
    const cur = bootclips[bootIdx];
    const dur = cur.duration || clipTimeline?.duration || 30;
    const loopAt = Math.min(bootLoopEnd, dur) - .06;
    if (!bootSwapping && (cur.ended || cur.currentTime >= loopAt)) {
      // Tail reached — the pacing gate only lets the clip get this far
      // once real progress released the online boundary, so looping the
      // authored online tail until dismissal is honest.
      startBootSwap();
    } else if (!bootSwapping) {
      const prog = externalProgress?.value ?? clamp(clock.time / manifest.duration);
      const target = clipGates.length
        ? clipTargetFor(prog) : Math.min(prog, .985) * dur;
      if (target === null) {
        // Released — real completion confirmed. Catch up to the authored
        // online boundary at bounded speed, then settle to natural pace.
        const rate = cur.currentTime < onlineAt ? MAX_CATCHUP : 1;
        if (cur.paused && !cur.ended) void cur.play().catch(() => {});
        if (Math.abs(cur.playbackRate - rate) > .01) cur.playbackRate = rate;
      } else {
        const drift = target - cur.currentTime;
        if (drift <= -.05) {
          // Ahead of real progress — hold before the next caption boundary.
          if (!cur.paused) cur.pause();
        } else {
          if (cur.paused) void cur.play().catch(() => {});
          // Rate follows the gap: a .35s lead is natural speed, deeper
          // backlog ramps toward the 1.75x cap, near-zero drifts crawl.
          const rate = clamp(drift / .35, .15, MAX_CATCHUP);
          if (Math.abs(cur.playbackRate - rate) > .01) cur.playbackRate = rate;
        }
      }
      // Fires on both the gated and released paths — crossing the online
      // caption is itself a logged transition.
      reportClipCaption(cur);
    }
  }
  $('stage').classList.toggle('fault', Boolean(state.fault));
  document.body.classList.toggle('overlay-clip', overlay !== null);
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
      if (clock.revision !== lastRevision) { lastRevision = clock.revision; if (!mediaOwnsMix()) void audio.sync(clock, { fade: .16 }); }
      if (previousHeld !== clock.held) { previousHeld = clock.held; if (!mediaOwnsMix()) void audio.sync(clock); }
      lastDraw = now;
      // Audio ownership follows whichever surface is actually playing —
      // baked clip audio while a clip runs, stem engine during holds/DOM.
      syncMediaAudio();
      // Recovery honesty gate: the clip runs at its authored pace through
      // the attempt — it may only be held at the ONLINE boundary, the one
      // claim that must be true. The footage pauses just before the green
      // caption until the host confirms the backend actually came back.
      if (overlay === 'recovery' && recvid && !recvid.paused && !recvid.ended
          && confirmedStage < RECOVERY_STAGE_TIMES.length - 1
          && recvid.currentTime >= RECOVERY_STAGE_TIMES[RECOVERY_STAGE_TIMES.length - 1] - .1) {
        recvid.pause();
      }
      // Narration schedule — keyed to the footage's own visual beats, not
      // caption boundaries: the iris opens ~14.2s, the power-up arc starts
      // ~16s, the green online caption appears at 21.4s. The online beat
      // sits past the honesty gate's hold point, so it can only fire once
      // the backend is genuinely confirmed back.
      while (overlay === 'recovery' && recvid && !recvid.ended
             && narratedUpTo + 1 < RECOVERY_NARRATION.length
             && recvid.currentTime >= RECOVERY_NARRATION[narratedUpTo + 1].at) {
        narratedUpTo++;
        host({ type: 'recovery-stage-shown', stage: RECOVERY_NARRATION[narratedUpTo].stage });
      }
      if (clock.playing) {
        const state = paint();
        if (state.phase.id !== lastPhaseId) { lastPhaseId = state.phase.id; host({ type: 'phase', id: state.phase.id }); }
        // sequence-complete means the online claim is actually on screen:
        // in video mode the clip must have crossed the authored online
        // caption boundary (it then free-runs/loops the tail until
        // dismissal); on the DOM fallback the timeline's own online state
        // is the confirmation. The clip can only reach that boundary after
        // displayed progress hit the online gate — the host's real
        // completion — so this never precedes the truth.
        if (!completePosted && clock.mode === 'normal' && (videoMode
            ? (externalProgress?.value ?? 0) >= onlineGate - 1e-9
              && (bootclips[bootIdx]?.currentTime ?? 0) >= onlineAt
            : state.online)) {
          completePosted = true;
          host({ type: 'sequence-complete' });
        }
      }
    }
    raf = requestAnimationFrame(frame);
  } catch (error) { fail(error); }
}

function changed(fade = .008) {
  if (failed) { if (clock?.activeFault) updateRecovery({ panel: 1, contained: true }); return; }
  try {
    previousHeld = clock.held; lastRevision = clock.revision; paint();
    if (mediaOwnsMix()) applyMediaAudio(); else void audio.sync(clock, { fade });
  }
  catch (error) { fail(error); }
}

// The error continuation clip branches from the fully powered startup frame —
// it only matches reality when the fault landed at/after full power. An early
// fault falls back to the DOM containment animation so partial geometry and
// partial progress are the actual state, not a jumped-to final frame.
function useErrorClip(progress) {
  return !reduced && videoMode && errvid
    && (bootclips.some(v => v.ended) || bootTailReached || clock.held || progress >= .9);
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
    if (bootclips.length) setTimeout(() => bootclips.forEach(v => v.pause()), 400);
  } else if (started && videoMode && bootclips.length) {
    // Early/mid fault while the clip ruled the surface: hand back to the
    // canvas so containment plays from the real partial geometry.
    videoMode = false;
    document.body.classList.remove('video-mode');
    bootclips.forEach(v => { v.classList.remove('live'); v.pause(); });
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

// The successful green tail — a real stem cue once the host confirms
// ONLINE, for the DOM fallback path only. When the recovery clip owns the
// surface its baked audio already carries the online score; doubling it
// would be exactly the desync this design removed.
function recoveryOnlineAudio() {
  if (overlay === 'recovery') return;
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
  confirmedStage = 0; shownStage = -1;                 // containment engaged IS stage 0
  narratedUpTo = -1;                                 // narration schedule restarts
  clearTimeout(recoveryWatchdog);                      // attempt supersedes the guard
  clearTimeout(failCaptionTimer);                      // a pending fail-caption chain must not clear this attempt
  clock.setRecoveryState('REPAIR_ATTEMPT', { attempt: recoveryAttempt, total: recoveryAttempt });
  syncRetryButton();
  changed(.16);
  // Video path when the clip can actually play; otherwise the DOM panel
  // stays up and narrates the same confirmed stages.
  if (!reduced && recvid) {
    // Pre-buffer the failure clip while the attempt runs — a failure then
    // swaps footage without a cold-decode stall on a dead surface.
    try { failvid?.load(); } catch { }
    void playClip(recvid, 'recovery', () => {
      // Success tail (stage 8 confirmed): hold the final green frame and
      // tell the host the authored sequence finished — the app shows only
      // after this plus the host's own dwell/voice gates. An ending before
      // full confirmation means the host stopped driving — fall back to
      // the intervention panel rather than a dead surface.
      if (confirmedStage >= RECOVERY_STAGES.length - 1) {
        host({ type: 'recovery-sequence-complete' });
      } else { overlay = null; changed(.16); }
    }, /* hold */ true);
  }
  host({ type: 'recovery-attempt-started', attempt: recoveryAttempt });
}

function confirmStage(stage) {
  if (!attemptInFlight || !Number.isFinite(stage)) return;
  const next = Math.min(Math.trunc(stage), RECOVERY_STAGES.length - 1);
  if (next <= confirmedStage) return;
  confirmedStage = next;
  // DOM fallback shows the stage label immediately — the shown moment IS
  // the confirm moment there.
  if (overlay !== 'recovery' && confirmedStage > shownStage) {
    shownStage = confirmedStage;
    host({ type: 'recovery-stage-shown', stage: shownStage });
  }
  // The only pause the clip can be sitting in is the online gate — release
  // it solely when the final stage itself is confirmed.
  if (overlay === 'recovery' && recvid?.paused && !recvid.ended
      && confirmedStage >= RECOVERY_STAGES.length - 1) {
    void recvid.play().catch(() => {});
  }
  if (confirmedStage === RECOVERY_STAGES.length - 1) {
    recoveryOnlineAudio();
    // DOM fallback has no clip to wait on — the online fade is the
    // sequence's natural end, so report completion after it settles.
    if (overlay !== 'recovery')
      setTimeout(() => host({ type: 'recovery-sequence-complete' }), 1900);
  }
  changed(.16);
}

function recoveryFailed(msg = {}) {
  if (!clock.activeFault) return;
  const wasRecovering = overlay === 'recovery';
  attemptInFlight = false;
  confirmedStage = -1;
  if (msg.message !== undefined) clock.diagnostics.message = String(msg.message).slice(0, 500);
  if (msg.detail !== undefined) clock.diagnostics.detail = String(msg.detail).slice(0, 12000);
  clock.setRecoveryState('HUMAN_INTERVENTION_REQUIRED');
  syncRetryButton();
  if (wasRecovering && recvid) {
    recvid.pause(); recvid.hidden = true; recvid.classList.remove('live');
  }
  if (overlay === 'recovery-failed') { changed(.16); return; }  // already told — no replay
  // The failure clip also covers a preflight failure — the host may declare
  // the attempt unrecoverable before the recovery clip ever starts, and the
  // footage still plays rather than jumping straight to the panel.
  if (!reduced && failvid) {
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
let voiceSeq = 0;
async function playVoice(id, b64, duckLevel) {
  const seq = ++voiceSeq;
  try {
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0)).buffer;
    mediaDuck = duckLevel ?? .6; applyMediaAudio();      // duck clip audio too
    const res = await voice.play(bytes, { duckLevel: duckLevel ?? .6 });
    host({ type: 'voice-result', id, started: res.started, seconds: res.seconds ?? 0 });
    if (res.started && res.done) res.done.then(() => {
      // A preempted clip still resolves done — only the latest playback may
      // lift the duck, or an interrupted line would un-duck its replacement.
      if (seq === voiceSeq) { mediaDuck = 1; applyMediaAudio(); }
      host({ type: 'voice-ended', id });
    });
    else { if (seq === voiceSeq) { mediaDuck = 1; applyMediaAudio(); } host({ type: 'voice-ended', id }); }
  } catch (error) {
    if (seq === voiceSeq) { mediaDuck = 1; applyMediaAudio(); }
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
      case 'set-volume': audio.setVolume(clamp(msg.value ?? audio.volume)); applyMediaAudio(); break;
      case 'set-audio':
        if (!msg.enabled) { audio.disabled = true; audio.stop(); }
        else { audio.disabled = false; void audio.sync(clock); }
        applyMediaAudio();
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
      case 'stop-voice': voice.stop(typeof msg.fade === 'number' ? msg.fade : .18); mediaDuck = 1; applyMediaAudio(); break;
      case 'dispose': clock.pause(); cancelAnimationFrame(raf); clearTimeout(failCaptionTimer); bootclips.forEach(v => v.pause()); errvid?.pause(); recvid?.pause(); failvid?.pause(); void audio.dispose(); void voice.dispose(); break;
    }
  } catch (error) { fail(error); }
}

async function boot() {
  const response = await fetch('../animation_manifest.json');
  if (!response.ok) throw new Error('Timeline asset missing');
  manifest = validateManifest(await response.json());
  // The authored caption timeline is the single source for clip geometry:
  // real-progress gates → clip times, the finalizing hold frame, and the
  // online tail loop bounds. A missing file degrades to the legacy linear
  // pacing rather than fabricating gates.
  try {
    const t = await fetch('../startup_caption_timeline.json');
    if (t.ok) clipTimeline = await t.json();
  } catch { clipTimeline = null; }
  const caps = clipTimeline?.captions;
  if (Array.isArray(caps) && caps.length >= 2) {
    clipGates = caps
      .filter(c => Number.isFinite(c.progressGate) && Number.isFinite(c.at))
      .sort((a, b) => a.progressGate - b.progressGate)
      .map(c => ({ id: c.id, gate: c.progressGate, at: c.at }));
    const onlineCap = clipGates.find(c => c.id === 'online') ?? clipGates[clipGates.length - 1];
    onlineGate = onlineCap.gate;
    onlineAt = onlineCap.at;
    holdAt = clipTimeline?.finalizingHold?.at ?? onlineAt;
    bootLoopStart = clipTimeline?.onlineTail?.loopStart ?? onlineAt;
    bootLoopEnd = clipTimeline?.onlineTail?.loopEnd ?? (clipTimeline?.duration ?? Infinity);
  }
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
      applyMediaAudio();
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
  // splash-ready means the surface can actually show frame one — the host
  // holds real startup work on it so the bar starts at a true cold 0%
  // instead of appearing mid-flight. Missing/broken media still reports
  // ready (the DOM fallback is a valid surface); the bound below is the
  // last resort for a wedged decoder.
  if (!reduced && bootvid) {
    let signaled = false;
    const ready = () => { if (!signaled) { signaled = true; host({ type: 'splash-ready' }); } };
    if (videoMode) ready();
    else {
      bootvid.addEventListener('loadeddata', ready, { once: true });
      bootvid.addEventListener('error', ready, { once: true });
      setTimeout(ready, 2500);
    }
  } else host({ type: 'splash-ready' });
  void audio.initialize().then(() => { applyMediaAudio(); changed(.16); });
}

window.addEventListener('error', e => fail(e.error ?? new Error(e.message)));
window.addEventListener('unhandledrejection', e => fail(e.reason instanceof Error ? e.reason : new Error(String(e.reason))));
boot().catch(fail);
