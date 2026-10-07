"""Workspace Manager — first-class local project workspaces.

Nexus must be able to receive "Work in D:\\Projects\\MyApp": inspect the
directory, identify the project, adopt it as a workspace (creating it when
permitted), and persist everything needed to resume — root, detected
project type, toolchain, build system, runtime commands, dev server,
tests, git branch/remote, and project conventions.

Design notes:

* The server's *primary* workspace (passed at boot) anchors the
  per-workspace stores (.agent task/checkpoint/memory). Additional
  workspaces opened through this manager are *registered* — filesystem
  and shell tools accept paths inside them via ``allowed_roots()``, so
  agent work stays bounded to user-approved roots rather than the whole
  disk.
* Detection is heuristic and honest: it reports what it found, never
  what it wishes. Unknown project → ``project_type: "generic"``.
* Persistence is a single JSON document — workspaces survive restart and
  feed the recent-workspaces UI.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .procutil import no_window_flags
from typing import Any, Callable

from .fsutil import atomic_write_text

MAX_WORKSPACES = 64
IGNORED_SCAN_DIRS = {
    ".git", ".agent", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", "target", ".next", "bin", "obj",
}

_LANGUAGE_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".cs": "csharp",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".c": "c",
    ".h": "cpp", ".hpp": "cpp", ".rs": "rust", ".go": "go",
    ".java": "java", ".kt": "kotlin", ".html": "html",
    ".css": "css", ".vue": "vue", ".php": "php", ".rb": "ruby",
}


def _new_id(name: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "-" for c in name)[:32] \
        .strip("-") or "workspace"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def _git(root: Path, *args: str, timeout: int = 5) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, text=True,
            capture_output=True, timeout=timeout,
            creationflags=no_window_flags())
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def _detect_git_state(root: Path) -> dict[str, Any]:
    git: dict[str, Any] = {"repo": False}
    if not (root / ".git").exists():
        return git
    git["repo"] = True
    git["branch"] = _git(root, "rev-parse", "--abbrev-ref", "HEAD") or ""
    git["remote"] = _git(root, "remote", "get-url", "origin") or ""
    status = _git(root, "status", "--porcelain")
    git["dirty_files"] = len(
        [ln for ln in (status or "").splitlines() if ln.strip()])
    counts = _git(root, "rev-list", "--left-right", "--count",
                  "HEAD...@{u}")
    if counts and "\t" in counts:
        ahead, behind = counts.split("\t", 1)
        git["ahead"] = int(ahead or 0)
        git["behind"] = int(behind or 0)
    return git


def _detect_commands(root: Path) -> dict[str, str]:
    """Best-effort build/test/dev/run commands from project markers."""
    cmds: dict[str, str] = {}
    pkg = _read_json(root / "package.json")
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else {}
    if isinstance(scripts, dict) and scripts:
        if "build" in scripts:
            cmds["build"] = "npm run build"
        for test_name in ("test", "test:unit"):
            if test_name in scripts:
                cmds["test"] = f"npm run {test_name}"
                break
        for dev_name in ("dev", "start", "serve"):
            if dev_name in scripts:
                cmds["dev"] = f"npm run {dev_name}"
                break
    if (root / "pyproject.toml").is_file() or \
            (root / "requirements.txt").is_file():
        pytest = False
        for marker in ("pyproject.toml", "requirements.txt"):
            try:
                if "pytest" in (root / marker).read_text(
                        encoding="utf-8", errors="replace").lower():
                    pytest = True
                    break
            except OSError:
                pass
        cmds.setdefault("test",
                        "python -m pytest" if pytest
                        else "python -m unittest discover -s tests")
    if (root / "Makefile").is_file():
        try:
            text = (root / "Makefile").read_text(
                encoding="utf-8", errors="replace")
            for target in ("build", "test", "run", "dev"):
                if f"\n{target}:" in "\n" + text:
                    cmds.setdefault(target, f"make {target}")
        except OSError:
            pass
    if any(root.glob("*.sln")) or any(root.glob("*.csproj")):
        cmds.setdefault("build", "dotnet build")
        cmds.setdefault("test", "dotnet test")
    if (root / "CMakeLists.txt").is_file():
        cmds.setdefault("build", "cmake --build build")
    if (root / "Cargo.toml").is_file():
        cmds.setdefault("build", "cargo build")
        cmds.setdefault("test", "cargo test")
    if (root / "go.mod").is_file():
        cmds.setdefault("build", "go build ./...")
        cmds.setdefault("test", "go test ./...")
    return cmds


def _detect_project(root: Path) -> dict[str, Any]:
    """Identify project type, toolchain, and languages from markers."""
    info: dict[str, Any] = {
        "project_type": "generic",
        "toolchain": "",
        "build_system": "",
        "languages": [],
    }
    pkg = _read_json(root / "package.json")
    if pkg:
        info["build_system"] = "npm"
        info["toolchain"] = "node"
        deps = {k.lower() for k in (pkg.get("dependencies") or {})} \
            | {k.lower() for k in (pkg.get("devDependencies") or {})}
        if "next" in deps:
            info["project_type"] = "nextjs"
        elif "react" in deps:
            info["project_type"] = "react"
        elif "vue" in deps or (root / "vue.config.js").is_file():
            info["project_type"] = "vue"
        elif "@angular/core" in deps:
            info["project_type"] = "angular"
        else:
            info["project_type"] = "node"
    elif (root / "pyproject.toml").is_file() or \
            (root / "setup.py").is_file() or \
            (root / "requirements.txt").is_file():
        info["toolchain"] = "python"
        info["build_system"] = "pyproject" \
            if (root / "pyproject.toml").is_file() else "pip"
        blob = ""
        for marker in ("pyproject.toml", "requirements.txt"):
            try:
                blob += (root / marker).read_text(
                    encoding="utf-8", errors="replace").lower()
            except OSError:
                pass
        if "fastapi" in blob:
            info["project_type"] = "fastapi"
        elif "django" in blob:
            info["project_type"] = "django"
        elif "flask" in blob:
            info["project_type"] = "flask"
        else:
            info["project_type"] = "python"
    elif any(root.glob("*.sln")) or any(root.glob("*.csproj")):
        info["project_type"] = "dotnet"
        info["toolchain"] = "dotnet"
        info["build_system"] = "msbuild"
    elif (root / "CMakeLists.txt").is_file():
        info["project_type"] = "cpp_cmake"
        info["toolchain"] = "cmake"
        info["build_system"] = "cmake"
    elif (root / "Cargo.toml").is_file():
        info["project_type"] = "rust"
        info["toolchain"] = "rust"
        info["build_system"] = "cargo"
    elif (root / "go.mod").is_file():
        info["project_type"] = "go"
        info["toolchain"] = "go"
        info["build_system"] = "go"
    elif (root / "index.html").is_file():
        info["project_type"] = "static_site"
        info["toolchain"] = "browser"
        info["build_system"] = "none"
    # Language census — bounded walk, dominant first.
    counts: dict[str, int] = {}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_SCAN_DIRS]
        for fn in filenames:
            scanned += 1
            if scanned > 5000:
                break
            lang = _LANGUAGE_EXT.get(Path(fn).suffix.lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        if scanned > 5000:
            break
    info["languages"] = sorted(counts, key=counts.get, reverse=True)  # type: ignore[arg-type]
    return info


class WorkspaceManager:
    """Registered workspaces: inspection, adoption, persistence, and the
    boundary set file tools are allowed to touch."""

    def __init__(self, store_path: Path, primary_root: Path,
                 *, git_probe: Callable[..., str | None] | None = None) -> None:
        self.store_path = Path(store_path)
        self.primary_root = Path(primary_root).resolve()
        self._git = git_probe or _git
        self._lock = threading.RLock()
        self._records: list[dict[str, Any]] = []
        self._active_id: str | None = None
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        data = _read_json(self.store_path)
        self._records = [r for r in (data.get("workspaces") or [])
                         if isinstance(r, dict) and r.get("root")]
        self._active_id = data.get("active_id") or None

    def _save(self) -> None:
        payload = {
            "active_id": self._active_id,
            "workspaces": self._records[:MAX_WORKSPACES],
        }
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.store_path,
                              json.dumps(payload, indent=2))
        except OSError:
            pass

    # -- inspection ----------------------------------------------------------

    def inspect(self, path: str | Path) -> dict[str, Any]:
        """Full detection report for a directory — no persistence."""
        root = Path(path).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        report: dict[str, Any] = {
            "root": str(root),
            "exists": root.is_dir(),
            "name": root.name,
        }
        if not root.is_dir():
            return report
        try:
            report["is_empty"] = not any(root.iterdir())
        except OSError:
            report["is_empty"] = True
        report.update(_detect_project(root))
        report["commands"] = _detect_commands(root)
        report["git"] = _detect_git_state(root)
        report["writable"] = os.access(str(root), os.W_OK)
        report["is_primary"] = root == self.primary_root
        return report

    # -- adoption --------------------------------------------------------------

    def open(self, path: str | Path, *, create: bool = False) -> dict[str, Any]:
        """Inspect + register a workspace. When the directory is missing,
        ``create=True`` makes it (bounded to a normal directory create —
        never overwrites). Returns the persisted record."""
        root = Path(path).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        if not root.is_dir():
            if not create:
                raise FileNotFoundError(str(root))
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"cannot create workspace: {exc}") from exc
        report = self.inspect(root)
        with self._lock:
            rec = next((r for r in self._records
                        if r.get("root") == str(root)), None)
            if rec is None:
                rec = {
                    "id": _new_id(report.get("name") or "workspace"),
                    "root": str(root),
                    "created_at": time.time(),
                    "conventions": {},
                    "pinned": False,
                }
                self._records.insert(0, rec)
            rec.update({
                "name": report.get("name"),
                "project_type": report.get("project_type"),
                "toolchain": report.get("toolchain"),
                "build_system": report.get("build_system"),
                "languages": report.get("languages"),
                "commands": report.get("commands"),
                "git": report.get("git"),
                "last_active_at": time.time(),
            })
            self._active_id = rec["id"]
            self._records = self._records[:MAX_WORKSPACES]
            self._save()
        return dict(rec)

    def set_active(self, workspace_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self.get(workspace_id)
            if rec is None:
                return None
            rec["last_active_at"] = time.time()
            self._active_id = rec["id"]
            self._save()
            return dict(rec)

    def get(self, workspace_id: str) -> dict[str, Any] | None:
        for r in self._records:
            if r.get("id") == workspace_id or r.get("root") == workspace_id:
                return r
        return None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (dict(r) for r in self._records),
                key=lambda r: r.get("last_active_at") or 0, reverse=True)

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            rec = self.get(self._active_id or "")
            return dict(rec) if rec else None

    def remove(self, workspace_id: str) -> bool:
        with self._lock:
            rec = self.get(workspace_id)
            if rec is None:
                return False
            self._records.remove(rec)
            if self._active_id == rec.get("id"):
                self._active_id = None
            self._save()
            return True

    # -- boundaries -------------------------------------------------------------

    def allowed_roots(self) -> list[Path]:
        """Roots file/shell tools may operate inside: the primary
        workspace plus every registered workspace."""
        roots = [self.primary_root]
        with self._lock:
            for rec in self._records:
                try:
                    p = Path(str(rec.get("root") or "")).resolve()
                except OSError:
                    continue
                if p != self.primary_root and p not in roots:
                    roots.append(p)
        return roots

    def contains(self, path: str | Path) -> bool:
        try:
            p = Path(path).resolve()
        except OSError:
            return False
        return any(p == root or root in p.parents
                   for root in self.allowed_roots())
