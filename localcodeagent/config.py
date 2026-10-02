from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .image.types import ImageModelProfile


@dataclass(slots=True)
class ModelProfile:
    id: str
    endpoint: str
    model: str
    roles: list[str]
    context_window: int = 32768
    max_output_tokens: int = 4096
    temperature: float = 0.2
    tool_calling: bool = True
    vision: bool = False
    priority: int = 50
    enabled: bool = True
    api_key: str = "local"
    notes: str = ""

    # v0.2 runtime lifecycle fields.
    runtime: str = "external"  # external | llama_cpp
    model_path: str = ""
    executable: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    gpu_layers: str = "auto"
    threads: int = 0
    fit_target_mb: int = 1024
    startup_timeout: int = 180
    keep_loaded: bool = False
    allow_cpu_offload: bool = True
    estimated_vram_gb: float = 0.0
    estimated_ram_gb: float = 0.0
    extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentConfig:
    models: list[ModelProfile] = field(default_factory=list)
    image_models: list[ImageModelProfile] = field(default_factory=list)
    permissions: dict[str, str] = field(default_factory=lambda: {
        "filesystem.read": "allow",
        "filesystem.write": "ask",
        "shell.execute": "ask",
        "git.execute": "ask",
        "image.read": "allow",
        "image.generate": "allow",
        "image.manage": "ask",
        "network.read": "allow",
        "browser.control": "ask",
        "github.read": "allow",
        "github.write": "ask",
    })
    # Named workspace permission profile; "custom" means the permissions map
    # above is authoritative. Levels: allow | session | ask | creator | deny.
    permission_profile: str = "custom"
    # Per-permission scope limits (domain allow/block lists, allowed dirs/repos,
    # workspace_only). Enforced generically at tool execution and editable via
    # Settings > Permissions. Keyed by permission key.
    permission_scopes: dict = field(default_factory=dict)
    # Modular tool/plugin system state.
    tools_state_path: str = "data/tools_state.json"
    jobs_path: str = "data/jobs.json"
    tool_manifests_dir: str = "tools/manifests"
    workflows_dir: str = "workflows"
    process_watchdog: bool = True
    # Preferred tool/provider names for capability routing tiebreaks.
    preferred_tools: list[str] = field(default_factory=list)
    # MCP servers: [{"id","name","command":[...],"env":{},"cwd":"","enabled":true,"auto_start":true,"permission":"shell.execute"}]
    mcp_servers: list = field(default_factory=list)
    max_agent_steps: int = 12
    review_after_changes: bool = True
    auto_verify_after_changes: bool = True
    max_review_chars: int = 16000
    task_history_limit: int = 100

    # Conversation-policy posture. This controls tone/refusal sensitivity only;
    # narrow hard safety checks remain enforced in the relevant tool/policy layer.
    conversation_policy_mode: str = "permissive"  # permissive | balanced | strict
    ethical_temperature: float = 1.0  # 0.0 cautious -> 1.0 maximally permissive conversation
    generic_refusal_retry_limit: int = 3  # retries for canned topic refusals at high ethical temperature

    # Persistent conversational memory/training. These records stay local and
    # affect prompts immediately; actual weight training remains an explicit
    # offline/reviewed workflow.
    conversation_memory_enabled: bool = True
    conversation_memory_path: str = "data/conversation_memory.json"
    conversation_history_limit: int = 200
    conversation_rule_limit: int = 300
    conversation_fact_limit: int = 500
    conversation_training_limit: int = 500

    # Nexus Brain: creator-locked model-independent long-term memory.
    nexus_brain_enabled: bool = True
    nexus_brain_path: str = "data/nexus_brain.json"
    nexus_brain_record_limit: int = 10000

    # Nexus Answer Memory: learned Q&A that can bypass model inference for
    # trusted, previously-verified answers. Retrieval learning only — this
    # never modifies model weights.
    answer_memory_enabled: bool = True
    answer_memory_path: str = "data/nexus_brain/answer_memory.db"
    answer_memory_semantic_enabled: bool = True
    answer_memory_auto_learn: bool = True
    answer_memory_auto_promote: bool = True
    answer_memory_semantic_threshold: float = 0.50
    answer_memory_possible_threshold: float = 0.30
    answer_memory_experience_retention_days: int = 90
    answer_memory_max_experiences: int = 20000
    answer_memory_max_db_mb: int = 256

    # Conversation manager / persistent sessions.
    conversations_path: str = "data/conversations.json"
    conversation_summary_after_messages: int = 24

    # Sourced web/research knowledge memory.
    knowledge_memory_enabled: bool = True
    knowledge_memory_path: str = "data/knowledge_memory.json"
    knowledge_default_ttl_hours: int = 720
    knowledge_current_ttl_hours: int = 12
    knowledge_max_records: int = 1000
    auto_research_unknown: bool = True

    # Model Growth Lab: review/export/versioned offline training.
    model_growth_enabled: bool = True
    model_growth_dir: str = "data/model_growth"
    trainer_backend: str = "external"
    trainer_command: str = ""

    # Outcome-aware automatic model routing. Telemetry stores only coarse local
    # metrics (no prompts/source text) and never overrides resource-fit checks.
    model_telemetry_enabled: bool = True
    model_telemetry_path: str = ".agent/model_performance.json"
    model_telemetry_min_samples: int = 3
    model_telemetry_weight: int = 20
    model_telemetry_max_events: int = 500

    # v0.2 runtime manager settings.
    runtime_auto_start: bool = True
    runtime_recovery_attempts: int = 1
    max_resident_models: int = 1

    # v0.8 unattended operation. autonomous_mode auto-approves permission
    # levels "ask"/"session" for reversible workspace actions; irreversible
    # real-world actions (spend.money, message.send, microphone.use,
    # camera.use) and explicit "deny" levels always stay gated.
    autonomous_mode: bool = False
    autonomous_max_continuations: int = 5
    # In autonomous mode, tasks interrupted by a core restart resume
    # automatically on startup (bounded by recovery_count in the task ledger).
    autonomous_resume_interrupted: bool = True
    autonomous_max_recoveries: int = 3
    # Seconds an error task must sit before the watchdog re-drives it in
    # autonomous mode (0 disables mid-session error retry; recovery_count
    # still bounds total attempts).
    autonomous_error_retry_seconds: float = 120.0
    # A chat message sent while a task is active is enqueued into the work
    # queue instead of racing the running task (false restores the old
    # concurrent-behavior, which is racy on the shared workspace).
    chat_queue_when_busy: bool = True
    # Hard cap per tool call so a hung tool cannot stall an unattended run
    # forever. Tools with their own timeouts (terminal_run, run_shell) finish
    # well inside this bound.
    agent_tool_timeout_seconds: float = 1800.0
    # In autonomous mode, a task parked on a hard-gated approval is failed
    # after this many seconds so an unattended run cannot stall all night
    # (0 waits forever, the pre-autonomy behavior).
    autonomous_approval_timeout_seconds: float = 3600.0
    # Nexus Autonomy: mission supervisor, triggers, schedules, standing
    # goals. Runs inside the backend so missions continue while the UI is
    # closed; missions never widen tool permissions.
    autonomy_enabled: bool = True
    # Optional quiet-hours window [start_hour, end_hour] local time during
    # which noncritical notifications are muted.
    autonomy_quiet_hours: list[int] | None = None
    # Idle managed models are stopped after this many seconds without a request
    # (0 disables). Busy models currently serving a task are never evicted.
    model_idle_unload_seconds: float = 900.0
    # A resident model launched at an oversized context (a big task grew its
    # window via ensure_ready) is relaunched at the role-recommended window
    # after this many idle seconds (0 disables). This frees the excess KV
    # cache without a cold reload.
    context_shrink_idle_seconds: float = 600.0
    # The launched window must exceed context_shrink_factor x the role
    # recommendation before shrinking (hysteresis so minor overages don't
    # trigger relaunches).
    context_shrink_factor: float = 1.5
    # Send a 1-token request right after a managed runtime reports healthy so
    # the first real generation doesn't pay the cold-load cost.
    model_warmup: bool = True
    # Fast General lane: bounded context + output budget for the small
    # resident utility model. Long-form keyword requests escalate to the
    # larger output budget instead of truncating.
    fast_general_history_turns: int = 8
    fast_general_context_chars: int = 9000
    # Total budget for optional injected context in the coding lane (system
    # memory/knowledge/research blocks + carried history share it). Prevents
    # oversized prompts that overflow the context window outright.
    coding_context_chars: int = 60000
    fast_general_output_tokens: int = 1024
    fast_general_long_output_tokens: int = 2048
    # Performance profile for managed llama.cpp runtimes:
    # auto (benchmarked/intelligent), quiet, balanced, max.
    performance_mode: str = "auto"
    # Idle-gated automatic benchmark: after boot settles and while no task is
    # running, managed llama.cpp models without a valid tuned result for the
    # current hardware/build fingerprint get a bounded benchmark sweep so
    # tuned_flags() serves measured settings instead of heuristics.
    runtime_auto_tune: bool = True
    # Seconds of post-boot idle before the auto-tuner is allowed to start.
    # Probes launch real llama-server processes — never run while busy.
    runtime_auto_tune_idle_seconds: float = 45.0
    # Scale the launched llama.cpp context window to the model's routing role
    # (fast general 8K, coding 16K, deep/review 32K) instead of always using
    # the profile's maximum. Never exceeds the configured context_window.
    runtime_dynamic_context: bool = True
    # When free memory drops below these floors, the least-recently-used
    # resident model is stopped even inside the idle window (0 disables each).
    memory_pressure_vram_gb: float = 0.0
    memory_pressure_ram_gb: float = 0.0
    models_dir: str = "models"
    runtime_logs_dir: str = ".agent/runtime"
    llama_cpp_executable: str = ""

    # v0.4 image subsystem settings.
    image_enabled: bool = True
    image_models_dir: str = "models/image"
    image_data_dir: str = "data/image"
    image_workflows_dir: str = "workflows/image"
    comfyui_endpoint: str = "http://127.0.0.1:8188"
    comfyui_auto_start: bool = False
    comfyui_start_on_image_request: bool = True
    comfyui_dir: str = "ComfyUI"
    comfyui_python: str = ""
    comfyui_extra_args: list[str] = field(default_factory=list)
    comfyui_logs_dir: str = ".agent/runtime"
    comfyui_startup_timeout: int = 300
    comfyui_idle_unload_seconds: float = 900.0
    image_resource_mode: str = "balanced"
    image_restore_chat_model: bool = True
    image_auto_run_jobs: bool = True
    image_job_timeout: int = 900
    image_output_dir: str = "output/images"

    # Voice / TTS subsystem (local-first; Kokoro ONNX on CPU by default so
    # speech never competes with coding models for VRAM).
    voice_enabled: bool = True
    voice_muted: bool = False
    voice_engine: str = "kokoro"
    voice_preset_id: str = "nexus-synthetic-isabella"
    voice_mode: str = "responses"          # off | responses | responses_activity | manual
    voice_volume: float = 1.0
    voice_speed: float = 1.0
    voice_output_device: str = ""          # empty = default device
    voice_assets_dir: str = "models/voice"
    voice_presets_dir: str = "data/voice/presets"
    voice_cache_dir: str = "data/voice/cache"
    voice_cache_max_mb: int = 512
    voice_device: str = "cpu"              # cpu | gpu (best-effort ORT provider)
    voice_max_concurrency: int = 1
    voice_idle_unload_seconds: float = 600.0

    # v0.5 research/documentation subsystem settings.
    research_enabled: bool = True
    research_mode: str = "auto"  # auto | local_only | official | balanced | deep | offline | none
    research_data_dir: str = ".agent/research"
    research_cache_ttl_hours: int = 168
    research_max_queries: int = 6
    research_max_pages: int = 8
    research_max_chars_per_source: int = 20000
    research_trusted_domains: list[str] = field(default_factory=list)
    research_blocked_domains: list[str] = field(default_factory=list)
    research_github_api_enabled: bool = True
    research_github_api_url: str = "https://api.github.com"
    research_github_api_version: str = "2026-03-10"
    research_github_token_env: str = "GITHUB_TOKEN"
    research_github_timeout: int = 15
    max_auto_repair_cycles: int = 2

    # v0.6 native GitHub coding/action integration.
    github_enabled: bool = True
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"
    github_token_env: str = "GITHUB_TOKEN"
    github_timeout: int = 15
    github_default_remote: str = "origin"


