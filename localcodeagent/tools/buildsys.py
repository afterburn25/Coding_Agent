from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

MAX_OUTPUT = 20000

# Ordered by precedence — first matching system wins for auto mode.
BUILD_SYSTEMS: list[dict[str, Any]] = [
    {
        "id": "cmake",
        "name": "CMake",
        "markers": ["CMakeLists.txt"],
        "commands": {
            "configure": "cmake -S . -B build",
            "build": "cmake --build build",
            "test": "ctest --test-dir build --output-on-failure",
            "clean": "cmake --build build --target clean",
        },
    },
    {
        "id": "meson",
        "name": "Meson",
        "markers": ["meson.build"],
        "commands": {
            "configure": "meson setup build",
            "build": "meson compile -C build",
            "test": "meson test -C build",
        },
    },
    {
        "id": "cargo",
        "name": "Cargo",
        "markers": ["Cargo.toml"],
        "commands": {"build": "cargo build", "test": "cargo test", "clean": "cargo clean"},
    },
    {
        "id": "dotnet",
        "name": ".NET",
        "markers": ["*.sln", "*.csproj", "*.fsproj"],
        "commands": {
            "build": "dotnet build",
            "test": "dotnet test",
            "clean": "dotnet clean",
            "restore": "dotnet restore",
        },
    },
    {
        "id": "msbuild",
        "name": "MSBuild",
        "markers": ["*.sln"],
        "commands": {"build": "msbuild /m", "clean": "msbuild /t:Clean"},
        "requires": ["msbuild"],
    },
    {
        "id": "pnpm",
        "name": "pnpm",
        "markers": ["pnpm-lock.yaml"],
        "commands": {"build": "pnpm build", "test": "pnpm test", "install": "pnpm install"},
    },
    {
        "id": "yarn",
        "name": "Yarn",
        "markers": ["yarn.lock"],
        "commands": {"build": "yarn build", "test": "yarn test", "install": "yarn install"},
    },
    {
        "id": "npm",
        "name": "npm",
        "markers": ["package.json"],
        "commands": {"build": "npm run build", "test": "npm test", "install": "npm install"},
    },
    {
        "id": "gradle",
        "name": "Gradle",
        "markers": ["build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"],
        "commands": {"build": "gradle build", "test": "gradle test", "clean": "gradle clean"},
    },
    {
        "id": "maven",
        "name": "Maven",
        "markers": ["pom.xml"],
        "commands": {"build": "mvn -q compile", "test": "mvn -q test", "clean": "mvn -q clean"},
    },
    {
        "id": "make",
        "name": "Make",
        "markers": ["Makefile", "makefile", "GNUmakefile"],
        "commands": {"build": "make", "test": "make test", "clean": "make clean"},
    },
    {
        "id": "python",
        "name": "Python",
        "markers": ["pyproject.toml", "setup.py"],
        "commands": {
            "build": "python -m compileall -q .",
            "test": "python -m unittest discover -s tests -v",
        },
    },
]


def _marker_matches(root: Path, pattern: str) -> bool:
    if any(c in pattern for c in "*?["):
        return any(root.glob(pattern))
    return (root / pattern).exists()


