(() => {
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString() : "—");

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function load() {
  try {
    const q = encodeURIComponent($("#q").value.trim());
    const trust = encodeURIComponent($("#trustFilter").value);
    const res = await fetch(`/api/answer-memory?q=${q}&trust=${trust}`);
    const data = await res.json();
    renderStats(data.stats || {});
    renderList(data.answers || []);
  } catch (e) {
    $("#stats").innerHTML = `<div class="stat-card"><div class="l">Answer Memory</div><div class="v">unavailable</div></div>`;
    $("#list").innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
}

function renderStats(s) {
  const cards = [
    ["Learned answers", s.answers ?? "—"],
    ["Trusted / verified", s.trusted ?? "—"],
    ["Candidates", s.candidates ?? "—"],
    ["Experiences", s.experiences ?? "—"],
    ["Hit rate", s.hit_rate != null ? `${Math.round(s.hit_rate * 100)}%` : "—"],
    ["Exact hits", s.exact_hits ?? "—"],
    ["Semantic hits", s.semantic_hits ?? "—"],
    ["Model calls avoided", s.model_calls_avoided ?? "—"],
    ["Avg lookup", s.avg_lookup_ms != null ? `${s.avg_lookup_ms} ms` : "—"],
    ["Est. time saved", s.est_time_saved_ms ? `${(s.est_time_saved_ms / 1000).toFixed(1)} s` : "—"],
    ["Corrections", s.corrections ?? "—"],
    ["Stale", s.stale ?? "—"],
    ["Embedder", s.embedder ?? "—"],
    ["DB size", s.db_bytes ? `${(s.db_bytes / 1048576).toFixed(1)} MB` : "—"],
  ];
  $("#stats").innerHTML = cards
    .map(([l, v]) => `<div class="stat-card"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`)
    .join("");
}

function renderList(rows) {
  if (!rows.length) {
    $("#list").innerHTML = `<div class="muted">No learned answers match. Ask questions in chat — reusable answers are learned automatically, or teach one above.</div>`;
    return;
  }
  $("#list").innerHTML = rows
    .map((r) => {
      const meta = [
        `<span class="pill ${esc(r.trust_state)}">${esc(r.trust_state)}</span>`,
        `<span>confidence <b>${Math.round((r.confidence || 0) * 100)}%</b></span>`,
        `<span>freshness <b>${esc(r.freshness || "static")}</b></span>`,
        `<span>scope <b>${esc(r.project_scope || "global")}</b></span>`,
        `<span>uses <b>${r.use_count ?? 0}</b></span>`,
        `<span>seen <b>${r.occurrence_count ?? 1}</b>×</span>`,
        `<span>corrections <b>${r.correction_count ?? 0}</b></span>`,
        `<span>source <b>${esc(r.source_type || "")}</b></span>`,
        `<span>last used <b>${fmtTime(r.last_used_at)}</b></span>`,
        r.invalidation_reason ? `<span>invalidated: <b>${esc(r.invalidation_reason)}</b></span>` : "",
      ].filter(Boolean).join("");
      return `<div class="answer-row" data-id="${esc(r.id)}">
        <div class="q">${esc(r.canonical_question)}</div>
        <div class="a">${esc((r.answer_text || "").slice(0, 400))}${(r.answer_text || "").length > 400 ? "…" : ""}</div>
        <div class="meta">${meta}</div>
        <div class="row-actions">
          <button data-act="verify" type="button">Verify / Refresh</button>
          <button data-act="trust" type="button">Trust</button>
          <button data-act="untrust" type="button">Untrust</button>
          <button data-act="edit" type="button">Edit</button>
          <button data-act="merge" type="button">Merge…</button>
          <button data-act="incorrect" type="button">Mark Incorrect</button>
          <button data-act="forget" class="danger" type="button">Forget</button>
        </div>
      </div>`;
    })
    .join("");
}

$("#list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest(".answer-row").dataset.id;
  const act = btn.dataset.act;
  try {
    if (act === "verify") await api("/api/answer-memory/refresh", { id });
    else if (act === "trust") await api("/api/answer-memory/update", { id, trust_state: "trusted" });
    else if (act === "untrust") await api("/api/answer-memory/update", { id, trust_state: "candidate" });
    else if (act === "forget") {
      if (!confirm("Forget this learned answer? It will not be reused.")) return;
      await api("/api/answer-memory/forget", { id });
    } else if (act === "incorrect") {
      const correction = prompt("Optional: provide the correct answer", "");
      if (correction === null) return;
      await api("/api/answer-memory/mark-incorrect", { id, correction });
    } else if (act === "edit") {
      const row = btn.closest(".answer-row");
      const answer = prompt("Edit answer text", row.querySelector(".a").textContent);
      if (answer === null) return;
      await api("/api/answer-memory/update", { id, answer_text: answer });
    } else if (act === "merge") {
      const into = prompt("Merge into answer id (canonical answer keeps its text):", "");
      if (!into) return;
      await api("/api/answer-memory/merge", { from_id: id, into_id: into.trim() });
    }
    await load();
  } catch (err) {
    alert(err.message);
  }
});

$("#learnBtn").addEventListener("click", async () => {
  const q = $("#learnQ").value.trim();
  const a = $("#learnA").value.trim();
  if (!q || !a) return alert("Provide both the question and the answer.");
  try {
    const r = await api("/api/answer-memory/learn", {
      question: q, answer: a,
      scope: $("#learnScope").value, freshness: $("#learnFreshness").value,
    });
    if (!r.ok) throw new Error(r.error || "learn failed");
    $("#learnQ").value = ""; $("#learnA").value = "";
    await load();
  } catch (e) { alert(e.message); }
});

$("#refresh").addEventListener("click", load);
$("#q").addEventListener("input", () => { clearTimeout(window._amT); window._amT = setTimeout(load, 250); });
$("#trustFilter").addEventListener("change", load);

$("#exportBtn").addEventListener("click", async () => {
  const res = await fetch("/api/answer-memory/export");
  const data = await res.json();
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `nexus-answer-memory-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});

$("#importBtn").addEventListener("click", () => $("#importFile").click());
$("#importFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    let payload;
    if (file.name.endsWith(".jsonl")) {
      payload = { answers: text.split("\n").filter(Boolean).map((l) => JSON.parse(l)) };
    } else {
      payload = JSON.parse(text);
    }
    const r = await api("/api/answer-memory/import", payload);
    alert(`Imported ${r.imported ?? 0}, skipped ${r.skipped ?? 0}, conflicts kept existing ${r.conflicts ?? 0}`);
    await load();
  } catch (err) { alert(`Import failed: ${err.message}`); }
  e.target.value = "";
});

$("#rebuildBtn").addEventListener("click", async () => {
  const r = await api("/api/answer-memory/rebuild-index", {});
  alert(`Rebuilt: ${r.reembedded ?? 0} answers re-embedded${r.fts_rebuilt ? ", FTS rebuilt" : ""}`);
  await load();
});
$("#vacuumBtn").addEventListener("click", async () => { await api("/api/answer-memory/vacuum", {}); await load(); });

const clearMap = [
  ["#clearExp", "experiences", "Clear ALL recorded experiences? Trusted answers are kept."],
  ["#clearCand", "candidates", "Delete all observed/candidate answers? Trusted and verified answers are kept."],
  ["#clearTrust", "trusted", "Delete all TRUSTED/VERIFIED answers? This removes every reusable learned answer."],
  ["#clearAll", "all", "Reset Answer Memory COMPLETELY? All answers, aliases, and experiences will be deleted."],
];
for (const [sel, scope, msg] of clearMap) {
  $(sel).addEventListener("click", async () => {
    if (!confirm(msg)) return;
    await api("/api/answer-memory/clear", { scope });
    await load();
  });
}

load();
})();
