"""Durable Projects — named bodies of work that outlive conversations.

A project binds repositories/files, goals, milestones, tasks, decisions,
project-scoped memory and worker history. Projects survive restart and are
profile-scoped: a task launched under Profile A never adopts Profile B's
identity or memory after a switch.

Project memory is deliberately separate from personal memory and global
technical knowledge — architecture decisions, conventions, known bugs and
rejected approaches belong to the project, not the person.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text
import json

MAX_PROJECTS = 200
MAX_MEMORY_ROWS = 500
MAX_HISTORY = 300

PROJECT_STATES = {"active", "paused", "completed", "archived"}

# Project-memory buckets — structured, not a blob, so compression/summary
# can preserve decisions without dragging raw transcript.
MEMORY_KINDS = {
    "architecture", "decision", "convention", "known_bug", "fix",
    "rejected_approach", "constraint", "unresolved", "test_result",
    "milestone", "note",
}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class ProjectStore:
    """JSON-document store; one file per project under store_dir/projects/.
    Profile scoping is enforced by the caller passing profile_id — rows
    carry it and list()/get() filter it."""

    def __init__(self, store_dir: Path) -> None:
        self.root = Path(store_dir) / "projects"
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = Path(store_dir) / "projects_index.json"
        self._lock = threading.RLock()
        self._index: list[dict] = []
        self._load_index()

    # -- persistence -----------------------------------------------------

    def _path(self, project_id: str) -> Path:
        safe = "".join(c for c in project_id if c.isalnum() or c in "-_")
        return self.root / f"{safe}.json"

    def _load_index(self) -> None:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            self._index = [r for r in raw.get("projects", [])
                           if isinstance(r, dict)]
        except (OSError, ValueError):
            self._index = []

    def _save_index(self) -> None:
        try:
            atomic_write_text(self._index_path, json.dumps(
                {"version": 1, "projects": self._index}, indent=2))
        except OSError:
            pass

    def _read(self, project_id: str) -> dict | None:
        try:
            raw = json.loads(self._path(project_id).read_text(
                encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except (OSError, ValueError):
            return None

    def _write(self, project: dict) -> None:
        project["updated_at"] = time.time()
        atomic_write_text(self._path(project["id"]),
                          json.dumps(project, indent=2, ensure_ascii=False))
        with self._lock:
            for i, r in enumerate(self._index):
                if r.get("id") == project["id"]:
                    self._index[i] = self._summary(project)
                    self._save_index()
                    return
            self._index.append(self._summary(project))
            if len(self._index) > MAX_PROJECTS:
                self._index = self._index[-MAX_PROJECTS:]
            self._save_index()

    @staticmethod
    def _summary(p: dict) -> dict:
        return {"id": p.get("id"), "name": p.get("name"),
                "status": p.get("status"), "profile_id": p.get("profile_id"),
                "goals": len(p.get("goals") or []),
                "tasks": len(p.get("tasks") or []),
                "updated_at": p.get("updated_at")}

    # -- CRUD ---------------------------------------------------------------

    def create(self, name: str, *, description: str = "",
               profile_id: str = "", repositories: list[str] | None = None,
               metadata: dict | None = None) -> dict:
        project = {
            "id": _new_id("proj"),
            "name": str(name or "project")[:140],
            "description": str(description or "")[:2000],
            "profile_id": str(profile_id or ""),
            "repositories": [str(r) for r in (repositories or [])][:50],
            "status": "active",
            "goals": [],
            "milestones": [],
            "tasks": [],
            "decisions": [],
            "blockers": [],
            "memory": [],          # [{kind, text, ts, source}]
            "worker_history": [],
            "activity": [],
            "conversations": [],
            "next_actions": [],
            "metadata": dict(metadata or {}),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._write(project)
        return dict(project)

    def get(self, project_id: str, *, profile_id: str = "") -> dict | None:
        p = self._read(project_id)
        if p is None:
            return None
        # Profile isolation — a project owned by A is invisible to B.
        owner = str(p.get("profile_id") or "")
        if owner and profile_id and owner != str(profile_id):
            return None
        return p

    def list(self, *, profile_id: str = "",
             status: str = "") -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._index]
        if profile_id:
            rows = [r for r in rows
                    if not r.get("profile_id")
                    or r.get("profile_id") == profile_id]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return sorted(rows, key=lambda r: r.get("updated_at") or 0,
                      reverse=True)

    def update(self, project_id: str, **fields: Any) -> dict | None:
        p = self._read(project_id)
        if p is None:
            return None
        for k, v in fields.items():
            if k in p and k != "id":
                p[k] = v
        self._write(p)
        return dict(p)

    # -- goals / milestones --------------------------------------------------

    def add_goal(self, project_id: str, objective: str, *,
                 done_when: str = "") -> dict | None:
        """Durable project goal — e.g. 'keep working until milestone tests
        are green'. Survives restart; the supervisor may read these."""
        p = self._read(project_id)
        if p is None:
            return None
        goal = {"id": _new_id("goal"), "objective": str(objective)[:500],
                "done_when": str(done_when or "")[:300],
                "status": "active", "created_at": time.time(),
                "completed_at": None}
        p.setdefault("goals", []).append(goal)
        self._write(p)
        return dict(goal)

    def complete_goal(self, project_id: str, goal_id: str) -> bool:
        p = self._read(project_id)
        if p is None:
            return False
        for g in p.get("goals", []):
            if g.get("id") == goal_id:
                g["status"] = "completed"
                g["completed_at"] = time.time()
                self._write(p)
                return True
        return False

    # -- project memory --------------------------------------------------------

    def remember(self, project_id: str, kind: str, text: str, *,
                 source: str = "") -> dict | None:
        """Append a project-memory row; kinds are bounded and structured so
        summaries can group them (decisions vs bugs vs conventions)."""
        if kind not in MEMORY_KINDS:
            kind = "note"
        p = self._read(project_id)
        if p is None:
            return None
        row = {"id": _new_id("pm"), "kind": kind,
               "text": str(text)[:2000], "source": str(source or "")[:120],
               "ts": time.time()}
        mem = p.setdefault("memory", [])
        mem.append(row)
        p["memory"] = mem[-MAX_MEMORY_ROWS:]
        self._write(p)
        return dict(row)

    def memory(self, project_id: str, *, kind: str = "",
               limit: int = 50) -> list[dict]:
        p = self._read(project_id)
        if p is None:
            return []
        rows = [r for r in p.get("memory", [])
                if not kind or r.get("kind") == kind]
        return [dict(r) for r in rows[-limit:]]

    def memory_summary(self, project_id: str) -> dict[str, list[str]]:
        """Structured compression — the shape long-running context
        summaries collapse into instead of dropping older context."""
        p = self._read(project_id) or {}
        out: dict[str, list[str]] = {
            "architecture": [], "decisions": [], "completed_work": [],
            "known_issues": [], "blockers": [], "next_steps": []}
        for r in p.get("memory", []):
            k = r.get("kind")
            if k == "architecture":
                out["architecture"].append(r.get("text", ""))
            elif k == "decision":
                out["decisions"].append(r.get("text", ""))
            elif k in {"known_bug", "unresolved"}:
                out["known_issues"].append(r.get("text", ""))
            elif k in {"fix", "milestone"}:
                out["completed_work"].append(r.get("text", ""))
        out["blockers"] = [str(b) for b in p.get("blockers", [])][:20]
        out["next_steps"] = [str(a) for a in p.get("next_actions", [])][:20]
        return {k: v[-20:] for k, v in out.items()}

    # -- activity / worker history ----------------------------------------------

    def log_activity(self, project_id: str, event: str, *,
                     detail: str = "", worker_id: str = "") -> None:
        p = self._read(project_id)
        if p is None:
            return
        p.setdefault("activity", []).append(
            {"ts": time.time(), "event": str(event)[:80],
             "detail": str(detail)[:300], "worker_id": worker_id})
        p["activity"] = p["activity"][-MAX_HISTORY:]
        self._write(p)

    def record_task(self, project_id: str, task: dict) -> None:
        p = self._read(project_id)
        if p is None:
            return
        p.setdefault("tasks", []).append(
            {"task_id": task.get("task_id") or task.get("id"),
             "worker_id": task.get("worker_id") or "",
             "title": str(task.get("title") or "")[:140],
             "status": str(task.get("status") or ""),
             "branch": str(task.get("branch") or ""),
             "files": list(task.get("files") or [])[:50],
             "ts": time.time()})
        p["tasks"] = p["tasks"][-MAX_HISTORY:]
        self._write(p)
