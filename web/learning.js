/* Learning dashboard — renders /api/learning verbatim. Every number
 * on this page comes from recorded evidence; nothing is inferred. */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function pct(v) {
    return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;
  }

  function relTime(ts) {
    if (!ts) return "";
    const mins = Math.max(0, (Date.now() / 1000 - ts) / 60);
    if (mins < 60) return `${Math.round(mins)}m ago`;
    if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
    return `${Math.round(mins / 1440)}d ago`;
  }

  function card(num, lbl, sub) {
    return `<div class="learning-card"><div class="num">${esc(num)}</div>` +
      `<div class="lbl">${esc(lbl)}</div>` +
      (sub ? `<div class="sub">${esc(sub)}</div>` : "") + `</div>`;
  }

  function renderOverview(d) {
    const L = d.lessons || {};
    const byOut = L.by_outcome || {};
    const outTxt = Object.entries(byOut)
      .map(([k, v]) => `${v} ${k}`).join(", ") || "none";
    const C = d.competencies || {};
    const bySt = C.by_status || {};
    const stTxt = Object.entries(bySt)
      .map(([k, v]) => `${v} ${k}`).join(", ") || "none";
    const P = d.procedures || {};
    const S = d.study || {};
    const M = d.mastery || {};
    const cards = [
      card(L.total ?? 0, "lessons", outTxt),
      card(P.procedures ?? 0, "procedures",
        `${P.candidates ?? 0} candidates`),
      card(C.total ?? 0, "competencies", stTxt),
      card(S.total ?? 0, "study sessions", `${S.active ?? 0} active`),
      card(M.evaluations ?? 0, "mastery evals",
        `${M.due_for_retention ?? 0} retention due`),
      card((d.strategies || {}).tracked ?? 0, "strategies",
        ((d.strategies || {}).problem_classes || []).join(", ")),
    ];
    $("overviewCards").innerHTML = cards.join("");
  }

  function renderWeaknesses(d) {
    const rows = d.weaknesses || [];
    $("weaknessEmpty").hidden = rows.length > 0;
    $("weaknessTable").querySelector("tbody").innerHTML = rows.map((r) => {
      const cls = ["strong", "developing", "weak", "untested"]
        .includes(r.status) ? r.status : "untested";
      return `<tr>` +
        `<td>${esc(r.id)}</td>` +
        `<td><span class="status-pill ${cls}">${esc(r.status || "?")}</span></td>` +
        `<td>${pct(r.success_rate)}</td>` +
        `<td>${esc(r.attempts ?? 0)}</td>` +
        `<td>${pct(r.confidence)}</td>` +
        `<td>${esc(r.trend || "—")}</td>` +
        `</tr>`;
    }).join("");
  }

  function renderPriorities(d) {
    const rows = d.priorities || [];
    $("priorityEmpty").hidden = rows.length > 0;
    $("priorityList").innerHTML = rows.map((r) =>
      `<li>${esc(r.id)}` +
      `<span class="meta">priority ${esc(r.priority ?? "—")} · ` +
      `${esc(r.status || "?")} · ${pct(r.success_rate)} over ` +
      `${esc(r.attempts ?? 0)} attempts</span></li>`).join("");
  }

  function renderStudy(d) {
    const S = d.study || {};
    const active = S.active_session || null;
    if (!active) {
      $("studyDetail").innerHTML =
        `<p class="empty-note">${S.active
          ? `${S.active} session(s) marked active`
          : "No active study session."} ${S.total
          ? `(${S.total} total)` : ""}</p>`;
      return;
    }
    const levels = (active.curriculum || {}).levels || [];
    $("studyDetail").innerHTML =
      `<p><strong>${esc(active.topic)}</strong> — ` +
      `${esc((active.concepts || []).length)} concepts, ` +
      `${esc((active.sources || []).length)} sources, ` +
      `${esc((active.questions || []).length)} questions</p>` +
      (levels.length
        ? `<ol class="learning-list">` + levels.map((lv) =>
          `<li>${esc(lv.objective || lv.level)}</li>`).join("") + `</ol>`
        : "");
  }

  function renderMastery(d) {
    const M = d.mastery || {};
    const comps = M.competencies || [];
    const due = d.retention_due || [];
    if (!comps.length && !due.length) {
      $("masteryDetail").innerHTML =
        `<p class="empty-note">No evaluations recorded yet — run ` +
        `<code>/mastery &lt;topic&gt;</code>.</p>`;
      return;
    }
    $("masteryDetail").innerHTML =
      `<ul class="learning-list">` +
      comps.map((c) => `<li>${esc(c)}</li>`).join("") +
      `</ul>` +
      (due.length
        ? `<p class="panel-sub" style="margin-top:8px">Retention due: ` +
          due.map((r) => esc(r.competency || r)).join(", ") + `</p>`
        : "");
  }

  function renderProposals(d) {
    const rows = d.skill_proposals || [];
    $("proposalEmpty").hidden = rows.length > 0;
    $("proposalList").innerHTML = rows.map((r) =>
      `<li>${esc(r.title || r.id)}` +
      `<span class="meta">${esc(r.status || "pending")} · ` +
      `${esc(r.evidence_count ?? r.evidence ?? "?")} evidence</span></li>`
    ).join("");
  }

  async function refresh() {
    try {
      const res = await fetch("/api/learning", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      renderOverview(d);
      renderWeaknesses(d);
      renderPriorities(d);
      renderStudy(d);
      renderMastery(d);
      renderProposals(d);
    } catch (exc) {
      $("overviewCards").innerHTML =
        card("—", "learning API", `unavailable: ${exc.message}`);
    }
  }

  $("refreshBtn").addEventListener("click", refresh);
  refresh();
  setInterval(refresh, 15000);
})();
