// Tools & Plugins — operational catalog: install state, health, runtime
// management. Authorization/approvals live under Settings > Permissions.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = (p) => fetch(p).then((r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(new Error(e.error || r.status)))));
  const post = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) })
    .then((r) => r.json().then((d) => (r.ok || d.needs_approval || d.needs_creator ? d : Promise.reject(new Error(d.error || r.status)))));
  const fmtBytes = (n) => (!n ? "" : n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} KB`);
  const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "");
  const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");
  const fmtUptime = (s) => (s > 3600 ? `${(s / 3600).toFixed(1)}h` : s > 60 ? `${Math.round(s / 60)}m` : `${Math.round(s)}s`);
  window.addEventListener("error", (e) => {
    const g = $("toolGrid");
    if (g && /Loading/.test(g.textContent || "")) g.textContent = `JS error: ${e.message} (line ${e.lineno})`;
  });

  const INSTALL_KINDS = new Set(["tool_install", "install", "tool_remove", "image_install"]);
  const ACTIVE_STATES = new Set(["queued", "preparing", "running", "downloading", "extracting", "verifying", "waiting_for_permission"]);

  let tools = [];
  let categories = [];
  let partials = {};
  let diskFreeBytes = 0;
  let processes = [];
  let jobs = [];
  let mcpServers = [];
  let imageStatus = [];
  let imageEnabled = false;

  let search = "";
  let filter = "all";       // chip id
  let catFilter = "";       // sidebar category
  let sortBy = "name";
  let selectedTool = "";
  let detailTab = "overview";
  let detailLog = null;     // {id, text} cache for Logs tab

  const FILTERS = [
    ["all", "All"], ["installed", "Installed"], ["available", "Available"],
    ["updates", "Updates"], ["running", "Running"], ["disabled", "Disabled"],
    ["native", "Native"], ["mcp", "MCP"],
    ["coding", "Developer"], ["images", "Image"], ["research", "Research"],
    ["media", "Media"], ["documents", "Documents"], ["data", "Data"],
  ];
  const CAT_GROUPS = { media: ["audio", "video"], coding: ["coding", "devops", "git", "3d", "utilities", "build"] };
  const catMatch = (t, f) => (CAT_GROUPS[f] || [f]).includes(t.category);

  // ------------------------------------------------------------- data state

  const jobForTool = (name) =>
    jobs.find((j) => INSTALL_KINDS.has(j.kind) && ACTIVE_STATES.has(j.state)
      && ((j.metadata || {}).tool === name || (j.title || "").toLowerCase().includes(String(name).replace(/_/g, " "))));
  const procForTool = (t) =>
    t.process_id ? processes.find((p) => p.id === t.process_id)
      : processes.find((p) => p.id === t.id || p.id === t.name || p.name === t.display_name);
  const isRunning = (t) => {
    const p = procForTool(t);
    return !!p && ["running", "healthy"].includes(p.state);
  };

  function toolStatus(t) {
    if (!t.enabled) return "disabled";
    if (t.health && t.health.result && !t.health.result.ok) return "error";
    if (isRunning(t)) return "running";
    if (t.update_available) return "update";
    if (t.install_status === "installed") return "installed";
    return "available";
  }
  const STATUS_LABEL = { installed: "Installed", available: "Available", update: "Update", running: "Running", disabled: "Disabled", error: "Error" };

  function passesFilter(t) {
    const st = toolStatus(t);
    if (catFilter && t.category !== catFilter) return false;
    if (filter === "installed") return st === "installed" || st === "running" || st === "error" || st === "update";
    if (filter === "available") return st === "available";
    if (filter === "updates") return !!t.update_available;
    if (filter === "running") return st === "running";
    if (filter === "disabled") return !t.enabled;
    if (filter === "native") return t.source !== "mcp" && !t.mcp_server;
    if (filter === "mcp") return t.source === "mcp" || !!t.mcp_server;
    if (filter !== "all") return catMatch(t, filter);
    return true;
  }

  function passesSearch(t) {
    if (!search) return true;
    const q = search.toLowerCase();
    return [t.display_name, t.id, t.name, t.description, t.provider, t.category,
      ...(t.capabilities || [])].join(" ").toLowerCase().includes(q);
  }

  const STATUS_RANK = { error: 0, update: 1, running: 2, installed: 3, disabled: 4, available: 5 };
  function sorted(list) {
    const key = {
      name: (t) => t.display_name.toLowerCase(),
      status: (t) => STATUS_RANK[toolStatus(t)] ?? 9,
      category: (t) => `${t.category}:${t.display_name.toLowerCase()}`,
      used: (t) => -(t.last_used_at || 0),
      installed: (t) => -(t.installed_at || 0),
    }[sortBy] || ((t) => t.display_name.toLowerCase());
    return [...list].sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
  }

  // ---------------------------------------------------------------- loading

  async function loadTools() {
    const data = await api("/api/tools");
    tools = data.tools || [];
    categories = data.categories || [];
    partials = data.partials || {};
    diskFreeBytes = data.disk_free_bytes || 0;
    renderSummary();
    renderCategories();
    renderChips();
    renderGrid();
  }

  async function loadProcesses() {
    const data = await api("/api/processes");
    processes = data.processes || [];
    renderProcesses();
    renderSummary();
  }

  async function loadMcp() {
    const data = await api("/api/mcp");
    mcpServers = data.servers || [];
    renderMcp();
  }

  async function loadJobs() {
    const data = await api("/api/jobs");
    jobs = data.jobs || [];
    renderQueue();
    renderJobs();
    renderSummary();
  }

  async function loadQueue() {
    const host = $("queueList");
    try {
      const data = await api("/api/queue");
      const rows = (data.items || []).slice(0, 60);
      if (!rows.length) { host.textContent = "Queue is empty — prompts sent while a task runs land here."; host.classList.add("muted"); return; }
      host.innerHTML = rows.map((q, i) => `
        <div class="job-row">
          <span class="muted">#${i + 1}</span>
          <span class="name" title="${esc(q.prompt || "")}">${esc((q.prompt || "").slice(0, 90)) || "(empty)"}</span>
          <span class="state queued">${esc(q.status || "queued")}</span>
          <span>${esc(q.mode || "auto")}</span>
          <span class="muted">${fmtTime(q.enqueued_at)}</span>
          <span class="row-actions"><button data-queue="${esc(q.id)}">Cancel</button></span>
        </div>`).join("");
      host.classList.remove("muted");
      host.querySelectorAll("[data-queue]").forEach((btn) => btn.addEventListener("click", async () => {
        btn.disabled = true;
        try { await post("/api/queue/cancel", { id: btn.dataset.queue }); }
        catch (e) { alert(e.message); }
        setTimeout(loadQueue, 400);
      }));
    } catch (e) { host.textContent = `Queue unavailable: ${e.message}`; }
  }

  async function loadTelemetry() {
    const host = $("telemetryList");
    try {
      const data = await api("/api/tools/telemetry");
      const stats = (data.stats && data.stats.routes) || [];
      const recent = (data.routing || []).slice(-15).reverse();
      if (!stats.length && !recent.length) {
        host.textContent = "No routing decisions yet — tools chosen via use_capability/run_workflow appear here.";
        return;
      }
      const statsHtml = stats.length ? `<div class="telemetry-sub">Learned routing (feeds tool ranking)</div>
        ${stats.map((r) => `<div class="telemetry-row"><span class="name">${esc(r.capability)}</span><span>${esc(r.tool || "—")}</span><span class="muted">${r.calls} calls</span><span class="${r.success_rate >= 0.8 ? "ok" : "warn"}">${Math.round(r.success_rate * 100)}%</span><span class="muted">${r.avg_ms}ms avg</span></div>`).join("")}` : "";
      const recentHtml = recent.length ? `<div class="telemetry-sub">Recent decisions</div>
        ${recent.map((e) => `<div class="telemetry-row"><span class="name">${esc(e.capability)}</span><span>${esc(e.chosen || "—")}</span><span class="${e.ok ? "ok" : "warn"}">${e.ok ? "ok" : "failed"}</span><span class="muted">${Math.round(e.elapsed_ms || 0)}ms · ${fmtTime(e.ts)}</span></div>`).join("")}` : "";
      host.innerHTML = statsHtml + recentHtml;
      host.classList.remove("muted");
    } catch (e) { host.textContent = `Telemetry unavailable: ${e.message}`; }
  }

  async function loadWorkflows() {
    const host = $("workflowList");
    try {
      const data = await api("/api/workflows");
      const rows = data.workflows || [];
      if (!rows.length) {
        host.textContent = `No workflows in ${data.directory || "workflows/"} — add *.json step pipelines to enable multi-step automation.`;
        return;
      }
      host.innerHTML = rows.map((w) => `
        <div class="telemetry-row">
          <span class="name" title="${esc(w.description || "")}">${esc(w.name)}</span>
          <span class="muted">${esc(w.id)}</span>
          <span class="muted">${(w.steps || []).length} steps</span>
          <span class="muted">${esc((w.steps || []).join(" → "))}</span>
          ${w.resumable ? `<span class="chip cap" title="Checkpoint saved — run_workflow with resume=true continues here">resumable @ step ${w.resume_step}</span>` : ""}
        </div>`).join("");
      host.classList.remove("muted");
    } catch (e) { host.textContent = `Workflows unavailable: ${e.message}`; }
  }

  async function loadImagePacks() {
    const host = $("imagePackList");
    try {
      const data = await api("/api/image");
      imageEnabled = !!data.enabled;
      imageStatus = data.model_status || [];
    } catch (e) {
      imageStatus = [];
      host.textContent = `Image workspace unavailable: ${e.message}`;
      return;
    }
    renderImagePacks();
  }

  async function refreshAll() {
    await Promise.all([loadTools(), loadProcesses(), loadMcp(), loadJobs(),
      loadImagePacks(), loadQueue(), loadTelemetry(), loadWorkflows()]);
    if (selectedTool) renderDetail();
  }

  // ---------------------------------------------------------------- summary

  function renderSummary() {
    const installed = tools.filter((t) => t.install_status === "installed").length;
    const available = tools.filter((t) => t.install_status === "missing" && t.enabled).length;
    const updates = tools.filter((t) => t.update_available).length;
    const errors = tools.filter((t) => t.health && t.health.result && !t.health.result.ok).length;
    const running = processes.filter((p) => ["running", "healthy"].includes(p.state)).length;
    const card = (cls, num, label, f) =>
      `<div class="sum-card ${cls}" data-sumfilter="${f}"><div class="s-num">${num}</div><div class="s-label">${esc(label)}</div></div>`;
    $("summaryCards").innerHTML =
      card("green", installed, "Installed Tools", "installed") +
      card("cyan", available, "Available Tools", "available") +
      card("amber", updates, "Updates", "updates") +
      card("blue", running, "Running Services", "running") +
      (errors ? card("red", errors, "Health Errors", "all") : "");
    document.querySelectorAll("[data-sumfilter]").forEach((c) =>
      c.addEventListener("click", () => { filter = c.dataset.sumfilter; catFilter = ""; renderChips(); renderGrid(); }));
    const df = $("diskFree");
    if (df) df.textContent = diskFreeBytes ? `${fmtBytes(diskFreeBytes)} free on install drive` : "";
  }

  function renderCategories() {
    const counts = {};
    tools.forEach((t) => { counts[t.category] = (counts[t.category] || 0) + 1; });
    const list = categories.length ? categories : Object.keys(counts).sort();
    $("categoryList").innerHTML = list.map((c) =>
      `<div class="category-item ${catFilter === c ? "selected" : ""}" data-cat="${esc(c)}"><span>${esc(c.replace("_", " "))}</span><span>${counts[c] || 0}</span></div>`).join("")
      || "No categories.";
    $("categoryList").classList.remove("muted");
    $("categoryList").querySelectorAll("[data-cat]").forEach((el) =>
      el.addEventListener("click", () => {
        catFilter = catFilter === el.dataset.cat ? "" : el.dataset.cat;
        renderCategories(); renderGrid();
      }));
  }

  function renderChips() {
    $("filterChips").innerHTML = FILTERS.map(([id, label]) =>
      `<button class="fchip ${filter === id && !catFilter ? "active" : ""}" data-filter="${id}">${esc(label)}</button>`).join("");
    $("filterChips").querySelectorAll("[data-filter]").forEach((b) =>
      b.addEventListener("click", () => {
        filter = b.dataset.filter; catFilter = "";
        renderChips(); renderCategories(); renderGrid();
      }));
  }

  // ------------------------------------------------------------------- grid

  function statusDot(t) {
    const st = toolStatus(t);
    const label = st === "update" ? `Update v${t.latest_version || t.version}` : STATUS_LABEL[st];
    return `<span class="status-dot ${st}">${esc(label)}</span>`;
  }

  function toolCard(t) {
    const spec = t.install || {};
    const job = jobForTool(t.name);
    const busy = !!job;
    const proc = procForTool(t);
    const running = isRunning(t);
    const installable = t.installable !== undefined ? !!t.installable
      : ["winget", "choco", "uv", "npm", "apt", "dnf", "brew", "pip", "archive"].includes(String(spec.method || ""));
    const osOk = t.os_supported !== false;
    const partial = partials[t.name];
    const caps = (t.capabilities || []).slice(0, 3);
    const actions = [];
    if (busy) actions.push(`<button data-cancel-job="${esc(job.id)}">Cancel</button>`);
    if (!busy && running) {
      if (proc && proc.port) actions.push(`<button class="primary" data-open-port="${proc.port}">Open</button>`);
      if (proc && proc.can_stop) actions.push(`<button data-proc="${esc(proc.id)}" data-action="stop">Stop</button>`);
      if (proc && proc.can_restart) actions.push(`<button data-proc="${esc(proc.id)}" data-action="restart">Restart</button>`);
    }
    if (!busy && !running && proc && proc.can_start && t.install_status === "installed")
      actions.push(`<button data-proc="${esc(proc.id)}" data-action="start">Start</button>`);
    if (!busy && osOk && t.install_status === "missing" && installable)
      actions.push(`<button class="primary" data-install="${esc(t.name)}">${partial ? "Resume Install" : "Install"}</button>`);
    if (!busy && osOk && t.install_status === "installed" && spec.method === "archive")
      actions.push(`<button data-install="${esc(t.name)}">${t.update_available ? "Update" : "Reinstall"}</button>`);
    if (!busy && t.install_status === "installed" && t.removable)
      actions.push(`<button class="danger" data-uninstall="${esc(t.name)}">Uninstall</button>`);
    if (!busy && t.install_status === "installed" && installable && spec.method !== "archive")
      actions.push(`<button data-install="${esc(t.name)}" title="Re-run installer">Repair</button>`);
    if (t.has_health_check && t.install_status === "installed")
      actions.push(`<button data-health="${esc(t.name)}">Check</button>`);
    actions.push(`<button class="tool-toggle ${t.enabled ? "on" : "off"}" data-state="${esc(t.name)}" data-enabled="${t.enabled}">${t.enabled ? "Enabled" : "Disabled"}</button>`);
    const initials = esc((t.display_name || t.name || "?").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase());
    return `
      <div class="tcard ${t.enabled ? "" : "disabled"} ${selectedTool === t.name ? "selected" : ""}" data-tool="${esc(t.name)}">
        <div class="tcard-top">
          <div class="tcard-icon">${initials}</div>
          <div style="min-width:0">
            <div class="tcard-name">${esc(t.display_name)}</div>
            <div class="tcard-sub">${esc(t.id)} · v${esc(t.version)} · ${esc(t.provider)}</div>
          </div>
          ${statusDot(t)}
        </div>
        <div class="tcard-desc">${esc(t.description)}</div>
        <div class="tcard-tags">
          <span class="chip">${esc(t.category.replace("_", " "))}</span>
          <span class="chip perm ${esc(t.permission_mode)}">${esc(t.permission)}: ${esc(t.permission_mode)}</span>
          ${t.requires_network ? '<span class="chip net">network</span>' : ""}
          ${t.requires_gpu ? '<span class="chip gpu">GPU</span>' : ""}
          ${!osOk ? `<span class="chip off">${esc((t.supported_os || ["other"]).join("/"))} only</span>` : ""}
          ${partial ? `<span class="chip net" title="Partial download kept">resumable · ${fmtBytes(partial)}</span>` : ""}
          ${caps.map((c) => `<span class="chip cap">${esc(c)}</span>`).join("")}
        </div>
        ${busy ? progressHtml(job) : ""}
        ${t.health && t.health.result ? `<div class="dtl-note">health: ${esc(t.health.result.status)} — ${esc(String(t.health.result.detail || "").slice(0, 80))}</div>` : ""}
        <div class="tcard-actions">${actions.join("")}</div>
      </div>`;
  }

  function progressHtml(j) {
    const m = j.metadata || {};
    const pct = Math.round((j.progress || 0) * 100);
    const dl = m.download_progress != null ? ` · dl ${Math.round(m.download_progress * 100)}%` : "";
    const ex = m.extract_progress != null ? ` · ex ${Math.round(m.extract_progress * 100)}%` : "";
    const spd = m.bytes_per_sec ? ` · ${fmtBytes(m.bytes_per_sec)}/s` : "";
    const eta = m.eta_seconds ? ` · ~${fmtUptime(m.eta_seconds)} left` : "";
    return `<div class="tool-progress"><div class="install-bar"><div class="install-fill" style="width:${pct}%"></div></div>
      <div class="install-meta"><span>${esc(m.phase || j.state)}${dl}${ex}${spd}${eta}</span><span>${pct}%</span></div>
      ${m.current_file ? `<div class="i-file">${esc(m.current_file)}${m.current_path ? ` — ${esc(m.current_path)}` : ""}</div>` : ""}</div>`;
  }

  function renderGrid() {
    const grid = $("toolGrid");
    const list = sorted(tools.filter((t) => passesFilter(t) && passesSearch(t)));
    if (!tools.length) { grid.textContent = "Loading…"; return; }
    if (!list.length) { grid.innerHTML = '<div class="muted" style="padding:20px">No tools match this filter.</div>'; return; }
    grid.innerHTML = list.map(toolCard).join("");
    grid.classList.remove("muted");
    wireGrid(grid);
  }

  function wireGrid(grid) {
    grid.querySelectorAll(".tcard").forEach((card) =>
      card.addEventListener("click", (ev) => {
        if (ev.target.tagName === "BUTTON" || ev.target.tagName === "SELECT") return;
        selectedTool = card.dataset.tool;
        detailTab = "overview";
        detailLog = null;
        grid.querySelectorAll(".tcard").forEach((c) => c.classList.toggle("selected", c.dataset.tool === selectedTool));
        renderDetail();
      }));
    grid.querySelectorAll("[data-install]").forEach((b) => b.addEventListener("click", () => doInstall(b)));
    grid.querySelectorAll("[data-uninstall]").forEach((b) => b.addEventListener("click", () => doUninstall(b)));
    grid.querySelectorAll("[data-cancel-job]").forEach((b) => b.addEventListener("click", () => doCancelJob(b.dataset.cancelJob)));
    grid.querySelectorAll("[data-health]").forEach((b) => b.addEventListener("click", () => doHealth(b)));
    grid.querySelectorAll("[data-state]").forEach((b) => b.addEventListener("click", () => doToggle(b)));
    grid.querySelectorAll("[data-proc]").forEach((b) => b.addEventListener("click", () => doProcAction(b)));
    grid.querySelectorAll("[data-open-port]").forEach((b) => b.addEventListener("click", () => window.open(`http://127.0.0.1:${b.dataset.openPort}`, "_blank")));
  }

  // ---------------------------------------------------------------- actions

  async function doInstall(btn) {
    btn.disabled = true;
    const tool = tools.find((t) => t.name === btn.dataset.install);
    try {
      let res = await post("/api/tools/install", { tool: btn.dataset.install });
      if (res.needs_approval) {
        const warn = res.error ? `\n${res.error}` : "";
        if (res.needs_creator) { alert(`This action requires an unlocked Nexus Brain creator session.${warn}`); btn.disabled = false; return; }
        if (!confirm(`Install ${tool ? tool.display_name : btn.dataset.install}?${warn}`)) { btn.disabled = false; return; }
        res = await post("/api/tools/install", { tool: btn.dataset.install, approve: true });
      }
      if (res.needs_creator) { alert("Requires an unlocked Nexus Brain creator session."); return; }
      if (res.error) { alert(res.error + (res.warning ? `\n${res.warning}` : "")); return; }
      setTimeout(refreshAll, 600);
    } catch (e) { alert(e.message); btn.disabled = false; }
  }

  async function doUninstall(btn) {
    const tool = tools.find((t) => t.name === btn.dataset.uninstall);
    const method = String(((tool || {}).install || {}).method || "");
    const prompt = method === "archive" || !method
      ? `Uninstall ${btn.dataset.uninstall}? This deletes its installed files.`
      : `Uninstall ${tool ? tool.display_name : btn.dataset.uninstall} via ${method}?`;
    if (!confirm(prompt)) return;
    btn.disabled = true;
    try {
      let res = await post("/api/tools/uninstall", { tool: btn.dataset.uninstall });
      if (res.needs_approval) {
        if (res.needs_creator) { alert("Requires an unlocked Nexus Brain creator session."); btn.disabled = false; return; }
        if (!confirm("Confirm removal")) { btn.disabled = false; return; }
        res = await post("/api/tools/uninstall", { tool: btn.dataset.uninstall, approve: true });
      }
      if (res.error) { alert(res.error); btn.disabled = false; return; }
      setTimeout(refreshAll, 600);
    } catch (e) { alert(e.message); btn.disabled = false; }
  }

  async function doToggle(btn) {
    btn.disabled = true;
    try {
      await post("/api/tools/state", { tool: btn.dataset.state, enabled: btn.dataset.enabled !== "true" });
      await loadTools();
    } catch (e) { alert(e.message); btn.disabled = false; }
  }

  async function doHealth(btn) {
    btn.disabled = true;
    try {
      const res = await api(`/api/tools/health/${encodeURIComponent(btn.dataset.health)}`);
      const h = res.health || {};
      alert(`${btn.dataset.health}: ${h.status}${h.detail ? ` — ${h.detail}` : ""}`);
      await loadTools();
      if (selectedTool) renderDetail();
    } catch (e) { alert(e.message); }
    btn.disabled = false;
  }

  async function doCancelJob(jobId) {
    try { await post("/api/jobs/cancel", { job_id: jobId }); setTimeout(loadJobs, 500); }
    catch (e) { alert(e.message); }
  }

  async function doProcAction(btn) {
    btn.disabled = true;
    try { await post("/api/processes/action", { id: btn.dataset.proc, action: btn.dataset.action }); }
    catch (e) { alert(e.message); }
    btn.disabled = false;
    setTimeout(loadProcesses, 800);
  }

  // --------------------------------------------------------- install queue

  function renderQueue() {
    const queue = $("installQueue");
    const rows = jobs.filter((j) => INSTALL_KINDS.has(j.kind)).slice(0, 12);
    const active = rows.filter((j) => ACTIVE_STATES.has(j.state));
    // Resumable partials with no active job → resumable rows.
    const resumable = Object.entries(partials)
      .filter(([name]) => !active.some((j) => (j.metadata || {}).tool === name))
      .map(([name, bytes]) => ({ name, bytes }));
    $("installQueueMeta").textContent =
      `${active.length} active${resumable.length ? ` · ${resumable.length} resumable` : ""}`;
    if (!rows.length && !resumable.length) { queue.style.display = "none"; return; }
    queue.style.display = "";
    queue.classList.toggle("collapsed", !active.length);
    $("installList").innerHTML =
      rows.map((j) => {
        const m = j.metadata || {};
        const pct = Math.round((j.progress || 0) * 100);
        const spd = m.bytes_per_sec ? ` · ${fmtBytes(m.bytes_per_sec)}/s` : "";
        const eta = m.eta_seconds ? ` · ~${fmtUptime(m.eta_seconds)} left` : "";
        const bytes = m.bytes_done ? `${fmtBytes(m.bytes_done)}${m.bytes_total ? ` of ${fmtBytes(m.bytes_total)}` : ""}` : "";
        return `<div class="inst-row">
          <div class="i-head"><span>${esc(j.title || j.id)}</span>
            <span class="i-state state ${esc(j.state)}">${esc(m.phase || j.state)}</span></div>
          <div class="install-bar"><div class="install-fill" style="width:${pct}%"></div></div>
          <div class="i-meta"><span>${bytes}${spd}${eta}${m.dest ? ` · ${esc(m.dest)}` : ""}</span><span>${pct}%</span></div>
          ${m.current_file ? `<div class="i-file">${esc(m.current_file)}${m.current_path ? ` — ${esc(m.current_path)}` : ""}</div>` : ""}
          ${j.error ? `<div class="i-file" style="color:#ff97a5">${esc(j.error)}</div>` : ""}
          <div class="i-actions">${j.cancellable ? `<button class="mini-button" data-qjob="${esc(j.id)}">Cancel</button>` : ""}</div>
        </div>`;
      }).join("") +
      resumable.map(([name, bytes]) => {
        const t = tools.find((x) => x.name === name);
        return `<div class="inst-row">
          <div class="i-head"><span>${esc(t ? t.display_name : name)}<span class="resumable-tag">Resumable — ${fmtBytes(bytes)} saved</span></span>
            <span class="i-state">partial</span></div>
          <div class="i-meta"><span>Partial download kept at .agent/downloads/${esc(name)}.part — Resume continues where it left off.</span></div>
          <div class="i-actions"><button class="mini-button" data-resume="${esc(name)}">Resume</button></div>
        </div>`;
      }).join("");
    $("installList").querySelectorAll("[data-qjob]").forEach((b) => b.addEventListener("click", () => doCancelJob(b.dataset.qjob)));
    $("installList").querySelectorAll("[data-resume]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      let res = await post("/api/tools/install", { tool: b.dataset.resume });
      if (res.needs_approval) res = await post("/api/tools/install", { tool: b.dataset.resume, approve: true });
      if (res.error) { alert(res.error); b.disabled = false; return; }
      setTimeout(refreshAll, 600);
    }));
  }

  // ------------------------------------------------------------- processes

  function renderProcesses() {
    const host = $("processList");
    if (!processes.length) { host.textContent = "No managed services registered."; return; }
    host.innerHTML = processes.map((p) => `
      <div class="proc-row">
        <span class="name" title="${esc(p.id)}">${esc(p.name)}</span>
        <span>${esc(p.kind)}</span>
        <span class="state ${esc(p.state)}">${esc(p.state)}</span>
        <span>${p.pid ? `pid ${p.pid}` : "—"}</span>
        <span class="muted">${p.port || "—"}${p.uptime_seconds ? ` · ${fmtUptime(p.uptime_seconds)}` : ""}${p.error ? ` · ${esc(String(p.error).slice(0, 60))}` : ""}</span>
        <span class="row-actions">
          ${p.can_start ? `<button data-proc="${esc(p.id)}" data-action="start">Start</button>` : ""}
          ${p.can_stop ? `<button data-proc="${esc(p.id)}" data-action="stop">Stop</button>` : ""}
          ${p.can_restart ? `<button data-proc="${esc(p.id)}" data-action="restart">Restart</button>` : ""}
          ${p.log_path ? `<button data-proclog="${esc(p.id)}">Logs</button>` : ""}
        </span>
      </div>`).join("");
    host.classList.remove("muted");
    host.querySelectorAll("[data-proc]").forEach((b) => b.addEventListener("click", () => doProcAction(b)));
    host.querySelectorAll("[data-proclog]").forEach((b) => b.addEventListener("click", async () => {
      const t = tools.find((x) => x.process_id === b.dataset.proclog);
      if (t) { selectedTool = t.name; detailTab = "logs"; renderDetail(); }
      else {
        try {
          const r = await api(`/api/processes/log?id=${encodeURIComponent(b.dataset.proclog)}`);
          alert(r.log ? r.log.slice(-1500) : (r.detail || "no log"));
        } catch (e) { alert(e.message); }
      }
    }));
  }

  function renderMcp() {
    const box = $("mcpList");
    if (!mcpServers.length) { box.textContent = "No MCP servers configured. Add mcp_servers entries to config.json."; return; }
    box.innerHTML = mcpServers.map((s) => `
      <div class="mcp-row">
        <div class="mcp-head"><span class="name">${esc(s.name || s.id)}</span><span class="state ${esc(s.state)}">${esc(s.state)}</span></div>
        <div class="muted small">${s.tools} tool(s)${s.pid ? ` · pid ${s.pid}` : ""}${s.error ? ` · ${esc(String(s.error).slice(0, 60))}` : ""}</div>
        <div class="mcp-actions">
          ${s.state !== "connected" ? `<button data-mcp="${esc(s.id)}" data-action="connect">Connect</button>` : ""}
          ${s.state === "connected" ? `<button data-mcp="${esc(s.id)}" data-action="disconnect">Disconnect</button>` : ""}
          <button data-mcp="${esc(s.id)}" data-action="restart">Restart</button>
        </div>
      </div>`).join("");
    box.classList.remove("muted");
    box.querySelectorAll("[data-mcp]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try { await post("/api/mcp/action", { id: b.dataset.mcp, action: b.dataset.action }); }
      catch (e) { alert(e.message); }
      b.disabled = false;
      setTimeout(loadMcp, 800);
    }));
  }

  function renderJobs() {
    const host = $("jobList");
    const rows = jobs.slice(0, 40);
    if (!rows.length) { host.textContent = "No jobs yet."; return; }
    host.innerHTML = rows.map((j) => `
      <div class="job-row">
        <span>${esc(j.kind)}</span>
        <span class="name" title="${esc(j.detail || j.error || "")}">${esc(j.title || j.id)}</span>
        <span class="state ${esc(j.state)}">${esc(j.state)}</span>
        <span>${Math.round((j.progress || 0) * 100)}%</span>
        <span class="muted">${fmtTime(j.created_at)}${j.error ? ` · ${esc(String(j.error).slice(0, 50))}` : ""}</span>
        <span class="row-actions">${j.cancellable ? `<button data-job="${esc(j.id)}">Cancel</button>` : ""}</span>
      </div>`).join("");
    host.classList.remove("muted");
    host.querySelectorAll("[data-job]").forEach((b) => b.addEventListener("click", () => doCancelJob(b.dataset.job)));
  }

  // ------------------------------------------------------------ image packs

  function renderImagePacks() {
    const host = $("imagePackList");
    if (!imageEnabled) { host.textContent = "Image generation is disabled in config.json."; return; }
    if (!imageStatus.length) { host.textContent = "No image model packs configured."; return; }
    $("imageSummary").textContent = `${imageStatus.filter((s) => s.installed).length}/${imageStatus.length} installed`;
    host.innerHTML = imageStatus.map((p) => {
      const job = jobs.find((j) => j.kind === "image_install" && ACTIVE_STATES.has(j.state)
        && ((j.metadata || {}).model_id === p.id || (j.title || "").includes(p.name || p.id)));
      const pct = job ? Math.round((job.progress || 0) * 100) : 0;
      const sizeTxt = fmtBytes(p.size_bytes);
      return `<div class="tcard">
        <div class="tcard-top">
          <div class="tcard-icon">◧</div>
          <div><div class="tcard-name">${esc(p.name || p.id)}</div>
          <div class="tcard-sub">${esc(p.id)}${sizeTxt ? ` · ${sizeTxt}` : ""}</div></div>
          <span class="status-dot ${p.installed ? "installed" : "missing"}">${p.installed ? "Installed" : "Missing"}</span>
        </div>
        <div class="tcard-desc">${esc(p.description || "")}</div>
        ${job ? progressHtml(job) : ""}
        <div class="tcard-actions">
          ${!p.installed && !job ? `<button class="primary" data-pack-install="${esc(p.id)}">Install</button>` : ""}
          ${job && job.cancellable ? `<button data-cancel-job="${esc(job.id)}">Cancel</button>` : ""}
        </div>
      </div>`;
    }).join("");
    host.classList.remove("muted");
    host.querySelectorAll("[data-pack-install]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await post("/api/image/models/install", { model_id: b.dataset.packInstall });
        setTimeout(() => { loadJobs(); loadImagePacks(); }, 800);
      } catch (e) { alert(e.message); b.disabled = false; }
    }));
    host.querySelectorAll("[data-cancel-job]").forEach((b) => b.addEventListener("click", () => doCancelJob(b.dataset.cancelJob)));
  }

  // ---------------------------------------------------------- detail panel

  function currentTool() { return tools.find((t) => t.name === selectedTool); }

  function renderDetail() {
    const panel = $("toolDetail");
    const t = currentTool();
    if (!t) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");
    $("detailName").textContent = t.display_name;
    $("detailSub").textContent = `${t.id} · ${t.category} · ${t.provider}`;
    $("detailTabs").querySelectorAll("button").forEach((b) =>
      b.classList.toggle("active", b.dataset.tab === detailTab));
    const body = $("detailBody");
    const proc = procForTool(t);
    const spec = t.install || {};
    const row = (k, v) => `<div class="dtl-row"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;
    let html = "";
    if (detailTab === "overview") {
      html =
        row("Status", STATUS_LABEL[toolStatus(t)]) +
        (t.enabled === false ? row("Enabled", "No — disabled") : "") +
        row("Version", esc(t.version)) +
        (t.installed_version ? row("Installed version", esc(t.installed_version)) : "") +
        row("Latest known version", t.update_check === "checked"
            ? esc(t.latest_version)
            : t.update_check === "current"
              ? "Up to date (checked " + esc(fmtDate(t.update_checked_at)) + ")"
              : "Update check unavailable") +
        row("Provider / source", `${esc(t.provider)} · ${esc(t.source)}`) +
        row("Permission", `${esc(t.permission)} <span class="badge ${esc(t.permission_mode)}">${esc(t.permission_mode)}</span>`) +
        row("OS support", esc((t.supported_os || []).join(", ") || "all") + (t.os_supported === false ? " — not supported here" : "")) +
        row("GPU", t.requires_gpu ? "required" : "not required") +
        row("Network", t.requires_network ? "required" : "not required") +
        row("Install location", esc(t.install_path || "—")) +
        row("Install size", esc(t.install_size_bytes ? fmtBytes(t.install_size_bytes) : "—")) +
        row("Installed", esc(t.installed_at ? fmtDate(t.installed_at) : "—")) +
        row("Last used", esc(t.last_used_at ? fmtDate(t.last_used_at) : "never")) +
        row("Use count", String(t.use_count || 0)) +
        (proc ? row("Service", `${esc(proc.state)}${proc.pid ? ` · pid ${proc.pid}` : ""}${proc.port ? ` · :${proc.port}` : ""}`) : "") +
        (t.health && t.health.result
          ? row("Health", `${esc(t.health.result.status)}${t.health.result.detail ? ` — ${esc(String(t.health.result.detail).slice(0, 120))}` : ""}`) +
            row("Checked at", fmtDate(t.health.at))
          : "") +
        (t.docs ? `<div class="dtl-note" style="margin-top:8px">${esc(t.docs)}</div>` : "");
      const busy = jobForTool(t.name);
      html += `<div class="dtl-actions" style="padding:10px 0 0;border-top:1px solid #1c2634;margin-top:8px">
        ${!busy && t.install_status === "missing" && t.os_supported !== false && (t.installable !== undefined ? t.installable : spec.method) ? `<button data-dact="install">Install</button>` : ""}
        ${t.install_status === "missing" && t.installable === false && spec.notes ? `<div class="dtl-note muted">Manual install: ${esc(spec.notes)}</div>` : ""}
        ${!busy && t.install_status === "installed" && spec.method === "archive" ? `<button data-dact="install">${t.update_available ? "Update" : "Reinstall"}</button>` : ""}
        ${!busy && t.install_status === "installed" && t.removable ? `<button class="danger" data-dact="uninstall">Uninstall</button>` : ""}
        ${t.has_health_check && t.install_status === "installed" ? `<button data-dact="health">Run health check</button>` : ""}
        ${proc && proc.can_start ? `<button data-dact="start">Start service</button>` : ""}
        ${proc && proc.can_stop ? `<button data-dact="stop">Stop service</button>` : ""}
        ${proc && proc.can_restart ? `<button data-dact="restart">Restart service</button>` : ""}
        <button data-dact="toggle">${t.enabled ? "Disable" : "Enable"}</button>
      </div>`;
    } else if (detailTab === "capabilities") {
      html =
        `<div class="dtl-note">Capabilities route work through the Tool Router.</div>` +
        (t.capabilities || []).map((c) => `<div class="dtl-row"><span class="v" style="text-align:left">${esc(c)}</span></div>`).join("") +
        row("Required permissions", esc((t.permissions_required || []).join(", ") || "—")) +
        row("Callable by agent", t.callable ? "yes" : "no (catalog only)");
    } else if (detailTab === "dependencies") {
      const deps = t.dependencies || [];
      const reqs = t.requirements || {};
      html =
        (deps.length ? `<div class="dtl-note">Declared dependencies:</div>` + deps.map((d) => `<div class="dtl-row"><span class="v" style="text-align:left">${esc(d)}</span></div>`).join("") : "") +
        (Object.keys(reqs).length ? `<div class="dtl-note">Requirements:</div>` + Object.entries(reqs).map(([k, v]) => row(k, esc(String(v)))).join("") : "") +
        ((t.executables || []).length ? `<div class="dtl-note">Executables:</div>` + t.executables.map((e) => `<div class="dtl-row"><span class="v" style="text-align:left"><code>${esc(e)}</code></span></div>`).join("") : "") +
        ((t.detect_files || []).length ? `<div class="dtl-note">Install markers:</div>` + t.detect_files.map((f) => `<div class="dtl-row"><span class="v" style="text-align:left"><code>${esc(f)}</code></span></div>`).join("") : "") +
        (!deps.length && !Object.keys(reqs).length && !(t.executables || []).length && !(t.detect_files || []).length
          ? '<div class="dtl-note">No dependencies declared.</div>' : "");
    } else if (detailTab === "config") {
      // Only real configuration: permission level + enable/disable.
      html =
        `<div class="dtl-note">Permission gate for this tool:</div>` +
        row("Permission key", `<code>${esc(t.permission)}</code>`) +
        row("Effective mode", `<span class="badge ${esc(t.permission_mode)}">${esc(t.permission_mode)}</span>`) +
        `<div class="dtl-note">Edit levels and scopes under <a href="/settings.html#permissions" style="color:var(--cyan)">Settings → Permissions</a>.</div>` +
        row("Manifest", t.manifest_path ? `<code>${esc(t.manifest_path)}</code>` : "built-in") +
        (t.mcp_server ? row("MCP server", esc(t.mcp_server)) : "");
    } else if (detailTab === "logs") {
      html = `<div class="dtl-log">${detailLog ? esc(detailLog.text) : "Loading…"}</div>`;
      if (!detailLog) {
        if (proc && proc.log_path) {
          api(`/api/processes/log?id=${encodeURIComponent(proc.id)}`).then((r) => {
            detailLog = { text: r.log || r.detail || "(empty log)" };
            if (detailTab === "logs") renderDetail();
          }).catch((e) => { detailLog = { text: e.message }; if (detailTab === "logs") renderDetail(); });
        } else {
          const rel = jobs.filter((j) => (j.metadata || {}).tool === t.name ||
            (j.title || "").toLowerCase().includes(t.display_name.toLowerCase())).slice(0, 15);
          detailLog = {
            text: rel.length
              ? rel.map((j) => `${fmtDate(j.created_at)}  ${j.kind}  ${j.state}  ${j.error || j.detail || ""}`).join("\n")
              : "No logs recorded for this tool.",
          };
          html = `<div class="dtl-log">${esc(detailLog.text)}</div>`;
        }
      }
    }
    body.innerHTML = html;
    body.querySelectorAll("[data-dact]").forEach((b) => b.addEventListener("click", () => {
      const act = b.dataset.dact;
      const fake = (ds) => ({ dataset: ds, disabled: false });
      if (act === "install") doInstall(fake({ install: t.name }));
      else if (act === "uninstall") doUninstall(fake({ uninstall: t.name }));
      else if (act === "health") doHealth(fake({ health: t.name }));
      else if (act === "toggle") doToggle(fake({ state: t.name, enabled: String(t.enabled) }));
      else if (proc && ["start", "stop", "restart"].includes(act)) doProcAction(fake({ proc: proc.id, action: act }));
    }));
  }

  // ------------------------------------------------------------------- wire

  $("toolSearch").addEventListener("input", (e) => { search = e.target.value; renderGrid(); });
  $("toolSort").addEventListener("change", (e) => { sortBy = e.target.value; renderGrid(); });
  $("checkUpdates").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    try {
      let r = await post("/api/tools/check-updates", {});
      if (r.needs_approval) {
        if (!confirm(`Update checks query package sources and GitHub — allow ${r.permission}?`)) return;
        r = await post("/api/tools/check-updates", { approve: true });
      }
      if (r.error) { b.title = r.error; return; }
      b.textContent = "Checking…";
      setTimeout(() => { loadJobs(); loadTools(); b.textContent = "Check updates"; b.disabled = false; }, 6000);
    } catch (err) {
      b.title = err.message;
      b.disabled = false;
    }
  });
  $("installQueueHead").addEventListener("click", () => $("installQueue").classList.toggle("collapsed"));
  $("detailClose").addEventListener("click", () => {
    $("toolDetail").classList.add("hidden"); selectedTool = "";
    document.querySelectorAll(".tcard.selected").forEach((c) => c.classList.remove("selected"));
  });
  $("detailTabs").addEventListener("click", (e) => {
    if (e.target.dataset.tab) { detailTab = e.target.dataset.tab; detailLog = detailTab === "logs" ? detailLog : null; renderDetail(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("toolDetail").classList.add("hidden");
  });

  const queueForm = $("queueForm");
  if (queueForm) queueForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = $("queuePrompt").value.trim();
    if (!prompt) return;
    try {
      await post("/api/queue", { prompt, mode: $("queueMode").value });
      $("queuePrompt").value = "";
    } catch (err) { alert(err.message); }
    setTimeout(loadQueue, 300);
  });

  try {
    const es = new EventSource("/api/events");
    es.addEventListener("job", () => { loadJobs(); });
    es.addEventListener("process", () => { loadProcesses(); });
    es.addEventListener("image_job", () => { loadJobs(); loadImagePacks(); });
    es.addEventListener("tool", () => {});
    es.onerror = () => {};
  } catch { /* SSE optional */ }

  refreshAll().catch((e) => { $("toolGrid").textContent = `Failed to load tools: ${e.message}`; });
  setInterval(() => { loadJobs(); loadProcesses(); }, 15000);
})();
