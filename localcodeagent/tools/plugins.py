from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
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


# Directories a tool-payload executable will never legitimately live in —
# pruned from the install-root scan so a ComfyUI portable tree or a cloned
# Source workspace can't turn boot into a multi-minute filesystem crawl.
_SCAN_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".tox", ".mypy_cache", ".pytest_cache", "comfyui_windows_portable",
    "comfyui", "models", "source", "$recycle.bin", "system volume information",
}
_SCAN_MAX_DEPTH = 4
_SCAN_MAX_ENTRIES = 60_000


@lru_cache(maxsize=512)
def _scan_for_executable(base: str, root: str, names_key: str) -> str:
    """Bounded depth-first scan for any of `names_key` (| separated) under
    `base`, never descending below `_SCAN_MAX_DEPTH` relative to `root` or
    into `_SCAN_SKIP_DIRS`, and giving up after `_SCAN_MAX_ENTRIES` entries.

    Path.rglob looks cheap but walks the ENTIRE subtree first — under a
    packaged install root with ComfyUI/models/Source that hung startup.
    """
    wanted = {n.lower() for n in names_key.split("|") if n}
    base_p, root_p = Path(base), Path(root)
    visited = 0
    for dirpath, dirnames, filenames in os.walk(base_p):
        visited += len(filenames)
        if visited > _SCAN_MAX_ENTRIES:
            return ""
        depth = len(Path(dirpath).relative_to(root_p).parts)
        if depth >= _SCAN_MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d.lower() not in _SCAN_SKIP_DIRS]
        for fname in filenames:
            if fname.lower() in wanted:
                hit = Path(dirpath) / fname
                if hit.is_file():
                    return str(hit)
    return ""


def resolve_executable(exe: str, install_root: Path | None,
                       tool_id: str = "") -> str:
    """Resolve an executable name to a runnable path.

    Order: absolute path → PATH → ``<install_root>/<tool_id>`` scan →
    bounded scan of ``<install_root>`` (depth ≤4, pruned). Lets
    archive-installed binaries (e.g. .agent/tools/whisper/Release/
    whisper-cli.exe) run without PATH changes.
    """
    p = Path(str(exe)).expanduser()
    if p.is_absolute() and p.is_file():
        return str(p)
    if shutil.which(str(exe)):
        return str(exe)
    root = Path(install_root).resolve() if install_root else None
    if root is None:
        return str(exe)
    name = str(exe)
    names = [name] if name.lower().endswith((".exe", ".bat", ".cmd")) else [name, f"{name}.exe"]
    bases = [root / tool_id] if tool_id else []
    bases.append(root)
    for base in bases:
        if not base.is_dir():
            continue
        for cand in names:
            direct = base / cand
            if direct.is_file():
                return str(direct)
        try:
            hit = _scan_for_executable(str(base), str(root), "|".join(names))
            if hit:
                return hit
        except OSError:
            continue
    return str(exe)

# Permission keys that are stricter/more specific than shell.execute — a
# manifest declaring one of these uses it as the effective invoker gate.
_DEDICATED_GATES = {"docker.access", "package.install"}


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
    detect_files: list[str] = field(default_factory=list)
    health_check: dict[str, Any] = field(default_factory=dict)
    invoke: dict[str, Any] | None = None
    config: dict[str, Any] = field(default_factory=dict)
    process: str = ""  # Process Manager service id this tool maps to
    dependencies: list[str] = field(default_factory=list)  # declared deps
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
            detect_files=[
                str(x) for x in (raw.get("detect") or {}).get("files") or []
            ],
            health_check=dict(raw.get("health_check") or {}),
            invoke=raw.get("invoke") if isinstance(raw.get("invoke"), dict) else None,
            config=dict(raw.get("config") or {}),
            process=str(raw.get("process") or ""),
            dependencies=[str(x) for x in raw.get("dependencies") or []],
            source_path=source_path,
        )

    @property
    def os_supported(self) -> bool:
        if not self.supported_os:
            return True
        current = "windows" if sys.platform.startswith("win") else "linux" if sys.platform.startswith("linux") else sys.platform
        return current in self.supported_os or sys.platform in self.supported_os

    def executables_found(self, install_root: Path | None = None) -> tuple[list[str], list[str]]:
        """Return (found, missing) executable names/paths.

        Resolution order: absolute path → PATH → install_root scan, so
        archive-installed payloads count even off-PATH.
        """
        found, missing = [], []
        for exe in self.executables:
            resolved = resolve_executable(exe, install_root, self.id)
            if resolved != exe or shutil.which(exe) or (Path(exe).expanduser().is_absolute() and Path(exe).expanduser().is_file()):
                found.append(exe)
            else:
                missing.append(exe)
        return found, missing

    def detect_files_found(self, install_root: Path | None) -> tuple[list[str], list[str]]:
        """Return (found, missing) install-root-relative marker files.

        Lets manifests detect payloads that live inside the app directory rather
        than on PATH (e.g. the bundled ComfyUI portable tree).
        """
        found, missing = [], []
        root = Path(install_root).resolve() if install_root else None
        for rel in self.detect_files:
            p = Path(rel).expanduser()
            target = p if p.is_absolute() else (root / p if root is not None else p)
            if target.exists():
                found.append(rel)
            else:
                missing.append(rel)
        return found, missing

    def is_installed(self, install_root: Path | None = None) -> bool:
        if self.executables or not self.detect_files:
            _, missing_exe = self.executables_found(install_root)
            if missing_exe:
                return False
        if self.detect_files:
            _, missing_files = self.detect_files_found(install_root)
            if missing_files:
                return False
        return True


INSTALL_METHODS = {
    "winget": lambda pkg: ["winget", "install", "--id", pkg, "-e", "--accept-source-agreements", "--accept-package-agreements"],
    "choco": lambda pkg: ["choco", "install", pkg, "-y"],
    "uv": lambda pkg: ["uv", "pip", "install", pkg],
    "npm": lambda pkg: ["npm", "install", "-g", pkg],
    "apt": lambda pkg: ["apt", "install", "-y", pkg],
    "dnf": lambda pkg: ["dnf", "install", "-y", pkg],
    "brew": lambda pkg: ["brew", "install", pkg],
}

# Package-manager removal for the same methods — lets the Tools page
# uninstall manager-owned packages (e.g. winget Blender) instead of only
# archive payloads.
REMOVE_METHODS = {
    "winget": lambda pkg: ["winget", "uninstall", "--id", pkg, "-e", "--accept-source-agreements"],
    "choco": lambda pkg: ["choco", "uninstall", pkg, "-y"],
    "uv": lambda pkg: ["uv", "pip", "uninstall", pkg],
    "npm": lambda pkg: ["npm", "uninstall", "-g", pkg],
    "apt": lambda pkg: ["apt", "remove", "-y", pkg],
    "dnf": lambda pkg: ["dnf", "remove", "-y", pkg],
    "brew": lambda pkg: ["brew", "uninstall", pkg],
}


def managed_python(install_root: Path | None) -> str | None:
    """Best Python interpreter for pip-based tool installs.

    sys.executable is only a real interpreter in development; under a frozen
    PyInstaller backend it is the app exe. In packaged builds we reuse the
    managed runtimes installed by other tools (ComfyUI portable's embedded
    Python, or a standalone runtime at {app}/python).
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    root = Path(install_root) if install_root else None
    candidates = [
        root / "ComfyUI_windows_portable" / "python_embeded" / "python.exe",
        root / "python" / "python.exe",
    ] if root else []
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def install_command(install: dict[str, Any], *, install_root: Path | None = None) -> list[str] | None:
    """Translate a manifest install spec into an argv, or None if not automatable."""
    if not isinstance(install, dict):
        return None
    method = str(install.get("method") or "").strip().lower()
    package = str(install.get("package") or "").strip()
    if method == "pip":
        if not package:
            return None
        python = managed_python(install_root)
        return [python, "-m", "pip", "install", package] if python else None
    builder = INSTALL_METHODS.get(method)
    if builder is None or not package:
        return None
    return builder(package)


def uninstall_command(install: dict[str, Any], *, install_root: Path | None = None) -> list[str] | None:
    """Translate a manifest install spec into a removal argv, or None if
    not automatable. Archive installs are file deletion, not a command —
    callers handle them through ToolDownloadManager.uninstall."""
    if not isinstance(install, dict):
        return None
    method = str(install.get("method") or "").strip().lower()
    package = str(install.get("package") or "").strip()
    if method == "pip":
        if not package:
            return None
        python = managed_python(install_root)
        return [python, "-m", "pip", "uninstall", "-y", package] if python else None
    builder = REMOVE_METHODS.get(method)
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


def _make_invoker(manifest: PluginManifest, *, workspace: Path | None,
                  default_timeout: int, install_root: Path | None = None):
    invoke = manifest.invoke or {}
    template = [str(x) for x in invoke.get("command") or []]
    if not template:
        return None
    timeout = max(1, min(3600, int(invoke.get("timeout_seconds", default_timeout))))
    stdin_key = str(invoke.get("stdin") or "")  # argument name piped to process stdin

    def handler(arguments: dict[str, Any]) -> str:
        cmd = _substitute(template, arguments)
        if cmd:
            cmd[0] = resolve_executable(cmd[0], install_root, manifest.id)
        stdin_data = str(arguments.get(stdin_key, "")) if stdin_key else None
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace) if workspace else None,
                input=stdin_data,
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


def _make_health_check(manifest: PluginManifest, *, install_root: Path | None = None):
    command = [str(x) for x in (manifest.health_check.get("command") or [])]

    def check() -> dict[str, Any]:
        found, missing = manifest.executables_found(install_root)
        if missing:
            return {"ok": False, "status": "missing", "detail": f"missing executables: {', '.join(missing)}"}
        if command:
            try:
                run_cmd = list(command)
                run_cmd[0] = resolve_executable(run_cmd[0], install_root, manifest.id)
                proc = subprocess.run(run_cmd, capture_output=True, text=True, timeout=15)
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
    install_root: Path | None = None,
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
    if install_root is not None:
        registry.install_root = Path(install_root).resolve()
    if not directory.is_dir():
        return {"loaded": loaded, "errors": errors}

    for path in sorted(directory.glob("*.json")):
        try:
            manifest = PluginManifest.from_dict(json.loads(path.read_text(encoding="utf-8")), source_path=str(path))
        except (OSError, ValueError) as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue

        invoker = _make_invoker(manifest, workspace=workspace,
                                default_timeout=default_timeout,
                                install_root=install_root)
        install_status = "installed" if manifest.is_installed(install_root) else "missing"
        # Invokers spawn a subprocess — the effective gate must reflect that.
        # Manifests may declare a stricter dedicated key (e.g. docker.access);
        # otherwise shell.execute is enforced regardless of declared read/write keys.
        dedicated = next((p for p in manifest.permissions if p in _DEDICATED_GATES), None)
        if invoker is not None:
            permission = dedicated or "shell.execute"
            required = sorted(set(manifest.permissions) | {"shell.execute"})
        else:
            permission = dedicated or (manifest.permissions[0] if manifest.permissions else "shell.execute")
            required = list(manifest.permissions) or [permission]
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
            permissions_required=required,
            requires_network=manifest.requires_network,
            requires_gpu=manifest.requires_gpu,
            requirements=dict(manifest.requirements),
            supported_os=list(manifest.supported_os),
            docs=manifest.docs,
            source="manifest",
            install_status=install_status,
            health_check=_make_health_check(manifest, install_root=install_root),
        )
        spec_fields = {"invocable": invoker is not None, "install": manifest.install,
                       "manifest_path": manifest.source_path, "process": manifest.process,
                       "dependencies": list(manifest.dependencies),
                       "detect_files": list(manifest.detect_files),
                       "executables": list(manifest.executables)}
        registry.register(spec)
        registry._plugin_meta[spec.name] = spec_fields  # noqa: SLF001 - registry-owned metadata
        loaded.append(manifest.id)
    return {"loaded": loaded, "errors": errors}
