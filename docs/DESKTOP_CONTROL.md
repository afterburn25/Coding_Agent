# Desktop Control

Nexus Core exposes local desktop observation and input through the central
`ToolRegistry`. The implementation lives in
`localcodeagent/computer_use/` and is registered by
`localcodeagent/tools/computer_use.py` during `AppState` startup.

Desktop control is intentionally conservative:

- all tools pass through `PermissionManager` — the `ComputerUse` primitive
  itself is not the policy boundary;
- every tool defaults to `ask` in every permission profile;
- autonomous mode never auto-grants desktop permissions;
- on non-Windows platforms tools return structured failures instead of
  pretending to work;
- every attempted action is audited with a terminal `completed` or `failed`
  state in the activity timeline;
- typed text and clipboard contents are never written to the audit log.

## Permissions

The desktop capability keys are granular so a user can allow observation
without allowing input injection.

| Permission | Tools | Purpose |
|---|---|---|
| `desktop.view` | `computer_windows`, `computer_active_window` | List visible windows and identify the foreground application. |
| `screen.capture` | `computer_screenshot` | Capture the visible virtual screen. This is higher risk because the screen may contain private content. |
| `desktop.control` | `computer_focus` | Focus or restore a visible window. |
| `mouse.control` | `computer_click`, `computer_scroll` | Move/click/scroll the physical cursor. |
| `keyboard.control` | `computer_type`, `computer_keys` | Inject Unicode text and key combinations into the focused app. |
| `clipboard.read` | `computer_clipboard_get` | Read clipboard text, which may contain secrets. |
| `clipboard.write` | `computer_clipboard_set` | Replace clipboard text. |
| `application.launch` | `computer_launch` | Start a local executable directly, without a shell. |

The older broad permissions remain backward-compatible aliases:

- `computer.observe` applies to `desktop.view`, `screen.capture`, and
  `clipboard.read` when the granular key is unset.
- `computer.control` applies to `desktop.control`, `mouse.control`,
  `keyboard.control`, `clipboard.write`, and `application.launch` when the
  granular key is unset.

Setting a granular key explicitly overrides the legacy alias.

## Tools

### Observation

- `computer_windows()` returns visible windows (`hwnd`, title, bounds, pid).
- `computer_active_window()` returns the current foreground window.
- `computer_screenshot()` writes a PNG under `data/screenshots/` using a
  timestamp + unique suffix, then returns path/size metadata.

### Window/application control

- `computer_focus(title="" | hwnd=0)` focuses a window by substring or
  handle. Handles are revalidated with `IsWindow` before focus.
- `computer_launch(path, args=[])` starts an executable with a bounded
  argument list and no shell interpretation.

### Input

- `computer_click(x, y, button="left|right")`
- `computer_scroll(x, y, clicks=3)`
- `computer_type(text)` — Unicode input, capped at 4000 characters.
- `computer_keys(keys=["ctrl", "s"])` — named keys plus single characters.

Mouse coordinates are validated against the Windows virtual screen before
`SetCursorPos` runs. Scroll detents are bounded, mouse buttons are validated
at both schema and primitive layers, and key combinations are limited to
known/single-character keys.

## Audit trail

Each primitive records:

- timestamp
- action name
- sanitized action detail
- terminal `ok` state
- bounded result metadata
- linked activity row when invoked through the tool layer

Sensitive payload rules:

- `computer_type` records character count, not text.
- `computer_clipboard_get` returns text to the approved caller, but audit
  stores only character count.
- `computer_clipboard_set` records character count.
- `computer_launch` records executable path and argument count, not the raw
  argument payload.

The in-memory `ComputerUse.actions` list is bounded to the newest 500
actions. Timeline rows use the `computer-use` task id and are closed as
`completed` or `failed`, so failed desktop actions remain distinguishable
from successful ones and do not linger as running work.

## Dogfood expectations

A safe desktop interaction should look like:

1. `computer_active_window` or `computer_windows` under `desktop.view`
2. optional `computer_screenshot` under `screen.capture`
3. human approval for any `desktop.control`, `mouse.control`,
   `keyboard.control`, `clipboard.write`, or `application.launch` action
4. activity timeline shows the action and terminal result

If desktop permission is denied, Nexus should explain the denial and remain
read-only instead of trying another path around `PermissionManager`.
