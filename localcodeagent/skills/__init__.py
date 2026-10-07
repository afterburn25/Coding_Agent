"""Nexus Skills — reusable packaged capabilities (Part E).

A skill is a directory with ``skill.json`` (or SKILL.md frontmatter):

    {"name": "github-development", "version": "1.0.0",
     "description": "...", "author": "...", "capabilities": [...],
     "tools": [...], "permissions": [...], "dependencies": [...],
     "supported_os": ["windows"], "instructions": "...",
     "examples": [...], "health_check": {"command": [...]},
     "update": {"source": "..."}, "ui": {...}}

Skills live under ``data/skills/`` (user) and ``skills/`` (bundled).
Discovery/verification are read-only. Install/update/rollback/remove and
executable health checks are permission-gated at the API boundary; enabled
skills surface bounded instruction context + allowed tools to the agent.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text
from ..permissions import KNOWN_PERMISSIONS
from ..procutil import no_window_flags

_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_BROAD_PERMISSIONS = {"*", "all", "admin", "root", "filesystem.*", "shell.*",
                      "network.*", "desktop.*", "computer.*", "credentials.*"}
_ROLLBACK_LIMIT = 3
_CHECKSUM_FILE_LIMIT = 10_000
_CHECKSUM_BYTE_LIMIT = 256 * 1024 * 1024


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, (str, int, float))]


def _parse_skill_dir(d: Path) -> dict | None:
    spec = d / "skill.json"
    md = d / "SKILL.md"
    if spec.is_file():
        try:
            data = json.loads(spec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    elif md.is_file():
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            return None
        m = _FRONT_RE.match(text)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except ValueError:
            # YAML-lite fallback: key: value lines
            data = {}
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    data[k.strip()] = v.strip()
        data.setdefault("instructions", text[m.end():].strip()[:4000])
    else:
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("name", d.name)
    data.setdefault("version", "0.0.0")
    data["_dir"] = str(d)
    return data


def _current_os() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("darwin"):
        return "macos"
    return sys.platform


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class SkillRegistry:
    def __init__(self, workspace: Path, *,
                 user_dir: Path | None = None,
                 bundled_dir: Path | None = None) -> None:
        self.workspace = Path(workspace)
        self.user_dir = Path(user_dir) if user_dir else (
            self.workspace / "data" / "skills")
        self.bundled_dir = Path(bundled_dir) if bundled_dir else (
            self.workspace / "skills")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state_path = self.user_dir / "_state.json"
        self._state = self._load_state()
        self._scan()

    def _load_state(self) -> dict:
        try:
            state = json.loads(self._state_path.read_text("utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("disabled", [])
        state.setdefault("versions", {})
        state.setdefault("installed", {})
        state.setdefault("rollback", {})
        return state

    def _save_state(self) -> None:
        atomic_write_text(self._state_path, json.dumps(self._state, indent=2))

    def _scan(self) -> None:
        self.skills: dict[str, dict] = {}
        self.scan_errors: list[dict[str, str]] = []
        for base in (self.bundled_dir, self.user_dir):
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                if not d.is_dir() or d.name.startswith("_"):
                    continue
                spec = _parse_skill_dir(d)
                if spec:
                    spec["bundled"] = base == self.bundled_dir
                    self.skills[spec["name"]] = spec
                else:
                    self.scan_errors.append({
                        "path": str(d),
                        "error": "missing or invalid skill.json/SKILL.md",
                    })

    # -- summaries / verification -------------------------------------------

    def _summary(self, spec: dict) -> dict[str, Any]:
        name = str(spec.get("name") or "")
        dis = set(self._state.get("disabled", []))
        rollback = self._state.get("rollback", {}).get(name) or []
        return {
            "name": name,
            "version": str(spec.get("version") or "0.0.0"),
            "description": str(spec.get("description") or ""),
            "author": str(spec.get("author") or spec.get("provider") or ""),
            "provider": str(spec.get("provider") or spec.get("author") or ""),
            "enabled": name not in dis,
            "bundled": bool(spec.get("bundled")),
            "capabilities": _string_list(spec.get("capabilities")),
            "tools": _string_list(spec.get("tools")),
            "permissions": _string_list(spec.get("permissions")),
            "dependencies": _string_list(spec.get("dependencies")),
            "supported_os": _string_list(spec.get("supported_os")),
            "requires_network": bool(spec.get("requires_network", False)),
            "requires_gpu": bool(spec.get("requires_gpu", False)),
            "docs": str(spec.get("docs") or ""),
            "ui": spec.get("ui") if isinstance(spec.get("ui"), dict) else {},
            "has_install": isinstance(spec.get("install"), dict),
            "has_health_check": isinstance(spec.get("health_check"), dict),
            "update_source": spec.get("update") or spec.get("source") or "",
            "path": str(spec.get("_dir") or ""),
            "rollback_count": len(rollback) if isinstance(rollback, list) else 0,
            "installed_at": self._state.get("installed", {}).get(name),
        }

    def list(self) -> list[dict]:
        return [self._summary(s) for s in sorted(
            self.skills.values(), key=lambda x: x["name"])]

    def detail(self, name: str) -> dict | None:
        spec = self.skills.get(str(name))
        if spec is None:
            return None
        out = self._summary(spec)
        out.update({
            "instructions": str(spec.get("instructions") or "")[:4000],
            "examples": _string_list(spec.get("examples")),
            "install": spec.get("install") if isinstance(spec.get("install"), dict) else {},
            "health_check": spec.get("health_check")
                if isinstance(spec.get("health_check"), dict) else {},
            "policy": spec.get("policy") if isinstance(spec.get("policy"), dict) else {},
            "verification": self.verify(name),
        })
        return out

    def validate(self, spec: dict) -> list[str]:
        errs = []
        name = str(spec.get("name") or "")
        if not name:
            errs.append("missing name")
        elif not _NAME_RE.fullmatch(name):
            errs.append("name must be 1-80 chars and contain only letters, numbers, '.', '_' or '-'")
        if not spec.get("description"):
            errs.append("missing description")
        for key in ("capabilities", "tools", "permissions", "dependencies",
                    "supported_os", "examples"):
            if key in spec and not isinstance(spec.get(key), list):
                errs.append(f"{key} must be a list")
        for perm in _string_list(spec.get("permissions")):
            if perm.strip().lower() in _BROAD_PERMISSIONS or perm.strip().endswith(".*"):
                errs.append(f"permission '{perm}' is too broad; declare a specific permission")
        for key in ("install", "health_check", "ui", "policy"):
            if key in spec and not isinstance(spec.get(key), dict):
                errs.append(f"{key} must be an object")
        supported = {x.lower() for x in _string_list(spec.get("supported_os"))}
        if supported and _current_os() not in supported and sys.platform not in supported:
            errs.append(f"skill does not support this OS ({_current_os()})")
        return errs

    def verify(self, target: str | Path) -> dict[str, Any]:
        """Read-only package verification: manifest shape, permissions,
        dependencies, OS compatibility, and a bounded package digest."""
        path = Path(str(target)).expanduser()
        spec: dict | None = None
        origin = "installed"
        if path.exists() or any(sep in str(target) for sep in ("/", "\\")):
            origin = "source"
            spec = _parse_skill_dir(path) if path.is_dir() else None
            if spec is None:
                return {"ok": False, "target": str(target), "origin": origin,
                        "errors": ["missing or invalid skill.json/SKILL.md"],
                        "warnings": []}
        else:
            spec = self.skills.get(str(target))
            if spec is None:
                return {"ok": False, "target": str(target), "origin": origin,
                        "errors": ["unknown skill"], "warnings": []}

        errors = self.validate(spec)
        warnings: list[str] = []
        unknown = [p for p in _string_list(spec.get("permissions"))
                   if p not in KNOWN_PERMISSIONS]
        for perm in unknown:
            warnings.append(
                f"permission '{perm}' is not a built-in key and will default to ask")
        for dep in _string_list(spec.get("dependencies")):
            if dep not in self.skills:
                warnings.append(f"dependency '{dep}' is not an installed skill")
        digest, files, truncated = self._package_digest(Path(str(spec.get("_dir") or "")))
        if truncated:
            warnings.append("package digest was truncated by safety limits")
        if errors:
            ok = False
        else:
            ok = True
        return {
            "ok": ok,
            "target": str(spec.get("name") or target),
            "origin": origin,
            "errors": errors,
            "warnings": warnings,
            "requested_permissions": _string_list(spec.get("permissions")),
            "capabilities": _string_list(spec.get("capabilities")),
            "tools": _string_list(spec.get("tools")),
            "dependencies": _string_list(spec.get("dependencies")),
            "supported_os": _string_list(spec.get("supported_os")),
            "version": str(spec.get("version") or "0.0.0"),
            "sha256": digest,
            "files": files,
            "install": spec.get("install") if isinstance(spec.get("install"), dict) else {},
            "health_check": bool(spec.get("health_check")),
        }

    @staticmethod
    def _package_digest(root: Path) -> tuple[str, int, bool]:
        if not root.is_dir():
            return "", 0, False
        h = hashlib.sha256()
        files = 0
        total = 0
        truncated = False
        try:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                files += 1
                if files > _CHECKSUM_FILE_LIMIT or total > _CHECKSUM_BYTE_LIMIT:
                    truncated = True
                    break
                rel = path.relative_to(root).as_posix()
                data = path.read_bytes()
                total += len(data)
                h.update(rel.encode("utf-8", "surrogateescape"))
                h.update(b"\0")
                h.update(data)
        except OSError:
            return "", files, True
        return h.hexdigest(), files, truncated

    # -- lifecycle -----------------------------------------------------------

    def _backup_root(self, name: str) -> Path:
        return self.user_dir / "_backups" / re.sub(r"[^A-Za-z0-9_.-]", "_", name)

    def _record_rollback(self, name: str, backup: Path, version: str) -> None:
        entries = self._state.setdefault("rollback", {}).setdefault(name, [])
        try:
            stored_path = backup.resolve().relative_to(self.user_dir.resolve()).as_posix()
        except ValueError:
            stored_path = str(backup)
        entries.insert(0, {"path": stored_path, "version": version,
                           "created_at": time.time()})
        stale = entries[_ROLLBACK_LIMIT:]
        del entries[_ROLLBACK_LIMIT:]
        for row in stale:
            shutil.rmtree(str(self._rollback_path(row)), ignore_errors=True)

    def _rollback_path(self, row: dict) -> Path:
        raw = Path(str(row.get("path") or "")).expanduser()
        path = raw if raw.is_absolute() else self.user_dir / raw
        return path.resolve()

    def _replace_dir(self, dest: Path, source: Path) -> None:
        previous = self.user_dir / f"_previous-{dest.name}-{time.time_ns()}"
        try:
            if dest.exists():
                dest.rename(previous)
            source.rename(dest)
        except Exception:
            if not dest.exists() and previous.exists():
                previous.rename(dest)
            raise
        finally:
            if previous.exists():
                shutil.rmtree(previous, ignore_errors=True)

    def install(self, source_dir: Path, *, validate: bool = True) -> dict:
        """Install/update a skill from a directory (user dir target).

        Updates are transactional and keep a bounded rollback snapshot of the
        replaced user skill under data/skills/_backups/<name>/.
        """
        source = Path(source_dir).expanduser().resolve()
        spec = _parse_skill_dir(source)
        if spec is None:
            return {"ok": False, "error": "no skill.json/SKILL.md found"}
        if validate:
            errs = self.validate(spec)
            if errs:
                return {"ok": False, "errors": errs,
                        "requested_permissions": _string_list(spec.get("permissions")),
                        "capabilities": _string_list(spec.get("capabilities"))}
        name = str(spec["name"])
        dest = (self.user_dir / name).resolve()
        if not _is_relative_to(dest, self.user_dir.resolve()):
            return {"ok": False, "errors": ["skill name resolves outside the managed directory"]}
        existing = self.skills.get(name)
        was_user_skill = bool(existing and not existing.get("bundled") and
                              Path(str(existing.get("_dir") or "")).resolve() == dest)
        if source == dest:
            self._state.setdefault("versions", {})[name] = str(spec.get("version") or "0.0.0")
            self._state.setdefault("installed", {})[name] = time.time()
            self._save_state()
            self._scan()
            return {"ok": True, "name": name, "version": spec["version"],
                    "updated": bool(existing), "unchanged_path": True}

        stage = Path(tempfile.mkdtemp(prefix="_incoming-", dir=self.user_dir)).resolve()
        shutil.rmtree(stage)
        try:
            shutil.copytree(source, stage)
            staged = _parse_skill_dir(stage)
            if staged is None:
                return {"ok": False, "error": "staged package is missing skill.json/SKILL.md"}
            if validate:
                errs = self.validate(staged)
                if errs:
                    return {"ok": False, "errors": errs}
            backup_path = None
            if was_user_skill:
                backup_path = self._backup_root(name) / f"{existing.get('version', '0')}-{time.time_ns()}"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(dest, backup_path)
            try:
                self._replace_dir(dest, stage)
            except Exception as exc:
                if backup_path is not None:
                    shutil.rmtree(backup_path, ignore_errors=True)
                return {"ok": False, "error": f"install failed: {type(exc).__name__}: {exc}"}
            if backup_path is not None:
                self._record_rollback(name, backup_path, str(existing.get("version") or "0.0.0"))
            self._state.setdefault("versions", {})[name] = str(staged.get("version") or "0.0.0")
            self._state.setdefault("installed", {})[name] = time.time()
            self._save_state()
            self._scan()
            return {"ok": True, "name": name, "version": staged["version"],
                    "updated": bool(existing),
                    "previous_version": str(existing.get("version")) if existing else "",
                    "rollback_available": backup_path is not None,
                    "requested_permissions": _string_list(staged.get("permissions")),
                    "capabilities": _string_list(staged.get("capabilities"))}
        except Exception as exc:
            return {"ok": False, "error": f"install failed: {type(exc).__name__}: {exc}"}
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def update(self, source_dir: Path) -> dict:
        spec = _parse_skill_dir(Path(source_dir).expanduser())
        if spec is None:
            return {"ok": False, "error": "no skill.json/SKILL.md found"}
        if spec.get("name") not in self.skills:
            return {"ok": False, "error": f"skill '{spec.get('name')}' is not installed"}
        out = self.install(source_dir)
        if out.get("ok"):
            out["action"] = "update"
        return out

    def rollback(self, name: str) -> dict:
        name = str(name)
        entries = self._state.get("rollback", {}).get(name) or []
        if not entries:
            return {"ok": False, "error": "no rollback snapshot available"}
        row = entries[0]
        backup = self._rollback_path(row)
        root = self._backup_root(name).resolve()
        if not _is_relative_to(backup, root) or not backup.is_dir():
            return {"ok": False, "error": "rollback snapshot is missing or invalid"}
        spec = _parse_skill_dir(backup)
        if spec is None or self.validate(spec):
            return {"ok": False, "error": "rollback snapshot failed verification"}
        dest = self.user_dir / name
        stage = Path(tempfile.mkdtemp(prefix="_rollback-", dir=self.user_dir)).resolve()
        shutil.rmtree(stage)
        try:
            shutil.copytree(backup, stage)
            self._replace_dir(dest, stage)
        except Exception as exc:
            return {"ok": False, "error": f"rollback failed: {type(exc).__name__}: {exc}"}
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        entries.pop(0)
        if not entries:
            self._state.get("rollback", {}).pop(name, None)
        self._state.setdefault("versions", {})[name] = str(spec.get("version") or "0.0.0")
        self._save_state()
        self._scan()
        shutil.rmtree(backup, ignore_errors=True)
        return {"ok": True, "name": name, "version": spec.get("version"),
                "restored_from": str(row.get("version") or "")}

    def remove(self, name: str) -> bool:
        name = str(name)
        skill = self.skills.get(name)
        skill_dir = Path(str(skill.get("_dir") or "")).resolve() if skill else None
        if (not skill or skill.get("bundled") or skill_dir is None or
                not _is_relative_to(skill_dir, self.user_dir.resolve())):
            return False  # bundled skills can be disabled, not deleted
        shutil.rmtree(skill_dir, ignore_errors=True)
        shutil.rmtree(self._backup_root(name), ignore_errors=True)
        self._state.get("rollback", {}).pop(name, None)
        self._state.get("versions", {}).pop(name, None)
        self._state.get("installed", {}).pop(name, None)
        self._save_state()
        self._scan()
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self.skills:
            return False
        dis = self._state.setdefault("disabled", [])
        if enabled:
            self._state["disabled"] = [n for n in dis if n != name]
        elif name not in dis:
            dis.append(name)
        self._save_state()
        return True

    def health(self, name: str, *, run: bool = False) -> dict[str, Any]:
        spec = self.skills.get(str(name))
        if spec is None:
            return {"ok": False, "status": "missing", "detail": "unknown skill"}
        errors = self.validate(spec)
        if errors:
            return {"ok": False, "status": "invalid", "detail": "; ".join(errors)}
        check = spec.get("health_check") if isinstance(spec.get("health_check"), dict) else {}
        command = _string_list(check.get("command"))
        if not command:
            return {"ok": True, "status": "unknown", "detail": "no health check declared"}
        if not run:
            return {"ok": True, "status": "declared", "detail": "health check not executed",
                    "command": command}
        exe = command[0]
        resolved = exe if Path(exe).is_file() else (shutil.which(exe) or exe)
        try:
            proc = subprocess.run(
                [resolved, *command[1:]],
                cwd=str(spec.get("_dir") or self.user_dir),
                capture_output=True,
                text=True,
                timeout=max(1, min(int(check.get("timeout_seconds", 30)), 120)),
                creationflags=no_window_flags(),
            )
        except FileNotFoundError:
            return {"ok": False, "status": "missing", "detail": f"health executable not found: {exe}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "status": "timeout", "detail": "health check timed out"}
        return {
            "ok": proc.returncode == 0,
            "status": "healthy" if proc.returncode == 0 else "unhealthy",
            "detail": ((proc.stderr or proc.stdout or "")[-500:]),
        }

    # -- queries ---------------------------------------------------------------

    def enabled(self) -> list[dict]:
        dis = set(self._state.get("disabled", []))
        return [s for s in self.skills.values() if s["name"] not in dis]

    def instructions_for(self, names: list[str] | None = None) -> str:
        """Instruction context to inject into an agent prompt."""
        chunks = []
        for s in self.enabled():
            if names and s["name"] not in names:
                continue
            chunks.append(f"### Skill: {s['name']} (v{s['version']})\n"
                          f"{s.get('instructions', '')[:2000]}")
        return "\n\n".join(chunks)

    def allowed_tools(self, names: list[str] | None = None) -> set[str]:
        tools: set[str] = set()
        for s in self.enabled():
            if names and s["name"] not in names:
                continue
            tools.update(s.get("tools", []))
        return tools
