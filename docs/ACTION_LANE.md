# Action Lane — deterministic local-action execution

Nexus is an execution agent, not a narrator. The action lane turns
bounded computer-task phrasings into a real lifecycle:

```
intent → plan → capability → permission → execute → verify → evidence → truthful reply
```

Success language in a reply is only legitimate when a `verified`
ledger entry or a tool `*_OK` result exists. A plan, a permission
grant, and a queued op are not evidence.

## Components

| Piece | File | Role |
|---|---|---|
| Parser | `localcodeagent/action_ops.py` | Bounded grammar → `ActionPlan`; `None` = out of grammar (model lane); `clarify` = intent certain, target missing |
| Executor | `action_ops.execute_plan()` | capability → permission → execute → verify → reply text (always describes what happened) |
| Ledger | `localcodeagent/action_ledger.py` | Durable `data/action_ledger.json`; `begin()/finish()` per action; `run_filesystem`/`verify_filesystem` shared contracts |
| Lane | `agent/orchestrator.py::_local_action_reply/_local_action_multi/_resume_local_action` | Claims unclaimed auto-mode turns; parks approvals on the task; resumes them |
| Tools | `tools/filesystem.py` | `fs_mkdir`, `fs_delete`, `fs_move`, `fs_copy`, verified `write_file` (`WROTE_OK`) |
| Eval | `scripts/eval_action_lane.py` | 30+ phrasings → expected outcome class; classifies from ledger evidence; CI gate |

## Grammar

- `mkdir`/`create|make folder|directory|dir <path>` (also `create <name> folder`)
- `write|save "<content>" to|into|in <file>`; `create file [named] <path> [containing "<content>"]`
- `delete|remove|erase|rm|rmdir <path>` (`recursive`/`and its contents` → recursive)
- `move|rename|copy|duplicate <src> to|into|inside|as <dst>`;
  single-operand forms clarify the missing destination
- Outside-workspace absolute paths are allowed **only** via explicit
  approval, always.

Deliberate fall-throughs (model lane owns them):

- Anything outside the grammar — questions, code requests, ambiguous
  paths.
- `write 'x'` with no destination (ambiguous: write text vs file).
- Pronoun tails: `copy that`, `move it` (acknowledgments / no target).
- A path tail containing an embedded second action clause
  (`create folder alpha and make dir beta`) — `_embedded_op()` catches
  classifier-missed compounds so no command fragment executes as a
  literal path.
- Compound turns where ANY clause fails to parse — the whole turn
  goes to the model; never partial execution on a guess.

## Ledger statuses

`recorded` → `awaiting_approval` | `denied` | `failed` | `verified` |
`unverified` | `clarify` | `unavailable` | `claim`

- `unavailable` on the *first* clause returns `None` (model lane may
  reach the goal another way); mid-sequence it stops honestly — no
  silent tail drops.
- `recover_orphans()` at boot closes crash-interrupted `recorded`
  entries as `unverified`; only `awaiting_approval` may park across
  restarts.
- Fabricated model claims write `claim`/`unverified` entries — the
  truth gate's findings are durable evidence too.
- Mission node outcomes write `mission_node` entries;
  `mission_rollup(mission_id)` powers `GET /api/missions/<id>/evidence`.

## Verification contracts (`verify_filesystem`)

| kind | post-condition |
|---|---|
| mkdir | target exists and `is_dir()` |
| write | file exists; `expected_size`/`sha256` when supplied |
| copy | destination exists + size parity with source |
| move/rename | destination exists, source absent |
| delete | target absent |

## Approvals

`awaiting_approval` parks a `local_action` pending payload on the task
(session-less — survives restart; params re-coerced on resume).
Approval executes the same verified path; denial records `denied` and
reports "nothing was changed". Compound sequences park their remaining
clauses on the approval (`plan.remaining`); resume continues the tail,
denial cancels it.

## Invariants (do not regress)

1. The lane never intercepts mission turns (`mission_id`) or claimed
   self-knowledge turns (genuine capability questions).
2. `clarify` > guess: a missing or ambiguous target always asks.
3. One unparseable compound clause hands the WHOLE turn to the model.
4. The sequence stops at the first gate — nothing past an
   approval/failure/denial/clarify executes undecided.
5. Every new grammar verb needs tests for: executes+verifies, denied,
   parked, failed, clarify-vs-guess.
6. `GET /api/action-ledger` is the ledger endpoint — `/api/actions`
   belongs to the self-knowledge capability catalog.
