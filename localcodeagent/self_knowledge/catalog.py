"""Canonical Nexus Core feature catalog.

Every feature Nexus knows about is registered here once — its meaning,
its development status, where it lives in the UI, and how its *runtime*
state is resolved. Runtime state is never duplicated: a feature declares
a ``capability_id`` (probed by the Capability Registry) and/or a
``probe`` key resolved through the injected env — the same pattern as
``capabilities.py``, so catalog and probes stay decoupled from AppState
and cheap to test.

Two different truths are kept strictly separate:

    development_status — what the code supports:
        implemented | partial | experimental | planned |
        not_implemented | deprecated

    runtime_state — what works right now, from the Capability Registry:
        verified | available | degraded | setup_required |
        unauthorized | unavailable | broken | experimental
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

DEV_STATUSES = (
    "implemented", "partial", "experimental", "planned",
    "not_implemented", "deprecated",
)

# Categories surfaced when Nexus summarizes what she can do.
FEATURE_CATEGORIES = (
    "development", "research", "images", "models", "git",
    "automation", "memory", "voice", "interface", "system", "connectors",
)


@dataclass(slots=True)
class FeatureSpec:
    """One user-meaningful Nexus Core feature."""
    id: str
    name: str
    category: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    # What the code supports — independent of whether it works right now.
    development_status: str = "implemented"
    # Runtime truth sources: a Capability Registry id and/or a probe key
    # resolved through the injected env (probe(name, env) -> dict).
    capability_id: str = ""
    probe: str = ""
    ui_route: str = ""           # canonical deep link, e.g. /settings.html#voice
    ui_section: str = ""
    tool_ids: tuple[str, ...] = ()
    connector_ids: tuple[str, ...] = ()
    settings_keys: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    supports_chat_control: bool = False
    requires_restart: bool = False
    introduced_version: str = ""
    deprecated: bool = False
    replacement: str = ""
    documentation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "description": self.description,
            "aliases": list(self.aliases), "keywords": list(self.keywords),
            "examples": list(self.examples),
            "development_status": self.development_status,
            "capability_id": self.capability_id, "probe": self.probe,
            "ui_route": self.ui_route, "ui_section": self.ui_section,
            "tool_ids": list(self.tool_ids),
            "connector_ids": list(self.connector_ids),
            "settings_keys": list(self.settings_keys),
            "action_ids": list(self.action_ids),
            "permissions": list(self.permissions),
            "limitations": list(self.limitations),
            "supports_chat_control": self.supports_chat_control,
            "requires_restart": self.requires_restart,
            "introduced_version": self.introduced_version,
            "deprecated": self.deprecated, "replacement": self.replacement,
        }


# ---------------------------------------------------------------------------
# The inventory. One entry per meaningful user-facing feature — the
# answers to "what can you do" are generated from this list, so entries
# must name real subsystems, never aspirations.
# ---------------------------------------------------------------------------

FEATURES: list[FeatureSpec] = [
    # -- Development ------------------------------------------------------
    FeatureSpec(
        id="chat", name="Chat", category="interface",
        description=("The primary natural-language interface to Nexus "
                     "Core — ask questions, issue work, and control "
                     "ordinary settings from conversation."),
        aliases=("chat", "talk", "conversation"),
        ui_route="/index.html",
        introduced_version="0.1.0"),
    FeatureSpec(
        id="code_editing", name="Code editing", category="development",
        description=("Inspecting, creating, and modifying files in the "
                     "workspace — patches, refactors, and new code."),
        aliases=("edit code", "code editing", "editing files",
                 "write files", "file editing", "edit files",
                 "edit a file", "change files", "modify files"),
        capability_id="code_editing",
        tool_ids=("apply_patch", "write_file", "read_file", "list_files"),
        permissions=("filesystem.write",),
        ui_route="/workspace.html",
        supports_chat_control=True,
        examples=("edit a file", "refactor this module")),
    FeatureSpec(
        id="terminal", name="Terminal commands", category="development",
        description=("Running shell commands in the workspace — builds, "
                     "scripts, and diagnostics behind permission gates."),
        aliases=("terminal", "shell", "run commands", "powershell",
                 "command line"),
        capability_id="terminal",
        tool_ids=("run_shell", "terminal_run"),
        permissions=("shell.execute",),
        supports_chat_control=True,
        examples=("run the tests", "what does git status say")),
    FeatureSpec(
        id="testing", name="Testing", category="development",
        description=("Running a project's test suite and reporting "
                     "results against the workspace."),
        aliases=("tests", "test suite", "run tests", "unit tests"),
        capability_id="testing",
        tool_ids=("run_tests",),
        examples=("run the tests", "does the suite pass")),
    FeatureSpec(
        id="build_toolchain", name="Build & compilation",
        category="development",
        description=("Detecting and driving build systems — Python, "
                     "Node, .NET, CMake, and friends."),
        aliases=("build", "compile", "compilation", "toolchain"),
        capability_id="compilation",
        tool_ids=("build_project", "detect_build_system"),
        ui_route="/command.html"),
    FeatureSpec(
        id="debugger", name="Debugger", category="development",
        description=("Launching programs under a debugger and stepping "
                     "through failures."),
        aliases=("debug", "debugger", "breakpoint"),
        tool_ids=("debug_run",),
        development_status="partial",
        limitations=("Debugger coverage is per-language; Python is the "
                     "primary target.",),
        ui_route="/command.html"),
    FeatureSpec(
        id="code_intel", name="Code intelligence", category="development",
        description=("Symbol search, references, impact analysis, and "
                     "repo indexing — code-aware navigation."),
        aliases=("symbols", "references", "code search", "code intel",
                 "lsp", "go to definition"),
        tool_ids=("code_map", "code_symbols", "code_references",
                  "code_impact", "search_repo_index", "rebuild_repo_index"),
        ui_route="/workspace.html"),
    FeatureSpec(
        id="dev_servers", name="Dev servers", category="development",
        description=("Starting, stopping, and inspecting project dev "
                     "servers and their logs."),
        aliases=("dev server", "dev servers", "web server",
                 "preview server"),
        tool_ids=("dev_server_list", "dev_server_start", "dev_server_stop",
                  "dev_server_restart", "dev_server_logs"),
        development_status="partial",
        ui_route="/command.html"),
    FeatureSpec(
        id="project_scaffold", name="Project scaffolding",
        category="development",
        description=("Creating new project structures and configuring "
                     "existing ones."),
        aliases=("scaffold", "new project", "project setup"),
        tool_ids=("project_scaffold", "project_setup",
                  "project_templates", "project_audit", "configure_project"),
        ui_route="/projects.html"),
    # -- Research -----------------------------------------------------------
    FeatureSpec(
        id="web_search", name="Web search & research", category="research",
        description=("Searching the web and synthesizing cited research "
                     "reports — technical and general knowledge."),
        aliases=("search", "web search", "research", "look up",
                 "search the web", "internet"),
        capability_id="", probe="research",
        tool_ids=("web_search", "research_topic", "search_documentation",
                  "search_error", "fetch_url", "summarize_research"),
        settings_keys=("research_enabled", "research_mode"),
        ui_route="/research.html",
        supports_chat_control=True,
        examples=("research WebRTC", "look up the docs")),
    FeatureSpec(
        id="knowledge", name="Knowledge memory", category="memory",
        description=("Sourced knowledge with provenance and TTL — "
                     "research results Nexus can recall later."),
        aliases=("knowledge", "knowledge memory", "what you know"),
        probe="knowledge_memory",
        tool_ids=("knowledge_search", "knowledge_index",
                  "knowledge_forget"),
        ui_route="/knowledge.html"),
    FeatureSpec(
        id="browser", name="Browser automation", category="research",
        description=("Driving a real browser — previewing pages, "
                     "clicking through flows, verifying web work."),
        aliases=("browser", "browse", "playwright", "web browser",
                 "open the page"),
        capability_id="browser_preview",
        tool_ids=("browser_run", "browser_verify"),
        permissions=("browser.control",),
        supports_chat_control=True),
    FeatureSpec(
        id="computer_use", name="Computer use", category="interface",
        description=("Seeing and driving the desktop — screenshots, "
                     "clicks, and keystrokes."),
        aliases=("computer use", "desktop control", "control my pc",
                 "click", "screenshot"),
        capability_id="desktop_control",
        tool_ids=("computer_screenshot", "computer_click",
                  "computer_type", "computer_windows"),
        permissions=("browser.control",),
        development_status="experimental",
        limitations=("Experimental — desktop automation is limited to "
                     "basic input and inspection.",)),
    # -- Images & vision ----------------------------------------------------
    FeatureSpec(
        id="image_generation", name="Image generation", category="images",
        description=("Generating and editing images locally through "
                     "InvokeAI or ComfyUI backends."),
        aliases=("image", "images", "image generation", "make images",
                 "generate images", "pictures", "art", "draw"),
        capability_id="image_generation",
        tool_ids=("list_image_models", "install_image_model",
                  "verify_image_models", "list_loras"),
        settings_keys=("image_enabled", "image_backend"),
        ui_route="/image.html",
        supports_chat_control=True,
        action_ids=("image.backend.set", "image.backend.install",
                    "image.backend.start", "image.backend.stop"),
        examples=("draw a sunset", "use comfyui")),
    FeatureSpec(
        id="image_library", name="Image library", category="images",
        description=("The generated-image library — browsing, reusing, "
                     "and organizing outputs."),
        aliases=("image library", "gallery", "my images"),
        ui_route="/image.html#library"),
    FeatureSpec(
        id="vision", name="Vision", category="images",
        description=("Understanding images you share — description, "
                     "visual Q&A, and reasoning over photos."),
        aliases=("vision", "see images", "image understanding",
                 "look at this", "ocr"),
        probe="vision",
        tool_ids=("ocr_image", "create_thumbnail", "media_probe"),
        ui_route="/models.html"),
    # -- Models ---------------------------------------------------------------
    FeatureSpec(
        id="model_management", name="Model management", category="models",
        description=("Installing, loading, unloading, and switching "
                     "local models — llama.cpp runtimes are managed "
                     "per-role."),
        aliases=("models", "model manager", "model management",
                 "load model", "switch model"),
        probe="models",
        settings_keys=("max_resident_models", "performance_mode"),
        ui_route="/models.html",
        supports_chat_control=True,
        action_ids=("model.provision", "model.set_performance"),
        examples=("what models are installed", "load the 14b")),
    FeatureSpec(
        id="model_routing", name="Model routing", category="models",
        description=("Role-based routing — fast general, coding, deep "
                     "reasoning, and vision lanes pick the right model "
                     "per request."),
        aliases=("routing", "model routing", "which model"),
        probe="models",
        ui_route="/models.html#routing"),
    FeatureSpec(
        id="model_growth", name="Model Growth Lab", category="models",
        description=("Reviewing accumulated experience and exporting "
                     "training datasets for offline fine-tuning."),
        aliases=("model growth", "training", "fine tune", "trainer"),
        probe="model_growth",
        ui_route="/trainer.html",
        development_status="partial",
        limitations=("Training itself is offline/reviewed — Nexus "
                     "collects and packages experience, she doesn't "
                     "retune weights in the background.",)),
    FeatureSpec(
        id="provisioning", name="Provisioning", category="models",
        description=("Background installation of components — models, "
                     "voice assets, image backends — after first launch."),
        aliases=("provisioning", "downloads", "installs", "setup"),
        probe="provisioning",
        settings_keys=("provisioning_enabled",),
        ui_route="/settings.html#setup",
        supports_chat_control=True,
        action_ids=("provisioning.pause", "provisioning.resume")),
    # -- Git & GitHub ---------------------------------------------------------
    FeatureSpec(
        id="git", name="Git", category="git",
        description=("Local version control — status, diff, branches, "
                     "commits, and worktrees behind permission gates."),
        aliases=("git", "commit", "branch", "version control"),
        capability_id="git",
        tool_ids=("git_status", "git_diff", "git_commit",
                  "git_create_branch", "git_switch", "git_log",
                  "git_stash", "git_revert", "git_pull", "git_push"),
        permissions=("git.execute",),
        supports_chat_control=True),
    FeatureSpec(
        id="github", name="GitHub", category="git",
        description=("Remote repository work — push, pull requests, "
                     "issues, CI inspection — through a connected "
                     "GitHub account."),
        aliases=("github", "pull request", "pr", "ci"),
        capability_id="github",
        connector_ids=("github",),
        tool_ids=("github_connect", "github_disconnect",
                  "github_auth_status", "github_list_repos",
                  "github_repository", "github_repo_activity",
                  "github_create_pull_request",
                  "github_create_issue", "github_ci_status",
                  "github_actions_run", "github_checkout_pr"),
        permissions=("github.read", "github.write"),
        settings_keys=("github_enabled",),
        ui_route="/settings.html#connections",
        supports_chat_control=True,
        action_ids=("github.connect", "github.disconnect",
                    "github.test"),
        examples=("is github connected", "open a pull request")),
    # -- Automation -----------------------------------------------------------
    FeatureSpec(
        id="missions", name="Missions", category="automation",
        description=("Persistent autonomous missions — long-running "
                     "goals that survive restarts and resume where they "
                     "left off."),
        aliases=("mission", "missions", "autonomous task"),
        probe="missions",
        ui_route="/missions.html"),
    FeatureSpec(
        id="autonomy", name="Autonomy supervisor", category="automation",
        description=("The mission supervisor — triggers, schedules, "
                     "standing goals, and background work policy."),
        aliases=("autonomy", "autonomous mode", "background work",
                 "supervisor"),
        probe="autonomy",
        settings_keys=("autonomy_enabled", "autonomous_mode"),
        ui_route="/command.html#autonomy",
        supports_chat_control=True,
        action_ids=("autonomy.pause", "autonomy.resume",
                    "autonomy.stop")),
    FeatureSpec(
        id="workers", name="Worker pool", category="automation",
        description=("Adaptive workers that pick up queued work — "
                     "hardware-aware, bounded by the worker ceiling."),
        aliases=("workers", "worker", "worker pool", "concurrency"),
        probe="workers",
        settings_keys=("worker_ceiling",),
        ui_route="/command.html#workers",
        supports_chat_control=True,
        action_ids=("workers.set_ceiling",)),
    FeatureSpec(
        id="queue", name="Work queue", category="automation",
        description=("The durable task queue — requests submitted while "
                     "busy land here and run in order."),
        aliases=("queue", "task queue", "queued work"),
        probe="queue",
        tool_ids=("queue_task", "queue_list", "queue_cancel"),
        ui_route="/command.html#queue"),
    FeatureSpec(
        id="scheduling", name="Scheduling & triggers", category="automation",
        description=("Time-based schedules and event triggers that fire "
                     "missions without user prompts."),
        aliases=("schedule", "schedules", "triggers", "cron"),
        probe="autonomy",
        ui_route="/missions.html"),
    FeatureSpec(
        id="notifications", name="Notifications", category="interface",
        description=("Status and provisioning notifications — spoken "
                     "and shown in the interface."),
        aliases=("notifications", "alerts", "notify me"),
        settings_keys=("provisioning_voice_notifications",
                       "autonomy_quiet_hours"),
        ui_route="/settings.html#notifications",
        supports_chat_control=True,
        action_ids=("notifications.set_voice",)),
    # -- Memory & knowledge ---------------------------------------------------
    FeatureSpec(
        id="nexus_brain", name="Nexus Brain", category="memory",
        description=("Persistent model-independent memory — goals, "
                     "facts, preferences, and patterns that survive "
                     "model replacement."),
        aliases=("brain", "nexus brain", "your memory",
                 "long term memory"),
        probe="nexus_brain",
        settings_keys=("nexus_brain_enabled",),
        ui_route="/knowledge.html#brain"),
    FeatureSpec(
        id="answer_memory", name="Answer Memory", category="memory",
        description=("Trusted learned answers that skip model inference "
                     "entirely — fast, consistent replies to questions "
                     "already answered."),
        aliases=("answer memory", "learned answers", "memory answers"),
        probe="answer_memory",
        settings_keys=("answer_memory_enabled",),
        ui_route="/answers.html"),
    FeatureSpec(
        id="conversation_memory", name="Conversation memory",
        category="memory",
        description=("Remembering facts, preferences, and patterns "
                     "across conversations."),
        aliases=("conversation memory", "remember", "your memory of me"),
        probe="conversation_memory",
        settings_keys=("conversation_memory_enabled",),
        ui_route="/knowledge.html"),
    FeatureSpec(
        id="persona", name="Persona & personality", category="voice",
        description=("Nexus's personality — presets, custom personas, "
                     "moods, and how strongly the persona colors "
                     "replies."),
        aliases=("persona", "personality", "mood", "be yourself",
                 "act like"),
        probe="persona",
        ui_route="/personality.html",
        supports_chat_control=True,
        action_ids=("persona.set_active", "persona.set_strength")),
    FeatureSpec(
        id="speech_lab", name="Speech Lab", category="voice",
        description=("The Persona Speech Genome — how Nexus phrases, "
                     "paces, and colors what she says."),
        aliases=("speech lab", "speech genome", "how you talk",
                 "speech style", "voice style"),
        ui_route="/personality.html#speech-lab"),
    # -- Voice ----------------------------------------------------------------
    FeatureSpec(
        id="voice", name="Voice output", category="voice",
        description=("Spoken replies through the local Kokoro TTS "
                     "engine — presets, volume, and pacing."),
        aliases=("voice", "speak", "talk to me", "tts", "audio",
                 "out loud"),
        capability_id="tts",
        settings_keys=("voice_enabled", "voice_muted", "voice_mode",
                       "voice_preset_id", "voice_volume", "voice_speed"),
        ui_route="/settings.html#voice",
        supports_chat_control=True,
        action_ids=("voice.enable", "voice.disable", "voice.mute",
                    "voice.unmute", "voice.set_preset",
                    "voice.set_volume"),
        examples=("turn voice off", "use isabella")),
    FeatureSpec(
        id="stt", name="Speech input", category="voice",
        description=("Speech-to-text — push-to-talk dictation through "
                     "faster-whisper or a streaming Vosk model."),
        aliases=("speech to text", "stt", "dictation", "microphone",
                 "transcribe", "voice input"),
        capability_id="stt",
        settings_keys=("stt_backend", "stt_model"),
        ui_route="/settings.html#voice",
        supports_chat_control=True),
    FeatureSpec(
        id="startup_narration", name="Startup narration", category="voice",
        description=("The startup cinematic and spoken boot narration — "
                     "and the persona farewell when the app closes."),
        aliases=("startup narration", "startup voice", "splash",
                 "farewell", "goodbye"),
        settings_keys=("startup_narration", "silent_startup",
                       "splash_audio_enabled"),
        ui_route="/settings.html#general"),
    # -- Projects & workspaces --------------------------------------------------
    FeatureSpec(
        id="projects", name="Projects", category="development",
        description=("Named projects with their own workspaces, memory, "
                     "and history."),
        aliases=("projects", "project", "my projects"),
        probe="projects",
        ui_route="/projects.html"),
    FeatureSpec(
        id="workspaces", name="Workspaces", category="development",
        description=("The working directories Nexus operates in — "
                     "inspect, open, and switch."),
        aliases=("workspace", "workspaces", "folder", "directory"),
        capability_id="filesystem",
        tool_ids=("workspace_list", "workspace_open",
                  "workspace_inspect", "workspace_active"),
        ui_route="/workspace.html",
        supports_chat_control=True),
    FeatureSpec(
        id="worktrees", name="Repair worktrees", category="development",
        description=("Isolated git worktrees used for repair and "
                     "canary work so fixes never touch the live tree."),
        aliases=("worktree", "worktrees", "repair worktree"),
        tool_ids=("git_worktree_add", "git_worktree_list",
                  "git_worktree_remove"),
        development_status="partial"),
    # -- System -----------------------------------------------------------------
    FeatureSpec(
        id="secrets", name="Secrets vault", category="system",
        description=("Encrypted credential storage — tokens and keys "
                     "live in the vault, never in chat history or "
                     "logs."),
        aliases=("secrets", "vault", "credentials", "api keys",
                 "tokens"),
        ui_route="/settings.html#connections"),
    FeatureSpec(
        id="permissions", name="Permissions", category="system",
        description=("The permission system — allow/session/ask/creator/"
                     "deny levels and scopes every tool call passes "
                     "through."),
        aliases=("permissions", "permission", "authorization",
                 "approvals"),
        ui_route="/settings.html#permissions"),
    FeatureSpec(
        id="health", name="Health & diagnostics", category="system",
        description=("Subsystem health, crash history, network "
                     "diagnostics, and readiness reports."),
        aliases=("health", "diagnostics", "status", "what's wrong"),
        probe="health",
        ui_route="/system.html"),
    FeatureSpec(
        id="safe_mode", name="Safe Mode", category="system",
        description=("A reduced-functionality mode after repeated "
                     "unclean boots — disables heavy subsystems while "
                     "keeping diagnostics and repair reachable."),
        aliases=("safe mode", "recovery mode"),
        probe="safe_mode",
        ui_route="/system.html",
        action_ids=("safe_mode.exit",)),
    FeatureSpec(
        id="lkg", name="Last-known-good & rollback", category="system",
        description=("Verified snapshots of the backend + config that "
                     "the host restores after repeated boot failures."),
        aliases=("rollback", "last known good", "lkg",
                 "restore backup"),
        probe="lkg",
        ui_route="/system.html",
        limitations=("Rollback is host-side — it applies at the next "
                     "launch, never to a running instance.",)),
    FeatureSpec(
        id="self_update", name="Self update", category="system",
        description=("Checking for and staging backend updates — the "
                     "host swaps them in at next launch."),
        aliases=("update", "self update", "upgrade"),
        probe="self_update",
        development_status="partial",
        ui_route="/settings.html#advanced"),
    FeatureSpec(
        id="self_repair", name="Self repair", category="system",
        description=("Detecting failures, forming repair hypotheses, "
                     "and applying verified fixes in isolated "
                     "worktrees."),
        aliases=("self repair", "fix yourself", "repair"),
        probe="self_repair",
        settings_keys=("self_repair_auto_promote",),
        ui_route="/system.html"),
    FeatureSpec(
        id="backups", name="Backups", category="system",
        description=("State backups for recovery and migration."),
        aliases=("backup", "backups", "export state"),
        probe="backups",
        ui_route="/settings.html#advanced"),
    FeatureSpec(
        id="evaluation", name="Evaluation & benchmarks", category="system",
        description=("Regression evals, benchmarks, and golden-test "
                     "runs that measure how Nexus is doing."),
        aliases=("benchmarks", "eval", "evaluation", "golden tests"),
        ui_route="/system.html",
        development_status="partial"),
    FeatureSpec(
        id="digital_twin", name="Digital Twin", category="system",
        description=("A simulated model of the workstation used for "
                     "calibration and what-if diagnostics."),
        aliases=("digital twin", "twin", "simulation"),
        ui_route="/system.html",
        development_status="experimental"),
    FeatureSpec(
        id="temp_specialists", name="Temp specialists", category="system",
        description=("Short-lived specialist configurations spun up "
                     "for a task and discarded."),
        aliases=("temp specialists", "specialist", "sub agent"),
        development_status="partial",
        ui_route="/command.html"),
    FeatureSpec(
        id="connectors", name="Connectors", category="connectors",
        description=("First-class external service integrations — "
                     "authenticated, permission-gated, and audited."),
        aliases=("connectors", "integrations", "services",
                 "connect service"),
        probe="connectors",
        ui_route="/settings.html#connections",
        supports_chat_control=True),
    FeatureSpec(
        id="plugins", name="Plugins & tool manifests",
        category="connectors",
        description=("Installable tool manifests and MCP servers that "
                     "extend what Nexus can do."),
        aliases=("plugins", "tools", "mcp", "extensions",
                 "tool manager"),
        probe="tools",
        ui_route="/tools.html",
        supports_chat_control=True),
    FeatureSpec(
        id="skills", name="Skills", category="system",
        description=("Packaged skill definitions Nexus can invoke — "
                     "documented procedures for specific jobs."),
        aliases=("skills", "skill"),
        ui_route="/settings.html#advanced"),
    FeatureSpec(
        id="command_center", name="Command Center", category="interface",
        description=("The operations page — workers, queue, autonomy, "
                     "and runtime controls in one place."),
        aliases=("command center", "command", "operations"),
        ui_route="/command.html"),
    # -- Interface ---------------------------------------------------------------
    FeatureSpec(
        id="profile", name="Profiles", category="interface",
        description=("User profiles — who Nexus is talking to, "
                     "including the creator-locked identity."),
        aliases=("profile", "profiles", "account", "who am i"),
        ui_route="/settings.html#profile"),
    FeatureSpec(
        id="appearance", name="Appearance", category="interface",
        description=("Interface preferences — the Nexus UI theme and "
                     "presentation options."),
        aliases=("dark mode", "theme", "appearance", "light mode"),
        probe="appearance",
        settings_keys=("ui_theme",),
        ui_route="/settings.html#appearance",
        supports_chat_control=True),
    FeatureSpec(
        id="activity", name="Activity log", category="interface",
        description=("The running record of what Nexus has done — "
                     "tool calls, installs, decisions."),
        aliases=("activity", "log", "history", "what did you do"),
        ui_route="/command.html#activity"),
    FeatureSpec(
        id="mcp", name="MCP servers", category="connectors",
        description=("Model Context Protocol servers — external tools "
                     "and data sources wired into Nexus."),
        aliases=("mcp", "mcp servers"),
        settings_keys=("mcp_servers",),
        ui_route="/tools.html#mcp"),
    FeatureSpec(
        id="deployment", name="Deployment", category="development",
        description=("Packaging and deploying releases — adapters are "
                     "experimental."),
        aliases=("deploy", "deployment", "release", "publish"),
        capability_id="deployment",
        tool_ids=("deploy_release", "deploy_static",
                  "package_release", "release_verify"),
        development_status="experimental",
        limitations=("Deploy adapters are experimental — verify "
                     "releases manually.",)),
    FeatureSpec(
        id="dependency_audit", name="Dependency audit",
        category="development",
        description=("Checking project dependencies for known issues "
                     "and freshness."),
        aliases=("dependencies", "dependency audit", "outdated packages"),
        tool_ids=("dep_list", "check_package_version"),
        development_status="partial"),
    FeatureSpec(
        id="coverage", name="Coverage", category="development",
        description=("Test coverage reporting for the project."),
        aliases=("coverage", "test coverage"),
        tool_ids=("coverage_report",),
        development_status="partial"),
    FeatureSpec(
        id="sandbox", name="Sandboxed execution", category="development",
        description=("Running untrusted code or commands in a sandbox."),
        aliases=("sandbox", "sandboxed run"),
        tool_ids=("sandbox_run",),
        development_status="experimental"),
    FeatureSpec(
        id="media_tools", name="Media & documents", category="research",
        description=("Converting, transcribing, and extracting from "
                     "documents, audio, and video."),
        aliases=("documents", "video", "audio tools", "convert",
                 "transcribe video"),
        tool_ids=("convert_document", "convert_video", "extract_text",
                  "extract_audio", "media_transcribe", "trim_video",
                  "normalize_audio", "add_subtitles"),
        development_status="partial"),
    FeatureSpec(
        id="docker", name="Docker", category="development",
        description=("Docker images and containers where docker is "
                     "installed."),
        aliases=("docker", "containers"),
        tool_ids=("docker_images", "docker_run"),
        development_status="experimental"),
    FeatureSpec(
        id="causal_memory", name="Causal memory", category="memory",
        description=("Cause-and-effect memory — why decisions were "
                     "made, linked to their outcomes."),
        aliases=("causal memory", "why did you"),
        ui_route="/knowledge.html"),
    FeatureSpec(
        id="environment", name="Environment & toolchains",
        category="system",
        description=("Detected toolchains and runtime environment — "
                     "Python, Node, .NET, compilers."),
        aliases=("environment", "toolchains", "what's installed"),
        capability_id="compilation",
        ui_route="/system.html"),
]


class FeatureCatalog:
    """The canonical inventory — static metadata plus live state.

    Runtime truth comes from the Capability Registry (``capability_id``)
    and probe keys resolved through the injected env. The catalog itself
    is stateless metadata; ``feature_state`` composes the two.
    """

    def __init__(self, features: list[FeatureSpec] | None = None) -> None:
        self._features = {f.id: f for f in (features or FEATURES)}
        self._by_alias: dict[str, str] = {}
        for f in self._features.values():
            names = {f.id, f.name.lower(), *(a.lower() for a in f.aliases),
                     *(k.lower() for k in f.keywords)}
            for n in names:
                if n:
                    self._by_alias.setdefault(n, f.id)

    # -- lookups ---------------------------------------------------------

    def get(self, feature_id: str) -> FeatureSpec | None:
        return self._features.get(feature_id)

    def all(self) -> list[FeatureSpec]:
        return list(self._features.values())

    def by_category(self, category: str) -> list[FeatureSpec]:
        return [f for f in self._features.values()
                if f.category == category]

    def find(self, text: str) -> FeatureSpec | None:
        """Alias/keyword resolution — longest match wins so
        'image backend' beats 'image'."""
        t = " " + " ".join(str(text or "").lower().split()) + " "
        best: tuple[int, str] | None = None
        for alias, fid in self._by_alias.items():
            needle = f" {alias} "
            if needle in t or t.strip() == alias:
                score = len(alias)
                if best is None or score > best[0]:
                    best = (score, fid)
        return self._features.get(best[1]) if best else None

    def find_all(self, text: str) -> list[FeatureSpec]:
        t = " " + " ".join(str(text or "").lower().split()) + " "
        seen: list[FeatureSpec] = []
        for alias, fid in self._by_alias.items():
            if f" {alias} " in t and fid in self._features:
                f = self._features[fid]
                if f not in seen:
                    seen.append(f)
        return seen

    # -- runtime state composition ----------------------------------------

    def feature_state(self, feature_id: str, env: dict[str, Any]
                      ) -> dict[str, Any]:
        """Compose development status with live runtime state.

        env callables (injected by the caller — same pattern as the
        Capability Registry):
            capability(id, force=False) -> CapabilityReport | None
            probe(name) -> dict   (custom subsystem probes)
        """
        f = self._features.get(feature_id)
        if f is None:
            return {"id": feature_id, "error": "unknown feature"}
        out = {"id": f.id, "name": f.name, "category": f.category,
               "development_status": f.development_status,
               "ui_route": f.ui_route,
               "limitations": list(f.limitations),
               "supports_chat_control": f.supports_chat_control}
        runtime = "not_applicable"
        detail = ""
        disposition = ""
        if f.capability_id:
            fn = env.get("capability")
            rep = fn(f.capability_id) if callable(fn) else None
            if rep is not None:
                runtime = getattr(rep, "state", "") or runtime
                detail = getattr(rep, "detail", "") or ""
                disposition = getattr(rep, "disposition", "") or ""
        if f.probe:
            fn = env.get("probe")
            try:
                p = fn(f.probe) if callable(fn) else None
            except Exception:
                p = None
            if isinstance(p, dict):
                runtime = str(p.get("state") or runtime)
                detail = str(p.get("detail") or detail)
        # Normalize the Capability Registry's vocabulary into the
        # catalog's — 'unauthorized' (probe term) reads as
        # 'authorization_required' (user-facing term).
        runtime = {"unauthorized": "authorization_required"}.get(
            runtime, runtime)
        out["runtime_state"] = runtime
        out["runtime_detail"] = detail
        if disposition:
            out["disposition"] = disposition
        return out

    def summary(self, env: dict[str, Any]) -> dict[str, Any]:
        states = [self.feature_state(f.id, env) for f in self.all()]
        return {
            "time": time.time(),
            "features": states,
            "by_category": {c: [s["id"] for s in states
                                if s["category"] == c]
                            for c in FEATURE_CATEGORIES},
            "counts": {
                "total": len(states),
                "implemented": sum(
                    1 for s in states
                    if s["development_status"] == "implemented"),
                "ready": sum(1 for s in states if s["runtime_state"]
                             in ("verified", "available", "running")),
                "needs_setup": sum(1 for s in states
                                   if s["runtime_state"] == "setup_required"),
                "broken": sum(1 for s in states
                              if s["runtime_state"] in ("broken", "unavailable")),
            },
        }
