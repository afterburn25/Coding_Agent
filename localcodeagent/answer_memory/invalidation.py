"""Dependency-aware invalidation.

Answers can depend on application state, configuration, or repository
revisions. When the dependency fingerprint drifts we downgrade the answer to
``stale`` (verification required) rather than silently deleting it — the next
model-produced answer refreshes it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from . import ttl


def config_fingerprint(config: Any) -> str:
    """Fingerprint the config surface that learned answers plausibly depend on.

    Only a curated subset is hashed — unrelated edits (theme, timeouts) must
    not invalidate knowledge.
    """
    if config is None:
        return ""
    keys = [
        "image_models",
        "models",
        "research_enabled",
        "auto_research_unknown",
        "performance_mode",
        "image_default_t2i_model",
    ]
    blob: dict[str, Any] = {}
    for key in keys:
        value = getattr(config, key, None)
        try:
            blob[key] = json.loads(json.dumps(value, default=str))
        except Exception:
            blob[key] = str(value)
    digest = hashlib.sha256(
        json.dumps(blob, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def repo_head(workspace: str | Path) -> str:
    """Current HEAD commit for a workspace, without spawning git when avoidable."""
    root = Path(workspace)
    git = root / ".git"
    try:
        if git.is_dir():
            head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            if head.startswith("ref:"):
                ref = head.split(":", 1)[1].strip()
                ref_path = git / ref
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()[:40]
                packed = git / "packed-refs"
                if packed.exists():
                    for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.endswith(ref):
                            return line.split(" ", 1)[0][:40]
            elif head:
                return head[:40]
            return ""
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return head.stdout.strip()[:40] if head.returncode == 0 else ""
    except Exception:
        return ""


def dependency_status(row: dict, *, config_fp: str = "", repo_head_sha: str = "") -> str:
    """Return 'current' | 'stale' | 'expired' for a stored answer row."""
    now = time.time()
    if ttl.is_expired(row, now):
        return "expired"
    freshness = row.get("freshness") or "static"
    if freshness == "configuration_dependent" and config_fp:
        stored = row.get("config_fingerprint") or ""
        if stored and stored != config_fp:
            return "stale"
    if freshness in {"application_state", "repository_dependent"} and repo_head_sha:
        stored_sha = row.get("git_commit") or ""
        if stored_sha and stored_sha != repo_head_sha and freshness == "repository_dependent":
            return "stale"
    if freshness == "application_state" and config_fp:
        stored = row.get("config_fingerprint") or ""
        if stored and stored != config_fp:
            return "stale"
    return "current"


def mark_stale_fields(row: dict) -> dict[str, Any]:
    return {
        "trust_state": "stale",
        "invalidation_reason": "dependency fingerprint changed — verification required",
    }
