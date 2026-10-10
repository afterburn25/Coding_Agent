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

---

# Cognitive Phase 1 — Strategy Router, Requirements Compiler, Assumption Ledger

## Reasoning Strategy Router (`localcodeagent/governor/metacognition.py`)

Before the op ladder is built, `classify_strategy()` reads the whole
utterance — question shape, modal verbs, policy flags — and emits a
primary reasoning strategy plus composable secondaries onto
`MetaAssessment.strategy` / `.strategies`. Deterministic; never a model
call. A short single-clause factual ask stays `direct_retrieval` so
simple questions pay no planning overhead.

| Shape | Strategy | Extra ops added |
|---|---|---|
| `14 × 37`, `calculate …` | `formal_math` | `run_formal_solver` |
| crash/error/"why does X fail" | `diagnostic` (+`causal`) | `generate_hypotheses`, `test_hypothesis` |
| "what happens if…" | `counterfactual` | `simulate_plan` |
| "schedule N models within 12 GB" | `optimization`/`constraint_solving` | `run_formal_solver` |
| "should we X or Y" | `comparative`/`decision_analysis` | `run_verifier`, `ask_critic` |
| "test whether"/"benchmark" | `experimental` | `run_experiment` |
| "like that bug before" | `analogy` | memory weighting |
| review/cross-check asks | `multi_agent_review` | `ask_critic`, `spawn_specialist` |

The strategy rides the same `IntelPlan` (`assessment.as_dict()`), flows
through `task.intel` and the `intel` SSE event — visible in the
Intelligence Inspector, never hidden reasoning.

## Formal solvers

`formal_math` maps to `run_formal_solver`. The install's existing
instrument is the thalamus math fast path (`brain/thalamus.py` —
whitelist-AST evaluation, exponent/finite guards, honest
division-by-zero), which already answers pure arithmetic asks
deterministically before any model lane. `run_formal_solver` stays
declared in the op catalog for heavier formal tooling (Z3/OR-Tools/
SymPy) and schedules once an execution lane declares the `solvers`
capability — it degrades cleanly until then.

## Requirements Compiler (`localcodeagent/requirements.py`)

`compile_requirement_spec(text)` splits user language into clauses and
classifies each: `MUST` / `SHOULD` / `MAY` / `MUST_NOT` / `ASSUMPTION`
/ `QUESTION` / `ACCEPTANCE_CRITERION` — prohibitions win over
imperatives ("don't change X" is `MUST_NOT`, not `MUST`). The user's
own phrasing is preserved; a compiled row never rewords intent.

`compile_to_store(store, text, scope_*, assumptions=)` persists the
actionable categories as `RequirementStore` rows (`MUST`/`MUST_NOT` at
high priority, `MUST_NOT` tagged with an `invariant_held` check) and
returns `unresolved.questions` for clarification plus `assumption_rows`
when an `AssumptionLedger` is wired — each assumption links back to the
created requirement ids as dependents.

## Assumption Ledger (`localcodeagent/assumptions.py`)

Durable `data/assumptions.json` (DocStore → `state.db`,
`domain="assumptions"`). Every meaningful plan assumption is a row:
text, scope, `dependents{decisions,requirements,missions,tasks,
procedures}`, `test`, evidence trail, and state
`untested → supported → verified | invalidated | superseded`.

- `invalidate(id)` → marks the row and returns the dependent ids — the
  targeted re-evaluation list, not a blind rebuild.
- `supersede(id, text)` → links old → new; both stay inspectable.
- `weakest(scope)` → the unknown-unknown probe: live assumptions ranked
  by least evidence, the investigation list before a decision.

## API

| Route | Purpose |
|---|---|
| `GET /api/assumptions` | list + summary + `weakest` probe |
| `POST /api/assumptions` | record `{text, scope_*, dependents, confidence, test}` |
| `POST /api/assumptions/state` | `{id, state\|action: invalidate\|supersede, evidence}` |
