# Reliability Scoring & Capability Health

Every measurable subject — `tool:`, `model:`, `workflow:`, `plugin:`,
`repair:`, `capability:` — accumulates durable outcome metrics in
`data/reliability.json`: success/failure counts, cumulative latency,
retries, per-class error histograms, a rolling 10-call window, and a
consecutive-failure counter.

## Reliability score

`reliability = 0.6 · recent_rate + 0.4 · success_rate` — recent
behavior weighs more, but history still counts. When two tools offer
the same capability, routing prefers the more reliable one (the tool
router's learned-score path now feeds this same store).

**Nothing is permanently blacklisted.** `broken`/`degraded` derive from
the bounded recent window — a recovering subject heals on its next good
calls.

## Status tiers

| Status | Rule |
|---|---|
| `verified` | ≥5 calls, ≥80% success, recent ≥80% |
| `available` | some signal, not degraded |
| `degraded` | recent window <70% |
| `broken` | recent <34% or 3+ consecutive failures |
| `untested` | no recorded calls |
| `disabled` | administratively off |
| `unavailable` | no backing implementation |

## Capability health (`CapabilityHealth`)

User-visible capabilities wrap the tracker plus optional lightweight
self-tests:

```python
capabilities.register("tts", tools=["tts_speak"],
                      probe=lambda: {"ok": play_test_audio()})
capabilities.probe("tts")      # records outcome into the tracker
capabilities.status("tts")     # → {status, tools[], probe, has_self_test}
```

A capability is only as healthy as its backing tools — a broken tool
downgrades a "verified" capability to "degraded"; all tools disabled →
`disabled`; nothing backing it → `unavailable`.

Every routed tool execution (`ToolRouter`) records both the chosen
`tool:` subject and the `capability:` outcome, so health emerges from
real usage, not just probes.

## API

| Route | Purpose |
|---|---|
| `GET /api/reliability` | all subjects + tier summary |
| `GET /api/reliability?subject=tool:x` | one subject's metrics |
| `POST /api/reliability/record` | `{subject, ok, latency_s, error_class, retries}` |
| `GET /api/capabilities` | capability → status map + counts |
| `GET /api/capabilities?name=…` | one capability (tools + probe) |
| `POST /api/capabilities/probe` | run a capability's self-test |
