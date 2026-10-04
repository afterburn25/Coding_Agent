/* Projects — durable project list, detail, goals and project memory. */
(function () {
  "use strict";
  const listEl = document.getElementById("projectList");
  const detailEl = document.getElementById("projectDetail");
  const countEl = document.getElementById("projectCount");
  const filterEl = document.getElementById("projectFilter");
  let projects = [];
  let selected = null;

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const ago = (ts) => {
    if (!ts) return "";
    const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
    return m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
  };
  async function api(path, method, body) {
    const res = await fetch(path, {
      method: method || "GET",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    return res.json().catch(() => ({}));
  }

  function renderList() {
    const q = (filterEl.value || "").toLowerCase();
    const rows = projects.filter(p => !q ||
      (p.name || "").toLowerCase().includes(q));
    countEl.textContent = `${projects.length} project${projects.length === 1 ? "" : "s"}`;
    listEl.innerHTML = rows.map(p => `
      <button class="mission-card ${p.id === selected ? "active" : ""}" data-id="${esc(p.id)}" type="button">
        <div class="mission-card-head"><strong>${esc(p.name)}</strong>
          <span class="pill">${esc(p.status)}</span></div>
        <div class="mission-card-meta">${p.goals || 0} goals · ${p.tasks || 0} tasks · ${ago(p.updated_at)}</div>
      </button>`).join("") ||
      `<div class="empty">No projects yet — create one to give Nexus durable goals.</div>`;
    listEl.querySelectorAll("[data-id]").forEach(b =>
      b.addEventListener("click", () => select(b.dataset.id)));
  }

  async function select(id) {
    selected = id;
    renderList();
    const d = await api(`/api/projects/${id}`);
    renderDetail(d.project || {});
  }

  function bucket(label, rows) {
    if (!rows || !rows.length) return "";
    return `<div class="section-title">${label}</div><ul class="kv-list">` +
      rows.map(r => `<li>${esc(String(r).slice(0, 220))}</li>`).join("") + `</ul>`;
  }

  function renderDetail(p) {
    if (!p.id) { detailEl.innerHTML = `<div class="empty">Select a project</div>`; return; }
    const m = p.memory_summary || {};
    const goals = (p.goals || []).map(g => `
      <li>${esc(g.objective)} <span class="pill">${esc(g.status)}</span>
        ${g.status === "active" ? `<button class="mini-button" data-goal="${esc(g.id)}" type="button">done</button>` : ""}</li>`).join("");
    const tasks = (p.tasks || []).slice(-12).reverse().map(t => `
      <li><span class="pill">${esc(t.status)}</span> ${esc(t.title)} <small>${ago(t.ts)}</small></li>`).join("");
    const activity = (p.activity || []).slice(-12).reverse().map(a => `
      <li>${esc(a.event)} — ${esc(a.detail)} <small>${ago(a.ts)}</small></li>`).join("");
    detailEl.innerHTML = `
      <div class="mission-card-head"><strong>${esc(p.name)}</strong>
        <span class="pill">${esc(p.status)}</span></div>
      <p>${esc(p.description || "")}</p>
      <div class="side-actions">
        ${p.status === "active" ? `<button class="mini-button" data-status="paused" type="button">Pause</button>` : ""}
        ${p.status === "paused" ? `<button class="mini-button" data-status="active" type="button">Resume</button>` : ""}
        ${p.status !== "archived" ? `<button class="mini-button" data-status="archived" type="button">Archive</button>` : ""}
        ${p.status !== "completed" ? `<button class="mini-button" data-status="completed" type="button">Complete</button>` : ""}
      </div>
      <div class="section-title">Goals</div>
      <ul class="kv-list">${goals || "<li>No goals</li>"}</ul>
      <div class="mini-form"><input id="newGoal" placeholder="Add goal — desired outcome…" />
        <button id="addGoal" class="mini-button" type="button">Add</button></div>
      ${bucket("Architecture", m.architecture)}
      ${bucket("Decisions", m.decisions)}
      ${bucket("Known issues", m.known_issues)}
      ${bucket("Completed work", m.completed_work)}
      ${bucket("Blockers", m.blockers)}
      ${bucket("Next steps", m.next_steps)}
      <div class="section-title">Remember</div>
      <div class="mini-form"><select id="memKind">
        <option value="note">note</option><option value="decision">decision</option>
        <option value="architecture">architecture</option><option value="known_bug">known bug</option>
        <option value="convention">convention</option><option value="constraint">constraint</option>
        <option value="unresolved">unresolved</option><option value="rejected_approach">rejected</option>
      </select><input id="memText" placeholder="Project memory…" />
        <button id="addMem" class="mini-button" type="button">Remember</button></div>
      <div class="section-title">Worker history</div>
      <ul class="kv-list">${tasks || "<li>No tasks yet</li>"}</ul>
      <div class="section-title">Activity</div>
      <ul class="kv-list">${activity || "<li>No activity yet</li>"}</ul>`;
    detailEl.querySelectorAll("[data-status]").forEach(b =>
      b.addEventListener("click", async () => {
        await api(`/api/projects/${p.id}/status`, "POST", { status: b.dataset.status });
        select(p.id); refresh();
      }));
    detailEl.querySelectorAll("[data-goal]").forEach(b =>
      b.addEventListener("click", async () => {
        await api(`/api/projects/${p.id}/goal-complete`, "POST", { goal_id: b.dataset.goal });
        select(p.id);
      }));
    const addGoal = detailEl.querySelector("#addGoal");
    if (addGoal) addGoal.addEventListener("click", async () => {
      const v = detailEl.querySelector("#newGoal").value.trim();
      if (!v) return;
      await api(`/api/projects/${p.id}/goal`, "POST", { objective: v });
      select(p.id); refresh();
    });
    const addMem = detailEl.querySelector("#addMem");
    if (addMem) addMem.addEventListener("click", async () => {
      const v = detailEl.querySelector("#memText").value.trim();
      if (!v) return;
      await api(`/api/projects/${p.id}/remember`, "POST",
        { kind: detailEl.querySelector("#memKind").value, text: v });
      select(p.id);
    });
  }

  async function refresh() {
    const d = await api("/api/projects");
    projects = d.projects || [];
    renderList();
  }

  document.getElementById("createProject").addEventListener("click", async () => {
    const name = document.getElementById("newProjectName").value.trim();
    if (!name) return;
    await api("/api/projects", "POST", {
      name, description: document.getElementById("newProjectDesc").value.trim() });
    document.getElementById("newProjectName").value = "";
    document.getElementById("newProjectDesc").value = "";
    refresh();
  });
  filterEl.addEventListener("input", renderList);
  refresh();
  setInterval(refresh, 15000);
})();
