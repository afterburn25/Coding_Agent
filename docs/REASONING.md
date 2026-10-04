# Hypothesis Engine, Causal Memory & Decision Journal

Phase 2 of the intelligence/reliability milestone — Nexus no longer
jumps from symptom to fix. Failures produce ranked hypotheses that are
tested, a mechanism-level causal memory feeds priors into the next
diagnosis, and significant decisions are journaled with alternatives.

## Hypothesis Engine (`localcodeagent/hypotheses.py`)

Durable rows in `data/hypotheses.json`. A hypothesis is a *candidate
explanation*, never a fact:

| Field | Meaning |
|---|---|
| `statement` / `kind` | what is claimed; machine-kind for routing |
| `confidence` | 0–0.99; evidence moves it, ±0.08/−0.10 per entry |
| `evidence_for` / `evidence_against` | bounded audit lists |
| `test` | discriminating check `{name, command, distinguishes[]}` |
| `test_result` | `{ran_at, passed, output}` — persisted, not lost |
| `status` | `proposed → testing → supported/weakened → confirmed/rejected` |
| `incident_id` / `mission_id` | scope |

- `record_test(passed)` — pass → `supported` (+0.12); fail →
  `weakened` (−0.15). The *why* stays on the row.
- `pick_discriminating(incident_id)` — prefers the declared test that
  rules out the most competing hypotheses (its `distinguishes` list
  names the kinds a result eliminates). Cheap tests that split the
  space beat speculative edits.

## Causal Memory (`localcodeagent/causal.py`)

Durable `data/causal_memory.json` records in the spec's chain form:

```
symptom → root cause → mechanism → fix → verification
```

`priors(symptom, subsystem)` returns hypothesis-shaped dicts
(`source: "causal_memory"`) ranked by token-overlap similarity and
reliability — seeded *into* `Diagnostician` output as candidates to
test, never assumed. A failed prior ranks below a successful one;
recurrence bumps trust by ≤0.10 total, capped at 0.85 confidence so a
stale record can never beat fresh deterministic evidence.

## Decision Journal (`localcodeagent/decisions.py`)

Durable `data/decisions.json`. `record()` captures `problem`,
`alternatives`, `evidence`, `decision`, `expected_outcome`; `outcome()`
closes the loop with `actual_outcome`, `reviewer_result`, `lessons`.
Self-repair journals its diagnosis choice (repair kind + top hypothesis
with the alternatives that were on the table) on every incident.

## Pipeline wiring

`SelfRepairCoordinator` accepts `hypotheses=`, `causal=`, `decisions=`
(all optional — an unwired coordinator behaves exactly as before):

- `_stage_diagnose` merges causal priors into the ranked hypothesis
  list, upserts rows into the HypothesisStore, and journals the choice.
- `_stage_promote` (resolved) confirms the winning hypothesis, rejects
  the losers, and writes the causal record (symptom → mechanism →
  procedure → verification evidence).

## API

| Route | Purpose |
|---|---|
| `GET /api/hypotheses` | list + summary + `discriminating` pick |
| `POST /api/hypotheses` | propose `{statement, kind, confidence, test}` |
| `POST /api/hypotheses/status` | `{id, action: confirm\|reject}` or `{status, detail}` |
| `POST /api/hypotheses/evidence` | `{id, supporting, detail, ref}` |
| `POST /api/hypotheses/test` | `{id, passed, output, test}` |
| `GET /api/causal-memory` | records + summary |
| `GET /api/decisions` | list + summary |
| `POST /api/decisions` | journal entry |
| `POST /api/decisions/outcome` | close with actual/reviewer/lessons |
