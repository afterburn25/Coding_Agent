// Personality Studio — presets, 47 sliders, strength, mood, voice,
// customs. Adult sections render only when the profile is 18+; the
// backend independently strips adult content either way.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = (p) => fetch(p).then((r) => (r.ok ? r.json()
    : r.json().then((e) => Promise.reject(new Error(e.error || r.status)))));
  const act = (pid, b) => fetch(`/api/profiles/${encodeURIComponent(pid)}/personality`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(b),
  }).then((r) => r.json().then((d) => (r.ok ? d
    : Promise.reject(new Error(d.error || r.status)))));

  const CAT_ORDER = ["core_social", "fun", "intellect",
    "energy_emotion", "response_style", "adult"];

  let pid = "", data = null;
  let work = { traits: {}, voice: {} };  // unsaved edits
  let dirty = false;
  let voicePresets = [], voiceSel = "", vocalStyles = [];

  async function load() {
    const a = await api("/api/profiles/active");
    if (!a.profile) { location.replace("/start.html"); return; }
    pid = a.profile.profile_id;
    data = await api(`/api/profiles/${encodeURIComponent(pid)}/personality`);
    try {
      voicePresets = (await api("/api/voice/presets")).presets || [];
      voiceSel = ((await api(
        `/api/profiles/${encodeURIComponent(pid)}/voice`)).voice || {})
        .preset_id || "";
    } catch { voicePresets = []; voiceSel = ""; }
    try {
      vocalStyles = (await api("/api/voice/vocalizations")).styles || [];
    } catch { vocalStyles = []; }
    const t = data.active || {};
    work = { traits: { ...(t.traits || {}) },
             voice: { ...(t.voice || {}) } };
    dirty = false;
    render();
  }

  const trait = (k) => work.traits[k] ?? 50;
  const voice = (k) => work.voice[k] ?? 50;

  let applySeq = 0;
  function markDirty() {
    dirty = true;
    const tag = $("dirtyTag");
    if (tag) tag.hidden = false;
    const aw = $("applyWork");
    if (aw && String(data.active.personality_id || "")
        .startsWith("custom:")) aw.disabled = false;
  }

  // Sliders live-apply: editing a preset auto-forks it into a custom so the
  // change actually takes effect; an active custom is patched in place.
  async function applyLive() {
    const seq = ++applySeq;
    try {
      let cid = String(data.active.personality_id || "");
      if (!cid.startsWith("custom:")) {
        const r = await act(pid, { action: "create_custom",
          name: `Custom based on ${data.active.name || "Preset"}`,
          base_preset: String(data.active.base_preset || ""),
          traits: work.traits, voice: work.voice });
        cid = `custom:${r.custom.personality_id}`;
        await act(pid, { action: "set_active", target: cid });
        dirty = false;
        await load();
        return;
      }
      await act(pid, { action: "patch_custom",
                       personality_id: cid.split(":")[1],
                       traits: work.traits, voice: work.voice });
      if (seq === applySeq) {
        dirty = false;
        const tag = $("dirtyTag");
        if (tag) tag.hidden = true;
      }
    } catch { /* keep dirty so the user can retry via the buttons */ }
  }
  let applyTimer = null;
  function scheduleApply() {
    clearTimeout(applyTimer);
    applyTimer = setTimeout(applyLive, 400);
  }

  function sliderRow(key, label, val, isVoice) {
    return `<div class="slider-row">
      <span>${esc(label)}</span>
      <input type="range" min="0" max="100" value="${val}"
             data-${isVoice ? "vkey" : "tkey"}="${esc(key)}" />
      <span class="s-val">${val}</span>
      <button class="s-reset" type="button" title="Reset to 50"
              data-${isVoice ? "vreset" : "treset"}="${esc(key)}">↺</button>
    </div>`;
  }

  function sliderSection(catId, title, open) {
    const rows = Object.entries(data.sliders)
      .filter(([, v]) => v.category === catId)
      .filter(([, v]) => !v.adult_only || data.is_adult)
      .map(([k, v]) => sliderRow(k, v.label, trait(k), false));
    if (!rows.length) return "";
    return `<details class="pst-sec" ${open ? "open" : ""}>
      <summary>${esc(title)}</summary>
      <div class="sec-body">${rows.join("")}</div>
    </details>`;
  }

  function voiceSection() {
    const rows = Object.entries(data.voice_controls)
      .map(([k, v]) => sliderRow(k, v.label, voice(k), true));
    const picker = `<label class="pst-vsel">Base voice preset
      <select id="voicePreset">
        <option value="">System default</option>
        ${voicePresets.map((p) =>
          `<option value="${esc(p.id)}" ${p.id === voiceSel ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
      </select></label>`;
    return `<details class="pst-sec">
      <summary>Voice Performance</summary>
      <div class="sec-body">${picker}${rows.join("")}
        <small class="hint">Unsupported acoustics become prosody hints — never faked.</small>
      </div>
    </details>`;
  }

  function vocalSection() {
    const levels = data.vocal_levels || ["off", "minimal", "natural", "expressive"];
    const cur = data.vocalizations || "natural";
    // A few representative samples — the catalog returns the full set.
    const picks = ["mmm_pleased", "hmm", "mm_hmm", "sigh_relieved",
                   "chuckle", "gasp", "aww"];
    const samples = vocalStyles.filter((s) => picks.includes(s.style) && s.preview);
    return `<details class="pst-sec">
      <summary>Natural Vocalizations</summary>
      <div class="sec-body">
        <label class="pst-vsel">Vocalization level
          <select id="vocalLevel">
            ${levels.map((l) => `<option value="${esc(l)}" ${l === cur ? "selected" : ""}>${esc(l[0].toUpperCase() + l.slice(1))}</option>`).join("")}
          </select></label>
        <div class="mood-row" id="vocalSamples">
          ${samples.map((s) => `<button class="mood-chip" type="button"
            data-vsample="${esc(s.style)}" data-vtext="${esc(s.preview)}">${esc(s.style.replace(/_/g, " "))}</button>`).join("")}
        </div>
        <small class="hint">Hums, sighs, chuckles and reactions — resolved to natural sounds, never spelled out. Preview uses the voice controls above.</small>
      </div>
    </details>`;
  }

  function presetCards() {
    const cats = {};
    for (const p of data.presets) (cats[p.category] ??= []).push(p);
    const order = ["general", "tech", "humor", "creative", "guidance",
      "efficiency", "adult"];
    const names = { general: "General", tech: "Tech & Intellectual",
      humor: "Humor & Fun", creative: "Creative & Social",
      guidance: "Guidance", efficiency: "Efficiency",
      adult: "Adult-Only (18+)" };
    return order.filter((c) => cats[c]).map((c) => `
      <div class="pst-cat-row">${esc(names[c] || c)}</div>
      <div class="pst-grid">${cats[c].map((p) => `
        <div class="pst-card ${p.adult_only ? "adult" : ""} ${data.active.personality_id === `preset:${p.id}` ? "active" : ""}"
             data-preset="${esc(p.id)}" role="button" tabindex="0">
          <div class="p-name">${esc(p.name)}</div>
          <div class="p-cat">${esc(p.greeting_style || "")}</div>
        </div>`).join("")}
      </div>`).join("");
  }

  function render() {
    const t = data.active || {};
    $("studioBody").innerHTML = `
      <section class="pst-panel">
        <h3>Preset — active: ${esc(t.name || "")} <em id="dirtyTag" style="color:var(--warn)" ${dirty ? "" : "hidden"}>(unsaved edits)</em></h3>
        ${presetCards()}
      </section>

      <section class="pst-panel">
        <h3>Personality Strength</h3>
        <div class="strength-wrap">
          <input id="strength" type="range" min="0" max="100" value="${data.strength}" />
          <span class="strength-val" id="strengthVal">${data.strength}</span>
        </div>
        <div class="strength-labels"><span>Subtle</span><span>Strong</span></div>
      </section>

      <section class="pst-panel">
        <h3>Mood (temporary)</h3>
        <div class="mood-row">
          <button class="mood-chip ${!data.mood ? "active" : ""}" data-mood="">None</button>
          ${(data.moods || []).map((mo) => `
            <button class="mood-chip ${data.mood === mo ? "active" : ""}"
                    data-mood="${esc(mo)}">${esc(mo)}</button>`).join("")}
        </div>
      </section>

      <section class="pst-panel">
        <h3>Sliders</h3>
        ${sliderSection("core_social", "Social", true)}
        ${sliderSection("fun", "Humor")}
        ${sliderSection("intellect", "Intelligence")}
        ${sliderSection("energy_emotion", "Energy & Emotion")}
        ${sliderSection("response_style", "Response Style")}
        ${data.is_adult ? sliderSection("adult", "Adult (18+)") : ""}
        ${voiceSection()}
        ${vocalSection()}
      </section>

      <section class="pst-panel">
        <h3>Preview</h3>
        <button id="previewBtn" class="mini-button" type="button">Preview Personality</button>
        <button id="previewVoiceBtn" class="mini-button" type="button">Preview Voice</button>
        <div id="previewOut" class="preview-box" hidden></div>
        <div id="previewVoice" class="preview-voice"></div>
      </section>

      <section class="pst-panel">
        <h3>Speech Lab</h3>
        <p class="hint">How this persona actually talks — the same facts
        rendered through its speech genome. Facts never change; only the
        wrapper does.</p>
        <div id="genomeSummary" class="genome-summary"></div>
        <div class="lab-controls">
          <label>Persona
            <select id="labPersona">
              <option value="active" selected>Active (${esc(t.name || "")})</option>
              ${(data.presets || []).map((p) =>
                `<option value="preset:${esc(p.id)}">${esc(p.name)}</option>`).join("")}
              ${(data.customs || []).map((c) =>
                `<option value="custom:${esc(c.personality_id)}">${esc(c.name)}</option>`).join("")}
            </select></label>
          <label>Register
            <select id="labRegister">
              ${["casual", "technical", "coding", "debugging",
                 "creative", "personal_conversation"].map((r) =>
                `<option value="${r}">${r.replace(/_/g, " ")}</option>`).join("")}
            </select></label>
          <label>Situation
            <select id="labSeriousness">
              <option value="0">Casual</option>
              <option value="1">Focused</option>
              <option value="2">Serious</option>
              <option value="3">Critical</option>
            </select></label>
          <label>Renders/act
            <select id="labTurns">
              <option value="1">1</option>
              <option value="2" selected>2</option>
              <option value="3">3</option>
            </select></label>
          <button id="labRun" class="mini-button" type="button">Render battery</button>
        </div>
        <div id="labOut" class="speech-lab"></div>
      </section>

      <section class="pst-panel">
        <h3>Custom Personalities</h3>
        <div class="custom-list" id="customList">
          ${(data.customs || []).map((c) => `
            <div class="custom-item ${data.active.personality_id === `custom:${c.personality_id}` ? "active" : ""}">
              <span class="ci-name">${esc(c.name)}</span>
              <button class="mini-button" data-cuse="${esc(c.personality_id)}" type="button">Use</button>
              <button class="mini-button" data-crename="${esc(c.personality_id)}" type="button">Rename</button>
              <button class="mini-button" data-cdel="${esc(c.personality_id)}" type="button">Delete</button>
            </div>`).join("") || '<small class="hint">No customs yet — edit sliders or duplicate a preset.</small>'}
        </div>
        <div class="pst-actions" style="margin-top:12px">
          <button id="saveCustom" class="mini-button" type="button">Save Custom Personality</button>
          <button id="applyWork" class="mini-button" type="button" ${dirty && String(data.active.personality_id || "").startsWith("custom:") ? "" : "disabled"}>Apply to Active Custom</button>
          <button id="resetAll" class="mini-button" type="button">Reset to Default Nexus</button>
        </div>
      </section>`;
    bind();
  }

  let sampleAudio = null;
  async function playVoiceSample() {
    const r = await act(pid, { action: "preview",
                               traits: work.traits, voice: work.voice });
    const v = r.voice || {};
    const res = await fetch("/api/voice/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: String(r.preview
            || "Hi, I'm Nexus. This is how I'll sound with this personality."
            ).slice(0, 500),
        // Rate rides the engine `speed` arg — tempo stays 1.0 so it
        // isn't applied twice through the DSP chain.
        overlay: { pitch_semitones: v.pitch_semitones, tempo: 1.0,
                   output_gain_db: v.output_gain_db },
        speed: v.speed }),
    }).then((x) => x.json().then((d) => (x.ok ? d
      : Promise.reject(new Error(d.error || x.status)))));
    if (sampleAudio) { try { sampleAudio.pause(); } catch {} }
    sampleAudio = new Audio(res.url);
    sampleAudio.play().catch(() => {});
    return v;
  }

  const pct = (v) => `${Math.round((+v || 0) * 100)}%`;

  function fillGenomeSummary() {
    const el = $("genomeSummary");
    if (!el) return;
    const s = (data && data.speech_genome_summary) || {};
    const chips = [];
    const push = (label, v) => {
      if (v !== undefined && v !== null && v !== "") chips.push(
        `<span class="gchip">${esc(label)} ${esc(String(v))}</span>`);
    };
    push("sent-len", pct(s.sentence_length));
    push("fragments", pct(s.fragment_rate));
    push("tempo", pct(s.tempo));
    push("disagree", pct(s.disagreement_directness));
    push("addr-rate", pct(s.address_frequency));
    push("questions", pct(s.question_frequency));
    if (s.repair_style) push("repair", s.repair_style.replace(/_/g, " "));
    for (const h of (s.humor_categories || []))
      chips.push(`<span class="gchip humor">${esc(h.replace(/_/g, " "))}</span>`);
    for (const w of (s.signature_words || []))
      chips.push(`<span class="gchip sig">“${esc(w)}”</span>`);
    el.innerHTML = chips.join("") ||
      `<span class="hint">Genome summary unavailable.</span>`;
  }

  const ACT_LABELS = {
    greet: "Greeting", answer: "Answer",
    report_success: "Success report", report_failure: "Failure report",
    disagree: "Disagreement", warn: "Warning",
    admit_uncertainty: "Uncertainty", farewell: "Farewell",
  };

  // Per-render audition cache — each lab card stashes its text + delivery
  // plan so the play button can audition THAT line with THAT plan.
  const labAudition = [];

  async function auditionLabLine(idx) {
    const item = labAudition[idx];
    if (!item) return;
    const r = await act(pid, { action: "preview",
                               traits: work.traits, voice: work.voice })
        .catch(() => null);
    const v = (r && r.voice) || {};
    const plan = item.plan || {};
    const res = await fetch("/api/voice/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: String(item.text || "").slice(0, 500),
        overlay: { pitch_semitones: v.pitch_semitones, tempo: 1.0,
                   output_gain_db: v.output_gain_db },
        // Profile rate × genome pace — same product the queue computes.
        speed: Math.min(2.0, Math.max(0.5,
                (v.speed || 1) * (plan.pace || 1))),
        delivery: plan }),
    }).then((x) => x.json()).catch(() => null);
    if (res && res.url) {
      if (sampleAudio) { try { sampleAudio.pause(); } catch {} }
      sampleAudio = new Audio(res.url);
      sampleAudio.play().catch(() => {});
    }
  }

  function renderLab(d) {
    const out = $("labOut");
    if (!out) return;
    if (!d || !d.ok) {
      out.innerHTML = `<div class="hint">Preview failed: ${esc(
        (d && d.error) || "unknown")}</div>`;
      return;
    }
    labAudition.length = 0;
    const cards = Object.entries(d.renders || {}).map(([act, rows]) => {
      const first = rows[0] || {};
      const plan = first.plan || {};
      const pIdx = labAudition.push({ text: first.text || "", plan }) - 1;
      const planBits = [
        `pace ${plan.pace}`, `energy ${Math.round((plan.energy ?? 0) * 100)}%`,
        `warmth ${Math.round((plan.warmth ?? 0) * 100)}%`,
        plan.seriousness ? `serious ${plan.seriousness}` : "",
        first.opening_family ? `open:${first.opening_family}` : "",
        first.closing_family && first.closing_family !== "hard_stop"
          ? `close:${first.closing_family}` : "",
        first.micro_reaction ? `micro:"${esc(first.micro_reaction)}"` : "",
        first.used_address ? "addr" : "",
        plan.sarcasm ? "sarcastic" : "",
      ].filter(Boolean).join(" · ");
      return `<div class="lab-card">
        <div class="lab-act">${esc(ACT_LABELS[act] || act)}
          <button type="button" class="lab-play" data-lplay="${pIdx}"
                  title="Hear this line with its delivery plan">&#9654;</button></div>
        ${rows.map((r, i) => `<div class="lab-line">${i === 0 ? "" : `<em>↻${i} </em>`}${esc(r.text)}</div>`).join("")}
        <div class="lab-plan">${planBits}</div>
      </div>`;
    }).join("");
    out.innerHTML = cards;
    out.querySelectorAll("[data-lplay]").forEach((b) =>
      b.addEventListener("click", () => auditionLabLine(+b.dataset.lplay)));
  }

  function bind() {
    fillGenomeSummary();
    $("labRun")?.addEventListener("click", async () => {
      const out = $("labOut");
      out.innerHTML = `<div class="hint">Rendering…</div>`;
      try {
        const q = new URLSearchParams({
          target: $("labPersona").value,
          register: $("labRegister").value,
          seriousness: $("labSeriousness").value,
          turns: $("labTurns").value });
        const d = await api(
          `/api/profiles/${encodeURIComponent(pid)}/personality/speech-preview?${q}`);
        renderLab(d);
      } catch (e) {
        out.innerHTML = `<div class="hint">Preview failed: ${esc(e.message)}</div>`;
      }
    });
    // Lab renders auto-run once so the surface isn't dead on open.
    setTimeout(() => $("labRun")?.click(), 60);

    $("studioBody").querySelectorAll("[data-preset]").forEach((el) =>
      el.addEventListener("click", async () => {
        try {
          await act(pid, { action: "set_active",
                           target: `preset:${el.dataset.preset}` });
          await load();
          playVoiceSample().catch(() => {});
        } catch (e) { alert(e.message); }
      }));

    $("strength").addEventListener("input", () => {
      $("strengthVal").textContent = $("strength").value;
    });
    $("strength").addEventListener("change", async () => {
      await act(pid, { action: "set_strength",
                       strength: +$("strength").value }).catch(() => {});
    });

    $("studioBody").querySelectorAll("[data-mood]").forEach((b) =>
      b.addEventListener("click", async () => {
        await act(pid, { action: "set_mood", mood: b.dataset.mood })
          .catch(() => {});
        data.mood = b.dataset.mood;
        render();
      }));

    const vocalSel = $("vocalLevel");
    if (vocalSel) vocalSel.addEventListener("change", async () => {
      try {
        const r = await act(pid, { action: "set_vocalizations",
                                   level: vocalSel.value });
        data.vocalizations = r.vocalizations || vocalSel.value;
      } catch (e) { alert(e.message); }
    });

    // Sample buttons preview the resolved vocalization with the current
    // voice-performance overlay so tuning hears the real delivery.
    const playVocal = async (text) => {
      const r = await act(pid, { action: "preview",
                                 traits: work.traits, voice: work.voice })
        .catch(() => null);
      const v = (r && r.voice) || {};
      const res = await fetch("/api/voice/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: String(text || "Mmm…").slice(0, 500),
          overlay: { pitch_semitones: v.pitch_semitones, tempo: 1.0,
                     output_gain_db: v.output_gain_db },
          speed: v.speed }),
      }).then((x) => x.json()).catch(() => null);
      if (res && res.url) {
        if (sampleAudio) { try { sampleAudio.pause(); } catch {} }
        sampleAudio = new Audio(res.url);
        sampleAudio.play().catch(() => {});
      }
    };
    $("studioBody").querySelectorAll("[data-vsample]").forEach((b) =>
      b.addEventListener("click", () => playVocal(b.dataset.vtext)));

    $("studioBody").querySelectorAll("[data-tkey]").forEach((sl) => {
      sl.addEventListener("input", () => {
        work.traits[sl.dataset.tkey] = +sl.value;
        sl.parentElement.querySelector(".s-val").textContent = sl.value;
        markDirty();
      });
      sl.addEventListener("change", scheduleApply);
    });
    $("studioBody").querySelectorAll("[data-vkey]").forEach((sl) => {
      sl.addEventListener("input", () => {
        work.voice[sl.dataset.vkey] = +sl.value;
        sl.parentElement.querySelector(".s-val").textContent = sl.value;
        markDirty();
      });
      sl.addEventListener("change", scheduleApply);
    });
    $("studioBody").querySelectorAll("[data-treset]").forEach((b) =>
      b.addEventListener("click", () => {
        delete work.traits[b.dataset.treset]; render(); markDirty(); scheduleApply();
      }));
    $("studioBody").querySelectorAll("[data-vreset]").forEach((b) =>
      b.addEventListener("click", () => {
        delete work.voice[b.dataset.vreset]; render(); markDirty(); scheduleApply();
      }));
    $("voicePreset")?.addEventListener("change", async (e) => {
      try {
        await fetch(`/api/profiles/${encodeURIComponent(pid)}/voice`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ preset_id: e.target.value }) });
        voiceSel = e.target.value;
      } catch (e2) { alert(e2.message); }
    });

    $("previewBtn").addEventListener("click", async () => {
      const out = $("previewOut"), vo = $("previewVoice");
      try {
        const r = await act(pid, {
          action: "preview",
          traits: work.traits, voice: work.voice });
        out.textContent = r.preview || "";
        out.hidden = false;
        const v = r.voice || {};
        vo.textContent = `Voice: speed ${v.speed} · pitch ${v.pitch_semitones} semitones · gain ${v.output_gain_db} dB` +
          ((v.preprocess || []).length ? ` · hints: ${v.preprocess.join("; ")}` : "");
      } catch (e) { out.textContent = e.message; out.hidden = false; }
    });

    $("previewVoiceBtn").addEventListener("click", async () => {
      const vo = $("previewVoice");
      try {
        vo.textContent = "Synthesizing…";
        const v = await playVoiceSample();
        vo.textContent = `Playing · speed ${v.speed} · pitch `
          + `${v.pitch_semitones}st · gain ${v.output_gain_db}dB`
          + ((v.preprocess || []).length
             ? ` · hints: ${v.preprocess.join("; ")}` : "");
      } catch (e) { vo.textContent = e.message; }
    });

    $("saveCustom")?.addEventListener("click", async () => {
      const base = String(data.active.name || "");
      const def = String(data.active.personality_id || "").startsWith("preset:")
        ? `Custom based on ${base}` : `${base} copy`;
      const name = prompt("Name for this personality:", def);
      if (name == null) return;
      try {
        const r = await act(pid, {
          action: "create_custom", name,
          base_preset: String(data.active.base_preset || ""),
          traits: work.traits, voice: work.voice });
        await act(pid, { action: "set_active",
                         target: `custom:${r.custom.personality_id}` });
        await load();
      } catch (e) { alert(e.message); }
    });

    $("applyWork")?.addEventListener("click", async () => {
      const cid = String(data.active.personality_id || "").split(":")[1];
      try {
        await act(pid, { action: "patch_custom", personality_id: cid,
                         traits: work.traits, voice: work.voice });
        await load();
      } catch (e) { alert(e.message); }
    });

    $("resetAll").addEventListener("click", async () => {
      try { await act(pid, { action: "reset" }); await load(); }
      catch (e) { alert(e.message); }
    });

    $("studioBody").querySelectorAll("[data-cuse]").forEach((b) =>
      b.addEventListener("click", async () => {
        await act(pid, { action: "set_active",
                         target: `custom:${b.dataset.cuse}` })
          .catch((e) => alert(e.message));
        await load();
      }));
    $("studioBody").querySelectorAll("[data-crename]").forEach((b) =>
      b.addEventListener("click", async () => {
        const name = prompt("New name:");
        if (!name) return;
        await act(pid, { action: "patch_custom",
                         personality_id: b.dataset.crename, name })
          .catch((e) => alert(e.message));
        await load();
      }));
    $("studioBody").querySelectorAll("[data-cdel]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Delete this custom personality?")) return;
        await act(pid, { action: "delete_custom",
                         personality_id: b.dataset.cdel })
          .catch((e) => alert(e.message));
        await load();
      }));
  }

  $("studioBody").classList.remove("muted");
  load().catch((e) => {
    $("studioBody").innerHTML =
      `<div class="panel">Failed to load: ${esc(e.message)}</div>`;
    $("studioBody").classList.remove("muted");
  });
})();
