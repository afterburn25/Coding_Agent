from __future__ import annotations

import json
import random
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .catalog import discover_image_models
from .comfyui import ComfyUIBackend
from .errors import describe_image_error
from .policy import ConsentStore, ImageSafetyPolicy
from .library import ImageAssetLibrary
from .profiles import SubjectProfileStore
from .router import ImageRouter
from .runtime import ComfyUIRuntime
from .types import ImageJob, ImageModelProfile, ImageRequest
from .workflow import WorkflowManager
from ..fsutil import atomic_write_text


def _atomic_json_write(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


class ImageManager:
    """Coordinates image routing, queue/history, workflows, profiles and the local backend."""

    def __init__(self, *, base_dir: Path, models: list[ImageModelProfile], runtime=None, config=None, workspace: Path | None = None) -> None:
        self.base_dir = base_dir.resolve()
        self.config = config
        self.runtime = runtime
        self.workspace = workspace.resolve() if workspace else None
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
        self.library = ImageAssetLibrary(base_dir=self.base_dir, models_dir=self.models_dir, workflows_dir=self.workflows_dir)
        self.comfy_extra_paths = self.library.write_comfy_extra_paths(self.base_dir / ".agent" / "runtime" / "comfyui_extra_model_paths.yaml")
        self.profiles = SubjectProfileStore(self.characters_dir)
        self.consents = ConsentStore(self.data_dir / "consent_records.json")
        self.policy = ImageSafetyPolicy(self.consents)
        self.adult_content_allowed: Callable[[], bool] | None = None
        self.backend = ComfyUIBackend(getattr(config, "comfyui_endpoint", "http://127.0.0.1:8188"))
        self.backend_runtime = ComfyUIRuntime(base_dir=self.base_dir, backend=self.backend, config=config, extra_model_paths_config=self.comfy_extra_paths)
        self.router = ImageRouter(models, resource_fit=self._resource_fit)
        self._jobs: dict[str, ImageJob] = {}
        self._lock = threading.RLock()
        self._ws_listener = None
        self._backend_up_ts = 0.0
        self._backend_up_val = False
        # Optional callback invoked with {"job": job.as_dict()} on each
        # persisted state transition — wired to the server EventBus.
        self.on_change = None
        self.last_activity = time.time()
        self._resumable: list[str] = []
        self._load_jobs()
        self.resume_interrupted_jobs()

    def _ws_progress_listener(self):
        """Lazy ComfyUI /ws listener for real per-node generation progress.

        Polling /history stays authoritative; the listener only refines the
        progress fraction between polls. Failures degrade silently.
        """
        if self._ws_listener is None:
            try:
                from .ws import ComfyUIProgressListener
                self._ws_listener = ComfyUIProgressListener(self.backend.endpoint)
                self._ws_listener.start()
            except Exception:
                self._ws_listener = None
        return self._ws_listener

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
                if job.state in {"queued", "loading_model", "generating", "refining", "upscaling"}:
                    # The app exited or crashed mid-flight: clear the stale
                    # runtime state and mark for automatic resume instead of
                    # showing a stranded failure.
                    was_active = job.state != "queued"
                    job.state="queued"; job.stage="resuming after restart"
                    job.progress=0.0
                    job.error=""; job.error_code=""; job.error_message=""
                    job.technical_details=""
                    job.started_at=0.0; job.finished_at=0.0
                    self._resumable.append((job.id, was_active))
                self._jobs[job.id]=job
        except Exception:
            pass

    def resume_interrupted_jobs(self) -> int:
        """Requeue jobs that were mid-flight when the app last exited.

        Bounded by image_max_resumes so a job that reliably crashes the
        backend can't loop forever; past the bound it fails with the honest
        'application_restarted' marker.
        """
        pending = [(self._jobs[i], was_active) for i, was_active in self._resumable
                   if i in self._jobs]
        self._resumable = []
        if not pending:
            return 0
        resumed = 0
        max_resumes = max(1, int(getattr(self.config, "image_max_resumes", 2)))
        auto_run = bool(getattr(self.config, "image_auto_run_jobs", True))
        for job, was_active in pending:
            if job.resume_count >= max_resumes or (not auto_run and was_active):
                job.state="failed"; job.stage="failed"
                job.error_code="application_restarted"
                job.error_message="The application restarted while this image job was active."
                job.error=job.error_message
                job.technical_details="The persisted job was in an active state when Local Code Agent started."
                job.backend_starting=False
                self._save_jobs(job)
                continue
            if not auto_run:
                # Manual mode: never-started jobs just stay queued.
                job.stage="queued"
                self._save_jobs(job)
                continue
            job.resume_count += 1
            job.backend_starting = not self._backend_up()
            self._save_jobs(job)
            try:
                threading.Thread(target=self._run_job, args=(job.id,), daemon=True).start()
            except Exception as exc:
                job.state = "failed"
                job.stage = "failed"
                job.error_code = "worker_spawn_failed"
                job.error_message = f"worker spawn failed: {type(exc).__name__}: {exc}"
                job.error = job.error_message
                self._save_jobs(job)
                continue
            resumed += 1
        return resumed

    def _save_jobs(self, job: "ImageJob | None" = None) -> None:
        self.last_activity = time.time()
        with self._lock:
            ordered = sorted(self._jobs.values(), key=lambda j:j.created_at, reverse=True)
            rows=[j.as_dict() for j in ordered[:500]]
            _atomic_json_write(self.jobs_path, rows)
            # The file keeps the newest 500 — evict evicted rows from memory
            # too, unless the job is still live (a running job must stay
            # reachable even if it somehow falls past the tail).
            _LIVE = {"queued", "loading_model", "generating", "refining",
                     "upscaling", "cancelling"}
            for stale in ordered[500:]:
                if stale.state not in _LIVE:
                    self._jobs.pop(stale.id, None)
        if job is not None and self.on_change is not None:
            try:
                self.on_change({"job": job.as_dict()})
            except Exception:
                pass

    def summary(self) -> dict[str, Any]:
        backend_runtime = self.backend_runtime.probe()
        healthy = bool(backend_runtime.get("healthy"))
        detail = str(backend_runtime.get("error") or "")
        return {
            "enabled": bool(getattr(self.config, "image_enabled", True)),
            "backend": {"type": "comfyui", "endpoint": self.backend.endpoint, "healthy": healthy, "detail": detail[:300], "runtime": backend_runtime},
            "models": [m.as_dict() for m in self.router.models],
            "inventory": discover_image_models(self.models_dir),
            "model_status": self.library.verify_all(self.router.models),
            "loras": self.library.list_loras(),
            "installs": self.library.install_jobs(),
            "workflows": self.workflows.list(),
            "comfy_extra_model_paths": str(self.comfy_extra_paths),
            "jobs": [j.as_dict() for j in sorted(self._jobs.values(), key=lambda j:j.created_at, reverse=True)[:25]],
            "profiles": self.profiles.list(),
            "resource_mode": getattr(self.config, "image_resource_mode", "balanced"),
        }



    def import_workflow(self, model_id: str, operation: str, workflow: dict[str, Any]) -> dict[str, Any]:
        profile = self.router.get_profile(model_id)
        operation = str(operation or "").strip()
        configured = profile.workflows or ({"default": profile.workflow} if profile.workflow else {})
        if operation not in configured:
            raise ValueError(f"{model_id} has no configured workflow for operation {operation!r}")
        name = profile.workflow_for(operation)
        if not name:
            raise ValueError(f"{model_id} has no workflow destination configured for {operation!r}")
        saved = self.workflows.save_api(name, workflow)
        return {
            "model_id": profile.id,
            "operation": operation,
            "workflow": saved,
            "model_status": self.library.verify_model(profile),
        }

    def verify_models(self, *, deep_hash: bool = False) -> list[dict[str, Any]]:
        return self.library.verify_all(self.router.models, deep_hash=deep_hash)

    def start_model_install(self, model_id: str, *, repair: bool = False) -> dict[str, Any]:
        profile=self.router.get_profile(model_id)
        return self.library.start_install(profile, repair=repair)

    def remove_model(self, model_id: str) -> list[str]:
        profile=self.router.get_profile(model_id)
        return self.library.remove_model(profile)

    def save_lora_metadata(self, lora_path: str, metadata: dict[str, Any]) -> dict[str, Any]:
        return self.library.save_lora_metadata(lora_path, metadata)

    def save_reference(self, filename: str, data: bytes) -> str:
        safe=Path(filename or "reference.png").name
        path=self.references_dir / f"{uuid.uuid4().hex[:12]}-{safe}"
        path.write_bytes(data)
        return str(path)

    def _apply_subject_profile(self, request: ImageRequest) -> dict[str, Any] | None:
        if not request.subject_profile:
            return None
        subject = self.profiles.get(request.subject_profile)
        saved_refs=list(subject.get("reference_images") or [])
        for key in ("face_reference", "body_reference"):
            value=str(subject.get(key) or "").strip()
            if value:
                saved_refs.append(value)
        request.reference_images=list(dict.fromkeys([*request.reference_images, *saved_refs]))
        preferred=str(subject.get("preferred_model") or "").strip()
        if preferred and request.model_override in {"", "auto"}:
            request.model_override=preferred
        assigned=[]
        for item in list(subject.get("loras") or []):
            assigned.append({"name": item} if isinstance(item, str) else item)
        legacy_lora=subject.get("assigned_lora")
        if legacy_lora:
            if isinstance(legacy_lora, dict):
                legacy=dict(legacy_lora)
            else:
                legacy={"name": str(legacy_lora)}
            if subject.get("lora_version") and not legacy.get("version"):
                legacy["version"]=str(subject.get("lora_version"))
            if subject.get("lora_strength") is not None and legacy.get("strength") is None:
                legacy["strength"]=float(subject.get("lora_strength"))
            assigned.append(legacy)
        if assigned:
            request.loras=[*assigned, *request.loras]
        defaults=subject.get("generation_defaults") or {}
        if isinstance(defaults, dict):
            defaults_map={"quality":"balanced","width":1024,"height":1024,"count":1,"steps":None,"guidance":None,"image_strength":None,"denoise_strength":None}
            for key,default in defaults_map.items():
                if key in defaults and getattr(request,key)==default:
                    setattr(request,key,defaults[key])
        return subject

    def _backend_up(self) -> bool:
        """5s-cached health probe — a batch create_job loop must not pay a
        connection-timeout per job when ComfyUI is down."""
        now = time.time()
        if now - self._backend_up_ts < 5.0:
            return self._backend_up_val
        try:
            self._backend_up_val = bool(self.backend.health()[0])
        except Exception:
            self._backend_up_val = False
        self._backend_up_ts = now
        return self._backend_up_val

    def create_job(self, request: ImageRequest, *, real_person: bool = False) -> ImageJob:
        self._apply_subject_profile(request)
        if (
            self.adult_content_allowed is not None
            and self.policy.is_explicit(request.prompt)
            and not bool(self.adult_content_allowed())
        ):
            raise PermissionError(
                "Adult/explicit image generation is disabled by the creator-locked Nexus Brain."
            )
        allowed, reason = self.policy.check(request.prompt, real_person=real_person, subject=request.subject_profile)
        if not allowed:
            raise PermissionError(reason)
        decision=self.router.choose(request)
        # Probe once up front so the UI/copy can distinguish "ComfyUI is
        # already up" from a cold start that may take minutes.
        backend_up = self._backend_up()
        job=ImageJob(
            id=uuid.uuid4().hex,
            request=request.as_dict(),
            state="queued", stage="queued", progress=0.0,
            model_id=decision.model_id, operation=decision.operation, workflow=decision.workflow,
            created_at=time.time(), routing_reasons=decision.reasons,
            backend_starting=not backend_up,
        )
        with self._lock:
            self._jobs[job.id]=job
            self._save_jobs(job)
        if bool(getattr(self.config, "image_auto_run_jobs", True)):
            try:
                threading.Thread(target=self._run_job, args=(job.id,), daemon=True).start()
            except Exception as exc:
                job.state = "failed"
                job.stage = "failed"
                job.error_code = "worker_spawn_failed"
                job.error_message = f"worker spawn failed: {type(exc).__name__}: {exc}"
                job.error = job.error_message
                self._save_jobs(job)
        return job

    def _safe_input_path(self, value: str) -> Path:
        path=Path(value).expanduser().resolve()
        roots=[self.references_dir.resolve(), self.generations_dir.resolve(), self.characters_dir.resolve()]
        if self.workspace is not None:
            roots.append(self.workspace.resolve())
        if not any(path.is_relative_to(root) for root in roots):
            raise PermissionError("Image inputs must come from the workspace or Local Code Agent image data directories.")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _upload_backend_input(self, value: str) -> str:
        if not value:
            return ""
        path=self._safe_input_path(value)
        result=self.backend.upload_image(path)
        name=str(result.get("name") or path.name)
        sub=str(result.get("subfolder") or "").strip("/\\")
        return f"{sub}/{name}" if sub else name

    def _workflow_variables(self, request: ImageRequest, profile: ImageModelProfile, resolved_loras: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        source=self._upload_backend_input(request.source_image) if request.source_image else ""
        refs=[self._upload_backend_input(x) for x in request.reference_images]
        mask=self._upload_backend_input(request.mask_path) if request.mask_path else ""
        variables: dict[str, Any] = {
            "prompt": request.prompt, "negative_prompt": request.negative_prompt,
            "width": request.width, "height": request.height, "count": request.count,
            "seed": request.seed if request.seed is not None else random.randint(0, 2**31-1),
            "steps": request.steps if request.steps is not None else (4 if profile.speed_tier == "fast" else 25),
            # CFG 1.0 is correct for distilled models (FLUX Klein); full
            # checkpoints like SDXL need real guidance.
            "guidance": request.guidance if request.guidance is not None else (6.5 if "stable-diffusion" in profile.family else 1.0),
            "model_path": Path(profile.model_path).name if profile.model_path else "",
            "source_image": source, "mask_path": mask, "references": refs,
            "image_strength": request.image_strength if request.image_strength is not None else 1.0,
            "denoise_strength": request.denoise_strength if request.denoise_strength is not None else 1.0,
        }
        for spec in profile.components:
            key=str(spec.get("key") or "").strip()
            raw=str(spec.get("path") or "")
            if key and raw:
                variables[key]=Path(raw).name
                variables[f"component_{key}"]=Path(raw).name
        for idx,ref in enumerate(refs,1):
            variables[f"reference_{idx}"]=ref
        for idx,lora in enumerate(resolved_loras or [],1):
            variables[f"lora_{idx}_name"]=str(lora.get("name") or "")
            variables[f"lora_{idx}_strength"]=float(lora.get("strength",1.0))
        return variables

    @staticmethod
    def _validate_lora_slots(workflow_name: str, workflow_status: dict[str, Any], resolved_loras: list[dict[str, Any]]) -> None:
        if not resolved_loras:
            return
        tokens=set(workflow_status.get("unresolved_tokens") or [])
        for idx,_lora in enumerate(resolved_loras,1):
            name_token=f"lora_{idx}_name"
            strength_token=f"lora_{idx}_strength"
            missing=[token for token in (name_token, strength_token) if token not in tokens]
            if missing:
                placeholders=", ".join(f"${{{token}}}" for token in missing)
                raise RuntimeError(
                    f"Workflow '{workflow_name}' does not expose complete LoRA slot {idx} ({placeholders}). "
                    "Import an API workflow with both name and strength template slots for every selected LoRA, "
                    "or remove that LoRA selection."
                )

    def _run_job(self, job_id: str) -> None:
        job=self._jobs[job_id]
        request=ImageRequest(**job.request)
        profile=self.router.get_profile(job.model_id)
        stopped=[]
        try:
            job.started_at=time.time(); job.state="loading_model"; job.stage="validating workflow"; job.progress=0.03
            self._save_jobs(job)
            workflow_name=profile.workflow_for(job.operation)
            if not workflow_name:
                raise RuntimeError(f"Image model '{profile.id}' has no ComfyUI API workflow configured for {job.operation}.")
            workflow_status=self.workflows.inspect(workflow_name)
            if not workflow_status.get("exists"):
                raise RuntimeError(f"ComfyUI workflow is missing: {workflow_name}")
            if not workflow_status.get("valid"):
                detail="; ".join(str(x) for x in workflow_status.get("errors",[])[:4]) or "invalid API workflow"
                raise RuntimeError(f"ComfyUI workflow '{workflow_name}' is not executable: {detail}")

            resolved_loras=self.library.resolve_loras(request.loras, profile)
            self._validate_lora_slots(workflow_name, workflow_status, resolved_loras)
            if resolved_loras:
                job.resolved_loras=resolved_loras

            verification=self.library.verify_model(profile)
            if not verification.get("installed"):
                missing=[c["key"] for c in verification.get("components",[]) if c.get("required") and not c.get("ok")]
                raise RuntimeError(f"Image model '{profile.id}' is not fully installed. Missing/invalid: {', '.join(missing) or 'required components'}")

            job.stage="loading model"; job.progress=0.08
            self._save_jobs(job)
            if self.runtime is not None:
                self.runtime.refresh_hardware(); job.vram_before_gb=self.runtime.hardware.free_vram_gb
                required=max(0.0, profile.estimated_vram_gb)
                if required and self.runtime.hardware.free_vram_gb < required:
                    stopped=self.runtime.release_managed_models_for_vram(required_vram_gb=required, mode=getattr(self.config,"image_resource_mode","balanced"))
            self._save_jobs(job)
            job.stage="starting ComfyUI" if job.backend_starting else "connecting to ComfyUI"
            job.progress=max(job.progress,0.10); self._save_jobs(job)
            self.backend_runtime.ensure_ready()
            job.backend_starting=False
            job.stage="preparing workflow"; job.progress=max(job.progress,0.12); self._save_jobs(job)
            if profile.required_nodes:
                info=self.backend.inspect().get("object_info", {})
                available=set(info) if isinstance(info, dict) else set()
                missing_nodes=[name for name in profile.required_nodes if name not in available]
                if missing_nodes:
                    raise RuntimeError("ComfyUI is missing required node(s): " + ", ".join(missing_nodes) + ". Update ComfyUI or install the required node implementation.")
            workflow=self.workflows.render(self.workflows.load(workflow_name), self._workflow_variables(request, profile, resolved_loras))
            rendered_status=self.workflows.validate_api(workflow)
            if not rendered_status.get("valid"):
                raise RuntimeError("Rendered ComfyUI workflow failed validation: " + "; ".join(rendered_status.get("errors",[])[:4]))
            unresolved=rendered_status.get("unresolved_tokens",[])
            if unresolved:
                raise RuntimeError("Rendered ComfyUI workflow still contains unresolved variable(s): " + ", ".join(unresolved))
            job.state="generating"; job.stage="generating"; job.progress=0.20
            job.backend_job_id=self.backend.submit(workflow); self._save_jobs(job)
            listener=self._ws_progress_listener()
            gen_started=time.monotonic()
            deadline=gen_started+max(30, int(getattr(self.config,"image_job_timeout",900)))
            while time.monotonic()<deadline:
                state=self.backend.status(job.backend_job_id)
                if state.get("state")=="finished":
                    break
                if state.get("state")=="failed":
                    raise RuntimeError(str(state.get("error") or "ComfyUI generation failed"))
                job.progress=max(job.progress, float(state.get("progress",0.25)))
                ws_node=None
                if listener is not None:
                    ws_progress, ws_node = listener.progress_for(job.backend_job_id)
                    if ws_progress:
                        # Map ComfyUI's 0..1 step fraction into the generating band.
                        job.progress=max(job.progress, 0.20 + 0.70 * ws_progress)
                if ws_node:
                    job.stage="generating" + (f" · node {ws_node}" if ws_node else "")
                # ComfyUI only emits step fractions inside the sampler; model
                # loads/encodes report nothing, which left the bar frozen at
                # 20%. Ramp toward 0.85 over ~4 min so the UI stays honest.
                elapsed=time.monotonic()-gen_started
                job.progress=max(job.progress, min(0.85, 0.20 + 0.65 * (elapsed / 240.0)))
                self._save_jobs(job); time.sleep(0.75)
            else:
                raise TimeoutError("Timed out waiting for ComfyUI image generation")
            job.state="generating"; job.stage="saving image"; job.progress=max(job.progress,0.92); self._save_jobs(job)
            destination=self.generations_dir / job.id
            job.outputs=[str(p) for p in self.backend.fetch_outputs(job.backend_job_id, destination)]
            # Mirror finished outputs into the user-facing output folder so
            # generated images are easy to find outside the app.
            try:
                output_setting = str(getattr(self.config, "image_output_dir", "") or "").strip()
                if output_setting:
                    output_dir = self._resolve(output_setting)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    mirrored = []
                    for p in job.outputs:
                        src = Path(p)
                        if src.is_file():
                            shutil.copy2(src, output_dir / src.name)
                            mirrored.append(str(output_dir / src.name))
                    if mirrored:
                        job.outputs = job.outputs + mirrored
            except OSError:
                pass
            job.state="finished"; job.stage="finished"; job.progress=1.0; job.finished_at=time.time()
            if self.runtime is not None:
                self.runtime.refresh_hardware(); job.vram_after_gb=self.runtime.hardware.free_vram_gb
            self._append_history(job, profile)
        except Exception as exc:
            error=describe_image_error(exc)
            job.state="failed"; job.stage="failed"
            job.error_code=error["code"]; job.error_message=error["message"]; job.error=job.error_message
            job.technical_details=error["technical_details"]; job.finished_at=time.time()
            try:
                from ..netdiag import is_transport_failure, record_failure
                if is_transport_failure(exc):
                    record_failure(exc)
            except Exception:
                pass
        finally:
            self._save_jobs(job)
            if stopped and bool(getattr(self.config,"image_restore_chat_model",True)) and self.runtime is not None:
                self.runtime.restore_managed_models(stopped)

    def _append_history(self, job: ImageJob, profile: ImageModelProfile) -> None:
        with self._lock:
            history=[]
            if self.history_path.exists():
                try: history=json.loads(self.history_path.read_text(encoding="utf-8"))
                except Exception: history=[]
            row={**job.as_dict(), "model": profile.as_dict()}
            history.insert(0,row)
            _atomic_json_write(self.history_path, history[:2000])
        for output in job.outputs:
            meta=Path(output).with_suffix(Path(output).suffix+".json")
            meta.write_text(json.dumps(row, indent=2), encoding="utf-8")

    def cancel(self, job_id: str) -> ImageJob:
        job=self._jobs[job_id]
        if job.state in {"finished","failed","cancelled"}: return job
        if job.backend_job_id:
            self.backend.cancel(job.backend_job_id)
        job.state="cancelled"; job.stage="cancelled"; job.finished_at=time.time(); self._save_jobs(job); return job

    def get_job(self, job_id: str) -> ImageJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"Unknown image job {job_id}") from exc

    def has_active_jobs(self) -> bool:
        return any(j.state in {"queued", "loading_model", "generating", "refining", "upscaling"} for j in self._jobs.values())

    def history(self, *, query: str = "") -> list[dict[str, Any]]:
        if not self.history_path.exists(): return []
        try: rows=json.loads(self.history_path.read_text(encoding="utf-8"))
        except Exception: return []
        if not query: return rows
        q=query.lower()
        return [r for r in rows if q in json.dumps(r, ensure_ascii=False).lower()]