def detect_build_systems(root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    found: list[dict[str, Any]] = []
    for spec in BUILD_SYSTEMS:
        marker = next((m for m in spec["markers"] if _marker_matches(root, m)), None)
        if marker:
            entry = {
                "id": spec["id"],
                "name": spec["name"],
                "marker": marker,
                "commands": dict(spec["commands"]),
            }
            found.append(entry)
    return found


def _command_for(entry: dict[str, Any], action: str) -> str | None:
    cmd = entry["commands"].get(action)
    if cmd:
        return cmd
    if action == "test":
        return entry["commands"].get("build")
    return None


def register_build_tools(registry: ToolRegistry, workspace: Path, *, default_timeout: int = 600, extra_roots=None) -> None:

    def _roots() -> list[Path]:
        roots = [workspace.resolve()]
        if extra_roots:
            try:
                for r in extra_roots() or []:
                    p = Path(r).resolve()
                    if p not in roots:
                        roots.append(p)
            except Exception:
                pass
        return roots

    def _resolve_root(raw: str) -> Path | None:
        if raw:
            cand = Path(raw)
            root = (workspace / cand).resolve() if not cand.is_absolute() \
                else cand.resolve()
        else:
            root = workspace.resolve()
        if not any(root == b or b in root.parents for b in _roots()):
            return None
        return root if root.is_dir() else None

    def detect(args: dict[str, Any]) -> str:
        raw = str(args.get("path", "") or "")
        root = _resolve_root(raw)
        if root is None:
            return json.dumps({"error": "path must be a directory inside a registered workspace"})
        systems = detect_build_systems(root)
        try:
            disp = str(root.relative_to(workspace)) \
                if root != workspace else "."
        except ValueError:
            disp = str(root)
        return json.dumps({"root": disp,
                           "count": len(systems), "systems": systems,
                           "primary": systems[0]["id"] if systems else None}, ensure_ascii=False)

    def _run_action(args: dict[str, Any], action: str) -> str:
        raw = str(args.get("path", "") or "")
        root = _resolve_root(raw)
        if root is None:
            return f"ERROR: path must be a directory inside a registered workspace"
        requested = str(args.get("system", "auto") or "auto").lower()
        systems = detect_build_systems(root)
        if not systems:
            return "ERROR: no build system detected in this directory"
        if requested == "auto":
            entry = systems[0]
        else:
            entry = next((s for s in systems if s["id"] == requested), None)
            if entry is None:
                ids = ", ".join(s["id"] for s in systems)
                return f"ERROR: build system '{requested}' not detected here (found: {ids})"
        cmd = _command_for(entry, action)
        if not cmd:
            return f"ERROR: {entry['name']} has no '{action}' command configured"
        extra = str(args.get("extra_args", "") or "").strip()
        if extra:
            cmd = f"{cmd} {extra}"
        target = str(args.get("target", "") or "").strip()
        if target and entry["id"] in {"cmake", "make", "msbuild"}:
            cmd = f"{cmd} --target {target}" if entry["id"] == "cmake" else f"{cmd} {target}"
        timeout = max(1, min(int(args.get("timeout_seconds", default_timeout)), 3600))
        started = time.time()
        try:
            proc = subprocess.run(cmd, cwd=str(root), shell=True, capture_output=True, text=True, timeout=timeout)
            payload = {"system": entry["id"], "command": cmd, "exit_code": proc.returncode,
                       "stdout": (proc.stdout or "")[-MAX_OUTPUT:], "stderr": (proc.stderr or "")[-8000:],
                       "timed_out": False}
        except subprocess.TimeoutExpired as exc:
            payload = {"system": entry["id"], "command": cmd, "exit_code": None,
                       "stdout": (exc.stdout or "")[-MAX_OUTPUT:] if isinstance(exc.stdout, str) else "",
                       "stderr": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
                       "timed_out": True}
        payload["elapsed_seconds"] = round(time.time() - started, 3)
        return json.dumps(payload, ensure_ascii=False)

    registry.register(ToolSpec(
        "detect_build_system",
        "Detect build systems in the workspace (CMake, Meson, Cargo, .NET, MSBuild, npm/pnpm/yarn, Gradle, Maven, Make, Python). Returns ranked candidates with suggested commands.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "workspace-relative directory"}},
        },
        "filesystem.read",
        detect,
        category="coding",
        capabilities=["detect_build_system", "repository_search"],
    ))
    registry.register(ToolSpec(
        "build_project",
        "Detect the project build system and run its build command (or a requested system). Returns JSON with exit code and output.",
        {
            "type": "object",
            "properties": {
                "system": {"type": "string", "default": "auto", "description": "build system id or 'auto'"},
                "path": {"type": "string", "description": "workspace-relative project directory"},
                "target": {"type": "string"},
                "extra_args": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 600},
            },
        },
        "shell.execute",
        lambda a: _run_action(a, "build"),
        category="coding",
        capabilities=["build_project", "terminal_run"],
    ))
    registry.register(ToolSpec(
        "run_tests",
        "Detect the project build system and run its test command. Returns JSON with exit code and output.",
        {
            "type": "object",
            "properties": {
                "system": {"type": "string", "default": "auto"},
                "path": {"type": "string"},
                "extra_args": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 600},
            },
        },
        "shell.execute",
        lambda a: _run_action(a, "test"),
        category="coding",
        capabilities=["run_tests", "terminal_run"],
    ))
    registry.register(ToolSpec(
        "configure_project",
        "Run the detected build system's configure/setup step when one exists (e.g. cmake -S . -B build, meson setup).",
        {
            "type": "object",
            "properties": {
                "system": {"type": "string", "default": "auto"},
                "path": {"type": "string"},
                "extra_args": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 600},
            },
        },
        "shell.execute",
        lambda a: _run_action(a, "configure"),
        category="coding",
        capabilities=["build_project", "terminal_run"],
    ))
    registry.register(ToolSpec(
        "clean_project",
        "Run the detected build system's clean step when one exists.",
        {
            "type": "object",
            "properties": {
                "system": {"type": "string", "default": "auto"},
                "path": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 300},
            },
        },
        "shell.execute",
        lambda a: _run_action(a, "clean"),
        category="coding",
        capabilities=["build_project", "terminal_run"],
    ))
