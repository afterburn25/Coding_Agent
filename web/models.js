(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = async (path, options) => {
    const res = await fetch(path, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
    return data;
  };
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const fmtGB = (n) => `${Number(n || 0).toFixed(1)} GB`;

  function renderHardware(hw) {
    if (!hw) { $("hwBox").textContent = "No hardware data."; return; }
    const gpus = (hw.gpus || []).map((g) => `<div class="muted small">${esc(g.name)} — ${fmtGB(g.free_vram_mb / 1024)} free / ${fmtGB(g.total_vram_mb / 1024)}</div>`).join("");
    $("hwBox").innerHTML = `
      ${gpus || '<div class="muted small">No GPU detected</div>'}
      <div class="muted small">RAM ${fmtGB(hw.available_ram_gb)} free / ${fmtGB(hw.total_ram_gb)}</div>
      <div class="muted small">${esc(hw.platform || "")}</div>`;
  }

  function renderRuntimes(runtime) {
    const rows = runtime.runtimes || [];
    const max = runtime.max_resident_models;
    if (!rows.length) { $("runtimeList").textContent = "No managed runtimes."; return; }
    $("runtimeList").innerHTML = `
      <div class="muted small" style="margin-bottom:6px">Resident-model limit: ${max}</div>` +
      rows.map((r) => `
      <div class="proc-row">
        <span class="name">${esc(r.model_id || r.id)}</span>
        <span>${esc(r.endpoint || "—")}</span>
        <span class="state ${esc(r.state)}">${esc(r.state)}</span>
        <span>${r.pid ? `pid ${r.pid}` : "—"}</span>
        <span class="muted">${r.error ? esc(String(r.error).slice(0, 50)) : ""}</span>
        <span class="row-actions">
          ${r.state !== "running" ? `<button data-model="${esc(r.model_id || r.id)}" data-action="start">Start</button>` : ""}
          ${r.state === "running" ? `<button data-model="${esc(r.model_id || r.id)}" data-action="stop">Stop</button>` : ""}
        </span>
      </div>`).join("");
    $("runtimeList").classList.remove("muted");
    $("runtimeList").querySelectorAll("[data-model]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await post(`/api/runtime/${btn.dataset.action}`, { model_id: btn.dataset.model });
        } catch (e) {
          alert(e.message);
        } finally {
          btn.disabled = false;
          setTimeout(loadRuntime, 1200);
        }
      });
    });
  }

  function renderProfiles(models) {
    if (!models.length) { $("modelList").textContent = "No model profiles configured."; return; }
    $("modelList").innerHTML = models.map((m) => {
      const chips = [
        ...m.roles.map((r) => `<span class="chip cap">${esc(r)}</span>`),
        m.enabled ? "" : '<span class="chip off">disabled</span>',
        m.vision ? '<span class="chip">vision</span>' : "",
        m.keep_loaded ? '<span class="chip">keep-loaded</span>' : "",
      ].join("");
      return `
      <div class="tool-card${m.enabled ? "" : " disabled"}">
        <div>
          <div class="tool-name">${esc(m.id)} <span class="tool-id">${esc(m.runtime)} · ${esc(m.model || m.model_path || "")}</span></div>
          <div class="tool-desc">${esc(m.notes || m.endpoint || "")}</div>
          <div class="tool-meta">
            <span class="chip">ctx ${m.context_window}</span>
            ${m.estimated_vram_gb ? `<span class="chip gpu">~${m.estimated_vram_gb} GB VRAM</span>` : ""}
            ${m.estimated_ram_gb ? `<span class="chip">~${m.estimated_ram_gb} GB RAM</span>` : ""}
            <span class="chip">gpu_layers ${esc(m.gpu_layers)}</span>
            ${chips}
          </div>
        </div>
      </div>`;
    }).join("");
    $("modelList").classList.remove("muted");
  }

  function renderFiles(inventory, storage) {
    const rows = inventory || [];
    if (!rows.length) { $("fileList").textContent = "No GGUF files found."; return; }
    const free = storage && storage.available ? `<div class="muted small" style="margin-bottom:6px">${fmtGB(storage.free_bytes / (1024 ** 3))} free in ${esc(storage.path)}</div>` : "";
    $("fileList").innerHTML = free + rows.map((f) => `<div class="muted small">${esc(f.name)} — ${fmtGB(f.size_gb)}</div>`).join("");
  }

  function renderCatalog(catalog) {
    const rows = catalog || [];
    if (!rows.length) { $("catalogList").textContent = "Catalog unavailable."; return; }
    $("catalogList").innerHTML = rows.map((c) => `
      <div class="tool-card">
        <div>
          <div class="tool-name">${esc(c.name || c.id)} <span class="tool-id">${esc(c.quantization || "")} · ${esc(c.size_gb ? c.size_gb + " GB" : "")}</span></div>
          <div class="tool-desc">${esc(c.description || c.hardware_note || "")}</div>
          <div class="tool-meta">${(c.roles || []).map((r) => `<span class="chip cap">${esc(r)}</span>`).join("")}</div>
        </div>
        <div class="tool-actions">
          ${c.installable !== false ? `<button class="mini-button" data-install="${esc(c.id)}">Install</button>` : ""}
        </div>
      </div>`).join("");
    $("catalogList").classList.remove("muted");
    $("catalogList").querySelectorAll("[data-install]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm(`Download and install ${btn.dataset.install}?`)) return;
        btn.disabled = true;
        try {
          await post("/api/models/install", { catalog_id: btn.dataset.install });
        } catch (e) {
          if (/already exists|repair/i.test(e.message) && confirm("Existing file detected — repair it?")) {
            try { await post("/api/models/install", { catalog_id: btn.dataset.install, repair: true }); }
            catch (e2) { alert(e2.message); }
          } else {
            alert(e.message);
          }
        } finally {
          btn.disabled = false;
          setTimeout(loadAll, 1000);
        }
      });
    });
  }

  function renderTierPlan(plan) {
    const box = $("tierPlanList");
    if (!plan || !Array.isArray(plan.tiers)) {
      box.textContent = "Tier plan unavailable.";
      return;
    }
    const hw = plan.hardware || {};
    const head = `<div class="muted small" style="margin-bottom:6px">${esc(hw.gpu_name || "No GPU")} · ` +
      `${fmtGB(hw.dedicated_vram_gb)} dedicated VRAM · ${fmtGB(hw.total_ram_gb)} RAM · ` +
      `${esc(hw.gpu_backend)} backend</div>`;
    const rows = plan.tiers.map((t) => {
      const cls = t.supported ? "cap" : "off";
      const sel = t.selected ? '<span class="chip gpu">install</span>' : "";
      const near = t.near_native ? '<span class="chip">below recommended</span>' : "";
      return `
      <div class="proc-row">
        <span class="name">${esc(t.display_name)}</span>
        <span class="muted">tier ${esc(t.tier)} · ${esc(t.ladder_label)}</span>
        <span class="chip ${cls}">${esc(t.execution_label)}</span>
        ${sel}${near}
        <span class="muted" title="${esc(t.reason)}">${esc(String(t.reason || "").slice(0, 70))}</span>
      </div>`;
    }).join("");
    const foot = plan.download_gb
      ? `<div class="muted small" style="margin-top:6px">Selected download: ${fmtGB(plan.download_gb)} · disk ${plan.disk_ok ? "ok" : "insufficient"}</div>`
      : "";
    box.innerHTML = head + rows + foot;
    box.classList.remove("muted");
  }

  function renderJobs(jobs) {
    const rows = jobs || [];
    if (!rows.length) { $("jobList").textContent = "No install jobs."; return; }
    $("jobList").innerHTML = rows.slice(0, 30).map((j) => `
      <div class="job-row">
        <span>${esc(j.id || j.job_id || "")}</span>
        <span class="name" title="${esc(j.error || "")}">${esc(j.model_id || j.title || "")}</span>
        <span class="state ${esc(j.state || j.status)}">${esc(j.state || j.status)}</span>
        <span>${Math.round((j.progress || 0) * 100)}%</span>
        <span class="muted">${esc((j.detail || "").slice(0, 60))}</span>
        <span></span>
      </div>`).join("");
    $("jobList").classList.remove("muted");
  }

  function renderPerf(telemetry) {
    const gens = (telemetry.generations || {}).models || [];
    const groups = telemetry.groups || [];
    if (!gens.length && !groups.length) {
      $("perfList").textContent = "No measured performance data yet — stats accumulate as models generate.";
      $("perfList").classList.add("muted");
      return;
    }
    $("perfList").innerHTML =
      gens.map((g) => `
      <div class="proc-row">
        <span class="name">${esc(g.model_id)}</span>
        <span>${g.avg_predicted_per_second ? esc(g.avg_predicted_per_second) + " tok/s avg" : "—"}</span>
        <span>${g.last_predicted_per_second ? "last " + esc(g.last_predicted_per_second) + " tok/s" : ""}</span>
        <span>${g.avg_time_to_first_token_ms != null ? "TTFT " + esc(g.avg_time_to_first_token_ms) + " ms" : ""}</span>
        <span class="muted">${g.samples} generation${g.samples === 1 ? "" : "s"}</span>
      </div>`).join("") +
      (groups.length ? `<div class="muted small" style="margin-top:8px">Task outcomes (learned routing signal)</div>` +
        groups.map((g) => `
      <div class="proc-row">
        <span class="name">${esc(g.model_id)}</span>
        <span class="muted">${esc(g.role)} · ${esc(g.complexity_band)}</span>
        <span>${g.clean_completions}/${g.samples} clean</span>
        <span>${g.verification_passes || g.verification_failures ? `verify ${g.verification_passes}/${g.verification_passes + g.verification_failures}` : ""}</span>
        <span class="muted">avg ${esc(g.average_elapsed_seconds)}s</span>
      </div>`).join("") : "");
    $("perfList").classList.remove("muted");
  }

  async function loadRuntime() {
    const runtime = await api("/api/runtime");
    renderHardware(runtime.hardware);
    renderRuntimes(runtime);
    renderFiles(runtime.inventory, runtime.model_storage);
    renderCatalog(runtime.catalog);
    renderJobs(runtime.install_jobs);
  }

  async function loadModels() {
    const data = await api("/api/models");
    renderProfiles(data.models || []);
  }

  async function loadPerf() {
    const data = await api("/api/model-telemetry");
    renderPerf(data);
  }

  async function loadTierPlan() {
    try {
      const data = await api("/api/model-plan");
      renderTierPlan(data);
    } catch (e) {
      const box = $("tierPlanList");
      if (box) box.textContent = "Tier plan unavailable.";
    }
  }

  function renderTuning(tuning, mode, models) {
    const box = $("tuningList");
    if (!tuning) { box.textContent = "Runtime tuner unavailable."; return; }
    const caps = tuning.capabilities || [];
    const spec = tuning.speculative_decoding || "unknown";
    const results = tuning.results || {};
    const rows = (models || []).filter((m) => m.runtime === "llama_cpp").map((m) => {
      const r = results[m.id];
      const metrics = (r && r.metrics) || {};
      const bits = [
        r ? esc(r.source || "heuristic") : "heuristic",
        metrics.predicted_per_second ? esc(metrics.predicted_per_second) + " tok/s" : "",
        metrics.ttft_ms != null ? "TTFT " + esc(metrics.ttft_ms) + " ms" : "",
        metrics.prompt_per_second ? "prompt " + esc(metrics.prompt_per_second) + " tok/s" : "",
      ].filter(Boolean).join(" · ");
      return `
      <div class="proc-row">
        <span class="name">${esc(m.id)}</span>
        <span class="muted">${bits || "not yet benchmarked"}</span>
        <span>
          <button class="mini-button" data-tune="${esc(m.id)}">Benchmark</button>
          <button class="mini-button" data-tune-reset="${esc(m.id)}">Reset</button>
        </span>
      </div>`;
    }).join("");
    box.innerHTML = `
      <div class="proc-row"><span class="name">llama.cpp</span><span class="muted">${esc(tuning.build || "unknown build")}</span></div>
      <div class="proc-row"><span class="name">Capabilities</span><span class="muted">${caps.length ? esc(caps.join(" ")) : "not probed"}</span></div>
      <div class="proc-row"><span class="name">Speculative decoding</span><span class="muted">${esc(String(spec).replaceAll("_", " "))}</span></div>
      ${rows || '<div class="proc-row muted">No managed llama.cpp profiles.</div>'}`;
    box.classList.remove("muted");
    if ($("perfMode")) $("perfMode").value = mode || "auto";
  }

  async function loadTuning() {
    const data = await api("/api/tuning");
    const models = await api("/api/models").catch(() => ({ models: [] }));
    renderTuning(data, data.performance_mode || "auto", models.models || []);
  }

  async function tuningAction(body) {
    await post("/api/tuning", body);
    await loadTuning();
  }

  async function loadAll() {
    await Promise.all([loadRuntime(), loadModels(), loadPerf(), loadTuning(), loadTierPlan()]).catch((e) => alert(e.message));
  }

  $("refreshAll").addEventListener("click", loadAll);
  $("tuningList").addEventListener("click", async (e) => {
    const bench = e.target.closest("[data-tune]");
    const reset = e.target.closest("[data-tune-reset]");
    if (bench) { bench.disabled = true; try { await tuningAction({ action: "benchmark", model_id: bench.dataset.tune }); } catch (err) { alert(err.message); } finally { bench.disabled = false; } }
    if (reset) { reset.disabled = true; try { await tuningAction({ action: "reset", model_id: reset.dataset.tuneReset }); } catch (err) { alert(err.message); } finally { reset.disabled = false; } }
  });
  if ($("perfMode")) $("perfMode").addEventListener("change", async (e) => {
    e.target.disabled = true;
    try { await tuningAction({ action: "mode", mode: e.target.value }); } catch (err) { alert(err.message); } finally { e.target.disabled = false; }
  });
  loadAll();
  setInterval(() => loadRuntime().catch(() => {}), 8000);
})();
