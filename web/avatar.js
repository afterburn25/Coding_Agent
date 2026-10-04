/* Nexus Core — bounded avatar presentation layer.
   Drives the canonical portrait from observable runtime activity only:
   mic state, agent streaming, voice playback, semantic gestures, and the
   operational-state endpoint. It never blocks or delays voice playback. */
(function () {
  const el = document.getElementById('nexusPresence');
  if (!el) return;
  const face = el.querySelector('img');
  if (face) {
    const fallback = () => {
      if (face.dataset.fallback) return;
      face.dataset.fallback = '1';
      face.src = '/assets/nexus-core-icon.png';
    };
    face.addEventListener('error', fallback);
    if (face.complete && face.naturalWidth === 0) fallback();
  }

  const EXPRESSION_BY_GESTURE = {
    small_smile: 'friendly', amused_expression: 'happy',
    playful_expression: 'playful', focused_expression: 'focused',
    concerned_expression: 'concerned', confident_expression: 'confident',
    nod: 'confident', small_nod: 'friendly', head_tilt: 'playful',
    eyebrow_raise: 'playful', shake_head: 'concerned',
    small_head_shake: 'concerned', eyes_widen: 'playful',
    subtle_exhale: 'neutral', small_frown: 'concerned',
    eye_narrow: 'concerned', smirk: 'playful', soft_gaze: 'friendly',
    bright_smile: 'happy', slow_blink: 'neutral', slight_lean: 'focused',
    contented_expression: 'friendly', wince: 'concerned',
    look_away: 'neutral', soft_smile: 'friendly',
  };
  const OPERATIONAL_STATE = {
    focused: 'focused', pressured: 'concerned', concerned: 'concerned',
  };

  let listening = false;
  let processing = false;
  let speaking = false;
  let speakUntil = 0;
  let speakStart = 0;
  let utteranceSeed = 1;
  let operational = 'idle';
  let expression = 'neutral';
  let expressionUntil = 0;

  function apply() {
    const now = Date.now();
    const currentExpression = now < expressionUntil ? expression : 'neutral';
    const state = speaking ? 'speaking'
      : listening ? 'listening'
      : (processing || (typeof agentStreamActive !== 'undefined' && agentStreamActive)) ? 'thinking'
      : operational;
    el.dataset.state = state;
    el.dataset.expression = currentExpression;
    el.title = 'Nexus Core — ' + state +
      (currentExpression !== 'neutral' ? ' · ' + currentExpression : '');
  }

  function seedUtterance(id) {
    const s = String(id || 'segment');
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i); h = Math.imul(h, 16777619);
    }
    return (h >>> 0) / 4294967295;
  }

  const NA = {
    setListening(on) { listening = !!on; apply(); },
    setThinking(on) { processing = !!on; apply(); },
    speaking(seconds, segmentId) {
      const dur = Math.max(0.8, Math.min(30, Number(seconds) || 2));
      const now = Date.now();
      if (!speaking) speakStart = now;
      utteranceSeed = seedUtterance(segmentId);
      speaking = true;
      // Queue-aware approximation: later utterance events extend the existing
      // speaking window rather than restarting it.
      speakUntil = Math.min(
        Math.max(speakUntil, now) + dur * 1000 + 180,
        now + 60000);
      apply();
    },
    stopSpeaking() {
      speaking = false; speakUntil = 0;
      el.style.removeProperty('--mouth-open');
      delete el.dataset.viseme;
      apply();
    },
    gesture(g) {
      const name = String((g && g.gesture) || g || '');
      const hint = EXPRESSION_BY_GESTURE[name];
      if (!hint) return;
      expression = hint;
      expressionUntil = Date.now() + 6000;
      apply();
    },
    operational(state) {
      operational = OPERATIONAL_STATE[String(state || '')] || 'idle';
      apply();
    },
  };
  window.NexusAvatar = NA;

  if (window.NexusVoice && window.NexusVoice.on) {
    window.NexusVoice.on(evt => {
      if (!evt) return;
      if (evt.event === 'segment') NA.speaking(evt.seconds, evt.segment_id);
      else if (evt.event === 'stop' || evt.event === 'muted' || evt.event === 'error') {
        NA.stopSpeaking();
      }
    });
  }

  async function refreshOperational() {
    try {
      const res = await fetch('/api/nexus/state');
      if (!res.ok) return;
      const data = await res.json();
      NA.operational(data && data.state);
    } catch (e) {}
  }

  function tick() {
    const nv = window.NexusVoice;
    const audioLive = !!(nv && nv.current && !nv.current.paused && !nv.current.ended);
    if (audioLive) {
      if (!speaking) speakStart = Date.now();
      speaking = true;
    }
    if (speaking && !audioLive && Date.now() > speakUntil) {
      NA.stopSpeaking();
    }
    if (speaking) {
      // Lightweight viseme approximation: no phoneme timing or audio payload is
      // consumed — utterance timing plus a stable segment-derived phase varies
      // the mouth cue while playback is active.
      const t = audioLive && Number.isFinite(nv.current.currentTime)
        ? nv.current.currentTime
        : Math.max(0, (Date.now() - speakStart) / 1000);
      const wave = Math.sin(t * 9.7 + utteranceSeed * 6.28) * 0.72
        + Math.sin(t * 17.3 + utteranceSeed * 11.1) * 0.28;
      const energy = Math.max(0.18, Math.min(1, 0.42 + Math.abs(wave) * 0.58));
      el.style.setProperty('--mouth-open', energy.toFixed(2));
      el.dataset.viseme = energy > 0.74 ? 'wide' : energy > 0.42 ? 'open' : 'rest';
    }
    apply();
  }

  refreshOperational();
  setInterval(refreshOperational, 20000);
  setInterval(tick, 200);
  apply();
})();