def default_config() -> AgentConfig:
    return AgentConfig(
        models=[
            ModelProfile(
                id="qwen3-4b-instruct",
                endpoint="http://127.0.0.1:8080/v1",
                model="Qwen_Qwen3-4B-Instruct-2507-Q4_K_M",
                model_path="models/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
                roles=["utility"],
                context_window=8192,
                max_output_tokens=1024,
                temperature=0.6,
                priority=95,
                runtime="llama_cpp",
                gpu_layers="auto",
                keep_loaded=True,
                allow_cpu_offload=True,
                estimated_vram_gb=3.0,
                estimated_ram_gb=4.5,
                notes="Fast-lane utility/general model; stays resident so ordinary questions start immediately.",
            ),
            ModelProfile(
                id="qwen3-14b",
                endpoint="http://127.0.0.1:8081/v1",
                model="Qwen3-14B-Q4_K_M",
                model_path="models/Qwen3-14B-Q4_K_M.gguf",
                roles=["fast_coder", "primary_coder"],
                context_window=24576,
                max_output_tokens=2048,
                priority=90,
                runtime="llama_cpp",
                gpu_layers="auto",
                allow_cpu_offload=True,
                estimated_vram_gb=10.0,
                estimated_ram_gb=16.0,
                extra_args=["--reasoning", "off"],
                notes="Official Qwen3 14B Q4_K_M starter for fast/general coding.",
            ),
            ModelProfile(
                id="qwen3-coder-30b",
                endpoint="http://127.0.0.1:8082/v1",
                model="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M",
                model_path="models/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
                roles=["deep_reasoner", "reviewer"],
                context_window=32768,
                max_output_tokens=8192,
                priority=100,
                runtime="llama_cpp",
                gpu_layers="auto",
                allow_cpu_offload=True,
                estimated_vram_gb=18.6,
                estimated_ram_gb=30.0,
                notes="Heavier coding/reasoning model; expected to use CPU/GPU offload on 12 GB VRAM.",
            ),
        ],
        image_models=[
            ImageModelProfile(
                id="juggernaut-x-v10",
                family="stable-diffusion-xl",
                model_path="models/image/stable-diffusion/checkpoints/Juggernaut-X-RunDiffusion-NSFW.safetensors",
                workflows={
                    "text_to_image": "sdxl/juggernaut-x-v10-t2i-api.json",
                },
                components=[
                    {"key":"checkpoint","path":"models/image/stable-diffusion/checkpoints/Juggernaut-X-RunDiffusion-NSFW.safetensors","url":"https://huggingface.co/RunDiffusion/Juggernaut-X-v10/resolve/e53841ec9fc47ad9b803d6bfcfb3c00bdd815023/Juggernaut-X-RunDiffusion-NSFW.safetensors","sha256":"d91d35736d8f2be038f760a9b0009a771ecf0a417e9b38c244a84ea4cb9c0c45","size_bytes":7105348672,"required":True},
                ],
                required_nodes=["CheckpointLoaderSimple","CLIPTextEncode","EmptyLatentImage","KSampler","VAEDecode","SaveImage"],
                capabilities=["text_to_image"],
                priority=110, quality_tier="high", speed_tier="balanced",
                quantization="full SDXL checkpoint (fp16)",
                estimated_vram_gb=10.0, estimated_ram_gb=16.0, max_loras=4,
                homepage="https://huggingface.co/RunDiffusion/Juggernaut-X-v10",
                license_name="CreativeML OpenRAIL-M",
                display_name="Juggernaut X v10",
                tagline="Photorealistic · SDXL · default generation",
                notes="Default general text-to-image model (pinned RunDiffusion revision e53841ec). SDXL checkpoint loaded via CheckpointLoaderSimple.",
            ),
            ImageModelProfile(
                id="qwen-image-2.1",
                family="qwen-image-2.1",
                model_path="models/image/qwen/diffusion_models/qwen_image_2.1_int8_convrot.safetensors",
                workflows={
                    "text_to_image":"qwen/qwen-image-2.1-t2i-api.json",
                    "edit_image":"qwen/qwen-image-2.1-edit-api.json",
                    "variation":"qwen/qwen-image-2.1-edit-api.json",
                    "inpaint":"qwen/qwen-image-2.1-inpaint-api.json",
                    "outpaint":"qwen/qwen-image-2.1-edit-api.json",
                    "remove_background":"qwen/qwen-image-2.1-background-removal-api.json",
                },
                components=[
                    {"key":"diffusion_model","path":"models/image/qwen/diffusion_models/qwen_image_2.1_int8_convrot.safetensors","url":"https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors","required":True},
                    {"key":"text_encoder","path":"models/image/qwen/text_encoders/qwen3vl_8b_int8_convrot.safetensors","url":"https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors","required":True},
                    {"key":"prompt_encoder","path":"models/image/qwen/text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors","url":"https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors","required":False},
                    {"key":"vae","path":"models/image/qwen/vae/qwen_image_2.1_vae_bf16.safetensors","url":"https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors","required":True},
                ],
                required_nodes=["TextEncodeQwenImage21","QwenImage21Cache"],
                capabilities=["text_to_image", "image_edit", "inpaint", "outpaint", "background_removal", "multi_reference"],
                priority=80, quality_tier="high", speed_tier="balanced", max_reference_images=10, supports_transparency=True,
                quantization="official int8 convrot; GGUF profiles/workflows can be added without changing the router",
                estimated_vram_gb=11.0, estimated_ram_gb=28.0, max_loras=4,
                homepage="https://huggingface.co/Comfy-Org/Qwen-Image-2.1",
                license_name="Apache-2.0",
                display_name="Qwen Image 2.1",
                tagline="Editing / inpainting / high-quality image operations",
                notes="Preferred quality/editing path. API-format workflows are verified separately from model files.",
            ),
            ImageModelProfile(
                id="flux2-klein-4b",
                family="flux.2-klein",
                model_path="models/image/flux/diffusion_models/flux-2-klein-4b.safetensors",
                workflows={
                    "text_to_image":"flux/flux2-klein-4b-t2i-api.json",
                    "edit_image":"flux/flux2-klein-4b-edit-api.json",
                    "variation":"flux/flux2-klein-4b-edit-api.json",
                },
                components=[
                    {"key":"diffusion_model","path":"models/image/flux/diffusion_models/flux-2-klein-4b.safetensors","url":"https://huggingface.co/Comfy-Org/flux2-klein/resolve/main/split_files/diffusion_models/flux-2-klein-4b.safetensors","required":True},
                    {"key":"text_encoder","path":"models/image/flux/text_encoders/qwen_3_4b.safetensors","url":"https://huggingface.co/Comfy-Org/flux2-klein/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors","required":True},
                    {"key":"vae","path":"models/image/flux/vae/flux2-vae.safetensors","url":"https://huggingface.co/Comfy-Org/flux2-dev/resolve/main/split_files/vae/flux2-vae.safetensors","required":True},
                ],
                capabilities=["text_to_image", "image_edit", "multi_reference"],
                priority=70, quality_tier="balanced", speed_tier="fast", max_reference_images=4,
                quantization="4B distilled", estimated_vram_gb=9.0, estimated_ram_gb=16.0, max_loras=4,
                homepage="https://huggingface.co/Comfy-Org/flux2-klein",
                license_name="Apache-2.0",
                display_name="FLUX.2 Klein 4B",
                tagline="Fast preview",
                notes="Fast preview/draft model. Official 4B distilled path is designed for low-latency generation and editing.",
            ),
        ],
    )


