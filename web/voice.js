/* Nexus Core Voice Studio — preset editing, A/B preview, import/export. */
(function () {
  const $ = s => document.querySelector(s);
  const api = (p, b) => fetch(p, b === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(b),
  }).then(r => r.json());

  let presets = [];
  let current = null;      // working copy of the selected preset
  let dirty = false;
  let lastSegment = null;

  const EQ_SLOTS = [
    { id: 'eqWarmth', freq: 220, label: 'warmth' },
    { id: 'eqPresence', freq: 3400, label: 'presence' },
    { id: 'eqAir', freq: 7200, label: 'air' },
  ];

  function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

  async function boot() {
    await NexusVoice.refresh();
    await Promise.all([loadPresets(), loadVoices(), loadStatus()]);
    wire();
    if (presets.length) selectPreset(presets[0].id);
  }

  async function loadPresets() {
    const d = await api('/api/voice/presets');
    presets = d.presets || [];
    renderPresetList();
  }

  async function loadVoices(engine) {
    const d = await api('/api/voice/voices' + (engine ? '?engine=' + encodeURIComponent(engine) : ''));
    const sel = $('#baseVoice');
    sel.innerHTML = (d.voices || []).map(v =>
      `<option value="${esc(v.id)}">${esc(v.id)} — ${esc(v.label)}${v.lang ? ' · ' + esc(v.lang) : ''}</option>`).join('')
      || '<option value="bf_isabella">bf_isabella</option>';
  }

  async function loadStatus() {
    const s = await api('/api/voice/status');
    const el = $('#engineStatus');
    if (!s.enabled) { el.innerHTML = 'Voice subsystem disabled.'; return; }
    const eng = s.engine || {};
    const assets = eng.assets || {};
    const missing = Object.entries(assets).filter(([, a]) => !a.verified).map(([k]) => k);
    const isCb = eng.name === 'chatterbox';
    el.innerHTML =
      `<div><b>${esc(eng.name || 'kokoro')}</b> ${esc(eng.version || '')}</div>` +
      `<div>${eng.loaded ? '✓ model loaded' : '○ model cold'} · ${eng.load_time_s || 0}s load` +
      (isCb ? ` · ${esc(eng.device || 'auto')} device` : '') + `</div>` +
      (isCb
        ? (eng.available
          ? `<div>✓ runtime + model ready${eng.worker_alive ? ' · worker live' : ''}</div>`
          : '<div class="voice-warn">Chatterbox runtime or model not installed — provisioning handles setup</div>')
        : (missing.length ? `<div class="voice-warn">Missing assets: ${esc(missing.join(', '))} — press Setup engine</div>`
                          : '<div>✓ assets verified</div>')) +
      (eng.rtf ? `<div>RTF ${eng.rtf} · ${eng.synth_audio_s}s audio in ${eng.synth_cpu_s || eng.synth_gen_s || 0}s</div>` : '');
    const m = $('#voiceMetrics');
    m.innerHTML =
      `Engine: ${esc(eng.name || '-')} ${esc(eng.version || '')}<br>` +
      `Loaded: ${eng.loaded ? 'yes' : 'no'} · load ${eng.load_time_s || 0}s` +
      (eng.device ? ` · ${esc(eng.device)}` : '') + `<br>` +
      `Synthesis calls: ${eng.synth_calls || 0} · RTF ${eng.rtf ?? '—'}` +
      (eng.vram_alloc_mb ? ` · VRAM ${eng.vram_alloc_mb} MB` : '') + `<br>` +
      `Cache: ${(s.cache && (s.cache.bytes / 1048576).toFixed(1)) || 0} MB / ${s.cache ? s.cache.entries : 0} files<br>` +
      `Queue: ${s.queue || 0} · Muted: ${s.muted ? 'yes' : 'no'} · Mode: ${esc(s.mode || '')}<br>` +
      `Upstream: ${esc(((eng.upstream || {}).model_repo) || eng.model || '')} (${esc(((eng.upstream || {}).license) || '')})`;
    $('#autoRead').checked = s.mode === 'responses' || s.mode === 'responses_activity';
    $('#voiceEnabled').checked = !!s.enabled;
  }

  function renderPresetList() {
    $('#presetList').innerHTML = presets.map(p =>
      `<div class="vp${current && current.id === p.id ? ' active' : ''}${dirty && current && current.id === p.id ? ' dirty' : ''}" data-id="${esc(p.id)}">` +
      `${esc(p.name)}${p.official ? '<span class="official-tag">official</span>' : ''}` +
      `<small>${esc(p.engine)} · ${esc(p.base_voice)} · ${Math.round((p.synthetic ?? 0) * 100)}% synthetic</small></div>`).join('')
      || '<span class="muted">No presets</span>';
  }

  function selectPreset(id) {
    const p = presets.find(x => x.id === id);
    if (!p) return;
    const prevEngine = current && current.engine;
    current = JSON.parse(JSON.stringify(p));
    dirty = false;
    $('#presetBadge').textContent = p.name + (p.official ? ' · official' : '');
    if (p.engine !== prevEngine) loadVoices(p.engine).then(() => {
      $('#baseVoice').value = p.base_voice;
    });
    syncControls();
    renderPresetList();
    loadLab(p.engine);
  }

  // -- controls <-> preset ------------------------------------------------
  function syncControls() {
    const p = current;
    $('#presetEngine').value = p.engine || 'kokoro';
    $('#baseVoice').value = p.base_voice;
    set('pitch', p.pitch_semitones); set('tempo', p.tempo);
    EQ_SLOTS.forEach(s => {
      const band = (p.eq || []).find(b => Math.abs(b.freq_hz - s.freq) < 40);
      set(s.id, band ? band.gain_db : 0);
    });
    set('exciter', p.exciter || 0);
    set('compRatio', (p.compression || {}).ratio || 1);
    set('synthetic', p.synthetic ?? 0.8);
    set('neuralMix', p.neural?.mix ?? 0);
    set('glassMix', p.glass?.mix ?? 0);
    set('microMix', p.micro?.mix ?? 0);
    set('stereoWidth', p.stereo_width ?? 1);
    set('neuralBits', p.neural?.bit_depth ?? 9);
    set('neuralWet', p.neural?.wet ?? 0.7);
    set('neuralDecay', p.neural?.decay ?? 0.28);
    set('neuralAM', p.neural?.am_depth ?? 0.16);
    set('glassPitch', p.glass?.pitch_factor ?? 1.245);
    set('microPitch', p.micro?.pitch_factor ?? 0.965);
    set('formantPreserve', p.formant_preserve ?? 0.4);
    set('limiter', p.limiter_ceiling ?? 0.89);
    set('outGain', p.output_gain_db ?? 0);
    set('targetLufs', p.loudness_target_lufs ?? -17);
    $('#normLoudness').checked = !!p.normalize_loudness;
    $('#limiterOn').checked = p.limiter_enabled !== false;
  }

  function set(id, v) {
    const el = $('#' + id);
    el.value = v;
    const out = $('#' + id + 'Val');
    if (out) out.textContent = fmt(id, v);
  }

  function fmt(id, v) {
    v = Number(v);
    if (id === 'synthetic') return Math.round(v * 100) + '%';
    if (id === 'pitch') return (v > 0 ? '+' : '') + v.toFixed(2) + ' st';
    if (id === 'tempo') return v.toFixed(2) + 'x';
    if (id.startsWith('eq')) return (v > 0 ? '+' : '') + v.toFixed(1) + ' dB';
    if (id === 'limiter') return v.toFixed(2) + ' fs';
    if (id === 'outGain') return (v > 0 ? '+' : '') + v.toFixed(1) + ' dB';
    if (id === 'targetLufs') return v.toFixed(1) + ' LUFS';
    return String(v);
  }

  function pullControls() {
    const p = current;
    p.engine = $('#presetEngine').value;
    p.base_voice = $('#baseVoice').value;
    p.pitch_semitones = +$('#pitch').value;
    p.tempo = +$('#tempo').value;
    p.eq = EQ_SLOTS.map(s => ({
      freq_hz: s.freq, gain_db: +$('#' + s.id).value, q: 1.1,
    }));
    p.exciter = +$('#exciter').value;
    p.compression = Object.assign({}, p.compression, { ratio: +$('#compRatio').value });
    p.synthetic = +$('#synthetic').value;
    p.neural = Object.assign({}, p.neural, {
      mix: +$('#neuralMix').value, bit_depth: +$('#neuralBits').value,
      wet: +$('#neuralWet').value, decay: +$('#neuralDecay').value,
      am_depth: +$('#neuralAM').value,
    });
    p.glass = Object.assign({}, p.glass, {
      mix: +$('#glassMix').value, pitch_factor: +$('#glassPitch').value,
    });
    p.micro = Object.assign({}, p.micro, {
      mix: +$('#microMix').value, pitch_factor: +$('#microPitch').value,
    });
    p.stereo_width = +$('#stereoWidth').value;
    p.formant_preserve = +$('#formantPreserve').value;
    p.limiter_ceiling = +$('#limiter').value;
    p.output_gain_db = +$('#outGain').value;
    p.normalize_loudness = $('#normLoudness').checked;
    p.loudness_target_lufs = +$('#targetLufs').value;
    p.limiter_enabled = $('#limiterOn').checked;
  }

  // -- preview --------------------------------------------------------------
  async function preview(raw) {
    if (!current) return;
    const status = $('#previewStatus');
    status.textContent = raw ? 'Synthesizing raw base…' : 'Synthesizing preset…';
    try {
      let out;
      if (raw) {
        out = await api('/api/voice/preview', {
          raw: true, base_voice: current.base_voice,
          engine: current.engine, text: $('#previewText').value,
        });
      } else {
        pullControls();
        out = await api('/api/voice/preview', {
          preset: current, text: $('#previewText').value,
        });
      }
      if (out.url) {
        lastSegment = out.segment_id;
        NexusVoice.enqueue(out.url, { preview: true });
        status.textContent = `${out.seconds}s`;
      } else {
        status.textContent = out.error || 'preview failed';
      }
    } catch (e) { status.textContent = 'preview failed: ' + e.message; }
    loadStatus();
  }

  async function ab() {
    $('#previewStatus').textContent = 'A/B…';
    await preview(true);
    setTimeout(() => preview(false), 500);
  }

  // -- voice lab --------------------------------------------------------------
  const LAB_TAGS = ['laugh', 'chuckle', 'sigh', 'gasp', 'groan', 'sniff',
    'shush', 'clear throat', 'happy', 'sarcastic', 'whispering',
    'surprised', 'dramatic', 'narration', 'angry', 'fear'];

  async function loadLab(engineName) {
    const card = $('#voiceLabCard');
    if (!card) return;
    if (engineName !== 'chatterbox') { card.hidden = true; return; }
    const st = $('#voiceLabStatus');
    try {
      const caps = await api('/api/voice/capabilities');
      const cb = (caps.engines || {}).chatterbox || {};
      const supported = new Set(cb.supported_tags || []);
      card.hidden = false;
      if (supported.size) {
        $('#voiceLabTags').innerHTML = LAB_TAGS.filter(t => supported.has(t))
          .map(t => `<button class="mini-button lab-tag" data-tag="${esc(t)}" type="button">${esc(t)}</button>`).join('')
          || '<span class="muted">runtime reports no supported tags</span>';
      } else {
        $('#voiceLabTags').innerHTML = cb.available
          ? '<button class="mini-button" id="voiceLabProbe" type="button">Probe engine (loads model ~15s)</button>'
          : '<span class="muted">chatterbox not provisioned</span>';
      }
      st.textContent = cb.available
        ? (supported.size ? `${supported.size} tags confirmed` : 'engine cold — probe to load + verify tags')
        : 'chatterbox not provisioned';
    } catch (e) {
      card.hidden = false;
      st.textContent = 'capabilities probe failed: ' + e.message;
    }
  }

  async function auditionTag(tag) {
    const st = $('#voiceLabStatus');
    st.textContent = `rendering [${tag}]…`;
    try {
      pullControls();
      const out = await api('/api/voice/preview', {
        preset: current, text: `[${tag}]`,
      });
      if (out.url) {
        lastSegment = out.segment_id;
        NexusVoice.enqueue(out.url, { preview: true });
        st.textContent = `[${tag}] · ${out.seconds}s`;
      } else {
        st.textContent = out.error || 'audition failed';
      }
    } catch (e) { st.textContent = 'audition failed: ' + e.message; }
  }

  // -- preset CRUD ----------------------------------------------------------
  async function savePreset() {
    if (!current) return;
    pullControls();
    const r = await api('/api/voice/preset/save', { preset: current });
    if (r.error) { $('#previewStatus').textContent = r.error; return; }
    dirty = false;
    await loadPresets();
    selectPreset(r.preset.id);
  }

  async function saveAs() {
    if (!current) return;
    const name = prompt('Name for the new voice:', current.name + ' copy');
    if (!name) return;
    pullControls();
    current.name = name;
    delete current.id;
    delete current.official;
    const r = await api('/api/voice/preset/save', { preset: current });
    if (r.error) { $('#previewStatus').textContent = r.error; return; }
    dirty = false;
    await loadPresets();
    selectPreset(r.preset.id);
  }

  async function duplicate() {
    if (!current) return;
    const r = await api('/api/voice/preset/duplicate', { preset_id: current.id });
    if (r.error) { $('#previewStatus').textContent = r.error; return; }
    await loadPresets();
    selectPreset(r.preset.id);
  }

  async function rename() {
    if (!current) return;
    const name = prompt('Rename preset:', current.name);
    if (!name) return;
    const r = await api('/api/voice/preset/rename', { preset_id: current.id, name });
    if (r.error) { $('#previewStatus').textContent = r.error; return; }
    await loadPresets();
    selectPreset(current.id);
  }

  async function del() {
    if (!current) return;
    if (!confirm(`Delete "${current.name}"? Official presets cannot be deleted.`)) return;
    const r = await api('/api/voice/preset/delete', { preset_id: current.id });
    if (r.error) { $('#previewStatus').textContent = r.error; return; }
    current = null;
    await loadPresets();
    if (presets.length) selectPreset(presets[0].id);
  }

  async function exportPreset() {
    if (!current) return;
    pullControls();
    const r = await api(`/api/voice/preset/${current.id}/export`);
    if (!r.json) {
      // unsaved/dirty → export working copy directly
      r.json = JSON.stringify(current, null, 2);
    }
    const blob = new Blob([r.json], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${current.id || 'voice-preset'}.json`;
    a.click();
  }

  async function importPreset(file) {
    const text = await file.text();
    const r = await api('/api/voice/preset/import', { json: text });
    if (r.error) { $('#previewStatus').textContent = r.error; return; }
    await loadPresets();
    selectPreset(r.preset.id);
  }

  async function exportLast(fmt) {
    if (!lastSegment) { $('#exportStatus').textContent = 'preview first'; return; }
    const name = ($('#exportName').value || `nexus-voice-${lastSegment}`).replace(/[^\w\-]+/g, '-');
    const r = await api('/api/voice/export', { segment_id: lastSegment, format: fmt, name });
    $('#exportStatus').textContent = r.path ? `saved: ${r.path}` : (r.error || 'export failed');
  }

  function wire() {
    // Clicking a preset auditions its *saved* form — the working copy may
    // carry unsaved edits, so samples always resolve by preset_id.
    async function samplePreset(id) {
      const status = $('#previewStatus');
      status.textContent = 'Synthesizing sample…';
      try {
        const out = await api('/api/voice/preview', {
          preset_id: id, text: $('#previewText').value,
        });
        if (out.url) {
          lastSegment = out.segment_id;
          NexusVoice.enqueue(out.url, { preview: true });
          status.textContent = `${out.seconds}s`;
        } else {
          status.textContent = out.error || 'sample failed';
        }
      } catch (e) { status.textContent = 'sample failed: ' + e.message; }
      loadStatus();
    }
    $('#presetList').addEventListener('click', e => {
      const el = e.target.closest('.vp');
      if (el) { selectPreset(el.dataset.id); samplePreset(el.dataset.id); }
    });
    ['pitch', 'tempo', 'eqWarmth', 'eqPresence', 'eqAir', 'exciter', 'compRatio',
      'synthetic', 'neuralMix', 'glassMix', 'microMix', 'stereoWidth',
      'neuralBits', 'neuralWet', 'neuralDecay', 'neuralAM', 'glassPitch',
      'microPitch', 'formantPreserve', 'limiter', 'outGain', 'targetLufs'].forEach(id => {
      $('#' + id).addEventListener('input', e => {
        $('#' + id + 'Val').textContent = fmt(id, e.target.value);
        dirty = true;
        renderPresetList();
      });
    });
    $('#presetEngine').addEventListener('change', e => {
      if (!current) return;
      current.engine = e.target.value;
      dirty = true;
      loadVoices(current.engine).then(() => {
        const sel = $('#baseVoice');
        if (sel.options.length) {
          // engine voices differ — snap to that engine's first voice
          // unless the preset's voice exists there already.
          const found = [...sel.options].some(o => o.value === current.base_voice);
          sel.value = found ? current.base_voice : sel.options[0].value;
          current.base_voice = sel.value;
        }
      });
      loadLab(current.engine);
      renderPresetList();
    });
    $('#baseVoice').addEventListener('change', () => { dirty = true; });
    ['normLoudness', 'limiterOn'].forEach(id => {
      const el = $('#' + id);
      if (el) el.addEventListener('change', () => { dirty = true; renderPresetList(); });
    });
    const lab = $('#voiceLabTags');
    if (lab) lab.addEventListener('click', async e => {
      const b = e.target.closest('.lab-tag');
      if (b) { auditionTag(b.dataset.tag); return; }
      if (e.target.closest('#voiceLabProbe')) {
        const st = $('#voiceLabStatus');
        st.textContent = 'probing engine — loading model…';
        try {
          await api('/api/voice/capabilities?probe=1');
        } catch (err) {
          st.textContent = 'probe failed: ' + err.message;
        }
        if (current) loadLab(current.engine);
      }
    });
    $('#previewA').addEventListener('click', () => preview(true));
    $('#previewB').addEventListener('click', () => preview(false));
    $('#previewAB').addEventListener('click', ab);
    $('#stopPlayback').addEventListener('click', () => NexusVoice.stop());
    $('#savePreset').addEventListener('click', savePreset);
    $('#saveAsPreset').addEventListener('click', saveAs);
    $('#duplicatePreset').addEventListener('click', duplicate);
    $('#renamePreset').addEventListener('click', rename);
    $('#deletePreset').addEventListener('click', del);
    $('#exportPreset').addEventListener('click', exportPreset);
    $('#resetPreset').addEventListener('click', () => current && selectPreset(current.id));
    $('#useAsDefault').addEventListener('click', async () => {
      if (!current) return;
      if (dirty) await savePreset();
      await api('/api/voice/config', { voice_preset_id: current.id });
      $('#presetBadge').textContent = current.name + ' · default';
    });
    $('#importPreset').addEventListener('click', () => $('#presetFile').click());
    $('#presetFile').addEventListener('change', e => {
      if (e.target.files[0]) importPreset(e.target.files[0]);
      e.target.value = '';
    });
    $('#installAssets').addEventListener('click', async () => {
      $('#engineStatus').textContent = 'Downloading Kokoro assets…';
      await api('/api/voice/assets/install', {});
    });
    $('#pickCloneFile').addEventListener('click', () => $('#cloneFile').click());
    $('#cloneFile').addEventListener('change', e => {
      const f = e.target.files[0];
      $('#cloneStatus').textContent = f ? f.name : '';
      if (f && !$('#cloneName').value)
        $('#cloneName').value = f.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
    });
    $('#importVoice').addEventListener('click', async () => {
      const st = $('#cloneStatus');
      const f = $('#cloneFile').files[0];
      const name = $('#cloneName').value.trim();
      if (!f) { st.textContent = 'pick an audio clip first'; return; }
      const id = (name || f.name.replace(/\.[^.]+$/, ''))
        .toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '');
      if (!id) { st.textContent = 'need a usable voice name'; return; }
      st.textContent = 'validating + importing…';
      try {
        const b64 = await new Promise((res, rej) => {
          const r = new FileReader();
          r.onload = () => res(String(r.result).split(',')[1] || '');
          r.onerror = () => rej(new Error('could not read file'));
          r.readAsDataURL(f);
        });
        const out = await api('/api/voice/voice/import', {
          voice_id: id, name, filename: f.name, audio_b64: b64,
          engine: 'chatterbox',
        });
        if (!out.ok) {
          st.textContent = 'import failed: ' + (out.error || 'unknown error');
          return;
        }
        const warns = (out.warnings || []).join('; ');
        st.textContent = 'imported "' + (out.voice?.name || id) + '"' +
          (warns ? ' — ' + warns : '');
        $('#cloneFile').value = '';
        loadVoices('chatterbox');
      } catch (err) {
        st.textContent = 'import failed: ' + err.message;
      }
    });
    $('#autoRead').addEventListener('change', e => {
      api('/api/voice/config', { voice_mode: e.target.checked ? 'responses' : 'manual' });
    });
    $('#voiceEnabled').addEventListener('change', e => {
      api('/api/voice/config', { voice_enabled: e.target.checked });
    });
    $('#exportWav').addEventListener('click', () => exportLast('wav'));
    $('#exportMp3').addEventListener('click', () => exportLast('mp3'));

    NexusVoice.on((evt) => {
      if (evt && evt.event === 'assets_ready') loadStatus();
      if (evt && evt.event === 'asset_progress') {
        $('#engineStatus').textContent =
          `Downloading ${evt.file} — ${(evt.bytes / 1048576).toFixed(1)} MB`;
      }
      if (evt && evt.event === 'asset_error') {
        $('#engineStatus').textContent = 'Asset install failed: ' + (evt.error || '');
      }
    });
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
