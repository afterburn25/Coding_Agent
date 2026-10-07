from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ..procutil import no_window_flags
from .base import ToolRegistry, ToolSpec

WORKTREE_ROOT = ".agent/worktrees"


def _run(workspace: Path, argv: list[str], timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(["git", *argv], cwd=workspace, text=True,
                          capture_output=True, timeout=timeout,
                          creationflags=no_window_flags())
    # rstrip only — leading whitespace is meaningful in porcelain output
    # (" M file" marks unstaged modification).
    return proc.returncode, (proc.stdout + proc.stderr).rstrip()


def register_git_tools(registry: ToolRegistry, workspace: Path,
                       extra_roots=None, journal=None) -> None:
    def _journal(action_type: str, subject: str, **fields: Any) -> None:
        if journal is None:
            return
        try:
            tls = registry.context.get("task_tls")
            journal.record(
                action_type, subject, actor="nexus",
                task_id=str(getattr(tls, "task_id", "")
                          or registry.context.get("task_id", "") or ""),
                mission_id=str(registry.context.get("mission_id", "")
                               or ""),
                **fields)
        except Exception:
            pass
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

    def _root_for(args: dict) -> Path:
        """Repo root for this call — primary workspace by default, any
        registered workspace root via an explicit 'path' arg."""
        raw = str(args.get("path", "") or "").strip()
        if not raw:
            return workspace.resolve()
        cand = Path(raw)
        root = (workspace / cand).resolve() if not cand.is_absolute() \
            else cand.resolve()
        if not any(root == b or b in root.parents for b in _roots()):
            raise ValueError("path escapes the registered workspaces")
        return root

    def git_status(args: dict) -> str:
        root = _root_for(args)
        proc = subprocess.run(["git", "status", "--short", "--branch"],
                              cwd=root, text=True, capture_output=True,
                              creationflags=no_window_flags())
        return proc.stdout.strip() or proc.stderr.strip() or "(clean)"

    def git_diff(args: dict) -> str:
        cmd = ["git", "diff"]
        if args.get("staged"):
            cmd.append("--staged")
        proc = subprocess.run(cmd, cwd=_root_for(args), text=True,
                              capture_output=True,
                              creationflags=no_window_flags())
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

    # -- extended local-git surface ----------------------------------------

    def git_init(args: dict) -> str:
        root = _root_for(args)
        code, out = _run(root, ["init"])
        if code != 0:
            return f"ERROR: git init failed: {out}"
        return json.dumps({"root": str(root), "output": out}, indent=2)

    def git_switch(args: dict) -> str:
        branch = str(args.get("branch") or "").strip()
        if not branch:
            return "ERROR: 'branch' is required"
        root = _root_for(args)
        argv = ["switch", "-c", branch] if args.get("create") \
            else ["switch", branch]
        prev = _run(root, ["rev-parse", "--abbrev-ref", "HEAD"]
                    )[1].strip() if args.get("create") else ""
        code, out = _run(root, argv)
        if code != 0:
            return f"ERROR: git switch failed: {out}"
        if args.get("create"):
            _journal("git_branch_create", f"branch '{branch}'",
                     before=prev, after=branch, reversible=True,
                     undo={"kind": "git_branch_delete", "branch": branch,
                           "previous_branch": prev, "root": str(root)},
                     risk="medium",
                     description=f"Created and switched to '{branch}'")
        return json.dumps({"branch": branch, "switched": True,
                           "created": bool(args.get("create"))}, indent=2)

    def git_log(args: dict) -> str:
        limit = max(1, min(int(args.get("limit", 20)), 100))
        code, out = _run(_root_for(args), [
            "log", f"-{limit}", "--pretty=format:%h %ad %an %s",
            "--date=short"])
        if code != 0:
            return f"ERROR: {out}"
        return out or "(no commits)"

    def git_blame(args: dict) -> str:
        rel = str(args.get("file") or "").strip()
        if not rel:
            return "ERROR: 'file' is required"
        code, out = _run(_root_for(args), ["blame", "--", rel])
        if code != 0:
            return f"ERROR: {out}"
        return out[-24000:]

    def git_fetch(args: dict) -> str:
        remote = str(args.get("remote") or "").strip()
        argv = ["fetch", remote] if remote else ["fetch", "--all", "--prune"]
        code, out = _run(_root_for(args), argv, timeout=180)
        if code != 0:
            return f"ERROR: git fetch failed: {out}"
        return json.dumps({"fetched": True, "output": out[-4000:]},
                          indent=2)

    def git_pull(args: dict) -> str:
        remote = str(args.get("remote") or "origin").strip()
        branch = str(args.get("branch") or "").strip()
        argv = ["pull", remote] + ([branch] if branch else [])
        code, out = _run(_root_for(args), argv, timeout=240)
        if code != 0:
            return f"ERROR: git pull failed: {out}"
        return json.dumps({"pulled": True, "output": out[-6000:]},
                          indent=2)

    def git_stash(args: dict) -> str:
        action = str(args.get("action") or "list").strip().lower()
        root = _root_for(args)
        if action == "push":
            msg = str(args.get("message") or "").strip()
            argv = ["stash", "push", "-u"] + (["-m", msg] if msg else [])
        elif action == "pop":
            argv = ["stash", "pop"]
        elif action == "list":
            argv = ["stash", "list"]
        else:
            return "ERROR: action must be push, pop, or list"
        code, out = _run(root, argv)
        if code != 0:
            return f"ERROR: git stash {action} failed: {out}"
        return json.dumps({"action": action, "output": out[-4000:]},
                          indent=2)

    def git_revert(args: dict) -> str:
        commit = str(args.get("commit") or "").strip()
        if not commit:
            return "ERROR: 'commit' is required"
        code, out = _run(_root_for(args),
                         ["revert", "--no-edit", commit], timeout=120)
        if code != 0:
            return f"ERROR: git revert failed: {out}"
        return json.dumps({"reverted": commit, "output": out[-4000:]},
                          indent=2)

    def git_restore(args: dict) -> str:
        paths = [str(p) for p in (args.get("paths") or []) if str(p).strip()]
        if not paths:
            return "ERROR: 'paths' is required"
        argv = ["restore"] + (["--staged"] if args.get("staged") else []) \
            + ["--"] + paths
        code, out = _run(_root_for(args), argv)
        if code != 0:
            return f"ERROR: git restore failed: {out}"
        return json.dumps({"restored": paths, "output": out[-2000:]},
                          indent=2)

    def git_sync(args: dict) -> str:
        root = _root_for(args)
        branch = (_run(root, ["rev-parse", "--abbrev-ref", "HEAD"])[1]
                  .strip())
        remote = _run(root, ["remote", "get-url", "origin"])[1].strip()
        status = _run(root, ["status", "--porcelain"])[1]
        dirty = [ln for ln in status.splitlines() if ln.strip()]
        counts = _run(root, ["rev-list", "--left-right", "--count",
                             "HEAD...@{u}"])[1].strip()
        ahead = behind = None
        if "\t" in counts:
            a, b = counts.split("\t", 1)
            ahead, behind = int(a or 0), int(b or 0)
        return json.dumps({
            "branch": branch or None, "remote": remote or None,
            "ahead": ahead, "behind": behind,
            "dirty_files": len(dirty), "dirty_sample": dirty[:20],
        }, indent=2)

    def git_conflicts(args: dict) -> str:
        code, out = _run(_root_for(args),
                         ["diff", "--name-only", "--diff-filter=U"])
        files = [ln for ln in out.splitlines() if ln.strip()]
        return json.dumps({"conflicted": files, "count": len(files)},
                          indent=2)

    def git_user_changes(args: dict) -> str:
        """Modified + untracked files — the pre-flight check before
        autonomous work so agent edits never trample uncommitted user
        work silently."""
        root = _root_for(args)
        _, status = _run(root, ["status", "--porcelain"])
        modified, untracked, staged = [], [], []
        for ln in status.splitlines():
            if not ln.strip():
                continue
            flag, name = ln[:2], ln[3:].strip()
            if flag == "??":
                untracked.append(name)
            elif flag[0] not in " ?":
                staged.append(name)
            if flag[1] not in " ?":
                modified.append(name)
        return json.dumps({
            "has_user_changes": bool(modified or untracked or staged),
            "modified": modified, "untracked": untracked,
            "staged": staged,
        }, indent=2)

    def git_commit(args: dict) -> str:
        message = str(args.get("message") or "").strip()
        if not message:
            return "ERROR: 'message' is required"
        root = _root_for(args)
        code, out = _run(root, ["rev-parse", "--git-dir"])
        if code != 0:
            return f"ERROR: not a git repository: {out}"
        paths = args.get("paths")
        if paths:
            stage = ["add", "--"] + [str(p) for p in paths]
        elif args.get("add_all", True):
            stage = ["add", "-A"]
        else:
            stage = []
        if stage:
            code, out = _run(root, stage)
            if code != 0:
                return f"ERROR: git add failed: {out}"
        _, staged = _run(root, ["diff", "--staged", "--name-only"])
        if not staged.strip():
            return json.dumps({"committed": False,
                               "detail": "nothing staged to commit"})
        code, out = _run(root, ["commit", "-m", message], timeout=120)
        if code != 0:
            return f"ERROR: git commit failed: {out}"
        _, sha = _run(root, ["rev-parse", "HEAD"])
        _journal("git_commit", f"commit {sha.strip()[:8]}",
                 after=sha.strip(), files=staged.splitlines(),
                 reversible=True,
                 undo={"kind": "git_reset_soft", "sha": sha.strip(),
                       "root": str(root)},
                 risk="medium",
                 description=message[:200])
        return json.dumps({"committed": True, "sha": sha.strip(),
                           "files": staged.splitlines(),
                           "output": out}, indent=2)

    registry.register(ToolSpec("git_status", "Show repository branch and changed files.", {
        "type": "object", "properties": {"path": {"type": "string"}}
    }, "filesystem.read", git_status))
    registry.register(ToolSpec("git_diff", "Show the current Git diff.", {
        "type": "object", "properties": {"staged": {"type": "boolean"},
                                       "path": {"type": "string"}}
    }, "filesystem.read", git_diff))
    _path_schema = {"path": {"type": "string",
                             "description": "repo directory (default: primary workspace)"}}
    registry.register(ToolSpec("git_init", "Initialize a git repository in a workspace directory.", {
        "type": "object", "properties": dict(_path_schema)
    }, "git.execute", git_init, category="git"))
    registry.register(ToolSpec("git_switch", "Switch branches (create=true to create and switch).", {
        "type": "object",
        "properties": {"branch": {"type": "string"},
                       "create": {"type": "boolean", "default": False},
                       **_path_schema},
        "required": ["branch"],
    }, "git.execute", git_switch, category="git"))
    registry.register(ToolSpec("git_log", "Show recent commit history.", {
        "type": "object",
        "properties": {"limit": {"type": "integer", "default": 20},
                       **_path_schema},
    }, "filesystem.read", git_log, category="git"))
    registry.register(ToolSpec("git_blame", "Show per-line authorship for a file.", {
        "type": "object",
        "properties": {"file": {"type": "string"}, **_path_schema},
        "required": ["file"],
    }, "filesystem.read", git_blame, category="git"))
    registry.register(ToolSpec("git_fetch", "Fetch from remotes (default: --all --prune).", {
        "type": "object",
        "properties": {"remote": {"type": "string"}, **_path_schema},
    }, "git.execute", git_fetch, category="git"))
    registry.register(ToolSpec("git_pull", "Pull from a remote into the current branch.", {
        "type": "object",
        "properties": {"remote": {"type": "string", "default": "origin"},
                       "branch": {"type": "string"}, **_path_schema},
    }, "git.execute", git_pull, category="git"))
    registry.register(ToolSpec("git_stash", "Stash operations: list, push (-u, optional message), pop.", {
        "type": "object",
        "properties": {"action": {"type": "string",
                                  "enum": ["list", "push", "pop"],
                                  "default": "list"},
                       "message": {"type": "string"}, **_path_schema},
    }, "git.execute", git_stash, category="git"))
    registry.register(ToolSpec("git_revert", "Revert a commit (creates a new commit; does not rewrite history).", {
        "type": "object",
        "properties": {"commit": {"type": "string"}, **_path_schema},
        "required": ["commit"],
    }, "git.execute", git_revert, category="git"))
    registry.register(ToolSpec("git_restore", "Restore files from HEAD/staging — discards changes to the given paths; confirm with the user for non-agent edits.", {
        "type": "object",
        "properties": {"paths": {"type": "array",
                                 "items": {"type": "string"}},
                       "staged": {"type": "boolean", "default": False},
                       **_path_schema},
        "required": ["paths"],
    }, "git.execute", git_restore, category="git"))
    registry.register(ToolSpec("git_sync", "Local vs remote sync state: branch, remote, ahead/behind, dirty files.", {
        "type": "object", "properties": dict(_path_schema)
    }, "filesystem.read", git_sync, category="git"))
    registry.register(ToolSpec("git_conflicts", "List files with unresolved merge conflicts.", {
        "type": "object", "properties": dict(_path_schema)
    }, "filesystem.read", git_conflicts, category="git"))
    registry.register(ToolSpec("git_commit", "Stage paths (or all changes with add_all) and create a commit. Report the real commit SHA from git — never narrate one.", {
        "type": "object",
        "properties": {"message": {"type": "string"},
                       "paths": {"type": "array",
                                 "items": {"type": "string"}},
                       "add_all": {"type": "boolean", "default": False},
                       **_path_schema},
        "required": ["message"],
    }, "git.execute", git_commit, category="git"))
    registry.register(ToolSpec("git_user_changes", "Detect modified/untracked/staged files — run before large autonomous work so agent edits never silently overwrite uncommitted user work.", {
        "type": "object", "properties": dict(_path_schema)
    }, "filesystem.read", git_user_changes, category="git"))
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
