"""Unified action-evidence ledger — the record behind every claim.

Every consequential action (filesystem write/delete/move, process
launch, download, git op, install) records an entry covering the full
execution lifecycle:

    intent -> capability -> tool -> permission -> execution
    -> verification -> evidence -> truthful response

An entry is the *only* basis on which Nexus may say something was
created, moved, deleted, opened, downloaded, fixed, or completed. A
model sentence is not evidence; a queued operation is not evidence;
an approval request is not evidence.

Verification contracts live here too — each action kind declares the
post-condition that must hold before success language is allowed:

    mkdir   : path exists and is a directory
    write   : path exists and is a file (optional size/hash)
    copy    : destination exists (size parity when the source exists)
    move    : destination exists and source is gone
    rename  : new path exists and old path is gone
    delete  : target is absent
    reveal  : open command dispatched to the shell (best-effort —
              process launch verification is a separate contract)
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

BOUND = 2000

STATUSES = {
    "recorded",       # intent captured, nothing run yet
    "awaiting_approval",
    "denied",
    "failed",
    "verified",
    "unverified",     # ran, but the post-condition could not be proven
    "clarify",        # intent certain, target missing — asked, not acted
    "unavailable",    # capability missing/disabled — nothing ran
}


def _sha256(path: Path, cap: int = 8 << 20) -> str:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            h.update(fh.read(cap))
        return h.hexdigest()
    except OSError:
        return ""


def verify_filesystem(kind: str, params: dict[str, Any]
                      ) -> tuple[bool, str]:
    """Check an action's post-condition on disk. Returns (ok, detail).

    ``kind`` is one of mkdir/write/copy/move/rename/delete. Verification
    failure is reported honestly — an action that ran but cannot be
    proven is ``unverified``, never silently ``verified``.
    """
    def p(key: str) -> Path | None:
        raw = params.get(key)
        try:
            return Path(str(raw)) if raw else None
        except Exception:
            return None

    try:
        if kind == "mkdir":
            target = p("path")
            if target is None or not target.exists():
                return False, "directory is absent"
            if not target.is_dir():
                return False, "path exists but is not a directory"
            return True, "directory exists"
        if kind == "write":
            target = p("path")
            if target is None or not target.is_file():
                return False, "file is absent"
            size = target.stat().st_size
            expected = params.get("expected_size")
            if expected is not None and int(expected) != size:
                return False, (f"size mismatch: expected {expected}, "
                               f"got {size}")
            digest = params.get("sha256")
            if digest and _sha256(target) != str(digest):
                return False, "sha256 mismatch"
            return True, f"file exists ({size} bytes)"
        if kind == "copy":
            dst, src = p("dst"), p("src")
            if dst is None or not dst.exists():
                return False, "destination is absent"
            if src is not None and src.is_file() and dst.is_file():
                s, d = src.stat().st_size, dst.stat().st_size
                if s != d:
                    return False, (f"size mismatch: source {s} bytes, "
                                   f"copy {d} bytes")
            return True, "destination exists"
        if kind in ("move", "rename"):
            dst, src = p("dst"), p("src")
            if dst is None or not dst.exists():
                return False, "destination is absent"
            if src is not None and src.exists():
                return False, "source still exists"
            return True, "moved"
        if kind == "delete":
            target = p("path")
            if target is not None and target.exists():
                return False, "target still exists"
            return True, "target absent"
    except OSError as exc:
        return False, f"verification error: {exc}"
    return False, f"unknown action kind '{kind}'"


def run_filesystem(kind: str, params: dict[str, Any]) -> None:
    """Execute the raw filesystem mutation. Raises OSError/ValueError —
    callers translate failures into ledger entries."""
    if kind == "mkdir":
        Path(str(params["path"])).mkdir(parents=True, exist_ok=True)
        return
    if kind == "write":
        target = Path(str(params["path"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(params.get("content") or ""),
                          encoding="utf-8")
        return
    if kind == "copy":
        src, dst = Path(str(params["src"])), Path(str(params["dst"]))
        if not src.exists():
            raise FileNotFoundError(f"source does not exist: {src}")
        if src.is_dir():
            if dst.exists():
                raise FileExistsError(
                    f"destination already exists: {dst}")
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return
    if kind in ("move", "rename"):
        src, dst = Path(str(params["src"])), Path(str(params["dst"]))
        if not src.exists():
            raise FileNotFoundError(f"source does not exist: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return
    if kind == "delete":
        target = Path(str(params["path"]))
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")
        if target.is_dir():
            rec = params.get("recursive")
            if isinstance(rec, str):
                rec = rec.strip().lower() in ("1", "true", "yes")
            if rec:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
        return
    raise ValueError(f"unknown action kind '{kind}'")


class ActionLedger:
    """Bounded, durable record of consequential actions."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "entries": []}
        self.data.setdefault("entries", [])

    def _save(self) -> None:
        try:
            atomic_write_text(
                self.path,
                json.dumps(self.data, indent=2, ensure_ascii=False,
                           default=str))
        except OSError:
            pass

    def _row(self, eid: str) -> dict | None:
        for e in self.data["entries"]:
            if e.get("id") == eid:
                return e
        return None

    # -- recording -----------------------------------------------------

    def begin(self, *, kind: str, action: str, capability: str = "",
              tool: str = "", params: dict[str, Any] | None = None,
              task_id: str = "", mission_id: str = "") -> dict:
        """Open an entry at intent time — the lifecycle is recorded even
        when execution is never reached (denial, failure)."""
        now = time.time()
        e = {
            "id": f"act-{uuid.uuid4().hex[:10]}",
            "kind": str(kind)[:40],
            "action": str(action)[:240],
            "capability": str(capability)[:60],
            "tool": str(tool)[:60],
            "params": {str(k): str(v)[:400]
                       for k, v in (params or {}).items()},
            "task_id": str(task_id)[:60],
            "mission_id": str(mission_id)[:60],
            "status": "recorded",
            "permission": "",
            "started_at": now,
            "ended_at": None,
            "elapsed_s": None,
            "exit_code": None,
            "verification": "",
            "verified": None,
            "artifact": "",
            "failure": "",
            "detail": "",
        }
        with self._lock:
            self.data["entries"].append(e)
            self.data["entries"] = self.data["entries"][-BOUND:]
            self._save()
        return dict(e)

    def finish(self, eid: str, *, status: str, permission: str = "",
               verification: str = "", verified: bool | None = None,
               artifact: str = "", failure: str = "", detail: str = "",
               exit_code: int | None = None) -> dict | None:
        """Close an entry with the outcome and its evidence."""
        with self._lock:
            e = self._row(eid)
            if e is None:
                return None
            e["status"] = status if status in STATUSES else "recorded"
            e["ended_at"] = time.time()
            e["elapsed_s"] = round(
                e["ended_at"] - float(e.get("started_at") or 0), 3)
            if permission:
                e["permission"] = str(permission)[:40]
            if verification:
                e["verification"] = str(verification)[:200]
            if verified is not None:
                e["verified"] = bool(verified)
            if artifact:
                e["artifact"] = str(artifact)[:400]
            if failure:
                e["failure"] = str(failure)[:300]
            if detail:
                e["detail"] = str(detail)[:300]
            if exit_code is not None:
                e["exit_code"] = int(exit_code)
            self._save()
            return dict(e)

    # -- retrieval -----------------------------------------------------

    def get(self, eid: str) -> dict | None:
        r = self._row(eid)
        return dict(r) if r else None

    def recent(self, limit: int = 50, *, task_id: str = "",
               mission_id: str = "") -> list[dict]:
        rows = [e for e in self.data["entries"]
                if (not task_id or e.get("task_id") == task_id)
                and (not mission_id or e.get("mission_id") == mission_id)]
        return [dict(e) for e in rows[-limit:]]

    def mission_rollup(self, mission_id: str,
                       limit: int = 500) -> dict:
        """Evidence summary for one mission — status counts plus the last
        few failed/verified actions so 'what did the mission do' answers
        from evidence, not narration."""
        entries = self.recent(limit=limit, mission_id=mission_id)
        statuses: dict[str, int] = {}
        for e in entries:
            s = str(e.get("status") or "recorded")
            statuses[s] = statuses.get(s, 0) + 1
        return {
            "mission_id": mission_id,
            "actions": len(entries),
            "statuses": statuses,
            "recent_failures": [
                e["action"] for e in entries
                if e.get("status") == "failed"][-5:],
            "recent_verified": [
                e["action"] for e in entries
                if e.get("status") == "verified"][-5:],
        }

    def latest_for(self, *, kind: str = "", artifact: str = "") -> dict | None:
        for e in reversed(self.data["entries"]):
            if kind and e.get("kind") != kind:
                continue
            if artifact and e.get("artifact") != artifact:
                continue
            return dict(e)
        return None
