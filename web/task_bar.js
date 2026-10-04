/* Global task bar — shows the currently running task / autonomous mission
 * on every page, with progress info and a cancel button for stuck or stale
 * jobs. Polls /api/tasks; nothing renders while the lane is idle. */
(() => {
  const POLL_MS = 4000;
  const STALE_MS = 5 * 60 * 1000;
  const ACTIVE = new Set(["running", "verifying", "reviewing", "waiting_approval"]);

  let bar = null;
  let lastTaskId = null;

  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement("div");
    bar.id = "task-bar";
    bar.className = "task-bar hidden";
    bar.innerHTML = `
      <span class="tb-dot" aria-hidden="true"></span>
      <span class="tb-label"></span>
      <span class="tb-meta"></span>
      <button type="button" class="tb-cancel">Cancel</button>`;
    bar.querySelector(".tb-cancel").addEventListener("click", onCancel);
    document.body.appendChild(bar);
    return bar;
  }

  function ago(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s}s ago`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    return `${Math.round(m / 60)}h ago`;
  }

  async function onCancel() {
    const id = bar && bar.dataset.taskId;
    if (!id) return;
    const btn = bar.querySelector(".tb-cancel");
    btn.disabled = true;
    btn.textContent = "Cancelling…";
    try {
      const r = await fetch("/api/jobs/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: `task-${id}` }),
      });
      if (!r.ok) throw new Error(`cancel failed (${r.status})`);
    } catch (err) {
      console.error("task cancel failed", err);
      btn.disabled = false;
      btn.textContent = "Cancel";
      return;
    }
    setTimeout(poll, 1200);
  }

  function render(payload) {
    const el = ensureBar();
    const cur = payload && payload.current;
    const queued = (payload && payload.queue) || [];
    if (!cur || !ACTIVE.has(cur.status)) {
      if (lastTaskId !== null || !el.classList.contains("hidden")) {
        el.classList.add("hidden");
      }
      lastTaskId = null;
      return;
    }

    const isMission = !!cur.mission_id || cur.mode === "autonomy" || cur.mode === "self_repair";
    const phase = cur.phase && cur.phase !== "running" ? cur.phase : "working";
    const prompt = (cur.prompt || "").replace(/\s+/g, " ").slice(0, 110);
    const label = el.querySelector(".tb-label");
    label.textContent = `${isMission ? "Autonomous mission" : "Task"} · ${phase}`;
    label.title = cur.prompt || "";

    const meta = el.querySelector(".tb-meta");
    const last = Number(cur.updated_at || 0) * 1000;
    const idleMs = Date.now() - last;
    const stale = last > 0 && idleMs > STALE_MS;
    const bits = [prompt];
    bits.push(`step ${cur.steps || 0}`);
    if (last > 0) bits.push(stale ? `stale — last activity ${ago(idleMs)}` : `active ${ago(idleMs)}`);
    if (queued.length) bits.push(`${queued.length} queued`);
    meta.textContent = bits.join(" · ");
    meta.title = meta.textContent;
    el.classList.toggle("stale", !!stale);

    if (cur.status === "waiting_approval") {
      el.classList.add("waiting");
    } else {
      el.classList.remove("waiting");
    }

    const btn = el.querySelector(".tb-cancel");
    btn.disabled = false;
    btn.textContent = "Cancel";
    el.dataset.taskId = cur.id;
    el.classList.remove("hidden");
    lastTaskId = cur.id;
  }

  async function poll() {
    try {
      const r = await fetch("/api/tasks");
      if (!r.ok) return;
      render(await r.json());
    } catch (_) { /* backend down — keep last state */ }
  }

  document.addEventListener("DOMContentLoaded", () => {
    ensureBar();
    poll();
    setInterval(poll, POLL_MS);
    // Kick faster on task events so the bar appears/disappears promptly.
    try {
      const es = new EventSource("/api/events");
      es.addEventListener("task", poll);
    } catch (_) { /* polling still covers it */ }
  });
})();
