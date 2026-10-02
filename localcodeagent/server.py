from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import secrets
import sys
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from typing import Any, Callable

from .fsutil import atomic_write_text
from . import netdiag
from .agent.orchestrator import AgentOrchestrator
from .image.manager import ImageManager
from .config import AgentConfig, ModelProfile, load_config
from .models.router import ModelRouter
from .models.telemetry import ModelPerformanceTelemetry
from .runtime.manager import RuntimeManager
from .runtime.setup import suggest_model_profiles, write_suggested_models
from .research import ResearchCoordinator
from .permissions import PermissionManager
from .jobs import JobManager
from .processes import ManagedService, ProcessManager
from .tools.base import TOOL_CATEGORIES, ToolRegistry, ToolSpec
from .tools.plugins import load_plugin_manifests
from .tools.downloads import ToolDownloadManager
from .secrets import SecretVault
from .tools.api import register_api_tools
from .tools.buildsys import register_build_tools
from .tools.codeintel import register_codeintel_tools
from .tools.data import register_data_tools
from .events import EventBus, make_emitter
from .tools.docker_tool import register_docker_tools
from .tools.documents import register_document_tools
from .tools.knowledge import register_knowledge_tools
from .tools.sandbox import register_sandbox_tools
from .tools.computer_use import register_computer_use_tools
from .tools.blender3d import register_blender_tools
from .tools.workflows import register_workflow_tools
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
from .workflow.activity import ActivityStore
from .autonomy.missions import DEFAULT_BUDGETS as _DEFAULT_BUDGETS
from .version import version as _canonical_version


VERSION = _canonical_version()


class _LazyActivity:
    """Resolves AppState.activities at call time (created after tools)."""

    def __init__(self, state: "AppState") -> None:
        self._state = state

    def open(self, *a, **kw):
        store = getattr(self._state, "activities", None)
        if store is None:
            return None
        return store.open(*a, **kw)


class AppState:
    def __init__(self, config: AgentConfig, workspace: Path, runtime_root: Path, config_path: Path | None = None, boot: Callable[[float, str, str], None] | None = None) -> None:
        self.config = config
        self.workspace = workspace.resolve()
        self.config_path = (config_path or (runtime_root / "config.json")).expanduser().resolve()
        self._boot: Callable[[float, str, str], None] = boot or (lambda *a: None)
        self._boot(4, "INITIALIZING · NEXUS CORE", "Preparing local application environment")
        self._boot(12, "CHECKING · GPU & SYSTEM RESOURCES", "Detecting CPU, RAM, VRAM, and available compute")
        self.runtime = RuntimeManager(config, base_dir=runtime_root)
        hw = self.runtime.hardware
        gpu_label = ", ".join(g.name for g in getattr(hw, "gpus", []) or []) or "CPU only"
        self._boot(
            14, "INITIALIZING · NEURAL ENGINE",
            f"Applying {getattr(config, 'performance_mode', 'auto')} runtime profile · {gpu_label}",
        )
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
        self._boot(30, "RESTORING · TASK QUEUE", "Recovering queued or interrupted work")
        self.tasks = TaskStore(self.workspace)
        from .workqueue import WorkQueue
        self.queue = WorkQueue(self.workspace)
        self.checkpoints = CheckpointManager(self.workspace)
        # Drop snapshots whose task aged out of the ledger — checkpoints
        # hold per-file copies and would otherwise grow without bound.
        self.checkpoints.prune_orphans({t["id"] for t in self.tasks.recent(1_000_000)})
        self.memory = ProjectMemory(self.workspace)
        self._boot(38, "RESTORING · MEMORY & KNOWLEDGE", "Preparing conversation and learned knowledge continuity")
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
        self._boot(52, "LOADING · NEXUS BRAIN", "Verifying persistent intelligence and signed Brain state")
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
        # Nexus Answer Memory: learned Q&A tier that can answer trusted
        # repeated questions without invoking a model at all.
        am_path = Path(getattr(config, "answer_memory_path", "data/nexus_brain/answer_memory.db")).expanduser()
        if not am_path.is_absolute():
            am_path = runtime_root / am_path
        self.answer_memory = self._build_answer_memory(config, am_path)
        self._boot(64, "PREPARING · WORKSPACE", "Restoring project and repository context")
        self.repository_index = RepositoryIndex(self.workspace)
        self.research = ResearchCoordinator(self.workspace, self.repository_index, config)
        self._boot(68, "INITIALIZING · PERMISSION SYSTEM", "Applying Nexus Core authorization policies")
        self.permission_manager = PermissionManager(
            config.permissions,
            profile=getattr(config, "permission_profile", "custom"),
            autonomous=getattr(config, "autonomous_mode", False),
            scopes=getattr(config, "permission_scopes", None),
            audit_path=runtime_root / "data" / "permission_audit.jsonl",
        )
        # The "creator" approval level requires an unlocked Nexus Brain session.
        self.permission_manager.creator_verified = (
            lambda: bool(self.nexus_brain.verified_for_session))
        tools_state_path = Path(getattr(config, "tools_state_path", "data/tools_state.json")).expanduser()
        if not tools_state_path.is_absolute():
            tools_state_path = runtime_root / tools_state_path
        self.tools = ToolRegistry(self.permission_manager, state_path=tools_state_path)
        jobs_path = Path(getattr(config, "jobs_path", "data/jobs.json")).expanduser()
        if not jobs_path.is_absolute():
            jobs_path = runtime_root / jobs_path
        self.jobs = JobManager(jobs_path)
        self.events = EventBus()
        self._stream_sinks: list = []  # live chat SSE queues that also want voice events
        self.jobs.on_change = make_emitter(self.events, "job")
        self.images.on_change = make_emitter(self.events, "image_job")
        self.tools.on_event = make_emitter(self.events, "tool")
        self.processes = ProcessManager()
        self.processes.on_event = make_emitter(self.events, "process")
        self._register_processes()
        if getattr(config, "process_watchdog", True):
            self.processes.start_watchdog(on_tick=self._watchdog_maintenance)
        self._boot(74, "REGISTERING · TOOLS & PLUGINS", "Loading installed capabilities and tool manifests")
        register_filesystem_tools(self.tools, self.workspace, checkpoints=self.checkpoints, tasks=self.tasks)
        register_shell_tools(self.tools, self.workspace)
        self.terminal_tracker = register_terminal_tools(
            self.tools, self.workspace, jobs=self.jobs, log_dir=runtime_root / ".agent" / "runtime"
        )
        register_search_tools(self.tools, self.workspace)
        register_build_tools(self.tools, self.workspace)
        register_codeintel_tools(self.tools, self.workspace)
        self.secrets = SecretVault(runtime_root / "data" / "secrets.vault")
        # Mask vaulted values in live tool-output chunks and logs.
        self.tools.context["redactor"] = self.secrets.redact
        register_api_tools(self.tools, vault=self.secrets)
        register_data_tools(self.tools, self.workspace, artifacts_dir=runtime_root / "data" / "charts")
        register_media_tools(self.tools, self.workspace, jobs=self.jobs)
        register_document_tools(self.tools, self.workspace, jobs=self.jobs)
        register_knowledge_tools(self.tools, self.workspace)
        register_sandbox_tools(self.tools, self.workspace)
        # activities store is created later in __init__ — resolve lazily.
        self.computer_use = register_computer_use_tools(
            self.tools, self.workspace,
            activity=_LazyActivity(self))
        # ---- Platform services (0.7 line) --------------------------------
        from .twin import DigitalTwin
        from .artifacts import ArtifactManager
        from .backups import BackupService
        from .health import HealthService
        from .connectors import ConnectorRegistry
        from .knowledge import KnowledgeGraph
        from .skills import SkillRegistry
        from .rag import RepoIndex
        from .lsp import LspPool
        from .eval import EvalLab, ExperimentStore
        self.twin = DigitalTwin(runtime_root / "data" / "twin.json")
        self.artifacts = ArtifactManager(runtime_root / "data" / "artifacts")
        self.backups = BackupService(runtime_root)
        self.health = HealthService(runtime_root / "data" / "health.json")
        self.connectors = ConnectorRegistry(
            state_path=runtime_root / "data" / "connectors_audit.json",
            vault=self.secrets,
            permission_check=lambda perm: self.permission_manager.effective(perm))
        self._knowledge_path = runtime_root / "data" / "knowledge_graph.db"
        self.skills = SkillRegistry(runtime_root)
        self._rag_db = self.workspace / ".agent" / "rag_index.db"
        self._lsp_pool: LspPool | None = None
        self._knowledge_obj: KnowledgeGraph | None = None
        self._knowledge_failed = False
        self._rag_obj: RepoIndex | None = None
        self.eval_lab = EvalLab(runtime_root / "data" / "eval")
        self.experiments = ExperimentStore(runtime_root / "data" / "eval")

        # Repository RAG as a first-class tool: incremental symbol+chunk
        # search so coding flows query the index instead of re-reading files.
        from .tools.base import ToolSpec

        def _repo_search(args: dict) -> str:
            try:
                idx = self.rag_index
                idx.update()  # incremental — changed files only
                out = idx.search(str(args.get("query") or ""))
                return json.dumps(out[:30], ensure_ascii=False)
            except Exception as exc:
                return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

        self.tools.register(ToolSpec(
            "repo_search",
            "Search the persistent repository index (symbols, chunks, docs) for a query. Faster and more targeted than scanning files — the index is updated incrementally.",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            "filesystem.read",
            _repo_search,
            category="coding",
            capabilities=["repository_search", "local_rag"],
        ))
        # Register core component probes with the health service.
        self.health.register("autonomy",
                             lambda: "healthy" if getattr(
                                 getattr(self, "autonomy", None),
                                 "enabled", False) else "stopped")
        self.health.register("voice",
                             lambda: "healthy" if getattr(
                                 getattr(self, "voice", None),
                                 "enabled", False) else "stopped")
        self.health.register("llm-runtime", self._probe_llm_runtime)
        # Monitoring only — ComfyUI already self-heals via auto-start on the
        # next image job and the idle evictor; a competing recover loop
        # would restart it while it is legitimately stopped.
        self.health.register("image-backend", self._probe_image_backend)
        self.workflows_dir = Path(getattr(config, "workflows_dir", "workflows")).expanduser()
        if not self.workflows_dir.is_absolute():
            self.workflows_dir = self.workspace / self.workflows_dir
        register_workflow_tools(self.tools, self.workspace, workflows_dir=self.workflows_dir, jobs=self.jobs)
        register_blender_tools(self.tools, self.workspace, jobs=self.jobs)
        register_docker_tools(self.tools, self.workspace)
        register_git_tools(self.tools, self.workspace)
        from .tools.queue import register_queue_tools
        register_queue_tools(self.tools, self.queue)
        if config.github_enabled:
            register_github_tools(self.tools, self.workspace, config)
        register_repository_tools(self.tools, self.repository_index)
        if config.research_enabled:
            register_research_tools(self.tools, self.research)
        register_web_tools(self.tools, runtime_root=runtime_root)
        if config.image_enabled:
            register_image_tools(self.tools, self.images)
        # Local-first voice/TTS subsystem (Kokoro ONNX, CPU by default).
        self.voice = None
        if getattr(config, "voice_enabled", True):
            try:
                from .voice.manager import VoiceManager
                from .voice.tools import register_voice_tools
                vdir = Path(getattr(config, "voice_assets_dir", "data/voice/assets"))
                if not vdir.is_absolute():
                    vdir = runtime_root / vdir
                pdir = Path(getattr(config, "voice_presets_dir", "data/voice/presets"))
                if not pdir.is_absolute():
                    pdir = runtime_root / pdir
                cdir = Path(getattr(config, "voice_cache_dir", "data/voice/cache"))
                if not cdir.is_absolute():
                    cdir = runtime_root / cdir
                _voice_keys = [k for k in asdict(config) if k.startswith("voice_")]

                def _persist_voice() -> None:
                    self.persist_config_fields(_voice_keys)

                self.voice = VoiceManager(
                    config, preset_dir=pdir, cache_dir=cdir, asset_dir=vdir,
                    publish=lambda kind, payload: self._voice_publish(payload),
                    persist=_persist_voice,
                )
                register_voice_tools(self.tools, self.voice)
            except Exception as exc:
                try:
                    self.events.publish("log",
                                        {"level": "warn",
                                         "message": f"voice subsystem unavailable: {exc}"})
                except Exception:
                    pass
                self.voice = None
        manifests_dir = Path(getattr(config, "tool_manifests_dir", "tools/manifests")).expanduser()
        if not manifests_dir.is_absolute():
            manifests_dir = runtime_root / manifests_dir
        if not manifests_dir.is_dir():
            # Packaged builds bundle the manifests inside the backend payload
            # (PyInstaller _MEIPASS) rather than next to config.json.
            bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "tools" / "manifests"
            if bundled.is_dir():
                manifests_dir = bundled
        self.plugin_manifests = load_plugin_manifests(
            manifests_dir, self.tools, workspace=self.workspace, install_root=runtime_root)
        self.tool_downloads = ToolDownloadManager(self.jobs, install_root=runtime_root)
        self.tool_downloads.on_done = lambda _tool: self.tools.refresh_install_status()
        from .tools.updates import ToolUpdateChecker
        self.tool_updates = ToolUpdateChecker(runtime_root)
        self.tools.update_lookup = self.tool_updates.info
        self._register_health_checks()
        self.tool_router = ToolRouter(
            self.tools,
            resources=lambda: (self.runtime.summary() or {}).get("hardware") or {},
            prefer=getattr(config, "preferred_tools", []),
            telemetry_path=runtime_root / "data" / "routing_telemetry.jsonl",
        )
        self.mcp = MCPManager(self.tools, load_mcp_configs(getattr(config, "mcp_servers", [])),
                              vault=self.secrets)
        if getattr(config, "mcp_servers", None):
            self._boot(86, "CONNECTING · MCP SERVICES", "Connecting configured external tool servers")
        try:
            self.mcp.connect_all()
        except Exception:
            pass

        def use_capability(args: dict[str, Any]) -> str:
            capability = str(args.get("capability", "")).strip()
            if not capability:
                return "ERROR: 'capability' is required"
            arguments = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}
            outcome = self.tool_router.execute(
                capability, arguments, approved=bool(args.get("approved", False)))
            return json.dumps(outcome, ensure_ascii=False, default=str)[:20000]

        def find_tools(args: dict[str, Any]) -> str:
            """Registry introspection for the model — discover tools by capability/keyword."""
            query = str(args.get("query", "") or args.get("capability", "")).strip().lower()
            limit = max(1, min(int(args.get("limit", 10)), 30))
            rows = []
            for m in self.tools.manifests():
                if not m.get("callable", True):
                    continue
                haystack = " ".join([m["name"], m.get("display_name", ""), m.get("category", ""),
                                     " ".join(m.get("capabilities", [])), m.get("description", "")]).lower()
                if query and query not in haystack and query not in [c.lower() for c in m.get("capabilities", [])]:
                    continue
                rows.append({
                    "name": m["name"], "description": m["description"][:160],
                    "category": m["category"], "capabilities": m.get("capabilities", [])[:8],
                    "permission": m.get("permission_mode"), "enabled": m.get("enabled"),
                    "install": m.get("install_status"), "installable": m.get("installable"),
                })
            if not rows:
                return json.dumps({"tools": [], "hint": "no matching callable tools — try a broader term or check the Tool Manager"})
            return json.dumps({"tools": rows[:limit], "total": len(rows)}, ensure_ascii=False)

        self.tools.register(ToolSpec(
            "find_tools",
            "Discover available tools by keyword or capability tag. Use before use_capability when unsure what capabilities exist — returns name, description, permission mode, and install state of matching tools.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "keyword or capability to search for"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
            "filesystem.read", find_tools,
            category="utilities", provider="nexus",
            capabilities=["find_tools", "tool_discovery"],
        ))

        def system_resources(args: dict[str, Any]) -> str:
            summary = self.runtime.summary() or {}
            return json.dumps({
                "hardware": summary.get("hardware"),
                "runtimes": summary.get("runtimes"),
                "processes": self.processes.list(),
            }, ensure_ascii=False, default=str)[:15000]

        self.tools.register(ToolSpec(
            "system_resources",
            "Report local hardware and runtime resource status — CPUs/GPUs/VRAM/RAM, running model runtimes and managed processes. Use before picking GPU-heavy tools or large models.",
            {"type": "object", "properties": {}},
            "filesystem.read", system_resources,
            category="utilities", provider="nexus",
            capabilities=["system_resources", "hardware_status", "resource_check"],
        ))

        def install_tool_call(args: dict[str, Any]) -> str:
            tool_id = str(args.get("tool", "")).strip()
            if not tool_id:
                return "ERROR: 'tool' is required (tool id from find_tools or the Tool Manager)"
            try:
                outcome = self.install_tool(tool_id, approve=bool(args.get("approved", False)))
            except KeyError as exc:
                return f"ERROR: {exc}"
            return json.dumps(outcome, ensure_ascii=False)

        self.tools.register(ToolSpec(
            "install_tool",
            "Install a missing manifest tool via its package manager (winget/choco/pip/uv/npm/apt/brew) as a tracked job. Requires packages.install approval. Use after find_tools/use_capability reports a tool as not installed.",
            {
                "type": "object",
                "properties": {"tool": {"type": "string", "description": "tool id to install"}},
                "required": ["tool"],
            },
            "packages.install", install_tool_call,
            category="utilities", provider="nexus",
            capabilities=["install_tool", "package_install", "provision_tool"],
        ))

        self.tools.register(ToolSpec(
            "use_capability",
            "Request a capability (e.g. 'ocr_image', 'convert_video', 'execute_code') and let the Tool Router pick the best installed/permitted tool for it. Prefer this when the exact tool name is unknown — the router ranks candidates, applies resource/permission checks, and falls back automatically. Pass 'arguments' matching the resolved tool's schema. If the result is 'tools_not_installed', reply to the user with the tool names in 'missing' and explain they must be installed first (Tools page or install_tool) — do not attempt the request without them.",
            {
                "type": "object",
                "properties": {
                    "capability": {"type": "string", "description": "semantic capability tag"},
                    "arguments": {"type": "object", "description": "arguments for the resolved tool"},
                },
                "required": ["capability"],
            },
            "filesystem.read", use_capability,
            category="utilities", provider="nexus",
            capabilities=["use_capability", "capability_dispatch", "auto_tool_selection"],
        ))
        self.activities = ActivityStore(runtime_root / "data" / "activity.jsonl")
        self.activities.on_row = lambda row: self.events.publish("activity", row)
        self.runtime.on_residency_event = self._residency_activity
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
            answer_memory=self.answer_memory,
            activities=self.activities,
            digital_twin=self.twin,
            # Callable keeps the knowledge-graph SQLite store lazily opened —
            # it must not lock files during AppState construction.
            knowledge_graph=lambda: self.knowledge,
        )
        self.history: list[dict] = self.conversation_manager.history(limit=32)
        self._brain_creator_token = ""
        self._prewarm_thread: threading.Thread | None = None
        # Autonomous supervisor — persistent missions, triggers, schedules,
        # standing goals. Never widens permissions; interactive lane wins.
        self._boot(90, "INITIALIZING · AUTONOMY", "Restoring missions, triggers, and schedules")
        self.autonomy = self._build_autonomy(config, runtime_root)
        self.queue.enrich = self._queue_enrich_mission
        self._boot(94, "SYNCHRONIZING · RUNTIME STATE", "Synchronizing running services and task state")
        self._start_primary_prewarm()
        self._start_auto_tune()
        self._start_auto_resume()
        if getattr(config, "autonomy_enabled", True):
            self.autonomy.start()

    # SQLite/subprocess-backed services are lazy — they hold OS handles
    # (file locks, child processes) only once actually used.
    @property
    def rag_index(self):
        if self._rag_obj is None:
            from .rag import RepoIndex
            self._rag_obj = RepoIndex(self.workspace,
                                      db_path=self._rag_db)
        return self._rag_obj

    @property
    def knowledge(self):
        if self._knowledge_failed:
            return None
        if self._knowledge_obj is None:
            try:
                from .knowledge import KnowledgeGraph
                self._knowledge_obj = KnowledgeGraph(self._knowledge_path)
            except Exception:
                self._knowledge_failed = True
                return None
        return self._knowledge_obj

    @property
    def lsp_pool(self):
        if self._lsp_pool is None:
            from .lsp import LspPool as _Pool
            self._lsp_pool = _Pool(self.workspace)
        return self._lsp_pool

    def exchange_for_message(self, message_id: str) -> tuple[str, str]:
        """Resolve a message id to (user_question, assistant_answer)."""
        if not message_id or self.conversation_manager is None:
            return ("", "")
        try:
            for conv in self.conversation_manager.snapshot().get("conversations", []):
                messages = conv.get("messages", [])
                for i, msg in enumerate(messages):
                    if str(msg.get("id")) != message_id:
                        continue
                    answer = str(msg.get("content") or "")
                    question = ""
                    for prev in reversed(messages[:i]):
                        if prev.get("role") == "user":
                            question = str(prev.get("content") or "")
                            break
                    return (question, answer)
        except Exception:
            pass
        return ("", "")

    def _build_answer_memory(self, config, am_path: Path):
        """Construct AnswerMemory with live dependency fingerprints and
        whitelisted dynamic-answer handlers (no code is ever read from the
        database — handler keys map to these closures only)."""
        from .answer_memory import AnswerMemory
        from .answer_memory import invalidation as _am_inv

        def _installed_text_models() -> str:
            ids = [m.id for m in self.config.models if m.enabled]
            return ", ".join(ids) if ids else "No text models are configured."

        def _installed_image_models() -> str:
            models = getattr(self.config, "image_models", []) or []
            ids = [m.get("id", "") for m in models if isinstance(m, dict) and m.get("enabled", True)]
            return ", ".join(ids) if ids else "No image models are configured."

        def _default_image_model() -> str:
            models = getattr(self.config, "image_models", []) or []
            t2i = [
                m for m in models
                if isinstance(m, dict) and m.get("enabled", True)
                and "text_to_image" in (m.get("capabilities") or [])
            ]
            if not t2i:
                return "No text-to-image model is configured."
            best = max(t2i, key=lambda m: int(m.get("priority", 0) or 0))
            return str(best.get("display_name") or best.get("id") or "unknown")

        return AnswerMemory(
            am_path,
            enabled=bool(getattr(config, "answer_memory_enabled", True)),
            semantic_enabled=bool(getattr(config, "answer_memory_semantic_enabled", True)),
            auto_learn=bool(getattr(config, "answer_memory_auto_learn", True)),
            auto_promote=bool(getattr(config, "answer_memory_auto_promote", True)),
            semantic_threshold=float(getattr(config, "answer_memory_semantic_threshold", 0.50)),
            possible_threshold=float(getattr(config, "answer_memory_possible_threshold", 0.30)),
            max_experiences=int(getattr(config, "answer_memory_max_experiences", 20000)),
            retention_days=int(getattr(config, "answer_memory_experience_retention_days", 90)),
            max_db_mb=int(getattr(config, "answer_memory_max_db_mb", 256)),
            workspace=str(self.workspace),
            config_fingerprint_fn=lambda: _am_inv.config_fingerprint(self.config),
            repo_head_fn=lambda: _am_inv.repo_head(self.workspace),
            brain_revision_fn=lambda: str(
                (self.nexus_brain.summary() or {}).get("schema_version", "")
            ),
            handlers={
                "app_version": lambda: f"Nexus Core {VERSION}",
                "installed_text_models": _installed_text_models,
                "installed_image_models": _installed_image_models,
                "default_image_model": _default_image_model,
            },
        )

    def _residency_activity(self, event: dict) -> None:
        """Surface managed-runtime reclaim/rewarm decisions on the timeline."""
        try:
            action = str(event.get("action") or "")
            model_id = str(event.get("model_id") or "")
            reason = str(event.get("reason") or "")
            current = self.tasks.current()
            task_id = current.id if current is not None else "system"
            if action == "evict":
                row = self.activities.open(
                    task_id, "vram", "Freeing VRAM",
                    f"Unloading {model_id}" + (f" — {reason}" if reason else ""),
                )
            elif action == "relaunch":
                row = self.activities.open(
                    task_id, "model", "Restarting Model",
                    f"Relaunching {model_id}" + (f" — {reason}" if reason else ""),
                )
            else:
                row = self.activities.open(
                    task_id, "model", "Starting Model",
                    f"Rewarming {model_id}" + (f" — {reason}" if reason else ""),
                )
            self.activities.update(task_id, row["id"], state="completed")
        except Exception:
            pass

    def _start_auto_resume(self) -> None:
        """In autonomous mode, restart tasks interrupted by a core restart.

        The task ledger already marks orphaned 'running' tasks as
        'interrupted' on load. recover() rebuilds the session from durable
        task/checkpoint state and re-inspects the repository, so no model
        context is required. Bounded by autonomous_max_recoveries so a
        crash-looping task cannot resume forever.
        """
        if not (getattr(self.config, "autonomous_mode", False)
                and getattr(self.config, "autonomous_resume_interrupted", True)):
            return
        max_recoveries = max(0, int(getattr(self.config, "autonomous_max_recoveries", 3)))

        def resume() -> None:
            try:
                candidates = [
                    t for t in self.tasks.recent(50)
                    if t.get("status") == "interrupted"
                    and int(t.get("recovery_count") or 0) < max_recoveries
                ]
                for task in candidates:
                    try:
                        tid = str(task.get("id") or "")
                        self.events.publish("task", {
                            "event": "auto_resume",
                            "task_id": tid,
                        })
                        # Honest recovery marker on the task timeline: the
                        # previously-open rows were already marked
                        # 'interrupted' at store load; this row shows the
                        # resume attempt as its own step.
                        rec_row_id = None
                        try:
                            row = self.activities.open(
                                tid, "recovery", "Recovering Task",
                                "Resuming interrupted work",
                                details={"recovery_count": int(task.get("recovery_count") or 0) + 1})
                            rec_row_id = row["id"]
                        except Exception:
                            pass
                        voice_rid = self._voice_begin()
                        try:
                            res = self.agent.recover(
                                str(task["id"]),
                                event_callback=self._voice_tee(voice_rid, self._bus_emit))
                            self._voice_finish(voice_rid, res.content)
                            if rec_row_id:
                                status = str((res.task or {}).get("status") or "")
                                self.activities.update(
                                    tid, rec_row_id,
                                    state="completed" if status == "done" else "failed",
                                    summary=f"Resumed · task {status or 'finished'}")
                        except Exception:
                            self._voice_finish(voice_rid)
                            if rec_row_id:
                                try:
                                    self.activities.update(
                                        tid, rec_row_id, state="failed",
                                        summary="Resume attempt failed")
                                except Exception:
                                    pass
                            raise
                    except Exception as exc:
                        self.events.publish("task", {
                            "event": "auto_resume_failed",
                            "task_id": task.get("id"),
                            "error": f"{type(exc).__name__}: {exc}",
                        })
            except Exception:
                pass

        threading.Thread(target=resume, name="auto-resume-interrupted", daemon=True).start()

    def _build_autonomy(self, config: AgentConfig, runtime_root: Path):
        """Construct the AutonomousSupervisor with live service hooks.

        The executor drives mission 'agent' nodes through the same
        AgentOrchestrator the chat lane uses — identical permissions,
        verification, checkpoints and event streaming — so autonomous work
        is fully observable on the normal timeline.
        """
        from .autonomy import AutonomousSupervisor

        def emit(etype: str, payload: dict) -> None:
            try:
                self.events.publish(str(etype or "mission"), payload)
            except Exception:
                pass

        def executor(mission: dict, node: dict, emit_cb) -> dict:
            voice_rid = self._voice_begin()
            self.agent.current_mission_id = str(mission.get("id") or "") or None
            try:
                result = self.agent.run(
                    str(node.get("instruction") or node.get("title") or ""),
                    history=[], mode="auto",
                    event_callback=emit_cb,
                )
                task = result.task or {}
                status = str(task.get("status") or "")
                out = {
                    "ok": status in {"done", "reverted"},
                    "output": result.content or str(task.get("error") or ""),
                    "task_id": str(task.get("id") or ""),
                    "artifacts": list(task.get("files_changed") or [])[:20],
                }
                if result.pending_approval or status == "waiting_approval":
                    out["pending_approval"] = (
                        result.pending_approval
                        or task.get("pending_approval") or {"kind": "task"})
                # Tag any pre-stamp rows so /api/activity?mission_id finds the
                # whole node run even if a row was opened before stamping.
                if out["task_id"] and mission.get("id"):
                    try:
                        for row in self.activities.for_task(out["task_id"]):
                            if not row.get("mission_id"):
                                self.activities.update(
                                    out["task_id"], row["id"],
                                    mission_id=str(mission["id"]))
                    except Exception:
                        pass
                return out
            finally:
                self.agent.current_mission_id = None
                self._voice_finish(voice_rid)

        def lane_free() -> bool:
            """Interactive chat/queued work outranks background missions."""
            try:
                recent = self.tasks.recent(10)
                if any(t.get("status") in {"running", "verifying", "reviewing",
                                           "waiting_approval"} for t in recent):
                    return False
                if getattr(self, "_queue_running", None):
                    return False
                if getattr(self, "_retrying_tasks", None):
                    return False
                return True
            except Exception:
                return False

        hooks = {
            "evict_idle_models": lambda: self.runtime.evict_idle(),
            "stop_models": lambda: self.runtime.stop_all(),
            "restart_service": lambda: True,   # process watchdog owns restarts
            "health_probe": lambda: bool(self.runtime.summary()),
            "reduce_context": lambda: True,     # marker: retry runs leaner
            "refresh_workspace": lambda: self.repository_index.rebuild()
                if hasattr(self.repository_index, "rebuild") else True,
        }

        quiet = getattr(config, "autonomy_quiet_hours", None)
        quiet_hours = tuple(quiet) if isinstance(quiet, (list, tuple)) and len(quiet) == 2 else None

        return AutonomousSupervisor(
            workspace=self.workspace,
            store_root=runtime_root / "data" / "autonomy",
            emit=emit,
            bus=self.events,
            executor=executor,
            job_runner=self._mission_job,
            research_runner=self._mission_research,
            lane_free=lane_free,
            permission_manager=self.permission_manager,
            activities=self.activities,
            runtime_hooks=hooks,
            resources=lambda: (self.runtime.summary() or {}).get("hardware") or {},
            quiet_hours=quiet_hours,
            enabled=bool(getattr(config, "autonomy_enabled", True)),
        )

    def _queue_enrich_mission(self, item: dict) -> dict:
        """Attribute a queue_task call made inside a mission agent run back
        to its owning mission (single agent lane ⇒ one owner at a time)."""
        mid = getattr(self.autonomy, "_lane_mission", None)
        if not mid:
            return {}
        return {"mission_id": mid, "source": "mission_subtask"}

    # -- health probes ---------------------------------------------------

    def _probe_llm_runtime(self) -> str:
        """Aggregate managed-model state into a component health value."""
        try:
            statuses = [s for s in (self.runtime._status or {}).values()
                        if getattr(s, "managed", True)]
        except Exception:
            return "degraded"
        states = {getattr(s, "state", "") for s in statuses}
        if not statuses:
            return "stopped"
        if "error" in states:
            return "crashed"
        if "loading" in states or "starting" in states:
            return "starting"
        if states <= {"running", "external", "external_unreachable"}:
            return "healthy" if "running" in states or "external" in states else "degraded"
        return "stopped"

    def _probe_image_backend(self) -> str:
        rt = getattr(getattr(self, "images", None), "backend_runtime", None)
        if rt is None:
            return "stopped"
        st = getattr(rt.status, "state", "stopped")
        if st in ("running", "healthy"):
            return "healthy"
        if st in ("starting", "loading"):
            return "starting"
        if st in ("error", "crashed"):
            return "crashed"
        return "stopped"

    def _mission_job(self, mission: dict, node: dict) -> dict:
        """Run an async platform job as a mission DAG node.

        ``node.metadata.job`` selects the operation:
          sandbox    — run ``command`` (shell string) or ``argv`` inside a
                       Sandbox with the workspace as cwd (bounded, cleaned)
          backup     — create a versioned state backup
          rag_update — incremental repository index refresh
          image      — submit an ImageRequest and wait for completion
          model_install — download an image model profile's components and
                          wait for the install job to finish
        """
        meta = dict(node.get("metadata") or {})
        op = str(meta.get("job") or "")
        try:
            if op == "sandbox":
                from .sandbox import Sandbox
                sbx = Sandbox()
                try:
                    argv = meta.get("argv")
                    if not argv:
                        cmd = str(meta.get("command") or "")
                        if not cmd:
                            return {"ok": False, "output": "sandbox job: no command"}
                        argv = (["cmd.exe", "/c", cmd] if os.name == "nt"
                                else ["sh", "-c", cmd])
                    r = sbx.run(list(argv), cwd=self.workspace,
                                timeout=float(meta.get("timeout", 120)),
                                allow_network=bool(meta.get("network", False)))
                    out = (r.get("stdout") or "")[-4000:]
                    err = (r.get("stderr") or "")[-2000:]
                    return {"ok": bool(r.get("ok")),
                            "output": out + (("\n[stderr]\n" + err) if err else ""),
                            "sandbox": {k: r.get(k) for k in
                                        ("exit", "timed_out", "elapsed_s",
                                         "mem_limit_mb", "job_limited")}}
                finally:
                    sbx.cleanup()
            if op == "backup":
                r = self.backups.create(label=str(meta.get("label") or "mission"))
                return {"ok": bool(r.get("ok")),
                        "output": f"backup {r.get('backup', '')} "
                                  f"({r.get('files', 0)} files)" if r.get("ok")
                        else f"backup failed: {r.get('error', 'unknown')}"}
            if op == "rag_update":
                r = self.rag_index.update(force=bool(meta.get("force", False)))
                return {"ok": True,
                        "output": f"index: +{r.get('added', 0)} added, "
                                  f"{r.get('updated', 0)} updated, "
                                  f"{r.get('removed', 0)} removed"}
            if op == "model_install":
                model_id = str(meta.get("model") or "")
                try:
                    profile = self.images.router.get_profile(model_id)
                except Exception:
                    profile = None
                if profile is None:
                    return {"ok": False,
                            "output": f"unknown image model: {model_id!r}"}
                job = self.images.library.start_install(
                    profile, repair=bool(meta.get("repair")))
                deadline = time.time() + float(meta.get("timeout", 3600))
                while time.time() < deadline:
                    cur = next((j for j in self.images.library.install_jobs()
                                if j["id"] == job["id"]), job)
                    if cur.get("state") in ("finished", "failed"):
                        return {"ok": cur["state"] == "finished",
                                "output": f"model install {cur['state']}"
                                          f"{': ' + cur['error'] if cur.get('error') else ''}",
                                "job": cur}
                    time.sleep(2.0)
                return {"ok": False, "output": "model install timed out",
                        "job": job}
            if op == "image":
                from .image.types import ImageRequest
                req = ImageRequest(
                    prompt=str(meta.get("prompt") or node.get("instruction") or ""),
                    operation=str(meta.get("operation") or "auto"),
                    width=int(meta.get("width", 1024)),
                    height=int(meta.get("height", 1024)),
                    count=max(1, min(int(meta.get("count", 1)), 4)))
                job = self.images.create_job(req)
                deadline = time.time() + float(meta.get("timeout", 600))
                while time.time() < deadline:
                    cur = self.images.get_job(job.id)
                    if cur.state in ("finished", "failed", "cancelled"):
                        arts = [job.id]
                        if cur.state == "finished":
                            arts = [self._register_output_artifact(
                                p, mission_id=str(mission.get("id") or ""),
                                task_id=str(node.get("id") or ""),
                                tool="comfyui").get("id")
                                for p in (getattr(cur, "outputs", []) or [])]
                        return {"ok": cur.state == "finished",
                                "output": f"image job {cur.state}: {cur.stage}",
                                "artifacts": arts}
                    time.sleep(1.0)
                return {"ok": False, "output": "image job timed out",
                        "artifacts": [job.id]}
            return {"ok": False, "output": f"unknown job op: {op!r}"}
        except Exception as exc:
            return {"ok": False, "output": f"{op or 'job'} failed: {exc}"}

    def _register_output_artifact(self, path, *, mission_id: str = "",
                                  task_id: str = "", tool: str = "") -> dict:
        """Register a produced file and record its provenance in the
        knowledge graph (artifact produced-by task/mission, contained-in
        repo) so prompts can surface real lineage."""
        rec = self.artifacts.register(
            path, creator="mission" if mission_id else "agent",
            mission_id=mission_id, task_id=task_id, tool=tool)
        try:
            kg = self.knowledge
            kg.add_entity("artifact", str(rec["name"]),
                          attrs={"kind": rec.get("kind"),
                                 "artifact_id": rec.get("id")})
            kg.add_entity("repo", self.workspace.name)
            kg.link(f"repo:{self.workspace.name}",
                    f"artifact:{rec['name']}", "contains")
            if task_id:
                kg.add_entity("task", task_id)
                kg.link(f"task:{task_id}",
                        f"artifact:{rec['name']}", "produced")
            if mission_id:
                kg.add_entity("mission", mission_id)
                kg.link(f"mission:{mission_id}",
                        f"artifact:{rec['name']}", "produced")
        except Exception:
            pass
        return rec

    def _mission_research(self, mission: dict, node: dict) -> dict:
        """Run a mission 'research' node through the real coordinator —
        repository/local-docs first, web sources when policy allows."""
        query = str(node.get("instruction") or node.get("title") or "")
        if not query.strip():
            return {"ok": False, "output": "research node: no query"}
        try:
            r = self.research.research_topic(query)
        except Exception as exc:
            return {"ok": False, "output": f"research failed: {exc}"}
        status = str(r.get("status") or "")
        return {"ok": status.startswith("completed"),
                "output": str(r.get("summary") or "")[:4000],
                "research": {"status": status,
                             "sources": len(r.get("sources") or []),
                             "errors": list(r.get("errors") or [])[:5]}}

    # -- mission chat commands -----------------------------------------
    #
    # Natural imperatives like "make this a mission" convert chat context
    # into a structured mission. Creation is never silent: every command
    # replies with exactly what was created and under which profile.

    _MISSION_CREATE_PHRASES = (
        "make this a mission", "turn this into a mission", "make it a mission",
        "keep working on this", "keep working until", "work on this until",
        "continue this overnight", "keep going until", "don't stop until",
    )
    _MISSION_STANDING_PHRASES = (
        "standing goal", "every day", "each day", "every morning",
        "check this daily", "check every day", "weekly", "every week",
    )

    def _mission_context_request(self, exclude: str) -> str:
        """The most recent real user request before the command itself."""
        try:
            for m in reversed(self.conversation_manager.history(limit=32)):
                if str(m.get("role")) != "user":
                    continue
                text = str(m.get("content") or "").strip()
                if text and text.lower() != exclude.lower():
                    return text
        except Exception:
            pass
        return ""

    def _mission_command(self, message: str) -> dict | None:
        """Detect autonomy imperatives; returns {"content", ...} or None."""
        if not getattr(self.config, "autonomy_enabled", True):
            return None
        low = message.strip().lower()
        sup = self.autonomy

        if any(p in low for p in ("stop autonomy", "stop all missions",
                                  "stop autonomous work", "halt autonomy")):
            out = sup.stop_autonomy()
            return {"content": (
                f"Autonomy stopped. {len(out['paused_missions'])} mission(s) "
                "paused cooperatively — no new autonomous work will start "
                "until you resume it from the Missions page.")}

        if any(p in low for p in ("resume autonomy", "start autonomy",
                                  "resume missions", "continue autonomy")):
            sup.resume_autonomy()
            return {"content": "Autonomy resumed — paused missions can continue."}

        if any(p in low for p in ("cancel the mission", "stop the mission",
                                  "stop working on that mission",
                                  "cancel that mission", "stop that mission")):
            live = [m for m in sup.missions.list()
                    if m.get("status") in
                    {"ready", "active", "planning", "executing", "verifying",
                     "evaluating", "replanning", "waiting_dependency",
                     "waiting_approval"}]
            if not live:
                return {"content": "There is no active mission to stop."}
            sup.cancel_mission(live[0]["id"])
            return {"content": f"Stopped mission '{live[0]['title']}'. Its state is preserved on the Missions page."}

        wants_standing = any(p in low for p in self._MISSION_STANDING_PHRASES)
        wants_mission = any(p in low for p in self._MISSION_CREATE_PHRASES)
        if not (wants_mission or wants_standing):
            return None

        context = self._mission_context_request(message)
        objective = context or message
        if not objective.strip():
            return {"content": "What should the mission objective be?"}
        if wants_standing:
            sched = {"kind": "daily", "hour": 9, "minute": 0} \
                if any(w in low for w in ("day", "daily", "morning")) \
                else {"kind": "weekly", "hour": 9, "minute": 0, "weekday": 0}
            goal = sup.add_standing_goal(
                objective, schedule=sched,
                success_criteria=[{"kind": "all_tasks_completed",
                                   "description": "goal run completes"}],
                notification_policy="important")
            return {"content": (
                f"Standing goal created: “{objective[:140]}”\n\n"
                f"It runs on a {sched['kind']} schedule under the Local "
                "Autonomous profile and notifies on important events. "
                "Manage it on the Missions page — disable any time.")}
        mission = sup.create_mission(
            objective=objective,
            user_request=objective,
            scope="workspace" if "this" in low or "overnight" in low else "one_shot",
            autonomy_profile="local_autonomous",
            source="chat",
            workspace=str(self.workspace),
            success_criteria=[{"kind": "all_tasks_completed",
                               "description": "all mission tasks completed"},
                              {"kind": "verify_passed",
                               "description": "verification passes"}])
        sup.start_mission(mission["id"])
        return {"content": (
            f"Mission created: “{mission['title'][:120]}”\n\n"
            "The supervisor is planning it now — watch live progress on the "
            "Missions page. It runs under the Local Autonomous profile; "
            "sensitive actions still ask for approval.")}

    def _mission_reply_result(self, text: str) -> dict:
        """Synthesize an agent-result-shaped payload for command replies."""
        task = self.tasks.create("autonomy command", "auto")
        task = self.tasks.update(
            task.id, status="done", phase="done",
            model_id="autonomy", model_role="mission",
            summary=text[:200], final_content=text)
        return {
            "content": text,
            "routing": {"role": "mission", "model_id": "autonomy",
                        "complexity": "trivial",
                        "reasons": ["autonomy_command"]},
            "tool_events": [], "model_events": [], "steps": 0,
            "task": task.as_dict(), "pending_approval": None,
            "verification": [], "review": "", "research": {},
            "response_source": "autonomy", "memory": {},
            "image_jobs": [],
            "runtime": self.runtime.summary(probe_external=False),
        }

    def _autonomy_watchdog(self) -> None:
        """Watchdog tick: bounded restart of a dead supervisor thread —
        detected via heartbeat, never on a single slow tick."""
        try:
            autonomy = getattr(self, "autonomy", None)
            if autonomy is None or not autonomy.enabled:
                return
            if autonomy._running and (
                    autonomy._thread is None or not autonomy._thread.is_alive()):
                restarts = getattr(self, "_autonomy_restarts", 0)
                if restarts >= 3:
                    return
                self._autonomy_restarts = restarts + 1
                self.events.publish("autonomy", {
                    "event": "supervisor_restart",
                    "attempt": restarts + 1})
                autonomy._running = False
                autonomy.start()
        except Exception:
            pass

    def _start_primary_prewarm(self) -> None:
        if not self.config.runtime_auto_start:
            return
        # Warm the fast-lane utility model first: it is small, cheap, and the
        # model ordinary conversation hits before anything else.
        utility = next(
            (
                profile
                for profile in self.config.models
                if profile.enabled and profile.keep_loaded and "utility" in profile.roles
            ),
            None,
        )
        if utility is not None and utility.runtime == "llama_cpp":
            self._boot(95, "INITIALIZING · NEURAL ENGINE", "Preparing fast conversational intelligence")
            threading.Thread(
                target=self._prewarm_with_retry, args=(utility,),
                name="chat-nexus-utility-prewarm", daemon=True,
            ).start()
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

        self._prewarm_thread = threading.Thread(
            target=self._prewarm_with_retry, args=(starter,),
            name="chat-nexus-primary-prewarm",
            daemon=True,
        )
        self._prewarm_thread.start()

    def _prewarm_with_retry(self, profile: ModelProfile, attempts: int = 8, delay: float = 45.0) -> None:
        """Warm a model, retrying while it doesn't fit yet.

        At boot the GPU may still be draining the previous session's models
        or busy with desktop apps — a single resource_fit snapshot that fails
        would leave the app cold until the first task pays the full load.
        Bounded retries ride out transient contention without retrying
        forever on genuinely unstartable profiles.
        """
        for attempt in range(attempts):
            try:
                if self.runtime.resource_fit(profile)[0]:
                    self.runtime.ensure_ready(profile)
                    return
            except Exception:
                # Runtime status captures the real failure; prewarm must never
                # prevent UI startup.
                return
            time.sleep(delay)

    def _start_auto_tune(self) -> None:
        """Idle-gated background benchmark for untuned managed models.

        Runs once per boot: after the idle settle, each llama_cpp profile
        without a valid fingerprinted tuned result gets a bounded sweep.
        While any task is running the tuner waits — probes launch real
        llama-server processes and must never compete with active work.
        """
        if not getattr(self.config, "runtime_auto_tune", True):
            return
        targets = [
            p for p in self.config.models
            if p.enabled and p.runtime == "llama_cpp" and p.model_path
        ]
        if not targets:
            return

        def _lane_busy() -> bool:
            try:
                if any(
                    t.get("status") in {"running", "verifying", "reviewing"}
                    for t in self.tasks.recent(10)
                ):
                    return True
                return bool(getattr(self, "_queue_running", None))
            except Exception:
                return True  # uncertain → don't benchmark

        def _has_result(profile) -> bool:
            try:
                tuner = self.runtime.tuner
                stored = tuner._data["results"].get(profile.id)
                return bool(stored and stored.get("fingerprint") == tuner.fingerprint(profile))
            except Exception:
                return True  # can't verify → leave it alone

        def _worker() -> None:
            time.sleep(max(5.0, float(getattr(
                self.config, "runtime_auto_tune_idle_seconds", 45.0))))
            for profile in targets:
                if _has_result(profile):
                    continue
                # Wait for an idle window — missions/queues can start at
                # any time; check cheaply every few seconds, bounded.
                for _ in range(240):  # ~20 min max wait
                    if not _lane_busy():
                        break
                    time.sleep(5.0)
                if _lane_busy():
                    return  # still busy after the window — skip this boot
                try:
                    self.events.publish("model", {"event": {
                        "type": "auto_tune_start", "model_id": profile.id}})
                    result = self.runtime.tuner.benchmark(profile)
                    self.events.publish("model", {"event": {
                        "type": "tuning_complete", "model_id": profile.id,
                        "status": result.get("status"), "source": "auto",
                        "tps": (result.get("best") or {}).get("metrics", {}).get(
                            "predicted_per_second"),
                    }})
                except Exception as exc:
                    self.events.publish("model", {"event": {
                        "type": "auto_tune_failed", "model_id": profile.id,
                        "error": f"{type(exc).__name__}: {exc}"[:200]}})

        threading.Thread(target=_worker, name="runtime-auto-tune", daemon=True).start()

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
        try:
            if self.answer_memory is not None and getattr(self.answer_memory, "store", None):
                self.answer_memory.store.close()
            am_path = Path(getattr(config, "answer_memory_path", "data/nexus_brain/answer_memory.db")).expanduser()
            if not am_path.is_absolute():
                am_path = self.runtime.base_dir / am_path
            self.answer_memory = self._build_answer_memory(config, am_path)
        except Exception:
            pass
        self.agent.conversation_memory = self.conversation_memory
        self.agent.conversation_manager = self.conversation_manager
        self.agent.knowledge_memory = self.knowledge_memory
        self.agent.model_growth = self.model_growth
        self.agent.nexus_brain = self.nexus_brain
        self.agent.answer_memory = self.answer_memory
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
        _tmp_backup = backup_path.with_suffix(".json.tmp")
        _tmp_backup.write_text(json.dumps(backup, indent=2), encoding="utf-8")
        _tmp_backup.replace(backup_path)

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
        atomic_write_text(self.config_path, json.dumps(raw, indent=2, ensure_ascii=False))
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
        atomic_write_text(self.config_path, json.dumps(raw, indent=2, ensure_ascii=False))
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
        atomic_write_text(self.config_path, json.dumps(raw, indent=2, ensure_ascii=False))

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
                metadata={"roles": list(profile.roles), "model_path": profile.model_path,
                          "auto_restart": True},
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
                metadata={"auto_restart": True},
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

    def _watchdog_maintenance(self) -> None:
        self._evict_idle_models()
        self._expire_stale_approvals()
        self._retry_failed_tasks()
        self._dequeue_next()
        self._check_disk_space()
        self._unload_idle_voice_engine()
        self._autonomy_watchdog()

    def _unload_idle_voice_engine(self) -> None:
        """Release the TTS model after voice_idle_unload_seconds of silence —
        keeps overnight sessions lean without losing anything."""
        voice = self.voice
        if voice is None:
            return
        idle_s = float(getattr(self.config, "voice_idle_unload_seconds", 600.0))
        if idle_s <= 0:
            return
        try:
            eng = voice._engines.get("kokoro") or next(
                iter(voice._engines.values()), None)
            if eng is None or getattr(eng, "_model", None) is None:
                return
            last = getattr(eng, "_last_used", getattr(eng, "_loaded_at", 0.0))
            if time.time() - last > idle_s:
                eng.unload()
                self._voice_publish({"event": "engine_idle_unload",
                                     "engine": "kokoro"})
        except Exception:
            pass

    def _check_disk_space(self) -> None:
        """Warn when the drive holding durable state is nearly full — writes
        fail silently on a full disk and unattended runs would corrupt.
        Throttled to one event per hour."""
        import shutil
        now = time.monotonic()
        if now - getattr(self, "_disk_warn_at", 0.0) < 3600:
            return
        try:
            usage = shutil.disk_usage(self.state.workspace)
            free_gb = usage.free / (1024 ** 3)
        except OSError:
            return
        if free_gb < 2.0:
            self._disk_warn_at = now
            self._bus_emit({"type": "model", "event": {
                "type": "disk_low",
                "free_gb": round(free_gb, 2),
                "path": str(self.state.workspace),
            }})

    def _bus_emit(self, event: dict) -> None:
        """Publish an agent event to the shared bus.

        Mirrors the /api/chat/stream policy: tokens and the final result stay
        off the bus (chat-only), everything else is published with task
        attribution so reconnecting pages can follow unattended runs.
        """
        etype = str(event.get("type", ""))
        if etype in {"token", "result"}:
            return
        payload = dict(event)
        payload.pop("type", None)
        try:
            # Prefer the id carried by the event itself (e.g. an approval
            # timeout on an older task) over the newest ledger record.
            event_task_id = str((event.get("task") or {}).get("id") or "")
            current = self.tasks.current()
            task_id = event_task_id or (current.id if current is not None else "")
            if task_id:
                payload.setdefault("task_id", task_id)
        except Exception:
            pass
        self.events.publish(etype or "task", payload)

    def persist_config_fields(self, keys) -> None:
        """Merge selected config fields back into config.json (atomic), so
        voice settings survive restarts without touching unrelated keys."""
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8")) \
                if self.config_path.exists() else {}
        except Exception:
            raw = {}
        full = asdict(self.config)
        for k in keys:
            if k in full:
                raw[k] = full[k]
        atomic_write_text(self.config_path, json.dumps(raw, indent=2, ensure_ascii=False))

    def _voice_publish(self, payload: dict) -> None:
        """Voice events go to the shared bus AND any live chat SSE sinks so
        playback segments reach the requesting client mid-stream."""
        event = {"type": "voice", **payload}
        try:
            self._bus_emit(event)
        except Exception:
            pass
        for sink in list(getattr(self, "_stream_sinks", [])):
            try:
                sink.put(dict(event))
            except Exception:
                pass

    def _voice_begin(self) -> str:
        """Open a speech context for a new assistant response.

        Starting at request time (rather than on the first streamed token)
        interrupts prior speech immediately and pre-warms the engine while
        the model is still thinking.
        """
        rid = f"chat-{secrets.token_hex(6)}"
        voice = self.voice
        if voice is not None:
            try:
                voice.begin_task(rid)
            except Exception:
                pass
        return rid

    def _voice_tee(self, rid: str, callback: Callable | None = None) -> Callable:
        """Wrap an agent event_callback so token deltas also feed speech."""
        voice = self.voice

        def tee(event: dict) -> None:
            if voice is not None:
                try:
                    if event.get("type") == "token":
                        voice.feed_token(
                            rid,
                            str(event.get("text") or event.get("delta") or ""))
                except Exception:
                    pass
            if callback is not None:
                callback(event)

        return tee

    def _voice_finish(self, rid: str, final_text: str = "") -> None:
        """Flush the streamer tail; speak final_text if nothing streamed."""
        voice = self.voice
        if voice is not None:
            try:
                voice.finish_task(rid, final_text)
            except Exception:
                pass

    def _dequeue_next(self, *, blocking: bool = False) -> None:
        """Start the next queued prompt when no task is active.

        Runs regardless of autonomous mode — queued work is explicit user
        intent; autonomous mode only affects permission levels inside it.
        Single-flight: skipped while any task is running or being retried.
        """
        if not len(self.queue):
            return
        if not hasattr(self, "_queue_running"):
            self._queue_running = set()
        if not hasattr(self, "_dequeue_lock"):
            self._dequeue_lock = threading.Lock()
        # Called from the watchdog, run-completion handlers, and enqueue —
        # two callers must not both observe idle and start two tasks. The
        # completion-chained call waits briefly: it may lose the lock to the
        # dequeue that spawned it, and dropping it would stall the queue.
        if not self._dequeue_lock.acquire(timeout=2 if blocking else 0):
            return
        try:
            recent = self.tasks.recent(20)
            if any(t.get("status") in {"running", "verifying", "reviewing", "waiting_approval"} for t in recent):
                return
            if getattr(self, "_retrying_tasks", None) or self._queue_running:
                return
            item = self.queue.pop()
            if item is None:
                return
            item_id = str(item["id"])
            if item_id in self._queue_running:
                return

            def run_item(entry: dict) -> None:
                voice_rid = self._voice_begin()
                try:
                    self.events.publish("task", {"event": "dequeued", "queue_item": entry})
                    result = self.agent.run(
                        str(entry["prompt"]),
                        history=self.history,
                        mode=str(entry.get("mode") or "auto"),
                        event_callback=self._voice_tee(voice_rid, self._bus_emit),
                    )
                    self._voice_finish(voice_rid, result.content)
                    self.history = self.conversation_manager.history(limit=32)
                except Exception as exc:
                    self._voice_finish(voice_rid)
                    self.events.publish("task", {
                        "event": "queue_item_failed", "queue_item": entry,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                finally:
                    self._queue_running.discard(entry["id"])
                    # Chain the next queued prompt immediately instead of
                    # waiting up to a full watchdog interval. Wait briefly for
                    # the lock — the dequeue that spawned this worker may still
                    # hold it while spawning, and losing it would stall work.
                    self._dequeue_next(blocking=True)

            self._queue_running.add(item_id)
            threading.Thread(target=run_item, args=(item,), name=f"queue-{item_id}", daemon=True).start()
        except Exception:
            pass
        finally:
            self._dequeue_lock.release()

    def _retry_failed_tasks(self) -> None:
        """Re-drive error tasks in autonomous mode after a backoff.

        recover() rebuilds the session from durable ledger/checkpoint state,
        so a task that failed while unattended (e.g. the model runtime died
        past in-drive recovery) gets another bounded attempt instead of
        sitting dead until someone notices. Bounded by
        autonomous_max_recoveries; single-flight: never runs while any task
        is active, waiting, or already being retried.
        """
        if not getattr(self.config, "autonomous_mode", False):
            return
        retry_after = float(getattr(self.config, "autonomous_error_retry_seconds", 120.0) or 0.0)
        max_recoveries = max(0, int(getattr(self.config, "autonomous_max_recoveries", 3)))
        if retry_after <= 0 or max_recoveries <= 0:
            return
        if not hasattr(self, "_retrying_tasks"):
            self._retrying_tasks = set()
        try:
            # A popped queue item may not have created its task row yet —
            # respect the in-flight set so retry never races a dequeue.
            if getattr(self, "_queue_running", None):
                return
            now = time.time()
            recent = self.tasks.recent(50)
            if any(t.get("status") in {"running", "verifying", "reviewing", "waiting_approval"} for t in recent):
                return
            stale = [
                t for t in recent
                if t.get("status") == "error"
                and int(t.get("recovery_count") or 0) < max_recoveries
                and now - float(t.get("updated_at") or now) >= retry_after
                and str(t.get("id")) not in self._retrying_tasks
            ]
            for item in stale[:1]:  # one retry per tick
                task_id = str(item["id"])
                self._retrying_tasks.add(task_id)

                def retry(tid: str = task_id) -> None:
                    voice_rid = self._voice_begin()
                    try:
                        self.events.publish("task", {"event": "auto_retry", "task_id": tid})
                        res = self.agent.recover(
                            tid,
                            event_callback=self._voice_tee(voice_rid, self._bus_emit))
                        self._voice_finish(voice_rid, res.content)
                    except Exception as exc:
                        self._voice_finish(voice_rid)
                        self.events.publish("task", {
                            "event": "auto_retry_failed", "task_id": tid,
                            "error": f"{type(exc).__name__}: {exc}",
                        })
                    finally:
                        self._retrying_tasks.discard(tid)

                threading.Thread(target=retry, name=f"auto-retry-{task_id}", daemon=True).start()
        except Exception:
            pass

    def _expire_stale_approvals(self) -> None:
        """Fail approval-parked tasks that outlived the autonomous wait bound.

        Only applies in autonomous mode; with autonomy off, waiting_approval
        tasks wait for a human indefinitely, as before.
        """
        timeout = float(getattr(self.config, "autonomous_approval_timeout_seconds", 0.0) or 0.0)
        if not getattr(self.config, "autonomous_mode", False) or timeout <= 0:
            return
        try:
            now = time.time()
            for item in self.tasks.recent(50):
                if item.get("status") != "waiting_approval":
                    continue
                if now - float(item.get("updated_at") or now) < timeout:
                    continue
                task = self.tasks.update(
                    item["id"],
                    status="error",
                    phase="done",
                    pending_approval=None,
                    error=(
                        f"Approval timed out after {int(timeout)}s in autonomous mode; "
                        f"the {item.get('pending_approval', {}).get('name', 'action')} action was not approved."
                    ),
                )
                self.events.publish("task", {"task": task.as_dict(), "event": "approval_timeout"})
                try:
                    self.tasks.append_log(item["id"], f"## task error/done\n## error: {task.error}\n")
                    self.tasks.flush_log(item["id"])
                except Exception:
                    pass
                try:
                    self.agent._close_session(str(item["id"]))
                except Exception:
                    pass
        except Exception:
            pass

    def _evict_idle_models(self) -> None:
        """Watchdog tick: reclaim memory from managed models that are not in use.

        Models serving an in-flight task are pinned; models parked behind a
        waiting_approval task are safe to unload since the task state is
        durable and ensure_ready() restores the runtime on resume.
        """
        try:
            busy = {
                str(t.get("model_id") or "")
                for t in self.tasks.recent(50)
                if t.get("status") in {"running", "verifying", "reviewing"}
            }
            busy.discard("")
            stopped = self.runtime.evict_idle(busy_models=busy)
            for model_id in stopped:
                self.events.publish("model", {"event": {"type": "idle_evicted",
                                                        "model_id": model_id}})
            self._evict_idle_comfyui()
        except Exception:
            pass

    def _evict_idle_comfyui(self) -> None:
        """Stop the managed ComfyUI process after an idle bound — it holds GPU
        memory even when no image job is running. Only managed processes are
        stopped; an external ComfyUI install belongs to the user."""
        timeout = float(getattr(self.config, "comfyui_idle_unload_seconds", 0.0) or 0.0)
        if timeout <= 0:
            return
        try:
            rt = self.images.backend_runtime
            status = rt.status
            if not (status.managed and status.state == "running"):
                return
            if self.images.has_active_jobs():
                return
            # Idle since the later of last job activity or process start —
            # auto-start boots ComfyUI before any job touches it.
            idle_since = max(
                float(getattr(self.images, "last_activity", 0.0)),
                float(getattr(status, "started_at", 0.0) or 0.0),
            )
            if time.time() - idle_since < timeout:
                return
            rt.stop()
            self.events.publish("model", {"event": {"type": "idle_evicted",
                                                    "model_id": "comfyui",
                                                    "role": "image_backend"}})
        except Exception:
            pass

    def _update_config_file(self, updates: dict) -> None:
        """Merge keys into config.json atomically, preserving unrelated settings."""
        if self.config_path.exists():
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            raw = asdict(self.config)
        raw.update(updates)
        atomic_write_text(self.config_path, json.dumps(raw, indent=2, ensure_ascii=False))

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
        gate = self._permission_gate("packages.install", approve, spec.name)
        if gate is not None:
            if gate.get("needs_approval"):
                gate["install"] = install
            return gate
        if manifest.get("os_supported") is False:
            return {"ok": False,
                    "error": f"{spec.display_name} is not supported on this OS "
                             f"({', '.join(manifest.get('supported_os') or ['other'])} only)"}
        if str(install.get("method") or "").strip().lower() == "archive":
            size = int(install.get("size_bytes") or 0)
            if size:
                import shutil
                free = shutil.disk_usage(str(self.runtime.base_dir)).free
                if free < size:
                    return {"ok": False,
                            "error": f"not enough disk space — the download alone needs "
                                     f"{size / 1e9:.1f} GB, only {free / 1e9:.1f} GB free"}
                result = self.tool_downloads.install(
                    spec.name, spec.display_name, install, version=spec.version)
                # Archive + extracted payload coexist at peak; warn when tight.
                if result.get("ok") and free < size * 3:
                    result["warning"] = (
                        f"disk may be tight — {free / 1e9:.1f} GB free; extraction could "
                        f"need ~{size * 2 / 1e9:.1f} GB more")
                return result
            return self.tool_downloads.install(
                spec.name, spec.display_name, install, version=spec.version)
        cmd = install_command(install, install_root=self.runtime.base_dir)
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

    def _permission_gate(self, key: str, approve: bool, label: str = "") -> dict | None:
        """Shared approval gate for gated API actions.

        Returns a response dict when the action must stop (denied / needs
        approval / needs creator verification), else None. On approval it also
        records session grants and an audit entry.
        """
        manager = self.permission_manager
        mode = manager.effective(key)
        if mode == "deny":
            manager.record_event("denied", key, label)
            return {"ok": False, "error": f"{key} permission is denied"}
        if mode in ("ask", "creator") and not approve:
            manager.record_event("approval_requested", key, label)
            return {"ok": False, "needs_approval": True, "permission": key,
                    "needs_creator": mode == "creator"}
        if approve and manager.level(key) == "creator" and not manager.creator_ok():
            manager.record_event("creator_required", key, label)
            return {"ok": False, "needs_creator": True, "permission": key,
                    "error": "requires an unlocked Nexus Brain creator session"}
        if approve and manager.level(key) == "session":
            manager.grant_session(key)
        if approve or mode == "creator":
            manager.record_event("approved", key, label)
        return None

    def uninstall_tool(self, tool_id: str, *, approve: bool = False) -> dict:
        """Remove a manifest tool (tracked job).

        Archive installs delete their payload directory; package-manager
        installs run the manager's own remove command. Tools without an
        automatable removal report manual instructions.
        """
        from .tools.plugins import uninstall_command

        spec = self.tools.get(tool_id)
        if spec is None:
            match = next((m for m in self.tools.manifests() if m["id"] == tool_id), None)
            if match is None:
                raise KeyError(f"unknown tool '{tool_id}'")
            spec = self.tools.get(match["name"])
        manifest = self.tools.manifest(spec.name)
        install = manifest.get("install") or {}
        is_archive = str(install.get("method") or "").strip().lower() == "archive"
        cmd = None if is_archive else uninstall_command(
            install, install_root=self.runtime.base_dir)
        if not is_archive and cmd is None:
            return {"ok": False,
                    "error": "automated removal is not available for this tool "
                             "— remove it with its package manager manually"}
        gate = self._permission_gate("packages.install", approve, spec.name)
        if gate is not None:
            if gate.get("needs_approval"):
                gate["action"] = "uninstall"
            return gate
        if is_archive:
            return self.tool_downloads.uninstall(spec.name, spec.display_name, install)
        job = self.jobs.submit("tool_remove", f"Remove {spec.display_name}",
                               metadata={"tool": spec.name, "phase": "queued"})

        def _run_remove() -> None:
            import subprocess
            self.jobs.update(job.id, state="running", status="removing",
                             detail=" ".join(cmd[:3]))
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                if proc.returncode == 0:
                    self.jobs.update(job.id, state="completed", status="finished",
                                     progress=1.0, detail=(proc.stdout or "")[-300:])
                else:
                    self.jobs.update(job.id, state="failed",
                                     error=(proc.stderr or proc.stdout or "uninstall failed")[-300:])
            except FileNotFoundError:
                self.jobs.update(job.id, state="failed", error=f"package manager not found: {cmd[0]}")
            except subprocess.TimeoutExpired:
                self.jobs.update(job.id, state="failed", error="uninstall timed out")
            changed = self.tools.refresh_install_status()
            if changed:
                self.jobs.update(job.id, detail=f"install status updated: {changed}")

        threading.Thread(target=_run_remove, daemon=True).start()
        return {"ok": True, "job_id": job.id, "tool": spec.name,
                "method": str(install.get("method") or "")}

    def check_tool_updates(self, *, approve: bool = False) -> dict:
        """Probe real package sources for newer tool versions (tracked job)."""
        gate = self._permission_gate("network.read", approve, "update check")
        if gate is not None:
            return gate
        job = self.jobs.submit("tool_update_check", "Check for tool updates",
                               metadata={"phase": "queued"})
        from .tools.plugins import managed_python

        def _run() -> None:
            self.jobs.update(job.id, state="running", status="checking")
            checked = found = 0
            try:
                py = managed_python(self.runtime.base_dir)
                for m in self.tools.manifests():
                    if not m.get("install"):
                        continue
                    if m.get("install_status") != "installed":
                        continue
                    res = self.tool_updates.probe(
                        m["id"], m.get("install"), python=py)
                    checked += 1
                    if res.get("status") == "checked":
                        found += 1
                self.tool_updates.save()
                self.jobs.update(
                    job.id, state="completed", status="finished", progress=1.0,
                    detail=f"checked {checked} tools, {found} versions resolved")
            except Exception as exc:  # noqa: BLE001 — job must not die silently
                self.jobs.update(job.id, state="failed", error=str(exc)[:300])

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "job_id": job.id}

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
            "queue": self.queue.list(),
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
        tools = self.tools.manifests()
        payload["tools_summary"] = {
            "total": len(tools),
            "installed": sum(1 for t in tools if t["install_status"] == "installed"),
            "missing": sum(1 for t in tools if t["install_status"] == "missing"),
            "disabled": sum(1 for t in tools if not t["enabled"]),
            "missing_tools": [t["name"] for t in tools
                              if t["install_status"] == "missing" and t["callable"]][:20],
        }
        payload["self_hosting_ready"] = bool(
            payload.get("ready_to_code")
            and self_tree
            and git_repo
            and payload["isolated_selftest_available"]
        )
        return payload

    def diagnostics_payload(self) -> dict:
        """Aggregated local-backend diagnostics: per-model process health,
        crash signatures, log tails, resource picture and recent transport
        failures. Contains no prompts or secrets."""
        hw = self.runtime.hardware
        models: list[dict] = []
        for m in self.config.models:
            entry: dict = {"id": m.id, "runtime": m.runtime,
                           "role": getattr(m, "role", "")}
            if m.runtime == "llama_cpp":
                try:
                    entry["backend"] = self.runtime.backend_health(m.id)
                except Exception:
                    entry["backend"] = {}
            models.append(entry)
        return {
            "version": VERSION,
            "time": time.time(),
            "hardware": hw.as_dict() if hasattr(hw, "as_dict") else {},
            "models": models,
            "processes": self.processes.list(),
            "recent_failures": netdiag.recent_failures(),
        }


def _clean_attachments(body: dict) -> list[dict]:
    """Validate/normalize chat attachments from the request body.

    Text content is capped at 400 KB per file server-side (the agent lane
    trims further to fit the context budget); image data URLs at ~12 MB
    decoded. Anything unparseable is dropped rather than failing the chat.
    """
    raw = body.get("attachments")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "file")[:200]
        kind = "image" if str(item.get("kind")) == "image" else "file"
        entry: dict = {"name": name, "kind": kind}
        if kind == "image":
            data_url = str(item.get("data_url") or "")
            if data_url.startswith("data:") and len(data_url) <= 17_000_000:
                entry["data_url"] = data_url
        else:
            content = item.get("content")
            if isinstance(content, str) and content and len(content) <= 400_000:
                entry["content"] = content
        if "data_url" in entry or "content" in entry:
            out.append(entry)
    return out


