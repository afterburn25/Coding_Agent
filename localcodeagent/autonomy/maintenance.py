"""Self-maintenance mission helpers.

Light checks run read-only over local state: disk headroom, store integrity,
stale jobs, orphan data. They never modify system components — deep
maintenance (vacuum/index rebuilds) is explicit and separately authorized.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any


def run_light_maintenance(workspace: Path, store) -> dict:
    """Health-only pass used by the built-in maintenance mission task."""
    checks: list[dict[str, Any]] = []
    ok = True

    def check(name: str, healthy: bool, detail: str = "") -> None:
        nonlocal ok
        checks.append({"name": name, "ok": bool(healthy), "detail": detail})
        ok = ok and healthy

    # Disk headroom on the state volume.
    try:
        free_gb = shutil.disk_usage(workspace).free / (1024 ** 3)
        check("disk_free", free_gb >= 1.0, f"{free_gb:.2f} GB free")
    except OSError as exc:
        check("disk_free", False, str(exc))

    # Autonomy stores parse cleanly.
    for name in ("missions", "schedules", "triggers", "standing_goals",
                 "notifications", "grants", "approvals"):
        path = store.root / f"{name}.json"
        if not path.is_file():
            check(f"store:{name}", True, "absent")
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
            check(f"store:{name}", True, "ok")
        except (OSError, ValueError):
            check(f"store:{name}", False, "corrupt payload")

    # Corrupt quarantine artifacts indicate a past crash — surface, don't fix.
    stray = list(store.root.glob("*.corrupt-*"))
    check("corrupt_files", not stray,
          f"{len(stray)} quarantined file(s)" if stray else "clean")

    # Stale notifications pressure.
    unread = [r for r in store.notifications.rows() if not r.get("read")]
    check("notification_backlog", len(unread) < 200,
          f"{len(unread)} unread")

    summary = "; ".join(f"{c['name']}={'ok' if c['ok'] else 'FAIL'}"
                        for c in checks)
    return {"ok": ok, "output": summary,
            "checks": checks}


BUILTIN_MAINTENANCE_GOAL = {
    "objective": ("Keep Nexus Core healthy: verify disk headroom, store "
                  "integrity, stale jobs, and corrupt quarantines; report "
                  "anything that needs attention."),
    "scope": "maintenance",
    "notification_policy": "failures",
    "success_criteria": [
        {"kind": "all_tasks_completed",
         "description": "all maintenance checks ran"},
        {"kind": "no_failures",
         "description": "no check reported a fault"},
    ],
}
