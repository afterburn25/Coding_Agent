// Settings — secondary sections. `permissions` is the primary surface:
// authorization boundaries, profiles, per-key levels, scopes, and audit.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = (p) => fetch(p).then((r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(new Error(e.error || r.status)))));
  const post = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }).then((r) => r.json().then((d) => (r.ok || d.needs_approval ? d : Promise.reject(new Error(d.error || r.status)))));
  const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");

  const SECTIONS = [
    ["general", "General"], ["permissions", "Permissions"], ["models", "Models"],
    ["appearance", "Appearance"], ["privacy", "Privacy"],
    ["notifications", "Notifications"], ["advanced", "Advanced"],
  ];
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

  renderNav();
  renderBody();
  window.addEventListener("hashchange", () => {
    const h = location.hash.slice(1);
    if (SECTIONS.some(([id]) => id === h)) { section = h; renderNav(); renderBody(); }
  });
})();
