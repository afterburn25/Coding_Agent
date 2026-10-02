"""Nexus Skills — reusable packaged capabilities (Part E).

A skill is a directory with ``skill.json`` (or SKILL.md frontmatter):

    {"name": "github-development", "version": "1.0.0",
     "description": "...", "capabilities": [...], "tools": [...],
     "permissions": [...], "instructions": "...", "examples": [...],
     "policy": {"profile": "local_autonomous"}, "enabled": true}

Skills live under ``data/skills/`` (user) and ``skills/`` (bundled).
Install/update/enable/disable are permission-gated; enabled skills are
surfaced to the agent as instruction context + allowed tools.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text

_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


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
    data.setdefault("name", d.name)
    data.setdefault("version", "0.0.0")
    data["_dir"] = str(d)
    return data


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
            return json.loads(self._state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {"disabled": [], "versions": {}}

    def _save_state(self) -> None:
        atomic_write_text(self._state_path, json.dumps(self._state, indent=2))

    def _scan(self) -> None:
        self.skills: dict[str, dict] = {}
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

    # -- lifecycle ----------------------------------------------------------

    def install(self, source_dir: Path, *, validate: bool = True) -> dict:
        """Install/update a skill from a directory (user dir target)."""
        spec = _parse_skill_dir(Path(source_dir))
        if spec is None:
            return {"ok": False, "error": "no skill.json/SKILL.md found"}
        if validate:
            errs = self.validate(spec)
            if errs:
                return {"ok": False, "errors": errs}
        dest = self.user_dir / spec["name"]
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source_dir, dest)
        self._scan()
        return {"ok": True, "name": spec["name"], "version": spec["version"]}

    def remove(self, name: str) -> bool:
        skill = self.skills.get(name)
        if not skill or skill.get("bundled"):
            return False  # bundled skills can be disabled, not deleted
        shutil.rmtree(skill["_dir"], ignore_errors=True)
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

    # -- queries --------------------------------------------------------------

    def validate(self, spec: dict) -> list[str]:
        errs = []
        if not spec.get("name"):
            errs.append("missing name")
        if not spec.get("description"):
            errs.append("missing description")
        if not isinstance(spec.get("capabilities", []), list):
            errs.append("capabilities must be a list")
        for t in spec.get("tools", []):
            if not isinstance(t, str):
                errs.append("tools must be strings")
        return errs

    def list(self) -> list[dict]:
        dis = set(self._state.get("disabled", []))
        return [{"name": s["name"], "version": s["version"],
                 "description": s.get("description", ""),
                 "enabled": s["name"] not in dis,
                 "bundled": s.get("bundled", False),
                 "capabilities": s.get("capabilities", []),
                 "permissions": s.get("permissions", [])}
                for s in sorted(self.skills.values(),
                                key=lambda x: x["name"])]

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
