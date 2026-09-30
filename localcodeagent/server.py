from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .agent.orchestrator import AgentOrchestrator
from .image.manager import ImageManager
from .config import AgentConfig
from .models.router import ModelRouter
from .runtime.manager import RuntimeManager
from .research import ResearchCoordinator
from .tools.base import ToolRegistry
from .tools.filesystem import register_filesystem_tools
from .tools.git import register_git_tools
from .tools.github import register_github_tools
from .tools.image import register_image_tools
from .tools.repository import register_repository_tools
from .tools.research import register_research_tools
from .tools.shell import register_shell_tools
from .tools.web import register_web_tools
from .workflow.checkpoint import CheckpointManager
from .workflow.memory import ProjectMemory
from .workflow.repository import RepositoryIndex
from .workflow.tasks import TaskStore


VERSION = "0.6.0-dev"


class AppState:
    def __init__(self, config: AgentConfig, workspace: Path, runtime_root: Path) -> None:
        self.config = config
        self.workspace = workspace.resolve()
        self.runtime = RuntimeManager(config, base_dir=runtime_root)
        self.router = ModelRouter(config.models, resource_advisor=self.runtime.resource_fit)
        self.images = ImageManager(base_dir=runtime_root, models=config.image_models, runtime=self.runtime, config=config, workspace=self.workspace)
        self.tasks = TaskStore(self.workspace)
        self.checkpoints = CheckpointManager(self.workspace)
        self.memory = ProjectMemory(self.workspace)
        self.repository_index = RepositoryIndex(self.workspace)
        self.research = ResearchCoordinator(self.workspace, self.repository_index, config)
        self.tools = ToolRegistry(config.permissions)
        register_filesystem_tools(self.tools, self.workspace, checkpoints=self.checkpoints, tasks=self.tasks)
        register_shell_tools(self.tools, self.workspace)
        register_git_tools(self.tools, self.workspace)
        if config.github_enabled:
            register_github_tools(self.tools, self.workspace, config)
        register_repository_tools(self.tools, self.repository_index)
        if config.research_enabled:
            register_research_tools(self.tools, self.research)
        register_web_tools(self.tools, runtime_root=runtime_root)
        if config.image_enabled:
            register_image_tools(self.tools, self.images)
        self.agent = AgentOrchestrator(
            config,
            self.router,
            self.tools,
            self.runtime,
            tasks=self.tasks,
            checkpoints=self.checkpoints,
            memory=self.memory,
            repository_index=self.repository_index,
            research=self.research,
        )
        self.history: list[dict] = []

    def task_payload(self) -> dict:
        current = self.tasks.current()
        return {
            "current": current.as_dict() if current else None,
            "recent": self.tasks.recent(12),
        }


