"""Dependency-aware mission task graph + resource locks + execution leases.

Graph lives on the mission record ("graph": {"nodes": [...]}) so DAG state
persists with the mission and survives restart for free.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

TASK_STATES = {
    "planned", "ready", "running", "waiting_dependency", "waiting_approval",
    "blocked", "verifying", "completed", "failed", "cancelled", "skipped",
}
TERMINAL_TASK_STATES = {"completed", "failed", "cancelled", "skipped"}

# Known exclusive resources — two running tasks may never share one.
RESOURCE_LOCKS = {
    "workspace_write", "git_write", "model_gpu", "comfy_gpu",
    "installer", "package_manager", "database_migration", "network_heavy",
}

# Default lock per task kind — agent tasks mutate the shared task lane and
# workspace, so they serialize through the lane; verify tasks serialize on
# workspace_write (they mutate build/test state); reads/research run free.
KIND_DEFAULT_LOCK = {
    "agent": "agent_lane",
    "verify": "workspace_write",
    "research": "network_heavy",
    "internal": "",
    "wait": "",
}


def new_task(title: str, instruction: str, *, kind: str = "agent",
             deps: list[str] | None = None, priority: int = 50,
             model_role: str = "", tools: list[str] | None = None,
             max_retries: int = 2, lock: str = "",
             verify: str = "none", created_by: str = "planner",
             metadata: dict | None = None) -> dict[str, Any]:
    return {
        "id": f"t-{uuid.uuid4().hex[:10]}",
        "title": str(title or instruction)[:140],
        "instruction": str(instruction)[:8000],
        "kind": kind if kind in {"agent", "verify", "research", "internal", "wait"} else "agent",
        "state": "planned",
        "deps": list(deps or []),
        "priority": int(priority),
        "model_role": model_role,
        "tools": list(tools or []),
        "lock": lock if lock else KIND_DEFAULT_LOCK.get(kind, ""),
        "verify": verify,
        "retries": 0,
        "max_retries": max(0, int(max_retries)),
        "failure_signatures": [],
        "result": None,             # {"ok", "output", "task_id", "artifacts"}
        "lease": None,              # {"owner", "expires"}
        "created_by": created_by,
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "metadata": dict(metadata or {}),
    }


class TaskGraph:
    """Operations over a mission's persisted node list."""

    def __init__(self, mission: dict) -> None:
        self.mission = mission
        self.graph = mission.setdefault("graph", {"nodes": []})
        self.graph.setdefault("nodes", [])

    # -- structure -----------------------------------------------------

    @property
    def nodes(self) -> list[dict]:
        return self.graph["nodes"]

    def get(self, task_id: str) -> dict | None:
        for n in self.nodes:
            if n.get("id") == task_id:
                return n
        return None

    def add(self, task: dict) -> dict:
        for dep in task.get("deps", []):
            if self.get(dep) is None:
                raise KeyError(f"unknown dependency {dep}")
        self._assert_acyclic(task["id"], set(task.get("deps") or []))
        self.nodes.append(task)
        self._refresh_ready()
        return task

    def add_many(self, tasks: list[dict]) -> list[dict]:
        for t in tasks:
            self.add(t)
        return tasks

    def dependents(self, task_id: str) -> list[dict]:
        return [n for n in self.nodes if task_id in (n.get("deps") or [])]

    def _assert_acyclic(self, new_id: str, new_deps: set[str]) -> None:
        """Adding node `new_id` depends on `new_deps` — reject if any dep
        already (transitively) depends on new_id or would form a cycle."""
        deps_of: dict[str, set] = {
            n["id"]: set(n.get("deps") or []) for n in self.nodes}
        deps_of[new_id] = new_deps
        # DFS from each new dep — if it reaches new_id we have a cycle.
        def reaches(start: str, target: str, seen: set) -> bool:
            for d in deps_of.get(start, set()):
                if d == target or (d not in seen and reaches(d, target, seen | {d})):
                    return True
            return False
        for dep in new_deps:
            if dep == new_id or reaches(dep, new_id, set()):
                raise ValueError(f"dependency cycle through {dep}")

    # -- scheduling ----------------------------------------------------

    def _refresh_ready(self) -> None:
        for n in self.nodes:
            if n.get("state") != "planned":
                continue
            deps = [self.get(d) for d in n.get("deps", [])]
            if all(d is not None and d.get("state") == "completed" for d in deps):
                n["state"] = "ready"
            elif any(d is not None and d.get("state") in {"failed", "cancelled"} for d in deps):
                n["state"] = "blocked"

    def refresh(self) -> None:
        self._refresh_ready()

    def runnable(self, *, limit: int = 10) -> list[dict]:
        """Ready nodes sorted by priority (lower first)."""
        self._refresh_ready()
        ready = [n for n in self.nodes if n.get("state") == "ready"]
        ready.sort(key=lambda n: (n.get("priority", 50), n.get("created_at", 0)))
        return ready[:max(1, limit)]

    def running(self) -> list[dict]:
        return [n for n in self.nodes if n.get("state") in {"running", "verifying"}]

    def blocked(self) -> list[dict]:
        return [n for n in self.nodes if n.get("state") == "blocked"]

    def pending(self) -> list[dict]:
        return [n for n in self.nodes
                if n.get("state") in {"planned", "ready", "running", "verifying",
                                      "waiting_dependency", "waiting_approval"}]

    def is_done(self) -> bool:
        return bool(self.nodes) and all(
            n.get("state") in TERMINAL_TASK_STATES for n in self.nodes)

    def mark(self, task_id: str, state: str, **fields: Any) -> dict | None:
        n = self.get(task_id)
        if n is None or state not in TASK_STATES:
            return None
        n["state"] = state
        for k, v in fields.items():
            n[k] = v
        now = time.time()
        if state in {"running", "verifying"} and not n.get("started_at"):
            n["started_at"] = now
        if state in TERMINAL_TASK_STATES:
            n["completed_at"] = now
            n["lease"] = None
        self._refresh_ready()
        return n

    # -- leases ---------------------------------------------------------
    #
    # A running node holds a lease: {owner, expires}. On restart every lease
    # is dead — nodes flip back to ready by MissionStore recovery. At
    # runtime, an expired lease means the worker died mid-task and the node
    # is safe to reclaim (bounded by retries).

    LEASE_SECONDS = 300.0

    def claim(self, task_id: str, owner: str) -> bool:
        n = self.get(task_id)
        if n is None or n.get("state") != "ready":
            return False
        lease = n.get("lease") or {}
        if lease.get("owner") and lease.get("expires", 0) > time.time() \
                and lease["owner"] != owner:
            return False
        n["lease"] = {"owner": owner, "expires": time.time() + self.LEASE_SECONDS}
        n["state"] = "running"
        n["started_at"] = n.get("started_at") or time.time()
        return True

    def renew_lease(self, task_id: str, owner: str) -> None:
        n = self.get(task_id)
        if n is not None and (n.get("lease") or {}).get("owner") == owner:
            n["lease"]["expires"] = time.time() + self.LEASE_SECONDS

    def reclaim_expired(self) -> list[dict]:
        """Nodes whose lease lapsed while running — worker died. Returns the
        reclaimed nodes (caller decides retry vs fail via retry budget)."""
        now = time.time()
        out = []
        for n in self.running():
            lease = n.get("lease") or {}
            if lease.get("expires", 0) < now:
                n["state"] = "ready"
                n["lease"] = None
                n["retries"] = int(n.get("retries") or 0) + 1
                out.append(n)
        return out


class ResourceLocks:
    """Process-local exclusive locks so parallel mission tasks never mutate
    overlapping resources (workspace writes, git index, GPU residency)."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {
            name: threading.Lock() for name in RESOURCE_LOCKS | {"agent_lane"}}
        self._holders: dict[str, str] = {}
        self._guard = threading.RLock()

    def acquire(self, name: str, owner: str) -> bool:
        if not name:
            return True
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            return False
        with self._guard:
            self._holders[name] = owner
        return True

    def release(self, name: str, owner: str) -> None:
        if not name:
            return
        with self._guard:
            if self._holders.get(name) != owner:
                return
            self._holders.pop(name, None)
            lock = self._locks.get(name)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass

    def held_by(self, name: str) -> str:
        with self._guard:
            return self._holders.get(name, "")

    def snapshot(self) -> dict[str, str]:
        with self._guard:
            return dict(self._holders)
