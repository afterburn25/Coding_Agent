"""Project build-out tools — scaffold, dependency setup, health checks.

These are the honest building blocks of the application-builder loop:
each step returns real evidence (exit codes, output tails, HTTP status)
so completion claims are backed by execution, not narrative.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..procutil import no_window_flags
from .base import ToolRegistry, ToolSpec
from ..scaffold import catalog, scaffold

MAX_OUT = 12000


def _run_step(root: Path, cmd: str, timeout: int = 600) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(root), shell=True, text=True,
                              capture_output=True, timeout=timeout,
                              creationflags=no_window_flags())
        return {"command": cmd, "exit_code": proc.returncode,
                "tail": ((proc.stdout or "") + "\n" + (proc.stderr or ""))
                .strip()[-MAX_OUT:],
                "elapsed_seconds": round(time.time() - started, 2),
                "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"command": cmd, "exit_code": None,
                "tail": "", "elapsed_seconds": round(time.time() - started, 2),
                "timed_out": True}
    except Exception as exc:
        return {"command": cmd, "exit_code": None,
                "tail": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": round(time.time() - started, 2),
                "timed_out": False}


def _venv_python(root: Path) -> str:
    win = root / ".venv" / "Scripts" / "python.exe"
    nix = root / ".venv" / "bin" / "python"
    if win.is_file():
        return str(win)
    if nix.is_file():
        return str(nix)
    return sys.executable


def _setup_steps(root: Path) -> list[str]:
    """Dependency-resolution commands for the detected project — honest:
    only steps evidenced by project files are scheduled."""
    steps: list[str] = []
    if (root / "package.json").is_file():
        steps.append("npm install")
    if (root / "requirements.txt").is_file():
        py = _venv_python(root)
        if py == sys.executable:
            steps.append(f"{sys.executable} -m venv .venv")
            py = _venv_python(root)
        steps.append(f'"{py}" -m pip install -r requirements.txt')
    if (root / "pyproject.toml").is_file() and \
            not (root / "requirements.txt").is_file():
        py = _venv_python(root)
        if py == sys.executable:
            steps.append(f"{sys.executable} -m venv .venv")
            py = _venv_python(root)
        steps.append(f'"{py}" -m pip install -e .')
    if any(root.glob("*.csproj")) or any(root.glob("*.sln")):
        steps.append("dotnet restore")
    if (root / "go.mod").is_file():
        steps.append("go mod download")
    return steps


def register_project_tools(registry: ToolRegistry, manager) -> None:
    from ..scaffold import TEMPLATES

    def project_templates(args: dict) -> str:
        return json.dumps({"templates": catalog()}, ensure_ascii=False)

    def project_scaffold(args: dict) -> str:
        raw = str(args.get("path") or "").strip()
        template_id = str(args.get("template") or "").strip()
        if not raw or not template_id:
            return "ERROR: 'path' and 'template' are required"
        if template_id not in TEMPLATES:
            return json.dumps({
                "error": "unknown_template",
                "detail": f"'{template_id}' is not a template",
                "available": [t["id"] for t in catalog()],
            })
        root = Path(raw).expanduser()
        try:
            # Adopt first — registration is what makes writes into the
            # (possibly brand-new) directory legal for other tools.
            manager.open(root, create=True)
        except Exception as exc:
            return f"ERROR: cannot adopt workspace: {exc}"
        try:
            result = scaffold(
                root, template_id, name=str(args.get("name") or ""),
                description=str(args.get("description") or ""),
                force=bool(args.get("force", False)))
        except (KeyError, ValueError) as exc:
            return f"ERROR: {exc}"
        try:
            result["workspace"] = manager.open(root)
        except Exception:
            pass
        return json.dumps({"ok": True, "scaffold": result},
                          ensure_ascii=False, default=str)

    def project_setup(args: dict) -> str:
        raw = str(args.get("path") or "").strip()
        root = Path(raw).expanduser() if raw else None
        if root is None or not root.is_dir():
            return "ERROR: 'path' must be an existing directory"
        try:
            if not manager.contains(root):
                return ("ERROR: path is outside all registered workspaces — "
                        "open it with workspace_open first")
        except Exception:
            pass
        steps = _setup_steps(root.resolve())
        if not steps:
            return json.dumps({"ok": True, "steps": [],
                               "detail": "no dependency step detected"})
        results = [_run_step(root.resolve(), cmd,
                             timeout=int(args.get("timeout", 600)))
                   for cmd in steps]
        ok = all(r["exit_code"] == 0 for r in results)
        return json.dumps({"ok": ok, "steps": results},
                          ensure_ascii=False)

    def app_health(args: dict) -> str:
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return "ERROR: 'url' must be http(s)://"
        timeout = max(1, min(int(args.get("timeout", 10)), 60))
        retries = max(0, min(int(args.get("retries", 0)), 30))
        expect = int(args.get("expect_status", 200))
        delay = max(0.2, float(args.get("retry_delay", 1.0)))
        last: dict = {}
        for attempt in range(retries + 1):
            started = time.time()
            try:
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    body = resp.read(4096).decode("utf-8", errors="replace")
                    last = {"ok": resp.status == expect,
                            "status": resp.status, "expected": expect,
                            "elapsed_ms": int((time.time() - started) * 1000),
                            "body": body[:800], "url": url}
                    if resp.status == expect:
                        return json.dumps(last, ensure_ascii=False)
            except urllib.error.HTTPError as exc:
                last = {"ok": False, "status": exc.code,
                        "expected": expect, "url": url,
                        "elapsed_ms": int((time.time() - started) * 1000)}
            except Exception as exc:
                last = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "url": url}
            if attempt < retries:
                time.sleep(delay)
        return json.dumps(last, ensure_ascii=False)

    registry.register(ToolSpec(
        "project_templates",
        "List available project scaffold templates (static_site, react_vite, "
        "fastapi_service, python_app, python_cli, node_api, cpp_cmake).",
        {"type": "object", "properties": {}},
        "filesystem.read", project_templates, category="workspace",
        capabilities=["project_templates", "scaffold_catalog"]))

    registry.register(ToolSpec(
        "project_scaffold",
        "Scaffold a new project into a directory (created when missing) and "
        "register it as a workspace. Returns files written and detected "
        "commands. Refuses to write into a non-empty directory unless "
        "force=true.",
        {"type": "object",
         "properties": {
             "path": {"type": "string"},
             "template": {"type": "string"},
             "name": {"type": "string"},
             "description": {"type": "string"},
             "force": {"type": "boolean", "default": False}},
         "required": ["path", "template"]},
        "filesystem.write", project_scaffold, category="workspace",
        capabilities=["project_scaffold", "create_project"]))

    registry.register(ToolSpec(
        "project_setup",
        "Install/configure project dependencies inside a registered "
        "workspace (npm install, python venv + pip install, dotnet restore, "
        "go mod download — only steps evidenced by project files).",
        {"type": "object",
         "properties": {"path": {"type": "string"},
                        "timeout": {"type": "integer", "default": 600}},
         "required": ["path"]},
        "shell.execute", project_setup, category="workspace",
        capabilities=["project_setup", "install_dependencies"]))

    registry.register(ToolSpec(
        "app_health",
        "HTTP health check against a local app/dev server. Returns real "
        "status code and body snippet; retries support slow-starting "
        "servers. Use to VERIFY an app actually runs — never claim it does "
        "without this or equivalent evidence.",
        {"type": "object",
         "properties": {"url": {"type": "string"},
                        "expect_status": {"type": "integer", "default": 200},
                        "timeout": {"type": "integer", "default": 10},
                        "retries": {"type": "integer", "default": 0},
                        "retry_delay": {"type": "number", "default": 1.0}},
         "required": ["url"]},
        "filesystem.read", app_health, category="workspace",
        capabilities=["app_health", "verify_running_app"]))
