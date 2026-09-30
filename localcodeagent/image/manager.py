from __future__ import annotations

import json
import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .catalog import discover_image_models
from .comfyui import ComfyUIBackend
from .policy import ConsentStore, ImageSafetyPolicy
from .profiles import SubjectProfileStore
from .router import ImageRouter
from .runtime import ComfyUIRuntime
from .types import ImageJob, ImageModelProfile, ImageRequest
from .workflow import WorkflowManager


class ImageManager:
    """Coordinates image routing, queue/history, workflows, profiles and the local backend."""

    def __init__(self, *, base_dir: Path, models: list[ImageModelProfile], runtime=None, config=None) -> None:
        self.base_dir = base_dir.resolve()
        self.config = config
        self.runtime = runtime
        self.models_dir = self._resolve(getattr(config, "image_models_dir", "models/image"))
        self.data_dir = self._resolve(getattr(config, "image_data_dir", "data/image"))
        self.workflows_dir = self._resolve(getattr(config, "image_workflows_dir", "workflows/image"))
        self.generations_dir = self.data_dir / "generations"
        self.references_dir = self.data_dir / "references"
        self.characters_dir = self.data_dir / "characters"
        self.history_path = self.data_dir / "history.json"
        self.jobs_path = self.data_dir / "jobs.json"
        for p in (self.models_dir, self.generations_dir, self.references_dir, self.characters_dir, self.workflows_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.workflows = WorkflowManager(self.workflows_dir)
        self.profiles = SubjectProfileStore(self.characters_dir)
        self.consents = ConsentStore(self.data_dir / "consent_records.json")
        self.policy = ImageSafetyPolicy(self.consents)
        self.backend = ComfyUIBackend(getattr(config, "comfyui_endpoint", "http://127.0.0.1:8188"))
        self.backend_runtime = ComfyUIRuntime(base_dir=self.base_dir, backend=self.backend, config=config)
        self.router = ImageRouter(models, resource_fit=self._resource_fit)
        self._jobs: dict[str, ImageJob] = {}
        self._lock = threading.RLock()
        self._load_jobs()

    def _resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.base_dir / path).resolve()

    def _resource_fit(self, profile: ImageModelProfile) -> tuple[bool, int, str]:
        if self.runtime is None:
            return True, 0, "resource manager unavailable"
        self.runtime.refresh_hardware()
        free_vram = self.runtime.hardware.free_vram_gb
        avail_ram = self.runtime.hardware.available_ram_gb
        req_vram = max(0.0, profile.estimated_vram_gb)
        req_ram = max(0.0, profile.estimated_ram_gb)
        if req_ram and avail_ram and req_ram > avail_ram * 0.92:
            return False, -100, f"estimated RAM need {req_ram:.1f} GB exceeds available {avail_ram:.1f} GB"
        if req_vram and free_vram >= req_vram:
            return True, 25, f"fits free VRAM ({free_vram:.1f} GB)"
        if req_vram:
            return True, -5, f"will require VRAM cleanup/offload; free VRAM {free_vram:.1f} GB"
        return True, 0, "no image VRAM estimate configured"

    def _load_jobs(self) -> None:
        if not self.jobs_path.exists():
            return
        try:
            rows = json.loads(self.jobs_path.read_text(encoding="utf-8"))
            for row in rows if isinstance(rows, list) else []:
                job=ImageJob(**row)
                if job.state in {"loading_model", "generating", "refining", "upscaling"}:
                    job.state="failed"
                    job.error="Application restarted while this image job was active."
                self._jobs[job.id]=job
        except Exception:
            pass

    def _save_jobs(self) -> None:
        rows=[j.as_dict() for j in sorted(self._jobs.values(), key=lambda j:j.created_at, reverse=True)[:500]]
        self.jobs_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def summary(self) -> dict[str, Any]:
        backend_runtime = self.backend_runtime.probe()
        healthy = bool(backend_runtime.get("healthy"))
        detail = str(backend_runtime.get("error") or "")
        return {
            "enabled": bool(getattr(self.config, "image_enabled", True)),
            "backend": {"type": "comfyui", "endpoint": self.backend.endpoint, "healthy": healthy, "detail": detail[:300], "runtime": backend_runtime},
            "models": [m.as_dict() for m in self.router.models],
            "inventory": discover_image_models(self.models_dir),
            "workflows": self.workflows.list(),
            "jobs": [j.as_dict() for j in sorted(self._jobs.values(), key=lambda j:j.created_at, reverse=True)[:25]],
            "profiles": self.profiles.list(),
            "resource_mode": getattr(self.config, "image_resource_mode", "balanced"),
        }

    def save_reference(self, filename: str, data: bytes) -> str:
        safe=Path(filename or "reference.png").name
        path=self.references_dir / f"{uuid.uuid4().hex[:12]}-{safe}"
        path.write_bytes(data)
        return str(path)

    def create_job(self, request: ImageRequest, *, real_person: bool = False) -> ImageJob:
        allowed, reason = self.policy.check(request.prompt, real_person=real_person, subject=request.subject_profile)
        if not allowed:
            raise PermissionError(reason)
        decision=self.router.choose(request)
        job=ImageJob(
            id=uuid.uuid4().hex,
            request=request.as_dict(),
            state="queued", stage="queued", progress=0.0,
            model_id=decision.model_id, operation=decision.operation, workflow=decision.workflow,
            created_at=time.time(), routing_reasons=decision.reasons,
        )
        with self._lock:
            self._jobs[job.id]=job
            self._save_jobs()
        if bool(getattr(self.config, "image_auto_run_jobs", True)):
            threading.Thread(target=self._run_job, args=(job.id,), daemon=True).start()
        return job

    def _workflow_variables(self, request: ImageRequest, profile: ImageModelProfile) -> dict[str, Any]:
        return {
            "prompt": request.prompt, "negative_prompt": request.negative_prompt,
            "width": request.width, "height": request.height, "count": request.count,
            "seed": request.seed if request.seed is not None else random.randint(0, 2**31-1),
            "model_path": profile.model_path, "source_image": request.source_image,
            "mask_path": request.mask_path, "references": request.reference_images,
        }

    def _run_job(self, job_id: str) -> None:
        job=self._jobs[job_id]
        request=ImageRequest(**job.request)
        profile=self.router.get_profile(job.model_id)
        stopped=[]
        try:
            job.started_at=time.time(); job.state="loading_model"; job.stage="loading model"; job.progress=0.05
            if self.runtime is not None:
                self.runtime.refresh_hardware(); job.vram_before_gb=self.runtime.hardware.free_vram_gb
                required=max(0.0, profile.estimated_vram_gb)
                if required and self.runtime.hardware.free_vram_gb < required:
                    stopped=self.runtime.release_managed_models_for_vram(required_vram_gb=required, mode=getattr(self.config,"image_resource_mode","balanced"))
            self._save_jobs()
            self.backend_runtime.ensure_ready()
            if not profile.workflow:
                raise RuntimeError(f"Image model '{profile.id}' has no ComfyUI API workflow configured.")
            workflow=self.workflows.render(self.workflows.load(profile.workflow), self._workflow_variables(request, profile))
            job.state="generating"; job.stage="generating"; job.progress=0.15
            job.backend_job_id=self.backend.submit(workflow); self._save_jobs()
            deadline=time.monotonic()+max(30, int(getattr(self.config,"image_job_timeout",900)))
            while time.monotonic()<deadline:
                state=self.backend.status(job.backend_job_id)
                if state.get("state")=="finished":
                    break
                if state.get("state")=="failed":
                    raise RuntimeError(str(state.get("error") or "ComfyUI generation failed"))
                job.progress=max(job.progress, float(state.get("progress",0.25))); self._save_jobs(); time.sleep(0.75)
            else:
                raise TimeoutError("Timed out waiting for ComfyUI image generation")
            destination=self.generations_dir / job.id
            job.outputs=[str(p) for p in self.backend.fetch_outputs(job.backend_job_id, destination)]
            job.state="finished"; job.stage="finished"; job.progress=1.0; job.finished_at=time.time()
            if self.runtime is not None:
                self.runtime.refresh_hardware(); job.vram_after_gb=self.runtime.hardware.free_vram_gb
            self._append_history(job, profile)
        except Exception as exc:
            job.state="failed"; job.stage="failed"; job.error=f"{type(exc).__name__}: {exc}"; job.finished_at=time.time()
        finally:
            self._save_jobs()
            if stopped and bool(getattr(self.config,"image_restore_chat_model",True)) and self.runtime is not None:
                self.runtime.restore_managed_models(stopped)

    def _append_history(self, job: ImageJob, profile: ImageModelProfile) -> None:
        history=[]
        if self.history_path.exists():
            try: history=json.loads(self.history_path.read_text(encoding="utf-8"))
            except Exception: history=[]
        row={**job.as_dict(), "model": profile.as_dict()}
        history.insert(0,row)
        self.history_path.write_text(json.dumps(history[:2000], indent=2), encoding="utf-8")
        for output in job.outputs:
            meta=Path(output).with_suffix(Path(output).suffix+".json")
            meta.write_text(json.dumps(row, indent=2), encoding="utf-8")

    def cancel(self, job_id: str) -> ImageJob:
        job=self._jobs[job_id]
        if job.state in {"finished","failed","cancelled"}: return job
        if job.backend_job_id:
            self.backend.cancel(job.backend_job_id)
        job.state="cancelled"; job.stage="cancelled"; job.finished_at=time.time(); self._save_jobs(); return job

    def history(self, *, query: str = "") -> list[dict[str, Any]]:
        if not self.history_path.exists(): return []
        try: rows=json.loads(self.history_path.read_text(encoding="utf-8"))
        except Exception: return []
        if not query: return rows
        q=query.lower()
        return [r for r in rows if q in json.dumps(r, ensure_ascii=False).lower()]
