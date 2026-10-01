from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Unified job states. Subsystem-specific raw statuses are normalized into this
# vocabulary; the raw value is preserved on the record as `status`.
JOB_STATES = (
    "queued",
    "preparing",
    "running",
    "waiting_for_tool",
    "waiting_for_permission",
    "completed",
    "failed",
    "cancelled",
)

_TASK_STATE_MAP = {
    "planning": "queued",
    "working": "running",
    "verifying": "running",
    "reviewing": "running",
    "waiting_approval": "waiting_for_permission",
    "done": "completed",
    "error": "failed",
    "interrupted": "cancelled",
    "cancelled": "cancelled",
    "step_limit": "failed",
    "reverted": "completed",
}

_GENERIC_STATE_MAP = {
    "queued": "queued",
    "preparing": "preparing",
    "running": "running",
    "loading_model": "preparing",
    "generating": "running",
    "refining": "running",
    "upscaling": "running",
    "downloading": "running",
    "verifying": "running",
    "waiting_for_tool": "waiting_for_tool",
    "waiting_for_permission": "waiting_for_permission",
    "finished": "completed",
    "completed": "completed",
    "done": "completed",
    "failed": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "interrupted": "cancelled",
}


def normalize_state(raw: str) -> str:
    state = str(raw or "").strip().lower()
    return _GENERIC_STATE_MAP.get(state, "running" if state else "queued")


