from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import secrets
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from typing import Any

from .agent.orchestrator import AgentOrchestrator
from .image.manager import ImageManager
from .config import AgentConfig, load_config
from .models.router import ModelRouter
from .models.telemetry import ModelPerformanceTelemetry
from .runtime.manager import RuntimeManager
from .runtime.setup import suggest_model_profiles, write_suggested_models
from .research import ResearchCoordinator
from .permissions import PermissionManager
from .jobs import JobManager
from .processes import ManagedService, ProcessManager
from .tools.base import TOOL_CATEGORIES, ToolRegistry
from .tools.plugins import load_plugin_manifests
from .secrets import SecretVault
from .tools.api import register_api_tools
from .tools.buildsys import register_build_tools
from .tools.codeintel import register_codeintel_tools
from .tools.data import register_data_tools
from .tools.documents import register_document_tools
from .tools.knowledge import register_knowledge_tools
from .tools.sandbox import register_sandbox_tools
from .tools.media import register_media_tools
from .tools.filesystem import register_filesystem_tools
from .tools.git import register_git_tools
from .tools.github import register_github_tools
from .tools.image import register_image_tools
from .tools.repository import register_repository_tools
from .tools.research import register_research_tools
from .tools.search import find_ripgrep, register_search_tools
from .tools.shell import register_shell_tools
from .tools.terminal import register_terminal_tools
from .tool_router import ToolRouter
from .mcp import MCPManager, load_mcp_configs
from .tools.web import register_web_tools
from .workflow.checkpoint import CheckpointManager
from .workflow.memory import ProjectMemory
from .workflow.conversation_memory import ConversationMemory
from .workflow.conversation_manager import ConversationManager
from .workflow.knowledge_memory import KnowledgeMemory
from .workflow.nexus_brain import NexusBrain
from .training import ModelGrowthLab
from .workflow.repository import RepositoryIndex
from .workflow.tasks import TaskStore


VERSION = "0.6.0-dev"


