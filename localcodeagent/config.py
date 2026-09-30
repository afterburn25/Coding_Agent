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
        "network.read": "allow",
        "browser.control": "ask",
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


def default_config() -> AgentConfig:
    return AgentConfig(
        models=[
            ModelProfile(
                id="primary-local",
                endpoint="http://127.0.0.1:8080/v1",
                model="local-model",
                roles=["utility", "fast_coder", "primary_coder", "deep_reasoner", "reviewer"],
                context_window=32768,
                priority=50,
                runtime="external",
                notes="Fallback profile. Point this at a llama.cpp/Ollama/OpenAI-compatible local endpoint.",
            )
        ],
        image_models=[
            ImageModelProfile(
                id="qwen-image-2.1",
                family="qwen-image-2.1",
                capabilities=["text_to_image", "image_edit", "inpaint", "outpaint", "background_removal", "multi_reference"],
                priority=80, quality_tier="high", speed_tier="balanced", max_reference_images=10, supports_transparency=True,
                estimated_vram_gb=11.0, estimated_ram_gb=24.0,
                notes="Preferred quality/editing model. Configure model_path and a ComfyUI API workflow before use.",
            ),
            ImageModelProfile(
                id="flux2-klein-4b",
                family="flux.2-klein",
                capabilities=["text_to_image", "image_edit", "multi_reference"],
                priority=70, quality_tier="balanced", speed_tier="fast", max_reference_images=4,
                estimated_vram_gb=12.5, estimated_ram_gb=16.0,
                notes="Fast preview/draft model. Configure model_path and a ComfyUI API workflow before use.",
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
    return cfg
