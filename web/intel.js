/* Intelligence Center — Situation, Capability Truth Graph, Identity.
 * Renders live probed state: /api/situation, /api/capabilities/graph,
 * /api/identity, /api/identity/audit. Nothing here is remembered lore. */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function relTime(ts) {
    if (!ts) return "never";
    const mins = Math.max(0, (Date.now() / 1000 - ts) / 60);
    if (mins < 60) return `${Math.round(mins)}m ago`;
    if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
    return `${Math.round(mins / 1440)}d ago`;
  }

  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
    const r = await fetch(path, opt);
    return r.json().catch(() => ({}));
  }

  // -- tabs ----------------------------------------------------------------

  document.querySelectorAll(".intel-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".intel-tab")
        .forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".intel-pane")
        .forEach((p) => p.classList.toggle(
          "hidden", p.id !== `pane-${btn.dataset.tab}`));
    });
  });

  // -- situation ------------------------------------------------------------

  const POS = new Set(["verified", "available", "degraded", "active",
                       "connected", "healthy", "running"]);
  const WARN = new Set(["setup_required", "degraded", "permission_required",
                        "temporarily_unavailable", "awaiting_verification",
                        "awaiting_human", "creating", "verifying_login",
                        "unauthenticated", "experimental"]);

  function stateClass(state) {
    const s = String(state || "");
    if (POS.has(s)) return "ok";
    if (WARN.has(s)) return "warn";
    if (!s) return "unknown";
    return "neg";
  }

  function renderSituation(sit) {
    $("situationText").textContent =
      sit.text || "Nothing is running right now.";
    const cells = [];
    const add = (label, val, sub) => cells.push(
      `<div class="sit-cell"><h3>${esc(label)}</h3>` +
      `<div class="sit-val">${esc(val || "—")}</div>` +
      (sub ? `<div class="sit-sub">${esc(sub)}</div>` : "") + `</div>`);

    const conv = sit.conversation || {};
    add("Conversation", conv.active ? "active" : "idle",
        conv.topic || conv.goal || "");
    const ms = sit.missions || {};
    add("Missions",
        `${(ms.active || []).length} active · ${(ms.paused || []).length} paused`,
        `${ms.workstreams_active || 0} workstreams`);
    const act = sit.activity || {};
    add("Activity",
        `${(act.running || []).length} jobs · ${(act.downloads || []).length} downloads`,
        `${(act.installs || []).length} installs`);
    const md = sit.models || {};
    add("Models", md.resident || "none resident", "");
    const cap = sit.capacity || {};
    add("Resources",
        `pressure: ${sit.resource_pressure || "low"}`,
        `RAM ${cap.ram_pct ?? "—"}% · VRAM ${cap.vram_pct ?? "—"}%`);
    const ap = sit.approvals || {};
    add("Approvals", `${ap.count || 0} pending`,
        (ap.pending || [])[0] || "");
    const svc = sit.services || {};
    const svcTxt = Object.entries(svc)
      .map(([k, v]) => `${k}: ${v}`).join(" · ") || "none";
    add("Services", svcTxt, "");
    const soc = sit.social || {};
    add("Social", `${soc.waiting_consults || 0} consults waiting`, "");
    const fl = sit.failures || {};
    add("Failures", `${fl.count || 0} recent`,
        (fl.recent || [])[0] || "");
    if (sit.project) add("Project", sit.project, "");
    $("situationGrid").innerHTML = cells.join("");
  }

  // -- capabilities ---------------------------------------------------------

  function renderCaps(graph) {
    const nodes = (graph.nodes || []).slice()
      .sort((a, b) => String(a.name).localeCompare(String(b.name)));
    $("capGrid").innerHTML = nodes.map((n) => {
      const cls = stateClass(n.state);
      const deps = (n.depends_on || []).length
        ? `<div class="cap-engine">needs: ${esc(n.depends_on.join(", "))}</div>`
        : "";
      const engine = n.engine
        ? `<div class="cap-engine">${esc(n.engine)}</div>` : "";
      const blks = (n.blockers || []).map((b) =>
        `<div class="blk">${esc(b.required_by
          ? `${b.capability} (${b.state}) — needed by ${b.required_by}`
          : `${b.requirement || b.capability}: ${b.detail || ""}`)}</div>`
      ).join("");
      const st = n.has_selftest
        ? `<div class="cap-actions"><button class="btn secondary cap-test"
             data-cap="${esc(n.id)}" type="button">Self-test</button></div>`
        : "";
      const when = n.verified_at ? `verified ${relTime(n.verified_at)}`
        : n.checked_at ? `checked ${relTime(n.checked_at)}` : "";
      return `<div class="cap-card">
        <div class="cap-head"><h3>${esc(n.name)}</h3>
          <span class="cap-state ${cls}">${esc(n.state)}</span></div>
        ${engine}
        <div class="cap-detail">${esc(n.detail || n.disposition || "")}
          ${when ? `· ${esc(when)}` : ""}</div>
        ${deps}
        ${blks ? `<div class="cap-blockers">${blks}</div>` : ""}
        ${st}
      </div>`;
    }).join("") || `<div class="cap-detail">No capabilities registered.</div>`;
    document.querySelectorAll(".cap-test").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        b.textContent = "Testing…";
        await api("/api/capabilities/selftest", { capability: b.dataset.cap });
        loadCaps(true);
      }));
  }

  // -- identity --------------------------------------------------------------

  function renderIdentity(idn) {
    const cards = [];
    const cadd = (label, val) => cards.push(
      `<div class="stat-card"><div class="idcard-row"><span>${esc(label)}</span>` +
      `<strong>${esc(val || "—")}</strong></div></div>`);
    cadd("Canonical name", idn.canonical_name);
    cadd("Primary email", idn.primary_email || "not set");
    cadd("Recovery owner", idn.recovery_owner === "user"
      ? "you (the owner)" : idn.recovery_owner);
    const c = idn.counts || {};
    cadd("Accounts", `${c.total || 0} total · ${c.active || 0} active`);
    if (c.awaiting_human) {
      cadd("Needs you", `${c.awaiting_human} account(s) waiting on a human step`);
    }
    $("identityCards").innerHTML = cards.join("");

    $("accountList").innerHTML = (idn.accounts || []).map((a) => {
      const cls = stateClass(a.state);
      const detail = [a.auth_method, a.credential_ref
        ? `credential: ${a.credential_ref}` : "", a.detail,
        a.live_detail].filter(Boolean).join(" · ");
      const wf = a.workflow || {};
      const wfBit = wf.human_challenge
        ? `<div class="acct-detail">waiting on you: ${esc(wf.human_challenge)}</div>`
        : "";
      return `<div class="acct-row">
        <span class="acct-svc">${esc(a.service)}</span>
        <span class="acct-handle">${esc(a.handle || "—")}
          ${detail ? `<div class="acct-detail">${esc(detail)}</div>` : ""}
          ${wfBit}</span>
        <span class="cap-state ${cls}">${esc(a.state)}</span>
        ${a.state === "verifying_login" || a.state === "awaiting_verification"
          ? `<span class="acct-actions"><button class="btn secondary acct-verify"
               data-svc="${esc(a.service)}" type="button">Verify</button></span>`
          : ""}
      </div>`;
    }).join("") || `<div class="cap-detail">No accounts registered yet.
      Accounts Nexus creates or connects appear here.</div>`;

    document.querySelectorAll(".acct-verify").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        await api("/api/identity/account/verify", { service: b.dataset.svc });
        loadIdentity();
      }));
  }

  function renderAudit(rows) {
    $("identityAudit").innerHTML = (rows || []).slice().reverse().map((a) => {
      const t = new Date((a.ts || 0) * 1000)
        .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const d = a.detail || {};
      const what = [d.service, d.state, d.challenge]
        .filter(Boolean).join(" · ");
      return `<div class="audit-row"><span>${esc(t)}</span> ` +
        `<span class="audit-action">${esc(a.action)}</span> ` +
        `<span>${esc(what)}</span></div>`;
    }).join("") || `<div class="cap-detail">No identity events yet.</div>`;
  }

  // -- loaders ----------------------------------------------------------------

  async function loadSituation() {
    renderSituation(await api("/api/situation"));
  }
  async function loadCaps(force) {
    renderCaps(await api("/api/capabilities/graph"
      + (force ? "?refresh=1" : "")));
  }
  async function loadIdentity() {
    renderIdentity(await api("/api/identity"));
    renderAudit((await api("/api/identity/audit")).audit);
  }

  async function loadAll() {
    await Promise.all([loadSituation(), loadCaps(), loadIdentity()]);
  }

  $("refreshBtn").addEventListener("click", loadAll);
  loadAll();
  setInterval(loadSituation, 15000);
})();
