# Startup & Splash Progress Architecture

Nexus Core's splash screen renders real startup — not a fake timer. This
document describes how the bar moves, why it never jumps, and where the
local timing profile lives.

## Readiness gating (unchanged contract)

The splash closes only when **both** are true:

- `MinimumDisplayTime` (7 s) has elapsed, and
- the application is genuinely ready: backend healthy → runtime sync →
  WebView2 initialized → frontend `nexus-core-ready` handshake →
  `MarkAppReady()`.

After readiness + minimum elapsed, the bar finishes to exactly 100 %, then
the existing READY/glow completion effect (~850 ms) plays, then the splash
hands off to the main window in the same UI turn. Long startups simply keep
the splash visible past 7 s; fast startups pace the finish until the minimum.

Fatal startup failure swaps to the actionable failure state (Retry /
Open Log / Exit) and freezes the bar — no endless crawl.

## Three-layer progress model

`desktop/ChatNexus.Desktop/StartupProgress.cs` keeps three independent
values:

| Layer | Property | Meaning |
|---|---|---|
| Real milestone progress | `RealProgress` | Highest fraction actually reported by startup (0.06 boot → 0.15 desktop init → 0.30 backend launched → 0.55 backend healthy → 0.72 WebView init → 0.85 page loading → 0.93 awaiting handshake → 1.0 ready). Drives phases and readiness; **never rendered directly**. |
| Predicted progress | `PredictedProgress` | Where the bar should be inside the current phase, given elapsed time vs the expected phase duration (local profile EMA or conservative default). Asymptotically approaches the phase ceiling — keeps creeping while waiting, decelerates as the phase overruns. |
| Displayed progress | `DisplayedProgress` | The only rendered value. Velocity-smoothed chase of the predictive target, integrated with real `dt` per tick. |

Rules enforced by construction:

- `DisplayedProgress` never decreases and never snaps to `RealProgress`.
- `DisplayedProgress < 1.0` until `AppReady` **and** minimum elapsed.
- Each phase has a ceiling just below the next milestone (≈1 % under, hard
  cap `0.985` before readiness), so the bar can creep while waiting but
  never claim work that hasn't happened.
- `ReadyToDismiss` additionally requires `DisplayedProgress >= 0.9995` —
  the READY transition only fires after the bar has visibly completed.

## Animation

Per `Tick()`:

```
target          = min(phase_ceiling, max(milestone, predicted))
desiredVelocity = (target − displayed) / responseTime
velocity       += (desiredVelocity − velocity) · (1 − e^(−dt/velTau))
displayed      += velocity · dt        // clamped to target, monotonic
```

`dt` is measured wall-clock (`dt = now − lastTick`, clamped to 250 ms), so
16/33/50/100 ms timer intervals produce the same trajectory — a delayed UI
tick is one bigger step, not a different animation. Milestone jumps raise
`target`, which raises `desiredVelocity`, which the velocity lag smooths
into a visible accelerate→decel S-curve instead of a snap. During the final
finish (`AppReady && MinimumElapsed`) the response/tau constants tighten so
the last 1–2 % completes crisply (~0.5 s) into exactly 1.0.

Windows reduced-motion (`SPI_GETCLIENTAREAANIMATION`) softens both
constants (~1.7×) — gentler interpolation, same real progress reporting.

## Stall-aware status

The bar may predict; the text must stay factual. Primary/secondary labels
always come from real `Report()` calls. When a phase exceeds ~2.5× its
expected duration (min 6 s) the secondary line switches to honest wording —
"Backend startup is taking longer than usual", escalating past ~6× to
"Still waiting for backend health". It never claims an error before the
real timeout/failure path does.

## Local startup timing profile

Phases are timed between milestone transitions and persisted once at
completion (never per frame) to `data/startup_profile.json`:

```json
{
  "schema": 1,
  "app_version": "0.9.0",
  "cpu_cores": 24,
  "ram_gb": 63.9,
  "samples": 9,
  "updated_utc": 1749000000,
  "phases": {
    "backend_health": {"ema": 4.2, "min": 2.1, "max": 8.0, "last": 3.9, "n": 5}
  }
}
```

- EMA (α = 0.3) per phase; a sample >4× the current EMA with ≥4 prior
  samples is damped to α = 0.08 so one AV-scan startup can't wreck the
  estimate.
- Missing / corrupt / schema-mismatched / >90-day-stale profiles fall back
  to conservative defaults.
- A different major/minor version or a radically different CPU core count
  halves sample trust instead of discarding history.
- Timings are **advisory** — they pace animation only and never decide
  readiness.

A structured one-line timing summary is appended to `data/logs/backend-host.log`
at completion (`[startup] backend_health=3812ms … total=9912ms`), and the
last run lands in `data/startup_last.json` for diagnostics.

## Tests

`desktop/StartupProgress.Tests` is a dependency-free console harness that
compile-includes the model and drives it with a fake clock: no-jump,
monotonicity, phase ceilings, readiness finish, min-display gating, long
startup, deceleration/acceleration, 16/33/50/100 ms dt-equivalence, and the
profile's missing/corrupt/outlier/roundtrip behavior. It runs in the
`windows-desktop` CI job before the packaging build.
