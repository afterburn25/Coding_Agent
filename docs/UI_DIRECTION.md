# Chat Nexus UI Direction

## Canonical design

The approved Chat Nexus application shell is the chat-first concept selected on 2026-09-30, hosted in the **native ChatNexus.exe desktop window**.

### Desktop hosting

- Windows users launch `ChatNexus.exe`; do not make a browser tab the normal product experience.
- The HTML/CSS/JS shell runs inside Windows WebView2 via the native desktop host.
- A loopback backend may be used internally but should remain invisible implementation detail.
- Normal desktop launch has an app/taskbar icon and standard native minimize/maximize/close behavior.
- Keep `--server` only for developer/debug workflows.

### Layout

- **Left rail:** official Chat Nexus brand, Chat, Projects, Models, Research, Images, Tools, Settings.
- **Settings** (`/settings.html`) is its own shell page with secondary nav (General, Permissions, Models, Appearance, Privacy, Notifications, Advanced). **Settings → Permissions** owns authorization: profiles, per-capability matrix, scopes, approval rules, and the audit log. **Tools** (`/tools.html`) owns operations: install/remove/health/runtime management only.
- **Center:** model selector/status, Chat Nexus conversation, quick actions, and a persistent composer.
- **Right rail:** tabs for **Code Diff**, **Tasks**, and **Terminal / agent activity**.
- System/model/hardware details are available but should stay secondary to the conversation.

### Visual language

- Dark navy / near-black surfaces.
- Cyan → electric blue → violet → magenta accents.
- Thin blue borders and restrained glow; polished rather than visually busy.
- Rounded 8–13 px panels.
- The selected orbital **CN** emblem is the official logo.
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

`web/assets/chat-nexus-emblem.png`

This is the canonical application emblem. Do not replace it without explicit user approval.