@dataclass(slots=True)
class JobRecord:
    id: str
    kind: str  # agent_task | image | model_install | image_install | generic
    title: str
    state: str = "queued"
    status: str = ""  # raw subsystem status
    progress: float = 0.0
    detail: str = ""
    source: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancellable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobManager:
    """Central asynchronous job ledger.

    Owns generic job records submitted by tool providers and also aggregates
    live job state from the task ledger, image queue, and download managers so
    the Jobs UI/API sees one unified queue.
    """

    def __init__(self, path: Path | None = None, *, limit: int = 300) -> None:
        self.path = Path(path).resolve() if path else None
        self.limit = max(20, int(limit))
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.RLock()
        # Optional callback invoked with {"job": record.as_dict()} after every
        # submit/update/cancel — wired to the server EventBus for live UI.
        self.on_change = None
        self._load()

    def _emit(self, record: JobRecord) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change({"job": record.as_dict()})
        except Exception:
            pass

    def submit(self, kind: str, title: str, *, metadata: dict[str, Any] | None = None) -> JobRecord:
        record = JobRecord(
            id=uuid.uuid4().hex[:16],
            kind=str(kind or "generic"),
            title=str(title or "")[:300],
            state="queued",
            status="queued",
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._jobs[record.id] = record
            self._trim()
            self._save()
        self._emit(record)
        return record

    def update(self, job_id: str, **fields: Any) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if "state" in fields:
                job.state = normalize_state(str(fields.pop("state")))
            if "status" in fields:
                raw = str(fields.pop("status"))
                job.status = raw
                job.state = normalize_state(raw)
            for key, value in fields.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            if job.state in {"running", "preparing", "waiting_for_tool", "waiting_for_permission"} and job.started_at is None:
                job.started_at = time.time()
            if job.state in {"completed", "failed", "cancelled"}:
                job.finished_at = job.finished_at or time.time()
            self._save()
        self._emit(job)
        return job

    def cancel(self, job_id: str) -> JobRecord:
        return self.update(job_id, state="cancelled", status="cancelled")

    def get(self, job_id: str) -> JobRecord:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.as_dict() for j in sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)]

    # -- aggregation over existing subsystem ledgers -------------------------

    @staticmethod
    def _task_jobs(tasks_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in (tasks_payload or {}).get("recent") or []:
            raw = str(task.get("status", ""))
            rows.append({
                "id": f"task-{task.get('id')}",
                "kind": "agent_task",
                "title": str(task.get("prompt", ""))[:140],
                "state": _TASK_STATE_MAP.get(raw, normalize_state(raw)),
                "status": raw,
                "progress": 1.0 if raw == "done" else 0.0,
                "detail": str(task.get("summary") or "")[:300],
                "source": str(task.get("model_id") or task.get("model_role") or ""),
                "error": str(task.get("error") or ""),
                "created_at": task.get("created_at"),
                "started_at": task.get("created_at"),
                "finished_at": task.get("updated_at") if raw in {"done", "error", "interrupted", "reverted"} else None,
                "cancellable": raw in {"working", "verifying", "reviewing", "waiting_approval", "planning"},
                "metadata": {"task_id": task.get("id"), "phase": task.get("phase"), "steps": task.get("steps")},
            })
        return rows

    @staticmethod
    def _image_jobs(image_jobs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for job in image_jobs or []:
            raw = str(job.get("state") or job.get("stage") or "")
            request = job.get("request") or {}
            rows.append({
                "id": f"image-{job.get('id')}",
                "kind": "image",
                "title": str(request.get("prompt") or job.get("operation") or "image job")[:140],
                "state": normalize_state(raw),
                "status": raw,
                "progress": float(job.get("progress") or 0.0),
                "detail": str(job.get("operation") or ""),
                "source": str(job.get("model_id") or "comfyui"),
                "error": str(job.get("error") or job.get("error_message") or ""),
                "created_at": job.get("created_at"),
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "cancellable": raw in {"queued", "loading_model", "generating", "refining", "upscaling"},
                "metadata": {"job_id": job.get("id"), "operation": job.get("operation")},
            })
        return rows

    @staticmethod
    def _install_jobs(installs: list[dict[str, Any]] | None, *, kind: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for job in installs or []:
            raw = str(job.get("state") or "")
            rows.append({
                "id": f"{kind}-{job.get('id')}",
                "kind": kind,
                "title": str(job.get("title") or job.get("current_file") or job.get("model_id") or "install")[:140],
                "state": normalize_state(raw),
                "status": raw,
                "progress": float(job.get("progress") or 0.0),
                "detail": str(job.get("current_file") or job.get("message") or "")[:200],
                "source": "installer",
                "error": str(job.get("error") or ""),
                "created_at": job.get("created_at"),
                "started_at": job.get("created_at"),
                "finished_at": job.get("finished_at"),
                "cancellable": raw in {"queued", "running", "downloading", "verifying"},
                "metadata": {"job_id": job.get("id"), "model_id": job.get("model_id") or job.get("catalog_id")},
            })
        return rows

    def aggregate(
        self,
        *,
        tasks_payload: dict[str, Any] | None = None,
        image_jobs: list[dict[str, Any]] | None = None,
        model_installs: list[dict[str, Any]] | None = None,
        image_installs: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        merged = self.list_jobs()
        merged += self._task_jobs(tasks_payload)
        merged += self._image_jobs(image_jobs)
        merged += self._install_jobs(model_installs, kind="model_install")
        merged += self._install_jobs(image_installs, kind="image_install")
        return sorted(merged, key=lambda j: (j.get("created_at") or 0), reverse=True)

    # -- persistence ---------------------------------------------------------

    def _trim(self) -> None:
        if len(self._jobs) <= self.limit:
            return
        ordered = sorted(self._jobs.values(), key=lambda j: j.created_at)
        for job in ordered[: len(self._jobs) - self.limit]:
            del self._jobs[job.id]

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for row in rows if isinstance(rows, list) else []:
            try:
                job = JobRecord(**{k: v for k, v in row.items() if k in JobRecord.__dataclass_fields__})
            except TypeError:
                continue
            if job.state in {"queued", "preparing", "running", "waiting_for_tool", "waiting_for_permission"}:
                job.state = "failed"
                job.status = "interrupted"
                job.error = "The application restarted while this job was active."
                job.finished_at = job.finished_at or time.time()
            self._jobs[job.id] = job

    def _save(self) -> None:
        if not self.path:
            return
        rows = [j.as_dict() for j in sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[: self.limit]]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass
