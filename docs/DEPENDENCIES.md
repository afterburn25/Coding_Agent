# Dependency Intelligence & Environment Manifests

## Dependencies (`localcodeagent/dependencies.py`)

`DependencyStore` (`data/dependencies.json`, bounded at 500) tracks
per-ecosystem rows: `installed_version`, `available_version`,
`compatible`, `advisory`, `impact`, `test_coverage`, `project_id`.
`outdated` derives from a real version comparison — no string-equal
shortcuts.

**Staged upgrades** — never blind production upgrades:

```
stage_upgrade(dep, candidate)
  → isolated_env → install → build → tests → review
  → conclude_upgrade(promote|reject)
```

A `failed` stage locks the attempt (`status=failed`) — terminal.
`promote` only succeeds once every stage is `passed`/`skipped`, and it
writes the candidate version onto the dependency row. `reject` keeps
the installed version.

## Environment manifests (`localcodeagent/environment.py`)

`EnvironmentStore` (`data/environment.json`) declares per-project
requirements: `{name, requirement, kind}` where requirement is `''`
(any), `>=x.y`, `==x.y.z`, or `x.x` prefix.

- `detect(names)` — probes the live host (python, node, dotnet, git,
  npm, pip built in; arbitrary tools via injectable probe).
- `verify(project_id)` — per component: `satisfied` / `missing` /
  `mismatched` (with required vs installed versions). "Recreate this
  environment" becomes a verifiable checklist.

## API

- `GET  /api/dependencies?ecosystem=&outdated=1&project=`
- `POST /api/dependencies/track` `{name, ecosystem, versions, ...}`
- `POST /api/dependencies/upgrade` `{dependency_id, candidate_version}`
- `POST /api/dependencies/upgrade/stage` `{upgrade_id, stage, status}`
- `POST /api/dependencies/upgrade/conclude` `{upgrade_id, promote}`
- `GET  /api/environment[?names=a,b]` — host detection
- `POST /api/environment/manifest` `{project_id, components}`
- `GET  /api/environment/manifest/<p>` / `verify/<p>`
