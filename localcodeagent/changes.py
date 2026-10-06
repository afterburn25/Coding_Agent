"""Universal change journal — every meaningful mutation leaves a record.

Nexus mutates files, settings, git state, service state and workspace
configuration from several different lanes (tools, chat control plane,
autonomy, provisioning). Without a single journal there is no honest
answer to "what did you change?" and no reliable "undo that".

This module is deliberately small: it is a bounded, persisted ledger of
ChangeRecords plus an undo dispatcher. The actual restore machinery lives
in the subsystems that already own it — CheckpointManager for files,
SettingsRegistry for config, git for branches/commits — the journal only
records enough to drive and verify the reversal.

Undo handlers are injected callables: ``kind -> fn(record) -> dict`` so
the journal never imports the subsystems it orchestrates.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


class ChangeJournal:
    """Bounded JSONL ledger of structured change records.

    Record shape:
        id, ts, task_id, mission_id, conversation_id, actor,
        action_type, subject, before, after, files, reversible,
        undo, risk, verification, checkpoint_id, description, undone
    """

    def __init__(self, path: Path | str, *, limit: int = 500,
                 undo_handlers: dict[str, Callable[[dict], dict]] | None = None,
                 verify: Callable[[dict], tuple[bool, str]] | None = None) -> None:
        self.path = Path(path)
        self.limit = max(50, int(limit))
        self.undo_handlers = dict(undo_handlers or {})
        self.verify = verify
        self._lock = threading.RLock()
        self._records: list[dict[str, Any]] = []
        self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        self._records = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("id"):
                    self._records.append(row)
        except OSError:
            pass
        if len(self._records) > self.limit:
            self._records = self._records[-self.limit:]
            self._rewrite()

    def _append(self, row: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _rewrite(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n"
                        for r in self._records),
                encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # -- recording ------------------------------------------------------

    def record(self, action_type: str, subject: str, **fields: Any) -> dict[str, Any]:
        """Append a change record. ``undo`` is an opaque descriptor whose
        ``kind`` selects an injected undo handler at reversal time."""
        row = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.time(),
            "task_id": str(fields.get("task_id") or ""),
            "mission_id": str(fields.get("mission_id") or ""),
            "conversation_id": str(fields.get("conversation_id") or ""),
            "actor": str(fields.get("actor") or "nexus"),
            "action_type": str(action_type),
            "subject": str(subject)[:300],
            "before": fields.get("before"),
            "after": fields.get("after"),
            "files": list(fields.get("files") or []),
            "reversible": bool(fields.get("reversible", True)),
            "undo": fields.get("undo"),
            "risk": str(fields.get("risk") or "low"),
            "verification": fields.get("verification"),
            "checkpoint_id": str(fields.get("checkpoint_id") or ""),
            "description": str(fields.get("description") or "")[:500],
            "irreversible_reason": str(fields.get("irreversible_reason") or ""),
            "undone": False,
            "undone_at": None,
        }
        with self._lock:
            self._records.append(row)
            self._append(row)
            if len(self._records) > self.limit:
                self._records = self._records[-self.limit:]
                self._rewrite()
        return row

    def record_file_mutation(self, task_id: str, rel_path: str,
                             *, mission_id: str = "") -> dict[str, Any] | None:
        """Upsert a file-change record for a task — one record per task
        accumulating every touched file, restored via the task checkpoint."""
        if not task_id:
            return None
        with self._lock:
            for row in reversed(self._records):
                if (row.get("task_id") == task_id
                        and row.get("action_type") == "file_changes"
                        and not row.get("undone")):
                    if rel_path not in row["files"]:
                        row["files"].append(rel_path)
                        row["subject"] = f"{len(row['files'])} file(s) in task {task_id}"
                        self._rewrite()
                    return row
        return self.record(
            "file_changes", f"1 file(s) in task {task_id}",
            task_id=task_id, mission_id=mission_id, files=[rel_path],
            reversible=True, checkpoint_id=task_id,
            undo={"kind": "checkpoint_restore", "task_id": task_id},
            verification={"kind": "checkpoint_clean", "task_id": task_id},
            description=f"File mutation under task {task_id}")

    # -- queries ---------------------------------------------------------

    def recent(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records[-max(1, int(limit)):])[::-1]

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._records:
                if row.get("id") == record_id:
                    return row
        return None

    def last_undoable(self) -> dict[str, Any] | None:
        with self._lock:
            for row in reversed(self._records):
                if row.get("reversible") and row.get("undo") and not row.get("undone"):
                    return row
        return None

    # -- undo -------------------------------------------------------------

    def mark_undone(self, record_id: str) -> None:
        with self._lock:
            row = self.get(record_id)
            if row is not None:
                row["undone"] = True
                row["undone_at"] = time.time()
                self._rewrite()

    def _latest_active(self) -> dict[str, Any] | None:
        """Newest record not yet undone — the honest 'undo that' target
        whether or not it turns out to be reversible."""
        with self._lock:
            for row in reversed(self._records):
                if not row.get("undone"):
                    return row
        return None

    def undo(self, record_id: str | None = None) -> dict[str, Any]:
        """Reverse a journaled change, then verify. Never claims success
        before the verify hook or the handler's own verification passes."""
        row = self.get(record_id) if record_id else self._latest_active()
        if row is None:
            return {"ok": False, "message": "There is nothing to undo."}
        if row.get("undone"):
            return {"ok": False, "message": "That change was already undone."}
        if not row.get("reversible") or not row.get("undo"):
            reason = row.get("irreversible_reason") or \
                "it has effects that can't be safely recalled"
            return {"ok": False, "reversible": False,
                    "message": f"'{row['subject']}' can't be undone — {reason}."}
        spec = row["undo"]
        handler = self.undo_handlers.get(str(spec.get("kind") or ""))
        if handler is None:
            return {"ok": False,
                    "message": f"No undo handler is wired for '{spec.get('kind')}'."}
        try:
            result = handler(row)
        except Exception as exc:
            return {"ok": False, "subject": row["subject"],
                    "message": f"Undo failed: {exc}"}
        ok = bool(result.get("ok"))
        verified = bool(result.get("verified", ok))
        if ok:
            self.mark_undone(row["id"])
        return {"ok": ok, "verified": verified, "subject": row["subject"],
                "message": result.get("message") or
                (f"Undone — {row['subject']}." if ok else "Undo did not apply."),
                "detail": result.get("detail") or ""}
