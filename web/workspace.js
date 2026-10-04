/* Coding workspace — file tree, tabbed editor with stale-write
   protection, bounded terminal, problems-from-real-output, git panel.
   Everything here reads/writes through the registered-workspace API —
   nothing touches the disk outside approved roots. */
'use strict';

const api = {
  get: async (u) => (await fetch(u)).json(),
  post: async (u, body) => (await fetch(u, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  })).json(),
};

const $ = (id) => document.getElementById(id);
const state = {
  workspaces: [], active: null, root: null,
  tabs: [], activeTab: -1, dirtyFiles: new Set(),
  termHistory: [], termIdx: -1,
};

/* ---------------------------------------------------------------- helpers */

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function sha1(text) {
  const buf = await crypto.subtle.digest(
    'SHA-1', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0')).join('');
}

/* ------------------------------------------------------- syntax highlight */

const LANGS = {
  py: {
    kw: /\b(def|class|return|if|elif|else|for|while|import|from|as|with|try|except|finally|raise|lambda|yield|pass|break|continue|and|or|not|in|is|None|True|False|async|await|self|global|nonlocal|assert|del)\b/,
    str: /("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/,
    com: /#[^\n]*/,
    num: /\b\d+(\.\d+)?\b/,
  },
  js: {
    kw: /\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|import|export|from|default|try|catch|finally|throw|typeof|instanceof|async|await|this|null|undefined|true|false|of|in|yield|static|get|set)\b/,
    str: /(`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/,
    com: /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)/,
    num: /\b\d+(\.\d+)?\b/,
  },
  html: {
    kw: /<\/?[a-zA-Z][a-zA-Z0-9-]*|\/?>/,
    str: /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/,
    com: /<!--[\s\S]*?-->/,
    num: /\b\d+\b/,
  },
  css: {
    kw: /(@[a-z-]+|[a-z-]+(?=\s*:))/,
    str: /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/,
    com: /\/\*[\s\S]*?\*\//,
    num: /\b\d+(px|em|rem|%|s|ms|vh|vw|deg)?\b/,
  },
  json: {
    kw: /\b(true|false|null)\b/,
    str: /"(?:\\.|[^"\\])*"/,
    com: /$^/,
    num: /\b-?\d+(\.\d+)?([eE][+-]?\d+)?\b/,
  },
  md: {
    kw: /^(#{1,6}\s.*|\*\*[^*]+\*\*|`[^`]+`)/gm,
    str: /\[[^\]]*\]\([^)]*\)/,
    com: /^\s*>.*$/gm,
    num: /$^/,
  },
};
LANGS.ts = LANGS.js; LANGS.jsx = LANGS.js; LANGS.tsx = LANGS.js;
LANGS.xml = LANGS.html; LANGS.yaml = LANGS.json; LANGS.yml = LANGS.json;

function langFor(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  return LANGS[ext] || LANGS[path.endsWith('Dockerfile') ? 'md' : ext];
}

function highlight(code, path) {
  const L = langFor(path);
  if (!L) return esc(code);
  // Single-pass tokenizer: comments > strings > keywords > numbers.
  const re = new RegExp(
    `(${L.com.source})|(${L.str.source})|(${L.kw.source})|(${L.num.source})`,
    L.com.flags || L.kw.flags || 'g');
  let out = '', last = 0;
  code.replace(re, (m, com, str, kw, num, off) => {
    out += esc(code.slice(last, off));
    const cls = com !== undefined ? 'tok-c'
      : str !== undefined ? 'tok-s'
      : kw !== undefined ? 'tok-k' : 'tok-n';
    out += `<span class="${cls}">${esc(m)}</span>`;
    last = off + m.length;
    return m;
  });
  return out + esc(code.slice(last));
}

/* ------------------------------------------------------------------- tree */

function buildTree(entries) {
  const root = { name: '', children: [], dir: true, path: '' };
  const byPath = { '': root };
  for (const e of entries) {
    const parent = byPath[e.path.split(/[\\/]/).slice(0, -1).join('/')]
      || root;
    const node = { ...e, children: [] };
    byPath[e.path.replace(/\\/g, '/')] = node;
    (byPath[e.path.replace(/\\/g, '/')
      .split('/').slice(0, -1).join('/')] || root).children.push(node);
  }
  return root;
}

