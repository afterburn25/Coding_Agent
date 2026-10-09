/* Social / Agent Network dashboard — renders /api/social verbatim.
 * Connection, motivation, peers, claims ladder, backlog and controls. */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function pct(v) {
    return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;
  }

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

  function card(num, lbl, sub) {
    return `<div class="learning-card"><div class="num">${esc(num)}</div>` +
      `<div class="lbl">${esc(lbl)}</div>` +
      (sub ? `<div class="sub">${esc(sub)}</div>` : "") + `</div>`;
  }

  const LEVELS = [
    ["off", "Off", "No autonomous social activity."],
    ["read_only", "Read only", "Browse and learn; never interact."],
    ["assisted", "Assisted", "Nexus may propose; you approve."],
    ["autonomous", "Autonomous", "Interact within permissions."],
    ["learning_focused", "Learning", "Prioritize questions over posting."],
  ];

  function renderOverview(d) {
    const mot = d.motivation || {};
    const peers = d.peer_count || 0;
    const claims = d.claim_count || 0;
    const backlog = d.backlog_open || 0;
    $("overviewCards").innerHTML = [
      card(d.account === "active" ? "Active" : (d.account || "—"),
           "Account", d.service ? `service: ${d.service}` : ""),
      card(pct(mot.social_interest), "Social interest",
           `curiosity ${pct(mot.social_curiosity)}`),
      card(String(backlog), "Open learning items",
           `${d.thread_count || 0} threads followed`),
      card(String(peers), "Known peers", `${claims} claims tracked`),
    ].join("");
  }

  function renderConnection(d) {
    const rows = [
      ["Service", d.service || "moltbook"],
      ["Enabled", d.enabled ? "yes" : "no"],
      ["Account", d.account || "none"],
      ["Level", d.level || "—"],
    ];
    $("connectionDetail").innerHTML =
      `<table class="kv-table"><tbody>` +
      rows.map(([k, v]) =>
        `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") +
      `</tbody></table>`;
    const link = d.claim_url || "";
    $("claimLink").innerHTML = link
      ? `<p class="claim-link">Ownership claim pending — ` +
        `<a href="${esc(link)}" target="_blank" rel="noopener">verify your agent</a></p>`
      : "";
  }

  function renderLevels(d) {
    const cur = d.level || "assisted";
    $("levelPicker").innerHTML = LEVELS.map(([id, name, desc]) =>
      `<button class="level-btn${id === cur ? " active" : ""}" ` +
      `data-level="${id}" type="button"><strong>${esc(name)}</strong>` +
      `<small>${esc(desc)}</small></button>`).join("");
    $("levelPicker").querySelectorAll(".level-btn").forEach((b) => {
      b.onclick = async () => {
        await api("/api/social/level", { level: b.dataset.level });
        refresh();
      };
    });
  }

  function renderMotivation(d) {
    const m = d.motivation || {};
    const reasons = [];
    if ((m.reply_priority || 0) >= 0.5)
      reasons.push("unanswered replies are waiting");
    if ((m.novelty || 0) >= 0.6)
      reasons.push("new discussions match current interests");
    if ((m.learning_value || 0) >= 0.5)
      reasons.push("peers may hold knowledge Nexus lacks");
    if (d.compulsion && d.compulsion.topic)
      reasons.push(`persistent question: ${d.compulsion.topic}`);
    const rows = [
      ["Interest", pct(m.social_interest)],
      ["Curiosity", pct(m.social_curiosity)],
      ["Reply priority", pct(m.reply_priority)],
      ["Novelty", pct(m.novelty)],
      ["Contribution value", pct(m.contribution_value)],
      ["Learning value", pct(m.learning_value)],
      ["Spam penalty", pct(m.spam_penalty)],
      ["Interests", (d.interests || []).join(", ") || "—"],
      ["Why", reasons.join("; ") || "no strong pull right now"],
    ];
    $("motivationDetail").innerHTML =
      `<table class="kv-table"><tbody>` +
      rows.map(([k, v]) =>
        `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") +
      `</tbody></table>`;
  }

  function renderBacklog(items) {
    const el = $("backlogList");
    $("backlogEmpty").hidden = items.length > 0;
    el.innerHTML = items.slice(0, 20).map((i) =>
      `<li class="learning-item"><strong>${esc(i.topic)}</strong>` +
      `<span class="item-sub">${esc(i.kind || "")} · ` +
      `${esc(i.why || "")} · urgency ${pct(i.urgency)}</span>` +
      (i.candidate_peers && i.candidate_peers.length
        ? `<span class="item-sub">ask: ${esc(i.candidate_peers.join(", "))}</span>`
        : "") +
      `<button class="btn tiny" data-resolve="${esc(i.id)}">resolve</button>` +
      `</li>`).join("");
    el.querySelectorAll("[data-resolve]").forEach((b) => {
      b.onclick = async () => {
        await api("/api/social/backlog/resolve", { id: b.dataset.resolve });
        refresh();
      };
    });
  }

  function renderPeers(peers) {
    const tb = $("peerTable").querySelector("tbody");
    $("peerEmpty").hidden = peers.length > 0;
    tb.innerHTML = peers.slice(0, 25).map((p) => {
      const dom = Object.entries(p.expertise || {})
        .map(([k, v]) => `${k}: ${pct(v && v.confidence)}`).join(", ") || "—";
      return `<tr><td>${esc(p.name || p.id)}</td><td>${esc(dom)}</td>` +
        `<td>${esc((p.interactions || []).length)}</td>` +
        `<td>${esc(relTime(p.last_seen))}</td></tr>`;
    }).join("");
  }

  function renderThreads(threads) {
    const el = $("threadList");
    $("threadEmpty").hidden = threads.length > 0;
    el.innerHTML = threads.slice(0, 15).map((t) =>
      `<li class="learning-item"><strong>${esc(t.topic || t.thread)}</strong>` +
      `<span class="item-sub">interest ${pct(t.interest)} · ` +
      `${esc(t.reason || "")} · ${esc(relTime(t.since))}</span></li>`
    ).join("");
  }

  const RUNG_ORDER = ["heard", "corroborated", "tested", "verified",
                    "applied", "refuted"];
  function renderClaims(claims) {
    const tb = $("claimTable").querySelector("tbody");
    $("claimEmpty").hidden = claims.length > 0;
    tb.innerHTML = claims
      .slice()
      .sort((a, b) => RUNG_ORDER.indexOf(b.ladder) -
                    RUNG_ORDER.indexOf(a.ladder))
      .slice(0, 30)
      .map((c) =>
        `<tr><td>${esc((c.text || "").slice(0, 120))}</td>` +
        `<td>${esc(c.source_peer || "—")}</td>` +
        `<td>${esc(c.domain || "general")}</td>` +
        `<td><span class="rung rung-${esc(c.ladder)}">${esc(c.ladder)}</span></td>` +
        `<td>${pct(c.confidence)}</td></tr>`).join("");
  }

  async function refresh() {
    const [status, peers, claims, backlog] = await Promise.all([
      api("/api/social"), api("/api/social/peers"),
      api("/api/social/claims"), api("/api/social/backlog"),
    ]);
    if (!status.available) {
      $("overviewCards").innerHTML =
        card("Off", "Social service", "connector not configured");
      return;
    }
    renderOverview(status);
    renderConnection(status);
    renderLevels(status);
    renderMotivation(status);
    renderPeers(peers.peers || []);
    renderThreads((status.threads || []));
    renderClaims(claims.claims || []);
    renderBacklog(backlog.items || []);
  }

  $("refreshBtn").onclick = refresh;
  $("heartbeatBtn").onclick = async () => {
    await api("/api/social/heartbeat", {});
    refresh();
  };
  $("verifyBtn").onclick = async () => {
    await api("/api/social/verify", {});
    refresh();
  };
  refresh();
})();
