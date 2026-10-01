from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

WORKTREE_ROOT = ".agent/worktrees"


def _run(workspace: Path, argv: list[str], timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(["git", *argv], cwd=workspace, text=True,
                          capture_output=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def register_git_tools(registry: ToolRegistry, workspace: Path) -> None:
    def git_status(_: dict) -> str:
        proc = subprocess.run(["git", "status", "--short", "--branch"], cwd=workspace, text=True, capture_output=True)
        return (proc.stdout + proc.stderr).strip() or "clean"

    def git_diff(args: dict) -> str:
        cmd = ["git", "diff"]
        if args.get("staged"):
            cmd.append("--staged")
        proc = subprocess.run(cmd, cwd=workspace, text=True, capture_output=True)
        return (proc.stdout + proc.stderr)[-30000:] or "no diff"

    def _worktree_path(raw: str) -> Path | None:
        name = "".join(c if c.isalnum() or c in "-_./" else "_" for c in str(raw or "")).strip("/")
        if not name:
            return None
        p = (workspace / WORKTREE_ROOT / name).resolve()
        if not p.is_relative_to((workspace / WORKTREE_ROOT).resolve()):
            return None
        return p

    def git_worktree_add(args: dict[str, Any]) -> str:
        branch = str(args.get("branch", "")).strip()
        if not branch:
            return "ERROR: 'branch' is required"
        code, out = _run(workspace, ["check-ref-format", "--branch", branch])
        if code != 0:
            return f"ERROR: invalid branch name: {out}"
        target = _worktree_path(str(args.get("path") or branch))
        if target is None:
            return "ERROR: worktree path must stay under .agent/worktrees/"
        if target.exists():
            return f"ERROR: worktree path already exists: {target}"
        if args.get("existing_branch"):
            argv = ["worktree", "add", str(target), branch]
        else:
            base = str(args.get("base", "")).strip() or "HEAD"
            argv = ["worktree", "add", "-b", branch, str(target), base]
        code, out = _run(workspace, argv, timeout=120)
        if code != 0:
            return f"ERROR: git worktree add failed: {out}"
        return json.dumps({"branch": branch, "path": str(target), "output": out}, indent=2)

    def git_worktree_list(_: dict) -> str:
        code, out = _run(workspace, ["worktree", "list", "--porcelain"])
        if code != 0:
            return f"ERROR: {out}"
        trees = []
        current: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                if current:
                    trees.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            trees.append(current)
        return json.dumps({"worktrees": trees}, indent=2)

    def git_worktree_remove(args: dict[str, Any]) -> str:
        target = _worktree_path(str(args.get("path", "")))
        if target is None or not target.exists():
            return "ERROR: worktree must exist under .agent/worktrees/"
        argv = ["worktree", "remove"]
        if args.get("force"):
            argv.append("--force")
        argv.append(str(target))
        code, out = _run(workspace, argv, timeout=60)
        if code != 0:
            return f"ERROR: git worktree remove failed: {out}"
        return json.dumps({"removed": str(target), "output": out}, indent=2)

    registry.register(ToolSpec("git_status", "Show repository branch and changed files.", {
        "type": "object", "properties": {}
    }, "filesystem.read", git_status))
    registry.register(ToolSpec("git_diff", "Show the current Git diff.", {
        "type": "object", "properties": {"staged": {"type": "boolean"}}
    }, "filesystem.read", git_diff))
    registry.register(ToolSpec(
        "git_worktree_add",
        "Create a git worktree under .agent/worktrees/ on a new branch — isolated checkout for parallel agent work without touching the main working tree.",
        {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "new branch name"},
                "path": {"type": "string", "description": "subpath under .agent/worktrees/ (default: branch)"},
                "base": {"type": "string", "default": "HEAD", "description": "base ref for the new branch"},
                "existing_branch": {"type": "boolean", "default": False, "description": "check out an existing branch instead of creating"},
            },
            "required": ["branch"],
        },
        "git.execute", git_worktree_add,
        category="git", capabilities=["git_worktree", "isolated_checkout", "parallel_branches"],
    ))
    registry.register(ToolSpec(
        "git_worktree_list", "List git worktrees (porcelain parse → structured JSON).",
        {"type": "object", "properties": {}},
        "filesystem.read", git_worktree_list,
        category="git", capabilities=["git_worktree", "list_worktrees"],
    ))
    registry.register(ToolSpec(
        "git_worktree_remove", "Remove a worktree under .agent/worktrees/ (force=true to discard changes).",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
        "git.execute", git_worktree_remove,
        category="git", capabilities=["git_worktree", "remove_worktree"],
    ))
