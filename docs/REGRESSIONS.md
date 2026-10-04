# Regression Memory, Performance Baselines & Bounded Git Bisect

Three pieces let Nexus *recognize* regressions instead of guessing:

## Regression memory (`RegressionStore`)

`data/regressions.json` tracks *verified behaviors* — "startup worked at
commit X", "voice greeting worked at commit Y".

- `record(behavior, ok, head)` — a pass stamps `verified_head`; the
  first failure *after* a verified pass opens a `regression_detected`
  event carrying the `known_good_head`; a later pass writes
  `regression_cleared`. A failure of something never verified is not a
  regression — it's just broken.

## Performance baselines (`BaselineStore`)

`data/baselines.json` keeps Welford running statistics (mean, variance,
min/max, n) per metric — startup time, TTFT, TPS, RAM/VRAM, build/test
duration.

- `check(metric, value)` flags a regression only when the value exceeds
  `max(mean · 1.25, mean + 3σ)` — tiny noise never alarms, and fewer
  than 5 samples answer `confidence: "insufficient"` honestly.

## Bounded git bisect (`GitBisector`)

Reproducible regression → first bad commit, without touching the user's
checkout:

```python
GitBisector(repo_root).run(good=<sha>, bad=<sha>,
                           test_command="python -c '…'",
                           timeout_s=600, step_timeout_s=120,
                           max_steps=12)
```

- Verifies the contract first: `good` must pass the test, `bad` must
  fail it — unusable ranges fail honestly.
- Checks out midpoints inside a throwaway `git worktree` — never the
  user's working tree.
- Returns `{ok, first_bad, first_bad_message, diff_stat, steps,
  commits_scanned, evidence[]}` — enough for self-repair / the
  hypothesis engine to localize the causal area.
- Always removes its worktree.

## API

| Route | Purpose |
|---|---|
| `GET /api/regressions` | open regressions + summary |
| `GET /api/regressions?behavior=…` | one behavior's state |
| `POST /api/regressions/record` | `{behavior, ok, head, detail}` |
| `GET /api/baselines` / `?metric=…` | summary / one baseline |
| `POST /api/baselines/record` | `{metric, value, context}` |
| `POST /api/baselines/check` | `{metric, value}` → regression verdict |
| `POST /api/bisect` | `{good, bad, test, timeout_s?, step_timeout_s?}` |
