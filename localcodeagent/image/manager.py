from __future__ import annotations

import json
import random
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .catalog import classify_model, discover_image_models
from .comfyui import ComfyUIBackend
from .errors import describe_image_error
from .invokeai import InvokeAIBackend
from .invokeai_runtime import InvokeAIRuntime
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


class _ImageJobCancelled(Exception):
    pass


class ImageManager:
    """Coordinates image routing, queue/history, workflows, profiles and the local backend."""

    def __init__(self, *, base_dir: Path, models: list[ImageModelProfile], runtime=None, config=None, workspace: Path | None = None,
                 comfy_installer=None, job_lookup=None) -> None:
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
        # InvokeAI — the preferred primary engine for standard generation/
        # editing; ComfyUI stays the advanced/custom-workflow fallback. Both
        # hang off the same contract so jobs/route/history stay uniform.
        self.invokeai_backend = InvokeAIBackend(
            getattr(config, "invokeai_endpoint", "http://127.0.0.1:9090"))
        self.invokeai_runtime = InvokeAIRuntime(
            base_dir=self.base_dir, backend=self.invokeai_backend, config=config)
        self.router = ImageRouter(models, resource_fit=self._resource_fit)
        from .sampling import SamplingAdvisor
        self.sampling_advisor = SamplingAdvisor(self.data_dir / "sampling_stats.json")
        self._jobs: dict[str, ImageJob] = {}
        self._lock = threading.RLock()
        self._ws_listener = None
        self._backend_up_ts: dict[str, float] = {}
        self._backend_up_val: dict[str, bool] = {}
        self._invokeai_models_ts = 0.0
        self._invokeai_model_cache: list[dict[str, Any]] = []
        # Optional callback invoked with {"job": job.as_dict()} on each
        # persisted state transition — wired to the server EventBus.
        self.on_change = None
        self.last_activity = time.time()
        self._resumable: list[str] = []
        # Bundled "install everything" flow: ComfyUI runtime + every configured
        # image model, driven by one persisted record so an app restart resumes
        # instead of leaving the workspace half-provisioned.
        self._comfy_installer = comfy_installer  # () -> {"ok","job_id","error"}
        self._job_lookup = job_lookup            # (job_id) -> job dict | None
        self.on_setup_change = None
        # Fired when a generation request arrives with no ComfyUI anywhere —
        # the server uses it to surface a one-click install offer.
        self.on_missing_backend = None
        self._setup_path = self.data_dir / "setup.json"
        self._setup_lock = threading.RLock()
        self._setup_thread: threading.Thread | None = None
        self._setup = self._load_setup()
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
            # RAM is reclaimable — resident LLMs can be evicted before the
            # job runs. Only hard-fail when the estimate exceeds TOTAL RAM.
            total_ram = getattr(self.runtime.hardware, "total_ram_gb", 0.0)
            if total_ram and req_ram > total_ram * 0.92:
                return False, -100, f"estimated RAM need {req_ram:.1f} GB exceeds total {total_ram:.1f} GB"
            return True, -10, (f"RAM tight ({avail_ram:.1f} GB free of {total_ram:.1f}); "
                               "idle models will be unloaded before the job runs")
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
            job.backend_starting = not self._backend_up(job.backend or "comfyui")
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

    # -- bundled setup (ComfyUI + all image models) -------------------------

    _SETUP_ACTIVE = {"installing_backend", "installing_models"}

    def _load_setup(self) -> dict[str, Any]:
        try:
            row = json.loads(self._setup_path.read_text(encoding="utf-8"))
            return row if isinstance(row, dict) else {}
        except Exception:
            return {}

    def _save_setup(self) -> None:
        try:
            _atomic_json_write(self._setup_path, self._setup)
        except OSError:
            pass

    def _update_setup(self, **fields: Any) -> None:
        with self._setup_lock:
            base = dict(self._setup or {})
            base.update(fields)
            base["updated_at"] = time.time()
            base.setdefault("model_ids", [m.id for m in self.router.models])
            self._setup = base
            self._save_setup()
        if self.on_setup_change is not None:
            try:
                self.on_setup_change(self.setup_state())
            except Exception:
                pass

    def _job_state(self, job_id: str | None) -> dict[str, Any] | None:
        if not job_id or self._job_lookup is None:
            return None
        try:
            return self._job_lookup(job_id)
        except Exception:
            return None

    def setup_state(self) -> dict[str, Any]:
        """Live view of the bundled install — persisted phase plus per-model
        and ComfyUI-download progress for the progress bar."""
        with self._setup_lock:
            row = dict(self._setup or {})
        row.setdefault("state", "idle")
        backend_dir, _py = self.backend_runtime.discover()
        comfy_job = self._job_state(row.get("comfy_job_id"))
        wanted = set(row.get("model_ids") or [m.id for m in self.router.models])
        live = {j["model_id"]: j for j in self.library.install_jobs()
                if j.get("model_id") in wanted}
        models = []
        for profile in self.router.models:
            installed = bool(self.library.verify_model(profile).get("installed"))
            job = live.get(profile.id)
            models.append({
                "id": profile.id,
                "display_name": profile.display_name or profile.id,
                "installed": installed,
                "state": (job or {}).get("state") or ("installed" if installed else "pending"),
                "progress": (job or {}).get("progress", 0.0) if not installed else 1.0,
                "current_file": (job or {}).get("current_file") or "",
                "error": (job or {}).get("error") or "",
            })
        # Overall: backend phase 0–40%, model phase 40–100%.
        backend_frac = 1.0 if backend_dir else float((comfy_job or {}).get("progress") or 0.0)
        model_frac = (sum(m["progress"] for m in models) / len(models)) if models else 1.0
        row["backend_installed"] = backend_dir is not None
        row["comfy_job"] = comfy_job
        row["models"] = models
        row["progress"] = min(1.0, 0.4 * backend_frac + 0.6 * model_frac)
        if row["state"] == "done" or (backend_dir and all(m["installed"] for m in models)):
            row["progress"] = 1.0
        return row

    def start_setup(self) -> dict[str, Any]:
        """Kick off (or re-attach to) the bundled ComfyUI + models install."""
        with self._setup_lock:
            alive = (self._setup or {}).get("state") in self._SETUP_ACTIVE \
                and self._setup_thread is not None and self._setup_thread.is_alive()
            if alive:
                return self.setup_state()
            self._setup = {"state": "installing_backend", "error": "",
                           "started_at": time.time(), "updated_at": time.time(),
                           "model_ids": [m.id for m in self.router.models]}
            self._save_setup()
            self._setup_thread = threading.Thread(target=self._run_setup, daemon=True)
            self._setup_thread.start()
            return self.setup_state()

    def resume_setup(self) -> None:
        """An interrupted setup re-enters the supervisor on boot — every phase
        re-derives from disk state, so restarts converge instead of restart.
        Deferred until the server has wired the installer/job callables."""
        if (self._setup or {}).get("state") in self._SETUP_ACTIVE:
            self._setup_thread = threading.Thread(target=self._run_setup, daemon=True)
            self._setup_thread.start()

    def _run_setup(self) -> None:
        try:
            directory, _python = self.backend_runtime.discover()
            if not directory:
                if self._comfy_installer is None:
                    raise RuntimeError("ComfyUI is not installed and no installer is available.")
                job_id = str((self._setup or {}).get("comfy_job_id") or "")
                live = self._job_state(job_id)
                if live is not None and live.get("state") in {"queued", "running", "preparing"}:
                    outcome = {"ok": True, "job_id": job_id}
                else:
                    outcome = self._comfy_installer() or {}
                if not outcome.get("ok"):
                    raise RuntimeError(str(outcome.get("error") or "ComfyUI install could not be started"))
                job_id = str(outcome.get("job_id") or job_id)
                self._update_setup(state="installing_backend", comfy_job_id=job_id)
                deadline = time.time() + 6 * 3600
                while time.time() < deadline:
                    live = self._job_state(job_id)
                    if live is None:
                        if self.backend_runtime.discover()[0]:
                            break
                        time.sleep(2)
                        continue
                    state = str(live.get("state") or "")
                    if state == "completed":
                        break
                    if state in {"failed", "cancelled"}:
                        raise RuntimeError(str(live.get("error") or "ComfyUI install was cancelled"))
                    self._update_setup()
                    time.sleep(2)
                else:
                    raise RuntimeError("ComfyUI install timed out")
                if not self.backend_runtime.discover()[0]:
                    raise RuntimeError("ComfyUI install finished but the runtime was not detected on disk")

            self._update_setup(state="installing_models")
            wanted = set((self._setup or {}).get("model_ids") or [m.id for m in self.router.models])
            for profile in self.router.models:
                if profile.id not in wanted:
                    continue
                if self.library.verify_model(profile).get("installed"):
                    continue
                self.library.start_install(profile)
            deadline = time.time() + 12 * 3600
            while time.time() < deadline:
                live_jobs = {j["model_id"]: j for j in self.library.install_jobs()
                             if j.get("model_id") in wanted}
                if not any(j.get("state") in {"queued", "downloading"} for j in live_jobs.values()):
                    break
                self._update_setup()
                time.sleep(2)
            else:
                raise RuntimeError("Image model downloads timed out")
            live_jobs = {j["model_id"]: j for j in self.library.install_jobs()
                         if j.get("model_id") in wanted}
            failures = []
            for profile in self.router.models:
                if profile.id not in wanted:
                    continue
                if not self.library.verify_model(profile).get("installed"):
                    job = live_jobs.get(profile.id) or {}
                    failures.append(f"{profile.display_name or profile.id}: "
                                    f"{job.get('error') or 'required components missing'}")
            if failures:
                raise RuntimeError("model installs incomplete — " + "; ".join(failures))
            self._update_setup(state="done", error="")
        except Exception as exc:
            self._update_setup(state="failed", error=f"{type(exc).__name__}: {exc}")

    def summary(self) -> dict[str, Any]:
        backend_runtime = self.backend_runtime.probe()
        healthy = bool(backend_runtime.get("healthy"))
        detail = str(backend_runtime.get("error") or "")
        invoke_runtime = self.invokeai_runtime.probe()
        invoke_models = self._invokeai_models()
        return {
            "enabled": bool(getattr(self.config, "image_enabled", True)),
            "backend": {"type": "comfyui", "endpoint": self.backend.endpoint, "healthy": healthy, "detail": detail[:300], "runtime": backend_runtime},
            # Multi-backend view for the router/UI; "backend" stays
            # ComfyUI-shaped for backward compatibility.
            "backend_preference": str(getattr(self.config, "image_backend", "auto") or "auto"),
            "backends": {
                "invokeai": {
                    "type": "invokeai", "endpoint": self.invokeai_backend.endpoint,
                    "healthy": bool(invoke_runtime.get("healthy")),
                    "detail": str(invoke_runtime.get("error") or "")[:300],
                    "runtime": invoke_runtime,
                    "capabilities": sorted(self.invokeai_backend.capabilities()),
                    "models": [self._invokeai_model_row(m) for m in invoke_models],
                },
                "comfyui": {
                    "type": "comfyui", "endpoint": self.backend.endpoint,
                    "healthy": healthy, "detail": detail[:300],
                    "runtime": backend_runtime,
                    "capabilities": sorted(self.backend.capabilities()),
                },
            },
            "models": [m.as_dict() for m in self.router.models],
            "inventory": discover_image_models(self.models_dir),
            "model_status": self.library.verify_all(self.router.models),
            "loras": self.library.list_loras(),
            "installs": self.library.install_jobs(),
            "setup": self.setup_state(),
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

    @property
    def backends(self) -> dict[str, Any]:
        """Live view — resolves attributes each call so stubs/tests that
        replace ``self.backend`` propagate everywhere."""
        return {"comfyui": self.backend, "invokeai": self.invokeai_backend}

    @property
    def backend_runtimes(self) -> dict[str, Any]:
        return {"comfyui": self.backend_runtime, "invokeai": self.invokeai_runtime}

    def _backend_up(self, name: str = "comfyui") -> bool:
        """5s-cached per-backend health probe — a batch create_job loop must
        not pay a connection-timeout per job when a backend is down."""
        now = time.time()
        if now - self._backend_up_ts.get(name, 0.0) < 5.0:
            return self._backend_up_val.get(name, False)
        try:
            self._backend_up_val[name] = bool(self.backends[name].health()[0])
        except Exception:
            self._backend_up_val[name] = False
        self._backend_up_ts[name] = now
        return self._backend_up_val.get(name, False)

    # -- backend selection -------------------------------------------------

    _INVOKEAI_OPS = {"text_to_image", "edit_image", "inpaint", "variation",
                     "upscale"}

    def _invokeai_ready(self) -> tuple[bool, str]:
        """(can-serve, why-not). Healthy OR installed-and-startable counts."""
        if self._backend_up("invokeai"):
            return True, ""
        root, _prefix = self.invokeai_runtime.discover()
        if root is not None and bool(
                getattr(self.config, "invokeai_start_on_image_request", True)
                or getattr(self.config, "invokeai_auto_start", False)):
            return True, ""
        if root is None:
            return False, "InvokeAI is not installed"
        return False, "InvokeAI is offline"

    def _invokeai_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        """InvokeAI's own model registry, 30s-cached; empty when offline."""
        now = time.time()
        if not force and self._invokeai_models_ts and now - self._invokeai_models_ts < 30.0:
            return self._invokeai_model_cache
        rows: list[dict[str, Any]] = []
        try:
            if self._backend_up("invokeai"):
                rows = self.invokeai_backend.models()
            elif self.invokeai_runtime.discover()[0] is not None:
                # Backend installed but stopped: read InvokeAI's own model
                # registry (SQLite) so the router still sees its models and
                # Auto can pick InvokeAI — the job then cold-starts the
                # server via ensure_ready(). Without this, a stopped
                # InvokeAI is invisible to routing and every request
                # silently falls back to ComfyUI.
                rows = self.invokeai_runtime.registry_models()
        except Exception:
            rows = []
        self._invokeai_model_cache = rows
        self._invokeai_models_ts = now
        return rows

    @staticmethod
    def _invokeai_model_row(m: dict[str, Any]) -> dict[str, Any]:
        name = str(m.get("name") or m.get("key") or "")
        cls = classify_model(name, family=str(m.get("base") or ""),
                             notes=str(m.get("description") or ""),
                             model_type=str(m.get("type") or ""))
        from .fleet import fleet_for_model_name
        row = {
            "key": m.get("key"), "name": name,
            "base": m.get("base"), "type": m.get("type"),
            "format": m.get("format"), "hash": m.get("hash"),
            "backend": "invokeai",
            "capability_class": cls["capability_class"],
            "restriction_status": cls["restriction_status"],
        }
        spec = None
        for cand in (name, str(m.get("source") or ""),
                     str(m.get("path") or "")):
            spec = fleet_for_model_name(cand)
            if spec:
                break
        if spec:
            row["fleet_id"] = spec["id"]
            row["fleet_role"] = spec["role"]
            row["display_name"] = spec["display_name"]
            row["license"] = spec["license_name"]
            row["adult_capable"] = bool(spec.get("adult_capable"))
            row["fleet_source"] = spec.get("homepage") or ""
        return row

    def _refresh_invokeai_models(self) -> None:
        """Merge InvokeAI-discovered models into the router pool as
        synthesized profiles (deduped by key; refreshed on each choose)."""
        base = [m for m in self.router.models
                if not m.id.startswith("invokeai:")]
        from .fleet import fleet_for_model_name
        for row in self._invokeai_models():
            if str(row.get("type") or "") != "main":
                continue
            key = str(row.get("key") or row.get("name") or "")
            if not key:
                continue
            base_name = str(row.get("base") or "sdxl").lower()
            family = {"sdxl": "stable-diffusion-xl", "sdxl-refiner": "stable-diffusion-xl",
                      "sd-1": "stable-diffusion", "sd-2": "stable-diffusion",
                      "flux": "flux", "sd-3": "stable-diffusion-3"}.get(base_name, base_name or "unknown")
            caps = ["text_to_image", "image_edit", "inpaint"]
            name = str(row.get("name") or key)
            cls = classify_model(name, family=family,
                                 notes=str(row.get("description") or ""))
            profile = ImageModelProfile(
                id=f"invokeai:{key}", family=family, backend="invokeai",
                model_path=str(row.get("path") or row.get("name") or ""),
                display_name=name, capabilities=caps,
                speed_tier="balanced", quality_tier="high" if "xl" in family else "balanced",
                notes=f"InvokeAI {row.get('format','')} model ({base_name})",
                capability_class=cls["capability_class"],
                restriction_status=cls["restriction_status"],
            )
            profile.metadata = dict(profile.metadata or {})
            profile.metadata["invokeai_model"] = {k: row.get(k) for k in
                ("key", "hash", "name", "base", "type", "format", "description")
                if row.get(k) is not None}
            # Fleet match: a known managed model (Juggernaut/RealVis/
            # CyberRealistic) gets role, scoring weights, and its default
            # sampling profile attached so routing + SamplingAdvisor can
            # use them.
            spec = None
            for candidate in (name, str(row.get("source") or ""),
                              str(row.get("path") or "")):
                spec = fleet_for_model_name(candidate)
                if spec:
                    break
            if spec:
                profile.metadata["fleet_id"] = spec["id"]
                profile.metadata["fleet_role"] = spec["role"]
                profile.metadata["fleet_display"] = spec["display_name"]
                profile.metadata["fleet_source"] = spec["invokeai_source"]
                profile.metadata["fleet_license"] = spec["license_name"]
                profile.display_name = spec["display_name"]
                profile.capability_class = "photoreal"
                profile.restriction_status = spec["restriction_status"]
                for k, v in (spec.get("sampling") or {}).items():
                    profile.metadata.setdefault("sampling", {})[k] = v
                profile.metadata["sampling"]["model_scope"] = spec["id"]
            base.append(profile)
        self.router.models = [m for m in base if m.enabled]

    def _select_backend(self, request: ImageRequest) -> tuple[str, list[str]]:
        """Pick the engine for this request. Returns (name, reasons).

        Order: explicit request override → configured preference → auto.
        Auto prefers InvokeAI for standard ops it advertises, ComfyUI for
        ops that need its specialized/custom workflows, with honest fallback
        when the preferred engine can't serve."""
        override = (request.backend_override or "").strip().lower()
        if override not in {"", "auto", "invokeai", "comfyui"}:
            raise ValueError(f"unknown image backend '{override}'")
        configured = str(getattr(self.config, "image_backend", "auto") or "auto").lower()
        choice = override or configured
        reasons: list[str] = []

        if choice == "invokeai":
            return "invokeai", ["manual backend override: invokeai"]
        if choice == "comfyui":
            return "comfyui", ["manual backend override: comfyui"]

        operation, op_reasons = self.router.infer_operation(request)
        reasons.extend(op_reasons)

        invoke_ok, invoke_why = self._invokeai_ready()
        comfy_ready = (self.backend_runtime.discover()[0] is not None
                       or self._backend_up("comfyui"))

        # Ops InvokeAI cannot express natively go to ComfyUI when a
        # configured workflow covers them (e.g. custom background removal,
        # outpainting, imported node graphs).
        if operation not in self._INVOKEAI_OPS or request.transparent_background:
            if comfy_ready:
                reasons.append(
                    f"ComfyUI handles '{operation}' via configured workflows"
                    + (" (InvokeAI does not support it natively)"
                       if operation not in self._INVOKEAI_OPS else ""))
                return "comfyui", reasons
            if invoke_ok:
                reasons.append(
                    f"operation '{operation}' unsupported on InvokeAI and ComfyUI "
                    "is unavailable — trying InvokeAI anyway")
                return "invokeai", reasons
            return "comfyui", reasons + ["no backend is currently available"]

        if invoke_ok:
            reasons.append("auto: InvokeAI preferred for standard generation/editing")
            return "invokeai", reasons
        if comfy_ready:
            reasons.append(f"auto: falling back to ComfyUI — {invoke_why}")
            return "comfyui", reasons
        return "invokeai", reasons + ["no image backend is currently available"]

    def _validate_request_inputs(self, request: ImageRequest) -> None:
        operation, _ = self.router.infer_operation(request)
        source_ops = {"edit_image", "inpaint", "outpaint", "remove_background", "upscale"}
        if operation in source_ops and not request.source_image and request.reference_images:
            request.source_image = request.reference_images[0]
        if operation in source_ops and not request.source_image:
            raise ValueError(f"{operation.replace('_', ' ')} requires a source or reference image")
        if operation == "inpaint" and not request.mask_path:
            raise ValueError("inpaint requires a saved mask")

    def create_job(self, request: ImageRequest, *, real_person: bool = False) -> ImageJob:
        self._apply_subject_profile(request)
        self._validate_request_inputs(request)
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
        backend_name, backend_reasons = self._select_backend(request)
        runtime = self.backend_runtimes[backend_name]
        explicit = (request.backend_override or "").strip().lower() in {"invokeai", "comfyui"}

        def _installed_and_up() -> bool:
            if backend_name == "comfyui":
                return self.backend_runtime.discover()[0] is not None or self._backend_up("comfyui")
            return self.invokeai_runtime.discover()[0] is not None or self._backend_up("invokeai")

        if not _installed_and_up():
            if not explicit:
                # Auto can fall back to the other engine when it can serve
                # the operation — never claim it, just reroute with a reason.
                other = "comfyui" if backend_name == "invokeai" else "invokeai"
                other_runtime = self.backend_runtimes[other]
                other_installed = (other_runtime.discover()[0] is not None
                                   or self._backend_up(other))
                operation, _ = self.router.infer_operation(request)
                other_supports = (other == "comfyui") or operation in self._INVOKEAI_OPS
                if other_installed and other_supports:
                    backend_name = other
                    backend_reasons.append(
                        f"auto fallback: preferred backend unavailable — routed to {other}")
                    runtime = other_runtime
                    explicit = False
            if not _installed_and_up():
                if self.on_missing_backend is not None:
                    try:
                        self.on_missing_backend()
                    except Exception:
                        pass
                label = "InvokeAI" if backend_name == "invokeai" else "ComfyUI"
                raise RuntimeError(
                    f"{label} is not installed or running — image generation cannot run without it. "
                    "Nothing is generating right now. An install/setup offer is available.")
        if backend_name == "invokeai":
            self._refresh_invokeai_models()
        try:
            decision=self.router.choose(request, backend=backend_name)
        except RuntimeError:
            if not explicit and backend_name == "invokeai":
                # Auto + InvokeAI has no model for this op — ComfyUI may.
                decision = self.router.choose(request, backend="comfyui")
                backend_name = "comfyui"
                backend_reasons.append(
                    "auto fallback: no InvokeAI model serves this operation — routed to ComfyUI")
            else:
                raise
        decision.reasons = backend_reasons + decision.reasons
        # Best-guess sampling params (and anything learned from prior
        # feedback) fill whatever the caller left unset — explicit user
        # controls like "cfg 4" or "denoise 0.6" always win.
        try:
            guess_notes = self.sampling_advisor.apply(
                request, self.router.get_profile(decision.model_id),
                decision.operation, backend=backend_name)
            decision.reasons.extend(guess_notes)
        except Exception:
            pass
        # Probe once up front so the UI/copy can distinguish "backend is
        # already up" from a cold start that may take minutes.
        backend_up = self._backend_up(backend_name)
        job=ImageJob(
            id=uuid.uuid4().hex,
            request=request.as_dict(),
            state="queued", stage="queued", progress=0.0,
            model_id=decision.model_id, operation=decision.operation, workflow=decision.workflow,
            created_at=time.time(), routing_reasons=decision.reasons,
            backend_starting=not backend_up,
            backend=backend_name,
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

    def record_feedback(self, job_id: str, rating: str) -> bool:
        """Thumbs on an image-bearing message -> sampling outcome."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        try:
            profile = self.router.get_profile(job.model_id)
        except KeyError:
            return False
        self.sampling_advisor.record_outcome(
            profile, str(job.operation or "auto"),
            dict(job.request or {}), rating,
            backend=str(job.backend or "comfyui"))
        return True

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
            # CFG is family-specific: distilled models (FLUX Klein) run at 1.0,
            # full checkpoints need real guidance — SDXL ~6.5, Qwen Image ~4.
            # At cfg 1.0 a full model ignores conditioning and emits noise.
            "guidance": request.guidance if request.guidance is not None else self._default_guidance(profile),
            # Sampler/scheduler also follow the model family's verified
            # defaults; explicit request values always win.
            "sampler_name": request.sampler_name or self._default_sampler(profile)[0],
            "scheduler": request.scheduler or self._default_sampler(profile)[1],
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
    def _default_guidance(profile: ImageModelProfile) -> float:
        family = (profile.family or "").lower()
        if "stable-diffusion" in family:
            return 6.5
        if "qwen" in family:
            return 4.0
        if "flux" in family and "klein" in family:
            return 1.0
        if "z-image" in family or "turbo" in family:
            return 1.0
        return 4.0

    @staticmethod
    def _default_sampler(profile: ImageModelProfile) -> tuple[str, str]:
        family = (profile.family or "").lower()
        if "stable-diffusion" in family:
            return "dpmpp_2m", "karras"
        # Qwen Image and FLUX official workflows use euler/simple.
        return "euler", "simple"

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

    def _submit_and_wait(self, job: ImageJob, workflow: dict[str, Any], *, stage: str,
                         progress_start: float, progress_end: float) -> str:
        job.state = stage
        job.stage = stage
        job.progress = max(job.progress, progress_start)
        job.backend_job_id = self.backend.submit(workflow)
        self._save_jobs(job)
        listener = self._ws_progress_listener()
        started = time.monotonic()
        deadline = started + max(30, int(getattr(self.config, "image_job_timeout", 900)))
        while time.monotonic() < deadline:
            if job.state == "cancelled":
                raise _ImageJobCancelled()
            state = self.backend.status(job.backend_job_id)
            if state.get("state") == "finished":
                return job.backend_job_id
            if state.get("state") == "failed":
                raise RuntimeError(str(state.get("error") or "ComfyUI generation failed"))
            job.progress = max(job.progress, float(state.get("progress", progress_start)))
            ws_node = None
            if listener is not None:
                ws_progress, ws_node = listener.progress_for(job.backend_job_id)
                if ws_progress:
                    job.progress = max(job.progress, progress_start + (progress_end - progress_start) * ws_progress)
            if ws_node:
                job.stage = stage + (f" · node {ws_node}" if ws_node else "")
            # ComfyUI only emits step fractions inside the sampler; model
            # loads/encodes report nothing, which left the bar frozen. Ramp
            # toward the stage end over ~4 min so the UI stays honest.
            elapsed = time.monotonic() - started
            job.progress = max(job.progress, min(progress_end - 0.05,
                progress_start + (progress_end - progress_start - 0.05) * (elapsed / 240.0)))
            self._save_jobs(job)
            time.sleep(0.75)
        raise TimeoutError("Timed out waiting for ComfyUI image generation")

    def _profile_ready(self, profile: ImageModelProfile, operation: str) -> str:
        workflow_name = profile.workflow_for(operation)
        if not workflow_name:
            raise RuntimeError(f"Image model '{profile.id}' has no ComfyUI API workflow configured for {operation}.")
        workflow_status = self.workflows.inspect(workflow_name)
        if not workflow_status.get("exists"):
            raise RuntimeError(f"ComfyUI workflow is missing: {workflow_name}")
        if not workflow_status.get("valid"):
            detail = "; ".join(str(x) for x in workflow_status.get("errors", [])[:4]) or "invalid API workflow"
            raise RuntimeError(f"ComfyUI workflow '{workflow_name}' is not executable: {detail}")
        verification = self.library.verify_model(profile)
        if not verification.get("installed"):
            missing = [c["key"] for c in verification.get("components", []) if c.get("required") and not c.get("ok")]
            raise RuntimeError(f"Image model '{profile.id}' is not fully installed. Missing/invalid: {', '.join(missing) or 'required components'}")
        if profile.required_nodes:
            info = self.backend.inspect().get("object_info", {})
            available = set(info) if isinstance(info, dict) else set()
            missing_nodes = [name for name in profile.required_nodes if name not in available]
            if missing_nodes:
                raise RuntimeError("ComfyUI is missing required node(s): " + ", ".join(missing_nodes) + ". Update ComfyUI or install the required node implementation.")
        return workflow_name

    def _run_upscale_stage(self, job: ImageJob, request: ImageRequest, source_path: str) -> list[str]:
        up_request = ImageRequest(
            prompt=f"Upscale image from job {job.id}",
            operation="upscale",
            source_image=source_path,
            metadata={**request.metadata, "parent_job": job.id},
        )
        decision = self.router.choose(up_request)
        profile = self.router.get_profile(decision.model_id)
        workflow_name = self._profile_ready(profile, "upscale")
        workflow = self.workflows.render(
            self.workflows.load(workflow_name),
            self._workflow_variables(up_request, profile),
        )
        rendered_status = self.workflows.validate_api(workflow)
        if not rendered_status.get("valid"):
            raise RuntimeError("Rendered upscaler workflow failed validation: " + "; ".join(rendered_status.get("errors", [])[:4]))
        unresolved = rendered_status.get("unresolved_tokens", [])
        if unresolved:
            raise RuntimeError("Rendered upscaler workflow still contains unresolved variable(s): " + ", ".join(unresolved))
        backend_job_id = self._submit_and_wait(
            job, workflow, stage="upscaling", progress_start=0.93, progress_end=0.98)
        job.routing_reasons.append(f"post-process upscaler: {profile.id}")
        return [str(p) for p in self.backend.fetch_outputs(
            backend_job_id, self.generations_dir / job.id / "upscaled")]

    def _invokeai_spec(self, job: ImageJob, request: ImageRequest,
                       profile: ImageModelProfile) -> dict[str, Any]:
        """Normalized generation spec for InvokeAIBackend.submit."""
        invoke_model = dict((profile.metadata or {}).get("invokeai_model") or {})
        if not invoke_model and profile.model_path:
            invoke_model = {"name": Path(profile.model_path).name}
        spec: dict[str, Any] = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "width": request.width, "height": request.height,
            "count": max(1, int(request.count or 1)),
            "seed": request.seed,
            "steps": request.steps,
            "guidance": request.guidance,
            "denoise_strength": request.denoise_strength
                if request.denoise_strength is not None else request.image_strength,
            "sampler_name": request.sampler_name,
            "scheduler": request.scheduler,
            "model": invoke_model,
            "loras": [],
        }
        # Upload inputs through InvokeAI's own image endpoint — it tracks
        # them by image_name, not by filesystem path.
        if request.source_image:
            path = self._safe_input_path(request.source_image)
            uploaded = self.invokeai_backend.upload_image(path)
            spec["source_image_name"] = str(uploaded.get("image_name") or "")
        if request.mask_path:
            path = self._safe_input_path(request.mask_path)
            uploaded = self.invokeai_backend.upload_image(path)
            spec["mask_image_name"] = str(uploaded.get("image_name") or "")
        # LoRAs: resolve to InvokeAI lora model records.
        if request.loras:
            available = {str(m.get("name") or ""): m
                         for m in self._invokeai_models()
                         if str(m.get("type") or "") == "lora"}
            for lora in request.loras[:4]:
                name = str(lora.get("name") or lora.get("id") or "")
                if name in available:
                    spec["loras"].append({"model": available[name],
                                          "strength": float(lora.get("strength", 1.0))})
        return spec

    @staticmethod
    def _is_swept_state_error(message: str) -> bool:
        """The backend's ephemeral state dir (outputs/tensors|conditioning
        tmp*) was deleted under it — e.g. a second InvokeAI instance on
        the same root sweeping 'dangling' tempdirs at startup."""
        return "does not exist" in message and (
            "Parent directory" in message
            or "/outputs/" in message.replace("\\", "/"))

    def _await_invokeai_job(self, job: ImageJob) -> None:
        started = time.monotonic()
        deadline = started + max(30, int(getattr(self.config, "image_job_timeout", 900)))
        while time.monotonic() < deadline:
            if job.state == "cancelled":
                raise _ImageJobCancelled()
            state = self.invokeai_backend.status(job.backend_job_id)
            if state.get("state") == "finished":
                return
            if state.get("state") == "failed":
                raise RuntimeError(str(state.get("error") or "InvokeAI generation failed"))
            frac = float(state.get("progress") or 0.0)
            elapsed = time.monotonic() - started
            job.progress = max(job.progress, min(
                0.90, 0.20 + 0.70 * max(frac, elapsed / 240.0)))
            self._save_jobs(job)
            time.sleep(1.0)
        raise TimeoutError("Timed out waiting for InvokeAI image generation")

    def _evict_peer_image_backend(self, job: ImageJob, active: str) -> bool:
        """Park the *other* image backend when it is Nexus-managed.

        Two resident image servers can exhaust VRAM/RAM together — the
        observed failure was a resident ComfyUI starving InvokeAI's model
        load until it died mid-job. Only Nexus-owned processes are
        evicted (evict_if_managed refuses user-owned external servers);
        the peer returns on its next request via start_on_image_request."""
        peer = "comfyui" if active == "invokeai" else "invokeai"
        runtime = self.backend_runtimes.get(peer)
        if runtime is None:
            return False
        try:
            status = runtime.probe()
        except Exception:
            return False
        if not status.get("healthy"):
            return False
        if runtime.evict_if_managed():
            label = "ComfyUI" if peer == "comfyui" else "InvokeAI"
            job.routing_reasons.append(
                f"resource arbitration: parked Nexus-managed {label} — "
                "free memory was below this job's estimate")
            return True
        return False

    def _run_invokeai_job(self, job: ImageJob) -> None:
        request = ImageRequest(**job.request)
        profile = self.router.get_profile(job.model_id)
        stopped = []
        try:
            job.started_at = time.time(); job.state = "loading_model"
            job.stage = "preparing request"; job.progress = 0.03
            self._save_jobs(job)

            if self.runtime is not None:
                self.runtime.refresh_hardware()
                job.vram_before_gb = self.runtime.hardware.free_vram_gb
                required = max(0.0, profile.estimated_vram_gb)
                if required and self.runtime.hardware.free_vram_gb < required:
                    stopped = self.runtime.release_managed_models_for_vram(
                        required_vram_gb=required,
                        mode=getattr(self.config, "image_resource_mode", "balanced"))
                required_ram = max(0.0, profile.estimated_ram_gb)
                headroom = required_ram * 1.15 + 2.0
                if required_ram and self.runtime.hardware.available_ram_gb < headroom:
                    job.stage = "freeing memory"; self._save_jobs(job)
                    self.runtime.release_managed_models_for_ram(required_ram_gb=headroom)
                # LLM eviction may not be enough when the *other* image
                # backend is resident — InvokeAI died mid-job under exactly
                # this contention. Park a managed ComfyUI before submitting.
                self.runtime.refresh_hardware()
                if (required and self.runtime.hardware.free_vram_gb < required) or \
                        (required_ram and self.runtime.hardware.available_ram_gb < headroom):
                    job.stage = "freeing memory"; self._save_jobs(job)
                    self._evict_peer_image_backend(job, "invokeai")

            job.stage = "starting InvokeAI" if job.backend_starting else "connecting to InvokeAI"
            job.progress = max(job.progress, 0.10); self._save_jobs(job)
            self.invokeai_runtime.ensure_ready()
            job.backend_starting = False

            job.stage = "preparing generation"; job.progress = max(job.progress, 0.14)
            spec = self._invokeai_spec(job, request, profile)
            if not spec.get("model"):
                raise RuntimeError(
                    "model_missing: no InvokeAI model is selected — install/import a "
                    "model in InvokeAI or pick another image model")

            job.state = "generating"; job.stage = "generating"
            job.progress = max(job.progress, 0.20)

            # InvokeAI's ephemeral object store writes tensors into ONE
            # TemporaryDirectory created at startup; a second InvokeAI
            # instance on the same root sweeps tmp* dirs on boot and kills
            # it ("Parent directory ... does not exist" on every save).
            # The same bounded restart-and-resubmit covers transport-level
            # backend crashes (connection refused/reset mid-job) — one
            # recovery attempt, never an infinite loop. Timeouts and
            # cancellations are NOT retried here.
            from ..netdiag import BackendConnectionError
            for attempt in range(2):
                try:
                    job.backend_job_id = self.invokeai_backend.submit(spec)
                    self._save_jobs(job)
                    self._await_invokeai_job(job)
                    break
                except RuntimeError as exc:
                    retryable = self._is_swept_state_error(str(exc)) \
                        or isinstance(exc, BackendConnectionError)
                    if attempt or not retryable:
                        raise
                    job.stage = "restarting InvokeAI"; self._save_jobs(job)
                    try:
                        self.invokeai_runtime.stop()
                    except Exception:
                        pass
                    self.invokeai_runtime.ensure_ready()

            job.stage = "saving image"; job.progress = max(job.progress, 0.92)
            self._save_jobs(job)
            destination = self.generations_dir / job.id
            job.outputs = [str(p) for p in
                           self.invokeai_backend.fetch_outputs(job.backend_job_id, destination)]
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
            job.state = "finished"; job.stage = "finished"; job.progress = 1.0
            job.finished_at = time.time()
            if self.runtime is not None:
                self.runtime.refresh_hardware()
                job.vram_after_gb = self.runtime.hardware.free_vram_gb
            self._append_history(job, profile)
        except _ImageJobCancelled:
            pass
        except Exception as exc:
            error = describe_image_error(exc)
            job.state = "failed"; job.stage = "failed"
            job.error_code = error["code"]; job.error_message = error["message"]
            job.error = job.error_message
            job.technical_details = error["technical_details"]
            job.finished_at = time.time()
            try:
                from ..netdiag import is_transport_failure, record_failure
                if is_transport_failure(exc):
                    record_failure(exc)
            except Exception:
                pass
        finally:
            self._save_jobs(job)
            if stopped and bool(getattr(self.config, "image_restore_chat_model", True)) \
                    and self.runtime is not None:
                self.runtime.restore_managed_models(stopped)

    def _run_job(self, job_id: str) -> None:
        job=self._jobs[job_id]
        if job.backend == "invokeai":
            self._run_invokeai_job(job)
            return
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
                # RAM is just as binding: ComfyUI memory-maps checkpoint files
                # (a HostBuffer read failure kills the run), and resident LLMs
                # can hold most of physical RAM. Evict them, lightest-first,
                # when the job's RAM estimate exceeds what's actually free.
                required_ram=max(0.0, profile.estimated_ram_gb)
                headroom=required_ram*1.15+2.0  # OS + ComfyUI overhead on top of weights
                if required_ram and self.runtime.hardware.available_ram_gb < headroom:
                    job.stage="freeing memory"; self._save_jobs(job)
                    self.runtime.release_managed_models_for_ram(required_ram_gb=headroom)
                # Same cross-backend contention as the InvokeAI path — park
                # a Nexus-managed InvokeAI if memory is still short.
                self.runtime.refresh_hardware()
                if (required and self.runtime.hardware.free_vram_gb < required) or \
                        (required_ram and self.runtime.hardware.available_ram_gb < headroom):
                    job.stage="freeing memory"; self._save_jobs(job)
                    self._evict_peer_image_backend(job, "comfyui")
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
            # A ComfyUI crash mid-job surfaces as BackendConnectionError —
            # restart once and resubmit (bounded; timeouts and cancels are
            # never retried).
            from ..netdiag import BackendConnectionError
            for attempt in range(2):
                try:
                    self._submit_and_wait(
                        job, workflow, stage="generating", progress_start=0.20, progress_end=0.90)
                    break
                except BackendConnectionError:
                    if attempt:
                        raise
                    job.stage="restarting ComfyUI"; self._save_jobs(job)
                    try:
                        self.backend_runtime.stop()
                    except Exception:
                        pass
                    self.backend_runtime.ensure_ready()
            job.state="generating"; job.stage="saving image"; job.progress=max(job.progress,0.92); self._save_jobs(job)
            destination=self.generations_dir / job.id
            job.outputs=[str(p) for p in self.backend.fetch_outputs(job.backend_job_id, destination)]
            if request.upscale and job.operation != "upscale" and job.outputs:
                try:
                    upscaled=self._run_upscale_stage(job, request, job.outputs[0])
                    if upscaled:
                        job.outputs=upscaled + job.outputs
                except _ImageJobCancelled:
                    raise
                except Exception as exc:
                    # Optional post-processing must not discard a successful
                    # generation; surface the failure while preserving output.
                    job.routing_reasons.append(
                        f"post-process upscaler failed: {type(exc).__name__}: {exc}")
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
        except _ImageJobCancelled:
            pass
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
            backend = self.backends.get(job.backend) or self.backend
            backend.cancel(job.backend_job_id)
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
