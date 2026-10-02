"""Signal detectors — periodic, evidence-based problem and opportunity
scans that feed the self-repair pipeline and the mission backlog.

Each detector is a small pure function over an injected `sources`
mapping (server wiring binds real telemetry; tests bind fakes). A
detector returns zero or more *findings*:

    kind        stable identity ("crash_storm", "low_memory_hits", …)
    severity    critical|high|normal|low
    confidence  0..1 — from measured evidence only
    title/evidence  human-readable + the numbers behind it
    route       "repair"      → SelfRepairCoordinator.report_failure
                "mission"     → generate a bounded investigation mission
                "suggestion"  → stays in the findings store for the UI

Findings persist in the autonomy store and dedupe by (kind, signature):
a repeat sighting refreshes the existing row instead of re-routing, and
a cooldown stops the same detector from spamming missions.

Nothing here fabricates telemetry — a detector that cannot measure its
signal returns nothing rather than a guessed finding.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable

SEVERITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
ROUTES = {"repair", "mission", "suggestion"}

# Minimum interval between two routings of the same signature.
ROUTE_COOLDOWN_S = 3600.0
# Default scan cadence per detector.
SCAN_INTERVAL_S = 120.0


def new_finding(*, kind: str, title: str, severity: str = "normal",
                confidence: float = 0.5, evidence: dict | None = None,
                route: str = "suggestion", signature: str = "",
                detail: str = "") -> dict[str, Any]:
    now = time.time()
    return {
        "id": f"fn-{uuid.uuid4().hex[:10]}",
        "kind": str(kind)[:60],
        "title": str(title)[:160],
        "detail": str(detail)[:800],
        "severity": severity if severity in SEVERITY_RANK else "normal",
        "confidence": round(min(max(float(confidence), 0.0), 1.0), 3),
        "evidence": dict(evidence or {}),
        "route": route if route in ROUTES else "suggestion",
        "signature": signature or f"{kind}:{title[:80]}",
        "status": "open",        # open|acted|dismissed|resolved
        "first_seen": now, "last_seen": now, "sightings": 1,
        "routed_to": "",          # incident/mission id once routed
        "routed_at": None,
        "created_at": now, "updated_at": now,
    }


# ----------------------------------------------------------------------
# detectors — each returns a finding dict or None

def detect_crash_storm(sources: dict) -> dict | None:
    """≥N backend/runtime crashes inside the window."""
    crash_history = sources.get("crash_history")
    if not crash_history:
        return None
    try:
        rows = crash_history(200) or []
    except Exception:
        return None
    cutoff = time.time() - 3600
    recent = [r for r in rows if float(r.get("time") or 0) >= cutoff]
    if len(recent) < 3:
        return None
    return new_finding(
        kind="crash_storm", severity="high",
        confidence=min(0.95, 0.6 + 0.05 * len(recent)),
        title=f"{len(recent)} process crashes in the last hour",
        detail="Repeated process exits — likely a bad runtime config or "
               "recurring fault worth a repair incident.",
        evidence={"crashes_1h": len(recent),
                  "last": rows[-1] if rows else {}},
        route="repair", signature="crash_storm:hourly")


def detect_mission_failures(sources: dict) -> dict | None:
    """Mission failure rate or consecutive-failure streak over 24h."""
    missions = sources.get("missions")
    if not missions:
        return None
    try:
        rows = missions() or []
    except Exception:
        return None
    cutoff = time.time() - 86400
    recent = [m for m in rows
              if float(m.get("updated_at") or 0) >= cutoff]
    terminal = [m for m in recent if str(m.get("status")) in
                {"completed", "completed_with_warnings", "failed",
                 "cancelled"}]
    failed = [m for m in terminal if str(m.get("status")) == "failed"]
    if len(terminal) < 3 or len(failed) / max(1, len(terminal)) < 0.4:
        return None
    return new_finding(
        kind="mission_failure_rate", severity="high",
        confidence=round(len(failed) / len(terminal), 2),
        title=f"Mission failure rate {len(failed)}/{len(terminal)} "
              "in 24h",
        evidence={"failed": len(failed), "terminal": len(terminal),
                  "sample": [m.get("title", "")[:60]
                             for m in failed[:5]]},
        route="mission", signature="mission_failure_rate:24h")


def detect_answer_memory_decay(sources: dict) -> dict | None:
    """Answer Memory hit rate below floor with enough samples to matter."""
    stats_fn = sources.get("answer_memory_stats")
    if not stats_fn:
        return None
    try:
        stats = stats_fn() or {}
    except Exception:
        return None
    hits = int(stats.get("hits") or 0)
    misses = int(stats.get("misses") or 0)
    total = hits + misses
    if total < 50:
        return None
    hit_rate = hits / total
    if hit_rate >= 0.25:
        return None
    return new_finding(
        kind="low_memory_hits", severity="low",
        confidence=0.7,
        title=f"Answer Memory hit rate {hit_rate:.0%} "
              f"({hits}/{total})",
        detail="Low reuse means repeated model calls for known answers — "
               "an opportunity to tune retrieval or seed more memory.",
        evidence={"hits": hits, "misses": misses},
        route="suggestion", signature="low_memory_hits")


def detect_model_failures(sources: dict) -> dict | None:
    """Model-call failure rate above threshold across telemetry."""
    telemetry = sources.get("model_telemetry")
    if not telemetry:
        return None
    try:
        summary = telemetry() or {}
        rows = summary.get("models") or summary.get("rows") or []
        total = sum(int(r.get("samples") or 0) for r in rows)
        fails = sum(int(r.get("failures") or 0) for r in rows)
    except Exception:
        return None
    if total < 20 or fails / total < 0.15:
        return None
    worst = max(rows, key=lambda r: int(r.get("failures") or 0),
                default=None)
    return new_finding(
        kind="model_failure_rate", severity="normal",
        confidence=round(fails / total, 2),
        title=f"Model call failure rate {fails}/{total}",
        evidence={"samples": total, "failures": fails,
                  "worst_model": (worst or {}).get("id", "")},
        route="repair", signature="model_failure_rate")


def detect_disk_pressure(sources: dict) -> dict | None:
    """Free disk under the safety floor."""
    disk = sources.get("disk_free_gb")
    if not disk:
        return None
    try:
        free = float(disk())
    except Exception:
        return None
    if free >= 5.0:
        return None
    sev = "critical" if free < 1.0 else "high" if free < 2.0 else "normal"
    return new_finding(
        kind="disk_pressure", severity=sev, confidence=0.95,
        title=f"Disk free space low: {free:.1f} GB",
        evidence={"free_gb": round(free, 2)},
        route="repair", signature="disk_pressure")


def detect_repair_thrash(sources: dict) -> dict | None:
    """The same repair signature recurring resolved→new repeatedly —
    the fix isn't holding; escalate rather than re-run it forever."""
    repairs = sources.get("repairs")
    if not repairs:
        return None
    try:
        rows = repairs() or []
    except Exception:
        return None
    from collections import Counter
    counts = Counter(r.get("signature") for r in rows)
    for sig, n in counts.items():
        if sig and n >= 3:
            return new_finding(
                kind="repair_thrash", severity="high", confidence=0.85,
                title=f"Same failure signature seen {n}× — repair not "
                      "holding",
                detail="A recurring signature means the fix addresses "
                       "symptoms; investigate the root cause.",
                evidence={"signature": sig, "incidents": n},
                route="mission", signature=f"repair_thrash:{sig[:80]}")
    return None


