/* Nexus Core — global voice client.
   Plays backend-synthesized WAV segments in order, tracks global mute,
   and exposes NexusVoice to every page. Mute stops playback immediately
   (local stop + server-side queue cancel). */
(function () {
  const NV = {
    muted: false,
    enabled: true,
    queue: [],
    current: null,
    volume: 1.0,
    status: null,
    listeners: [],
    _playSeq: 0,
  };

  function api(path, body) {
    const opt = body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    };
    return fetch(path, opt).then(r => r.json()).catch(() => ({}));
  }

  NV.refresh = async function () {
    const s = await api('/api/voice/status');
    if (s && s.enabled !== undefined) {
      NV.status = s;
      NV.muted = !!s.muted;
      NV.enabled = !!s.enabled;
      if (s.volume != null) NV.volume = s.volume;
      NV._emit();
    }
    return s;
  };

  NV.setMuted = async function (muted) {
    NV.muted = !!muted;
    if (muted) NV.stop();
    NV._emit();
    await api('/api/voice/mute', { muted: !!muted });
  };

  NV.stop = function () {
    NV.queue.length = 0;
    if (NV.current) {
      try { NV.current.pause(); NV.current.src = ''; } catch (e) {}
      NV.current = null;
      NV._lastEnd = Date.now();
    }
    NV._playSeq++;
    NV._emit();
    api('/api/voice/stop', { reason: 'user' });
  };

  NV._seenSegments = new Set();
  NV.enqueue = function (url, meta) {
    if (NV.muted || !NV.enabled) return;
    const sid = meta && meta.segment_id;
    if (sid && NV._seenSegments.has(sid)) return;  // bus + stream dedupe
    if (sid) {
      NV._seenSegments.add(sid);
      if (NV._seenSegments.size > 500) {
        NV._seenSegments = new Set([...NV._seenSegments].slice(-400));
      }
    }
    NV.queue.push({ url, meta });
    NV._playNext();
    NV._emit();
  };

  NV._lastEnd = 0;
  NV._gapTimer = null;
  NV._playNext = function () {
    if (NV.current || !NV.queue.length) return;
    // Global rule: clips never overlap — the next one may only start once
    // 2s have passed since the last clip finished playing.
    const wait = NV._lastEnd + 2000 - Date.now();
    if (wait > 0) {
      if (!NV._gapTimer) {
        NV._gapTimer = setTimeout(() => { NV._gapTimer = null; NV._playNext(); }, wait);
      }
      return;
    }
    const item = NV.queue.shift();
    const audio = new Audio(item.url);
    audio.volume = Math.min(1, Math.max(0, NV.volume));
    const seq = ++NV._playSeq;
    audio.onended = () => { if (NV.current === audio) { NV.current = null; NV._lastEnd = Date.now(); NV._playNext(); NV._emit(); } };
    audio.onerror = () => { if (NV.current === audio) { NV.current = null; NV._playNext(); NV._emit(); } };
    NV.current = audio;
    audio.play().catch(err => {
      if (err && err.name === 'NotAllowedError') {
        // Autoplay policy blocked playback — hold the segment and replay it
        // on the next user gesture instead of dropping it silently.
        const retry = () => {
          if (NV.current !== audio) return; // stopped/replaced meanwhile
          NV.current = null;
          if (!NV.muted && NV.enabled) NV.queue.unshift(item);
          NV._playNext();
        };
        document.addEventListener('pointerdown', retry, { once: true });
        document.addEventListener('keydown', retry, { once: true });
        return;
      }
      if (NV.current === audio) { NV.current = null; NV._playNext(); }
    });
  };

  NV.speak = async function (text, opts) {
    if (NV.muted || !NV.enabled) return null;
    const out = await api('/api/voice/speak', Object.assign({ text: String(text || '') }, opts || {}));
    if (out && out.url) NV.enqueue(out.url, { manual: true });
    return out;
  };

  NV.onEvent = function (data) {
    const e = data || {};
    // /api/events replays recent history on connect — never re-speak audio
    // that is more than a few seconds old.
    if (e.event === 'segment' && e.url) {
      if (e.ts && Date.now() / 1000 - Number(e.ts) > 15) return;
      NV.enqueue(e.url, e); NV._emit(e);
    }
    else if (e.event === 'stop' || e.event === 'muted') {
      // Replayed history must never kill live playback — a 'stop' from
      // the previous session's shutdown would otherwise silence whatever
      // is speaking seconds after page load.
      if (e.ts && Date.now() / 1000 - Number(e.ts) > 15) return;
      if (e.event === 'stop') { NV.queue.length = 0; if (NV.current) { try { NV.current.pause(); } catch (_) {} NV.current = null; NV._lastEnd = Date.now(); } } NV.refresh(); }
    else NV._emit(e);
  };

  NV.on = function (fn) { NV.listeners.push(fn); };
  NV._emit = function (evt) {
    for (const fn of NV.listeners) { try { fn(evt, NV); } catch (e) {} }
    const btn = document.getElementById('voiceToggle');
    if (btn) {
      btn.classList.toggle('muted', NV.muted);
      const label = btn.querySelector('span:last-child');
      const icon = btn.querySelector('.nav-icon');
      if (label) label.textContent = NV.muted ? 'Muted' : 'Voice On';
      if (icon) icon.textContent = NV.muted ? '🔇' : '🔊';
      btn.title = NV.muted ? 'Voice muted — click to unmute' : 'Voice on — click to mute';
    }
    // Icon-only mute buttons (e.g. next to the composer send button).
    for (const mb of document.querySelectorAll('.voice-mute-btn')) {
      mb.classList.toggle('muted', NV.muted);
      mb.textContent = NV.muted ? '🔇' : '🔊';
      mb.title = NV.muted ? 'Voice muted — click to unmute' : 'Voice on — click to mute';
      mb.setAttribute('aria-label', mb.title);
    }
  };

  // Wire the global speaker button + shared event bus.
  document.addEventListener('DOMContentLoaded', async () => {
    const btn = document.getElementById('voiceToggle');
    if (btn) btn.addEventListener('click', () => NV.setMuted(!NV.muted));
    for (const mb of document.querySelectorAll('.voice-mute-btn')) {
      mb.addEventListener('click', () => NV.setMuted(!NV.muted));
    }
    await NV.refresh();
    // Shared bus mirrors voice stop/segment events so other pages react.
    try {
      const es = new EventSource('/api/events');
      es.addEventListener('voice', ev => {
        try { NV.onEvent(JSON.parse(ev.data)); } catch (e) {}
      });
      // Semantic gesture events from the Vocalization Engine — consumed
      // by the avatar layer when it exists; stashed + reflected on
      // <body data-gesture> so CSS/visual hooks can already react.
      const gq = [];
      es.addEventListener('gesture', ev => {
        try {
          const g = JSON.parse(ev.data);
          gq.push(g); if (gq.length > 24) gq.shift();
          window.NexusGestures = { last: g, recent: gq };
          document.body.dataset.gesture = g.gesture || '';
          clearTimeout(window.NexusGestures._clear);
          window.NexusGestures._clear = setTimeout(() => {
            if (document.body.dataset.gesture === g.gesture) {
              document.body.dataset.gesture = '';
            }
          }, 900);
          if (window.NexusAvatar && window.NexusAvatar.gesture) {
            window.NexusAvatar.gesture(g);
          }
        } catch (e) {}
      });
    } catch (e) {}
  });

  window.NexusVoice = NV;
})();
