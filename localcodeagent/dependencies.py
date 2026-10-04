"""Dependency intelligence — tracked packages and staged upgrades.

`DependencyStore` (`data/dependencies.json`) keeps per-ecosystem
dependency rows: installed/available versions, compatibility notes,
security advisories, project impact, test coverage. Upgrade attempts
run through a staged record (isolated_env → install → build → tests →
review) that must pass every required stage before `promoted` —
production deps are never blindly upgraded.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_DEPS = 500
UPGRADE_STAGES = ("isolated_env", "install", "build", "tests", "review")


def _ver_tuple(v: str) -> tuple:
    """Semver-ish tuple: '1.2.3-rc1' → (1, 2, 3)."""
    parts = re.findall(r"\d+", str(v)[:60])[:4]
    return tuple(int(p) for p in parts) if parts else ()


def version_newer(a: str, b: str) -> bool:
    """True when version string `a` sorts newer than `b`."""
    ta, tb = _ver_tuple(a), _ver_tuple(b)
    if not ta or not tb:
        return False
    for i in range(max(len(ta), len(tb))):
        x = ta[i] if i < len(ta) else 0
        y = tb[i] if i < len(tb) else 0
        if x != y:
            return x > y
    return False


class DependencyStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "dependencies": [],
                         "upgrades": []}
        self.data.setdefault("dependencies", [])
        self.data.setdefault("upgrades", [])

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            {"version": 1,
             "dependencies": self.data["dependencies"][-MAX_DEPS:],
             "upgrades": self.data["upgrades"][-200:]},
            indent=2, ensure_ascii=False, default=str))

    def _find(self, dep_id: str) -> dict | None:
        for d in self.data["dependencies"]:
            if d.get("id") == dep_id:
                return d
        return None

    def track(self, name: str, *, ecosystem: str = "pip",
              installed_version: str = "", available_version: str = "",
              compatible: bool | None = None, advisory: str = "",
              impact: str = "", test_coverage: str = "",
              project_id: str = "") -> dict:
        """Create or update a dependency row keyed by ecosystem+name."""
        with self._lock:
            row = next(
                (d for d in self.data["dependencies"]
                 if d["name"] == name and d["ecosystem"] == ecosystem),
                None)
            if row is None:
                row = {"id": f"dep-{uuid.uuid4().hex[:10]}",
                       "name": str(name)[:120],
                       "ecosystem": str(ecosystem)[:40],
                       "created_at": time.time()}
                self.data["dependencies"].append(row)
            for k, v in {"installed_version": installed_version,
                         "available_version": available_version,
                         "advisory": advisory, "impact": impact,
                         "test_coverage": test_coverage,
                         "project_id": project_id}.items():
                if v:
                    row[k] = str(v)[:300]
            if compatible is not None:
                row["compatible"] = bool(compatible)
            row["checked_at"] = time.time()
            row["outdated"] = bool(
                row.get("available_version")
                and version_newer(row["available_version"],
                                  row.get("installed_version") or ""))
            self._save()
        return dict(row)

    def list(self, *, ecosystem: str = "", outdated_only: bool = False,
             project_id: str = "") -> list[dict]:
        with self._lock:
            return [dict(d) for d in self.data["dependencies"]
                    if (not ecosystem or d.get("ecosystem") == ecosystem)
                    and (not outdated_only or d.get("outdated"))
                    and (not project_id
                         or d.get("project_id") == project_id)]

    # -- staged upgrades ------------------------------------------------------
    def stage_upgrade(self, dep_id: str, candidate_version: str) -> dict | None:
        dep = self._find(dep_id)
        if dep is None:
            return None
        row = {"id": f"upg-{uuid.uuid4().hex[:10]}",
               "dependency_id": dep_id,
               "dependency": dep["name"],
               "from_version": dep.get("installed_version") or "",
               "candidate_version": str(candidate_version)[:60],
               "status": "pending",
               "stages": {s: {"status": "pending", "detail": ""}
                          for s in UPGRADE_STAGES},
               "created_at": time.time()}
        with self._lock:
            self.data["upgrades"].append(row)
            self._save()
        return dict(row)

    def record_upgrade_stage(self, upgrade_id: str, stage: str,
                             status: str, *, detail: str = "") -> dict | None:
        """status: passed/failed/skipped — a failed required stage marks
        the attempt `failed`; all required stages passed → `passed`."""
        if stage not in UPGRADE_STAGES or \
                status not in {"passed", "failed", "skipped"}:
            return None
        with self._lock:
            row = next((u for u in self.data["upgrades"]
                        if u.get("id") == upgrade_id), None)
            if row is None or row["status"] in {"promoted", "rejected",
                                                "failed"}:
                return None
            row["stages"][stage] = {"status": status,
                                    "detail": str(detail)[:300],
                                    "at": time.time()}
            if status == "failed":
                row["status"] = "failed"
            elif all(s["status"] in {"passed", "skipped"}
                     for s in row["stages"].values()):
                row["status"] = "passed"
            else:
                row["status"] = "running"
            self._save()
            return dict(row)

    def conclude_upgrade(self, upgrade_id: str, promote: bool) -> dict | None:
        """promote=True only valid after every stage passed; installs
        the candidate version onto the dependency row."""
        with self._lock:
            row = next((u for u in self.data["upgrades"]
                        if u.get("id") == upgrade_id), None)
            if row is None or row["status"] in {"promoted", "rejected"}:
                return None
            if promote and row["status"] != "passed":
                return None
            row["status"] = "promoted" if promote else "rejected"
            if promote:
                dep = self._find(row["dependency_id"])
                if dep is not None:
                    dep["installed_version"] = row["candidate_version"]
                    dep["outdated"] = False
            self._save()
            return dict(row)

    def summary(self) -> dict:
        deps = self.data["dependencies"]
        return {"tracked": len(deps),
                "outdated": sum(1 for d in deps if d.get("outdated")),
                "advisories": sum(1 for d in deps if d.get("advisory")),
                "upgrades": len(self.data["upgrades"])}