def detect_startup_regression(sources: dict) -> dict | None:
    """Last startup materially slower than the learned profile."""
    fn = sources.get("startup_ms")
    if not fn:
        return None
    try:
        data = fn() or {}
        total = float(data.get("total_ms") or 0)
        expected = float(data.get("expected_ms") or 0)
    except Exception:
        return None
    if not expected or total < expected * 1.6 or total < 3000:
        return None
    return new_finding(
        kind="startup_regression", severity="normal", confidence=0.7,
        title=f"Startup {total/1000:.1f}s vs expected "
              f"{expected/1000:.1f}s",
        evidence={"total_ms": total, "expected_ms": expected},
        route="suggestion", signature="startup_regression")


def detect_approval_backlog(sources: dict) -> dict | None:
    """Pending approvals sitting >24h — user may want a standing grant."""
    fn = sources.get("pending_approvals")
    if not fn:
        return None
    try:
        rows = fn() or []
    except Exception:
        return None
    stale = [r for r in rows
             if time.time() - float(r.get("created_at") or 0) > 86400]
    if not stale:
        return None
    return new_finding(
        kind="approval_backlog", severity="low", confidence=0.8,
        title=f"{len(stale)} approval(s) waiting over a day",
        detail="Recurring approvals for the same action could become "
               "scoped standing grants.",
        evidence={"stale": len(stale)},
        route="suggestion", signature="approval_backlog")


