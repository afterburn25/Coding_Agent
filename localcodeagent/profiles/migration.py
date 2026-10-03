"""Legacy-state migration — runs once, idempotently, on first profile.

Existing installs keep EVERYTHING: chats, memory, models, ComfyUI,
voice assets, settings, project state — none of it is moved or copied
(huge shared assets are never duplicated per profile). What changes is
association: the first created profile is recorded as the owner of the
legacy user state, and a schema_version marker lets future migrations
know the baseline has already run.

Safe to run any number of times: if the marker exists it returns
immediately.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def migrate_legacy_state(profiles_root: Path, first_profile_id: str,
                         *, runtime_root: Path | None = None) -> dict:
    """Associate pre-profile legacy state with the first profile.

    Returns the marker dict — ``{"already_migrated": True}`` flag on
    repeat runs.
    """
    root = Path(profiles_root)
    marker_path = root / "migration.json"
    try:
        existing = json.loads(marker_path.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("schema_version"):
            out = dict(existing)
            out["already_migrated"] = True
            return out
    except Exception:
        pass

    runtime = Path(runtime_root) if runtime_root else root.parent.parent
    preserved = []
    try:
        for p in sorted(runtime.iterdir()):
            if p.is_dir() and p.name in {
                "conversations", "memory", "models", "comfyui",
                "voice", "data", "chats", "settings",
            }:
                preserved.append(p.name)
    except OSError:
        pass

    marker = {
        "schema_version": SCHEMA_VERSION,
        "migrated_at": time.time(),
        "legacy_profile": str(first_profile_id),
        "preserved_paths": preserved,
        "note": "legacy state preserved in place — associated, not "
                "moved or copied",
    }
    root.mkdir(parents=True, exist_ok=True)
    tmp = marker_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    tmp.replace(marker_path)
    return dict(marker)