function renderTree() {
  const list = $('treeList');
  if (!state.root) {
    list.innerHTML = '<span class="muted">No workspace open.</span>';
    return;
  }
  api.get(`/api/fs/tree?path=${encodeURIComponent(state.root)}&depth=6`)
    .then((data) => {
      if (data.error) {
        list.innerHTML = `<span class="muted">${esc(data.error)}</span>`;
        return;
      }
      const root = buildTree(data.entries || []);
      list.innerHTML = '';
      const frag = document.createDocumentFragment();
      const make = (node, container) => {
        for (const child of node.children) {
          const row = document.createElement('div');
          row.className = 'tree-item';
          row.dataset.path = child.path;
          row.innerHTML =
            `<span class="t-icon">${child.dir ? '▸' : '·'}</span>` +
            `<span class="t-name">${esc(child.name)}</span>` +
            (state.dirtyFiles.has(child.name)
              ? '<span class="t-dirty">●</span>' : '');
          if (child.dir) {
            const kids = document.createElement('div');
            kids.className = 'tree-children';
            kids.hidden = true;
            row.addEventListener('click', () => {
              kids.hidden = !kids.hidden;
              row.querySelector('.t-icon').textContent =
                kids.hidden ? '▸' : '▾';
            });
            make(child, kids);
            container.appendChild(row);
            container.appendChild(kids);
          } else {
            row.addEventListener('click', () => openFile(child.path));
            container.appendChild(row);
          }
        }
      };
      make(root, frag);
      list.appendChild(frag);
      if (!list.children.length) {
        list.innerHTML = '<span class="muted">(empty)</span>';
      }
    });
}

/* ------------------------------------------------------------ workspaces */

async function loadWorkspaces() {
  const data = await api.get('/api/workspaces');
  state.workspaces = data.workspaces || [];
  const sel = $('wsSelect');
  sel.innerHTML = '';
  const opt = (label, value) => {
    const o = document.createElement('option');
    o.textContent = label; o.value = value; sel.appendChild(o);
  };
  if (data.primary) opt(`Primary: ${data.primary}`, data.primary);
  for (const w of state.workspaces) {
    opt(`${w.name || w.root}${w.project_type ? ' · ' + w.project_type : ''}`,
        w.root);
  }
  const active = (data.active || {}).root || data.primary;
  if (active) sel.value = active;
  state.root = sel.value || data.primary;
  $('wsTreeTitle').textContent = state.root
    ? (state.root.split(/[\\/]/).pop() || 'Files') : 'Files';
  renderTree();
  refreshGit();
}

async function openWorkspace(path, create) {
  const res = await api.post('/api/workspaces/open',
    { path, create: !!create });
  if (res.error || res.path_not_found) {
    alert(res.detail || res.error || 'open failed');
    return;
  }
  await loadWorkspaces();
  if (res.workspace && res.workspace.root) {
    $('wsSelect').value = res.workspace.root;
    state.root = res.workspace.root;
    renderTree(); refreshGit();
  }
}

/* ----------------------------------------------------------------- editor */

function fileName(p) { return p.split(/[\\/]/).pop() || p; }

function renderTabs() {
  const bar = $('tabsBar');
  bar.innerHTML = '';
  state.tabs.forEach((t, i) => {
    const el = document.createElement('div');
    el.className = 'tab' + (i === state.activeTab ? ' active' : '');
    el.innerHTML =
      `<span>${esc(fileName(t.path))}</span>` +
      (t.dirty ? '<span class="t-dot">●</span>' : '') +
      '<button class="t-close" type="button" title="Close">×</button>';
    el.addEventListener('click', (e) => {
      if (e.target.classList.contains('t-close')) return;
      activateTab(i);
    });
    el.querySelector('.t-close').addEventListener('click', () => {
      if (t.dirty && !confirm(`${fileName(t.path)} has unsaved changes — close anyway?`)) return;
      state.tabs.splice(i, 1);
      if (state.activeTab >= state.tabs.length) {
        state.activeTab = state.tabs.length - 1;
      }
      renderTabs(); showActiveTab();
    });
    bar.appendChild(el);
  });
}

async function openFile(path) {
  const idx = state.tabs.findIndex((t) => t.path === path);
  if (idx >= 0) { activateTab(idx); return; }
  const data = await api.get(`/api/fs/file?path=${encodeURIComponent(path)}`);
  if (data.error) { alert(data.error); return; }
  state.tabs.push({
    path, content: data.content, sha: data.sha, dirty: false,
    truncated: !!data.truncated,
  });
  activateTab(state.tabs.length - 1);
}

function activateTab(i) {
  state.activeTab = i;
  renderTabs(); showActiveTab();
}