DETECTORS: list[Callable[[dict], dict | None]] = [
    detect_crash_storm,
    detect_mission_failures,
    detect_model_failures,
    detect_disk_pressure,
    detect_repair_thrash,
    detect_answer_memory_decay,
    detect_startup_regression,
    detect_approval_backlog,
]


class SignalScanner:
    """Runs detectors on a bounded cadence, persists findings, and hands
    routable findings to the supervisor's sinks."""

    def __init__(self, store, *,
                 sources: dict[str, Callable] | None = None,
                 detectors: list[Callable] | None = None,
                 interval_s: float = SCAN_INTERVAL_S,
                 route: Callable[[dict], str] | None = None):
        self._store = store
        self.sources = dict(sources or {})
        self.detectors = list(detectors or DETECTORS)
        self.interval_s = float(interval_s)
        self._route = route            # finding -> routed id or ""
        self._last_scan = 0.0

    # ------------------------------------------------------------------
    def _rows(self) -> list[dict]:
        return self._store.findings.data.setdefault("findings", [])

    def _save(self) -> None:
        self._store.findings.save()

    def list(self, *, include_closed: bool = False) -> list[dict]:
        rows = [dict(r) for r in self._rows()
                if include_closed or r.get("status") in {"open", "acted"}]
        rows.sort(key=lambda r: (SEVERITY_RANK.get(r.get("severity"), 9),
                                 -(r.get("last_seen") or 0)))
        return rows

    def dismiss(self, finding_id: str) -> bool:
        for r in self._rows():
            if r.get("id") == finding_id:
                r["status"] = "dismissed"
                r["updated_at"] = time.time()
                self._save()
                return True
        return False

    # ------------------------------------------------------------------
    def tick(self, now: float | None = None) -> list[dict]:
        """Bounded scan — at most once per interval."""
        now = now or time.time()
        if now - self._last_scan < self.interval_s:
            return []
        self._last_scan = now
        routed = []
        for det in self.detectors:
            try:
                finding = det(self.sources)
            except Exception:
                continue            # a bad detector must never stall a tick
            if finding is None:
                continue
            existing = self._dedupe(finding, now)
            if existing is None:     # suppressed by cooldown
                continue
            routed.append(existing)
        return routed

    def _dedupe(self, finding: dict, now: float) -> dict | None:
        """Merge into an existing open/recent finding, or insert new.
        Returns the row to route, or None when suppressed."""
        for row in self._rows():
            if row.get("signature") != finding["signature"]:
                continue
            row["last_seen"] = now
            row["sightings"] = int(row.get("sightings") or 0) + 1
            row["confidence"] = max(row.get("confidence") or 0,
                                    finding["confidence"])
            row["updated_at"] = now
            fresh = (row.get("status") == "open"
                     and not row.get("routed_to")
                     and row.get("route") != "suggestion")
            cooled = (row.get("status") in {"acted", "open"}
                      and row.get("routed_to")
                      and now - float(row.get("routed_at") or 0)
                      > ROUTE_COOLDOWN_S)
            if cooled:
                row["status"] = "open"
                row["routed_to"] = ""
            self._save()
            if fresh or cooled:
                return self._route_finding(row, now)
            return None
        self._rows().append(finding)
        self._save()
        return self._route_finding(finding, now)

    def force(self, now: float | None = None) -> list[dict]:
        """Run a scan now, ignoring the interval gate."""
        self._last_scan = 0.0
        return self.tick(now)

    def _route_finding(self, row: dict, now: float) -> dict:
        if self._route is None or row.get("route") == "suggestion":
            return row
        rid = self._route(dict(row))
        if rid:
            row["routed_to"] = rid
            row["routed_at"] = now
            row["status"] = "acted"
            self._save()
        return row
