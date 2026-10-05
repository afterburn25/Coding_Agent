// Settings — secondary sections. `permissions` is the primary surface:
// authorization boundaries, profiles, per-key levels, scopes, and audit.
(async () => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = (p) => fetch(p).then((r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(new Error(e.error || r.status)))));
  const post = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }).then((r) => r.json().then((d) => (r.ok || d.needs_approval ? d : Promise.reject(new Error(d.error || r.status)))));
  const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");

  const SECTIONS = [
    ["profile", "Profile"], ["general", "General"],
    ["permissions", "Permissions"], ["models", "Models"],
    ["appearance", "Appearance"], ["privacy", "Privacy"],
    ["notifications", "Notifications"], ["setup", "Setup"],
    ["advanced", "Advanced"],
  ];
  let activeProfile = null;   // /api/profiles/active — Creator section
                              // is appended only for is_creator.
  const LEVEL_LABEL = { allow: "Allowed", session: "Session Only", ask: "Ask First", creator: "Creator Only", deny: "Denied" };
  const PROFILE_DESC = {
    safe: "Reads allowed; writes, shell, git, and network ask first.",
    research_only: "Research and reads allowed; writes/shell/git denied.",
    developer: "Coding workflow defaults; remote writes ask.",
    power_user: "Local writes/git allowed; shell and remote actions session-gated.",
    offline: "Developer baseline with network, browser, and GitHub denied.",
    custom: "Manual per-capability configuration.",
  };
  // Approval-rule buttons map onto real levels (no invented semantics).
  const APPROVAL_RULES = [
    ["ask", "Always Ask"], ["session", "Allow This Session"],
    ["allow", "Allow for Workspace"], ["creator", "Require Creator Approval"],
    ["deny", "Deny"],
  ];

  let section = (location.hash || "#permissions").slice(1);
  if (!SECTIONS.some(([id]) => id === section)) section = "permissions";
  let manager = null;      // /api/permissions summary
  let audit = [];          // /api/permissions/audit entries
  let status = null;       // /api/status
  let selected = "";       // selected permission key
  let permSearch = "";
  let permFilter = "";
  let catFilter = "";

  function renderNav() {
    $("settingsNav").innerHTML = SECTIONS
      .map(([id, label]) => `<button class="${id === section ? "active" : ""}" data-section="${id}">${esc(label)}</button>`)
      .join("");
    $("settingsNav").querySelectorAll("[data-section]").forEach((b) =>
      b.addEventListener("click", () => { section = b.dataset.section; location.hash = section; renderNav(); renderBody(); }));
  }

  async function refreshPermissions() {
    manager = await api("/api/permissions");
    // Audit endpoint is newer — tolerate backends that predate it.
    audit = await api("/api/permissions/audit").then((a) => a.entries || []).catch(() => []);
  }

  async function renderBody() {
    const host = $("settingsBody");
    try {
      if (section === "permissions") {
        if (!manager) await refreshPermissions();
        renderPermissions(host);
      } else if (section === "profile") {
        await renderProfile(host);
      } else if (section === "creator") {
        await renderCreator(host);
      } else if (section === "setup") {
        await renderSetup(host);
      } else {
        if (!status) status = await api("/api/status");
        renderPlain(host);
      }
    } catch (e) {
      host.innerHTML = `<div class="panel">Failed to load: ${esc(e.message)}</div>`;
    }
    host.classList.remove("muted");
  }

  // ------------------------------------------------------------- permissions

  function domLevel(counts) {
    // Worst-case label for a category: any deny -> Denied-ish, else ask-ish.
    if (counts.deny) return "deny";
    if (counts.creator) return "creator";
    if (counts.ask) return "ask";
    if (counts.session) return "session";
    return "allow";
  }

  function permRows() {
    const keys = Object.keys(manager.permissions);
    return keys
      .filter((k) => {
        const info = manager.info[k] || {};
        if (catFilter && info.category !== catFilter) return false;
        if (permFilter && manager.permissions[k] !== permFilter) return false;
        if (permSearch) {
          const q = permSearch.toLowerCase();
          if (!(k.toLowerCase().includes(q) || (info.label || "").toLowerCase().includes(q))) return false;
        }
        return true;
      })
      .sort((a, b) => {
        const ia = manager.info[a] || {}, ib = manager.info[b] || {};
        return (ia.category || "").localeCompare(ib.category || "") || (ia.label || a).localeCompare(ib.label || b);
      });
  }

  function renderPermissions(host) {
    const m = manager;
    host.innerHTML = `
      <div class="settings-title">
        <div>
          <h2>Permissions &amp; Safety</h2>
          <p>Control what Nexus Core can do on your system, what requires approval, and where access is allowed.</p>
        </div>
      </div>
      <div class="autonomy-bar">
        <label class="autonomy-toggle"><input type="checkbox" id="autonomyToggle" ${m.autonomous ? "checked" : ""}> Autonomous mode — auto-approves ask/session workspace actions</label>
        <span class="muted small">Hard gates always require you: ${esc((m.autonomy_hard_gates || []).join(", "))}</span>
      </div>
      <div>
        <h4 class="sec-h">Permission Profiles</h4>
        <div class="profile-row">${(m.available_profiles || []).map((p) => `
          <div class="profile-card ${p === m.profile ? "active" : ""}" data-profile="${esc(p)}">
            <div class="p-name">${esc(p.replace("_", " "))}</div>
            <div class="p-desc">${esc(PROFILE_DESC[p] || "")}</div>
            ${p === m.profile ? '<span class="p-tag">Active</span>' : ""}
          </div>`).join("")}
        </div>
      </div>
      <div>
        <h4 class="sec-h">Categories</h4>
        <div class="cat-grid">${(m.categories || []).map((c) => {
          const lvl = domLevel(c.counts);
          const label = c.counts.deny ? `${c.counts.deny} Denied`
            : c.counts.ask ? `${c.counts.ask} Ask First`
            : c.counts.creator ? "Creator approval"
            : c.counts.session ? "Session gated" : `${c.total} Allowed`;
          return `<div class="cat-card ${catFilter === c.category ? "selected" : ""}" data-cat="${esc(c.category)}">
            <div class="c-name">${esc(c.category)}</div>
            <div class="c-state ${lvl}">${esc(label)} · ${c.total} rule${c.total === 1 ? "" : "s"}</div>
          </div>`;
        }).join("")}
        </div>
      </div>
      <div>
        <h4 class="sec-h">Permission Matrix</h4>
        <div class="matrix-controls">
          <input type="search" id="permSearch" placeholder="Search capabilities..." value="${esc(permSearch)}">
          <select id="permFilter">
            <option value="">All statuses</option>
            ${(m.levels || []).map((l) => `<option value="${l}" ${permFilter === l ? "selected" : ""}>${esc(LEVEL_LABEL[l] || l)}</option>`).join("")}
          </select>
          ${catFilter ? `<button class="mini-button" id="clearCat">Clear: ${esc(catFilter)}</button>` : ""}
        </div>
        <div class="perm-matrix"><table>
          <thead><tr><th>Capability</th><th>Status</th><th>Scope</th><th>Approval</th><th>Last Used</th></tr></thead>
          <tbody>${permRows().map((k) => {
            const info = m.info[k] || {};
            const lvl = m.permissions[k];
            const scope = (m.scopes || {})[k] || {};
            const scopeText = scope.allowed_domains ? `${scope.allowed_domains.length} domain(s)` :
              scope.allowed_dirs ? `${scope.allowed_dirs.length} dir(s)` :
              scope.workspace_only ? "Workspace only" : info.scope || "Global";
            return `<tr data-perm="${esc(k)}" class="${selected === k ? "selected" : ""}">
              <td class="perm-cap"><div class="cap-label">${esc(info.label || k)}</div><div class="cap-key">${esc(k)}</div></td>
              <td><span class="badge ${lvl}">${esc(LEVEL_LABEL[lvl] || lvl)}</span></td>
              <td>${esc(scopeText)}</td>
              <td><select class="perm-level-select" data-level="${esc(k)}">
                ${(m.levels || []).map((l) => `<option value="${l}" ${l === lvl ? "selected" : ""}>${esc(LEVEL_LABEL[l] || l)}</option>`).join("")}
              </select></td>
              <td>${esc(m.last_used && m.last_used[k] ? fmtTime(m.last_used[k]) : "never")}</td>
            </tr>`;
          }).join("")}</tbody>
        </table></div>
      </div>
      <div class="audit-log">
        <h4>Recent Activity</h4>
        <div id="auditList">${renderAuditRows(audit.slice(0, 30))}</div>
      </div>`;
    wirePermissions(host);
    renderDetail();
  }

  function renderAuditRows(rows) {
    if (!rows.length) return '<div class="muted small">No permission events recorded yet.</div>';
    return rows.map((e) => `
      <div class="audit-row"><time>${esc(new Date(e.ts * 1000).toLocaleTimeString())}</time>
        <b>${esc(e.event)}</b>
        ${e.permission ? `<span class="a-perm">${esc(e.permission)}</span>` : ""}
        ${e.detail ? `<span class="muted">${esc(e.detail)}</span>` : ""}
      </div>`).join("");
  }

  function wirePermissions(host) {
    const toggle = host.querySelector("#autonomyToggle");
    if (toggle) toggle.addEventListener("change", async () => {
      try { const r = await post("/api/permissions/autonomous", { enabled: toggle.checked }); manager = r.manager; renderPermissions(host); }
      catch (e) { alert(e.message); toggle.checked = !toggle.checked; }
    });
    host.querySelectorAll("[data-profile]").forEach((c) => c.addEventListener("click", async () => {
      const p = c.dataset.profile;
      if (p === "custom" || p === manager.profile) return;
      if (!confirm(`Apply the '${p}' permission profile? This rewrites the current levels.`)) return;
      try { const r = await post("/api/permissions/profile", { profile: p }); manager = r.manager; renderPermissions(host); }
      catch (e) { alert(e.message); }
    }));
    host.querySelectorAll("[data-cat]").forEach((c) => c.addEventListener("click", () => {
      catFilter = catFilter === c.dataset.cat ? "" : c.dataset.cat;
      renderPermissions(host);
    }));
    const search = host.querySelector("#permSearch");
    if (search) search.addEventListener("input", () => { permSearch = search.value; renderMatrixOnly(host); });
    const filter = host.querySelector("#permFilter");
    if (filter) filter.addEventListener("change", () => { permFilter = filter.value; renderPermissions(host); });
    const clear = host.querySelector("#clearCat");
    if (clear) clear.addEventListener("click", () => { catFilter = ""; renderPermissions(host); });
    wireMatrix(host);
  }

  // Re-render only the table body for cheap search typing.
  function renderMatrixOnly(host) {
    const tbody = host.querySelector(".perm-matrix tbody");
    if (!tbody) { renderPermissions(host); return; }
    tbody.innerHTML = permRows().map((k) => {
      const info = manager.info[k] || {};
      const lvl = manager.permissions[k];
      const scope = (manager.scopes || {})[k] || {};
      const scopeText = scope.allowed_domains ? `${scope.allowed_domains.length} domain(s)` :
        scope.allowed_dirs ? `${scope.allowed_dirs.length} dir(s)` :
        scope.workspace_only ? "Workspace only" : info.scope || "Global";
      return `<tr data-perm="${esc(k)}" class="${selected === k ? "selected" : ""}">
        <td class="perm-cap"><div class="cap-label">${esc(info.label || k)}</div><div class="cap-key">${esc(k)}</div></td>
        <td><span class="badge ${lvl}">${esc(LEVEL_LABEL[lvl] || lvl)}</span></td>
        <td>${esc(scopeText)}</td>
        <td><select class="perm-level-select" data-level="${esc(k)}">
          ${(manager.levels || []).map((l) => `<option value="${l}" ${l === lvl ? "selected" : ""}>${esc(LEVEL_LABEL[l] || l)}</option>`).join("")}
        </select></td>
        <td>${esc(manager.last_used && manager.last_used[k] ? fmtTime(manager.last_used[k]) : "never")}</td>
      </tr>`;
    }).join("");
    wireMatrix(host);
  }

  function wireMatrix(host) {
    host.querySelectorAll("tr[data-perm]").forEach((tr) =>
      tr.addEventListener("click", (ev) => {
        if (ev.target.tagName === "SELECT") return;
        selected = tr.dataset.perm;
        host.querySelectorAll("tr[data-perm]").forEach((r) => r.classList.toggle("selected", r.dataset.perm === selected));
        renderDetail();
      }));
    host.querySelectorAll("[data-level]").forEach((sel) =>
      sel.addEventListener("change", async () => {
        sel.disabled = true;
        try {
          const r = await post("/api/permissions/level", { permission: sel.dataset.level, level: sel.value });
          manager = r.manager;
          renderPermissions($("settingsBody"));
        } catch (e) { alert(e.message); sel.disabled = false; }
      }));
  }

  // ----------------------------------------------------------- detail panel

  function renderDetail() {
    let panel = $("permDetail");
    if (!selected || !manager) { if (panel) panel.classList.add("hidden"); return; }
    const m = manager;
    const info = m.info[selected] || {};
    const lvl = m.permissions[selected];
    const scope = (m.scopes || {})[selected] || {};
    const keyAudit = audit.filter((e) => e.permission === selected).slice(0, 20);
    if (!panel) {
      panel = document.createElement("div");
      panel.id = "permDetail";
      panel.className = "perm-detail";
      document.body.appendChild(panel);
    }
    panel.classList.remove("hidden");
    const hasDomainScope = ["network.read", "external_api.call", "browser.control", "browser.submit"].includes(selected);
    const hasDirScope = selected.startsWith("filesystem.");
    const hasRepoScope = selected.startsWith("github.") || selected.startsWith("git.");
    panel.innerHTML = `
      <div class="perm-detail-head"><h3>${esc(info.label || selected)}</h3>
        <button class="perm-detail-close" id="permClose">✕</button></div>
      <div class="perm-detail-body">
        <div class="pd-row"><span class="k">What it controls</span><span class="v">${esc(info.blurb || "")}</span></div>
        <div class="pd-row"><span class="k">Permission key</span><span class="v" style="font-family:Consolas,monospace">${esc(selected)}</span></div>
        <div class="pd-row"><span class="k">Current state</span><span class="v"><span class="badge ${lvl}">${esc(LEVEL_LABEL[lvl] || lvl)}</span></span></div>
        <div class="pd-row"><span class="k">Category / risk</span><span class="v">${esc(info.category || "Other")} · ${esc(info.risk || "medium")} risk</span></div>
        <div class="pd-row"><span class="k">Scope</span><span class="v">${esc(scope.workspace_only ? "Workspace only" : info.scope || "Global")}</span></div>
        <div class="pd-row"><span class="k">Associated tools</span>
          <div class="pd-tools">${(info.tools || []).length ? info.tools.map((t) => `<span class="chip">${esc(t)}</span>`).join("") : '<span class="muted">no tools mapped</span>'}</div></div>
        <div class="pd-row"><span class="k">Approval rules</span>
          <div class="approval-rules">${APPROVAL_RULES.map(([lv, label]) =>
            `<button class="${lv === lvl ? "current" : ""} ${lv === "deny" ? "danger-btn" : ""}" data-rule="${lv}">${esc(label)}</button>`).join("")}
          </div></div>
        ${hasDomainScope ? `
        <div class="pd-row"><span class="k">Domain scope</span>
          <div class="scope-edit">
            <label>Allowed domains (one per line — empty = all)</label>
            <textarea id="scopeAllowed" rows="3">${esc((scope.allowed_domains || []).join("\n"))}</textarea>
            <label>Blocked domains</label>
            <textarea id="scopeBlocked" rows="2">${esc((scope.blocked_domains || []).join("\n"))}</textarea>
            <span class="hint">Applies to URLs passed to tools gated by this permission.</span>
            <button class="scope-save" id="scopeSave">Save scope</button>
          </div></div>` : ""}
        ${hasDirScope ? `
        <div class="pd-row"><span class="k">Directory scope</span>
          <div class="scope-edit">
            <label>Allowed directories (one per line — empty = workspace)</label>
            <textarea id="scopeDirs" rows="3">${esc((scope.allowed_dirs || []).join("\n"))}</textarea>
            <button class="scope-save" id="scopeSave">Save scope</button>
          </div></div>` : ""}
        ${hasRepoScope ? `
        <div class="pd-row"><span class="k">Repository scope</span>
          <div class="scope-edit">
            <label>Approved repositories (one per line)</label>
            <textarea id="scopeRepos" rows="3">${esc((scope.allowed_repos || []).join("\n"))}</textarea>
            <button class="scope-save" id="scopeSave">Save scope</button>
          </div></div>` : ""}
        <div class="pd-row"><span class="k">Last used</span><span class="v">${esc(m.last_used && m.last_used[selected] ? fmtTime(m.last_used[selected]) : "never")}</span></div>
        <div class="pd-row"><span class="k">Recent activity</span>
          <div class="pd-audit">${renderAuditRows(keyAudit)}</div></div>
      </div>`;
    panel.querySelector("#permClose").addEventListener("click", () => {
      panel.classList.add("hidden"); selected = "";
      document.querySelectorAll("tr[data-perm]").forEach((r) => r.classList.remove("selected"));
    });
    panel.querySelectorAll("[data-rule]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        const r = await post("/api/permissions/level", { permission: selected, level: b.dataset.rule });
        manager = r.manager;
        renderPermissions($("settingsBody"));
      } catch (e) { alert(e.message); b.disabled = false; }
    }));
    const save = panel.querySelector("#scopeSave");
    if (save) save.addEventListener("click", async () => {
      const lines = (id) => (panel.querySelector(id)?.value || "").split("\n").map((s) => s.trim()).filter(Boolean);
      const scopeBody = {};
      if (panel.querySelector("#scopeAllowed")) { scopeBody.allowed_domains = lines("#scopeAllowed"); scopeBody.blocked_domains = lines("#scopeBlocked"); }
      if (panel.querySelector("#scopeDirs")) scopeBody.allowed_dirs = lines("#scopeDirs");
      if (panel.querySelector("#scopeRepos")) scopeBody.allowed_repos = lines("#scopeRepos");
      save.disabled = true;
      try {
        await post("/api/permissions/scope", { permission: selected, scope: scopeBody });
        await refreshPermissions();
        renderPermissions($("settingsBody"));
      } catch (e) { alert(e.message); save.disabled = false; }
    });
  }

  // --------------------------------------------------------- plain sections

  async function renderSetup(host) {
    const pv = await api("/api/provisioning").catch(() => ({}));
    const items = pv.items || [];
    const gb = (b) => (b / 1073741824).toFixed(1);
    const toggle = (id, key, on) =>
      `<label class="autonomy-toggle"><input type="checkbox" data-prov="${key}" ${on ? "checked" : ""}> ${id}</label>`;
    const cfg = pv.config || {};
    host.innerHTML =
      `<div class="settings-title"><div><h2>Background setup</h2>` +
      `<p>Nexus finishes installing models, image backends, voice, and tools in the background after the app itself is ready. Failed items can be retried from the Command Center.</p></div></div>` +
      `<div class="settings-section"><div class="kv">` +
      `<div class="row"><span class="k">Status</span><span class="v">${pv.complete ? "complete" : `${pv.completed || 0} of ${pv.total || 0} ready`}${pv.paused ? " · paused" : ""}</span></div>` +
      `<div class="row"><span class="k">Remaining download</span><span class="v">~${gb(pv.remaining_download_bytes || 0)} GB</span></div>` +
      `<div class="row"><span class="k">Free disk</span><span class="v">${gb(pv.free_disk_bytes || 0)} GB</span></div>` +
      `</div></div>` +
      `<div class="settings-section">` +
      toggle("Background setup", "provisioning_enabled", cfg.provisioning_enabled !== false) +
      toggle("Automatic retry on transient failures", "provisioning_auto_retry", cfg.provisioning_auto_retry !== false) +
      toggle("Voice setup notifications", "provisioning_voice_notifications", cfg.provisioning_voice_notifications !== false) +
      `</div>` +
      `<div class="settings-section"><div class="section-title">Components</div><div class="kv">` +
      (items.map((it) =>
        `<div class="row"><span class="k">${esc(it.label)}</span><span class="v">${esc(it.state)}${it.error_code ? ` · ${esc(it.error_code)}` : ""}</span></div>`).join("") ||
        `<div class="row"><span class="v muted">Nothing to set up.</span></div>`) +
      `</div></div>` +
      `<p class="muted small">Live progress and per-item retry/cancel live on the <a href="/command.html" style="color:var(--cyan)">Command Center →</a></p>`;
    host.querySelectorAll("[data-prov]").forEach((el) =>
      el.addEventListener("change", async () => {
        try {
          await post("/api/provisioning/config", { [el.dataset.prov]: el.checked });
        } catch (e) { alert(e.message); el.checked = !el.checked; }
      }));
  }

  function renderPlain(host) {
    const s = status || {};
    const kv = (rows) => `<div class="settings-section"><div class="kv">${rows
      .map(([k, v]) => `<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join("")}</div></div>`;
    const bodies = {
      general: `<div class="settings-title"><div><h2>General</h2><p>Core application state.</p></div></div>` + kv([
        ["Version", s.version || "—"], ["Workspace", s.workspace || "—"],
        ["Policy mode", s.policy_mode || "—"],
        ["Models configured", String((s.models || []).length)],
      ]),
      models: `<div class="settings-title"><div><h2>Models</h2><p>Model profiles are managed on the Models page.</p></div></div>` + kv(
        (s.models || []).map((m) => [m.id, `${(m.roles || []).join(", ")} · ${m.runtime || "—"}${m.enabled ? "" : " · disabled"}`])
      ) + `<p class="muted small"><a href="/models.html" style="color:var(--cyan)">Open Models page →</a></p>`,
      appearance: `<div class="settings-title"><div><h2>Appearance</h2><p>Nexus Core uses its fixed dark sci-fi workstation theme. No appearance toggles are exposed yet.</p></div></div>`,
      privacy: `<div class="settings-title"><div><h2>Privacy</h2><p>Local data locations and memory state.</p></div></div>` + kv([
        ["Workspace", s.workspace || "—"],
        ["Nexus Brain records", s.nexus_brain ? String(s.nexus_brain.records ?? "—") : "—"],
        ["Nexus Brain", s.nexus_brain ? (s.nexus_brain.initialized ? "initialized" : "not initialized") : "—"],
        ["Repository index", s.repository_index ? `${s.repository_index.files ?? "—"} files` : "—"],
      ]) + `<p class="muted small">Permissions and the decision audit live under <a href="#permissions" style="color:var(--cyan)">Settings → Permissions</a>.</p>`,
      notifications: `<div class="settings-title"><div><h2>Notifications</h2><p>Nexus Core currently has no notification channel settings.</p></div></div>`,
      advanced: `<div class="settings-title"><div><h2>Advanced</h2><p>Runtime and autonomy tuning live in config.json.</p></div></div>` + kv([
        ["Workspace", s.workspace || "—"],
        ["Version", s.version || "—"],
      ]) + `<p class="muted small">Autonomy bounds: <code>autonomous_max_continuations</code>, <code>autonomous_approval_timeout_seconds</code>, <code>agent_tool_timeout_seconds</code>, memory pressure thresholds — see PERMISSIONS.md.</p>`,
    };
    host.innerHTML = bodies[section] || bodies.general;
  }

  // ------------------------------------------------------------- profile

  const ro = (label, val) =>
    `<label class="field-locked">${esc(label)}
       <input value="${esc(val)}" disabled tabindex="-1" /></label>`;

  async function renderProfile(host) {
    const ob = await api("/api/onboarding/status");
    if (!status) status = await api("/api/status").catch(() => null);
    const prefProjects = await api("/api/projects")
      .then((r) => r.projects || []).catch(() => []);
    const p = (ob.profiles || []).find((x) => x.profile_id === ob.active) || null;
    if (!p) {
      host.innerHTML = `<div class="settings-title"><div><h2>Profile</h2><p>No profile exists yet.</p></div></div>
        <div class="settings-section"><a class="mini-button" href="/start.html">Start Here →</a></div>`;
      return;
    }
    const avUrl = p.avatar_path
      ? `/api/profiles/${encodeURIComponent(p.profile_id)}/avatar` : "";
    const workspaceScope = `project:${(status && status.workspace) || ""}`;
    const scopeOptions = [
      [`profile:${p.profile_id}`, "This profile"],
      ["global", "Global"],
      ...((status && status.workspace)
          ? [[workspaceScope, "This workspace"]] : []),
      ...(prefProjects || []).map((proj) =>
        [`project:${proj.id || proj.project_id}`, `Project: ${proj.name || proj.id || proj.project_id}`]),
    ];
    const scopeLabel = (scope) => {
      const hit = scopeOptions.find(([value]) => value === scope);
      if (hit) return hit[1];
      if (String(scope).startsWith("profile:")) return "Another profile";
      if (String(scope).startsWith("project:")) return "Project scope";
      return scope || "global";
    };
    host.innerHTML = `
      <div class="settings-title"><div><h2>Profile</h2>
        <p>Identity is locked at creation. Contact and location stay editable.</p></div></div>
      <div class="settings-section prof-identity">
        <div class="prof-avatar-col">
          ${avUrl
            ? `<img class="avatar-preview" src="${esc(avUrl)}" alt="avatar" />`
            : `<div class="avatar-preview ps-initials" style="display:flex;align-items:center;justify-content:center;font-size:28px">${esc((p.first_name || "?")[0])}</div>`}
          <input id="profAvatarFile" type="file" accept="image/*" hidden />
          <button id="profAvatarBtn" class="mini-button" type="button">Change Avatar</button>
          <canvas id="profAvatarCanvas" width="160" height="160" class="crop-canvas" hidden></canvas>
          <input id="profAvatarZoom" type="range" min="100" max="400" value="100" hidden />
          <button id="profAvatarSave" class="mini-button" type="button" hidden>Save Avatar</button>
        </div>
        <div class="prof-fields">
          ${ro("First Name", p.first_name || "")}
          ${ro("Last Name", p.last_name || "")}
          ${ro("Sex", p.sex || "")}
          ${ro("Birthdate", p.birth_date || "")}
          ${ro("Age", p.age != null ? `${p.age}${p.is_adult ? " (18+)" : ""}` : "—")}
          ${p.is_creator ? `<div class="pd-row"><span class="k">Role</span><span class="v"><span class="badge allow">Nexus Creator</span></span></div>` : ""}
        </div>
      </div>
      <div class="settings-section">
        <h3>Contact &amp; Location</h3>
        <div class="prof-fields">
          <label>Email <input id="pfEmail" value="${esc(p.email || "")}" /></label>
          <label>Phone <input id="pfPhone" value="${esc(p.phone || "")}" /></label>
          <label>Street <input id="pfStreet" value="${esc(p.street_address || "")}" /></label>
          <label>City <input id="pfCity" value="${esc(p.city || "")}" /></label>
          <label>State <input id="pfState" maxlength="2" value="${esc(p.state || "")}" /></label>
          <label>ZIP <input id="pfZip" maxlength="5" inputmode="numeric" value="${esc(p.zip_code || "")}" /></label>
        </div>
        <button id="pfSave" class="mini-button" type="button" style="margin-top:10px">Save Changes</button>
        <span id="pfMsg" class="muted small" style="margin-left:10px"></span>
      </div>
      <div class="settings-section">
        <h3>Personal Memory</h3>
        <p class="muted small">Private to this profile — Nexus uses these in
        conversation; other profiles can't see them.</p>
        <div id="pmList" class="pm-list"></div>
        <div style="display:flex;gap:8px;margin-top:8px">
          <input id="pmText" placeholder="Remember something about you…"
                 style="flex:1" maxlength="4000" />
          <button id="pmAdd" class="mini-button" type="button">Remember</button>
        </div>
      </div>
      <div class="settings-section">
        <h3>Learned Preferences</h3>
        <p class="muted small">Explicit corrections become candidates first;
        repeated or user-activated rules become active. They never override
        identity, permissions, safety, or factual correctness.</p>
        <div id="prefList" class="pm-list"></div>
        <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
          <input id="prefText" placeholder="Add a behavior rule…"
                 style="flex:1" maxlength="300" />
          <select id="prefScope">${scopeOptions.map(([value, label]) =>
            `<option value="${esc(value)}">${esc(label)}</option>`).join("")}</select>
          <button id="prefAdd" class="mini-button" type="button">Add</button>
        </div>
      </div>
      <div class="settings-section">
        <h3>All Profiles</h3>
        ${(ob.profiles || []).map((x) => `
          <div class="custom-item ${x.profile_id === ob.active ? "active" : ""}">
            <span class="ci-name">${esc(x.display_name || x.first_name)}${x.is_creator ? ' <em class="ps-creator">Creator</em>' : ""}</span>
            ${x.profile_id !== ob.active ? `<button class="mini-button" data-switch="${esc(x.profile_id)}" type="button">Switch</button>` : '<span class="muted small">active</span>'}
          </div>`).join("")}
        <div class="pst-actions" style="margin-top:10px">
          <a class="mini-button" href="/start.html">Create Profile</a>
          <a class="mini-button" href="/personality.html">Personality Studio</a>
        </div>
      </div>`;

    host.querySelector("#pfSave").addEventListener("click", async () => {
      const msg = host.querySelector("#pfMsg");
      try {
        await post(`/api/profiles/${encodeURIComponent(p.profile_id)}/patch`, {
          email: $("pfEmail").value.trim(), phone: $("pfPhone").value.trim(),
          street_address: $("pfStreet").value.trim(), city: $("pfCity").value.trim(),
          state: $("pfState").value.trim().toUpperCase(), zip_code: $("pfZip").value.trim(),
        });
        msg.textContent = "Saved.";
      } catch (e) { msg.textContent = e.message; }
    });
    host.querySelectorAll("[data-switch]").forEach((b) =>
      b.addEventListener("click", async () => {
        try { await post("/api/profiles/switch", { profile_id: b.dataset.switch }); location.reload(); }
        catch (e) { alert(e.message); }
      }));

    // --- personal memory (profile-isolated) ---
    const pmBase = `/api/profiles/${encodeURIComponent(p.profile_id)}/memory`;
    const loadMem = async () => {
      const list = host.querySelector("#pmList");
      try {
        const r = await api(pmBase);
        list.innerHTML = (r.memories || []).map((m) => `
          <div class="pm-item" data-mid="${esc(m.id)}">
            <span class="pm-text">${esc(m.text)}</span>
            <button class="mini-button pm-forget" type="button">Forget</button>
          </div>`).join("")
          || '<span class="muted small">Nothing remembered yet.</span>';
      } catch { list.innerHTML = '<span class="muted small">Unavailable.</span>'; }
    };
    host.querySelector("#pmAdd").addEventListener("click", async () => {
      const t = $("pmText").value.trim();
      if (!t) return;
      try {
        await post(pmBase, { text: t });
        $("pmText").value = ""; loadMem();
      } catch (e) { alert(e.message); }
    });
    host.querySelector("#pmList").addEventListener("click", async (e) => {
      const btn = e.target.closest(".pm-forget");
      if (!btn) return;
      const id = btn.closest(".pm-item").dataset.mid;
      try { await post(`${pmBase}/forget`, { id }); loadMem(); }
      catch (e2) { alert(e2.message); }
    });
    loadMem();

    // --- learned preferences (inspect / activate / edit / forget) ---
    const prefList = host.querySelector("#prefList");
    const renderPrefs = (rules) => {
      prefList.innerHTML = (rules || []).map((r) => `
        <div class="pm-item" data-pref="${esc(r.id)}" data-scope="${esc(r.scope)}">
          <div class="pm-text">
            <div>${esc(r.rule)}</div>
            <div class="muted small">${esc(scopeLabel(r.scope))} ·
              ${r.active ? "active" : "candidate"} · confidence
              ${Math.round((r.confidence || 0) * 100)}% · seen
              ${r.count || 1}×</div>
          </div>
          <button class="mini-button" data-pref-toggle type="button">${r.active ? "Pause" : "Activate"}</button>
          <button class="mini-button" data-pref-edit type="button">Edit</button>
          <button class="mini-button danger pm-forget" data-pref-forget type="button">Forget</button>
        </div>`).join("")
        || '<span class="muted small">No learned preferences yet.</span>';
    };
    const loadPrefs = async () => {
      try {
        const r = await api("/api/preferences");
        renderPrefs(r.rules || []);
      } catch {
        prefList.innerHTML = '<span class="muted small">Unavailable.</span>';
      }
    };
    host.querySelector("#prefAdd").addEventListener("click", async () => {
      const text = $("prefText").value.trim();
      if (!text) return;
      try {
        await post("/api/preferences/add", {
          rule: text, scope: $("prefScope").value, active: true });
        $("prefText").value = ""; loadPrefs();
      } catch (e) { alert(e.message); }
    });
    prefList.addEventListener("click", async (e) => {
      const row = e.target.closest(".pm-item");
      const id = row && row.dataset.pref;
      if (!id) return;
      try {
        if (e.target.closest("[data-pref-toggle]")) {
          const active = !e.target.closest("[data-pref-toggle]")
            .textContent.includes("Pause");
          await post("/api/preferences/toggle", { id, active });
          loadPrefs(); return;
        }
        if (e.target.closest("[data-pref-edit]")) {
          const cur = row.querySelector(".pm-text div").textContent;
          const next = prompt("Edit learned preference", cur);
          if (next === null) return;
          const nextScope = prompt(
            "Scope: global, profile:<id>, or project:<id>",
            row.dataset.scope || "global");
          if (nextScope === null) return;
          await post("/api/preferences/update", {
            id, rule: next, scope: nextScope });
          loadPrefs(); return;
        }
        if (e.target.closest("[data-pref-forget]")) {
          if (!confirm("Forget this learned preference?")) return;
          await post("/api/preferences/forget", { id });
          loadPrefs();
        }
      } catch (e2) { alert(e2.message); }
    });
    loadPrefs();

    // --- avatar change (same circular-crop model as onboarding) ---
    const cvs = host.querySelector("#profAvatarCanvas");
    const ctx = cvs.getContext("2d");
    const V = 160, R = 70;
    let img = null, dataURL = "", s0 = 1, drag = { x: 0, y: 0 };
    const draw = () => {
      ctx.clearRect(0, 0, V, V); ctx.fillStyle = "#0a1626"; ctx.fillRect(0, 0, V, V);
      if (img) { const z = $("profAvatarZoom").value / 100, s = s0 * z;
        ctx.drawImage(img, drag.x, drag.y, img.width * s, img.height * s); }
      ctx.save(); ctx.fillStyle = "rgba(4,10,20,.55)";
      ctx.beginPath(); ctx.rect(0, 0, V, V);
      ctx.arc(V / 2, V / 2, R, 0, Math.PI * 2, true); ctx.fill("evenodd");
      ctx.strokeStyle = "#11cfff"; ctx.beginPath();
      ctx.arc(V / 2, V / 2, R, 0, Math.PI * 2); ctx.stroke(); ctx.restore();
    };
    const clamp = () => { if (!img) return;
      const z = $("profAvatarZoom").value / 100, s = s0 * z;
      const w = img.width * s, h = img.height * s;
      drag.x = Math.min(V / 2 - R, Math.max(V / 2 + R - w, drag.x));
      drag.y = Math.min(V / 2 - R, Math.max(V / 2 + R - h, drag.y)); };
    let dg = null;
    cvs.addEventListener("pointerdown", (e) => { dg = { x: e.clientX - drag.x, y: e.clientY - drag.y }; cvs.setPointerCapture(e.pointerId); });
    cvs.addEventListener("pointermove", (e) => { if (!dg || !img) return; drag.x = e.clientX - dg.x; drag.y = e.clientY - dg.y; clamp(); draw(); });
    cvs.addEventListener("pointerup", () => { dg = null; });
    $("profAvatarZoom").addEventListener("input", () => { clamp(); draw(); });
    host.querySelector("#profAvatarBtn").addEventListener("click", () => $("profAvatarFile").click());
    $("profAvatarFile").addEventListener("change", () => {
      const f = $("profAvatarFile").files[0]; if (!f) return;
      const rd = new FileReader();
      rd.onload = () => { dataURL = rd.result;
        const im = new Image();
        im.onload = () => { img = im;
          s0 = V / Math.min(im.width, im.height);
          drag = { x: (V - im.width * s0) / 2, y: (V - im.height * s0) / 2 };
          cvs.hidden = false; $("profAvatarZoom").hidden = false;
          $("profAvatarSave").hidden = false; clamp(); draw(); };
        im.src = dataURL; };
      rd.readAsDataURL(f);
    });
    host.querySelector("#profAvatarSave").addEventListener("click", async () => {
      if (!img) return;
      const z = $("profAvatarZoom").value / 100, s = s0 * z;
      const crop = { cx: +((V / 2 - drag.x) / s / img.width).toFixed(4),
        cy: +((V / 2 - drag.y) / s / img.height).toFixed(4),
        zoom: +(Math.min(img.width, img.height) * s / (R * 2)).toFixed(4) };
      try {
        await post(`/api/profiles/${encodeURIComponent(p.profile_id)}/avatar`,
          { data_url: dataURL, crop });
        location.reload();
      } catch (e) { alert(e.message); }
    });
  }

  // ------------------------------------------------------------- creator

  async function renderCreator(host) {
    const p = activeProfile;
    if (!p || !p.is_creator) {
      host.innerHTML = `<div class="settings-title"><div><h2>Creator</h2>
        <p>Creator settings are only available to the verified Creator profile.</p></div></div>`;
      return;
    }
    const ADDR = ["John", "Father", "Creator", "Sir", "Master"];
    host.innerHTML = `
      <div class="settings-title"><div><h2>Creator</h2>
        <p>How Nexus addresses its verified creator. Changes require the Creator passcode.</p></div></div>
      <div class="settings-section">
        <h3>How should Nexus address you?</h3>
        <div class="mood-row">
          ${ADDR.map((a) => `<button class="mood-chip ${p.creator_address === a ? "active" : ""}" data-addr="${esc(a)}" type="button">${esc(a)}</button>`).join("")}
          <button class="mood-chip ${p.creator_address && !ADDR.includes(p.creator_address) ? "active" : ""}" data-addr="__custom" type="button">Custom…</button>
          <button class="mood-chip ${!p.creator_address ? "active" : ""}" data-addr="" type="button">First name</button>
        </div>
        <label style="margin-top:10px">Custom address
          <input id="crCustomAddr" maxlength="40" value="${esc(ADDR.includes(p.creator_address) ? "" : (p.creator_address || ""))}" /></label>
        <div class="prof-fields" style="margin-top:12px">
          <label><input id="crGreet" type="checkbox" ${p.creator_title_greetings ? "checked" : ""} /> Use address in greetings</label>
          <label><input id="crConv" type="checkbox" ${p.creator_title_conversation ? "checked" : ""} /> Use address in conversation</label>
          <label><input id="crNotif" type="checkbox" ${p.creator_title_notifications ? "checked" : ""} /> Use address in notifications</label>
        </div>
        <label style="margin-top:12px">Creator Passcode
          <input id="crPass" type="password" autocomplete="off" inputmode="numeric" /></label>
        <button id="crSave" class="mini-button" type="button" style="margin-top:10px">Save Creator Settings</button>
        <span id="crMsg" class="muted small" style="margin-left:10px"></span>
      </div>`;
    let chosen = p.creator_address || "";
    host.querySelectorAll("[data-addr]").forEach((b) =>
      b.addEventListener("click", () => {
        chosen = b.dataset.addr === "__custom" ? $("crCustomAddr").value.trim() : b.dataset.addr;
        host.querySelectorAll("[data-addr]").forEach((x) => x.classList.toggle("active", x === b));
      }));
    host.querySelector("#crSave").addEventListener("click", async () => {
      const msg = host.querySelector("#crMsg");
      const custom = $("crCustomAddr").value.trim();
      const address = custom || chosen;
      try {
        await post(`/api/profiles/${encodeURIComponent(p.profile_id)}/creator`, {
          passcode: $("crPass").value,
          fields: {
            creator_address: address,
            creator_title_greetings: $("crGreet").checked,
            creator_title_conversation: $("crConv").checked,
            creator_title_notifications: $("crNotif").checked,
          },
        });
        msg.textContent = "Saved.";
        $("crPass").value = "";
      } catch (e) { msg.textContent = e.message; }
    });
  }

  // Boot: fetch the active profile first so the Creator section is only
  // offered to verified Creator profiles.
  try {
    const a = await api("/api/profiles/active");
    activeProfile = a.profile || null;
    if (activeProfile && activeProfile.is_creator) {
      SECTIONS.push(["creator", "Creator"]);
    }
  } catch { /* profiles API unavailable — sections stay as-is */ }

  renderNav();
  renderBody();
  window.addEventListener("hashchange", () => {
    const h = location.hash.slice(1);
    if (SECTIONS.some(([id]) => id === h)) { section = h; renderNav(); renderBody(); }
  });
})();
