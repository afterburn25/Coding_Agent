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

  async function loadVoices() {
    const d = await api('/api/voice/voices');
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
    el.innerHTML =
      `<div><b>${esc(eng.name || 'kokoro')}</b> ${esc(eng.version || '')}</div>` +
      `<div>${eng.loaded ? '✓ model loaded' : '○ model cold'} · ${eng.load_time_s || 0}s load</div>` +
      (missing.length ? `<div class="voice-warn">Missing assets: ${esc(missing.join(', '))} — press Setup engine</div>`
                      : '<div>✓ assets verified</div>') +
      (eng.rtf ? `<div>RTF ${eng.rtf} · ${eng.synth_audio_s}s audio in ${eng.synth_cpu_s}s</div>` : '');
    const m = $('#voiceMetrics');
    m.innerHTML =
      `Engine: ${esc(eng.name || '-')} ${esc(eng.version || '')}<br>` +
      `Loaded: ${eng.loaded ? 'yes' : 'no'} · load ${eng.load_time_s || 0}s<br>` +
      `Synthesis calls: ${eng.synth_calls || 0} · RTF ${eng.rtf ?? '—'}<br>` +
      `Cache: ${(s.cache && (s.cache.bytes / 1048576).toFixed(1)) || 0} MB / ${s.cache ? s.cache.entries : 0} files<br>` +
      `Queue: ${s.queue || 0} · Muted: ${s.muted ? 'yes' : 'no'} · Mode: ${esc(s.mode || '')}<br>` +
      `Upstream: ${esc(((eng.upstream || {}).model_repo) || '')} (${esc(((eng.upstream || {}).license) || '')})`;
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
    current = JSON.parse(JSON.stringify(p));
    dirty = false;
    $('#presetBadge').textContent = p.name + (p.official ? ' · official' : '');
    syncControls();
    renderPresetList();
  }

  // -- controls <-> preset ------------------------------------------------
  function syncControls() {
    const p = current;
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
    return String(v);
  }

  function pullControls() {
    const p = current;
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
      'microPitch', 'formantPreserve', 'limiter', 'outGain'].forEach(id => {
      $('#' + id).addEventListener('input', e => {
        $('#' + id + 'Val').textContent = fmt(id, e.target.value);
        dirty = true;
        renderPresetList();
      });
    });
    $('#baseVoice').addEventListener('change', () => { dirty = true; });
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