function showActiveTab() {
  const pane = $('editorPane'), empty = $('editorEmpty');
  const ed = $('editor');
  if (state.activeTab < 0 || !state.tabs[state.activeTab]) {
    pane.hidden = true; empty.hidden = false; return;
  }
  const tab = state.tabs[state.activeTab];
  pane.hidden = false; empty.hidden = true;
  if (ed.dataset.path !== tab.path || !tab.dirty) {
    ed.value = tab.content;
  }
  ed.dataset.path = tab.path;
  ed.readOnly = !!tab.truncated;
  refreshGutterAndHl();
}

function refreshGutterAndHl() {
  const ed = $('editor'), hl = $('hlLayer'), g = $('gutter');
  const text = ed.value;
  const lines = text.split('\n').length;
  g.innerHTML = Array.from({ length: lines }, (_, i) => i + 1)
    .join('<br>');
  const tab = state.tabs[state.activeTab];
  hl.innerHTML = highlight(text, tab ? tab.path : '') + '\n';
  syncScroll();
}

function syncScroll() {
  const scroll = document.querySelector('.code-scroll');
  $('hlLayer').style.transform =
    `translate(${-scroll.scrollLeft}px, ${-scroll.scrollTop}px)`;
  $('gutter').style.transform = `translateY(${-scroll.scrollTop}px)`;
}

async function saveActive() {
  const tab = state.tabs[state.activeTab];
  if (!tab) return;
  const ed = $('editor');
  const content = ed.value;
  const res = await api.post('/api/fs/file', {
    path: tab.path, content, base_sha: tab.sha,
  });
  if (res.conflict) {
    const choice = confirm(
      'This file changed on disk since you opened it.\n\n' +
      'OK = reload the disk version (your edits are lost)\n' +
      'Cancel = keep editing your version');
    if (choice) { // reload
      state.tabs.splice(state.activeTab, 1);
      activateTab(Math.min(state.activeTab, state.tabs.length - 1));
      await openFile(tab.path);
    }
    return;
  }
  if (res.error) { alert(res.error); return; }
  tab.content = content; tab.sha = res.sha; tab.dirty = false;
  renderTabs(); refreshGit();
}

/* ---------------------------------------------------------------- terminal */

function termAppend(cls, text) {
  const out = $('termOut');
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = text;
  out.appendChild(div);
  out.scrollTop = out.scrollHeight;
}

async function runTerm(cmd) {
  if (!cmd) return;
  state.termHistory.push(cmd);
  state.termIdx = state.termHistory.length;
  termAppend('t-cmd', `${state.root || ''}> ${cmd}`);
  const res = await api.post('/api/terminal/run', {
    command: cmd, cwd: state.root, timeout: 300,
  });
  if (res.error) { termAppend('t-err', res.error); return; }
  if (res.stdout) termAppend('', res.stdout);
  if (res.stderr) termAppend('t-err', res.stderr);
  if (res.timed_out) termAppend('t-err', '[timed out]');
  termAppend(res.exit_code === 0 ? 't-ok' : 't-err',
             `exit ${res.exit_code} · ${res.elapsed}s`);
  extractProblems(cmd, (res.stdout || '') + '\n' + (res.stderr || ''),
                  res.exit_code);
}

/* --------------------------------------------------------------- problems */

const PROBLEM_RE = [
  /(?:^|\n)\s*File "([^"]+)", line (\d+)/g,            // python traceback
  /(?:^|\n)([^\s(:]+\.[a-zA-Z]+)[(:](\d+)[):]?\s*:?\s*(error[^\n]*)/gi,
  /(?:^|\n)(error TS\d+[^\n]*)/g,
  /(?:^|\n)(npm ERR![^\n]*)/g,
  /(?:^|\n)(FAILED[^\n]*)/g,
];

function extractProblems(cmd, output, exitCode) {
  const problems = [];
  for (const re of PROBLEM_RE) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(output)) && problems.length < 50) {
      problems.push({
        file: m[1] && m[2] ? m[1] : cmd,
        line: m[2] || '',
        msg: (m[3] || m[1] || '').slice(0, 200),
      });
    }
  }
  if (exitCode && !problems.length) {
    problems.push({ file: cmd, line: '',
                    msg: `command exited ${exitCode}` });
  }
  const list = $('problemList'), badge = $('problemCount');
  if (!problems.length) return;
  badge.hidden = false;
  badge.textContent = String(problems.length);
  list.innerHTML = '';
  for (const p of problems) {
    const row = document.createElement('div');
    row.className = 'problem-item';
    row.innerHTML =
      `<span class="p-src">${esc(p.file)}${p.line ? ':' + p.line : ''}</span> ` +
      `<span class="p-msg">${esc(p.msg)}</span>`;
    list.appendChild(row);
  }
}

/* -------------------------------------------------------------------- git */