class AppState:
    def __init__(self, config: AgentConfig, workspace: Path, runtime_root: Path, config_path: Path | None = None) -> None:
        self.config = config
        self.workspace = workspace.resolve()
        self.config_path = (config_path or (runtime_root / "config.json")).expanduser().resolve()
        self.runtime = RuntimeManager(config, base_dir=runtime_root)
        self.model_telemetry = ModelPerformanceTelemetry(
            self.workspace,
            enabled=config.model_telemetry_enabled,
            storage_path=config.model_telemetry_path,
            min_samples=config.model_telemetry_min_samples,
            weight=config.model_telemetry_weight,
            max_events=config.model_telemetry_max_events,
        )
        self.router = ModelRouter(
            config.models,
            resource_advisor=self.runtime.resource_fit,
            performance_advisor=self.model_telemetry.score,
        )
        self.images = ImageManager(base_dir=runtime_root, models=config.image_models, runtime=self.runtime, config=config, workspace=self.workspace)
        self.tasks = TaskStore(self.workspace)
        self.checkpoints = CheckpointManager(self.workspace)
        self.memory = ProjectMemory(self.workspace)
        conversation_path = Path(config.conversation_memory_path).expanduser()
        if not conversation_path.is_absolute():
            conversation_path = runtime_root / conversation_path
        self.conversation_memory = ConversationMemory(
            conversation_path,
            enabled=config.conversation_memory_enabled,
            history_limit=config.conversation_history_limit,
            rule_limit=config.conversation_rule_limit,
            fact_limit=config.conversation_fact_limit,
            training_limit=config.conversation_training_limit,
        )
        conversations_path = Path(config.conversations_path).expanduser()
        if not conversations_path.is_absolute():
            conversations_path = runtime_root / conversations_path
        self.conversation_manager = ConversationManager(
            conversations_path,
            summarize_after_messages=config.conversation_summary_after_messages,
        )
        knowledge_path = Path(config.knowledge_memory_path).expanduser()
        if not knowledge_path.is_absolute():
            knowledge_path = runtime_root / knowledge_path
        self.knowledge_memory = KnowledgeMemory(
            knowledge_path,
            enabled=config.knowledge_memory_enabled,
            default_ttl_hours=config.knowledge_default_ttl_hours,
            current_ttl_hours=config.knowledge_current_ttl_hours,
            max_records=config.knowledge_max_records,
        )
        growth_dir = Path(config.model_growth_dir).expanduser()
        if not growth_dir.is_absolute():
            growth_dir = runtime_root / growth_dir
        self.model_growth = ModelGrowthLab(growth_dir)
        # Protected Nexus Brain state has one canonical location. Mutable
        # config.json cannot redirect an initialized Brain to an unprotected file.
        brain_path = (runtime_root / "data" / "nexus_brain.json").resolve()
        brain_auth_path = brain_path.with_name(brain_path.stem + ".auth.json")
        protected_brain_exists = brain_path.is_file() or brain_auth_path.is_file()
        self.nexus_brain = NexusBrain(
            brain_path,
            enabled=True if protected_brain_exists else config.nexus_brain_enabled,
            max_records=max(10000, int(config.nexus_brain_record_limit)),
        )
        self.brain_seed_status = {
            "path": str(runtime_root / "brain-seed" / "nexus-brain-locked.json"),
            "found": False,
            "installed": False,
            "verified": bool(self.nexus_brain.verified_for_session),
            "error": "",
        }
        brain_seed_path = runtime_root / "brain-seed" / "nexus-brain-locked.json"
        if brain_seed_path.is_file():
            self.brain_seed_status["found"] = True
            try:
                seed_payload = json.loads(brain_seed_path.read_text(encoding="utf-8"))
                if self.nexus_brain.initialized:
                    seed_result = self.nexus_brain.install_signed_update(seed_payload)
                    self.brain_seed_status["installed"] = bool(seed_result.get("updated"))
                    self.brain_seed_status["action"] = str(seed_result.get("reason") or "")
                else:
                    seed_summary = self.nexus_brain.install_locked_export(seed_payload)
                    if not seed_summary.get("verified_for_session"):
                        raise PermissionError("Bundled Nexus Brain seed failed public signature verification")
                    self.brain_seed_status["installed"] = True
                    self.brain_seed_status["action"] = "installed_initial_brain"
                self.brain_seed_status["verified"] = bool(self.nexus_brain.verified_for_session)
            except Exception as exc:
                self.brain_seed_status["error"] = f"{type(exc).__name__}: {exc}"
                if not self.nexus_brain.initialized:
                    # A failed first-install seed must not permanently occupy the
                    # Brain path or block valid creator initialization later.
                    try:
                        self.nexus_brain.path.unlink(missing_ok=True)
                        self.nexus_brain.auth_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self.nexus_brain = NexusBrain(
                        brain_path,
                        enabled=config.nexus_brain_enabled,
                        max_records=max(10000, int(config.nexus_brain_record_limit)),
                    )
        self.images.adult_content_allowed = lambda: self.brain_allows("adult_content", True)
        self.repository_index = RepositoryIndex(self.workspace)
        self.research = ResearchCoordinator(self.workspace, self.repository_index, config)
        self.permission_manager = PermissionManager(config.permissions, profile=getattr(config, "permission_profile", "custom"))
        tools_state_path = Path(getattr(config, "tools_state_path", "data/tools_state.json")).expanduser()
        if not tools_state_path.is_absolute():
            tools_state_path = runtime_root / tools_state_path
        self.tools = ToolRegistry(self.permission_manager, state_path=tools_state_path)
        jobs_path = Path(getattr(config, "jobs_path", "data/jobs.json")).expanduser()
        if not jobs_path.is_absolute():
            jobs_path = runtime_root / jobs_path
        self.jobs = JobManager(jobs_path)
        self.processes = ProcessManager()
        self._register_processes()
        register_filesystem_tools(self.tools, self.workspace, checkpoints=self.checkpoints, tasks=self.tasks)
        register_shell_tools(self.tools, self.workspace)
        self.terminal_tracker = register_terminal_tools(
            self.tools, self.workspace, jobs=self.jobs, log_dir=runtime_root / ".agent" / "runtime"
        )
        register_search_tools(self.tools, self.workspace)
        register_build_tools(self.tools, self.workspace)
        register_codeintel_tools(self.tools, self.workspace)
        self.secrets = SecretVault(runtime_root / "data" / "secrets.vault")
        register_api_tools(self.tools, vault=self.secrets)
        register_data_tools(self.tools, self.workspace, artifacts_dir=runtime_root / "data" / "charts")
        register_media_tools(self.tools, self.workspace, jobs=self.jobs)
        register_document_tools(self.tools, self.workspace, jobs=self.jobs)
        register_knowledge_tools(self.tools, self.workspace)
        register_sandbox_tools(self.tools, self.workspace)
        register_git_tools(self.tools, self.workspace)
        if config.github_enabled:
            register_github_tools(self.tools, self.workspace, config)
        register_repository_tools(self.tools, self.repository_index)
        if config.research_enabled:
            register_research_tools(self.tools, self.research)
        register_web_tools(self.tools, runtime_root=runtime_root)
        if config.image_enabled:
            register_image_tools(self.tools, self.images)
        manifests_dir = Path(getattr(config, "tool_manifests_dir", "tools/manifests")).expanduser()
        if not manifests_dir.is_absolute():
            manifests_dir = runtime_root / manifests_dir
        self.plugin_manifests = load_plugin_manifests(manifests_dir, self.tools, workspace=self.workspace)
        self._register_health_checks()
        self.tool_router = ToolRouter(
            self.tools,
            resources=lambda: (self.runtime.summary() or {}).get("hardware") or {},
            prefer=getattr(config, "preferred_tools", []),
        )
        self.mcp = MCPManager(self.tools, load_mcp_configs(getattr(config, "mcp_servers", [])))
        try:
            self.mcp.connect_all()
        except Exception:
            pass
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
            telemetry=self.model_telemetry,
            conversation_memory=self.conversation_memory,
            conversation_manager=self.conversation_manager,
            knowledge_memory=self.knowledge_memory,
            model_growth=self.model_growth,
            nexus_brain=self.nexus_brain,
        )
        self.history: list[dict] = self.conversation_manager.history(limit=32)
        self._brain_creator_token = ""
        self._prewarm_thread: threading.Thread | None = None
        self._start_primary_prewarm()

    def _start_primary_prewarm(self) -> None:
        if not self.config.runtime_auto_start:
            return
        starter = next(
            (
                profile
                for profile in self.config.models
                if profile.enabled and "primary_coder" in profile.roles
            ),
            None,
        )
        if starter is None or starter.runtime != "llama_cpp":
            return
        fits, _score, _reason = self.runtime.resource_fit(starter)
        if not fits:
            return

        def warm() -> None:
            try:
                self.runtime.ensure_ready(starter)
            except Exception:
                # Runtime status captures the real failure; prewarm must never prevent UI startup.
                pass

        self._prewarm_thread = threading.Thread(
            target=warm,
            name="chat-nexus-primary-prewarm",
            daemon=True,
        )
        self._prewarm_thread.start()

    def reload_model_configuration(self) -> dict:
        """Reload model profiles without allowing config.json to bypass a protected Brain."""
        config = load_config(self.config_path if self.config_path.exists() else None)
        if self.nexus_brain.initialized:
            # Once creator-protected state exists, mutable config cannot disable,
            # relocate, or shrink it. Signed Brain controls stay authoritative.
            config.nexus_brain_enabled = True
            config.nexus_brain_path = "data/nexus_brain.json"
            config.nexus_brain_record_limit = max(10000, int(self.nexus_brain.max_records))
        self.runtime.reconfigure_models(config)

        model_telemetry = ModelPerformanceTelemetry(
            self.workspace,
            enabled=config.model_telemetry_enabled,
            storage_path=config.model_telemetry_path,
            min_samples=config.model_telemetry_min_samples,
            weight=config.model_telemetry_weight,
            max_events=config.model_telemetry_max_events,
        )
        router = ModelRouter(
            config.models,
            resource_advisor=self.runtime.resource_fit,
            performance_advisor=model_telemetry.score,
        )
        self.config = config
        self.model_telemetry = model_telemetry
        self.router = router
        # The permission manager and tool registry wrap the live permissions
        # dict; point them at the reloaded map and refresh process services.
        self.permission_manager.permissions = config.permissions
        self.permission_manager.profile = getattr(config, "permission_profile", "custom")
        self.tools.permissions = config.permissions
        self._register_processes()
        self.agent.config = config
        self.agent.router = router
        self.agent.telemetry = model_telemetry
        conversation_path = Path(config.conversation_memory_path).expanduser()
        if not conversation_path.is_absolute():
            conversation_path = self.runtime.base_dir / conversation_path
        self.conversation_memory = ConversationMemory(
            conversation_path,
            enabled=config.conversation_memory_enabled,
            history_limit=config.conversation_history_limit,
            rule_limit=config.conversation_rule_limit,
            fact_limit=config.conversation_fact_limit,
            training_limit=config.conversation_training_limit,
        )
        conversations_path = Path(config.conversations_path).expanduser()
        if not conversations_path.is_absolute():
            conversations_path = self.runtime.base_dir / conversations_path
        self.conversation_manager = ConversationManager(
            conversations_path,
            summarize_after_messages=config.conversation_summary_after_messages,
        )
        knowledge_path = Path(config.knowledge_memory_path).expanduser()
        if not knowledge_path.is_absolute():
            knowledge_path = self.runtime.base_dir / knowledge_path
        self.knowledge_memory = KnowledgeMemory(
            knowledge_path,
            enabled=config.knowledge_memory_enabled,
            default_ttl_hours=config.knowledge_default_ttl_hours,
            current_ttl_hours=config.knowledge_current_ttl_hours,
            max_records=config.knowledge_max_records,
        )
        growth_dir = Path(config.model_growth_dir).expanduser()
        if not growth_dir.is_absolute():
            growth_dir = self.runtime.base_dir / growth_dir
        self.model_growth = ModelGrowthLab(growth_dir)
        brain_path = (self.runtime.base_dir / "data" / "nexus_brain.json").resolve()
        if not self.nexus_brain.initialized and getattr(self.nexus_brain, "path", None) != brain_path:
            self.nexus_brain = NexusBrain(
                brain_path,
                enabled=config.nexus_brain_enabled,
                max_records=max(10000, int(config.nexus_brain_record_limit)),
            )
        elif self.nexus_brain.initialized:
            self.nexus_brain.enabled = True
            self.nexus_brain.max_records = max(10000, int(self.nexus_brain.max_records))
        else:
            self.nexus_brain.enabled = bool(config.nexus_brain_enabled)
            self.nexus_brain.max_records = max(10000, int(config.nexus_brain_record_limit))
        self.agent.conversation_memory = self.conversation_memory
        self.agent.conversation_manager = self.conversation_manager
        self.agent.knowledge_memory = self.knowledge_memory
        self.agent.model_growth = self.model_growth
        self.agent.nexus_brain = self.nexus_brain
        self.history = self.conversation_manager.history(limit=32)
        self.images.config = config
        self.images.adult_content_allowed = lambda: self.brain_allows("adult_content", True)

        starter = next(
            (
                profile
                for profile in config.models
                if profile.enabled and "primary_coder" in profile.roles
            ),
            None,
        )
        started = False
        start_error = ""
        if starter is not None:
            fits, _score, _reason = self.runtime.resource_fit(starter)
            if fits:
                try:
                    self.runtime.ensure_ready(starter)
                    started = True
                except Exception as exc:
                    start_error = f"{type(exc).__name__}: {exc}"

        return {
            "models": [asdict(model) for model in config.models],
            "starter_model": starter.id if starter else "",
            "started": started,
            "start_error": start_error,
            "readiness": self.readiness_payload(probe_external=True),
        }

    def open_brain_creator_session(self) -> str:
        self._brain_creator_token = secrets.token_urlsafe(32)
        return self._brain_creator_token

    def require_brain_creator_session(self, token: str) -> None:
        supplied = str(token or "")
        if not self._brain_creator_token or not supplied or not secrets.compare_digest(supplied, self._brain_creator_token):
            raise PermissionError("Creator authentication session is required")

    def close_brain_creator_session(self) -> None:
        self._brain_creator_token = ""

    def brain_allows(self, name: str, default: bool = True) -> bool:
        if not self.nexus_brain.initialized:
            return default
        if not self.nexus_brain.verified_for_session:
            return False
        return self.nexus_brain.subroutine(name, default)

    def sync_nexus_brain(self) -> dict[str, Any]:
        if not self.nexus_brain.unlocked:
            raise PermissionError("Nexus Brain is creator-locked")
        return {
            "conversation_records": self.nexus_brain.sync_conversation_memory(self.conversation_memory.snapshot()),
            "autobiographical_records": self.nexus_brain.sync_conversations(self.conversation_manager.snapshot()),
            "knowledge_records": self.nexus_brain.sync_knowledge_records(self.knowledge_memory.records()),
            "training_records": self.nexus_brain.sync_model_growth(self.model_growth.candidates(limit=5000)),
            "brain": self.nexus_brain.summary(),
        }

    @staticmethod
    def _remove_lora_args(args: list[str]) -> list[str]:
        cleaned: list[str] = []
        skip = False
        for item in args:
            if skip:
                skip = False
                continue
            text = str(item)
            if text == "--lora":
                skip = True
                continue
            if text.startswith("--lora="):
                continue
            cleaned.append(text)
        return cleaned

    def activate_growth_candidate(self, candidate_id: str) -> dict:
        candidate = self.model_growth.candidate_model(candidate_id)
        if candidate.get("status") != "evaluated" or not candidate.get("evaluation_passed"):
            raise ValueError("Candidate must pass evaluation before activation")
        artifact = Path(str(candidate.get("artifact_path") or "")).expanduser()
        if not artifact.is_absolute():
            artifact = (self.runtime.base_dir / artifact).resolve()
        else:
            artifact = artifact.resolve()
        if not artifact.is_file():
            raise FileNotFoundError(f"Candidate artifact does not exist: {artifact}")
        if artifact.suffix.lower() != ".gguf":
            raise ValueError("Active llama.cpp growth artifacts must be GGUF files")

        if self.config_path.exists():
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            raw = asdict(self.config)
        models = list(raw.get("models") or [])
        base_id = str(candidate.get("base_model_id") or "")
        target = next((m for m in models if str(m.get("id")) == base_id), None)
        if target is None:
            raise KeyError(f"Base model profile not found: {base_id}")

        backup = {
            "candidate_id": candidate_id,
            "base_model_id": base_id,
            "model_path": str(target.get("model_path") or ""),
            "extra_args": list(target.get("extra_args") or []),
            "created_at": time.time(),
        }
        backup_path = self.model_growth.root / "activation_backup.json"
        backup_path.write_text(json.dumps(backup, indent=2), encoding="utf-8")

        method = str(candidate.get("method") or "lora")
        extra_args = self._remove_lora_args([str(x) for x in target.get("extra_args") or []])
        if method in {"lora", "qlora"}:
            target["extra_args"] = [*extra_args, "--lora", str(artifact)]
        elif method == "full_finetune":
            target["model_path"] = str(artifact)
            target["extra_args"] = extra_args
        else:
            raise ValueError(f"Unsupported growth method for activation: {method}")

        raw["models"] = models
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".growth.tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.config_path)
        try:
            applied = self.reload_model_configuration()
        except Exception:
            self.restore_growth_activation()
            raise
        return {
            "candidate": candidate,
            "artifact_path": str(artifact),
            "method": method,
            "applied": applied,
        }

    def restore_growth_activation(self) -> dict:
        backup_path = self.model_growth.root / "activation_backup.json"
        if not backup_path.is_file():
            return {"restored": False, "reason": "no activation backup"}
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        if self.config_path.exists():
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            raw = asdict(self.config)
        models = list(raw.get("models") or [])
        base_id = str(backup.get("base_model_id") or "")
        target = next((m for m in models if str(m.get("id")) == base_id), None)
        if target is None:
            raise KeyError(f"Base model profile not found during rollback: {base_id}")
        target["model_path"] = str(backup.get("model_path") or "")
        target["extra_args"] = list(backup.get("extra_args") or [])
        raw["models"] = models
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".rollback.tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.config_path)
        applied = self.reload_model_configuration()
        try:
            backup_path.unlink()
        except OSError:
            pass
        return {"restored": True, "base_model_id": base_id, "applied": applied}

    def set_conversation_policy_mode(
        self,
        mode: str,
        *,
        ethical_temperature: float | None = None,
    ) -> dict:
        mode = str(mode or "").strip().lower()
        if mode not in {"permissive", "balanced", "strict"}:
            raise ValueError("policy mode must be permissive, balanced, or strict")

        if ethical_temperature is None:
            temperature = float(getattr(self.config, "ethical_temperature", 1.0))
        else:
            temperature = max(0.0, min(1.0, float(ethical_temperature)))

        if self.config_path.exists():
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            raw = asdict(self.config)
        raw["conversation_policy_mode"] = mode
        raw["ethical_temperature"] = temperature
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".policy.tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.config_path)

        self.config.conversation_policy_mode = mode
        self.config.ethical_temperature = temperature
        self.agent.config.conversation_policy_mode = mode
        self.agent.config.ethical_temperature = temperature
        return {
            "mode": mode,
            "ethical_temperature": temperature,
            "hard_tool_safety": True,
            "message": (
                "Permissive reduces generic topic-based refusals while narrow hard tool safety remains enforced."
                if mode == "permissive"
                else "Conversation policy updated."
            ),
        }

    def _register_processes(self) -> None:
        """Register controllable services with the central ProcessManager."""
        for service_id in self.processes.service_ids(prefix="llama:"):
            self.processes.unregister(service_id)
        for model in self.config.models:
            if model.runtime != "llama_cpp":
                continue
            profile = model
            model_id = model.id

            def describe(mid: str = model_id) -> dict:
                statuses = {row.get("model_id"): row for row in self.runtime.statuses(probe_external=False)}
                return dict(statuses.get(mid) or {"state": "stopped"})

            self.processes.register(ManagedService(
                id=f"llama:{model_id}",
                name=f"llama.cpp · {model_id}",
                kind="llm_runtime",
                port=self.runtime._port_from_endpoint(profile.endpoint) or 0,
                describe=describe,
                start=lambda p=profile: self.runtime.ensure_ready(p),
                stop=lambda mid=model_id: self.runtime.stop_model(mid).as_dict(),
                metadata={"roles": list(profile.roles), "model_path": profile.model_path},
            ))
        if self.config.image_enabled:
            endpoint = str(getattr(self.config, "comfyui_endpoint", ""))
            self.processes.register(ManagedService(
                id="comfyui",
                name="ComfyUI",
                kind="image_backend",
                port=self.runtime._port_from_endpoint(endpoint) or 8188,
                describe=lambda: self.images.backend_runtime.probe(),
                start=lambda: self.images.backend_runtime.ensure_ready(),
                stop=lambda: self.images.backend_runtime.stop(),
                restart=lambda: self.images.backend_runtime.recover(),
            ))

    def _register_health_checks(self) -> None:
        """Attach lightweight health probes to built-in tool families."""
        import shutil
        import subprocess

        def exe_probe(executable: str, label: str):
            def check() -> dict:
                if shutil.which(executable):
                    try:
                        proc = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
                        version = (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr) else "ok"
                    except Exception:
                        version = "ok"
                    return {"ok": True, "status": "healthy", "detail": f"{label}: {version[:120]}"}
                return {"ok": False, "status": "unhealthy", "detail": f"{executable} not found on PATH"}
            return check

        def always_ok(detail: str):
            return lambda: {"ok": True, "status": "healthy", "detail": detail}

        def rg_probe() -> dict:
            exe = find_ripgrep()
            if exe:
                return {"ok": True, "status": "healthy", "detail": f"ripgrep at {exe}"}
            return {"ok": True, "status": "degraded", "detail": "ripgrep missing; built-in scanner fallback active"}

        probes = {
            "git": exe_probe("git", "git"),
            "terminal": always_ok("native shells via subprocess"),
            "ripgrep": rg_probe,
            "github": (
                (lambda: {"ok": True, "status": "healthy", "detail": "github credentials detected"})
                if (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or shutil.which("gh"))
                else (lambda: {"ok": False, "status": "unhealthy", "detail": "no GitHub token or gh CLI"})
            ),
        }
        if self.config.image_enabled:
            def comfy_probe() -> dict:
                try:
                    st = self.images.backend_runtime.probe()
                except Exception as exc:
                    return {"ok": False, "status": "error", "detail": f"{type(exc).__name__}: {exc}"}
                return {"ok": bool(st.get("healthy")), "status": str(st.get("state") or "unknown"),
                        "detail": str(st.get("error") or st.get("state") or "")}
            probes["comfyui"] = comfy_probe
        for manifest in self.tools.manifests():
            probe = probes.get(str(manifest.get("provider") or ""))
            spec = self.tools.get(manifest["name"])
            if probe and spec and spec.health_check is None:
                spec.health_check = probe
        for name in ("run_shell", "terminal_run", "terminal_processes", "terminal_kill"):
            spec = self.tools.get(name)
            if spec and spec.health_check is None:
                spec.health_check = always_ok("native subprocess execution")

    def _update_config_file(self, updates: dict) -> None:
        """Merge keys into config.json atomically, preserving unrelated settings."""
        if self.config_path.exists():
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            raw = asdict(self.config)
        raw.update(updates)
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".cnx.tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.config_path)

    def install_tool(self, tool_id: str, *, approve: bool = False) -> dict:
        """Run a manifest tool's install command as a tracked job.

        Gated by the packages.install permission; returns needs_approval when
        the effective mode is ask and the caller hasn't confirmed.
        """
        from .tools.plugins import install_command

        spec = self.tools.get(tool_id)
        if spec is None:
            match = next((m for m in self.tools.manifests() if m["id"] == tool_id), None)
            if match is None:
                raise KeyError(f"unknown tool '{tool_id}'")
            spec = self.tools.get(match["name"])
        manifest = self.tools.manifest(spec.name)
        install = manifest.get("install") or {}
        mode = self.permission_manager.effective("packages.install")
        if mode == "deny":
            return {"ok": False, "error": "packages.install permission is denied"}
        if mode == "ask" and not approve:
            return {"ok": False, "needs_approval": True, "permission": "packages.install",
                    "tool": spec.name, "install": install}
        if approve and self.permission_manager.level("packages.install") == "session":
            self.permission_manager.grant_session("packages.install")
        cmd = install_command(install)
        if cmd is None:
            return {"ok": False, "error": "no automated install method — manual install required",
                    "install": install}
        job = self.jobs.submit("install", f"Install {spec.display_name}")

        def _run() -> None:
            import subprocess
            self.jobs.update(job.id, state="running", detail=" ".join(cmd[:3]))
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                if proc.returncode == 0:
                    self.jobs.update(job.id, state="completed", detail=(proc.stdout or "")[-300:])
                else:
                    self.jobs.update(job.id, state="failed", error=(proc.stderr or proc.stdout or "install failed")[-300:])
            except FileNotFoundError:
                self.jobs.update(job.id, state="failed", error=f"installer not found: {cmd[0]}")
            except subprocess.TimeoutExpired:
                self.jobs.update(job.id, state="failed", error="install timed out")
            changed = self.tools.refresh_install_status()
            if changed:
                self.jobs.update(job.id, detail=f"install status updated: {changed}")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "job_id": job.id, "tool": spec.name, "method": str(install.get("method") or "")}

    def jobs_payload(self) -> dict:
        image = self.images.summary() if self.config.image_enabled else {}
        catalog_jobs: list = []
        try:
            catalog_jobs = self.runtime.model_catalog.jobs()
        except Exception:
            catalog_jobs = []
        return {
            "jobs": self.jobs.aggregate(
                tasks_payload=self.task_payload(),
                image_jobs=list(image.get("jobs") or []),
                model_installs=catalog_jobs,
                image_installs=list(image.get("installs") or []),
            ),
            "states": [
                "queued", "preparing", "running", "waiting_for_tool",
                "waiting_for_permission", "completed", "failed", "cancelled",
            ],
        }

    def task_payload(self) -> dict:
        current = self.tasks.current()
        return {
            "current": current.as_dict() if current else None,
            "recent": self.tasks.recent(12),
        }

    def readiness_payload(self, *, probe_external: bool = True) -> dict:
        payload = self.runtime.readiness(probe_external=probe_external)
        self_tree = (
            (self.workspace / "localcodeagent" / "server.py").is_file()
            and (self.workspace / "web" / "index.html").is_file()
            and (self.workspace / "pyproject.toml").is_file()
        )
        git_repo = (self.workspace / ".git").exists()
        payload.update({
            "workspace": str(self.workspace),
            "config_path": str(self.config_path),
            "self_hosting_tree": self_tree,
            "git_repository": git_repo,
            "isolated_selftest_available": (self.workspace / "localcodeagent" / "selftest.py").is_file(),
            "suggested_models": suggest_model_profiles(payload.get("inventory") or []),
        })
        payload["self_hosting_ready"] = bool(
            payload.get("ready_to_code")
            and self_tree
            and git_repo
            and payload["isolated_selftest_available"]
        )
        return payload


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
        if path == "/api/conversation-memory":
            self._json(self.state.conversation_memory.snapshot())
            return
        if path == "/api/nexus-brain":
            self._json(self.state.nexus_brain.summary())
            return
        if path == "/api/nexus-brain/audit":
            self._json({"events": self.state.nexus_brain.audit_events(100)})
            return
        if path == "/api/conversations":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            query = str((q.get("q") or [""])[0]).strip()
            if query:
                self._json({"results": self.state.conversation_manager.search(query)})
            else:
                self._json(self.state.conversation_manager.snapshot())
            return
        if path == "/api/knowledge-memory":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            query = str((q.get("q") or [""])[0]).strip()
            payload = self.state.knowledge_memory.snapshot()
            if query:
                payload["results"] = self.state.knowledge_memory.search(query)
            self._json(payload)
            return
        if path == "/api/model-growth":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            status = str((q.get("status") or [""])[0]).strip()
            payload = self.state.model_growth.summary()
            payload["candidates"] = self.state.model_growth.candidates(status=status, limit=500)
            self._json(payload)
            return
        if path.startswith("/api/model-growth/job/"):
            job_id = unquote(path[len("/api/model-growth/job/"):]).strip("/")
            try:
                jobs = {str(row.get("id")): row for row in self.state.model_growth.jobs()}
                if job_id not in jobs:
                    self._json({"error": "training job not found"}, 404)
                    return
                self._json({
                    "job": jobs[job_id],
                    "log": self.state.model_growth.training_log(job_id),
                })
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return

        if path == "/api/time":
            self._json(self.state.agent.current_time_snapshot())
            return

        if path == "/api/policy":
            self._json({
                "mode": self.state.config.conversation_policy_mode,
                "ethical_temperature": float(getattr(self.state.config, "ethical_temperature", 1.0)),
                "available_modes": ["permissive", "balanced", "strict"],
                "hard_tool_safety": True,
            })
            return

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
                "policy_mode": self.state.config.conversation_policy_mode,
                "ethical_temperature": float(getattr(self.state.config, "ethical_temperature", 1.0)),
                "clock": self.state.agent.current_time_snapshot(),
                "nexus_brain": self.state.nexus_brain.summary(),
                "nexus_brain_seed": dict(self.state.brain_seed_status),
                "runtime": runtime,
                "tasks": self.state.task_payload(),
                "repository_index": self.state.repository_index.summary(),
                "research": self.state.research.summary(),
                "model_telemetry": self.state.model_telemetry.summary(),
                "image": self.state.images.summary(),
            })
            return
        if path == "/api/models":
            self._json({"models": [asdict(m) for m in self.state.config.models]})
            return
        if path == "/api/model-telemetry":
            self._json(self.state.model_telemetry.summary())
            return
        if path == "/api/tools":
            self.state.tools.refresh_install_status()
            self._json({
                "tools": self.state.tools.manifests(),
                "categories": TOOL_CATEGORIES,
                "plugins": self.state.plugin_manifests,
            })
            return
        if path.startswith("/api/tools/health/"):
            tool_id = unquote(path[len("/api/tools/health/"):]).strip("/")
            if not tool_id:
                self._json({"error": "tool id is required"}, 400)
                return
            try:
                self._json({"tool": tool_id, "health": self.state.tools.health(tool_id)})
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return
        if path.startswith("/api/tools/route/"):
            capability = unquote(path[len("/api/tools/route/"):]).strip("/")
            if not capability:
                self._json({"error": "capability is required"}, 400)
                return
            check_health = "health=1" in (self.path.split("?", 1)[-1] if "?" in self.path else "")
            self._json(self.state.tool_router.explain(capability, check_health=check_health))
            return
        if path == "/api/tools/telemetry":
            self._json({"routing": self.state.tool_router.recent(50)})
            return
        if path == "/api/mcp":
            self._json(self.state.mcp.status())
            return
        if path == "/api/secrets":
            self._json({"secrets": self.state.secrets.list()})
            return
        if path == "/api/permissions":
            self._json(self.state.permission_manager.summary())
            return
        if path == "/api/jobs":
            self._json(self.state.jobs_payload())
            return
        if path == "/api/processes":
            self._json({"processes": self.state.processes.list()})
            return
        if path == "/api/resources":
            runtime = self.state.runtime.summary(probe_external=True)
            self._json({
                "hardware": runtime.get("hardware"),
                "runtimes": runtime.get("runtimes"),
                "max_resident_models": runtime.get("max_resident_models"),
                "image_resource_mode": getattr(self.state.config, "image_resource_mode", "balanced"),
                "model_storage": runtime.get("model_storage"),
            })
            return
        if path == "/api/models/catalog":
            self._json({
                "models": self.state.runtime.model_catalog.catalog(),
                "jobs": self.state.runtime.model_catalog.jobs(),
            })
            return
        if path.startswith("/api/models/install/"):
            job_id = unquote(path[len("/api/models/install/"):]).strip("/")
            if not job_id:
                self._json({"error": "install job id is required"}, 400)
                return
            try:
                job = self.state.runtime.model_catalog.get_job(job_id)
            except KeyError:
                self._json({"error": "model install job not found"}, 404)
                return
            self._json({"job": job})
            return
        if path == "/api/runtime":
            self.state.runtime.refresh_hardware()
            self._json(self.state.runtime.summary(probe_external=True))
            return
        if path == "/api/readiness":
            self._json(self.state.readiness_payload(probe_external=True))
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
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    def _agent_payload(self, result) -> dict:
        return {
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
        }

    def _agent_response(self, result) -> None:
        self._json(self._agent_payload(result))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/nexus-brain/initialize":
                state = self.state.nexus_brain.initialize_creator(
                    str(body.get("creator_name", "")).strip(),
                    str(body.get("passcode", "")),
                )
                synced = self.state.sync_nexus_brain()
                token = self.state.open_brain_creator_session()
                self._json({"ok": True, "brain": state, "synced": synced, "creator_token": token})
                return

            if path == "/api/nexus-brain/unlock":
                state = self.state.nexus_brain.unlock(
                    str(body.get("creator_name", "")).strip(),
                    str(body.get("passcode", "")),
                )
                synced = self.state.sync_nexus_brain()
                token = self.state.open_brain_creator_session()
                self._json({"ok": True, "brain": state, "synced": synced, "creator_token": token})
                return

            if path == "/api/nexus-brain/lock":
                self.state.require_brain_creator_session(str(body.get("creator_token", "")))
                brain = self.state.nexus_brain.lock()
                self.state.close_brain_creator_session()
                self._json({"ok": True, "brain": brain})
                return

            if path == "/api/nexus-brain/sync":
                self.state.require_brain_creator_session(str(body.get("creator_token", "")))
                self._json({"ok": True, **self.state.sync_nexus_brain()})
                return

            if path == "/api/nexus-brain/subroutines":
                self.state.require_brain_creator_session(str(body.get("creator_token", "")))
                values = body.get("subroutines")
                if not isinstance(values, dict):
                    self._json({"error": "subroutines object is required"}, 400)
                    return
                saved = self.state.nexus_brain.set_subroutines(values)
                self._json({"ok": True, "subroutines": saved, "brain": self.state.nexus_brain.summary()})
                return

            if path == "/api/nexus-brain/emotions":
                self.state.require_brain_creator_session(str(body.get("creator_token", "")))
                values = body.get("emotion_profile")
                if not isinstance(values, dict):
                    self._json({"error": "emotion_profile object is required"}, 400)
                    return
                saved = self.state.nexus_brain.set_emotion_profile(values)
                self._json({"ok": True, "emotion_profile": saved, "brain": self.state.nexus_brain.summary()})
                return

            if path == "/api/nexus-brain/self-model":
                self.state.require_brain_creator_session(str(body.get("creator_token", "")))
                values = body.get("self_model")
                if not isinstance(values, dict):
                    self._json({"error": "self_model object is required"}, 400)
                    return
                saved = self.state.nexus_brain.set_self_model(values)
                self._json({"ok": True, "self_model": saved, "brain": self.state.nexus_brain.summary()})
                return

            if path == "/api/nexus-brain/export":
                self.state.require_brain_creator_session(str(body.get("creator_token", "")))
                self._json({"ok": True, "brain": self.state.nexus_brain.export_payload()})
                return

            if path == "/api/nexus-brain/import":
                payload = body.get("brain")
                if not isinstance(payload, dict):
                    self._json({"error": "brain object is required"}, 400)
                    return
                installed = self.state.nexus_brain.install_locked_export(payload)
                self._json({"ok": True, "brain": installed})
                return

            if path.startswith("/api/model-growth/") and not self.state.brain_allows("model_growth", True):
                self._json({"error": "Model Growth is disabled by the creator-locked Nexus Brain.", "code": "brain_subroutine_disabled"}, 403)
                return
            if path in {"/api/research/plan", "/api/research/run"} and not self.state.brain_allows("web_research", True):
                self._json({"error": "Web research is disabled by the creator-locked Nexus Brain.", "code": "brain_subroutine_disabled"}, 403)
                return
            if path == "/api/image/generate" and not self.state.brain_allows("image_generation", True):
                self._json({"error": "Image generation is disabled by the creator-locked Nexus Brain.", "code": "brain_subroutine_disabled"}, 403)
                return

            if path == "/api/policy/mode":
                saved = self.state.set_conversation_policy_mode(
                    str(body.get("mode", "")),
                    ethical_temperature=body.get("ethical_temperature"),
                )
                self._json({"ok": True, **saved})
                return

            if path == "/api/policy/temperature":
                saved = self.state.set_conversation_policy_mode(
                    self.state.config.conversation_policy_mode,
                    ethical_temperature=float(body.get("ethical_temperature", 1.0)),
                )
                self._json({"ok": True, **saved})
                return

            if path == "/api/models/install":
                catalog_id = str(body.get("catalog_id", "")).strip()
                if not catalog_id:
                    self._json({"error": "catalog_id is required"}, 400)
                    return
                try:
                    job = self.state.runtime.model_catalog.start_install(catalog_id, repair=bool(body.get("repair", False)))
                except FileExistsError as exc:
                    self._json({"error": str(exc), "repair_available": True}, 409)
                    return
                except OSError as exc:
                    self._json({"error": str(exc), "code": "insufficient_model_storage"}, 507)
                    return
                self._json({"ok": True, "job": job})
                return

            if path == "/api/models/install/cancel":
                job_id = str(body.get("job_id", "")).strip()
                if not job_id:
                    self._json({"error": "job_id is required"}, 400)
                    return
                job = self.state.runtime.model_catalog.cancel(job_id)
                self._json({"ok": True, "job": job})
                return

            if path == "/api/models/verify":
                catalog_id = str(body.get("catalog_id", "")).strip()
                if not catalog_id:
                    self._json({"error": "catalog_id is required"}, 400)
                    return
                result = self.state.runtime.model_catalog.verify(catalog_id, deep_hash=bool(body.get("deep_hash", True)))
                self._json({"ok": True, "model": result})
                return
            if path == "/api/readiness/configure":
                if body.get("apply") is not True:
                    self._json({"error": "apply=true is required to change the Chat Nexus config"}, 400)
                    return
                readiness = self.state.readiness_payload(probe_external=False)
                suggestions = list(readiness.get("suggested_models") or [])
                if not suggestions:
                    self._json({"error": "No local GGUF models were discovered to configure."}, 400)
                    return
                saved = write_suggested_models(self.state.config_path, suggestions)
                applied = self.state.reload_model_configuration()
                saved["restart_required"] = False
                message = "Coding models configured and activated."
                if applied.get("start_error"):
                    message = (
                        "Coding models were configured live, but the starter model did not finish starting: "
                        + str(applied["start_error"])
                    )
                self._json({
                    "ok": True,
                    "saved": saved,
                    "suggested_models": suggestions,
                    "applied": applied,
                    "message": message,
                })
                return
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
                if session.get("summary") and session.get("sources"):
                    record = self.state.knowledge_memory.remember_research(
                        query,
                        str(session.get("summary") or ""),
                        list(session.get("sources") or []),
                        current_sensitive=self.state.knowledge_memory.is_current_sensitive(query),
                        metadata={"research_session_id": session.get("id", ""), "manual": True},
                    )
                    if record is not None:
                        self.state.model_growth.collect(
                            kind="sourced_knowledge",
                            instruction=query,
                            response=str(session.get("summary") or ""),
                            source="manual_research",
                            metadata={"knowledge_id": record.get("id"), "sources": record.get("sources", [])},
                        )
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

            if path == "/api/conversation-memory/update":
                kind = str(body.get("kind", "")).strip()
                item_id = str(body.get("item_id", "")).strip()
                if not kind or not item_id:
                    self._json({"error": "kind and item_id are required"}, 400)
                    return
                kwargs = {}
                if "text" in body:
                    kwargs["text"] = str(body.get("text", ""))
                if "active" in body:
                    kwargs["active"] = bool(body.get("active"))
                if "scope" in body:
                    scope = str(body.get("scope", "global")).strip().lower()
                    kwargs["scope"] = scope
                    if scope == "project":
                        kwargs["scope_id"] = str(self.state.workspace)
                    elif scope == "conversation":
                        kwargs["scope_id"] = str(self.state.conversation_manager.active().get("id") or "")
                    else:
                        kwargs["scope_id"] = ""
                saved = self.state.conversation_memory.update_item(kind, item_id, **kwargs)
                self._json({"ok": True, "item": saved, "memory": self.state.conversation_memory.snapshot()})
                return

            if path == "/api/conversation-memory/forget":
                query = str(body.get("query", "")).strip()
                if not query:
                    self._json({"error": "query is required"}, 400)
                    return
                forgotten = self.state.conversation_memory.forget(query)
                self._json({"ok": True, "forgotten": forgotten, "memory": self.state.conversation_memory.snapshot()})
                return

            if path == "/api/conversation-memory/exchange":
                user_text = str(body.get("user", "")).strip()
                assistant_text = str(body.get("assistant", "")).strip()
                if not user_text or not assistant_text:
                    self._json({"error": "user and assistant are required"}, 400)
                    return
                if self.state.brain_allows("long_term_memory", True):
                    self.state.conversation_memory.learn_from_user(
                        user_text,
                        project_id=str(self.state.workspace),
                        conversation_id=str(self.state.conversation_manager.active().get("id") or ""),
                    )
                    self.state.conversation_memory.record_exchange(user_text, assistant_text)
                self.state.conversation_manager.record_exchange(
                    user_text,
                    assistant_text,
                    intent="utility",
                    model_id="builtin-local",
                )
                self.state.model_growth.import_conversation_memory(self.state.conversation_memory.snapshot())
                self.state.history = self.state.conversation_manager.history(limit=32)
                self._json({
                    "ok": True,
                    "memory": self.state.conversation_memory.snapshot(),
                    "conversation": self.state.conversation_manager.active(),
                })
                return

            if path == "/api/conversations/new":
                title = str(body.get("title", "New chat"))
                row = self.state.conversation_manager.create(title)
                self.state.history = []
                self._json({"ok": True, "conversation": row})
                return

            if path == "/api/conversations/select":
                conversation_id = str(body.get("conversation_id", "")).strip()
                if not conversation_id:
                    self._json({"error": "conversation_id is required"}, 400)
                    return
                row = self.state.conversation_manager.set_active(conversation_id)
                self.state.history = self.state.conversation_manager.history(limit=32)
                self._json({
                    "ok": True,
                    "conversation": row,
                    # UI history keeps durable message IDs/timestamps for precise feedback,
                    # while self.state.history remains role/content-only for model APIs.
                    "history": list(row.get("messages", []))[-32:],
                })
                return

            if path == "/api/conversations/personality":
                values = body.get("personality")
                if not isinstance(values, dict):
                    self._json({"error": "personality object is required"}, 400)
                    return
                saved = self.state.conversation_manager.update_personality(values)
                self._json({"ok": True, "personality": saved})
                return

            if path == "/api/conversations/feedback":
                saved = self.state.conversation_manager.add_feedback(
                    message_id=str(body.get("message_id", "")),
                    rating=str(body.get("rating", "")),
                    note=str(body.get("note", "")),
                    conversation_id=str(body.get("conversation_id", "")) or None,
                )
                self.state.model_growth.import_conversation_feedback(
                    self.state.conversation_manager.snapshot()
                )
                self._json({"ok": True, "feedback": saved})
                return

            if path == "/api/model-growth/sync":
                c = self.state.model_growth.import_conversation_memory(self.state.conversation_memory.snapshot())
                f = self.state.model_growth.import_conversation_feedback(self.state.conversation_manager.snapshot())
                k = self.state.model_growth.import_knowledge_memory(self.state.knowledge_memory.snapshot())
                self._json({
                    "ok": True,
                    "conversation_candidates": c,
                    "feedback_candidates": f,
                    "knowledge_candidates": k,
                    "growth": self.state.model_growth.summary(),
                    "nexus_brain": {"brain": self.state.nexus_brain.summary(), "protected_sync_required": True},
                })
                return

            if path == "/api/model-growth/review":
                candidate_id = str(body.get("candidate_id", "")).strip()
                status = str(body.get("status", "")).strip()
                if not candidate_id:
                    self._json({"error": "candidate_id is required"}, 400)
                    return
                item = self.state.model_growth.review(candidate_id, status=status, note=str(body.get("note", "")))
                self._json({"ok": True, "candidate": item})
                return

            if path == "/api/model-growth/export":
                manifest = self.state.model_growth.export_dataset(
                    name=str(body.get("name", "")),
                    include_knowledge=bool(body.get("include_knowledge", False)),
                )
                self._json({"ok": True, "dataset": manifest})
                return

            if path == "/api/model-growth/job":
                job = self.state.model_growth.create_training_job(
                    base_model_id=str(body.get("base_model_id", "qwen3-14b")).strip(),
                    dataset_path=str(body.get("dataset_path", "")).strip(),
                    method=str(body.get("method", "lora")).strip(),
                    output_name=str(body.get("output_name", "")).strip(),
                    trainer_backend=str(body.get("trainer_backend", self.state.config.trainer_backend)).strip(),
                    trainer_command=str(body.get("trainer_command", self.state.config.trainer_command)).strip(),
                    hyperparameters=dict(body.get("hyperparameters") or {}),
                )
                self._json({"ok": True, "job": job})
                return

            if path == "/api/model-growth/job/start":
                job_id = str(body.get("job_id", "")).strip()
                if not job_id:
                    self._json({"error": "job_id is required"}, 400)
                    return
                job = self.state.model_growth.start_training_job(job_id)
                self._json({"ok": True, "job": job})
                return

            if path == "/api/model-growth/evaluate":
                candidate_id = str(body.get("candidate_id", "")).strip()
                if not candidate_id:
                    self._json({"error": "candidate_id is required"}, 400)
                    return
                candidate = self.state.model_growth.evaluate(
                    candidate_id,
                    passed=bool(body.get("passed", False)),
                    metrics=dict(body.get("metrics") or {}),
                )
                self._json({"ok": True, "candidate_model": candidate})
                return

            if path == "/api/model-growth/register":
                candidate = self.state.model_growth.register_candidate(
                    job_id=str(body.get("job_id", "")).strip(),
                    base_model_id=str(body.get("base_model_id", "")).strip(),
                    artifact_path=str(body.get("artifact_path", "")).strip(),
                    metrics=dict(body.get("metrics") or {}),
                )
                self._json({"ok": True, "candidate_model": candidate})
                return

            if path == "/api/model-growth/promote":
                candidate_id = str(body.get("candidate_id", "")).strip()
                if not candidate_id:
                    self._json({"error": "candidate_id is required"}, 400)
                    return
                activation = self.state.activate_growth_candidate(candidate_id)
                candidate = self.state.model_growth.promote(candidate_id)
                self._json({"ok": True, "candidate_model": candidate, "activation": activation})
                return

            if path == "/api/model-growth/rollback":
                restored = self.state.restore_growth_activation()
                self._json({"ok": True, "restored": restored, "registry": self.state.model_growth.rollback()})
                return

            if path == "/api/chat/stream":
                message = str(body.get("message", "")).strip()
                mode = str(body.get("mode", "auto"))
                if not message:
                    self._json({"error": "message is required"}, 400)
                    return
                coding_model_optional = (
                    mode == "auto"
                    and self.state.agent.can_run_without_coding_model(message)
                )
                if not coding_model_optional:
                    readiness = self.state.runtime.readiness(probe_external=True)
                    if not readiness.get("ready_to_code"):
                        self._json({
                            "error": "No usable coding model is ready. Open Local system → Coding readiness, install/configure a model, then retry.",
                            "code": "coding_model_setup_required",
                            "readiness": readiness,
                        }, 409)
                        return

                self._sse_begin()
                if not self._sse_event("ready", {"mode": mode}):
                    self.close_connection = True
                    return

                events: queue.Queue[dict] = queue.Queue()
                done = threading.Event()
                started = time.monotonic()

                def emit(event: dict) -> None:
                    # Agent/model work may run for a while before the first token.
                    # Queue events back to the request thread so socket writes remain
                    # serialized and the request thread can send idle heartbeats.
                    events.put(dict(event))

                def run_agent() -> None:
                    try:
                        result = self.state.agent.run(
                            message,
                            history=self.state.history,
                            mode=mode,
                            event_callback=emit,
                        )
                        self.state.history = self.state.conversation_manager.history(limit=32)
                        events.put({"type": "result", **self._agent_payload(result)})
                    except Exception as exc:
                        events.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                    finally:
                        done.set()

                threading.Thread(
                    target=run_agent,
                    name="chat-nexus-agent-stream",
                    daemon=True,
                ).start()

                stream_open = True
                while stream_open and (not done.is_set() or not events.empty()):
                    try:
                        event = events.get(timeout=1.0)
                    except queue.Empty:
                        current = self.state.tasks.current()
                        heartbeat = {
                            "elapsed_seconds": int(time.monotonic() - started),
                            "phase": current.phase if current else "starting",
                            "status": current.status if current else "starting",
                            "model_id": current.model_id if current else "",
                            "model_role": current.model_role if current else "",
                        }
                        stream_open = self._sse_event("heartbeat", heartbeat)
                        continue

                    event_type = str(event.get("type") or "message")
                    payload = {k: v for k, v in event.items() if k != "type"}
                    stream_open = self._sse_event(event_type, payload)

                # If the client disappeared, the daemon worker continues the durable
                # task to completion; reconnect/status UI can inspect the task ledger.
                self.close_connection = True
                return
            if path == "/api/chat":
                message = str(body.get("message", "")).strip()
                mode = str(body.get("mode", "auto"))
                if not message:
                    self._json({"error": "message is required"}, 400)
                    return
                coding_model_optional = (
                    mode == "auto"
                    and self.state.agent.can_run_without_coding_model(message)
                )
                if not coding_model_optional:
                    readiness = self.state.runtime.readiness(probe_external=True)
                    if not readiness.get("ready_to_code"):
                        self._json({
                            "error": "No usable coding model is ready. Open Local system → Coding readiness, install/configure a model, then retry.",
                            "code": "coding_model_setup_required",
                            "readiness": readiness,
                        }, 409)
                        return
                result = self.state.agent.run(message, history=self.state.history, mode=mode)
                self.state.history = self.state.conversation_manager.history(limit=32)
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
                row = self.state.conversation_manager.create("New chat")
                self.state.history = []
                self._json({"ok": True, "conversation": row})
                return

            if path == "/api/tools/state":
                tool_id = str(body.get("tool", "")).strip()
                if not tool_id:
                    self._json({"error": "tool is required"}, 400)
                    return
                spec = self.state.tools.get(tool_id)
                if spec is None:
                    # Allow addressing a tool by manifest id as well as name.
                    match = next(
                        (m for m in self.state.tools.manifests() if m["id"] == tool_id),
                        None,
                    )
                    if match is None:
                        self._json({"error": f"unknown tool '{tool_id}'"}, 404)
                        return
                    tool_id = match["name"]
                enabled = bool(body.get("enabled", True))
                self.state.tools.set_enabled(tool_id, enabled)
                self._json({"ok": True, "tool": self.state.tools.manifest(tool_id)})
                return

            if path == "/api/permissions/level":
                permission = str(body.get("permission", "")).strip()
                level = str(body.get("level", "")).strip().lower()
                try:
                    applied = self.state.permission_manager.set_level(permission, level)
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self.state._update_config_file({
                    "permissions": dict(self.state.permission_manager.permissions),
                    "permission_profile": self.state.permission_manager.profile,
                })
                self._json({"ok": True, "permission": permission, "level": applied, "manager": self.state.permission_manager.summary()})
                return

            if path == "/api/permissions/profile":
                profile = str(body.get("profile", "")).strip().lower()
                try:
                    applied = self.state.permission_manager.apply_profile(profile)
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self.state._update_config_file({
                    "permissions": dict(self.state.permission_manager.permissions),
                    "permission_profile": applied,
                })
                self._json({"ok": True, "profile": applied, "manager": self.state.permission_manager.summary()})
                return

            if path == "/api/processes/action":
                service_id = str(body.get("id", "")).strip()
                action = str(body.get("action", "")).strip().lower()
                if not service_id or action not in {"start", "stop", "restart"}:
                    self._json({"error": "id and action (start|stop|restart) are required"}, 400)
                    return
                try:
                    result = self.state.processes.action(service_id, action)
                except (KeyError, ValueError) as exc:
                    self._json({"error": str(exc)}, 404)
                    return
                except Exception as exc:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                    return
                self._json(result)
                return

            if path == "/api/tools/install":
                tool_id = str(body.get("tool", "")).strip()
                if not tool_id:
                    self._json({"error": "tool is required"}, 400)
                    return
                try:
                    result = self.state.install_tool(tool_id, approve=bool(body.get("approve", False)))
                except KeyError as exc:
                    self._json({"error": str(exc)}, 404)
                    return
                status = 200 if result.get("ok") else (403 if "denied" in str(result.get("error", "")) else 200)
                self._json(result, status)
                return

            if path in {"/api/secrets/set", "/api/secrets/delete"}:
                mode = self.state.permission_manager.effective("credentials.use")
                if mode == "deny":
                    self._json({"error": "credentials.use permission is denied"}, 403)
                    return
                if mode == "ask" and not body.get("approve"):
                    self._json({"ok": False, "needs_approval": True, "permission": "credentials.use"})
                    return
                if body.get("approve") and self.state.permission_manager.level("credentials.use") == "session":
                    self.state.permission_manager.grant_session("credentials.use")
                try:
                    if path.endswith("/set"):
                        self._json(self.state.secrets.set(
                            str(body.get("name", "")), str(body.get("value", "")),
                            description=str(body.get("description", ""))))
                    else:
                        deleted = self.state.secrets.delete(str(body.get("name", "")))
                        self._json({"ok": True, "deleted": deleted})
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                return

            if path == "/api/mcp/action":
                server_id = str(body.get("id", "")).strip()
                action = str(body.get("action", "")).strip().lower()
                if not server_id or action not in {"connect", "disconnect", "restart"}:
                    self._json({"error": "id and action (connect|disconnect|restart) are required"}, 400)
                    return
                try:
                    if action == "connect":
                        row = self.state.mcp.connect(server_id)
                    elif action == "disconnect":
                        row = self.state.mcp.disconnect(server_id)
                    else:
                        row = self.state.mcp.restart(server_id)
                except KeyError as exc:
                    self._json({"error": str(exc)}, 404)
                    return
                except Exception as exc:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                    return
                self._json({"ok": row.get("state") == "connected" or action == "disconnect", "server": row})
                return

            if path == "/api/jobs/cancel":
                job_id = str(body.get("job_id", "")).strip()
                if not job_id:
                    self._json({"error": "job_id is required"}, 400)
                    return
                if job_id.startswith("image-"):
                    job = self.state.images.cancel(job_id[len("image-"):])
                    self._json({"ok": True, "job": job.as_dict()})
                    return
                if job_id.startswith("model_install-"):
                    try:
                        job = self.state.runtime.model_catalog.cancel(job_id[len("model_install-"):])
                    except KeyError:
                        self._json({"error": "model install job not found"}, 404)
                        return
                    self._json({"ok": True, "job": job})
                    return
                try:
                    job = self.state.jobs.cancel(job_id)
                except KeyError:
                    self._json({"error": "job not found or not cancellable through the Job Manager"}, 404)
                    return
                self._json({"ok": True, "job": job.as_dict()})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except KeyError as exc:
            self._json({"error": f"Not found: {exc}"}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def log_message(self, format: str, *args) -> None:
        pass


def create_server(
    config: AgentConfig,
    workspace: Path,
    host: str,
    port: int,
    web_root: Path,
    runtime_root: Path,
    config_path: Path | None = None,
) -> tuple[ThreadingHTTPServer, AppState]:
    state = AppState(config, workspace, runtime_root, config_path=config_path)
    handler = type("ChatNexusHandler", (Handler,), {"state": state, "web_root": web_root})
    server = ThreadingHTTPServer((host, port), handler)
    return server, state


def stop_state(state: AppState) -> None:
    try:
        state.images.backend_runtime.stop()
    finally:
        state.runtime.stop_all()


def serve(config: AgentConfig, workspace: Path, host: str, port: int, web_root: Path, runtime_root: Path, config_path: Path | None = None) -> None:
    server, state = create_server(config, workspace, host, port, web_root, runtime_root, config_path=config_path)
    actual_port = int(server.server_address[1])
    print(f"Chat Nexus v{VERSION}")
    print(f"Workspace: {workspace.resolve()}")
    print(f"UI: http://{host}:{actual_port}")
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
        stop_state(state)
        server.server_close()
