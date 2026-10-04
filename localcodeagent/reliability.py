"""Persisted reliability scoring + capability health tiers.

Every measurable subject — a tool, model, workflow, plugin, repair
strategy, build adapter, image workflow — gets a durable metrics row:
success/failure counts, latency, retries, error classes, and a rolling
recent window. `status()` maps metrics onto the spec's tiers:

    verified  — calls≥5, success_rate≥0.8, recent≥0.8
    available — some signal, not degraded
    degraded  — recent window below 0.7
    broken    — recent window below 0.34 or 3+ consecutive failures
    untested  — no recorded calls
    disabled  — administratively off (caller-supplied flag)
    unavailable — caller says the subject isn't installed/reachable

Nothing is blacklisted permanently: `broken`/`degraded` derive from a
bounded recent window, so a recovering subject heals itself on the next
good calls.

`CapabilityHealth` layers user-visible capability status over the
tracker + tool registry + lightweight self-tests: `register(name,
tools=…, probe=callable)` then `probe(name)` runs the check and feeds
the tracker.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text

CAP_STATUSES = {"verified", "available", "degraded", "untested",
                "broken", "disabled", "unavailable"}

_WINDOW = 10          # recent-call window that drives status
_MIN_VERIFIED = 5     # calls before "verified" is possible
_HISTORY_BOUND = 400  # distinct subjects


class ReliabilityTracker:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "subjects": {}}
        self.data.setdefault("subjects", {})

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    # ------------------------------------------------------------------
    def record(self, subject: str, *, ok: bool, latency_s: float = 0.0,
               error_class: str = "", retries: int = 0) -> dict:
        subject = str(subject)[:120]
        with self._lock:
            subj = self.data["subjects"].setdefault(subject, {
                "calls": 0, "ok": 0, "failed": 0, "total_latency_s": 0.0,
                "retries": 0, "error_classes": {}, "window": [],
                "consecutive_failures": 0, "first_seen": time.time(),
                "last_seen": 0.0})
            subj["calls"] += 1
            subj["ok" if ok else "failed"] += 1
            subj["total_latency_s"] = round(
                subj["total_latency_s"] + max(float(latency_s), 0.0), 3)
            subj["retries"] += max(int(retries), 0)
            subj["last_seen"] = time.time()
            if ok:
                subj["consecutive_failures"] = 0
            else:
                subj["consecutive_failures"] += 1
                if error_class:
                    ec = subj["error_classes"]
                    ec[str(error_class)[:60]] = ec.get(
                        str(error_class)[:60], 0) + 1
            subj["window"].append(bool(ok))
            subj["window"] = subj["window"][-_WINDOW:]
            # Bound subject count — oldest untouched subjects evict.
            if len(self.data["subjects"]) > _HISTORY_BOUND:
                oldest = sorted(
                    self.data["subjects"].items(),
                    key=lambda kv: kv[1].get("last_seen", 0))
                for name, _ in oldest[:len(self.data["subjects"])
                                      - _HISTORY_BOUND]:
                    del self.data["subjects"][name]
            self._save()
            return dict(subj)

    # ------------------------------------------------------------------
    def score(self, subject: str) -> dict:
        subj = self.data["subjects"].get(str(subject))
        if not subj:
            return {"subject": subject, "calls": 0,
                    "status": "untested", "reliability": None}
        calls = subj["calls"]
        win = subj.get("window") or []
        recent_rate = (sum(win) / len(win)) if win else 0.0
        success_rate = subj["ok"] / calls if calls else 0.0
        reliability = round(0.6 * recent_rate + 0.4 * success_rate, 3)
        avg_lat = (subj["total_latency_s"] / calls) if calls else 0.0
        return {
            "subject": subject,
            "calls": calls, "ok": subj["ok"], "failed": subj["failed"],
            "success_rate": round(success_rate, 3),
            "recent_rate": round(recent_rate, 3),
            "reliability": reliability,
            "avg_latency_s": round(avg_lat, 3),
            "retries": subj["retries"],
            "error_classes": dict(subj.get("error_classes") or {}),
            "consecutive_failures": subj.get("consecutive_failures", 0),
            "last_seen": subj.get("last_seen", 0.0),
            "status": self.status(subject),
        }

    def status(self, subject: str, *, disabled: bool = False,
               unavailable: bool = False) -> str:
        if disabled:
            return "disabled"
        if unavailable:
            return "unavailable"
        subj = self.data["subjects"].get(str(subject))
        if not subj or not subj.get("calls"):
            return "untested"
        win = subj.get("window") or []
        recent_rate = (sum(win) / len(win)) if win else 1.0
        if subj.get("consecutive_failures", 0) >= 3 \
                or (len(win) >= 5 and recent_rate < 0.34):
            return "broken"
        if len(win) >= 3 and recent_rate < 0.7:
            return "degraded"
        if subj["calls"] >= _MIN_VERIFIED \
                and subj["ok"] / subj["calls"] >= 0.8 \
                and recent_rate >= 0.8:
            return "verified"
        return "available"

    def all(self) -> dict[str, dict]:
        return {name: self.score(name)
                for name in self.data["subjects"]}

    def summary(self) -> dict:
        subs = self.all()
        by: dict[str, int] = {}
        for s in subs.values():
            by[s["status"]] = by.get(s["status"], 0) + 1
        return {"subjects": len(subs), "by_status": by}


class CapabilityHealth:
    """User-visible capability status over the tracker + registry.

    A capability is 'verified' when its self-test passes and/or its
    backing tools are reliable; 'broken'/'degraded' follow the tracker;
    'disabled' when all backing tools are off; 'unavailable' when no
    backing tool exists at all.
    """

    def __init__(self, tracker: ReliabilityTracker, *,
                 registry: Any = None):
        self.tracker = tracker
        self.registry = registry      # ToolRegistry (optional)
        self._probes: dict[str, Callable[[], Any]] = {}
        self._declared: dict[str, dict] = {}
        self._lock = threading.RLock()

    def register(self, name: str, *, tools: list[str] | None = None,
                 probe: Callable[[], Any] | None = None,
                 disabled: bool = False) -> dict:
        name = str(name)[:80]
        self._declared[name] = {"tools": list(tools or []),
                                "disabled": bool(disabled)}
        if probe is not None:
            self._probes[name] = probe
        return self.status(name)

    def probe(self, name: str) -> dict:
        """Run a capability's self-test (if registered) and record the
        outcome. Returns {ok, latency_s, detail}."""
        fn = self._probes.get(name)
        if fn is None:
            return {"ok": False, "detail": "no self-test registered",
                    "latency_s": 0.0}
        started = time.time()
        try:
            res = fn()
            ok = bool(res) if not isinstance(res, dict) \
                else bool(res.get("ok"))
            detail = res.get("detail", "") if isinstance(res, dict) \
                else ""
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        lat = round(time.time() - started, 3)
        self.tracker.record(f"capability:{name}", ok=ok,
                            latency_s=lat,
                            error_class="" if ok else "self_test")
        return {"ok": ok, "latency_s": lat, "detail": str(detail)[:300]}

    def status(self, name: str) -> dict:
        name = str(name)
        decl = self._declared.get(name, {})
        tools = list(decl.get("tools") or [])
        if not tools and self.registry is not None:
            try:
                tools = self.registry.find_by_capability(name)
            except Exception:
                tools = []
        tool_rows = []
        disabled = decl.get("disabled", False)
        all_disabled = bool(tools) and all(
            not self.registry.is_enabled(t) for t in tools) \
            if self.registry is not None and tools else False
        for t in tools:
            tool_rows.append({
                "tool": t,
                "enabled": self.registry.is_enabled(t)
                if self.registry is not None else True,
                "status": self.tracker.status(f"tool:{t}"),
            })
        probe = self.tracker.score(f"capability:{name}")
        if disabled or all_disabled:
            st = "disabled"
        elif not tools and name not in self._declared \
                and probe["calls"] == 0:
            st = "unavailable"
        elif probe["calls"] == 0 and not any(
                self.tracker.data["subjects"].get(f"tool:{t}", {})
                .get("calls") for t in tools):
            st = "untested" if (tools or name in self._probes) \
                else "unavailable"
        else:
            # Combine capability self-test history with the worst of its
            # backing tools — a capability is only as healthy as what
            # serves it.
            st = self.tracker.status(f"capability:{name}")
            tool_st = [self.tracker.status(f"tool:{t}") for t in tools]
            if "broken" in tool_st and st in {"verified", "available"}:
                st = "degraded"
            if st == "untested" and tool_st:
                if "broken" in tool_st:
                    st = "degraded"
                elif "verified" in tool_st:
                    st = "available"
        return {"name": name, "status": st, "tools": tool_rows,
                "probe": probe, "has_self_test": name in self._probes}

    def summary(self, names: list[str] | None = None) -> dict:
        if names is None:
            names = sorted(set(self._declared) | set(self._probes) | {
                s.split(":", 1)[1] for s in self.tracker.data["subjects"]
                if s.startswith("capability:")})
            if self.registry is not None:
                try:
                    names = sorted(set(names) | set(
                        c for spec in
                        getattr(self.registry, "_tools", {}).values()
                        for c in spec.capabilities))
                except Exception:
                    pass
        rows = {n: self.status(n) for n in names}
        by: dict[str, int] = {}
        for r in rows.values():
            by[r["status"]] = by.get(r["status"], 0) + 1
        return {"capabilities": rows, "by_status": by}
