# Browser automation & E2E verification

Nexus Core ships a real local browser for interactive automation and
end-to-end UI verification of web work — not a simulated fetch.

## Deployment model (frozen Windows build)

- The **Playwright package + node driver ship inside the frozen backend**
  (`--collect-all playwright --collect-all greenlet` in
  `packaging/build_windows.ps1`).
- The **browser binary resolves at run time**, never from a dev-machine
  path:
  1. **System Microsoft Edge** (`channel="msedge"`) — present on every
     supported Windows host; preferred, zero download.
  2. **Managed Chromium** provisioned into
     `<runtime>/data/browser-browsers` via the bundled Playwright driver
     when Edge is absent — `POST /api/browser/install` runs it as a
     tracked job (`packages.install` gated), or the first `browser_run`
     call provisions it inline.
  3. A developer's own `playwright install` user cache as last resort.
- `PLAYWRIGHT_BROWSERS_PATH` is pinned to the managed dir so provisioned
  browsers live in preserved user state (survive app updates).

## Tools

- **`browser_run`** — navigate + act (`click`, `type`, `press`, `wait`,
  `wait_for`, `goto`, `select`, `hover`, `evaluate`, `upload`, `scroll`);
  returns page text, console output, console errors, failed requests,
  optional full-page screenshot. Named `session`/`save_session` persist
  storage state (cookies/login) between runs. `timeout_s` bounds the run;
  the timeline Stop button cancels mid-run cooperatively.
- **`browser_verify`** — E2E pass for the coding pipeline: navigate, run
  optional actions, then assert `expect_text` / `expect_selector` /
  `expect_title_contains` and (by default) a clean console. Returns a
  structured `{pass, verdict, checks[]}` — used to verify scaffolded web
  apps actually render and behave, e.g. dev-server start → navigate →
  interact → console-error-free.

Screenshots register in the Artifact Store (`kind=screenshot`) linked to
the originating task.

## Honest status

- `GET /api/browser/status` reports `playwright`, resolved `channel`,
  and `ready`.
- Tool health (`browser_run`) is healthy only when a browser actually
  resolves — `degraded` when Playwright is present but no browser is.
- The `browser_preview` capability reports `verified` only on a real
  resolvable browser; otherwise `setup_required` with the specific
  unmet requirement (`playwright` vs `browser`).
- Failures report real reasons — never a generic "browser unavailable".
