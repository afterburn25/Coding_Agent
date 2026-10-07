"""Unified developer performance timeline (backlog §20).

One bounded ring of timestamped stage marks across four lanes:

- ``startup``  host → backend → config → state → health → interface
- ``chat``     send → route → model → first token → render/done
- ``voice``    text ready → filter → synthesis → audio start
- ``gpu``      owner → model → VRAM reservation → peak → release

``route_event`` mirrors the server's existing event bus — every
task/voice/activity/model event already flows through one publish
funnel, so the timeline rides it instead of adding instrumentation at
every call site. ``mark`` is for stages that have no bus event (boot
milestones). Reads are lock-free snapshots for ``/api/perf/timeline``.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class PerfTrace:
    LANES = ("startup", "chat", "voice", "gpu")

    # Bus event type -> lane. Stages are derived from payload fields.
    _EVENT_LANES = {
        "task": "chat",
        "voice": "voice",
        "model": "gpu",
        "worker": "gpu",
    }

    def __init__(self, *, max_events: int = 400) -> None:
        self._events: deque[dict] = deque(maxlen=max(50, int(max_events)))
        self._lock = threading.Lock()
        self._boot0 = time.monotonic()

    # -- direct marks --------------------------------------------------

    def mark(self, lane: str, stage: str, *, detail: str = "",
             ts: float | None = None) -> None:
        if lane not in self.LANES:
            return
        with self._lock:
            self._events.append({
                "lane": lane,
                "stage": str(stage)[:80],
                "detail": str(detail)[:200],
                "ts": float(ts) if ts is not None else time.time(),
                "mono": time.monotonic(),
            })

    def boot_mark(self, stage: str, *, detail: str = "") -> None:
        """Startup-lane mark with a `t=` relative to process boot."""
        self.mark("startup", stage,
                  detail=detail or f"t+{time.monotonic() - self._boot0:.2f}s")

    # -- event-bus mirroring -------------------------------------------

    def route_event(self, etype: str, payload: dict) -> None:
        lane = self._EVENT_LANES.get(str(etype))
        if lane is None or not isinstance(payload, dict):
            return
        stage = self._stage_for(lane, payload)
        if stage:
            self.mark(lane, stage,
                      detail=str(payload.get("detail")
                                 or payload.get("text") or "")[:200])

    @staticmethod
    def _stage_for(lane: str, payload: dict) -> str:
        if lane == "chat":
            state = str(payload.get("state") or "")
            return {"queued": "send", "running": "model",
                    "completed": "done", "failed": "done",
                    "interrupted": "done"}.get(state, state or "")
        if lane == "voice":
            return str(payload.get("event") or payload.get("kind") or "")
        if lane == "gpu":
            return str(payload.get("event") or payload.get("state") or "")
        return ""

    # -- reads ----------------------------------------------------------

    def timeline(self, lane: str | None = None, *,
                 limit: int = 200) -> list[dict]:
        """Ordered marks with per-lane deltas from the previous stage."""
        with self._lock:
            rows = [e for e in self._events
                    if lane is None or e["lane"] == lane]
        rows = rows[-max(1, int(limit)):]
        last: dict[str, float] = {}
        out: list[dict] = []
        for e in rows:
            delta = e["mono"] - last.get(e["lane"], e["mono"])
            last[e["lane"]] = e["mono"]
            out.append({
                "lane": e["lane"], "stage": e["stage"],
                "detail": e["detail"], "ts": e["ts"],
                "delta_s": round(max(0.0, delta), 3),
            })
        return out

    def summary(self) -> dict:
        """Per-lane stage->latest-delta rollup for the dev perf view."""
        with self._lock:
            rows = list(self._events)
        lanes: dict[str, dict] = {l: {} for l in self.LANES}
        last: dict[str, float] = {}
        for e in rows:
            lane = e["lane"]
            delta = e["mono"] - last.get(lane, e["mono"])
            last[lane] = e["mono"]
            lane_rows = lanes.setdefault(lane, {})
            lane_rows[e["stage"]] = {
                "count": lane_rows.get(e["stage"], {}).get("count", 0) + 1,
                "last_delta_s": round(max(0.0, delta), 3),
            }
        return {"events": len(rows), "lanes": lanes}
