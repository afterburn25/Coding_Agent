from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

# Declarative tool/plugin manifest format (JSON). Manifests let external tools
# register install/health/capability metadata — and optionally an executable
# invoker — without writing Python. Example:
#
# {
#   "id": "ffmpeg",
#   "name": "FFmpeg",
#   "version": "1.0.0",
#   "category": "video",
#   "provider": "ffmpeg-project",
#   "executables": ["ffmpeg", "ffprobe"],
#   "capabilities": ["convert_video", "trim_video"],
#   "permissions": ["shell.execute"],
#   "requires_gpu": false,
#   "supported_os": ["windows"],
#   "install": {"method": "winget", "package": "Gyan.FFmpeg"},
#   "health_check": {"command": ["ffmpeg", "-version"]},
#   "invoke": {"command": ["ffmpeg", "-hide_banner", "{args}"],
#              "input_schema": {"type": "object", "properties": {"args": {"type": "array"}}},
#              "timeout_seconds": 300}
# }

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass(slots=True)
class PluginManifest:
    id: str
    name: str
    version: str = "0"
    category: str = "utilities"
    description: str = ""
    provider: str = "external"
    executables: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    requires_network: bool = False
    requires_gpu: bool = False
    requirements: dict[str, Any] = field(default_factory=dict)
    supported_os: list[str] = field(default_factory=list)
    docs: str = ""
    install: dict[str, Any] = field(default_factory=dict)
    health_check: dict[str, Any] = field(default_factory=dict)
    invoke: dict[str, Any] | None = None
    config: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    @staticmethod
    def from_dict(raw: dict[str, Any], *, source_path: str = "") -> "PluginManifest":
        if not isinstance(raw, dict):
            raise ValueError("manifest must be a JSON object")
        manifest_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not manifest_id or not name:
            raise ValueError("manifest requires 'id' and 'name'")
        return PluginManifest(
            id=manifest_id,
            name=name,
            version=str(raw.get("version") or "0"),
            category=str(raw.get("category") or "utilities"),
            description=str(raw.get("description") or ""),
            provider=str(raw.get("provider") or "external"),
            executables=[str(x) for x in raw.get("executables") or []],
            capabilities=[str(x) for x in raw.get("capabilities") or []],
            permissions=[str(x) for x in raw.get("permissions") or []],
            requires_network=bool(raw.get("requires_network", False)),
            requires_gpu=bool(raw.get("requires_gpu", False)),
            requirements=dict(raw.get("requirements") or {}),
            supported_os=[str(x).lower() for x in raw.get("supported_os") or []],
            docs=str(raw.get("docs") or ""),
            install=dict(raw.get("install") or {}),
            health_check=dict(raw.get("health_check") or {}),
            invoke=raw.get("invoke") if isinstance(raw.get("invoke"), dict) else None,
            config=dict(raw.get("config") or {}),
            source_path=source_path,
        )

    @property
    def os_supported(self) -> bool:
        if not self.supported_os:
            return True
        current = "windows" if sys.platform.startswith("win") else "linux" if sys.platform.startswith("linux") else sys.platform
        return current in self.supported_os or sys.platform in self.supported_os

    def executables_found(self) -> tuple[list[str], list[str]]:
        """Return (found, missing) executable names/paths."""
        found, missing = [], []
        for exe in self.executables:
            p = Path(exe).expanduser()
            if (p.is_absolute() and p.is_file()) or shutil.which(exe):
                found.append(exe)
            else:
                missing.append(exe)
        return found, missing


INSTALL_METHODS = {
    "winget": lambda pkg: ["winget", "install", "--id", pkg, "-e", "--accept-source-agreements", "--accept-package-agreements"],
    "choco": lambda pkg: ["choco", "install", pkg, "-y"],
    "pip": lambda pkg: [sys.executable, "-m", "pip", "install", pkg],
    "uv": lambda pkg: ["uv", "pip", "install", pkg],
    "npm": lambda pkg: ["npm", "install", "-g", pkg],
    "apt": lambda pkg: ["apt", "install", "-y", pkg],
    "dnf": lambda pkg: ["dnf", "install", "-y", pkg],
    "brew": lambda pkg: ["brew", "install", pkg],
}


def install_command(install: dict[str, Any]) -> list[str] | None:
    """Translate a manifest install spec into an argv, or None if not automatable."""
    if not isinstance(install, dict):
        return None
    method = str(install.get("method") or "").strip().lower()
    package = str(install.get("package") or "").strip()
    builder = INSTALL_METHODS.get(method)
    if builder is None or not package:
        return None
    return builder(package)


def _substitute(template: list[str], arguments: dict[str, Any]) -> list[str]:
    command: list[str] = []
    for part in template:
        whole = _PLACEHOLDER.fullmatch(part)
        if whole:
            value = arguments.get(whole.group(1))
            if isinstance(value, list):
                command.extend(str(v) for v in value)
            elif value is not None:
                command.append(str(value))
            continue
        command.append(_PLACEHOLDER.sub(lambda m: str(arguments.get(m.group(1), m.group(0))), part))
    return command


def _make_invoker(manifest: PluginManifest, *, workspace: Path | None, default_timeout: int):
    invoke = manifest.invoke or {}
    template = [str(x) for x in invoke.get("command") or []]
    if not template:
        return None
    timeout = max(1, min(3600, int(invoke.get("timeout_seconds", default_timeout))))

    def handler(arguments: dict[str, Any]) -> str:
        cmd = _substitute(template, arguments)
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace) if workspace else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return f"ERROR: executable not found: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return f"ERROR: TimeoutExpired after {timeout}s running {manifest.id}"
        elapsed = round(time.time() - started, 3)
        return json.dumps({
            "tool": manifest.id,
            "command": cmd,
            "exit_code": proc.returncode,
            "elapsed_seconds": elapsed,
            "stdout": (proc.stdout or "")[-20000:],
            "stderr": (proc.stderr or "")[-8000:],
        }, ensure_ascii=False)

    return handler


def _make_health_check(manifest: PluginManifest):
    command = [str(x) for x in (manifest.health_check.get("command") or [])]

    def check() -> dict[str, Any]:
        found, missing = manifest.executables_found()
        if missing:
            return {"ok": False, "status": "missing", "detail": f"missing executables: {', '.join(missing)}"}
        if command:
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=15)
            except FileNotFoundError:
                return {"ok": False, "status": "missing", "detail": f"health command not found: {command[0]}"}
            except subprocess.TimeoutExpired:
                return {"ok": False, "status": "timeout", "detail": "health check timed out"}
            if proc.returncode != 0:
                return {"ok": False, "status": "unhealthy", "detail": (proc.stderr or proc.stdout or "")[-300:]}
        return {"ok": True, "status": "healthy", "detail": ", ".join(found) or "no executables required"}

    return check


def load_plugin_manifests(
    directory: Path,
    registry: ToolRegistry,
    *,
    workspace: Path | None = None,
    default_timeout: int = 300,
) -> dict[str, Any]:
    """Load `*.json` tool manifests from a directory into the registry.

    Returns {"loaded": [...], "errors": [...]}. Manifest tools default to the
    `shell.execute` permission so every invocation still passes the permission
    gate; a manifest can declare a stricter/different permission key.
    """
    directory = Path(directory)
    loaded: list[str] = []
    errors: list[dict[str, str]] = []
    if not directory.is_dir():
        return {"loaded": loaded, "errors": errors}

    for path in sorted(directory.glob("*.json")):
        try:
            manifest = PluginManifest.from_dict(json.loads(path.read_text(encoding="utf-8")), source_path=str(path))
        except (OSError, ValueError) as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue

        invoker = _make_invoker(manifest, workspace=workspace, default_timeout=default_timeout)
        found, missing = manifest.executables_found()
        install_status = "installed" if not manifest.executables or not missing else "missing"
        permission = manifest.permissions[0] if manifest.permissions else "shell.execute"
        spec = ToolSpec(
            name=manifest.id,
            description=manifest.description or manifest.name,
            parameters=(manifest.invoke or {}).get("input_schema") or {"type": "object", "properties": {}},
            permission=permission,
            handler=invoker or (lambda args, mid=manifest.id: f"ERROR: manifest tool '{mid}' has no invoke command"),
            tool_id=manifest.id,
            display_name=manifest.name,
            category=manifest.category,
            version=manifest.version,
            provider=manifest.provider,
            capabilities=list(manifest.capabilities) or [manifest.id],
            permissions_required=list(manifest.permissions) or [permission],
            requires_network=manifest.requires_network,
            requires_gpu=manifest.requires_gpu,
            requirements=dict(manifest.requirements),
            supported_os=list(manifest.supported_os),
            docs=manifest.docs,
            source="manifest",
            install_status=install_status,
            health_check=_make_health_check(manifest),
        )
        spec_fields = {"invocable": invoker is not None, "install": manifest.install, "manifest_path": manifest.source_path}
        registry.register(spec)
        registry._plugin_meta[spec.name] = spec_fields  # noqa: SLF001 - registry-owned metadata
        loaded.append(manifest.id)
    return {"loaded": loaded, "errors": errors}
