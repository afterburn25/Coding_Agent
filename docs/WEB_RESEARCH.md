# Web Research and Browser Tooling

The coding agent has two internet layers.

## Research layer

`web_search(query, count)` performs lightweight public-web discovery and returns result titles/URLs.

`fetch_url(url, max_chars)` downloads a source and extracts readable text or JSON. The agent is instructed to include the URLs it actually used in its answer.

Permission: `network.read`.

The initial search implementation uses DuckDuckGo's lightweight HTML result surface to avoid requiring a hosted search API. The abstraction can later gain Brave/Bing/Google/custom providers without changing the agent tool schema.

## Browser layer

`browser_run(url, actions, screenshot)` drives a real browser through
Playwright — the system Edge channel (`msedge`) when present, else a
managed Chromium under the runtime data dir (`POST /api/browser/install`,
approval-gated by the provisioning plan). Actions: `goto`, `click`,
`type`, `press`, `wait`, `wait_for`, `select`, `hover`, `evaluate`,
`upload`, `scroll`. Results include console messages/errors, failed
requests, screenshots (registered as artifacts), and page text.

**Persistent sessions** are landed: `session` + `save_session` store/
restore `storage_state` per named session. Status is honest —
`GET /api/browser/status` distinguishes missing Playwright, missing
browser, and ready; nothing fakes browser capability.

Install the optional browser stack with:

```bash
pip install -e '.[browser]'
playwright install chromium
```

or let provisioning install the managed runtime. The core application
remains usable without Playwright, and the per-page E2E suite
(`tests/test_browser_e2e.py`, see `docs/BROWSER_E2E.md`) skips cleanly
when no browser is available.

## Next work

- structured DOM/accessibility snapshots
- download capture
- source-quality scoring and research bundles
- streaming browser events into the chat UI

## Research coordinator

For complex/version-sensitive technical work, the higher-level research coordinator in `docs/RESEARCH_SYSTEM.md` should normally be preferred over raw web search. It performs repository/environment preflight, source ranking, cache/provenance handling, and keeps network retrieval as an explicit tool action.
