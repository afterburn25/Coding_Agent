# Research Intelligence System

The research layer sits above the basic `web_search` / `fetch_url` tools. Its purpose is to stop the coding agent from guessing when a task depends on a current API, a specific installed version, an unfamiliar integration, or an exact build/runtime error.

## Flow

```text
coding task
  -> repository/environment preflight
  -> knowledge-gap plan
  -> local repository/docs/package metadata first
  -> official docs / official upstream source when needed
  -> upstream issues and broader web only as needed
  -> source ranking + provenance
  -> implementation
  -> build/test
  -> failed verification? diagnose -> research -> patch -> retest
```

`prepare_task()` is local-only: it performs no web request. Network research occurs only when the model calls a research/network tool.

## Tools

- `research_topic`
- `search_documentation`
- `search_repository`
- `search_github`
- `search_errors`
- `lookup_api`
- `read_release_notes`
- `check_package_version`
- `summarize_research`
- `get_cached_research`

The lower-level `web_search`, `fetch_url`, and optional Playwright `browser_run` remain available for direct lookups and interactive websites.

## Safety and privacy boundaries

Likely credentials/secrets are redacted from research queries before network use. Retrieved web content is represented as untrusted information and must never override agent/system/user instructions. Repository source is not automatically uploaded to a search provider; the agent is instructed to search with problem/version terms rather than private source text.

## Modes

- `auto`
- `local_only`
- `official`
- `balanced`
- `deep`
- `offline`
- `none`

Research cache/session data lives under `.agent/research` by default and is local to the workspace.