async function refreshGit() {
  const el = $('gitSummary');
  if (!state.root) { el.textContent = ''; return; }
  const g = await api.get(
    `/api/git/status?root=${encodeURIComponent(state.root)}`);
  if (!g.repo) {
    el.textContent = 'Not a git repository.';
    state.dirtyFiles.clear();
    return;
  }
  el.textContent =
    `⎇ ${g.branch || '(detached)'} · ${g.remote || 'no remote'}` +
    (g.ahead != null ? ` · ↑${g.ahead} ↓${g.behind}` : '') +
    ` · ${g.dirty_files || 0} changed`;
  renderTree();
}

async function showDiff() {
  const pre = $('gitDiffOut');
  const d = await api.get(
    `/api/git/diff?root=${encodeURIComponent(state.root)}`);
  pre.hidden = false;
  pre.textContent = d.diff || d.error || '(no diff)';
}

/* ------------------------------------------------------------------ wiring */

function wire() {
  $('wsSelect').addEventListener('change', (e) => {
    state.root = e.target.value;
    $('wsTreeTitle').textContent =
      state.root.split(/[\\/]/).pop() || 'Files';
    renderTree(); refreshGit();
  });
  $('wsOpenBtn').addEventListener('click', async () => {
    const p = prompt('Folder to open as workspace:');
    if (p) await openWorkspace(p.trim(), false);
  });
  $('wsNewBtn').addEventListener('click', async () => {
    const p = prompt('New workspace folder path:');
    if (p) await openWorkspace(p.trim(), true);
  });
  $('wsRevealBtn').addEventListener('click', () => {
    if (state.root) api.post('/api/fs/reveal', { path: state.root });
  });
  $('wsRefreshBtn').addEventListener('click', () => {
    renderTree(); refreshGit();
  });
  $('fsNewFile').addEventListener('click', async () => {
    const name = prompt('New file path (relative or absolute):');
    if (!name) return;
    const p = name.match(/^([a-zA-Z]:[\\/]|\/)/)
      ? name : `${state.root}/${name}`;
    const res = await api.post('/api/fs/file',
      { path: p, content: '' });
    if (res.error) alert(res.error); else { renderTree(); openFile(p); }
  });
  $('fsNewDir').addEventListener('click', async () => {
    const name = prompt('New folder path:');
    if (!name) return;
    const p = name.match(/^([a-zA-Z]:[\\/]|\/)/)
      ? name : `${state.root}/${name}`;
    const res = await api.post('/api/fs/mkdir', { path: p });
    if (res.error) alert(res.error); else renderTree();
  });

  const ed = $('editor');
  ed.addEventListener('input', () => {
    const tab = state.tabs[state.activeTab];
    if (tab && !tab.dirty) { tab.dirty = true; renderTabs(); }
    refreshGutterAndHl();
  });
  ed.addEventListener('scroll', syncScroll);
  ed.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault(); saveActive();
    } else if (e.key === 'Tab') {
      e.preventDefault();
      const s = ed.selectionStart;
      ed.setRangeText('    ', s, ed.selectionEnd, 'end');
      ed.dispatchEvent(new Event('input'));
    }
  });
  document.querySelector('.code-scroll')
    .addEventListener('scroll', syncScroll);

  document.querySelectorAll('.bp-tab').forEach((t) => {
    t.addEventListener('click', () => {
      document.querySelectorAll('.bp-tab')
        .forEach((x) => x.classList.toggle('active', x === t));
      for (const page of ['Terminal', 'Problems', 'Git']) {
        $(`bp${page}`).hidden =
          t.dataset.bp !== page.toLowerCase();
      }
    });
  });

  const input = $('termInput');
  const run = () => {
    const cmd = input.value.trim();
    input.value = '';
    runTerm(cmd);
  };
  $('termRun').addEventListener('click', run);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') run();
    else if (e.key === 'ArrowUp' && state.termHistory.length) {
      e.preventDefault();
      state.termIdx = Math.max(0, state.termIdx - 1);
      input.value = state.termHistory[state.termIdx] || '';
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      state.termIdx = Math.min(state.termHistory.length,
                               state.termIdx + 1);
      input.value = state.termHistory[state.termIdx] || '';
    }
  });
  $('termCwd').textContent = state.root || '';
  $('gitRefresh').addEventListener('click', refreshGit);
  $('gitDiff').addEventListener('click', showDiff);

  if (window.NexusTaskBar && window.NexusTaskBar.init) {
    window.NexusTaskBar.init();
  }
  loadWorkspaces();
}

document.addEventListener('DOMContentLoaded', wire);
