from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from ..config import ModelProfile
from ..fsutil import atomic_write_text
from typing import Any


# Known tier-catalog files → (profile id, ladder tier, base roles).
# Ordered smallest → largest; roles follow docs/HARDWARE_MODEL_TIERS.md.
_TIER_FILES: tuple[tuple[str, str, int, list[str]], ...] = (
    ("qwen_qwen3-4b-instruct-2507-q4_k_m.gguf", "qwen3-4b-instruct", 1,
     ["utility"]),
    ("qwen3-8b-q4_k_m.gguf", "qwen3-8b", 2,
     ["lightweight_reasoner", "light_coder", "general_assistant"]),
    ("qwen3-14b-q4_k_m.gguf", "qwen3-14b", 3,
     ["fast_coder", "primary_coder"]),
    ("qwen3-coder-30b-a3b-instruct-q4_k_m.gguf", "qwen3-coder-30b", 4,
     ["deep_reasoner", "reviewer"]),
)

# Roles every usable coding config must cover somewhere.
_CORE_ROLES = ("fast_coder", "primary_coder", "deep_reasoner", "reviewer")


def _catalog_pair_assignments(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str], str]]:
    """Prefer the known Nexus Core 4B/8B/14B/30B tier files when present.

    Coverage rules: the utility lane lands on the smallest present tier;
    every other uncovered core role is absorbed by the largest present
    tier — a lone model ends up serving everything, and partial stacks
    degrade toward their biggest model."""
    by_name = {str(row["name"]).lower(): row for row in rows}
    present = [
        (by_name[fname], list(roles), pid)
        for fname, pid, _tier, roles in _TIER_FILES
        if fname in by_name
    ]
    if not present:
        return []
    if len(present) == 1:
        row, roles, pid = present[0]
        return [(row, ["utility", *_CORE_ROLES], pid)]
    if "utility" not in {r for _row, roles, _pid in present for r in roles}:
        present[0][1].append("utility")           # smallest present tier
    covered = {r for _row, roles, _pid in present for r in roles}
    missing = [r for r in _CORE_ROLES if r not in covered]
    if missing:
        present[-1][1].extend(missing)            # largest present tier
    return present


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
    fixed_ports = {"qwen3-4b-instruct": 8080, "qwen3-14b": 8081,
                   "qwen3-coder-30b": 8082, "qwen3-8b": 8084}
    port = 8085
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
            "context_window": (
                8192 if profile_id == "qwen3-4b-instruct"
                else 12288 if profile_id == "qwen3-8b"
                else 32768
            ),
            "max_output_tokens": (
                1024 if profile_id == "qwen3-4b-instruct"
                else 2048 if profile_id == "qwen3-8b"
                else 2048 if profile_id == "qwen3-14b"
                else 8192 if profile_id == "qwen3-coder-30b"
                else 4096
            ),
            "tool_calling": True,
            "temperature": (
                0.6 if profile_id == "qwen3-4b-instruct"
                else 0.4 if profile_id == "qwen3-8b"
                else 0.2
            ),
            "vision": "vision" in roles,
            "priority": {
                "fast-coder": 80,
                "fast-primary": 90,
                "primary-coder": 90,
                "deep-reasoner": 100,
                "qwen3-4b-instruct": 95,
                "qwen3-8b": 93,
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
            "extra_args": (["--reasoning", "off"]
                          if profile_id in {"qwen3-8b", "qwen3-14b"} else []),
            "notes": f"Auto-suggested from local GGUF: {item['name']}. Review before use.",
        })
    return suggestions


_PROFILE_FIELDS = {f.name for f in fields(ModelProfile)}


def apply_detected_models(
    models: list[ModelProfile],
    inventory: list[dict[str, Any]],
    base_dir: Path,
) -> dict[str, list[str]]:
    """Autodetect installed GGUFs against the configured profile list.

    - A discovered file with no profile gets one (roles from the tier
      suggestion so the new model lands on the right complexity lane).
    - An enabled profile whose file supplies an otherwise-uncovered role
      absorbs that role — e.g. deleting the 30B hands deep_reasoner to
      the largest remaining model instead of leaving the lane dead.
    - An enabled llama_cpp profile whose file no longer exists is
      disabled (marked ``auto_disabled``) so the router can never select
      a dead endpoint; if the file reappears the profile is re-enabled.
    - User-disabled profiles are never re-enabled or role-edited: a user
      who turned a model off stays off.

    Mutates ``models`` in place; callers persist when the returned
    summary is non-empty.
    """
    changes: dict[str, list[str]] = {
        "added": [], "roles_merged": [], "disabled": [], "reenabled": []}
    base = Path(base_dir).resolve()
    by_name = {
        Path(str(m.model_path or "")).name.lower(): m
        for m in models
        if m.runtime == "llama_cpp" and m.model_path
    }

    # Missing files first: a dead profile must be disabled BEFORE coverage
    # is measured, or its orphaned roles would look served and never
    # migrate to the models that remain.
    disk_names = {
        Path(str(row.get("path") or "")).name.lower()
        for row in inventory if row.get("path")}
    for m in models:
        if m.runtime != "llama_cpp" or not m.model_path:
            continue
        mp = Path(str(m.model_path))
        present = mp.name.lower() in disk_names
        if not present:
            candidates = [mp] if mp.is_absolute() else [
                base / mp, base / "models" / mp.name]
            present = any(c.is_file() for c in candidates)
        if present:
            if not m.enabled and m.auto_disabled:
                # The file came back (download finished, drive remounted) —
                # restore only profiles autodetect itself turned off.
                m.enabled = True
                m.auto_disabled = False
                changes["reenabled"].append(m.id)
        elif m.enabled:
            m.enabled = False
            m.auto_disabled = True
            changes["disabled"].append(m.id)

    covered = {
        r for m in models if m.enabled for r in (m.roles or [])}

    for sug in suggest_model_profiles(inventory):
        key = Path(str(sug.get("model_path") or "")).name.lower()
        roles = [str(r) for r in (sug.get("roles") or [])]
        existing = by_name.get(key)
        if existing is not None:
            if not existing.enabled:
                continue
            missing = [r for r in roles if r not in covered]
            if missing:
                existing.roles = sorted(set(existing.roles or []) | set(missing))
                covered.update(missing)
                changes["roles_merged"].append(existing.id)
            continue
        payload = {k: v for k, v in sug.items() if k in _PROFILE_FIELDS}
        try:
            payload["model_path"] = (
                Path(str(sug["model_path"])).resolve()
                .relative_to(base).as_posix())
        except (ValueError, OSError):
            pass
        profile = ModelProfile(**payload)
        models.append(profile)
        by_name[key] = profile
        covered.update(roles)
        changes["added"].append(profile.id)
    return changes


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
    atomic_write_text(path, json.dumps(raw, indent=2) + "\n")
    return {
        "path": str(path),
        "models": len(suggestions),
        "restart_required": False,
    }
