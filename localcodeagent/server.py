from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .agent.orchestrator import AgentOrchestrator
from .image.manager import ImageManager
from .config import AgentConfig
from .models.router import ModelRouter
from .runtime.manager import RuntimeManager
from .tools.base import ToolRegistry
from .tools.filesystem import register_filesystem_tools
from .tools.git import register_git_tools
from .tools.image import register_image_tools
from .tools.repository import register_repository_tools
from .tools.shell import register_shell_tools
from .tools.web import register_web_tools
from .workflow.checkpoint import CheckpointManager
from .workflow.memory import ProjectMemory
from .workflow.repository import RepositoryIndex
from .workflow.tasks import TaskStore


VERSION = "0.4.0-dev"


class AppState:
    def __init__(self, config: AgentConfig, workspace: Path, runtime_root: Path) -> None:
        self.config = config
        self.workspace = workspace.resolve()
        self.runtime = RuntimeManager(config, base_dir=runtime_root)
        self.router = ModelRouter(config.models, resource_advisor=self.runtime.resource_fit)
        self.images = ImageManager(base_dir=runtime_root, models=config.image_models, runtime=self.runtime, config=config)
        self.tasks = TaskStore(self.workspace)
        self.checkpoints = CheckpointManager(self.workspace)
        self.memory = ProjectMemory(self.workspace)
        self.repository_index = RepositoryIndex(self.workspace)
        self.tools = ToolRegistry(config.permissions)
        register_filesystem_tools(self.tools, self.workspace, checkpoints=self.checkpoints, tasks=self.tasks)
        register_shell_tools(self.tools, self.workspace)
        register_git_tools(self.tools, self.workspace)
        register_repository_tools(self.tools, self.repository_index)
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
        if path == "/api/image":
            self._json(self.state.images.summary())
            return
        if path == "/api/image/history":
            query = urlparse(self.path).query
            from urllib.parse import parse_qs
            q = parse_qs(query).get("q", [""])[0]
            self._json({"history": self.state.images.history(query=q)})
            return
        if path.startswith("/api/image/output/"):
            rel = path[len("/api/image/output/"):].strip("/")
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
            "runtime": self.state.runtime.summary(probe_external=False),
        })

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
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
                request = ImageRequest(**{k: v for k, v in body.items() if k in allowed})
                job = self.state.images.create_job(request, real_person=bool(body.get("real_person", False)))
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
                        self._json({"error": "External runtimes are not controlled by Local Code Agent."}, 400)
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
    print(f"Local Code Agent v{VERSION}")
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
