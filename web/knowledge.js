/* Knowledge — knowledge-graph entity browser: search, detail, neighborhood. */
(function () {
  "use strict";
  const listEl = document.getElementById("entityList");
  const detailEl = document.getElementById("entityDetail");
  const statsEl = document.getElementById("knowledgeStats");
  const filterEl = document.getElementById("knowledgeFilter");
  const depthEl = document.getElementById("graphDepth");
  let entities = [];
  let selected = null;

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  async function api(path) {
    const res = await fetch(path);
    return res.json().catch(() => ({}));
  }

  function renderList() {
    listEl.innerHTML = entities.map(e => `
      <button class="mission-card ${e.id === selected ? "active" : ""}" data-id="${esc(e.id)}" type="button">
        <div class="mission-card-head"><strong>${esc(e.name)}</strong>
          <span class="pill">${esc(e.kind)}</span></div>
      </button>`).join("") ||
      `<div class="empty">No entities — Nexus records knowledge as it works.</div>`;
    listEl.querySelectorAll("[data-id]").forEach(b =>
      b.addEventListener("click", () => select(b.dataset.id)));
  }

  async function select(id) {
    selected = id;
    renderList();
    const d = await api(`/api/knowledge?id=${encodeURIComponent(id)}&depth=${depthEl.value}`);
    renderDetail(d.entity, d.graph || { nodes: [], edges: [] });
  }

  function attrRows(attrs) {
    const entries = Object.entries(attrs || {});
    if (!entries.length) return "<li>No attributes</li>";
    return entries.map(([k, v]) =>
      `<li><strong>${esc(k)}</strong> — ${esc(typeof v === "object" ? JSON.stringify(v) : String(v)).slice(0, 300)}</li>`
    ).join("");
  }

  function renderDetail(ent, graph) {
    if (!ent) { detailEl.innerHTML = `<div class="empty">Select an entity</div>`; return; }
    const byId = {};
    (graph.nodes || []).forEach(n => { byId[n.id] = n; });
    const nameOf = (ref) => (byId[ref] && byId[ref].name) || ref;
    const edges = (graph.edges || []).map(e => `
      <li>${esc(nameOf(e.src))}
        <span class="pill">${esc(e.rel)}</span>
        ${esc(nameOf(e.dst))}</li>`).join("");
    const neighbors = (graph.nodes || [])
      .filter(n => n.id !== ent.id)
      .map(n => `<li><a href="#" data-neighbor="${esc(n.id)}">${esc(n.name)}</a>
        <span class="pill">${esc(n.kind)}</span></li>`).join("");
    detailEl.innerHTML = `
      <div class="mission-card-head"><strong>${esc(ent.name)}</strong>
        <span class="pill">${esc(ent.kind)}</span></div>
      <div class="section-title">Attributes</div>
      <ul class="kv-list">${attrRows(ent.attrs)}</ul>
      <div class="section-title">Relations (${(graph.edges || []).length})</div>
      <ul class="kv-list">${edges || "<li>No relations</li>"}</ul>
      <div class="section-title">Neighbors (${(graph.nodes || []).length - 1})</div>
      <ul class="kv-list">${neighbors || "<li>None</li>"}</ul>`;
    detailEl.querySelectorAll("[data-neighbor]").forEach(a =>
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        select(a.dataset.neighbor);
      }));
  }

  let searchTimer = null;
  async function refresh(q) {
    const url = q ? `/api/knowledge?q=${encodeURIComponent(q)}` : "/api/knowledge";
    const d = await api(url);
    if (d.available === false) {
      statsEl.textContent = "knowledge graph unavailable";
      listEl.innerHTML = `<div class="empty">Knowledge graph is not available.</div>`;
      return;
    }
    const s = d.stats || {};
    statsEl.textContent = `${s.entities || 0} entities · ${s.edges || 0} relations`;
    entities = d.entities || [];
    renderList();
  }

  filterEl.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => refresh(filterEl.value.trim()), 250);
  });
  depthEl.addEventListener("change", () => { if (selected) select(selected); });
  refresh("");
})();
