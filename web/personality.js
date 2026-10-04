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
  let voicePresets = [], voiceSel = "";

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
      </section>

      <section class="pst-panel">
        <h3>Preview</h3>
        <button id="previewBtn" class="mini-button" type="button">Preview Personality</button>
        <button id="previewVoiceBtn" class="mini-button" type="button">Preview Voice</button>
        <div id="previewOut" class="preview-box" hidden></div>
        <div id="previewVoice" class="preview-voice"></div>
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
        text: "Hi, I'm Nexus. This is how I'll sound and respond "
            + "with your current personality settings.",
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

  function bind() {
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