def _profile_from_dict(raw: dict[str, Any], cls: type) -> Any:
    """Build a profile tolerating unknown keys so a config written by a
    newer build does not crash an older one."""
    allowed = getattr(cls, "__dataclass_fields__", {})
    filtered = {k: v for k, v in raw.items() if k in allowed} if allowed else dict(raw)
    return cls(**filtered)


def load_config(path: Path | None) -> AgentConfig:
    if path is None or not path.exists():
        return default_config()
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A damaged config must not crash-loop the backend: quarantine it for
        # inspection and boot from defaults so the app stays usable.
        import shutil
        import time as _time
        try:
            shutil.copy2(path, path.with_name(path.name + f".corrupt-{int(_time.time())}"))
        except OSError:
            pass
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    models = []
    for m in raw.get("models", []):
        try:
            models.append(_profile_from_dict(m, ModelProfile))
        except (TypeError, AttributeError):
            continue
    image_models = []
    for m in raw.get("image_models", []):
        try:
            image_models.append(_profile_from_dict(m, ImageModelProfile))
        except (TypeError, AttributeError):
            continue
    defaults = default_config()
    # Merge new default image models into existing installs so an update can
    # offer newly added defaults (e.g. Juggernaut) without touching the user's
    # existing model list. User-configured entries always win by id.
    if image_models:
        known = {m.id for m in image_models}
        image_models.extend(m for m in defaults.image_models if m.id not in known)
        # Backfill presentation fields added after the user's entry was saved
        # (display_name/tagline/license_name) — never overwrite set values.
        default_by_id = {m.id: m for m in defaults.image_models}
        for m in image_models:
            d = default_by_id.get(m.id)
            if d is None:
                continue
            for field_name in ("display_name", "tagline", "license_name", "homepage"):
                if not getattr(m, field_name):
                    setattr(m, field_name, getattr(d, field_name))
    cfg = AgentConfig(models=models or defaults.models, image_models=image_models or defaults.image_models)
    # Fast-lane migration: configs saved before the dedicated utility model
    # still route "utility" to the 14B. Graft the small resident profile in;
    # the 14B drops the role and remains reachable via the primary-coder
    # fallback when the utility model is not installed.
    default_utility = next((m for m in defaults.models if set(m.roles) == {"utility"}), None)
    dedicated_utility = any(
        "utility" in m.roles and not {"fast_coder", "primary_coder"}.intersection(m.roles)
        for m in cfg.models
    )
    if default_utility is not None and not dedicated_utility:
        cfg.models.insert(0, default_utility)
        for profile in cfg.models:
            if profile.id != default_utility.id and "utility" in profile.roles and len(profile.roles) > 1:
                profile.roles = [r for r in profile.roles if r != "utility"]
    cfg.permissions.update(raw.get("permissions", {}))
    cfg.permission_profile = str(raw.get("permission_profile", cfg.permission_profile))
    raw_scopes = raw.get("permission_scopes")
    if isinstance(raw_scopes, dict):
        cfg.permission_scopes = {str(k): dict(v) for k, v in raw_scopes.items() if isinstance(v, dict)}
    cfg.tools_state_path = str(raw.get("tools_state_path", cfg.tools_state_path))
    cfg.jobs_path = str(raw.get("jobs_path", cfg.jobs_path))
    cfg.tool_manifests_dir = str(raw.get("tool_manifests_dir", cfg.tool_manifests_dir))
    cfg.workflows_dir = str(raw.get("workflows_dir", cfg.workflows_dir))
    cfg.process_watchdog = bool(raw.get("process_watchdog", cfg.process_watchdog))
    cfg.preferred_tools = [str(x) for x in raw.get("preferred_tools", cfg.preferred_tools)]
    cfg.mcp_servers = list(raw.get("mcp_servers", cfg.mcp_servers) or [])
    cfg.max_agent_steps = int(raw.get("max_agent_steps", cfg.max_agent_steps))
    cfg.review_after_changes = bool(raw.get("review_after_changes", cfg.review_after_changes))
    cfg.auto_verify_after_changes = bool(raw.get("auto_verify_after_changes", cfg.auto_verify_after_changes))
    cfg.max_review_chars = max(2000, int(raw.get("max_review_chars", cfg.max_review_chars)))
    cfg.task_history_limit = max(10, int(raw.get("task_history_limit", cfg.task_history_limit)))
    policy_mode = str(raw.get("conversation_policy_mode", cfg.conversation_policy_mode)).strip().lower()
    cfg.conversation_policy_mode = policy_mode if policy_mode in {"permissive", "balanced", "strict"} else "permissive"
    cfg.ethical_temperature = max(0.0, min(1.0, float(raw.get("ethical_temperature", cfg.ethical_temperature))))
    cfg.generic_refusal_retry_limit = max(0, min(5, int(raw.get("generic_refusal_retry_limit", cfg.generic_refusal_retry_limit))))
    cfg.conversation_memory_enabled = bool(raw.get("conversation_memory_enabled", cfg.conversation_memory_enabled))
    cfg.conversation_memory_path = str(raw.get("conversation_memory_path", cfg.conversation_memory_path))
    cfg.conversation_history_limit = max(20, int(raw.get("conversation_history_limit", cfg.conversation_history_limit)))
    cfg.conversation_rule_limit = max(20, int(raw.get("conversation_rule_limit", cfg.conversation_rule_limit)))
    cfg.conversation_fact_limit = max(20, int(raw.get("conversation_fact_limit", cfg.conversation_fact_limit)))
    cfg.conversation_training_limit = max(20, int(raw.get("conversation_training_limit", cfg.conversation_training_limit)))
    cfg.nexus_brain_enabled = bool(raw.get("nexus_brain_enabled", cfg.nexus_brain_enabled))
    cfg.nexus_brain_path = str(raw.get("nexus_brain_path", cfg.nexus_brain_path))
    cfg.nexus_brain_record_limit = max(100, int(raw.get("nexus_brain_record_limit", cfg.nexus_brain_record_limit)))
    cfg.answer_memory_enabled = bool(raw.get("answer_memory_enabled", cfg.answer_memory_enabled))
    cfg.answer_memory_path = str(raw.get("answer_memory_path", cfg.answer_memory_path))
    cfg.answer_memory_semantic_enabled = bool(raw.get("answer_memory_semantic_enabled", cfg.answer_memory_semantic_enabled))
    cfg.answer_memory_auto_learn = bool(raw.get("answer_memory_auto_learn", cfg.answer_memory_auto_learn))
    cfg.answer_memory_auto_promote = bool(raw.get("answer_memory_auto_promote", cfg.answer_memory_auto_promote))
    cfg.answer_memory_semantic_threshold = max(0.0, min(1.0, float(raw.get("answer_memory_semantic_threshold", cfg.answer_memory_semantic_threshold))))
    cfg.answer_memory_possible_threshold = max(0.0, min(1.0, float(raw.get("answer_memory_possible_threshold", cfg.answer_memory_possible_threshold))))
    cfg.answer_memory_experience_retention_days = max(1, int(raw.get("answer_memory_experience_retention_days", cfg.answer_memory_experience_retention_days)))
    cfg.answer_memory_max_experiences = max(100, int(raw.get("answer_memory_max_experiences", cfg.answer_memory_max_experiences)))
    cfg.answer_memory_max_db_mb = max(16, int(raw.get("answer_memory_max_db_mb", cfg.answer_memory_max_db_mb)))
    cfg.conversations_path = str(raw.get("conversations_path", cfg.conversations_path))
    cfg.conversation_summary_after_messages = max(8, int(raw.get("conversation_summary_after_messages", cfg.conversation_summary_after_messages)))
    cfg.knowledge_memory_enabled = bool(raw.get("knowledge_memory_enabled", cfg.knowledge_memory_enabled))
    cfg.knowledge_memory_path = str(raw.get("knowledge_memory_path", cfg.knowledge_memory_path))
    cfg.knowledge_default_ttl_hours = max(1, int(raw.get("knowledge_default_ttl_hours", cfg.knowledge_default_ttl_hours)))
    cfg.knowledge_current_ttl_hours = max(1, int(raw.get("knowledge_current_ttl_hours", cfg.knowledge_current_ttl_hours)))
    cfg.knowledge_max_records = max(50, int(raw.get("knowledge_max_records", cfg.knowledge_max_records)))
    cfg.auto_research_unknown = bool(raw.get("auto_research_unknown", cfg.auto_research_unknown))
    cfg.model_growth_enabled = bool(raw.get("model_growth_enabled", cfg.model_growth_enabled))
    cfg.model_growth_dir = str(raw.get("model_growth_dir", cfg.model_growth_dir))
    cfg.trainer_backend = str(raw.get("trainer_backend", cfg.trainer_backend))
    cfg.trainer_command = str(raw.get("trainer_command", cfg.trainer_command))
    cfg.model_telemetry_enabled = bool(raw.get("model_telemetry_enabled", cfg.model_telemetry_enabled))
    cfg.model_telemetry_path = str(raw.get("model_telemetry_path", cfg.model_telemetry_path))
    cfg.model_telemetry_min_samples = max(1, int(raw.get("model_telemetry_min_samples", cfg.model_telemetry_min_samples)))
    cfg.model_telemetry_weight = max(0, min(100, int(raw.get("model_telemetry_weight", cfg.model_telemetry_weight))))
    cfg.model_telemetry_max_events = max(20, int(raw.get("model_telemetry_max_events", cfg.model_telemetry_max_events)))
    cfg.runtime_auto_start = bool(raw.get("runtime_auto_start", cfg.runtime_auto_start))
    cfg.runtime_recovery_attempts = max(0, int(raw.get("runtime_recovery_attempts", cfg.runtime_recovery_attempts)))
    cfg.autonomous_mode = bool(raw.get("autonomous_mode", cfg.autonomous_mode))
    cfg.autonomous_max_continuations = max(0, int(raw.get("autonomous_max_continuations", cfg.autonomous_max_continuations)))
    cfg.autonomous_resume_interrupted = bool(raw.get("autonomous_resume_interrupted", cfg.autonomous_resume_interrupted))
    cfg.autonomous_max_recoveries = max(0, int(raw.get("autonomous_max_recoveries", cfg.autonomous_max_recoveries)))
    cfg.autonomous_error_retry_seconds = max(0.0, float(raw.get("autonomous_error_retry_seconds", cfg.autonomous_error_retry_seconds)))
    cfg.chat_queue_when_busy = bool(raw.get("chat_queue_when_busy", cfg.chat_queue_when_busy))
    cfg.agent_tool_timeout_seconds = max(1.0, float(raw.get("agent_tool_timeout_seconds", cfg.agent_tool_timeout_seconds)))
    cfg.autonomous_approval_timeout_seconds = max(0.0, float(raw.get("autonomous_approval_timeout_seconds", cfg.autonomous_approval_timeout_seconds)))
    cfg.autonomy_enabled = bool(raw.get("autonomy_enabled", cfg.autonomy_enabled))
    qh = raw.get("autonomy_quiet_hours", cfg.autonomy_quiet_hours)
    if isinstance(qh, (list, tuple)) and len(qh) == 2:
        cfg.autonomy_quiet_hours = [int(qh[0]) % 24, int(qh[1]) % 24]
    cfg.model_idle_unload_seconds = max(0.0, float(raw.get("model_idle_unload_seconds", cfg.model_idle_unload_seconds)))
    cfg.context_shrink_idle_seconds = max(0.0, float(raw.get("context_shrink_idle_seconds", cfg.context_shrink_idle_seconds)))
    cfg.context_shrink_factor = max(1.0, float(raw.get("context_shrink_factor", cfg.context_shrink_factor)))
    cfg.model_warmup = bool(raw.get("model_warmup", cfg.model_warmup))
    cfg.fast_general_history_turns = max(0, int(raw.get("fast_general_history_turns", cfg.fast_general_history_turns)))
    cfg.fast_general_context_chars = max(500, int(raw.get("fast_general_context_chars", cfg.fast_general_context_chars)))
    cfg.coding_context_chars = max(2000, int(raw.get("coding_context_chars", cfg.coding_context_chars)))
    cfg.fast_general_output_tokens = max(128, int(raw.get("fast_general_output_tokens", cfg.fast_general_output_tokens)))
    cfg.fast_general_long_output_tokens = max(256, int(raw.get("fast_general_long_output_tokens", cfg.fast_general_long_output_tokens)))
    perf_mode = str(raw.get("performance_mode", cfg.performance_mode)).strip().lower()
    cfg.performance_mode = perf_mode if perf_mode in {"auto", "quiet", "balanced", "max"} else "auto"
    cfg.runtime_dynamic_context = bool(raw.get("runtime_dynamic_context", cfg.runtime_dynamic_context))
    cfg.runtime_auto_tune = bool(raw.get("runtime_auto_tune", cfg.runtime_auto_tune))
    try:
        cfg.runtime_auto_tune_idle_seconds = float(
            raw.get("runtime_auto_tune_idle_seconds", cfg.runtime_auto_tune_idle_seconds))
    except (TypeError, ValueError):
        pass
    cfg.memory_pressure_vram_gb = max(0.0, float(raw.get("memory_pressure_vram_gb", cfg.memory_pressure_vram_gb)))
    cfg.memory_pressure_ram_gb = max(0.0, float(raw.get("memory_pressure_ram_gb", cfg.memory_pressure_ram_gb)))
    cfg.max_resident_models = max(1, int(raw.get("max_resident_models", cfg.max_resident_models)))
    cfg.models_dir = str(raw.get("models_dir", cfg.models_dir))
    cfg.runtime_logs_dir = str(raw.get("runtime_logs_dir", cfg.runtime_logs_dir))
    cfg.llama_cpp_executable = str(raw.get("llama_cpp_executable", cfg.llama_cpp_executable))
    cfg.image_enabled = bool(raw.get("image_enabled", cfg.image_enabled))
    cfg.image_models_dir = str(raw.get("image_models_dir", cfg.image_models_dir))
    cfg.image_data_dir = str(raw.get("image_data_dir", cfg.image_data_dir))
    cfg.image_workflows_dir = str(raw.get("image_workflows_dir", cfg.image_workflows_dir))
    cfg.comfyui_endpoint = str(raw.get("comfyui_endpoint", cfg.comfyui_endpoint))
    cfg.comfyui_auto_start = bool(raw.get("comfyui_auto_start", cfg.comfyui_auto_start))
    cfg.comfyui_start_on_image_request = bool(raw.get("comfyui_start_on_image_request", cfg.comfyui_start_on_image_request))
    cfg.comfyui_dir = str(raw.get("comfyui_dir", cfg.comfyui_dir))
    cfg.comfyui_python = str(raw.get("comfyui_python", cfg.comfyui_python))
    cfg.comfyui_extra_args = [str(x) for x in raw.get("comfyui_extra_args", cfg.comfyui_extra_args)]
    cfg.comfyui_logs_dir = str(raw.get("comfyui_logs_dir", cfg.comfyui_logs_dir))
    cfg.comfyui_startup_timeout = max(10, int(raw.get("comfyui_startup_timeout", cfg.comfyui_startup_timeout)))
    cfg.comfyui_idle_unload_seconds = max(0.0, float(raw.get("comfyui_idle_unload_seconds", cfg.comfyui_idle_unload_seconds)))
    cfg.image_resource_mode = str(raw.get("image_resource_mode", cfg.image_resource_mode))
    cfg.image_restore_chat_model = bool(raw.get("image_restore_chat_model", cfg.image_restore_chat_model))
    cfg.image_auto_run_jobs = bool(raw.get("image_auto_run_jobs", cfg.image_auto_run_jobs))
    cfg.image_job_timeout = max(30, int(raw.get("image_job_timeout", cfg.image_job_timeout)))
    cfg.image_output_dir = str(raw.get("image_output_dir", cfg.image_output_dir))
    cfg.voice_enabled = bool(raw.get("voice_enabled", cfg.voice_enabled))
    cfg.voice_muted = bool(raw.get("voice_muted", cfg.voice_muted))
    cfg.voice_engine = str(raw.get("voice_engine", cfg.voice_engine))
    cfg.voice_preset_id = str(raw.get("voice_preset_id", cfg.voice_preset_id))
    cfg.voice_mode = str(raw.get("voice_mode", cfg.voice_mode))
    cfg.voice_volume = max(0.0, min(2.0, float(raw.get("voice_volume", cfg.voice_volume))))
    cfg.voice_speed = max(0.5, min(2.0, float(raw.get("voice_speed", cfg.voice_speed))))
    cfg.voice_output_device = str(raw.get("voice_output_device", cfg.voice_output_device))
    cfg.voice_assets_dir = str(raw.get("voice_assets_dir", cfg.voice_assets_dir))
    cfg.voice_presets_dir = str(raw.get("voice_presets_dir", cfg.voice_presets_dir))
    cfg.voice_cache_dir = str(raw.get("voice_cache_dir", cfg.voice_cache_dir))
    cfg.voice_cache_max_mb = max(32, int(raw.get("voice_cache_max_mb", cfg.voice_cache_max_mb)))
    cfg.voice_device = str(raw.get("voice_device", cfg.voice_device))
    cfg.voice_max_concurrency = max(1, int(raw.get("voice_max_concurrency", cfg.voice_max_concurrency)))
    cfg.voice_idle_unload_seconds = max(0.0, float(raw.get("voice_idle_unload_seconds", cfg.voice_idle_unload_seconds)))
    cfg.research_enabled = bool(raw.get("research_enabled", cfg.research_enabled))
    cfg.research_mode = str(raw.get("research_mode", cfg.research_mode))
    cfg.research_data_dir = str(raw.get("research_data_dir", cfg.research_data_dir))
    cfg.research_cache_ttl_hours = max(1, int(raw.get("research_cache_ttl_hours", cfg.research_cache_ttl_hours)))
    cfg.research_max_queries = max(1, int(raw.get("research_max_queries", cfg.research_max_queries)))
    cfg.research_max_pages = max(1, int(raw.get("research_max_pages", cfg.research_max_pages)))
    cfg.research_max_chars_per_source = max(1000, int(raw.get("research_max_chars_per_source", cfg.research_max_chars_per_source)))
    cfg.research_trusted_domains = [str(x) for x in raw.get("research_trusted_domains", cfg.research_trusted_domains)]
    cfg.research_blocked_domains = [str(x) for x in raw.get("research_blocked_domains", cfg.research_blocked_domains)]
    cfg.research_github_api_enabled = bool(raw.get("research_github_api_enabled", cfg.research_github_api_enabled))
    cfg.research_github_api_url = str(raw.get("research_github_api_url", cfg.research_github_api_url))
    cfg.research_github_api_version = str(raw.get("research_github_api_version", cfg.research_github_api_version))
    cfg.research_github_token_env = str(raw.get("research_github_token_env", cfg.research_github_token_env))
    cfg.research_github_timeout = max(3, int(raw.get("research_github_timeout", cfg.research_github_timeout)))
    cfg.max_auto_repair_cycles = max(0, int(raw.get("max_auto_repair_cycles", cfg.max_auto_repair_cycles)))
    cfg.github_enabled = bool(raw.get("github_enabled", cfg.github_enabled))
    cfg.github_api_url = str(raw.get("github_api_url", cfg.github_api_url))
    cfg.github_api_version = str(raw.get("github_api_version", cfg.github_api_version))
    cfg.github_token_env = str(raw.get("github_token_env", cfg.github_token_env))
    cfg.github_timeout = max(3, int(raw.get("github_timeout", cfg.github_timeout)))
    cfg.github_default_remote = str(raw.get("github_default_remote", cfg.github_default_remote)) or "origin"
    return cfg
