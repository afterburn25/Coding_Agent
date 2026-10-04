/* Nexus palette — Ctrl+K / Cmd+K global search across tasks, missions,
   projects, skills, answers, knowledge, queue, dev servers, files.
   Self-contained: injects DOM + styles, loaded on every page. */
(function () {
  "use strict";

  const css = `
    #nexusPalette{position:fixed;inset:0;z-index:9999;display:none;
      background:rgba(8,10,18,.55);backdrop-filter:blur(2px)}
    #nexusPalette.open{display:flex;justify-content:center;
      align-items:flex-start;padding-top:12vh}
    #nexusPalette .np-box{width:min(560px,92vw);background:var(--panel,
      #141826);border:1px solid var(--line,#2a3040);border-radius:10px;
      box-shadow:0 18px 50px rgba(0,0,0,.5);overflow:hidden}
    #nexusPalette input{width:100%;padding:12px 16px;font-size:15px;
      background:transparent;border:0;outline:0;color:var(--text,#e8ecf4);
      border-bottom:1px solid var(--line,#2a3040);box-sizing:border-box}
    #nexusPalette .np-list{max-height:50vh;overflow-y:auto}
    #nexusPalette .np-item{padding:9px 16px;cursor:pointer;display:flex;
      gap:10px;align-items:baseline}
    #nexusPalette .np-item.sel,#nexusPalette .np-item:hover{
      background:var(--panel2,#1b2133)}
    #nexusPalette .np-kind{flex:0 0 auto;font-size:10px;text-transform:
      uppercase;letter-spacing:.08em;color:var(--accent,#67e8f9);
      width:74px}
    #nexusPalette .np-title{flex:1 1 auto;overflow:hidden;
      text-overflow:ellipsis;white-space:nowrap;font-size:13px}
    #nexusPalette .np-detail{flex:0 1 auto;color:var(--muted,#8b93a7);
      font-size:11px;overflow:hidden;text-overflow:ellipsis;
      white-space:nowrap;max-width:40%}
    #nexusPalette .np-empty{padding:18px;text-align:center;
      color:var(--muted,#8b93a7);font-size:12px}`;

  let overlay, input, list, results = [], sel = 0, timer = null;

  function build() {
    const st = document.createElement("style");
    st.textContent = css;
    document.head.appendChild(st);
    overlay = document.createElement("div");
    overlay.id = "nexusPalette";
    overlay.innerHTML =
      '<div class="np-box"><input placeholder="Search Nexus — tasks, ' +
      'missions, files, skills…"/><div class="np-list"></div></div>';
    document.body.appendChild(overlay);
    input = overlay.querySelector("input");
    list = overlay.querySelector(".np-list");
    overlay.addEventListener("mousedown", (e) => {
      if (e.target === overlay) close();
    });
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(run, 180);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); pick(); }
      else if (e.key === "Escape") close();
    });
  }

  function open() {
    if (!overlay) build();
    overlay.classList.add("open");
    input.value = "";
    results = []; sel = 0;
    render("Type to search Nexus");
    setTimeout(() => input.focus(), 10);
  }

  function close() {
    if (overlay) overlay.classList.remove("open");
  }

  function move(d) {
    if (!results.length) return;
    sel = (sel + d + results.length) % results.length;
    render();
  }

  function pick() {
    const r = results[sel];
    if (r && r.ref) window.location.href = r.ref;
  }

  function render(emptyMsg) {
    if (!results.length) {
      list.innerHTML =
        '<div class="np-empty">' + (emptyMsg || "No matches") + "</div>";
      return;
    }
    list.innerHTML = results.map((r, i) =>
      '<div class="np-item' + (i === sel ? " sel" : "") +
      '" data-i="' + i + '">' +
      '<span class="np-kind">' + esc(r.kind) + "</span>" +
      '<span class="np-title">' + esc(r.title) + "</span>" +
      '<span class="np-detail">' + esc(r.detail) + "</span>" +
      "</div>").join("");
    list.querySelectorAll(".np-item").forEach((el) =>
      el.addEventListener("mousedown", (e) => {
        e.preventDefault();
        const r = results[+el.dataset.i];
        if (r && r.ref) window.location.href = r.ref;
      }));
    const selEl = list.querySelector(".np-item.sel");
    if (selEl) selEl.scrollIntoView({ block: "nearest" });
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  async function run() {
    const q = input.value.trim();
    if (!q) { results = []; render("Type to search Nexus"); return; }
    try {
      const r = await fetch("/api/search?q=" + encodeURIComponent(q));
      const data = await r.json();
      results = data.results || [];
      sel = 0;
      render();
    } catch {
      results = [];
      render("Search unavailable");
    }
  }

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (overlay && overlay.classList.contains("open")) close();
      else open();
    }
  });
})();
