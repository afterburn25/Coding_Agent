"""Git safety guard for autonomous mission shell commands.

Mission-attributed runs (``task_tls.mission_id``) may not mutate or publish
repository state through shell tools — the self-repair pipeline is the
governed path (worktree → review → bounded promote/commit). Read-only git
stays available so missions can still orient themselves.

Policy converges with ``autonomy/policy.py`` — three tiers:

- ``git_push`` grant (irreversible/off-host): ``push``, remote admin.
- ``git_commit`` grant, checked regardless of cwd — refs, objects and
  config are shared across every linked worktree: branch/tag/ref surgery,
  config writes, worktree admin, object-db writes.
- local checkout mutations (``commit``/``merge``/``checkout``/``reset``…):
  safe inside a worktree sandbox; in the primary checkout they need a
  ``git_commit`` grant.

User-initiated missions and unattributed (interactive) runs are exempt.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# A `git` invocation. The boundary alternation (^, &&, ||, ;, |, `, $(),
# quote) catches `git add . && git commit -m x` and `powershell -c "git
# push"` while `git log | grep push` correctly scans only the segment
# containing `git`. An optional common-launcher prefix covers
# `cmd /c git …` / `bash -c "…"` / `env X=1 git …`, and an optional path
# prefix covers `C:\tools\git.exe`. Leading *global* options (-C dir,
# -c k=v, --git-dir=…) are consumed so the verb must be the actual
# subcommand — `git log --all -- push` doesn't match `push`.
_GIT = (
    r"(?:^|&&|\|\||;|\||`|\$\(|[\"']|\n)"
    r"\s*(?:(?:cmd|powershell|pwsh|bash|sh|zsh|fish|start|call|xargs|"
    r"env|time|nice|sudo)(?:\.exe)?[\s/\"'\-\w=.]*)?"
    r"(?:[\w.\\/:~-]*[\\/])?git(?:\.(?:exe|cmd|bat))?[\"']?\s+"
    r"(?:-[cC]\s+\S+\s+|--\S+(?:=\S+)?\s+|-[a-zA-Z]\s+)*")

# Pushes and remote admin — off-host / remote-defining state.
_GIT_REMOTE_RE = re.compile(
    _GIT + r"(?:push|remote\s+(?:add|remove|rm|rename|set-url|set-head|"
    r"set-branches|prune|update))\b",
    re.IGNORECASE)

# Shared-repo writes — visible to the primary checkout and all linked
# worktrees even when run from a worktree cwd.
_GIT_SHARED_RE = re.compile(
    _GIT + r"(?:"
    r"tag\s+(?!--(?:list|points-at|contains|merged|no-merged|sort|format|"
    r"show)\b|-[ln]\b)\S|"
    r"branch\s+(?:-[dDmMcCfuU]\b|--\w*delete|--\w*move|--\w*copy|"
    r"--force|--set-upstream|--unset-upstream|[\w.][\w./]*)|"
    r"update-ref\b|reflog\s+(?:expire|delete)\b|"
    r"symbolic-ref\s+(?:-[a-zA-Z]|[\w-]+\s+\S)|"
    r"worktree\s+(?:add|remove|move|prune|repair|lock|unlock)\b|"
    r"notes\s+(?:add|append|copy|edit|remove|prune)\b|"
    r"config\s+(?:--unset\b|--add\b|--replace-all\b|--remove-section\b|"
    r"--rename-section\b|(?:--(?:global|local|system|worktree)\s+)?"
    r"[A-Za-z][\w.-]*\s+(?![&|;])\S)|"
    r"prune\b|prune-packed\b|repack\b|gc\b|maintenance\b|commit-graph\b|"
    r"update-index\b|update-server-info\b|write-tree\b|commit-tree\b|"
    r"mktree\b|mktag\b|hash-object\s+-[a-zA-Z]*w|"
    r"replace\b|filter-branch|filter-repo)",
    re.IGNORECASE)

# GitHub CLI — `gh` publishes without touching `git` verbs at all.
_GH = (
    r"(?:^|&&|\|\||;|\||`|\$\(|[\"']|\n)"
    r"\s*(?:(?:cmd|powershell|pwsh|bash|sh|zsh|fish|start|call|xargs|"
    r"env|time|nice|sudo)(?:\.exe)?[\s/\"'\-\w=.]*)?"
    r"(?:[\w.\\/:~-]*[\\/])?gh(?:\.exe)?[\"']?\s+"
    r"(?:-[a-zA-Z]\s+\S+\s+|--\S+(?:=\S+)?\s+)*")
_GH_REMOTE_RE = re.compile(
    _GH + r"(?:api\b[^;&|]*?(?:-X\s*[\"']?(?:POST|PUT|PATCH|DELETE)\b|"
    r"--method\s+[\"']?(?:post|put|patch|delete)\b|\s-[fF]\s)|"
    r"(?:pr|issue|release|repo|workflow|run|gist|secret|variable|"
    r"label|ruleset|project|org|milestone)\s+[^;&|]*?\b(?:create|merge|"
    r"close|reopen|delete|edit|comment|review|lock|unlock|pin|unpin|"
    r"transfer|rename|archive|unarchive|fork|upload|rerun|cancel|"
    r"enable|disable|run|set|add|remove|ready|revert|resolve|develop|"
    r"adopt|publish)\b|"
    r"auth\s+(?:login|logout|refresh)\b)",
    re.IGNORECASE)
_GH_MUTATE_RE = re.compile(
    _GH + r"(?:pr\s+checkout|repo\s+sync)\b", re.IGNORECASE)

# Local checkout/index mutations — the worktree sandbox tolerates them.
_GIT_MUTATE_RE = re.compile(
    _GIT + r"(?:commit|merge|rebase|pull|checkout|switch|reset|"
    r"restore|revert|cherry-pick|am|apply|add|rm|mv|"
    r"stash\b(?!\s+(?:list|show)\b)|"
    r"clean\s+-[a-zA-Z]*[fdx]|"
    r"submodule\s+(?:add|deinit|update)\b|"
    r"bisect\s+(?:start|bad|good|skip|reset|new|old|term|run|replay)\b|"
    r"sparse-checkout\s+(?!list\b)\S|"
    r"read-tree|checkout-index|merge-index|lfs)\b",
    re.IGNORECASE)

# Argv-style git calls — `subprocess.run(["git", "push"])` inside
# python_exec splits the verb into its own quoted token, so the verb
# just needs to appear as a quoted arg later on the same line.
_PY_ARGV = (r"[\[(,]\s*[\"'][^\n\"']*git(?:\.exe)?[\"']\s*,"
            r"[^\n]{0,400}?[\"']")
_PY_ARGV_REMOTE = re.compile(
    _PY_ARGV + r"(?:push|remote)[\"']", re.IGNORECASE)
_PY_ARGV_MUTATE = re.compile(
    _PY_ARGV + r"(?:commit|merge|rebase|pull|checkout|switch|reset|"
    r"restore|revert|cherry-pick|am|apply|tag|branch|worktree|config|"
    r"update-ref|clean|stash|init|clone|add|rm|mv|bisect|notes)[\"']",
    re.IGNORECASE)

_USER_SOURCES = {
    "user", "chat", "manual", "api", "cli", "ui", "interactive",
    "command", "voice", "operator",
}


def _mission_attributed(registry) -> bool:
    """True when the running task is mission-attributed and the mission
    is autonomous (not user-initiated)."""
    ctx = getattr(registry, "context", None) or {}
    tls = ctx.get("task_tls")
    mission_id = str(getattr(tls, "mission_id", "") or "")
    if not mission_id:
        return False
    src_fn = ctx.get("mission_source")
    try:
        src = str(src_fn(mission_id) or "") if callable(src_fn) else ""
    except Exception:
        src = ""
    return src not in _USER_SOURCES

_BLOCK = (
    "blocked: autonomous missions may not run `{sub}` — commits, merges, "
    "ref changes and pushes belong in the governed self-repair path "
    "(worktree → review → promote) or need a standing autonomy grant "
    "(action `{action}`)"
)


def _repo_root(cwd: Path) -> Path | None:
    try:
        cur = Path(cwd).resolve()
    except Exception:
        return None
    for p in (cur, *cur.parents):
        if (p / ".git").exists():
            return p
    return None


def _is_primary_dir(path: Path) -> bool:
    """True when ``path`` sits inside a primary checkout — ``.git`` is a
    real directory. A linked worktree carries a ``.git`` *file* pointing
    into the primary's ``.git/worktrees``, so it reports False and stays
    usable as the repair sandbox."""
    root = _repo_root(path)
    return bool(root is not None and (root / ".git").is_dir())


_GIT_SEG_RE = re.compile(
    r"(?:^|&&|\|\||;|\||`|\$\(|[\"']|\n)"
    r"\s*(?:(?:cmd|powershell|pwsh|bash|sh|zsh|fish|start|call|xargs|"
    r"env|time|nice|sudo)(?:\.exe)?[\s/\"'\-\w=.]*)?"
    r"(?:[\w.\\/:~-]*[\\/])?git(?:\.(?:exe|cmd|bat))?[\"']?\s+"
    r"([^;&|`$]*)",
    re.IGNORECASE)

_CD_RE = re.compile(
    r"(?:^|&&|\|\||;|\||\n)\s*(?:cd|chdir|pushd|Set-Location|sl)\s+"
    r"(?:/d\s+)?[\"']?([^\s\"'`$;&|]+)",
    re.IGNORECASE)

_GIT_DIR_OPT_RE = re.compile(
    r"(?:-C\s+|--work-tree(?:=|\s+))[\"']?([^\s\"'`$;&|]+)[\"']?",
    re.IGNORECASE)


# Script tokens — a `.bat`/`.ps1`/`.sh` on the command line gets its
# contents scanned too, so `run fix.bat` can't smuggle `git push`.
_SCRIPT_TOKEN_RE = re.compile(
    r"[\"']?([\w.\\/:~-][^\s\"'`$;&|]*?\.(?:bat|cmd|ps1|sh|bash|zsh|py))"
    r"[\"']?", re.IGNORECASE)
_SCRIPT_MAX_BYTES = 256 * 1024


def _expand_scripts(command: str, cwd: Path, depth: int = 0,
                    _seen: set | None = None) -> str:
    """Append the bodies of script files the command invokes (depth-2) so
    git verbs hidden inside `script.bat` are still scanned."""
    if depth >= 2:
        return command
    seen = _seen if _seen is not None else set()
    parts = [command]
    for m in _SCRIPT_TOKEN_RE.finditer(command):
        token = m.group(1)
        if token.lower().endswith((".git.bat", ".git.cmd")):
            continue
        try:
            p = Path(os.path.expanduser(token))
            if not p.is_absolute():
                p = cwd / p
            p = Path(os.path.realpath(str(p)))
            key = os.path.normcase(str(p))
            if key in seen or not p.is_file():
                continue
            if p.stat().st_size > _SCRIPT_MAX_BYTES:
                continue
            seen.add(key)
            parts.append("\n" + _expand_scripts(
                p.read_text(encoding="utf-8", errors="replace"),
                p.parent, depth + 1, seen))
        except Exception:
            continue
    return "".join(parts)


def _target_dirs(command: str, cwd: Path) -> list[Path]:
    """Effective working dirs of each git invocation — ``cd``/``pushd``
    earlier in the chain and ``git -C <dir>`` / ``--work-tree <dir>``
    relocate the target away from the spawn cwd."""
    events: list[tuple[int, str]] = []
    for m in _GIT_SEG_RE.finditer(command):
        events.append((m.start(), "git\0" + (m.group(1) or "")))
    for m in _CD_RE.finditer(command):
        events.append((m.start(), "cd\0" + m.group(1)))
    out: list[Path] = []
    cur = cwd
    for _pos, ev in sorted(events, key=lambda e: e[0]):
        kind, val = ev.split("\0", 1)
        if kind == "cd":
            target = Path(val.strip("\"'"))
            cur = target if target.is_absolute() else cur / target
            continue
        base = cur
        seen = False
        for opt in _GIT_DIR_OPT_RE.finditer(val):
            target = Path(opt.group(1))
            if not target.is_absolute():
                target = base / target
            base = target
            seen = True
        out.append(base if seen else cur)
    return out or [cwd]


def _has_grant(registry, action: str, scope: str) -> bool:
    grant_fn = (getattr(registry, "context", None) or {}).get(
        "autonomy_grant")
    try:
        return bool(callable(grant_fn) and grant_fn(action, scope))
    except Exception:
        return False


def mission_git_tool_block(registry, root: Path, *,
                           remote: bool = False) -> str | None:
    """Gate for the dedicated git_* / github tools — autonomous missions
    may not push or mutate a primary checkout without a standing grant.
    ``root`` is the repo the tool call would act on."""
    if not _mission_attributed(registry):
        return None
    scope = str(_repo_root(root) or root)
    if remote:
        if not _has_grant(registry, "git_push", scope):
            return _BLOCK.format(sub="git push / remote changes",
                                 action="git_push")
        return None
    if _is_primary_dir(root) and not _has_grant(
            registry, "git_commit", scope):
        return _BLOCK.format(sub="git mutations in the primary checkout",
                             action="git_commit")
    return None


def autonomous_git_block(registry, command: str, cwd: Path,
                         workspace: Path) -> str | None:
    """Return a refusal string when a mission-attributed ``command`` runs
    an unsafe git mutation, else None (allowed to execute)."""
    if not _mission_attributed(registry):
        return None

    scope = str(_repo_root(cwd) or cwd)
    text = _expand_scripts(command, cwd)

    if _GIT_REMOTE_RE.search(text) or _GH_REMOTE_RE.search(text):
        if not _has_grant(registry, "git_push", scope):
            return _BLOCK.format(sub="git push / remote changes",
                                 action="git_push")
    elif _GIT_SHARED_RE.search(text):
        if not _has_grant(registry, "git_commit", scope):
            return _BLOCK.format(sub="git branch/tag/ref/config changes",
                                 action="git_commit")
    elif _GIT_MUTATE_RE.search(text) or _GH_MUTATE_RE.search(text):
        # `-C`/`--work-tree`/`cd` overrides: primary iff ANY targeted dir
        # is a primary checkout (.git is a dir, not a worktree pointer
        # file). Non-repo targets are left to git's own failure.
        if any(_is_primary_dir(d) for d in _target_dirs(text, cwd)):
            if not _has_grant(registry, "git_commit", scope):
                return _BLOCK.format(sub="git mutations in the primary "
                                         "checkout", action="git_commit")
    # Argv-form calls inside embedded scripts (python_exec bodies are
    # expanded above): targets can't be resolved, so any mutation needs
    # the grant — worktree-or-primary discrimination isn't possible.
    if _PY_ARGV_REMOTE.search(text):
        if not _has_grant(registry, "git_push", scope):
            return _BLOCK.format(sub="git push / remote changes",
                                 action="git_push")
    elif _PY_ARGV_MUTATE.search(text):
        if not _has_grant(registry, "git_commit", scope):
            return _BLOCK.format(sub="git mutations via script argv",
                                 action="git_commit")
    return None


def mission_python_block(registry, code: str) -> str | None:
    """Gate for `python_exec` bodies — the sandbox runs inside the
    workspace repo and argv-form targets can't be resolved, so any
    mutation-form git call needs the corresponding standing grant."""
    if not _mission_attributed(registry):
        return None
    if _GIT_REMOTE_RE.search(code) or _PY_ARGV_REMOTE.search(code):
        if not _has_grant(registry, "git_push", ""):
            return _BLOCK.format(sub="git push / remote changes",
                                 action="git_push")
        return None
    if _GIT_SHARED_RE.search(code) or _GIT_MUTATE_RE.search(code) \
            or _PY_ARGV_MUTATE.search(code):
        if not _has_grant(registry, "git_commit", ""):
            return _BLOCK.format(sub="git mutations via python_exec",
                                 action="git_commit")
    return None