def _queueable_message(message: str, attachments: list[dict]) -> str:
    """Fold text attachments into a queued prompt — queue items only carry
    plain text, so attachments survive a busy-queue hop."""
    texts = [a for a in attachments if a.get("content")]
    if not texts:
        return message
    blocks = [f"--- {a.get('name', 'file')} ---\n{str(a['content'])[:40000]}"
              for a in texts[:8]]
    return message + "\n\nAttached context:\n" + "\n\n".join(blocks)


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
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # ------------------------------------------------------------------
    # Autonomy API — missions, triggers, schedules, standing goals,
    # approvals, notifications, global control.

    _AUTONOMY_PREFIXES = ("/api/missions", "/api/autonomy", "/api/triggers",
                          "/api/schedules", "/api/standing-goals")

    _PLATFORM_PREFIXES = ("/api/health", "/api/twin", "/api/artifacts",
                          "/api/skills", "/api/connectors", "/api/knowledge",
                          "/api/rag", "/api/eval", "/api/experiments",
                          "/api/lsp", "/api/backups", "/api/simulate")

    def _platform_get(self, path: str) -> bool:
        q = parse_qs(urlparse(self.path).query)
        if path == "/api/health":
            self.state.health.tick()
            self._json(self.state.health.summary())
            return True
        if path == "/api/twin":
            self._json(self.state.twin.status())
            return True
        if path == "/api/artifacts":
            self._json({"artifacts": self.state.artifacts.list(
                kind=(q.get("kind") or [""])[0],
                mission_id=(q.get("mission") or [""])[0],
                task_id=(q.get("task") or [""])[0])})
            return True
        if path == "/api/skills":
            self._json({"skills": self.state.skills.list()})
            return True
        if path == "/api/connectors":
            self._json({"connectors": self.state.connectors.status()})
            return True
        if path == "/api/knowledge":
            if self.state.knowledge is None:
                self._json({"available": False})
                return True
            name = (q.get("q") or [""])[0]
            payload: dict = {"stats": self.state.knowledge.stats()}
            if name:
                payload["entities"] = self.state.knowledge.find_entities(
                    name_like=name)
                payload["context"] = self.state.knowledge.context_for(name)
            self._json(payload)
            return True
        if path == "/api/rag":
            query = (q.get("q") or [""])[0]
            payload = {"stats": self.state.rag_index.stats()}
            if query:
                payload["results"] = self.state.rag_index.search(query)
            self._json(payload)
            return True
        if path == "/api/lsp":
            self._json(self.state.lsp_pool.status())
            return True
        if path == "/api/eval/history":
            self._json({"runs": self.state.eval_lab.history(
                suite=(q.get("suite") or [""])[0],
                subject=(q.get("subject") or [""])[0])})
            return True
        if path == "/api/experiments":
            self._json({"experiments": self.state.experiments.list()})
            return True
        if path == "/api/backups":
            self._json({"backups": self.state.backups.list()})
            return True
        return False

    def _platform_post(self, path: str, body: dict) -> bool:
        if path == "/api/backups/create":
            self._json(self.state.backups.create(
                label=str(body.get("label", ""))))
            return True
        if path == "/api/backups/restore":
            out = self.state.backups.restore(
                str(body.get("backup", "")),
                dry_run=bool(body.get("dry_run", False)))
            self._json(out, 400 if not out.get("ok") else 200)
            return True
        if path == "/api/simulate":
            from .simulate import simulate_plan
            perm_map = {t.name: t.permission
                        for t in self.state.tools._tools.values()}
            out = simulate_plan(dict(body.get("plan") or {}),
                                policy=self.state.autonomy.policy,
                                permission_map=perm_map,
                                twin=self.state.twin)
            self._json(out)
            return True
        if path == "/api/rag/update":
            self._json(self.state.rag_index.update(
                force=bool(body.get("force", False))))
            return True
        if path == "/api/skills/enable" or path == "/api/skills/disable":
            ok = self.state.skills.set_enabled(
                str(body.get("name", "")), path.endswith("enable"))
            self._json({"ok": ok}, 404 if not ok else 200)
            return True
        if path == "/api/knowledge/entity" and self.state.knowledge:
            self._json(self.state.knowledge.add_entity(
                str(body.get("kind", "note")), str(body.get("name", "")),
                attrs=body.get("attrs")))
            return True
        if path == "/api/knowledge/link" and self.state.knowledge:
            self._json(self.state.knowledge.link(
                str(body.get("src", "")), str(body.get("dst", "")),
                str(body.get("rel", "related")), attrs=body.get("attrs")))
            return True
        if path == "/api/connectors/call":
            out = self.state.connectors.call(
                str(body.get("connector", "")),
                str(body.get("capability", "")),
                **dict(body.get("params") or {}))
            self._json(out, 400 if not out.get("ok") else 200)
            return True
        if path == "/api/experiments/create":
            self._json(self.state.experiments.create(
                str(body.get("hypothesis", "")),
                arms=list(body.get("arms") or []),
                metric=str(body.get("metric", "score"))))
            return True
        if path == "/api/experiments/conclude":
            ok = self.state.experiments.conclude(
                str(body.get("id", "")), str(body.get("conclusion", "")))
            self._json({"ok": ok}, 404 if not ok else 200)
            return True
        return False

    def _autonomy_get(self, path: str) -> bool:
        """GET handler; returns True when the route was handled."""
        sup = self.state.autonomy
        query = parse_qs(urlparse(self.path).query)
        if path == "/api/autonomy/status":
            self._json(sup.status())
            return True
        if path == "/api/autonomy/summary":
            self._json(sup.daily_summary(
                hours=float(query.get("hours", ["24"])[0] or 24)))
            return True
        if path == "/api/autonomy/notifications":
            self._json({"notifications": sup.notifications.list(
                unread_only=query.get("unread", [""])[0] == "1")})
            return True
        if path == "/api/autonomy/approvals":
            self._json({"approvals": sup.approvals(
                pending_only=query.get("pending", [""])[0] == "1")})
            return True
        if path == "/api/autonomy/policy":
            self._json({
                "resource_mode": sup.policy.resource_mode(),
                "stopped": sup.policy.is_stopped(),
                "paused": sup.policy.is_paused(),
                "grants": sup.policy.grants(),
            })
            return True
        if path == "/api/missions":
            self._json({"missions": sup.missions.list(
                include_archived=query.get("archived", [""])[0] == "1")})
            return True
        if path.startswith("/api/missions/"):
            mid = path[len("/api/missions/"):].strip("/")
            if mid.endswith("/graph"):
                mid = mid[:-len("/graph")]
                m = sup.missions.get(mid)
                if m is None:
                    self._json({"error": "mission not found"}, 404)
                    return True
                self._json({"graph": m.get("graph") or {"nodes": []}})
                return True
            if mid.endswith("/history"):
                mid = mid[:-len("/history")]
                m = sup.missions.get(mid)
                if m is None:
                    self._json({"error": "mission not found"}, 404)
                    return True
                self._json({
                    "history": m.get("history") or [],
                    "failures": m.get("failure_history") or [],
                    "evaluations": m.get("evaluator_history") or [],
                    "verifications": m.get("verification_history") or [],
                })
                return True
            m = sup.missions.get(mid)
            if m is None:
                self._json({"error": "mission not found"}, 404)
                return True
            self._json({"mission": m})
            return True
        if path == "/api/triggers":
            self._json({"triggers": sup.triggers.list()})
            return True
        if path == "/api/schedules":
            self._json({"schedules": sup.scheduler.list()})
            return True
        if path == "/api/standing-goals":
            self._json({"goals": sup.standing_goals()})
            return True
        return False

    def _autonomy_post(self, path: str, body: dict) -> bool:
        """POST handler; returns True when the route was handled."""
        sup = self.state.autonomy

        if path == "/api/autonomy/stop":
            self._json(sup.stop_autonomy())
            return True
        if path in {"/api/autonomy/resume", "/api/autonomy/start"}:
            self._json(sup.resume_autonomy())
            return True
        if path == "/api/autonomy/pause":
            sup.policy.set_paused(True)
            self._json({"paused": True})
            return True
        if path == "/api/autonomy/notifications/read":
            self._json({"marked": sup.notifications.mark_read(
                str(body.get("id") or "") or None)})
            return True
        if path.startswith("/api/autonomy/approvals/"):
            rest = path[len("/api/autonomy/approvals/"):].strip("/")
            for verb in ("approve", "deny"):
                if rest.endswith("/" + verb):
                    aid = rest[:-len(verb) - 1]
                    out = sup.resolve_approval(aid, approve=(verb == "approve"))
                    if out is None:
                        self._json({"error": "approval not found or already resolved"}, 404)
                        return True
                    self._json({"ok": True, "approval": out})
                    return True
            self._json({"error": "unknown approval action"}, 400)
            return True
        if path == "/api/autonomy/policy":
            mode = body.get("resource_mode")
            if mode:
                sup.policy.set_resource_mode(str(mode))
            self._json({"ok": True, "resource_mode": sup.policy.resource_mode()})
            return True
        if path == "/api/autonomy/grants":
            row = sup.policy.grant(
                str(body.get("action") or ""),
                scope=str(body.get("scope") or ""),
                expires_in_s=float(body.get("expires_in_s") or 0),
                note=str(body.get("note") or ""))
            self._json({"ok": True, "grant": row})
            return True
        if path.startswith("/api/autonomy/grants/") and path.endswith("/revoke"):
            gid = path[len("/api/autonomy/grants/"):-len("/revoke")].strip("/")
            self._json({"ok": sup.policy.revoke(gid)})
            return True

        if path == "/api/missions":
            objective = str(body.get("objective") or body.get("title") or "").strip()
            if not objective:
                self._json({"error": "objective is required"}, 400)
                return True
            m = sup.create_mission(
                objective=objective,
                title=str(body.get("title") or ""),
                user_request=str(body.get("user_request") or objective),
                scope=str(body.get("scope") or "one_shot"),
                priority=str(body.get("priority") or "normal"),
                success_criteria=body.get("success_criteria")
                    if isinstance(body.get("success_criteria"), list) else None,
                constraints=body.get("constraints")
                    if isinstance(body.get("constraints"), list) else None,
                autonomy_profile=str(body.get("autonomy_profile") or "local_autonomous"),
                budgets=body.get("budgets")
                    if isinstance(body.get("budgets"), dict) else None,
                notification_policy=str(body.get("notification_policy") or "important"),
                source=str(body.get("source") or "api"),
                source_id=str(body.get("source_id") or ""),
                workspace=str(self.state.workspace))
            if body.get("start", True):
                m = sup.start_mission(m["id"]) or m
            self._json({"ok": True, "mission": m})
            return True
        if path.startswith("/api/missions/"):
            rest = path[len("/api/missions/"):].strip("/")
            for verb in ("pause", "resume", "cancel", "replan", "ask", "update"):
                suffix = "/" + verb
                if not rest.endswith(suffix):
                    continue
                mid = rest[:-len(suffix)]
                if verb == "pause":
                    out = sup.pause_mission(mid, reason=str(body.get("reason") or "user pause"))
                elif verb == "resume":
                    out = sup.resume_mission(mid)
                elif verb == "cancel":
                    out = sup.cancel_mission(mid)
                elif verb == "replan":
                    out = sup.replan_mission(mid, reason=str(body.get("reason") or "manual replan"))
                elif verb == "ask":
                    self._json({"ok": True,
                                "answer": sup.answer_about_mission(
                                    mid, str(body.get("question") or ""))})
                    return True
                else:  # update — objective/criteria/constraints/priority edits
                    updates = {}
                    for key in ("objective", "title", "priority",
                                "notification_policy"):
                        if key in body:
                            updates[key] = body[key]
                    for key in ("success_criteria", "constraints"):
                        if isinstance(body.get(key), list):
                            updates[key] = body[key]
                    if isinstance(body.get("budgets"), dict):
                        updates["budgets"] = {**_DEFAULT_BUDGETS,
                                              **body["budgets"]}
                    def _bump(row: dict, _u=updates) -> None:
                        row.update(_u)
                        row["revision"] = int(row.get("revision") or 1) + 1
                        row.setdefault("history", []).append({
                            "ts": time.time(), "event": "edited",
                            "detail": f"user edited: {', '.join(_u)}",
                        })
                    out = sup.missions.mutate(mid, _bump)
                if out is None:
                    self._json({"error": "mission not found or illegal transition"}, 404)
                    return True
                self._json({"ok": True, "mission": out})
                return True
            self._json({"error": "unknown mission action"}, 400)
            return True

        if path == "/api/triggers":
            try:
                row = sup.triggers.add(
                    str(body.get("name") or body.get("event") or "trigger"),
                    str(body.get("event") or ""),
                    conditions=body.get("conditions")
                        if isinstance(body.get("conditions"), dict) else None,
                    action=body.get("action")
                        if isinstance(body.get("action"), dict) else None,
                    debounce_s=float(body.get("debounce_s") or 60.0),
                    watch=str(body.get("watch") or ""),
                    enabled=bool(body.get("enabled", True)))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return True
            self._json({"ok": True, "trigger": row})
            return True
        if path.startswith("/api/triggers/"):
            rest = path[len("/api/triggers/"):].strip("/")
            if rest.endswith("/enable") or rest.endswith("/disable"):
                tid = rest.rsplit("/", 1)[0]
                self._json({"ok": sup.triggers.set_enabled(
                    tid, rest.endswith("/enable"))})
                return True
            if rest.endswith("/delete"):
                tid = rest[:-len("/delete")]
                self._json({"ok": sup.triggers.remove(tid)})
                return True
            self._json({"error": "unknown trigger action"}, 400)
            return True

        if path == "/api/schedules":
            try:
                row = sup.scheduler.add(
                    str(body.get("name") or "schedule"),
                    str(body.get("kind") or "once"),
                    at=body.get("at"),
                    interval_s=float(body.get("interval_s") or 0),
                    hour=int(body.get("hour") or 3),
                    minute=int(body.get("minute") or 0),
                    weekday=body.get("weekday"),
                    action=body.get("action")
                        if isinstance(body.get("action"), dict) else None,
                    enabled=bool(body.get("enabled", True)))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return True
            self._json({"ok": True, "schedule": row})
            return True
        if path.startswith("/api/schedules/"):
            rest = path[len("/api/schedules/"):].strip("/")
            if rest.endswith("/delete"):
                self._json({"ok": sup.scheduler.remove(rest[:-len("/delete")])})
                return True
            if rest.endswith("/enable") or rest.endswith("/disable"):
                sid = rest.rsplit("/", 1)[0]
                self._json({"ok": sup.scheduler.set_enabled(
                    sid, rest.endswith("/enable"))})
                return True
            self._json({"error": "unknown schedule action"}, 400)
            return True

        if path == "/api/standing-goals":
            try:
                row = sup.add_standing_goal(
                    str(body.get("objective") or ""),
                    trigger=body.get("trigger")
                        if isinstance(body.get("trigger"), dict) else None,
                    schedule=body.get("schedule")
                        if isinstance(body.get("schedule"), dict) else None,
                    allowed_actions=body.get("allowed_actions")
                        if isinstance(body.get("allowed_actions"), list) else None,
                    notification_policy=str(body.get("notification_policy") or "important"),
                    enabled=bool(body.get("enabled", True)),
                    success_criteria=body.get("success_criteria")
                        if isinstance(body.get("success_criteria"), list) else None,
                    constraints=body.get("constraints")
                        if isinstance(body.get("constraints"), list) else None)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return True
            self._json({"ok": True, "goal": row})
            return True
        if path.startswith("/api/standing-goals/"):
            rest = path[len("/api/standing-goals/"):].strip("/")
            if rest.endswith("/enable") or rest.endswith("/disable"):
                gid = rest.rsplit("/", 1)[0]
                self._json({"ok": sup.set_goal_enabled(gid, rest.endswith("/enable"))})
                return True
            if rest.endswith("/run"):
                gid = rest[:-len("/run")]
                goal = next((g for g in sup.standing_goals() if g.get("id") == gid), None)
                if goal is None:
                    self._json({"error": "goal not found"}, 404)
                    return True
                m = sup._spawn_goal_mission(goal, origin=f"manual:{gid}")
                self._json({"ok": True, "mission": m})
                return True
            self._json({"error": "unknown goal action"}, 400)
            return True
        return False

    def _handle_voice_post(self, path: str, body: dict) -> None:
        """Voice subsystem POST endpoints. TTS failures never reach chat —
        every error returns a concise JSON payload."""
        voice = self.state.voice
        try:
            if path == "/api/voice/mute":
                self._json(voice.set_muted(bool(body.get("muted"))))
                return
            if path == "/api/voice/stop":
                self._json(voice.stop_all(reason=str(body.get("reason") or "user")))
                return
            if path == "/api/voice/speak":
                text = str(body.get("text", "")).strip()
                if not text:
                    self._json({"error": "text is required"}, 400)
                    return
                out = voice.speak_text(
                    text[:20000],
                    preset_id=body.get("preset_id") or None,
                    speed=max(0.5, min(2.0, float(body.get("speed") or 1.0))),
                    auto_filter=bool(body.get("auto_filter", True)),
                )
                self._json(out)
                return
            if path == "/api/voice/preview":
                from .voice.types import PREVIEW_PHRASE, VoicePreset
                text = str(body.get("text") or PREVIEW_PHRASE)
                raw = bool(body.get("raw"))
                if raw:
                    # A/B 'A' side — raw base voice, no preset processing.
                    base_voice = str(body.get("base_voice") or "bf_isabella")
                    import numpy as np
                    import uuid as _uuid
                    from .voice.dsp import wav_bytes
                    eng = voice.engine(str(body.get("engine") or ""))
                    audio, sr = eng.synthesize(
                        text, voice=base_voice,
                        lang="en-gb" if base_voice.startswith("b") else "en-us")
                    pcm = np.asarray(audio, dtype=np.float32)
                    seg = voice.cache.put(
                        f"raw-{base_voice}-{abs(hash(text))}",
                        wav_bytes(np.stack([pcm, pcm], axis=1), sr))
                    seg_id = _uuid.uuid4().hex[:16]
                    voice.segments[seg_id] = seg
                    self._json({"ok": True, "segment_id": seg_id,
                                "url": f"/api/voice/audio/{seg_id}",
                                "seconds": round(pcm.size / sr, 2)})
                    return
                preset_raw = body.get("preset")
                if isinstance(preset_raw, dict):
                    preset = VoicePreset.from_dict(preset_raw)
                    preset.id = preset.id or "_preview"
                    pcm, sr, seg = voice._synthesize(
                        text, preset, max(0.5, min(2.0, float(body.get("speed") or 1.0))))
                    seg_id = voice._register_segment(seg, "preview")
                    self._json({"ok": True, "segment_id": seg_id,
                                "url": f"/api/voice/audio/{seg_id}",
                                "seconds": round(pcm.shape[0] / sr, 2)})
                    return
                out = voice.speak_text(
                    text, preset_id=body.get("preset_id") or None,
                    auto_filter=False)
                self._json(out)
                return
            if path == "/api/voice/config":
                changed = {}
                for key in ("voice_enabled", "voice_muted", "voice_mode",
                            "voice_preset_id", "voice_engine",
                            "voice_output_device", "voice_device"):
                    if key in body:
                        changed[key] = body[key]
                for key in ("voice_volume", "voice_speed"):
                    if key in body:
                        changed[key] = float(body[key])
                for k, v in changed.items():
                    if hasattr(self.state.config, k):
                        setattr(self.state.config, k, v)
                if body.get("muted") is not None:
                    self._json(voice.set_muted(bool(body["muted"])))
                    return
                try:
                    self.state.persist_config_fields(changed.keys())
                except Exception:
                    pass
                self._json({"ok": True, "voice": voice.status()})
                return
            if path == "/api/voice/preset/save":
                from .voice.types import VoicePreset
                raw = body.get("preset")
                if not isinstance(raw, dict):
                    self._json({"error": "preset object required"}, 400)
                    return
                p = voice.presets.save(VoicePreset.from_dict(raw))
                self._json({"ok": True, "preset": p.as_dict()})
                return
            if path == "/api/voice/preset/duplicate":
                src = voice.presets.get(str(body.get("preset_id", "")))
                if src is not None:
                    name = str(body.get("name", "")).strip()
                    p = voice.presets.save_as(src, new_name=name) if name \
                        else voice.presets.duplicate(src.id)
                else:
                    p = None
                if p is None:
                    self._json({"error": "preset not found"}, 404)
                    return
                self._json({"ok": True, "preset": p.as_dict()})
                return
            if path == "/api/voice/preset/rename":
                p = voice.presets.rename(str(body.get("preset_id", "")),
                                         str(body.get("name", "")))
                if p is None:
                    self._json({"error": "preset not found"}, 404)
                    return
                self._json({"ok": True, "preset": p.as_dict()})
                return
            if path == "/api/voice/preset/delete":
                removed = voice.presets.delete(str(body.get("preset_id", "")))
                self._json({"ok": bool(removed)})
                return
            if path == "/api/voice/preset/import":
                p = voice.presets.import_json(str(body.get("json", "")))
                self._json({"ok": True, "preset": p.as_dict()})
                return
            if path == "/api/voice/export":
                import re as _re
                seg_id = str(body.get("segment_id", ""))
                fmt = str(body.get("format", "wav")).lower()
                name = _re.sub(r"[^\w\-]+", "-",
                               str(body.get("name", "")).strip())[:80] or \
                    f"nexus-voice-{seg_id}"
                export_dir = Path(getattr(self.state.config, "voice_cache_dir",
                                          "data/voice/cache"))
                if not export_dir.is_absolute():
                    export_dir = Path(self.state.config_path).parent / export_dir
                dest = export_dir / "exports" / f"{name}.{fmt}"
                out = voice.export_segment(seg_id, fmt, dest)
                self._json({"ok": True, "path": str(out)})
                return
            if path == "/api/voice/assets/install":
                def _install():
                    try:
                        from .voice import assets as _assets
                        _assets.ensure_assets(
                            voice.engine().asset_dir,
                            progress=lambda n, done: self.state._voice_publish(
                                {"event": "asset_progress", "file": n, "bytes": done}))
                        self.state._voice_publish({"event": "assets_ready"})
                    except Exception as exc:
                        self.state._voice_publish({"event": "asset_error",
                                                   "error": str(exc)[:200]})
                threading.Thread(target=_install, name="nexus-voice-assets",
                                 daemon=True).start()
                self._json({"ok": True, "started": True})
                return
            self._json({"error": f"unknown voice endpoint {path}"}, 404)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)[:300]}, 500)

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
        if path == "/api/nexus-brain/history":
            self._json({"versions": self.state.nexus_brain.settings_history()})
            return
        if path == "/api/answer-memory":
            q = parse_qs(urlparse(self.path).query)
            am = self.state.answer_memory
            if am is None:
                self._json({"available": False, "answers": [], "stats": {}})
                return
            self._json({
                "stats": am.stats(),
                "answers": am.list_answers(
                    query=str((q.get("q") or [""])[0]).strip(),
                    trust=str((q.get("trust") or [""])[0]).strip(),
                    project_id=str(self.state.workspace),
                ),
            })
            return
        if path == "/api/answer-memory/export":
            am = self.state.answer_memory
            self._json(am.export() if am is not None else {"version": 1, "answers": []})
            return
        if path == "/api/conversations":
            q = parse_qs(urlparse(self.path).query)
            query = str((q.get("q") or [""])[0]).strip()
            if query:
                self._json({"results": self.state.conversation_manager.search(query)})
            else:
                self._json(self.state.conversation_manager.snapshot())
            return
        if path == "/api/knowledge-memory":
            q = parse_qs(urlparse(self.path).query)
            query = str((q.get("q") or [""])[0]).strip()
            payload = self.state.knowledge_memory.snapshot()
            if query:
                payload["results"] = self.state.knowledge_memory.search(query)
            self._json(payload)
            return
        if path == "/api/model-growth":
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
            payload = self.state.model_telemetry.summary()
            payload["generations"] = self.state.model_telemetry.generation_summary()
            self._json(payload)
            return
        if path == "/api/tools":
            self.state.tools.refresh_install_status()
            import shutil
            manifests = self.state.tools.manifests()
            # Resumable partial downloads: tool name -> bytes kept in .part.
            downloads_dir = self.state.runtime.base_dir / ".agent" / "downloads"
            partials: dict[str, int] = {}
            for t in manifests:
                if str((t.get("install") or {}).get("method") or "").lower() != "archive":
                    continue
                part = downloads_dir / f"{t['name']}.part"
                try:
                    if part.is_file():
                        partials[t["name"]] = part.stat().st_size
                except OSError:
                    pass
            self._json({
                "tools": manifests,
                "categories": TOOL_CATEGORIES,
                "plugins": self.state.plugin_manifests,
                "disk_free_bytes": shutil.disk_usage(str(self.state.runtime.base_dir)).free,
                "partials": partials,
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
            self._json({"routing": self.state.tool_router.recent(50),
                        "stats": self.state.tool_router.stats()})
            return
        if path == "/api/workflows":
            from .tools.workflows import _load_resume, _resume_file, load_workflows
            workflows = load_workflows(self.state.workflows_dir)
            resume_dir = self.state.workspace / ".agent" / "workflow_runs"
            rows = []
            for w in workflows.values():
                prior = _load_resume(_resume_file(resume_dir, str(w["id"])))
                rows.append({
                    "id": w["id"], "name": w.get("name", w["id"]),
                    "description": (w.get("description") or "")[:300],
                    "steps": [s.get("tool") for s in w.get("steps", [])],
                    "params": w.get("params") or {},
                    "resumable": prior is not None,
                    "resume_step": prior.get("next_step") if prior else None,
                    "file": w.get("_path", ""),
                })
            self._json({"directory": str(self.state.workflows_dir), "workflows": rows})
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
        if path == "/api/permissions/audit":
            try:
                limit = int(urlparse(self.path).query.split("limit=")[-1].split("&")[0] or "200")
            except (ValueError, IndexError):
                limit = 200
            self._json({"entries": self.state.permission_manager.audit_entries(
                limit=max(1, min(500, limit)))})
            return
        if path == "/api/jobs":
            self._json(self.state.jobs_payload())
            return
        if path == "/api/processes":
            self._json({"processes": self.state.processes.list()})
            return
        if path == "/api/processes/log":
            params = parse_qs(urlparse(self.path).query)
            service_id = str(params.get("id", [""])[0]).strip()
            row = next((p for p in self.state.processes.list()
                        if p.get("id") == service_id), None)
            if row is None:
                self._json({"error": f"unknown service '{service_id}'"}, 404)
                return
            log_path = str(row.get("log_path") or "")
            if not log_path or not Path(log_path).is_file():
                self._json({"id": service_id, "log": "",
                            "detail": "no log file for this service"})
                return
            try:
                text = Path(log_path).read_text(encoding="utf-8", errors="replace")[-20000:]
                # Redact vaulted secrets before any log content leaves the process.
                text = self.state.secrets.redact(text) if hasattr(self.state.secrets, "redact") else text
            except OSError as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                return
            self._json({"id": service_id, "path": log_path, "log": text})
            return
        if path == "/api/diagnostics":
            self._json(self.state.diagnostics_payload())
            return
        if path == "/api/events":
            # Long-lived SSE stream of job/tool events for live UI updates.
            self._sse_begin()
            try:
                replay = int(parse_qs(urlparse(self.path).query).get("replay", ["20"])[0])
            except ValueError:
                replay = 20
            subscription = self.state.events.subscribe(replay=max(0, min(100, replay)))
            try:
                while True:
                    try:
                        event = subscription.get(timeout=15.0)
                    except queue.Empty:
                        event = {"type": "heartbeat", "ts": time.time(),
                                 "subscribers": self.state.events.subscriber_count()}
                    if not self._sse_event(str(event.pop("type", "message")), event):
                        break
            finally:
                self.state.events.unsubscribe(subscription)
            self.close_connection = True
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
        if path == "/api/tuning":
            self._json(self.state.runtime.tuner.status())
            return
        if path == "/api/readiness":
            self._json(self.state.readiness_payload(probe_external=True))
            return
        if path == "/api/tasks":
            self._json(self.state.task_payload())
            return
        if path == "/api/task-log":
            task_id = parse_qs(urlparse(self.path).query).get("task_id", [""])[0]
            self._json({"task_id": task_id, "log": self.state.tasks.read_log(task_id) if task_id else ""})
            return
        if path == "/api/activity":
            query = parse_qs(urlparse(self.path).query)
            task_id = query.get("task_id", [""])[0]
            mission_id = query.get("mission_id", [""])[0]
            payload: dict = {"task_id": task_id}
            if mission_id:
                payload["mission_id"] = mission_id
                payload["activities"] = self.state.activities.for_mission(mission_id)
            elif task_id:
                rows = self.state.activities.for_task(task_id)
                payload["activities"] = rows
                payload["summary"] = self.state.activities.summary(task_id)
            else:
                latest = self.state.tasks.recent(1)
                rows = self.state.activities.for_task(str(latest[0]["id"])) if latest else []
                task_id = str(latest[0]["id"]) if latest else ""
                payload["task_id"] = task_id
                payload["activities"] = rows
                if task_id:
                    payload["summary"] = self.state.activities.summary(task_id)
            self._json(payload)
            return
        if path == "/api/queue":
            self._json({"items": self.state.queue.list(), "size": len(self.state.queue)})
            return
        if path.startswith(self._AUTONOMY_PREFIXES):
            if self._autonomy_get(path):
                return
            self._json({"error": "unknown autonomy route"}, 404)
            return
        if path.startswith(self._PLATFORM_PREFIXES):
            if self._platform_get(path):
                return
            self._json({"error": "unknown platform route"}, 404)
            return
        if path == "/api/index":
            self._json(self.state.repository_index.summary())
            return
        if path == "/api/research":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            payload = self.state.research.cached(q) if q else self.state.research.summary()
            self._json(payload)
            return
        if path == "/api/image":
            self._json(self.state.images.summary())
            return
        if path == "/api/voice/status":
            self._json(self.state.voice.status() if self.state.voice
                       else {"enabled": False})
            return
        if path == "/api/voice/presets":
            presets = [p.as_dict() for p in self.state.voice.presets.list()] \
                if self.state.voice else []
            self._json({"presets": presets})
            return
        if path == "/api/voice/voices":
            try:
                eng = self.state.voice.engine() if self.state.voice else None
                self._json({"voices": eng.voices() if eng else []})
            except Exception as exc:
                self._json({"voices": [], "error": str(exc)})
            return
        if path.startswith("/api/voice/audio/"):
            seg_id = path[len("/api/voice/audio/"):].strip("/")
            seg = self.state.voice.segment_path(seg_id) if self.state.voice else None
            if seg is None or not seg.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = seg.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/api/voice/preset/") and path.endswith("/export"):
            pid = path[len("/api/voice/preset/"):-len("/export")].strip("/")
            text = self.state.voice.presets.export_json(pid) if self.state.voice else None
            if text is None:
                self._json({"error": "preset not found"}, 404)
                return
            self._json({"ok": True, "json": text})
            return
        if path.startswith("/api/voice/preset/"):
            pid = path[len("/api/voice/preset/"):].strip("/")
            p = self.state.voice.presets.get(pid) if self.state.voice else None
            if p is None:
                self._json({"error": "preset not found"}, 404)
                return
            self._json({"preset": p.as_dict()})
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

    def _answer_memory_post(self, path: str, body: dict) -> None:
        am = self.state.answer_memory
        if am is None or not am.available:
            self._json({"ok": False, "error": "answer memory unavailable"}, 503)
            return
        project_id = str(self.state.workspace)
        # Optional message_id resolves to the exchange's question/answer.
        msg_q = msg_a = ""
        message_id = str(body.get("message_id", ""))
        if message_id:
            msg_q, msg_a = self.state.exchange_for_message(message_id)

        if path == "/api/answer-memory/learn":
            question = str(body.get("question", "") or msg_q)
            answer = str(body.get("answer", "") or msg_a)
            if not question or not answer:
                self._json({"ok": False, "error": "question and answer are required"}, 400)
                return
            self._json(am.learn(
                question, answer,
                scope=str(body.get("scope", "global")),
                project_id=project_id,
                freshness=str(body.get("freshness", "user_defined")),
            ))
            return
        if path == "/api/answer-memory/forget":
            self._json(am.forget(
                answer_id=str(body.get("id", "")),
                question=str(body.get("question", "") or msg_q),
            ))
            return
        if path == "/api/answer-memory/mark-incorrect":
            conv = self.state.conversation_manager.active()
            self._json(am.mark_incorrect(
                answer_id=str(body.get("id", "")),
                question=str(body.get("question", "") or msg_q),
                correction=str(body.get("correction", "")),
                conversation_id=str(conv.get("id") or ""),
                project_id=project_id,
            ))
            return
        if path == "/api/answer-memory/update":
            self._json(am.edit(
                str(body.get("id", "")),
                answer_text=str(body.get("answer_text", "")),
                canonical_question=str(body.get("canonical_question", "")),
                trust_state=str(body.get("trust_state", "")),
                freshness=str(body.get("freshness", "")),
            ))
            return
        if path == "/api/answer-memory/merge":
            self._json(am.merge(str(body.get("from_id", "")), str(body.get("into_id", ""))))
            return
        if path == "/api/answer-memory/refresh":
            self._json(am.refresh(str(body.get("id", ""))))
            return
        if path == "/api/answer-memory/clear":
            self._json(am.clear(str(body.get("scope", ""))))
            return
        if path == "/api/answer-memory/rebuild-index":
            self._json(am.rebuild_index())
            return
        if path == "/api/answer-memory/vacuum":
            if am.store is not None:
                am.store.vacuum()
            self._json({"ok": True})
            return
        if path == "/api/answer-memory/import":
            self._json(am.import_(body if isinstance(body, dict) else {}))
            return
        self._json({"error": "unknown answer-memory endpoint"}, 404)

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
            "response_source": getattr(result, "response_source", ""),
            "memory": getattr(result, "memory", {}),
            "image_jobs": self._agent_image_jobs(result),
            "runtime": self.state.runtime.summary(probe_external=False),
        }

    def _agent_response(self, result) -> None:
        self._json(self._agent_payload(result))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path.startswith("/api/voice/"):
                if self.state.voice is None:
                    self._json({"error": "voice subsystem is disabled"}, 503)
                    return
                self._handle_voice_post(path, body)
                return
            if path.startswith(self._AUTONOMY_PREFIXES):
                if self._autonomy_post(path, body):
                    return
                self._json({"error": "unknown autonomy route"}, 404)
                return
            if path.startswith(self._PLATFORM_PREFIXES):
                if self._platform_post(path, body):
                    return
                self._json({"error": "unknown platform route"}, 404)
                return

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

            if path == "/api/nexus-brain/key-backup":
                self.state.require_brain_creator_session(str(body.get("creator_token", "")))
                bundle = self.state.nexus_brain.export_creator_key_backup(
                    str(body.get("passcode", "")),
                    str(body.get("backup_passcode", "")),
                )
                self._json({"ok": True, "backup": bundle})
                return

            if path == "/api/nexus-brain/key-restore":
                # No creator session required — this is the recovery path for a
                # lost/corrupt auth sidecar, where no session can exist. The
                # restore itself authenticates via backup-passphrase decryption
                # and rejects keys that did not sign this Brain.
                bundle = body.get("backup")
                if not isinstance(bundle, dict):
                    self._json({"error": "backup object is required"}, 400)
                    return
                restored = self.state.nexus_brain.restore_creator_key_backup(
                    bundle,
                    str(body.get("backup_passcode", "")),
                    str(body.get("passcode", "")),
                )
                self._json({"ok": True, "brain": restored})
                return

            if path == "/api/nexus-brain/rollback":
                self.state.require_brain_creator_session(str(body.get("creator_token", "")))
                restored = self.state.nexus_brain.rollback_settings(float(body.get("updated_at") or 0))
                self._json({"ok": True, "brain": restored})
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

            if path == "/api/tuning":
                action = str(body.get("action", "status")).strip().lower()
                if action == "reset":
                    self.state.runtime.tuner.reset(str(body.get("model_id") or "") or None)
                    self._json({"ok": True, "tuning": self.state.runtime.tuner.status()})
                    return
                if action == "mode":
                    mode = str(body.get("mode", "")).strip().lower()
                    if mode not in {"auto", "quiet", "balanced", "max"}:
                        self._json({"error": "mode must be auto|quiet|balanced|max"}, 400)
                        return
                    self.state.config.performance_mode = mode
                    self.state._update_config_file({"performance_mode": mode})
                    self._json({"ok": True, "performance_mode": mode})
                    return
                if action in {"benchmark", "retune"}:
                    model_id = str(body.get("model_id") or "").strip()
                    profiles = {m.id: m for m in self.state.config.models if m.runtime == "llama_cpp"}
                    target = profiles.get(model_id) or next(iter(profiles.values()), None)
                    if target is None:
                        self._json({"error": "no managed llama.cpp model profile configured"}, 400)
                        return
                    job = self.state.jobs.submit(
                        "runtime_tune", f"Benchmark {target.id}", metadata={"model_id": target.id})
                    tuner = self.state.runtime.tuner

                    def _bench(p: ModelProfile = target, jid: str = job.id) -> None:
                        self.state.jobs.update(jid, state="running", status="benchmarking")
                        try:
                            result = tuner.benchmark(p)
                            best = result.get("best")
                            self.state.jobs.update(
                                jid, state="completed", status="finished", progress=1.0,
                                detail=json.dumps(result.get("results", []))[-300:],
                            )
                            self.state.events.publish("model", {"event": {
                                "type": "tuning_complete", "model_id": p.id,
                                "status": result.get("status"),
                                "tps": (best or {}).get("metrics", {}).get("predicted_per_second"),
                            }})
                        except Exception as exc:  # noqa: BLE001
                            self.state.jobs.update(jid, state="failed", error=str(exc)[:300])

                    threading.Thread(target=_bench, name="runtime-tune", daemon=True).start()
                    self._json({"ok": True, "job": job.as_dict()})
                    return
                self._json({"error": "unknown tuning action"}, 400)
                return
            if path == "/api/readiness/configure":
                if body.get("apply") is not True:
                    self._json({"error": "apply=true is required to change the Nexus Core config"}, 400)
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
                    "history": list(row.get("messages", [])),
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
                # Feed the same signal into Answer Memory trust scoring.
                try:
                    am = self.state.answer_memory
                    if am is not None and am.available:
                        rating = str(body.get("rating", ""))
                        q, _a = self.state.exchange_for_message(str(body.get("message_id", "")))
                        conv_id = str(body.get("conversation_id", "")) or str(
                            self.state.conversation_manager.active().get("id") or "")
                        am.apply_feedback(
                            "up" if rating == "up" else "down",
                            conversation_id=conv_id,
                            question=q,
                        )
                except Exception:
                    pass
                self._json({"ok": True, "feedback": saved})
                return

            if path.startswith("/api/answer-memory/"):
                self._answer_memory_post(path, body)
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
                chat_attachments = _clean_attachments(body)
                if not message and not chat_attachments:
                    self._json({"error": "message is required"}, 400)
                    return
                if not message:
                    message = "Please look at the attached file(s)."
                current = self.state.tasks.current()
                if current is not None and current.status in {"running", "verifying", "reviewing", "waiting_approval"}:
                    if not getattr(self.state.config, "chat_queue_when_busy", True):
                        self._json({
                            "error": "A task is already running. Enable chat_queue_when_busy to auto-queue, or wait for it to finish.",
                            "code": "task_busy",
                            "task": current.as_dict(),
                        }, 409)
                        return
                    try:
                        item = self.state.queue.enqueue(
                            _queueable_message(message, chat_attachments), mode=mode)
                    except ValueError as exc:
                        self._json({"error": str(exc)}, 429)
                        return
                    self.state.events.publish("task", {"event": "queued", "queue_item": item})
                    self._sse_begin()
                    self._sse_event("ready", {"mode": mode, "queued": True})
                    self._sse_event("task", {"event": "queued", "queue_item": item})
                    self._sse_event("result", {
                        "content": (
                            f"Queued behind the running task (position {len(self.state.queue)}). "
                            "It starts automatically when the current task finishes."
                        ),
                        "queued": True,
                        "queue_item": item,
                        "task": current.as_dict(),
                        "tool_events": [],
                        "model_events": [{"type": "queued", "queue_item": item}],
                        "pending_approval": None,
                        "verification": [],
                        "steps": 0,
                        "runtime": self.state.runtime.summary(probe_external=False),
                    })
                    return
                # Autonomy chat commands ("make this a mission", "stop
                # autonomy") never need a model — answer directly.
                mission_cmd = self.state._mission_command(message)
                if mission_cmd is not None:
                    payload = self.state._mission_reply_result(mission_cmd["content"])
                    self._sse_begin()
                    self._sse_event("ready", {"mode": mode})
                    self._sse_event("task", {"task": payload["task"]})
                    self._sse_event("result", payload)
                    return
                coding_model_optional = (
                    mode == "auto"
                    and (
                        self.state.agent.can_run_without_coding_model(message)
                        or self.state.agent.has_memory_answer(message)
                    )
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
                self.state._stream_sinks.append(events)
                done = threading.Event()
                started = time.monotonic()
                # Speech context for this response — opened now so prior
                # speech stops immediately and the TTS engine pre-warms while
                # the model thinks. Feeding/finishing is wrapped into the
                # agent callback by _voice_tee / _voice_finish.
                voice_rid = self.state._voice_begin()

                def emit(event: dict) -> None:
                    # Agent/model work may run for a while before the first token.
                    # Queue events back to the request thread so socket writes remain
                    # serialized and the request thread can send idle heartbeats.
                    # Mirror onto the shared bus first — a stalled direct-stream
                    # client must not suppress updates for other viewers.
                    try:
                        self.state._bus_emit(event)
                    except Exception:
                        pass
                    # Token deltas and live-output chunks are drop-safe under
                    # backpressure (a disconnected or stalled client must not
                    # grow memory for the rest of the task); everything else —
                    # task/tool/result/error — always lands.
                    if event.get("type") in {"token", "tool_output"} and events.qsize() > 2000:
                        return
                    events.put(dict(event))

                def run_agent() -> None:
                    try:
                        result = self.state.agent.run(
                            message,
                            history=self.state.history,
                            mode=mode,
                            event_callback=self.state._voice_tee(voice_rid, emit),
                            attachments=chat_attachments,
                        )
                        self.state._voice_finish(voice_rid, result.content)
                        self.state.history = self.state.conversation_manager.history(limit=32)
                        events.put({"type": "result", **self._agent_payload(result)})
                    except Exception as exc:
                        self.state._voice_finish(voice_rid)
                        err = f"{type(exc).__name__}: {exc}"
                        events.put({"type": "error", "error": err})
                        try:
                            current = self.state.tasks.current()
                            payload = {"error": err}
                            if current is not None:
                                payload["task_id"] = current.id
                            self.state.events.publish("error", payload)
                            # _drive_or_error already logs errors raised inside the
                            # drive loop; this covers crashes before a session exists
                            # (e.g. model-selection failure leaves an empty transcript).
                            if current is not None and current.status != "error":
                                self.state.tasks.append_log(
                                    current.id, f"## error: {err[:300]}\n")
                                self.state.tasks.flush_log(current.id)
                        except Exception:
                            pass
                    finally:
                        done.set()
                        try:
                            self.state._dequeue_next()
                        except Exception:
                            pass

                threading.Thread(
                    target=run_agent,
                    name="chat-nexus-agent-stream",
                    daemon=True,
                ).start()

                stream_open = True
                seen_model_id = ""
                seen_model_role = ""
                while stream_open and (not done.is_set() or not events.empty()):
                    try:
                        event = events.get(timeout=1.0)
                    except queue.Empty:
                        current = self.state.tasks.current()
                        heartbeat = {
                            "elapsed_seconds": int(time.monotonic() - started),
                            "phase": current.phase if current else "starting",
                            "status": current.status if current else "starting",
                            "model_id": (current.model_id if current else "") or seen_model_id,
                            "model_role": (current.model_role if current else "") or seen_model_role,
                        }
                        stream_open = self._sse_event("heartbeat", heartbeat)
                        continue

                    # The task ledger may not stamp model_id until the drive
                    # loop's first update — remember the selection event so
                    # heartbeats stop reporting an empty model meanwhile.
                    if event.get("type") == "model":
                        inner = event.get("event")
                        if isinstance(inner, dict):
                            seen_model_id = str(inner.get("model_id") or inner.get("to") or "") or seen_model_id
                            seen_model_role = str(inner.get("role") or "") or seen_model_role

                    event_type = str(event.get("type") or "message")
                    payload = {k: v for k, v in event.items() if k != "type"}
                    stream_open = self._sse_event(event_type, payload)

                # If the client disappeared, the daemon worker continues the durable
                # task to completion; reconnect/status UI can inspect the task ledger.
                try:
                    self.state._stream_sinks.remove(events)
                except ValueError:
                    pass
                self.close_connection = True
                return
            if path == "/api/chat":
                message = str(body.get("message", "")).strip()
                mode = str(body.get("mode", "auto"))
                chat_attachments = _clean_attachments(body)
                if not message and not chat_attachments:
                    self._json({"error": "message is required"}, 400)
                    return
                if not message:
                    message = "Please look at the attached file(s)."
                current = self.state.tasks.current()
                if current is not None and current.status in {"running", "verifying", "reviewing", "waiting_approval"}:
                    if not getattr(self.state.config, "chat_queue_when_busy", True):
                        self._json({
                            "error": "A task is already running. Enable chat_queue_when_busy to auto-queue, or wait for it to finish.",
                            "code": "task_busy",
                            "task": current.as_dict(),
                        }, 409)
                        return
                    try:
                        item = self.state.queue.enqueue(
                            _queueable_message(message, chat_attachments), mode=mode)
                    except ValueError as exc:
                        self._json({"error": str(exc)}, 429)
                        return
                    self.state.events.publish("task", {"event": "queued", "queue_item": item})
                    self._json({
                        "content": (
                            f"Queued behind the running task (position {len(self.state.queue)}). "
                            "It starts automatically when the current task finishes."
                        ),
                        "queued": True,
                        "queue_item": item,
                        "task": current.as_dict(),
                        "tool_events": [],
                        "model_events": [{"type": "queued", "queue_item": item}],
                        "pending_approval": None,
                        "verification": [],
                        "steps": 0,
                        "runtime": self.state.runtime.summary(probe_external=False),
                    })
                    return
                mission_cmd = self.state._mission_command(message)
                if mission_cmd is not None:
                    self._json(self.state._mission_reply_result(mission_cmd["content"]))
                    return
                coding_model_optional = (
                    mode == "auto"
                    and (
                        self.state.agent.can_run_without_coding_model(message)
                        or self.state.agent.has_memory_answer(message)
                    )
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
                voice_rid = self.state._voice_begin()
                try:
                    result = self.state.agent.run(message, history=self.state.history, mode=mode,
                                                  event_callback=self.state._voice_tee(voice_rid, self.state._bus_emit),
                                                  attachments=chat_attachments)
                    self.state._voice_finish(voice_rid, result.content)
                except Exception:
                    self.state._voice_finish(voice_rid)
                    raise
                finally:
                    try:
                        self.state._dequeue_next()
                    except Exception:
                        pass
                self.state.history = self.state.conversation_manager.history(limit=32)
                self._agent_response(result)
                return

            if path == "/api/queue":
                prompt = str(body.get("prompt", "")).strip()
                if not prompt:
                    self._json({"error": "prompt is required"}, 400)
                    return
                try:
                    item = self.state.queue.enqueue(prompt, mode=str(body.get("mode", "auto")))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 429)
                    return
                self.state.events.publish("task", {"event": "queued", "queue_item": item})
                try:
                    self.state._dequeue_next()
                except Exception:
                    pass
                self._json({"ok": True, "item": item})
                return

            if path == "/api/queue/cancel":
                item_id = str(body.get("id", "")).strip()
                removed = self.state.queue.remove(item_id)
                if removed:
                    self.state.events.publish("task", {"event": "queue_item_cancelled", "queue_item": {"id": item_id}})
                self._json({"ok": removed})
                return

            if path == "/api/tasks/resume":
                task_id = str(body.get("task_id", "")).strip()
                approved = bool(body.get("approved", False))
                if not task_id:
                    self._json({"error": "task_id is required"}, 400)
                    return
                voice_rid = self.state._voice_begin()
                try:
                    result = self.state.agent.resume(task_id, approved=approved,
                                                     event_callback=self.state._voice_tee(voice_rid, self.state._bus_emit))
                    self.state._voice_finish(voice_rid, result.content)
                except Exception:
                    self.state._voice_finish(voice_rid)
                    raise
                finally:
                    try:
                        self.state._dequeue_next()
                    except Exception:
                        pass
                if result.task.get("status") not in {"waiting_approval", "running", "verifying", "reviewing"}:
                    self.state.history.append({"role": "assistant", "content": result.content})
                self._agent_response(result)
                return

            if path == "/api/tasks/recover":
                task_id = str(body.get("task_id", "")).strip()
                if not task_id:
                    self._json({"error": "task_id is required"}, 400)
                    return
                voice_rid = self.state._voice_begin()
                try:
                    result = self.state.agent.recover(task_id,
                                                      event_callback=self.state._voice_tee(voice_rid, self.state._bus_emit))
                    self.state._voice_finish(voice_rid, result.content)
                except Exception:
                    self.state._voice_finish(voice_rid)
                    raise
                finally:
                    try:
                        self.state._dequeue_next()
                    except Exception:
                        pass
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
                task = self.state.tasks.update(task_id, reverted=True, status="reverted", phase="done")
                self.state.events.publish("task", {"event": "reverted", "task": task.as_dict()})
                try:
                    self.state.tasks.append_log(task_id, "## task reverted/done\n")
                    self.state.tasks.flush_log(task_id)
                except Exception:
                    pass
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
                        self._json({"error": "External runtimes are not controlled by Nexus Core."}, 400)
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

            if path == "/api/permissions/autonomous":
                enabled = bool(body.get("enabled"))
                self.state.permission_manager.set_autonomous(enabled)
                self.state.config.autonomous_mode = enabled
                self.state._update_config_file({"autonomous_mode": enabled})
                self._json({"ok": True, "autonomous": enabled,
                            "manager": self.state.permission_manager.summary()})
                return

            if path == "/api/permissions/scope":
                permission = str(body.get("permission", "")).strip()
                scope = body.get("scope")
                try:
                    self.state.permission_manager.set_scope(permission, scope)
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self.state._update_config_file({
                    "permission_scopes": dict(self.state.permission_manager.scopes),
                })
                self._json({"ok": True, "permission": permission,
                            "scope": self.state.permission_manager.scope(permission)})
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

            if path == "/api/tools/uninstall":
                tool_id = str(body.get("tool", "")).strip()
                if not tool_id:
                    self._json({"error": "tool is required"}, 400)
                    return
                try:
                    result = self.state.uninstall_tool(
                        tool_id, approve=bool(body.get("approve", False)))
                except KeyError as exc:
                    self._json({"error": str(exc)}, 404)
                    return
                status = 200 if result.get("ok") else (403 if "denied" in str(result.get("error", "")) else 200)
                self._json(result, status)
                return

            if path == "/api/tools/check-updates":
                result = self.state.check_tool_updates(
                    approve=bool(body.get("approve", False)))
                status = 200 if result.get("ok") else (
                    403 if "denied" in str(result.get("error", "")) else 200)
                self._json(result, status)
                return

            if path in {"/api/secrets/set", "/api/secrets/delete"}:
                gate = self.state._permission_gate(
                    "credentials.use", bool(body.get("approve")), path.rsplit("/", 1)[-1])
                if gate is not None:
                    status = 403 if gate.get("error") and not gate.get("needs_approval") else 200
                    self._json(gate, status)
                    return
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
                    self.state.permission_manager.record_event(
                        f"mcp_{action}", "mcp.servers", server_id)
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
                if job_id.startswith("task-"):
                    task_id = job_id[len("task-"):]
                    try:
                        task = self.state.tasks.update(
                            task_id, status="cancelled", phase="done",
                            summary="Cancelled by user.",
                        )
                    except KeyError:
                        self._json({"error": "agent task not found"}, 404)
                        return
                    try:
                        self.state.events.publish("task", {"task": task.as_dict(), "event": "cancelled"})
                    except Exception:
                        pass
                    self._json({"ok": True, "job": task.as_dict()})
                    return
                if job_id.startswith("command-"):
                    task_id = job_id[len("command-"):]
                    # Per-command stop: kill the running foreground subprocess
                    # only — the task continues with a [cancelled] tool result.
                    flags = self.state.agent.tools.context.get("command_cancel") or {}
                    flag = flags.get(task_id)
                    if flag is None:
                        self._json({"error": "no running command for this task"}, 404)
                        return
                    flag.set()
                    self._json({"ok": True, "task_id": task_id})
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
                    job = self.state.jobs.get(job_id)
                except KeyError:
                    self._json({"error": "job not found or not cancellable through the Job Manager"}, 404)
                    return
                if job.kind == "tool_install":
                    self.state.tool_downloads.cancel(job_id)
                    job = self.state.jobs.get(job_id)
                else:
                    job = self.state.jobs.cancel(job_id)
                # A cancelled background terminal job must actually kill the
                # process, not just update the ledger row.
                if job.kind == "terminal":
                    try:
                        self.state.terminal_tracker.kill(job.id)
                    except Exception:
                        pass
                self._json({"ok": True, "job": job.as_dict()})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except KeyError as exc:
            self._json({"error": f"Not found: {exc}"}, 404)
        except Exception as exc:
            # Transport-classified failures carry a friendly message and a
            # structured diagnostic — keep the raw exception off the chat
            # surface while preserving it for diagnostics.
            diag_fn = getattr(exc, "diagnostic", None)
            friendly = getattr(exc, "friendly", "")
            payload: dict[str, Any] = {
                "error": friendly or f"{type(exc).__name__}: {exc}"
            }
            if callable(diag_fn):
                payload["technical"] = f"{type(exc).__name__}: {exc}"
                payload["diagnostic"] = diag_fn()
            self._json(payload, 500)

    def log_message(self, format: str, *args) -> None:
        pass


class _NexusHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't traceback-log client disconnects.

    The default handle_error prints a full exception for every request
    thread failure — including BrokenPipe/ConnectionReset/Aborted when a
    browser tab or SSE stream goes away mid-write. Those are normal client
    behavior, not server faults; logging them floods the backend log over
    long unattended runs. Real errors still go through the default path.
    """

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def create_server(
    config: AgentConfig,
    workspace: Path,
    host: str,
    port: int,
    web_root: Path,
    runtime_root: Path,
    config_path: Path | None = None,
    boot: Callable[[float, str, str], None] | None = None,
) -> tuple[ThreadingHTTPServer, AppState]:
    state = AppState(config, workspace, runtime_root, config_path=config_path, boot=boot)
    handler = type("ChatNexusHandler", (Handler,), {"state": state, "web_root": web_root})
    server = _NexusHTTPServer((host, port), handler)
    return server, state


def stop_state(state: AppState) -> None:
    try:
        state.mcp.shutdown()
    except Exception:
        pass
    try:
        state.terminal_tracker.shutdown()
    except Exception:
        pass
    try:
        if getattr(state, "answer_memory", None) is not None:
            state.answer_memory.close()
    except Exception:
        pass
    try:
        state.images.backend_runtime.stop()
    finally:
        state.runtime.stop_all()
    # Platform services holding OS handles/subprocesses.
    try:
        if getattr(state, "_lsp_pool", None) is not None:
            state._lsp_pool.shutdown_all()
    except Exception:
        pass
    for obj_name in ("_rag_obj", "_knowledge_obj"):
        try:
            obj = getattr(state, obj_name, None)
            if obj is not None:
                obj.close()
        except Exception:
            pass
    try:
        if getattr(state, "autonomy", None) is not None:
            state.autonomy.stop()
    except Exception:
        pass


def serve(config: AgentConfig, workspace: Path, host: str, port: int, web_root: Path, runtime_root: Path, config_path: Path | None = None) -> None:
    from .boot import boot_report, reporter_from_env
    boot = reporter_from_env()
    if boot is not None:
        boot(2, "STARTING · CORE SERVICES", "Launching Nexus Core backend services")
    server, state = create_server(config, workspace, host, port, web_root, runtime_root, config_path=config_path, boot=boot)
    actual_port = int(server.server_address[1])
    if boot is not None:
        boot_report(98, "STARTING · CORE SERVICES", "Backend interface online — synchronizing runtime state")
    print(f"Nexus Core v{VERSION}")
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
