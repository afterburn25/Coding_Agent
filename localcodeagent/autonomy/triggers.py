"""Trigger Engine — persistent, event-driven signal → mission bindings.

Trigger record:
    {id, name, event, conditions, action, enabled, debounce_s,
     last_fired, fire_count, watch, created_by}

`event` is a signal name (see SIGNALS). `conditions` is a flat
{key: expected} map — every key must equal-match the fired payload
(no code execution, no expressions). `action` describes what to start:
    {"kind": "mission", "objective": ..., "scope": ...}
    {"kind": "standing_goal", "goal_id": ...}

file_changed triggers carry `watch` (a file or directory path) and are
polled cheaply on supervisor ticks — never whole-drive scans.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# Signals emitted internally or adapted from the event bus.
SIGNALS = {
    "schedule_due", "interval", "startup", "application_idle",
    "file_changed", "repository_changed", "branch_changed",
    "ci_completed", "ci_failed", "github_issue_created",
    "pull_request_updated", "download_completed",
    "image_generation_completed", "model_install_completed",
    "model_runtime_failed", "backend_crashed", "disk_space_threshold",
    "memory_threshold", "test_failure", "stale_knowledge", "custom",
}

# Event-bus event types mapped onto autonomy signal names.
BUS_SIGNAL_MAP = {
    "process": {
        "auto_restart_failed": "backend_crashed",
        "auto_restart": "backend_crashed",
    },
    "job": {"failed": "job_failed"},
    "image_job": {"finished": "image_generation_completed",
                  "failed": "image_generation_completed"},
}


class TriggerEngine:
    def __init__(self, store, *,
                 on_fire: Callable[[dict, dict], None] | None = None,
                 workspace: Path | None = None) -> None:
        self._store = store
        self.on_fire = on_fire
        self.workspace = Path(workspace) if workspace else None
        self._lock = threading.RLock()
        self._fired_at: dict[str, float] = {}
        self._watch_state: dict[str, float] = {}  # watch path -> last mtime

    # -- CRUD -----------------------------------------------------------

    def add(self, name: str, event: str, *, conditions: dict | None = None,
            action: dict | None = None, debounce_s: float = 60.0,
            watch: str = "", enabled: bool = True,
            created_by: str = "user") -> dict:
        if event not in SIGNALS:
            raise ValueError(f"unknown trigger event '{event}'")
        row = {
            "id": f"tr-{uuid.uuid4().hex[:10]}",
            "name": str(name or event)[:140],
            "event": event,
            "conditions": {str(k): str(v) for k, v in (conditions or {}).items()},
            "action": dict(action or {"kind": "mission", "objective": name}),
            "watch": str(watch or ""),
            "enabled": bool(enabled),
            "debounce_s": max(0.0, float(debounce_s)),
            "last_fired": None,
            "fire_count": 0,
            "created_by": created_by,
            "created_at": time.time(),
        }
        with self._lock:
            self._store.triggers.data.setdefault("triggers", []).append(row)
            self._store.triggers.save()
        return dict(row)

    def remove(self, trigger_id: str) -> bool:
        with self._lock:
            rows = self._store.triggers.data.setdefault("triggers", [])
            for i, r in enumerate(rows):
                if r.get("id") == trigger_id:
                    rows.pop(i)
                    self._store.triggers.save()
                    return True
        return False

    def set_enabled(self, trigger_id: str, enabled: bool) -> bool:
        with self._lock:
            for r in self._store.triggers.data.setdefault("triggers", []):
                if r.get("id") == trigger_id:
                    r["enabled"] = bool(enabled)
                    self._store.triggers.save()
                    return True
        return False

    def list(self) -> list[dict]:
        return [dict(r) for r in self._store.triggers.rows()]

    # -- firing ----------------------------------------------------------

    def _conditions_met(self, trigger: dict, payload: dict) -> bool:
        flat = {str(k): str(v) for k, v in payload.items()
                if isinstance(v, (str, int, float, bool))}
        for k, expected in (trigger.get("conditions") or {}).items():
            if flat.get(str(k)) != str(expected):
                return False
        return True

    def _debounced(self, trigger: dict) -> bool:
        last = self._fired_at.get(trigger["id"], 0.0)
        return time.time() - last < float(trigger.get("debounce_s") or 0)

    def fire(self, signal: str, payload: dict | None = None) -> list[dict]:
        """Match enabled triggers for a signal and run their actions.
        Returns the triggers that fired (post-debounce)."""
        payload = dict(payload or {})
        payload.setdefault("signal", signal)
        fired: list[dict] = []
        with self._lock:
            for trig in self._store.triggers.data.setdefault("triggers", []):
                if not trig.get("enabled") or trig.get("event") != signal:
                    continue
                if not self._conditions_met(trig, payload):
                    continue
                if self._debounced(trig):
                    continue
                trig["last_fired"] = time.time()
                trig["fire_count"] = int(trig.get("fire_count") or 0) + 1
                self._fired_at[trig["id"]] = time.time()
                fired.append(dict(trig))
            if fired:
                self._store.triggers.save()
        for trig in fired:
            if self.on_fire is not None:
                try:
                    self.on_fire(trig, payload)
                except Exception:
                    pass
        return fired

    def handle_bus_event(self, event: dict) -> None:
        """Adapt an EventBus event into zero or more autonomy signals."""
        etype = str(event.get("type") or "")
        inner = event.get("event")
        if etype == "process":
            ev = str((event.get("event") if isinstance(inner, str) else event.get("event", "")))
        mapping = BUS_SIGNAL_MAP.get(etype, {})
        # For tool/job events the interesting sub-state lives in the payload.
        sub = str(event.get("state") or event.get("status") or "")
        signal = None
        if etype == "process":
            ev_name = str(event.get("event") or "")
            signal = mapping.get(ev_name)
        elif etype == "image_job":
            signal = "image_generation_completed"
            if sub in {"failed", "error"}:
                signal = "image_generation_completed"
        elif etype == "ci":
            # GitHub workflow-run observations — the closing edge of the
            # git→push→PR→CI loop. Published by the github tools when a
            # run reaches a terminal conclusion.
            conclusion = str(event.get("conclusion") or "").lower()
            if conclusion in {"success"}:
                signal = "ci_completed"
            elif conclusion in {"failure", "timed_out", "cancelled",
                                "action_required"}:
                signal = "ci_failed"
        elif etype == "pull_request":
            signal = "pull_request_updated"
        elif etype == "social":
            # SocialService events — a peer reply can release missions
            # parked on an external-wait dependency.
            if str(event.get("event") or "") == "peer_reply":
                signal = "social_reply"
        elif etype == "job" and sub == "failed":
            signal = "custom"
        if signal:
            self.fire(signal, dict(event))

    # -- file watching ----------------------------------------------------

    _WATCH_MAX_FILES = 5000
    _WATCH_MAX_DEPTH = 8
    _WATCH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv",
                        "venv", "dist", "build"}

    def _tree_mtime(self, root: Path) -> float:
        """Newest file mtime under `root`, recursive but bounded.

        Replaces a one-level glob so edits in nested project files fire
        the watch. Bounded by file count and depth — a huge tree falls
        back to the dir's own mtime signal (which still shifts when
        entries appear or vanish)."""
        import os
        newest = 0.0
        seen = 0
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and seen < self._WATCH_MAX_FILES:
            current, depth = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                seen += 1
                if seen >= self._WATCH_MAX_FILES:
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if (depth < self._WATCH_MAX_DEPTH
                                and entry.name not in self._WATCH_SKIP_DIRS):
                            stack.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        newest = max(newest, entry.stat(
                            follow_symlinks=False).st_mtime)
                except OSError:
                    continue
        return newest

    def check_watches(self) -> list[dict]:
        """Poll configured file_changed watches (scoped paths only) — cheap
        mtime check per trigger, debounced by the trigger record itself."""
        fired: list[dict] = []
        with self._lock:
            watch_trigs = [t for t in self._store.triggers.data.setdefault("triggers", [])
                           if t.get("enabled") and t.get("event") == "file_changed"
                           and t.get("watch")]
        for trig in watch_trigs:
            path = Path(trig["watch"])
            if self.workspace is not None and not path.is_absolute():
                path = self.workspace / path
            try:
                path = path.resolve()
            except OSError:
                continue
            # Confine watches to the workspace — no whole-drive scans.
            if self.workspace is not None:
                try:
                    path.relative_to(self.workspace.resolve())
                except ValueError:
                    continue
            try:
                if path.is_dir():
                    mtime = self._tree_mtime(path)
                else:
                    mtime = path.stat().st_mtime
            except OSError:
                continue
            key = trig["id"]
            prev = self._watch_state.get(key)
            if prev is None:
                self._watch_state[key] = mtime
                continue
            if mtime > prev:
                self._watch_state[key] = mtime
                fired += self.fire("file_changed",
                                 {"path": str(path), "trigger_id": key})
        return fired
