# Data Lineage & Artifact Versioning

## Lineage (`localcodeagent/lineage.py`)

`LineageStore` (`data/lineage.json`, bounded at 1000 records) records
what contributed to each produced target so provenance is inspectable
rather than implied.

Each record:

- `target_id`, `target_kind` — `answer` / `report` / `code_change` /
  `artifact` / `decision` / `mission` / `other`
- `contributors` — `{kind, ref, note}` where kind ∈ `file`, `memory`,
  `answer_memory`, `web`, `tool_output`, `worker`, `reviewer`,
  `decision`, `requirement`, `artifact`, `user`, `other`
- `project_id`, `mission_id`, `task_id`, `note`, `created_at`

Unknown contributor kinds normalize to `other` rather than being
rejected — lineage records what producers actually report.

## Artifact versioning (`localcodeagent/artifacts.py`)

Every registered artifact carries `project_id` and
`requirement_ids` alongside the existing kind/path/sha256/creator/
mission/task/provenance fields. Same-filename registrations append a
new version row instead of overwriting history:

- `versions(name)` → all versions oldest → newest
- `latest(name)` → newest row
- `verify(id)` → existence + sha256 match; silent modification returns
  `{"ok": false, "reason": "hash mismatch (modified)"}`

When a `LineageStore` is attached (`artifacts.lineage`), `register`
emits a lineage record per artifact with contributors derived from
`tool`, `creator`, `provenance.inputs`, and `requirement_ids`.

## API

- `GET  /api/lineage?kind=&project=&mission=` — records + kind counts
- `GET  /api/lineage/<target_id>` — every record for one target
- `POST /api/lineage` `{target_id, target_kind, contributors, ...}` —
  record lineage manually
- `GET  /api/artifacts/versions/<name>` — version history + latest row