class Handler(BaseHTTPRequestHandler):
    state: AppState
    web_root: Path

    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _sse_begin(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_event(self, event: str, payload: dict) -> bool:
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str)
            self.wfile.write(f"event: {event}\\ndata: {data}\\n\\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _image_job_payload(self, job) -> dict:
        row = job.as_dict()
        urls: list[str] = []
        root = self.state.images.generations_dir.resolve()
        for output in job.outputs:
            try:
                rel = Path(output).resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                continue
            urls.append("/api/image/output/" + quote(rel, safe="/"))
        row["output_urls"] = urls
        return row

    def _agent_image_jobs(self, result) -> list[dict]:
        image_tools = {"generate_image", "edit_image", "inpaint_image", "outpaint_image", "remove_background", "upscale_image", "create_image_variations"}
        rows: list[dict] = []
        seen: set[str] = set()
        for event in result.tool_events:
            if event.get("name") not in image_tools:
                continue
            try:
                payload = json.loads(str(event.get("result") or "{}"))
                job_id = str((payload.get("job") or {}).get("id") or "")
                if not job_id or job_id in seen:
                    continue
                rows.append(self._image_job_payload(self.state.images.get_job(job_id)))
                seen.add(job_id)
            except Exception:
                continue
        return rows

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            runtime = self.state.runtime.summary(probe_external=False)
            self._json({
                "version": VERSION,
                "workspace": str(self.state.workspace),
                "models": [
                    {
                        "id": m.id,
                        "roles": m.roles,
                        "endpoint": m.endpoint,
                        "enabled": m.enabled,
                        "runtime": m.runtime,
                        "model_path": m.model_path,
                    }
                    for m in self.state.config.models
                ],
                "permissions": self.state.config.permissions,
                "runtime": runtime,
                "tasks": self.state.task_payload(),
                "repository_index": self.state.repository_index.summary(),
                "research": self.state.research.summary(),
                "image": self.state.images.summary(),
            })
            return
        if path == "/api/models":
            self._json({"models": [asdict(m) for m in self.state.config.models]})
            return
        if path == "/api/runtime":
            self.state.runtime.refresh_hardware()
            self._json(self.state.runtime.summary(probe_external=True))
            return
        if path == "/api/tasks":
            self._json(self.state.task_payload())
            return
        if path == "/api/index":
            self._json(self.state.repository_index.summary())
            return
        if path == "/api/research":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            payload = self.state.research.cached(q) if q else self.state.research.summary()
            self._json(payload)
            return
        if path == "/api/image":
            self._json(self.state.images.summary())
            return
        if path.startswith("/api/image/job/"):
            job_id = unquote(path[len("/api/image/job/"):]).strip("/")
            if not job_id:
                self._json({"error": "job id is required"}, 400)
                return
            try:
                job = self.state.images.get_job(job_id)
            except KeyError:
                self._json({"error": "image job not found"}, 404)
                return
            self._json({"job": self._image_job_payload(job)})
            return
        if path == "/api/image/history":
            query = urlparse(self.path).query
            from urllib.parse import parse_qs
            q = parse_qs(query).get("q", [""])[0]
            self._json({"history": self.state.images.history(query=q)})
            return
        if path.startswith("/api/image/output/"):
            rel = unquote(path[len("/api/image/output/"):].strip("/"))
            target = (self.state.images.generations_dir / rel).resolve()
            root = self.state.images.generations_dir.resolve()
            if not target.is_relative_to(root) or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        target = self.web_root / ("index.html" if path == "/" else path.lstrip("/"))
        if not target.resolve().is_relative_to(self.web_root.resolve()) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _agent_response(self, result) -> None:
        self._json({
            "content": result.content,
            "routing": {
                "role": result.routing.role,
                "model_id": result.routing.model_id,
                "complexity": result.routing.complexity,
                "reasons": result.routing.reasons,
            },
            "tool_events": result.tool_events,
            "model_events": result.model_events,
            "steps": result.steps,
            "task": result.task,
            "pending_approval": result.pending_approval,
            "verification": result.verification,
            "review": result.review,
            "research": result.research,
            "image_jobs": self._agent_image_jobs(result),
            "runtime": self.state.runtime.summary(probe_external=False),
        })

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/research/plan":
                task = str(body.get("task", "")).strip()
                if not task:
                    self._json({"error": "task is required"}, 400)
                    return
                plan = self.state.research.prepare_task(task, mode=str(body.get("mode", "auto")))
                self._json({"ok": True, **plan})
                return

            if path == "/api/research/run":
                query = str(body.get("query", "")).strip()
                if not query:
                    self._json({"error": "query is required"}, 400)
                    return
                session = self.state.research.research_topic(query, mode=str(body.get("mode", "auto")), version=str(body.get("version", "")))
                self._json({"ok": True, "session": session})
                return

            if path == "/api/image/upload":
                filename = str(body.get("filename", "reference.png"))
                encoded = str(body.get("data_base64", ""))
                if not encoded:
                    self._json({"error": "data_base64 is required"}, 400)
                    return
                if "," in encoded and encoded.lower().startswith("data:"):
                    encoded = encoded.split(",", 1)[1]
                data = base64.b64decode(encoded, validate=True)
                if len(data) > 50 * 1024 * 1024:
                    self._json({"error": "image upload exceeds 50 MB"}, 413)
                    return
                saved = self.state.images.save_reference(filename, data)
                self._json({"ok": True, "path": saved})
                return

            if path == "/api/image/generate":
                from .image.types import ImageRequest
                allowed = set(ImageRequest.__dataclass_fields__)
                try:
                    request = ImageRequest(**{k: v for k, v in body.items() if k in allowed})
                    job = self.state.images.create_job(request, real_person=bool(body.get("real_person", False)))
                except PermissionError as exc:
                    self._json({"error": str(exc)}, 403)
                    return
                except (ValueError, TypeError, RuntimeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"ok": True, "job": job.as_dict()})
                return

            if path == "/api/image/backend/start":
                self.state.images.backend_runtime.start()
                self._json({"ok": True, "image": self.state.images.summary()})
                return

            if path == "/api/image/backend/stop":
                self.state.images.backend_runtime.stop()
                self._json({"ok": True, "image": self.state.images.summary()})
                return

            if path == "/api/image/backend/inspect":
                self._json({"ok": True, "backend": self.state.images.backend.inspect()})
                return


            if path == "/api/image/workflows/import":
                model_id = str(body.get("model_id", "")).strip()
                operation = str(body.get("operation", "")).strip()
                workflow = body.get("workflow")
                if not model_id or not operation or not isinstance(workflow, dict):
                    self._json({"error": "model_id, operation, and workflow object are required"}, 400)
                    return
                try:
                    result = self.state.images.import_workflow(model_id, operation, workflow)
                except (ValueError, PermissionError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"ok": True, **result})
                return

            if path == "/api/image/models/verify":
                self._json({"ok": True, "models": self.state.images.verify_models(deep_hash=bool(body.get("deep_hash", False)))})
                return

            if path == "/api/image/models/install":
                model_id = str(body.get("model_id", "")).strip()
                if not model_id:
                    self._json({"error": "model_id is required"}, 400)
                    return
                job = self.state.images.start_model_install(model_id, repair=bool(body.get("repair", False)))
                self._json({"ok": True, "install": job})
                return

            if path == "/api/image/models/remove":
                model_id = str(body.get("model_id", "")).strip()
                if not model_id:
                    self._json({"error": "model_id is required"}, 400)
                    return
                removed = self.state.images.remove_model(model_id)
                self._json({"ok": True, "removed": removed})
                return

            if path == "/api/image/loras/metadata":
                lora_path = str(body.get("path", "")).strip()
                metadata = body.get("metadata")
                if not lora_path or not isinstance(metadata, dict):
                    self._json({"error": "path and metadata object are required"}, 400)
                    return
                saved = self.state.images.save_lora_metadata(lora_path, metadata)
                self._json({"ok": True, "metadata": saved})
                return

            if path == "/api/image/cancel":
                job_id = str(body.get("job_id", ""))
                if not job_id:
                    self._json({"error": "job_id is required"}, 400)
                    return
                job = self.state.images.cancel(job_id)
                self._json({"ok": True, "job": job.as_dict()})
                return

            if path == "/api/image/profile":
                profile = body.get("profile")
                if not isinstance(profile, dict):
                    self._json({"error": "profile object is required"}, 400)
                    return
                saved = self.state.images.profiles.save(profile)
                self._json({"ok": True, "profile": saved})
                return

            if path == "/api/image/consent":
                from .image.policy import ConsentStore
                subject = str(body.get("subject", "")).strip()
                if not subject:
                    self._json({"error": "subject is required"}, 400)
                    return
                record = ConsentStore.create(subject, scope=str(body.get("scope", "")), notes=str(body.get("notes", "")))
                self.state.images.consents.save(record)
                self._json({"ok": True, "consent": record.as_dict()})
                return

            if path == "/api/chat":
                message = str(body.get("message", "")).strip()
                mode = str(body.get("mode", "auto"))
                if not message:
                    self._json({"error": "message is required"}, 400)
                    return
                result = self.state.agent.run(message, history=self.state.history, mode=mode)
                self.state.history.extend([
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": result.content},
                ])
                self._agent_response(result)
                return

            if path == "/api/tasks/resume":
                task_id = str(body.get("task_id", "")).strip()
                approved = bool(body.get("approved", False))
                if not task_id:
                    self._json({"error": "task_id is required"}, 400)
                    return
                result = self.state.agent.resume(task_id, approved=approved)
                if result.task.get("status") not in {"waiting_approval", "running", "verifying", "reviewing"}:
                    self.state.history.append({"role": "assistant", "content": result.content})
                self._agent_response(result)
                return

            if path == "/api/tasks/recover":
                task_id = str(body.get("task_id", "")).strip()
                if not task_id:
                    self._json({"error": "task_id is required"}, 400)
                    return
                result = self.state.agent.recover(task_id)
                if result.task.get("status") not in {"waiting_approval", "running", "verifying", "reviewing"}:
                    self.state.history.append({"role": "assistant", "content": result.content})
                self._agent_response(result)
                return

            if path == "/api/tasks/undo":
                task_id = str(body.get("task_id", "")).strip()
                task = self.state.tasks.get(task_id)
                if task.status in {"running", "waiting_approval", "verifying", "reviewing"}:
                    self._json({"error": "Cannot undo a task while it is actively running or waiting for approval."}, 409)
                    return
                restored = self.state.checkpoints.restore(task_id)
                self.state.tasks.update(task_id, reverted=True, status="reverted", phase="done")
                try:
                    self.state.repository_index.build()
                except Exception:
                    pass
                self._json({"ok": True, "task_id": task_id, "restored": restored, "tasks": self.state.task_payload()})
                return

            if path == "/api/index/rebuild":
                summary = self.state.repository_index.build()
                self._json({"ok": True, "repository_index": summary})
                return

            if path in {"/api/runtime/start", "/api/runtime/stop"}:
                model_id = str(body.get("model_id", "")).strip()
                profile = self.state.router.get_profile(model_id)
                if path.endswith("/start"):
                    endpoint = self.state.runtime.ensure_ready(profile)
                    self._json({"ok": True, "model_id": model_id, "endpoint": endpoint, "runtime": self.state.runtime.summary()})
                else:
                    if profile.runtime == "external":
                        self._json({"error": "External runtimes are not controlled by Chat Nexus."}, 400)
                    else:
                        status = self.state.runtime.stop_model(model_id)
                        self._json({"ok": True, "status": status.as_dict(), "runtime": self.state.runtime.summary()})
                return

            if path == "/api/runtime/refresh":
                self.state.runtime.refresh_hardware()
                self._json(self.state.runtime.summary(probe_external=True))
                return

            if path == "/api/chat/reset":
                self.state.history.clear()
                self._json({"ok": True})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except KeyError as exc:
            self._json({"error": f"Not found: {exc}"}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def log_message(self, format: str, *args) -> None:
        pass


def serve(config: AgentConfig, workspace: Path, host: str, port: int, web_root: Path, runtime_root: Path) -> None:
    state = AppState(config, workspace, runtime_root)
    handler = type("LocalCodeAgentHandler", (Handler,), {"state": state, "web_root": web_root})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Chat Nexus v{VERSION}")
    print(f"Workspace: {workspace.resolve()}")
    print(f"UI: http://{host}:{port}")
    print(f"Models: {state.runtime.models_dir}")
    if state.runtime.hardware.gpus:
        for gpu in state.runtime.hardware.gpus:
            print(f"GPU {gpu.index}: {gpu.name} ({gpu.free_vram_gb:.1f}/{gpu.total_vram_gb:.1f} GB free)")
    else:
        print("GPU: no NVIDIA GPU detected through nvidia-smi")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.images.backend_runtime.stop()
        state.runtime.stop_all()
        server.server_close()
