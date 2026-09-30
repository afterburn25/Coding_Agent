# Web Research and Browser Tooling

The coding agent has two internet layers.

## Research layer

`web_search(query, count)` performs lightweight public-web discovery and returns result titles/URLs.

`fetch_url(url, max_chars)` downloads a source and extracts readable text or JSON. The agent is instructed to include the URLs it actually used in its answer.

Permission: `network.read`.

The initial search implementation uses DuckDuckGo's lightweight HTML result surface to avoid requiring a hosted search API. The abstraction can later gain Brave/Bing/Google/custom providers without changing the agent tool schema.

## Browser layer

`browser_run(url, actions, screenshot)` uses local Chromium through optional Playwright. Supported initial actions are:

- `goto`
- `click`
- `type`
- `press`
- `wait`

Permission: `browser.control` and defaults to `ask`.

Install the optional browser stack with:

```bash
pip install -e '.[browser]'
playwright install chromium
```

The core application remains usable without Playwright.

## Next work

- persistent browser sessions/tabs
- structured DOM/accessibility snapshots
- download capture
- login/session vault integration
- source-quality scoring and research bundles
- streaming browser events into the chat UI

## Research coordinator

For complex/version-sensitive technical work, the higher-level research coordinator in `docs/RESEARCH_SYSTEM.md` should normally be preferred over raw web search. It performs repository/environment preflight, source ranking, cache/provenance handling, and keeps network retrieval as an explicit tool action.
