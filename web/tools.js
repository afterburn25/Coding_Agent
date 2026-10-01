(() => {
  const $ = (id) => document.getElementById(id);
  let tools = [];
  let categories = [];
  let activeCategory = "";

  const api = async (path, options) => {
    const res = await fetch(path, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  };
  const post = (path, body) =>
    api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "—");
  const fmtUptime = (s) => (s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`);

  async function loadPermissions() {
    const data = await api("/api/permissions");
    const sel = $("permProfile");
    sel.innerHTML = data.available_profiles
      .map((p) => `<option value="${p}"${p === data.profile ? " selected" : ""}>${p.replace(/_/g, " ")}</option>`)
      .join("");
    const counts = {};
    for (const level of Object.values(data.permissions)) counts[level] = (counts[level] || 0) + 1;
    const autoEl = $("autonomousMode");
    if (autoEl) autoEl.checked = !!data.autonomous;
    $("permSummary").textContent =
      `Profile: ${data.profile} · ` +
      Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(" · ") +
      (data.autonomous ? " · AUTONOMOUS" : "") +
      (data.session_grants.length ? ` · session: ${data.session_grants.join(", ")}` : "");
    $("permTable").innerHTML = Object.entries(data.permissions)
      .map(
        ([key, level]) => `
        <div class="perm-row">
          <code>${esc(key)}</code>
          <select data-perm="${esc(key)}">
            ${data.levels.map((l) => `<option value="${l}"${l === level ? " selected" : ""}>${l}</option>`).join("")}
          </select>
        </div>`
      )
      .join("");
    $("permTable").classList.remove("muted");
    $("permTable").querySelectorAll("select[data-perm]").forEach((selEl) => {
      selEl.addEventListener("change", async () => {
        try {
          await post("/api/permissions/level", { permission: selEl.dataset.perm, level: selEl.value });
          await loadPermissions();
        } catch (e) {
          alert(e.message);
        }
      });
    });
  }

  async function loadTools() {
    const data = await api("/api/tools");
    tools = data.tools || [];
    categories = data.categories || [];
    renderCategories();
    renderTools();
  }

  function renderCategories() {
    const used = [...new Set(tools.map((t) => t.category))].sort();
    const list = ["", ...used];
    $("categoryList").innerHTML = list
      .map(
        (c) => `
        <div class="category-item${c === activeCategory ? " active" : ""}" data-cat="${esc(c)}">
          ${c ? esc(c.replace(/_/g, " ")) : "All tools"}
          <span>${c ? tools.filter((t) => t.category === c).length : tools.length}</span>
        </div>`
      )
      .join("");
    $("categoryList").querySelectorAll(".category-item").forEach((el) => {
      el.addEventListener("click", () => {
        activeCategory = el.dataset.cat;
        renderCategories();
        renderTools();
      });
    });
  }

  function renderTools() {
    const shown = activeCategory ? tools.filter((t) => t.category === activeCategory) : tools;
    if (!shown.length) {
      $("toolList").textContent = "No tools in this category.";
      return;
    }
    const groups = {};
    for (const t of shown) (groups[t.category] ||= []).push(t);
    $("toolList").innerHTML = Object.entries(groups)
      .map(
        ([cat, items]) => `
        <div class="tool-group">
          <div class="tool-group-title">${esc(cat.replace(/_/g, " "))}</div>
          ${items.map(renderTool).join("")}
        </div>`
      )
      .join("");
    $("toolList").classList.remove("muted");
    $("toolList").querySelectorAll(".tool-toggle").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await post("/api/tools/state", { tool: btn.dataset.tool, enabled: btn.dataset.enabled !== "true" });
          await loadTools();
        } catch (e) {
          alert(e.message);
        }
      });
    });
    $("toolList").querySelectorAll("[data-install]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          let res = await post("/api/tools/install", { tool: btn.dataset.install });
          if (res.needs_approval) {
            if (!confirm(`Install ${btn.dataset.install} via ${res.install.method || "package manager"}?`)) return;
            res = await post("/api/tools/install", { tool: btn.dataset.install, approve: true });
          }
          if (!res.ok) { alert(res.error || "install not available"); return; }
          btn.textContent = "Installing…";
          setTimeout(() => { loadTools(); loadJobs(); }, 2000);
        } catch (e) {
          alert(e.message);
        } finally {
          btn.disabled = false;
        }
      });
    });
    $("toolList").querySelectorAll("[data-health]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          const res = await api(`/api/tools/health/${encodeURIComponent(btn.dataset.health)}`);
          const h = res.health || {};
          btn.textContent = h.status || (h.ok ? "healthy" : "unhealthy");
          btn.title = h.detail || "";
        } catch (e) {
          btn.textContent = "error";
          btn.title = e.message;
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  function renderTool(t) {
    const chips = [
      `<span class="chip perm ${esc(t.permission_mode)}">${esc(t.permission)}: ${esc(t.permission_mode)}</span>`,
      t.requires_network ? '<span class="chip net">network</span>' : "",
      t.requires_gpu ? '<span class="chip gpu">GPU</span>' : "",
      ...t.capabilities.slice(0, 4).map((c) => `<span class="chip cap">${esc(c)}</span>`),
    ].join("");
    return `
      <div class="tool-card${t.enabled ? "" : " disabled"}">
        <div>
          <div class="tool-name">${esc(t.display_name)} <span class="tool-id">${esc(t.id)} · v${esc(t.version)} · ${esc(t.provider)}</span></div>
          <div class="tool-desc">${esc(t.description)}</div>
          <div class="tool-meta">${chips}${t.use_count ? `<span class="chip">used ${t.use_count}×</span>` : ""}</div>
        </div>
        <div class="tool-actions">
          <button class="tool-toggle ${t.enabled ? "on" : "off"}" data-tool="${esc(t.name)}" data-enabled="${t.enabled}">${t.enabled ? "Enabled" : "Disabled"}</button>
          ${t.install_status === "missing" && t.install && t.install.package ? `<button class="mini-button" data-install="${esc(t.name)}" title="via ${esc(t.install.method)}">Install</button>` : ""}
          ${t.has_health_check ? `<button class="mini-button" data-health="${esc(t.name)}">Check</button>` : '<span class="tool-health">no health check</span>'}
        </div>
      </div>`;
  }

  async function loadProcesses() {
    const data = await api("/api/processes");
    const rows = data.processes || [];
    if (!rows.length) {
      $("processList").textContent = "No managed services registered.";
      return;
    }
    $("processList").innerHTML = rows
      .map(
        (p) => `
        <div class="proc-row">
          <span class="name" title="${esc(p.id)}">${esc(p.name)}</span>
          <span>${esc(p.kind)}</span>
          <span class="state ${esc(p.state)}">${esc(p.state)}</span>
          <span>${p.pid ? `pid ${p.pid}` : "—"}</span>
          <span class="muted">${p.port || "—"}${p.uptime_seconds ? ` · ${fmtUptime(p.uptime_seconds)}` : ""}${p.error ? ` · ${esc(p.error.slice(0, 60))}` : ""}</span>
          <span class="row-actions">
            ${p.can_start ? `<button data-proc="${esc(p.id)}" data-action="start">Start</button>` : ""}
            ${p.can_stop ? `<button data-proc="${esc(p.id)}" data-action="stop">Stop</button>` : ""}
            ${p.can_restart ? `<button data-proc="${esc(p.id)}" data-action="restart">Restart</button>` : ""}
          </span>
        </div>`
      )
      .join("");
    $("processList").classList.remove("muted");
    $("processList").querySelectorAll("[data-proc]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await post("/api/processes/action", { id: btn.dataset.proc, action: btn.dataset.action });
        } catch (e) {
          alert(e.message);
        } finally {
          btn.disabled = false;
          setTimeout(loadProcesses, 800);
        }
      });
    });
  }

  async function loadMcp() {
    const data = await api("/api/mcp");
    const rows = data.servers || [];
    const box = $("mcpList");
    if (!rows.length) {
      box.textContent = "No MCP servers configured. Add mcp_servers entries to config.json.";
      return;
    }
    box.innerHTML = rows
      .map(
        (s) => `
        <div class="mcp-row">
          <div class="mcp-head"><span class="name">${esc(s.name || s.id)}</span><span class="state ${esc(s.state)}">${esc(s.state)}</span></div>
          <div class="muted small">${s.tools} tool(s)${s.pid ? ` · pid ${s.pid}` : ""}${s.error ? ` · ${esc(String(s.error).slice(0, 60))}` : ""}</div>
          <div class="mcp-actions">
            ${s.state !== "connected" ? `<button data-mcp="${esc(s.id)}" data-action="connect">Connect</button>` : ""}
            ${s.state === "connected" ? `<button data-mcp="${esc(s.id)}" data-action="disconnect">Disconnect</button>` : ""}
            <button data-mcp="${esc(s.id)}" data-action="restart">Restart</button>
          </div>
        </div>`
      )
      .join("");
    box.classList.remove("muted");
    box.querySelectorAll("[data-mcp]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await post("/api/mcp/action", { id: btn.dataset.mcp, action: btn.dataset.action });
        } catch (e) {
          alert(e.message);
        } finally {
          btn.disabled = false;
          setTimeout(loadMcp, 800);
        }
      });
    });
  }

  async function loadJobs() {
    const data = await api("/api/jobs");
    const rows = (data.jobs || []).slice(0, 60);
    if (!rows.length) {
      $("jobList").textContent = "No jobs yet.";
      return;
    }
    $("jobList").innerHTML = rows
      .map(
        (j) => `
        <div class="job-row">
          <span>${esc(j.kind)}</span>
          <span class="name" title="${esc(j.detail || j.error || "")}">${esc(j.title || j.id)}</span>
          <span class="state ${esc(j.state)}">${esc(j.state)}</span>
          <span>${Math.round((j.progress || 0) * 100)}%</span>
          <span class="muted">${fmtTime(j.created_at)}${j.error ? ` · ${esc(j.error.slice(0, 50))}` : ""}</span>
          <span class="row-actions">${j.cancellable ? `<button data-job="${esc(j.id)}">Cancel</button>` : ""}</span>
        </div>`
      )
      .join("");
    $("jobList").classList.remove("muted");
    $("jobList").querySelectorAll("[data-job]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await post("/api/jobs/cancel", { job_id: btn.dataset.job });
        } catch (e) {
          alert(e.message);
        } finally {
          setTimeout(loadJobs, 600);
        }
      });
    });
  }

  async function loadQueue() {
    const data = await api("/api/queue");
    const rows = (data.items || []).slice(0, 60);
    const host = $("queueList");
    if (!rows.length) {
      host.textContent = "Queue is empty — prompts sent while a task runs land here.";
      host.classList.add("muted");
      return;
    }
    host.innerHTML = rows
      .map(
        (q, i) => `
        <div class="job-row">
          <span class="muted">#${i + 1}</span>
          <span class="name" title="${esc(q.prompt || "")}">${esc((q.prompt || "").slice(0, 90)) || "(empty)"}</span>
          <span class="state queued">${esc(q.status || "queued")}</span>
          <span>${esc(q.mode || "auto")}</span>
          <span class="muted">${fmtTime(q.enqueued_at)}</span>
          <span class="row-actions"><button data-queue="${esc(q.id)}">Cancel</button></span>
        </div>`
      )
      .join("");
    host.classList.remove("muted");
    host.querySelectorAll("[data-queue]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await post("/api/queue/cancel", { id: btn.dataset.queue });
        } catch (e) {
          alert(e.message);
        } finally {
          setTimeout(loadQueue, 400);
        }
      });
    });
  }

  async function loadTelemetry() {
    const data = await api("/api/tools/telemetry");
    const stats = (data.stats && data.stats.routes) || [];
    const recent = (data.routing || []).slice(-15).reverse();
    const host = $("telemetryList");
    if (!stats.length && !recent.length) {
      host.textContent = "No routing decisions yet — tools chosen via use_capability/run_workflow appear here.";
      return;
    }
    const statsHtml = stats.length ? `
      <div class="telemetry-sub">Learned routing (feeds tool ranking)</div>
      ${stats.map((r) => `
        <div class="telemetry-row">
          <span class="name">${esc(r.capability)}</span>
          <span>${esc(r.tool || "—")}</span>
          <span class="muted">${r.calls} calls</span>
          <span class="${r.success_rate >= 0.8 ? "ok" : "warn"}">${Math.round(r.success_rate * 100)}%</span>
          <span class="muted">${r.avg_ms}ms avg</span>
        </div>`).join("")}` : "";
    const recentHtml = recent.length ? `
      <div class="telemetry-sub">Recent decisions</div>
      ${recent.map((e) => `
        <div class="telemetry-row">
          <span class="name">${esc(e.capability)}</span>
          <span>${esc(e.chosen || "—")}</span>
          <span class="${e.ok ? "ok" : "warn"}">${e.ok ? "ok" : "failed"}</span>
          <span class="muted">${Math.round(e.elapsed_ms || 0)}ms · ${fmtTime(e.ts)}</span>
        </div>`).join("")}` : "";
    host.innerHTML = statsHtml + recentHtml;
    host.classList.remove("muted");
  }

  async function loadWorkflows() {
    const data = await api("/api/workflows");
    const host = $("workflowList");
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
  }

  $("applyProfile").addEventListener("click", async () => {
    if (!confirm(`Apply the "${$("permProfile").value}" permission profile?`)) return;
    try {
      await post("/api/permissions/profile", { profile: $("permProfile").value });
      await loadPermissions();
      await loadTools();
    } catch (e) {
      alert(e.message);
    }
  });
  const autoToggle = $("autonomousMode");
  if (autoToggle) {
    autoToggle.addEventListener("change", async () => {
      const enabled = autoToggle.checked;
      if (enabled && !confirm(
        "Enable autonomous mode? The agent will run without asking for approvals on " +
        "reversible workspace actions (file edits, commands, git, installs). Spending " +
        "money, sending messages, microphone, and camera still require approval."
      )) {
        autoToggle.checked = false;
        return;
      }
      try {
        await post("/api/permissions/autonomous", { enabled });
        await loadPermissions();
      } catch (e) {
        autoToggle.checked = !enabled;
        alert(e.message);
      }
    });
  }
  $("refreshAll").addEventListener("click", refreshAll);

  async function refreshAll() {
    await Promise.all([loadPermissions(), loadTools(), loadProcesses(), loadJobs(), loadQueue(), loadMcp(), loadTelemetry(), loadWorkflows()]).catch((e) => alert(e.message));
  }
  refreshAll();
  setInterval(() => Promise.all([loadProcesses(), loadJobs(), loadQueue(), loadMcp(), loadTelemetry(), loadWorkflows()]).catch(() => {}), 5000);

  // Live updates: job/tool events stream over SSE; polling above stays as the
  // fallback if EventSource is unavailable or the connection drops.
  try {
    const events = new EventSource("/api/events");
    let refreshTimer = null;
    const scheduleRefresh = () => {
      if (refreshTimer) return;
      refreshTimer = setTimeout(() => {
        refreshTimer = null;
        Promise.all([loadJobs(), loadTools(), loadQueue()]).catch(() => {});
      }, 400);
    };
    events.addEventListener("job", scheduleRefresh);
    events.addEventListener("tool", scheduleRefresh);
    events.addEventListener("task", scheduleRefresh);
  } catch (e) { /* EventSource unsupported — interval polling still applies */ }
})();
