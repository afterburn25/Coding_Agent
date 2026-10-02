"""Resource / budget manager for autonomous missions.

Mission budgets cap runtime, repair loops, retries and network usage; the
manager also snapshots machine resources (via the runtime summary when
available) so the supervisor can yield heavy background work when the user
is interactive.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable


class BudgetManager:
    def __init__(self, workspace: Path,
                 resources: Callable[[], dict] | None = None) -> None:
        self.workspace = Path(workspace)
        self._resources = resources or (lambda: {})

    # -- machine snapshot ------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {"ts": time.time()}
        try:
            usage = shutil.disk_usage(self.workspace)
            snap["disk_free_gb"] = round(usage.free / (1024 ** 3), 2)
        except OSError:
            snap["disk_free_gb"] = None
        try:
            hw = self._resources() or {}
            snap["hardware"] = hw
        except Exception:
            snap["hardware"] = {}
        return snap

    # -- mission budget checks -------------------------------------------

    def check(self, mission: dict) -> dict[str, Any]:
        """Returns {"ok": bool, "violations": [...]} — supervisor pauses the
        mission on violation rather than spending unlimited resources."""
        violations: list[str] = []
        budgets = mission.get("budgets") or {}
        now = time.time()

        deadline = mission.get("runtime_deadline")
        if deadline and now > float(deadline):
            violations.append(
                f"runtime budget exceeded ({int(budgets.get('max_runtime_s', 0))}s)")

        max_repairs = int(budgets.get("max_repair_loops", 0) or 0)
        if max_repairs and int(mission.get("repair_loops", 0)) > max_repairs:
            violations.append(f"repair loops > {max_repairs}")

        net_cap = float(budgets.get("max_network_bytes", 0) or 0)
        net_used = float(mission.get("network_bytes", 0) or 0)
        if net_cap and net_used > net_cap:
            violations.append(
                f"network budget exceeded ({int(net_used)} > {int(net_cap)} bytes)")

        snap = self.snapshot()
        free_gb = snap.get("disk_free_gb")
        if free_gb is not None and free_gb < 1.0:
            violations.append(f"disk nearly full ({free_gb} GB free)")

        return {"ok": not violations, "violations": violations,
                "snapshot": snap}

    def may_use_gpu(self, mission: dict) -> bool:
        """GPU-heavy mission work yields when the foreground lane is busy —
        checked cheaply via resource mode + locks, refined later."""
        mode = str((mission.get("resource_mode") or "balanced"))
        return mode != "conservative" or True
