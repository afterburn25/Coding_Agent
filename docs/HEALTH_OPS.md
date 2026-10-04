# Predictive Health, Idle Cleanup, Benchmarks, Temp Specialists

## Trends (`localcodeagent/trends.py`)

`TrendAnalyzer` (`data/trends.json`, ≤500 points per metric) records
metric samples and fits a least-squares slope. Predictions are honest:

- <6 samples → `insufficient_data`
- drift < 2× residual noise (or <2% of mean) → `stable`
- otherwise `rising`/`falling` + optional `projection` with hedged
  language: "may reach the limit in roughly Nh if the current trend
  continues" — no fake precision.

## Idle cleanup (`localcodeagent/cleanup.py`)

`IdleCleaner` only ever scans bounded runtime subtrees
(`worktrees`, `data/logs`, `data/tmp`, `data/cache`). `scan()` reports
stale dirs (>24h) and oversized files (>25 MB) with reasons. `run()`
defaults to `dry_run` — real deletion requires `execute=true`, is
capped per run (512 MB / 200 items), and refuses anything outside the
runtime root.

## Benchmark lab (`localcodeagent/benchmarks.py`)

`run(name, fn, iterations)` times a callable (a raising callable is a
failed run, not a crash); `record_result` accepts externally measured
numbers (latency, throughput, memory, VRAM, quality). Successful
latencies feed `BaselineStore` — benchmark regressions trip the same
noise-tolerant thresholds as production metrics.

## Temporary specialists (`localcodeagent/temp_specialists.py`)

`spawn()` creates a task-scoped specialist — name, domain, explicit
task, `capabilities` allowlist (intersected with the real tool
registry at the API layer), `context_refs`, `acceptance_criteria`,
`mission_id`, TTL (default 4h). `dispatchable()` returns the spec only
while active and unexpired; `complete`/`retire`/`reap_expired` move it
through the lifecycle. Records persist — expired specs stay
inspectable.

## API

- `POST /api/trends/record` `{metric, value}` · `GET /api/trends` ·
  `GET /api/trends/<metric>?limit=`
- `GET  /api/cleanup` (scan) · `POST /api/cleanup/run` `{execute}`
- `GET  /api/benchmarks` · `GET /api/benchmarks/<name>` ·
  `POST /api/benchmarks/record` `{name, latency_ms, ...}`
- `GET  /api/specialists?active=1&mission=` · `POST
  /api/specialists/spawn|complete|retire`
