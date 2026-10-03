// Profile guard + switcher — included on every page.
// While onboarding_required, every page redirects to Start Here; the
// backend independently 403s protected APIs (this is UX, not security).
// After unlock, a profile switcher is injected into the primary nav.
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = (p) => fetch(p).then((r) => (r.ok ? r.json()
    : Promise.reject(new Error(String(r.status)))));
  const post = (p, b) => fetch(p, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(b || {}),
  }).then((r) => r.json());

  const ON_START = location.pathname === "/start.html";
  let _status = null;

  const NP = window.NexusProfile = {
    status: () => _status,
    avatarUrl: (p) => (p && p.avatar_path
      ? `/api/profiles/${encodeURIComponent(p.profile_id)}/avatar` : ""),
    refresh: () => api("/api/onboarding/status").then((s) => {
      _status = s; return s;
    }),
  };

  function removeSwitcher() {
    $("#profileSwitcher")?.remove();
  }

  function initials(p) {
    const f = (p.first_name || " ")[0] || "";
    const l = (p.last_name || " ")[0] || "";
    return (f + l).toUpperCase() || "?";
  }

  function chip(p) {
    const url = NP.avatarUrl(p);
    return url
      ? `<img class="ps-avatar" src="${esc(url)}" alt="" />`
      : `<span class="ps-avatar ps-initials">${esc(initials(p))}</span>`;
  }

  async function doSwitch(profile_id) {
    try {
      const r = await post("/api/profiles/switch", { profile_id });
      if (r.ok) {
        // Survive the reload so the greeting displays on the new page.
        if (r.greeting && r.greeting.text) {
          try {
            sessionStorage.setItem("nexus-greeting",
              JSON.stringify(r.greeting));
          } catch {}
        }
        location.reload(); return;
      }
      alert(r.error || "Switch failed");
    } catch (e) { alert("Switch failed: " + e.message); }
  }

  function showGreetingToast() {
    let g = null;
    try {
      g = JSON.parse(sessionStorage.getItem("nexus-greeting") || "null");
      sessionStorage.removeItem("nexus-greeting");
    } catch {}
    if (!g || !g.text) return;
    const t = document.createElement("div");
    t.className = "greeting-toast";
    t.innerHTML = `
      <img class="ps-avatar" src="/assets/nexus-core-icon.png" alt="" />
      <div class="gt-text">${esc(g.text)}</div>
      <button class="gt-close" type="button" aria-label="Dismiss">✕</button>`;
    t.querySelector(".gt-close").addEventListener("click", () => t.remove());
    document.body.appendChild(t);
    setTimeout(() => t.classList.add("show"), 20);
    setTimeout(() => { t.classList.remove("show");
      setTimeout(() => t.remove(), 400); }, 12000);
  }

  function injectSwitcher(s) {
    removeSwitcher();
    const nav = $(".primary-nav");
    if (!nav) return;
    const active = (s.profiles || []).find((p) => p.profile_id === s.active)
      || (s.profiles || [])[0];
    if (!active) return;
    const host = document.createElement("div");
    host.id = "profileSwitcher";
    host.className = "profile-switcher";
    host.innerHTML = `
      <button class="ps-chip" type="button" aria-haspopup="true">
        ${chip(active)}
        <span class="ps-name">${esc(active.first_name || "Profile")}${active.is_creator ? ' <em class="ps-creator">Creator</em>' : ""}</span>
        <span class="ps-caret">▾</span>
      </button>
      <div class="ps-menu" hidden>
        ${(s.profiles || []).map((p) => `
          <button class="ps-item ${p.profile_id === s.active ? "active" : ""}"
                  data-pid="${esc(p.profile_id)}" type="button">
            ${chip(p)}<span>${esc(p.display_name || p.first_name)}</span>
            ${p.is_creator ? '<em class="ps-creator">Creator</em>' : ""}
          </button>`).join("")}
        <div class="ps-sep"></div>
        <a class="ps-item" href="/personality.html"><span class="ps-ico">◆</span><span>Personality Studio</span></a>
        <a class="ps-item" href="/settings.html#profile"><span class="ps-ico">⚙</span><span>Profile Settings</span></a>
      </div>`;
    nav.appendChild(host);
    const chipBtn = host.querySelector(".ps-chip");
    const menu = host.querySelector(".ps-menu");
    chipBtn.addEventListener("click", (e) => {
      e.stopPropagation(); menu.hidden = !menu.hidden;
    });
    document.addEventListener("click", () => { menu.hidden = true; });
    host.querySelectorAll("[data-pid]").forEach((b) =>
      b.addEventListener("click", () => doSwitch(b.dataset.pid)));
  }

  async function guard() {
    let s;
    try { s = await NP.refresh(); }
    catch { return; }                    // backend unreachable — don't trap
    if (s.required) {
      removeSwitcher();
      if (!ON_START) location.replace("/start.html");
      return;
    }
    // start.html stays reachable when unlocked — it's also the
    // "Create Profile" surface for additional users.
    injectSwitcher(s);
    if (!ON_START) {
      showGreetingToast();
      // Startup greeting once per browser session — the backend owns
      // once-per-profile intro + returning-greeting rotation.
      if (!sessionStorage.getItem("nexus-greeted") && s.active) {
        sessionStorage.setItem("nexus-greeted", "1");
        api(`/api/profiles/${encodeURIComponent(s.active)}/greeting`)
          .then((g) => {
            if (g && g.text) {
              try {
                sessionStorage.setItem("nexus-greeting",
                  JSON.stringify(g));
              } catch {}
              showGreetingToast();
            }
          }).catch(() => {});
      }
    }
  }

  NP.guard = guard;
  document.addEventListener("DOMContentLoaded", guard);
})();
