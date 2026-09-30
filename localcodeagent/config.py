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
    max_agent_steps: int = 12
    review_after_changes: bool = True
    auto_verify_after_changes: bool = True
    max_review_chars: int = 16000
    task_history_limit: int = 100

    # v0.2 runtime manager settings.
    runtime_auto_start: bool = True
    runtime_recovery_attempts: int = 1
    max_resident_models: int = 1
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
    comfyui_dir: str = "ComfyUI"
    comfyui_python: str = ""
    comfyui_extra_args: list[str] = field(default_factory=list)
    comfyui_logs_dir: str = ".agent/runtime"
    comfyui_startup_timeout: int = 180
    image_resource_mode: str = "balanced"
    image_restore_chat_model: bool = True
    image_auto_run_jobs: bool = True
    image_job_timeout: int = 900

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
                id="qwen3-14b",
                endpoint="http://127.0.0.1:8081/v1",
                model="Qwen3-14B-Q4_K_M",
                model_path="models/Qwen3-14B-Q4_K_M.gguf",
                roles=["utility", "fast_coder", "primary_coder"],
                context_window=32768,
                priority=90,
                runtime="llama_cpp",
                gpu_layers="auto",
                allow_cpu_offload=True,
                estimated_vram_gb=10.0,
                estimated_ram_gb=16.0,
                notes="Official Qwen3 14B Q4_K_M starter for fast/general coding.",
            ),
            ModelProfile(
                id="qwen3-coder-30b",
                endpoint="http://127.0.0.1:8082/v1",
                model="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M",
                model_path="models/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
                roles=["deep_reasoner", "reviewer"],
                context_window=32768,
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
                notes="Fast preview/draft model. Official 4B distilled path is designed for low-latency generation and editing.",
            ),
        ],
    )


def load_config(path: Path | None) -> AgentConfig:
    if path is None or not path.exists():
        return default_config()
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    models = [ModelProfile(**m) for m in raw.get("models", [])]
    image_models = [ImageModelProfile(**m) for m in raw.get("image_models", [])]
    defaults = default_config()
    cfg = AgentConfig(models=models or defaults.models, image_models=image_models or defaults.image_models)
    cfg.permissions.update(raw.get("permissions", {}))
    cfg.max_agent_steps = int(raw.get("max_agent_steps", cfg.max_agent_steps))
    cfg.review_after_changes = bool(raw.get("review_after_changes", cfg.review_after_changes))
    cfg.auto_verify_after_changes = bool(raw.get("auto_verify_after_changes", cfg.auto_verify_after_changes))
    cfg.max_review_chars = max(2000, int(raw.get("max_review_chars", cfg.max_review_chars)))
    cfg.task_history_limit = max(10, int(raw.get("task_history_limit", cfg.task_history_limit)))
    cfg.runtime_auto_start = bool(raw.get("runtime_auto_start", cfg.runtime_auto_start))
    cfg.runtime_recovery_attempts = max(0, int(raw.get("runtime_recovery_attempts", cfg.runtime_recovery_attempts)))
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
    cfg.comfyui_dir = str(raw.get("comfyui_dir", cfg.comfyui_dir))
    cfg.comfyui_python = str(raw.get("comfyui_python", cfg.comfyui_python))
    cfg.comfyui_extra_args = [str(x) for x in raw.get("comfyui_extra_args", cfg.comfyui_extra_args)]
    cfg.comfyui_logs_dir = str(raw.get("comfyui_logs_dir", cfg.comfyui_logs_dir))
    cfg.comfyui_startup_timeout = max(10, int(raw.get("comfyui_startup_timeout", cfg.comfyui_startup_timeout)))
    cfg.image_resource_mode = str(raw.get("image_resource_mode", cfg.image_resource_mode))
    cfg.image_restore_chat_model = bool(raw.get("image_restore_chat_model", cfg.image_restore_chat_model))
    cfg.image_auto_run_jobs = bool(raw.get("image_auto_run_jobs", cfg.image_auto_run_jobs))
    cfg.image_job_timeout = max(30, int(raw.get("image_job_timeout", cfg.image_job_timeout)))
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
