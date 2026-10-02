# Nexus Core UI Direction

## Canonical design

The approved Nexus Core application shell is the chat-first concept selected on 2026-09-30, hosted in the **native NexusCore.exe desktop window**.

### Desktop hosting

- Windows users launch `NexusCore.exe`; do not make a browser tab the normal product experience.
- The HTML/CSS/JS shell runs inside Windows WebView2 via the native desktop host.
- A loopback backend may be used internally but should remain invisible implementation detail.
- Normal desktop launch has an app/taskbar icon and standard native minimize/maximize/close behavior.
- Keep `--server` only for developer/debug workflows.

### Layout

- **Left rail:** official Nexus Core brand, Chat, Projects, Models, Research, Images, Tools, Settings.
- **Settings** (`/settings.html`) is its own shell page with secondary nav (General, Permissions, Models, Appearance, Privacy, Notifications, Advanced). **Settings → Permissions** owns authorization: profiles, per-capability matrix, scopes, approval rules, and the audit log. **Tools** (`/tools.html`) owns operations: install/remove/health/runtime management only.
- **Center:** model selector/status, Nexus Core conversation, quick actions, and a persistent composer.
- **Right rail:** tabs for **Code Diff**, **Tasks**, and **Terminal / agent activity**.
- System/model/hardware details are available but should stay secondary to the conversation.

### Visual language

- Dark navy / near-black surfaces.
- Cyan → electric blue → violet → magenta accents.
- Thin blue borders and restrained glow; polished rather than visually busy.
- Rounded 8–13 px panels.
- The official brand is **Nexus Core**: the full shield/wordmark
  (`web/assets/nexus-core-logo.png`) on large surfaces and the compact shield
  (`web/assets/nexus-core-icon.png`, `desktop/ChatNexus.Desktop/nexus-core.ico`)
  where space is tight. The retired orbital CN emblem must not be used as the
  primary identity.
- Startup shows the official splash artwork (`nexus-core-splash.png`) with a
  real milestone-driven progress bar; see `desktop/ChatNexus.Desktop/Program.cs`.
- Use the logo as a recognizable mark; do not surround every control with extra decorative effects.

### Interaction principles

1. Chat remains the primary control surface.
2. Automatic model routing stays visible but does not demand manual selection.
3. Code changes should surface in the right Code Diff rail.
4. Approvals and autonomous task progress belong in Tasks.
5. Tool calls/model switches/command output belong in Terminal/activity.
6. Image and Research workspaces remain first-class navigation destinations.
7. Preserve existing backend API IDs when restyling so UI work does not break agent functionality.

### Asset

`web/assets/nexus-core-icon.png`

This is the canonical application emblem. Do not replace it without explicit user approval.

### Shared design system (mandatory for all pages)

All pages must consume the design tokens and shared primitives in
`web/styles.css`. Never reintroduce per-page hardcoded palettes — the gray
`#101217/#242833` scheme that previously appeared in tools/research/image/
answers/system was consolidated into the single navy/cyan token set.

**Tokens** (`:root` in `styles.css`):

| Role | Token |
|---|---|
| App background | `--bg` |
| Primary surface / inputs | `--surface` |
| Sidebar / secondary surface | `--panel` |
| Cards / raised panels | `--panel2` |
| Borders | `--line`, `--line2`, `--border` (alias) |
| Text | `--text`, `--text-dim`, `--muted`, `--faint` |
| Accents | `--cyan`, `--blue`, `--violet`, `--magenta`, `--accent`, `--link` |
| Status | `--good`, `--warn`, `--danger`, `--info` |
| Status chip backgrounds | `--good-tint`, `--warn-tint`, `--danger-tint`, `--info-tint`, `--violet-tint` |
| Misc | `--code-bg`, `--focus`, `--selection`, `--r-sm/md/lg`, `--shadow-card` |

**Shared primitives** (in `styles.css`, no page-local overrides):

- Navigation: `.workspace-link` / `.side-links a` (nav items), `.brand` +
  `.brand-logo` + `.brand-sub` (sidebar header), `.side-toggle` (in-sidebar
  buttons such as Voice On), `.section-title` / `.side-block-title`.
- Controls: element-level `input`/`select`/`textarea` defaults — bare
  controls are automatically themed; `.mini-button` for small actions;
  `.btn`, `.btn-primary`, `.btn-danger` for the common button variants.
- Patterns: `.page-head` (page title + subtitle that never collides),
  `.empty-state`, themed thin scrollbars, `[hidden]` always wins over
  class-level `display` rules.

**Canonical workspace nav** — every non-chat page links to the same set,
in this order, with the current page marked `.active`:
Chat (`/`), Models, Research, Missions, Image Studio, Tools, Trainer,
Voice Studio, Learned Answers, System, Settings.

**Chat layout invariants** (guarded by `tests/test_ui_layout.py`):

- `.main` is a CSS grid whose content-bearing rows are `auto` — never pin
  the composer/attachment/footer to a fixed-pixel track.
- The composer is a strong visual anchor: auto-resizing textarea, bottom-
  anchored controls, never overlaps messages or rails.
- The utility rail collapses via `#railToggle` (state persisted in
  `localStorage`); a slim edge strip keeps it discoverable.
- At narrow widths the sidebar hides and `.mobile-nav` inside the topbar
  provides navigation.

Structural layout guards live in `tests/test_ui_layout.py` — run it after
any shell/nav/grid change.
