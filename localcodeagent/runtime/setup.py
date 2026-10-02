from __future__ import annotations

import json
from pathlib import Path

from ..fsutil import replace_with_retry
from typing import Any


def _catalog_pair_assignments(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str], str]]:
    """Prefer the known Nexus Core 4B/14B/30B trio when those files are present."""
    by_name = {str(row["name"]).lower(): row for row in rows}
    q4 = by_name.get("qwen_qwen3-4b-instruct-2507-q4_k_m.gguf")
    q14 = by_name.get("qwen3-14b-q4_k_m.gguf")
    q30 = by_name.get("qwen3-coder-30b-a3b-instruct-q4_k_m.gguf")
    fast_lane = [(q4, ["utility"], "qwen3-4b-instruct")] if q4 else []
    if q14 and q30:
        return fast_lane + [
            (q14, ["fast_coder", "primary_coder"] + (["utility"] if not q4 else []), "qwen3-14b"),
            (q30, ["deep_reasoner", "reviewer"], "qwen3-coder-30b"),
        ]
    if q14:
        return fast_lane + [(q14, ["utility", "fast_coder", "primary_coder", "deep_reasoner", "reviewer"] if not q4 else ["fast_coder", "primary_coder", "deep_reasoner", "reviewer"], "qwen3-14b")]
    if q30:
        return fast_lane + [(q30, ["utility", "fast_coder", "primary_coder", "deep_reasoner", "reviewer"] if not q4 else ["fast_coder", "primary_coder", "deep_reasoner", "reviewer"], "qwen3-coder-30b")]
    if q4:
        return [(q4, ["utility", "fast_coder", "primary_coder", "deep_reasoner", "reviewer"], "qwen3-4b-instruct")]
    return []


def suggest_model_profiles(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suggest managed llama.cpp profiles from discovered GGUF files.

    This deliberately does not download, rename, or modify model files. The sizing
    heuristic only chooses *roles*; llama.cpp remains responsible for actual GPU/CPU
    fitting at runtime.
    """
    rows: list[dict[str, Any]] = []
    for item in inventory:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        size = max(0.0, float(item.get("size_gb") or 0.0))
        name = str(item.get("name") or Path(path).name)
        rows.append({"path": path, "name": name, "size_gb": size})
    rows.sort(key=lambda x: (x["size_gb"], x["name"].lower()))
    if not rows:
        return []

    assignments: list[tuple[dict[str, Any], list[str], str]] = _catalog_pair_assignments(rows)
    if assignments:
        pass
    elif len(rows) == 1:
        assignments.append((rows[0], ["utility", "fast_coder", "primary_coder", "deep_reasoner", "reviewer"], "primary-local"))
    elif len(rows) == 2:
        assignments.append((rows[0], ["utility", "fast_coder", "primary_coder"], "fast-primary"))
        assignments.append((rows[1], ["deep_reasoner", "reviewer"], "deep-reasoner"))
    else:
        assignments.append((rows[0], ["utility", "fast_coder"], "fast-coder"))
        assignments.append((rows[len(rows) // 2], ["primary_coder"], "primary-coder"))
        assignments.append((rows[-1], ["deep_reasoner", "reviewer"], "deep-reasoner"))

    seen_paths: set[str] = set()
    suggestions: list[dict[str, Any]] = []
    fixed_ports = {"qwen3-4b-instruct": 8080, "qwen3-14b": 8081, "qwen3-coder-30b": 8082}
    port = 8083
    for item, roles, profile_id in assignments:
        if item["path"] in seen_paths:
            # Three-way role assignment can converge on the same file with a tiny
            # inventory. Merge roles rather than creating duplicate runtimes.
            existing = next(x for x in suggestions if x["model_path"] == item["path"])
            existing["roles"] = sorted(set(existing["roles"] + roles))
            continue
        seen_paths.add(item["path"])
        name_lower = item["name"].lower()
        if any(token in name_lower for token in ("vision", "-vl", "_vl", "qwen2-vl", "qwen3-vl")):
            roles = sorted(set(roles + ["vision"]))
        size = item["size_gb"]
        item_port = fixed_ports.get(profile_id)
        if item_port is None:
            item_port = port
            port += 1
        suggestions.append({
            "id": profile_id,
            "runtime": "llama_cpp",
            "endpoint": f"http://127.0.0.1:{item_port}/v1",
            "model": Path(item["path"]).stem,
            "model_path": item["path"],
            "roles": roles,
            "context_window": 8192 if profile_id == "qwen3-4b-instruct" else 32768,
            "max_output_tokens": (
                1024 if profile_id == "qwen3-4b-instruct"
                else 2048 if profile_id == "qwen3-14b"
                else 8192 if profile_id == "qwen3-coder-30b"
                else 4096
            ),
            "tool_calling": True,
            "temperature": 0.6 if profile_id == "qwen3-4b-instruct" else 0.2,
            "vision": "vision" in roles,
            "priority": {
                "fast-coder": 80,
                "fast-primary": 90,
                "primary-coder": 90,
                "deep-reasoner": 100,
                "qwen3-4b-instruct": 95,
                "qwen3-14b": 90,
                "qwen3-coder-30b": 100,
            }.get(profile_id, 80),
            "enabled": True,
            "api_key": "local",
            "gpu_layers": "auto",
            "fit_target_mb": 1024,
            "keep_loaded": profile_id == "qwen3-4b-instruct" and roles == ["utility"],
            "allow_cpu_offload": True,
            # Estimates are intentionally conservative routing hints, not hard
            # requirements. Runtime auto-fit may choose a different split.
            "estimated_vram_gb": round(max(2.0, size * 1.08), 1),
            "estimated_ram_gb": round(max(4.0, size * 1.35 + 2.0), 1),
            "extra_args": ["--reasoning", "off"] if profile_id == "qwen3-14b" else [],
            "notes": f"Auto-suggested from local GGUF: {item['name']}. Review before use.",
        })
    return suggestions


def write_suggested_models(config_path: Path, suggestions: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist explicit user-approved model suggestions while preserving other config."""
    path = config_path.expanduser().resolve()
    if not suggestions:
        raise ValueError("No model suggestions are available to save.")
    raw: dict[str, Any] = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(raw, dict):
            raise ValueError("Config JSON root must be an object.")
    raw["models"] = suggestions
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    replace_with_retry(tmp, path)
    return {
        "path": str(path),
        "models": len(suggestions),
        "restart_required": False,
    }
