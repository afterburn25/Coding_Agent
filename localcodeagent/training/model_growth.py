from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ..fsutil import replace_with_retry
from typing import Any


class ModelGrowthLab:
    """Versioned, review-first model growth pipeline.

    The lab never mutates a running model. It collects approved behavior/knowledge
    examples, exports datasets, creates training manifests, and tracks candidate
    adapters/model forks through evaluation and promotion.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.datasets_dir = self.root / "datasets"
        self.jobs_dir = self.root / "jobs"
        self.registry_dir = self.root / "registry"
        self.adapters_dir = self.root / "adapters"
        for path in (self.datasets_dir, self.jobs_dir, self.registry_dir, self.adapters_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.candidates_path = self.root / "candidates.json"
        self.registry_path = self.registry_dir / "models.json"
        self._candidates = self._load_json(self.candidates_path, {"version": 1, "items": []})
        self._registry = self._load_json(self.registry_path, {"version": 1, "models": [], "active_candidate_id": ""})
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}

    @staticmethod
    def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.is_file():
            return dict(default)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else dict(default)
        except (OSError, ValueError, TypeError):
            return dict(default)

    @staticmethod
    def _save_json(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        replace_with_retry(tmp, path)

    @staticmethod
    def _signature(kind: str, instruction: str, response: str) -> str:
        return f"{kind}\0{instruction.strip().casefold()}\0{response.strip().casefold()}"

    def collect(
        self,
        *,
        kind: str,
        instruction: str,
        response: str,
        source: str = "conversation",
        metadata: dict[str, Any] | None = None,
        auto_approved: bool = False,
    ) -> dict[str, Any] | None:
        instruction = str(instruction or "").strip()[:12000]
        response = str(response or "").strip()[:20000]
        if not instruction or not response:
            return None
        signature = self._signature(kind, instruction, response)
        for item in self._candidates.get("items", []):
            if item.get("signature") == signature:
                return dict(item)
        item = {
            "id": uuid.uuid4().hex[:12],
            "signature": signature,
            "kind": str(kind or "behavior"),
            "instruction": instruction,
            "response": response,
            "source": str(source or "conversation"),
            "metadata": dict(metadata or {}),
            "status": "approved" if auto_approved else "pending",
            "created_at": time.time(),
            "reviewed_at": time.time() if auto_approved else 0.0,
            "review_note": "",
        }
        self._candidates.setdefault("items", []).append(item)
        self._candidates["items"] = self._candidates["items"][-5000:]
        self._save_json(self.candidates_path, self._candidates)
        return dict(item)

    def import_conversation_memory(self, snapshot: dict[str, Any]) -> int:
        count = 0
        for row in snapshot.get("training_examples", []):
            if not isinstance(row, dict):
                continue
            instruction = str(row.get("instruction", "")).strip()
            correction = str(row.get("correction", "")).strip()
            previous = str(row.get("previous_response", "")).strip()
            if instruction and correction:
                result = self.collect(
                    kind="correction",
                    instruction=instruction,
                    response=correction,
                    source="conversation_correction",
                    metadata={"previous_response": previous},
                    auto_approved=bool(row.get("approved", False)),
                )
                count += int(result is not None)
        for row in snapshot.get("behavior_rules", []):
            if isinstance(row, dict) and row.get("active", True):
                text = str(row.get("text", "")).strip()
                if text:
                    result = self.collect(
                        kind="behavior_rule",
                        instruction="Apply this operating rule in relevant conversations.",
                        response=text,
                        source="behavior_rule",
                        auto_approved=True,
                    )
                    count += int(result is not None)
        return count

    def import_conversation_feedback(self, snapshot: dict[str, Any]) -> int:
        """Turn explicit user feedback into contextual training signals."""
        count = 0
        for row in snapshot.get("feedback", []):
            if not isinstance(row, dict):
                continue
            prompt = str(row.get("user_prompt", "")).strip()
            response = str(row.get("assistant_response", "")).strip()
            rating = str(row.get("rating", "")).strip().lower()
            if not prompt or not response or rating not in {"up", "down", "better", "worse"}:
                continue
            positive = rating in {"up", "better"}
            result = self.collect(
                kind="conversation_example" if positive else "negative_feedback",
                instruction=prompt,
                response=response,
                source="conversation_feedback_positive" if positive else "conversation_feedback_negative",
                metadata={
                    "rating": rating,
                    "note": str(row.get("note", ""))[:4000],
                    "conversation_id": row.get("conversation_id", ""),
                    "message_id": row.get("message_id", ""),
                    "assistant_timestamp": row.get("assistant_timestamp", 0),
                },
                # A thumbs-up/better rating is already an explicit human approval signal.
                auto_approved=positive,
            )
            count += int(result is not None)
        return count

    def import_knowledge_memory(self, snapshot: dict[str, Any]) -> int:
        count = 0
        for row in snapshot.get("recent", []):
            if not isinstance(row, dict):
                continue
            query = str(row.get("query", "")).strip()
            answer = str(row.get("answer", "")).strip()
            if not query or not answer:
                continue
            result = self.collect(
                kind="sourced_knowledge",
                instruction=query,
                response=answer,
                source="sourced_web_memory",
                metadata={
                    "sources": row.get("sources", []),
                    "current_sensitive": bool(row.get("current_sensitive", False)),
                    "expires_at": row.get("expires_at", 0),
                },
            )
            count += int(result is not None)
        return count

    def review(self, candidate_id: str, *, status: str, note: str = "") -> dict[str, Any]:
        status = status.strip().lower()
        if status not in {"approved", "rejected", "pending"}:
            raise ValueError("status must be approved, rejected, or pending")
        for item in self._candidates.get("items", []):
            if item.get("id") == candidate_id:
                item["status"] = status
                item["review_note"] = str(note or "")[:4000]
                item["reviewed_at"] = time.time()
                self._save_json(self.candidates_path, self._candidates)
                return dict(item)
        raise KeyError(candidate_id)

    def candidates(self, *, status: str = "", limit: int = 500) -> list[dict[str, Any]]:
        items = [dict(x) for x in self._candidates.get("items", []) if isinstance(x, dict)]
        if status:
            items = [x for x in items if x.get("status") == status]
        items.sort(key=lambda x: float(x.get("created_at", 0)), reverse=True)
        return items[:max(1, int(limit))]

    def export_dataset(self, *, name: str = "", include_knowledge: bool = False) -> dict[str, Any]:
        approved = [
            item for item in self._candidates.get("items", [])
            if item.get("status") == "approved"
            and item.get("kind") != "negative_feedback"
            and (include_knowledge or item.get("kind") != "sourced_knowledge")
        ]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_name = "".join(c for c in (name or f"chat-nexus-{stamp}") if c.isalnum() or c in "-_")[:80]
        path = self.datasets_dir / f"{safe_name}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for item in approved:
                row = {
                    "messages": [
                        {"role": "user", "content": item.get("instruction", "")},
                        {"role": "assistant", "content": item.get("response", "")},
                    ],
                    "metadata": {
                        "candidate_id": item.get("id"),
                        "kind": item.get("kind"),
                        "source": item.get("source"),
                    },
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        manifest = {
            "id": uuid.uuid4().hex[:12],
            "name": safe_name,
            "path": str(path),
            "created_at": time.time(),
            "examples": len(approved),
            "include_knowledge": bool(include_knowledge),
            "format": "messages-jsonl",
        }
        self._save_json(path.with_suffix(".manifest.json"), manifest)
        return manifest

    def create_training_job(
        self,
        *,
        base_model_id: str,
        dataset_path: str,
        method: str = "lora",
        output_name: str = "",
        trainer_backend: str = "external",
        trainer_command: str = "",
        hyperparameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method not in {"lora", "qlora", "full_finetune"}:
            raise ValueError("method must be lora, qlora, or full_finetune")
        dataset = Path(dataset_path).expanduser().resolve()
        if not dataset.is_file():
            raise FileNotFoundError(f"Training dataset does not exist: {dataset}")
        job_id = uuid.uuid4().hex[:12]
        name = output_name.strip() or f"{base_model_id}-{method}-{job_id[:6]}"
        output_dir = self.adapters_dir / name
        job = {
            "id": job_id,
            "base_model_id": base_model_id,
            "dataset_path": str(dataset),
            "method": method,
            "trainer_backend": trainer_backend,
            "trainer_command": trainer_command,
            "hyperparameters": dict(hyperparameters or {}),
            "output_name": name,
            "output_dir": str(output_dir),
            "status": "planned",
            "created_at": time.time(),
            "started_at": 0.0,
            "finished_at": 0.0,
            "error": "",
        }
        self._save_json(self.jobs_dir / f"{job_id}.json", job)
        return dict(job)

    def register_candidate(
        self,
        *,
        job_id: str,
        base_model_id: str,
        artifact_path: str,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = ""
        try:
            method = str(self._load_job(job_id).get("method") or "")
        except Exception:
            method = ""
        model = {
            "id": uuid.uuid4().hex[:12],
            "job_id": job_id,
            "base_model_id": base_model_id,
            "artifact_path": artifact_path,
            "method": method,
            "metrics": dict(metrics or {}),
            "status": "candidate",
            "evaluation_passed": False,
            "created_at": time.time(),
            "evaluated_at": 0.0,
            "promoted_at": 0.0,
        }
        self._registry.setdefault("models", []).append(model)
        self._save_json(self.registry_path, self._registry)
        return dict(model)

    def evaluate(self, candidate_id: str, *, passed: bool, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
        for row in self._registry.get("models", []):
            if row.get("id") == candidate_id:
                row["metrics"] = dict(metrics or row.get("metrics") or {})
                row["evaluation_passed"] = bool(passed)
                row["status"] = "evaluated" if passed else "evaluation_failed"
                row["evaluated_at"] = time.time()
                self._save_json(self.registry_path, self._registry)
                return dict(row)
        raise KeyError(candidate_id)

    def promote(self, candidate_id: str) -> dict[str, Any]:
        found = None
        for row in self._registry.get("models", []):
            if row.get("id") == candidate_id:
                if row.get("status") != "evaluated" or not row.get("evaluation_passed"):
                    raise ValueError("Candidate must pass evaluation before promotion")
                found = row
                row["status"] = "active"
                row["promoted_at"] = time.time()
            elif row.get("status") == "active":
                row["status"] = "superseded"
        if found is None:
            raise KeyError(candidate_id)
        self._registry["active_candidate_id"] = candidate_id
        self._save_json(self.registry_path, self._registry)
        return dict(found)

    def rollback(self) -> dict[str, Any]:
        active_id = str(self._registry.get("active_candidate_id") or "")
        for row in self._registry.get("models", []):
            if row.get("id") == active_id:
                row["status"] = "rolled_back"
        self._registry["active_candidate_id"] = ""
        self._save_json(self.registry_path, self._registry)
        return self.registry()

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _load_job(self, job_id: str) -> dict[str, Any]:
        path = self._job_path(job_id)
        if not path.is_file():
            raise KeyError(job_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid training job: {job_id}")
        return raw

    def start_training_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._load_job(job_id)
            if job.get("status") == "running":
                return job
            command_template = str(job.get("trainer_command") or "").strip()
            if not command_template:
                raise RuntimeError(
                    "No trainer_command is configured for this job. Configure an external LoRA/QLoRA/fine-tune trainer first."
                )
            output_dir = Path(str(job.get("output_dir") or "")).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            values = {
                "dataset": str(job.get("dataset_path") or ""),
                "output": str(output_dir),
                "base_model": str(job.get("base_model_id") or ""),
                "method": str(job.get("method") or ""),
                "job_id": str(job.get("id") or job_id),
            }
            command_text = command_template.format(**values)
            args = command_text if os.name == "nt" else shlex.split(command_text)
            if not args:
                raise ValueError("trainer_command produced an empty command")
            log_path = self.jobs_dir / f"{job_id}.log"
            log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
            process = subprocess.Popen(
                args,
                cwd=str(self.root),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._processes[job_id] = process
            job["status"] = "running"
            job["started_at"] = time.time()
            job["log_path"] = str(log_path)
            job["command_preview"] = command_text
            self._save_json(self._job_path(job_id), job)

            def wait_for_training() -> None:
                code = process.wait()
                try:
                    log_handle.close()
                except Exception:
                    pass
                with self._lock:
                    latest = self._load_job(job_id)
                    latest["finished_at"] = time.time()
                    latest["returncode"] = int(code)
                    latest["status"] = "finished" if code == 0 else "failed"
                    if code != 0:
                        latest["error"] = f"Trainer exited with code {code}; see {log_path}"
                    self._save_json(self._job_path(job_id), latest)
                    self._processes.pop(job_id, None)

            threading.Thread(
                target=wait_for_training,
                name=f"chat-nexus-training-{job_id}",
                daemon=True,
            ).start()
            return dict(job)

    def training_log(self, job_id: str, max_chars: int = 12000) -> str:
        job = self._load_job(job_id)
        path = Path(str(job.get("log_path") or ""))
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-max(1000, int(max_chars)):]
        except OSError:
            return ""

    def jobs(self) -> list[dict[str, Any]]:
        rows = []
        for path in sorted(self.jobs_dir.glob("*.json"), reverse=True):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    rows.append(raw)
            except Exception:
                continue
        return rows[:100]

    def candidate_model(self, candidate_id: str) -> dict[str, Any]:
        for row in self._registry.get("models", []):
            if row.get("id") == candidate_id:
                return dict(row)
        raise KeyError(candidate_id)

    def registry(self) -> dict[str, Any]:
        return {
            "active_candidate_id": self._registry.get("active_candidate_id", ""),
            "models": list(self._registry.get("models", [])),
        }

    def summary(self) -> dict[str, Any]:
        items = self._candidates.get("items", [])
        return {
            "root": str(self.root),
            "candidate_counts": {
                "pending": sum(1 for x in items if x.get("status") == "pending"),
                "approved": sum(1 for x in items if x.get("status") == "approved"),
                "rejected": sum(1 for x in items if x.get("status") == "rejected"),
            },
            "jobs": self.jobs(),
            "registry": self.registry(),
        }
