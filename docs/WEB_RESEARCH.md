# Web Research and Browser Tooling

The coding agent has two internet layers.

## Research layer
`web_search(query, count)` performs lightweight public-web discovery and returns result titles/URLs.

`fetch_url(url, max_chars)` downloads a source and extracts readable text or JSON. The agent is instructed to include the URLs it actually used.

Permission: `network.read`.

The initial search implementation uses DuckDuckGo's lightweight HTML result surface so a hosted search API is not required. The provider can later be swapped without changing the agent tool schema.

## Browser layer
`browser_run(url, actions, screenshot)` uses local Chromium through optional Playwright. Initial actions: `goto`, `click`, `type`, `press`, `wait`.

Permission: `browser.control` and defaults to ask.

Install:
```bash
pip install -e '.[browser]'
playwright install chromium
```

The core agent remains usable without Playwright.
