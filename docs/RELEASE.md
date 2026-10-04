# Release Candidate Mode

`localcodeagent/release.py` — the RC workflow and scorecard.

## RC mode

`enter(label)` activates the freeze: while active,
`AutonomyPolicy.check` denies `self_development` and `packages` —
non-essential feature/dependency changes don't land during a release
window. `exit()` releases the freeze. State persists in
`data/rc.json`.

## Scorecard

`run_scorecard()` evaluates ten sections:

| functional_tests | performance | installer | upgrade | profiles |
| voice | images | workers | memory | self_repair |

Each section reports `pass` / `fail` / `skip` / `unknown` from:

- **Live checks** — registered callables evaluated at scorecard time.
  Defaults wired in AppState: `workers` (capability health — any
  `broken` capability fails), `performance` (open regressions fail),
  `memory` (brain store attached). A live check that raises fails.
- **Manual records** — external evidence via `record_section` /
  `POST /api/rc/section` (installer runs, voice tests, upgrade tests).
- **Unknown** — no live check and no manual record.

Verdict: `PASS` only when no *blocking* section is `fail` or
`unknown`. Non-blocking failures land in `known_issues`; blocking ones
in `blocking_issues`. Readiness is never claimed on missing evidence.
Scorecards persist (bounded, last 20).

## API

- `GET  /api/rc` — active state, frozen actions, last scorecard
- `POST /api/rc/enter` `{label}` / `POST /api/rc/exit`
- `POST /api/rc/scorecard` — run checks, render verdict, persist
- `POST /api/rc/section` `{section, status, detail, blocking,
  evidence}` — record external check evidence
