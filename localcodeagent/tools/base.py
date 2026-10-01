from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..permissions import PermissionManager

# Canonical plugin/tool categories surfaced by the Tool Manager UI.
TOOL_CATEGORIES = [
    "ai_models",
    "coding",
    "browsers",
    "research",
    "images",
    "audio",
    "video",
    "documents",
    "data",
    "devops",
    "git",
    "3d",
    "utilities",
    "external_apis",
    "mcp",
]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    permission: str
    handler: Callable[[dict[str, Any]], str]

    # Modular tool manifest metadata. All fields are optional so the original
    # (name, description, parameters, permission, handler) calling convention
    # keeps working for existing register_* helpers and manifests.
    tool_id: str = ""
    display_name: str = ""
    category: str = "utilities"
    version: str = "builtin"
    provider: str = "chat-nexus"
    capabilities: list[str] = field(default_factory=list)
    permissions_required: list[str] = field(default_factory=list)
    requires_network: bool = False
    requires_gpu: bool = False
    requirements: dict[str, Any] = field(default_factory=dict)
    supported_os: list[str] = field(default_factory=list)  # empty = all
    docs: str = ""
    source: str = "builtin"  # builtin | manifest | mcp
    install_status: str = "installed"  # installed | missing | not_applicable
    health_check: Callable[[], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.tool_id:
            self.tool_id = self.name
        if not self.display_name:
            self.display_name = self.name.replace("_", " ").title()
        if not self.capabilities:
            self.capabilities = [self.name]
        if not self.permissions_required:
            self.permissions_required = [self.permission] if self.permission else []

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Central tool registry.

    Tools register a `ToolSpec` manifest. The agent queries the registry for
    schemas/capabilities instead of hard-coding tool knowledge; execution always
    passes through the permission manager and the enabled/disabled gate.
    """

    def __init__(self, permissions, *, state_path: Path | None = None) -> None:
        if isinstance(permissions, PermissionManager):
            self.permission_manager = permissions
            self.permissions = permissions.permissions
        else:
            self.permissions = permissions
            self.permission_manager = PermissionManager(permissions)
        self._tools: dict[str, ToolSpec] = {}
        self._disabled: set[str] = set()
        self._usage: dict[str, dict[str, Any]] = {}
        # Registry-owned metadata for manifest/plugin tools (invocable flag,
        # install spec, manifest path). Built-in tools have no entry.
        self._plugin_meta: dict[str, dict[str, Any]] = {}
        # Cached health-check results (populated by health()).
        self._health_cache: dict[str, dict[str, Any]] = {}
        # Cached on-disk sizes for installed payloads: name -> (signature, bytes)
        self._size_cache: dict[str, tuple[float, int]] = {}
        # Install root for manifest `detect.files` markers (set by
        # load_plugin_manifests); used when refreshing install status.
        self.install_root: Path | None = None
        # Optional hook: tool_id -> cached update-probe result (see
        # tools.updates.ToolUpdateChecker). Wired by AppState.
        self.update_lookup: Any = None
        self._lock = threading.RLock()
        self.state_path = Path(state_path).resolve() if state_path else None
        # Mutable execution context is set by the orchestrator before tool calls.
        self.context: dict[str, Any] = {}
        self._load_state()

    def register(self, spec: ToolSpec) -> None:
        from .manifests import manifest_defaults

        for key, value in manifest_defaults(spec.name).items():
            if key == "capabilities":
                spec.capabilities = sorted({*spec.capabilities, *value})
                continue
            current = getattr(spec, key, None)
            if current in (None, "", [], {}, "utilities", "builtin", "chat-nexus", False):
                setattr(spec, key, value)
        with self._lock:
            self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            t.openai_schema()
            for t in self._tools.values()
            if t.name not in self._disabled
            and self._plugin_meta.get(t.name, {}).get("invocable", True)
            and t.install_status != "missing"
        ]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def find_by_capability(self, capability: str) -> list[str]:
        cap = str(capability or "").strip().lower()
        if not cap:
            return []
        return sorted(
            name
            for name, spec in self._tools.items()
            if cap in {str(c).lower() for c in spec.capabilities}
        )

    def refresh_install_status(self) -> dict[str, str]:
        """Re-scan manifest tools for executable presence (post-install/update)."""
        changed: dict[str, str] = {}
        from .plugins import PluginManifest
        for name, meta in self._plugin_meta.items():
            manifest_path = meta.get("manifest_path")
            spec = self._tools.get(name)
            if not manifest_path or spec is None:
                continue
            try:
                manifest = PluginManifest.from_dict(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            new_status = "installed" if manifest.is_installed(self.install_root) else "missing"
            if spec.install_status != new_status:
                spec.install_status = new_status
                changed[name] = new_status
        return changed

    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._tools:
            raise KeyError(name)
        with self._lock:
            if enabled:
                self._disabled.discard(name)
            else:
                self._disabled.add(name)
            self._save_state()

    def permission_for(self, name: str) -> tuple[str, str]:
        tool = self._tools.get(name)
        if tool is None:
            return "", "deny"
        return tool.permission, self.permission_manager.effective(tool.permission)

    def requires_approval(self, name: str) -> tuple[bool, str]:
        permission, mode = self.permission_for(name)
        return mode == "ask", permission

    def health(self, name: str) -> dict[str, Any]:
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "status": "missing", "detail": "unknown tool"}
        if spec.health_check is None:
            return {"ok": True, "status": "unknown", "detail": "no health check registered"}
        try:
            result = spec.health_check() or {}
        except Exception as exc:
            result = {"ok": False, "status": "error", "detail": f"{type(exc).__name__}: {exc}"}
        result.setdefault("ok", True)
        result.setdefault("status", "healthy" if result["ok"] else "unhealthy")
        result.setdefault("detail", "")
        self._health_cache[name] = {"result": dict(result), "at": time.time()}
        self.permission_manager.record_use(spec.permission)
        return result

    def installed_version(self, name: str) -> str:
        """Version recorded in an archive install's .chatnexus-version marker."""
        spec = self._tools.get(name)
        if spec is None or spec.install_status != "installed":
            return ""
        install = self._plugin_meta.get(name, {}).get("install") or {}
        if str(install.get("method") or "").lower() != "archive":
            return ""
        dest = str(install.get("dest") or spec.tool_id)
        try:
            marker = (self.install_root / dest).resolve() / ".chatnexus-version"
            if not marker.is_relative_to(self.install_root) or not marker.is_file():
                return ""
            return marker.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
        except (OSError, IndexError):
            return ""

    def installed_at(self, name: str) -> float | None:
        """Install timestamp from the archive marker's mtime, if present."""
        spec = self._tools.get(name)
        if spec is None or spec.install_status != "installed" or not self.install_root:
            return None
        install = self._plugin_meta.get(name, {}).get("install") or {}
        if str(install.get("method") or "").lower() != "archive":
            return None
        dest = str(install.get("dest") or spec.tool_id)
        try:
            marker = (self.install_root / dest).resolve() / ".chatnexus-version"
            if marker.is_relative_to(self.install_root) and marker.is_file():
                return marker.stat().st_mtime
        except (OSError, ValueError):
            pass
        return None

    def install_path(self, name: str) -> str:
        """Resolved on-disk install location, when one is known."""
        spec = self._tools.get(name)
        meta = self._plugin_meta.get(name, {})
        install = meta.get("install") or {}
        if spec is None:
            return ""
        root = self.install_root
        # Prefer the common top-level dir declared by detect.files — e.g.
        # `ComfyUI_windows_portable/...` resolves to that payload directory even
        # when install.dest is the app root itself.
        segments = {
            str(rel).replace("\\", "/").split("/", 1)[0]
            for rel in meta.get("detect_files") or [] if rel
        }
        if len(segments) == 1 and root:
            payload = (root / segments.pop()).resolve()
            try:
                if payload.is_relative_to(root) and payload.is_dir():
                    return str(payload)
            except (OSError, ValueError):
                pass
        if str(install.get("method") or "").lower() == "archive" and root:
            dest = str(install.get("dest") or spec.tool_id)
            try:
                resolved = (root / dest).resolve()
                if resolved.is_relative_to(root):
                    return str(resolved)
            except (OSError, ValueError):
                return ""
        for rel in meta.get("detect_files") or []:
            p = Path(rel).expanduser()
            target = p if p.is_absolute() else (root / p if root else p)
            if target.exists():
                return str(target.resolve().parent)
        for exe in meta.get("executables") or []:
            found = shutil.which(str(exe))
            if found:
                return str(Path(found).resolve().parent)
        return ""

    def install_size(self, name: str) -> int:
        """Bytes on disk for the installed payload (cached; 0 if unknown)."""
        spec = self._tools.get(name)
        meta = self._plugin_meta.get(name, {})
        install = meta.get("install") or {}
        if spec is None:
            return 0
        if spec.install_status != "installed":
            return int(install.get("size_bytes") or 0)
        path = self.install_path(name)
        if not path:
            return int(install.get("size_bytes") or 0)
        dest = Path(path)
        if not dest.is_dir():
            try:
                return dest.stat().st_size
            except OSError:
                return 0
        try:
            signature = max((p.stat().st_mtime for p in [dest, *dest.iterdir()]
                            if p.exists()), default=0.0)
        except OSError:
            signature = 0.0
        cached = self._size_cache.get(name)
        if cached and cached[0] == signature:
            return cached[1]
        # Bound the walk: a tool payload can sit inside a very large tree
        # (e.g. ComfyUI portable) — without a cap, manifests() hangs startup
        # for minutes while it stats every file.
        deadline = time.monotonic() + 0.75
        visited = 0
        aborted = False
        total = 0
        try:
            stack = [dest]
            while stack and not aborted:
                for entry in os.scandir(stack.pop()):
                    visited += 1
                    if visited > 25000 or time.monotonic() > deadline:
                        aborted = True
                        break
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
        except OSError:
            pass
        if aborted:
            # Don't cache a truncated figure as if it were real.
            return 0
        self._size_cache[name] = (signature, total)
        self._save_state()
        return total

    def manifest(self, name: str) -> dict[str, Any]:
        # plugins imports this module — keep the uninstall_command lookup lazy.
        from .plugins import install_command, uninstall_command

        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(name)
        permission, mode = self.permission_for(name)
        usage = self._usage.get(name) or {}
        installed_version = self.installed_version(name)
        install = self._plugin_meta.get(name, {}).get("install") or {}
        update = self.update_lookup(spec.tool_id) if self.update_lookup else {}
        latest = (update.get("latest") if update.get("status") == "checked"
                  else None) or spec.version or ""
        return {
            "id": spec.tool_id,
            "name": spec.name,
            "display_name": spec.display_name,
            "description": spec.description,
            "category": spec.category,
            "version": spec.version,
            "provider": spec.provider,
            "capabilities": list(spec.capabilities),
            "input_schema": spec.parameters,
            "permission": permission,
            "permission_mode": mode,
            "permissions_required": list(spec.permissions_required),
            "requires_network": bool(spec.requires_network),
            "requires_gpu": bool(spec.requires_gpu),
            "requirements": dict(spec.requirements),
            "supported_os": list(spec.supported_os),
            "os_supported": (
                not spec.supported_os
                or ("windows" if sys.platform.startswith("win")
                    else "linux" if sys.platform.startswith("linux")
                    else sys.platform) in {s.lower() for s in spec.supported_os}
                or sys.platform in {s.lower() for s in spec.supported_os}
            ),
            "docs": spec.docs,
            "source": spec.source,
            "install_status": spec.install_status,
            "installed_version": installed_version,
            "update_available": bool(
                installed_version and latest
                and installed_version != latest),
            "update_check": update.get("status") or "",
            "update_checked_at": update.get("checked_at"),
            "enabled": name not in self._disabled,
            "has_health_check": spec.health_check is not None,
            "use_count": int(usage.get("count", 0)),
            "last_used_at": usage.get("last_used_at"),
            "callable": bool(self._plugin_meta.get(name, {}).get("invocable", True)),
            "install": dict(install),
            "installable": (
                str(install.get("method") or "").strip().lower() == "archive"
                or install_command(install, install_root=self.install_root)
                is not None
            ),
            "removable": (
                str(install.get("method") or "").strip().lower() == "archive"
                or uninstall_command(install, install_root=self.install_root)
                is not None
            ),
            "manifest_path": str(self._plugin_meta.get(name, {}).get("manifest_path") or ""),
            "process_id": str(self._plugin_meta.get(name, {}).get("process") or ""),
            "dependencies": list(self._plugin_meta.get(name, {}).get("dependencies") or []),
            "executables": list(self._plugin_meta.get(name, {}).get("executables") or []),
            "detect_files": list(self._plugin_meta.get(name, {}).get("detect_files") or []),
            "install_path": self.install_path(name),
            "install_size_bytes": self.install_size(name),
            "installed_at": self.installed_at(name),
            "latest_version": str(latest),
            "health": dict(self._health_cache.get(name) or {}),
            "mcp_server": str(self._plugin_meta.get(name, {}).get("mcp_server") or ""),
        }

    def manifests(self) -> list[dict[str, Any]]:
        rows = [self.manifest(name) for name in self._tools]
        return sorted(rows, key=lambda m: (m["category"], m["display_name"]))

    def execute(self, name: str, arguments: dict[str, Any], *, approved: bool = False) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"ERROR: unknown tool '{name}'"
        if name in self._disabled:
            return f"TOOL_DISABLED: tool '{name}' is disabled in the Tool Manager"
        if tool.install_status == "missing":
            return f"TOOL_NOT_INSTALLED: '{name}' is not installed — see the Tool Manager for install options"
        manager = self.permission_manager
        mode = manager.effective(tool.permission)
        if mode == "deny":
            manager.record_event("denied", tool.permission, name)
            return f"PERMISSION_DENIED: {tool.permission} is disabled"
        if mode in ("ask", "creator") and not approved:
            manager.record_event("approval_requested", tool.permission, name)
            extra = " (creator approval required)" if mode == "creator" else ""
            return f"APPROVAL_REQUIRED: permission '{tool.permission}' must be approved by the user before running {name}{extra}"
        if approved and manager.level(tool.permission) == "creator" and not manager.creator_ok():
            manager.record_event("creator_required", tool.permission, name)
            return ("CREATOR_APPROVAL_REQUIRED: permission "
                    f"'{tool.permission}' requires an unlocked Nexus Brain creator session")
        if mode == "creator":
            manager.record_event("approved", tool.permission, name)
        elif approved and mode == "ask":
            manager.record_event("approved", tool.permission, name)
        if approved and manager.level(tool.permission) == "session":
            manager.grant_session(tool.permission)
        # Domain/scope enforcement: any URL argument is checked against the
        # permission's configured allowed/blocked domain list.
        for key, value in arguments.items():
            if isinstance(value, str) and "url" in key.lower() and "://" in value:
                reason = manager.check_url(tool.permission, value)
                if reason:
                    manager.record_event("scope_denied", tool.permission, name)
                    return f"SCOPE_DENIED: {reason}"
        manager.record_use(tool.permission)
        started = time.time()
        try:
            result = tool.handler(arguments)
        except Exception as exc:
            result = f"ERROR: {type(exc).__name__}: {exc}"
            self._emit({"tool": name, "ok": False, "elapsed_seconds": round(time.time() - started, 3),
                        "detail": result[:200]})
            return result
        with self._lock:
            usage = self._usage.setdefault(name, {"count": 0, "last_used_at": None})
            usage["count"] = int(usage.get("count", 0)) + 1
            usage["last_used_at"] = time.time()
        self._emit({"tool": name, "ok": not result.startswith(("ERROR", "PERMISSION", "TOOL_", "APPROVAL")),
                    "elapsed_seconds": round(time.time() - started, 3)})
        return result

    def _emit(self, payload: dict[str, Any]) -> None:
        """Optional event callback — wired to the server EventBus."""
        emitter = getattr(self, "on_event", None)
        if emitter is None:
            return
        try:
            emitter(payload)
        except Exception:
            pass

    # -- persisted tool state ------------------------------------------------

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._disabled = {str(x) for x in raw.get("disabled", [])}
            usage = raw.get("usage", {})
            if isinstance(usage, dict):
                self._usage = {
                    str(k): {"count": int(v.get("count", 0)), "last_used_at": v.get("last_used_at")}
                    for k, v in usage.items()
                    if isinstance(v, dict)
                }
            sizes = raw.get("sizes", {})
            if isinstance(sizes, dict):
                self._size_cache = {
                    str(k): (float(v[0]), int(v[1]))
                    for k, v in sizes.items()
                    if isinstance(v, (list, tuple)) and len(v) == 2
                }

    def _save_state(self) -> None:
        if not self.state_path:
            return
        payload = {
            "version": 1,
            "disabled": sorted(self._disabled),
            "usage": self._usage,
            "sizes": {k: [s, b] for k, (s, b) in self._size_cache.items()},
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass
