"""Dependency listing and security-audit tools.

`dep_list` parses the manifests it finds (requirements.txt,
pyproject.toml, package.json, Cargo.toml) — no network, always honest.
`project_audit` runs the real auditor for each detected ecosystem
(pip-audit / npm audit / cargo audit) and reports
`auditor_unavailable` per ecosystem rather than pretending a scan ran.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..procutil import no_window_flags
from .base import ToolRegistry, ToolSpec


def _parse_requirements(text: str) -> list[dict]:
    deps = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = line
        spec = ""
        for op in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            if op in line:
                name, spec = line.split(op, 1)
                spec = op + spec
                break
        name = name.split("[")[0].strip()
        if name:
            deps.append({"name": name, "spec": spec})
    return deps


def _parse_pyproject(text: str) -> list[dict]:
    deps = []
    try:
        import tomllib
        data = tomllib.loads(text)
        deps.extend({"name": d, "spec": ""} for d in
                    data.get("project", {}).get("dependencies") or [])
    except Exception:
        pass
    return deps


def _parse_package_json(text: str) -> list[dict]:
    deps = []
    try:
        data = json.loads(text)
        for section in ("dependencies", "devDependencies"):
            for name, spec in (data.get(section) or {}).items():
                deps.append({"name": name, "spec": spec,
                             "dev": section == "devDependencies"})
    except Exception:
        pass
    return deps


def _parse_cargo(text: str) -> list[dict]:
    deps = []
    try:
        import tomllib
        data = tomllib.loads(text)
        for section in ("dependencies", "dev-dependencies"):
            for name, spec in (data.get(section) or {}).items():
                deps.append({"name": name,
                             "spec": spec if isinstance(spec, str)
                             else (spec.get("version") or ""),
                             "dev": section.startswith("dev")})
    except Exception:
        pass
    return deps


_MANIFESTS = {
    "requirements.txt": _parse_requirements,
    "pyproject.toml": _parse_pyproject,
    "package.json": _parse_package_json,
    "Cargo.toml": _parse_cargo,
}

_AUDITORS = {
    "python": {
        "exe": "pip-audit",
        "argv": ["pip-audit", "--format", "json"],
        "ecosystems": ("requirements.txt", "pyproject.toml"),
    },
    "node": {
        "exe": "npm",
        "argv": ["npm", "audit", "--json"],
        "ecosystems": ("package.json",),
    },
    "rust": {
        "exe": "cargo",
        "argv": ["cargo", "audit", "--json"],
        "ecosystems": ("Cargo.toml",),
    },
}


def register_audit_tools(registry: ToolRegistry, workspace: Path, *,
                         extra_roots=None) -> None:

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

    def dep_list(args: dict[str, Any]) -> str:
        root = _resolve_root(str(args.get("path", "") or ""))
        if root is None:
            return json.dumps(
                {"error": "path must be a directory inside a registered workspace"})
        found = []
        for fname, parser in _MANIFESTS.items():
            f = root / fname
            if not f.is_file():
                continue
            deps = parser(f.read_text(encoding="utf-8", errors="replace"))
            if deps or fname == "requirements.txt":
                found.append({"manifest": fname, "count": len(deps),
                              "dependencies": deps})
        return json.dumps({"root": str(root), "manifests": found},
                          ensure_ascii=False)

    def project_audit(args: dict[str, Any]) -> str:
        root = _resolve_root(str(args.get("path", "") or ""))
        if root is None:
            return json.dumps(
                {"error": "path must be a directory inside a registered workspace"})
        present = [f for f in _MANIFESTS if (root / f).is_file()]
        if not present:
            return json.dumps({"root": str(root), "audits": [],
                               "note": "no dependency manifests found"})
        audits = []
        for eco_name, spec in _AUDITORS.items():
            matched = [m for m in present if m in spec["ecosystems"]]
            if not matched:
                continue
            if not shutil.which(spec["exe"]):
                audits.append({"ecosystem": eco_name,
                               "manifests": matched,
                               "status": "auditor_unavailable",
                               "install": {"python": "pip install pip-audit",
                                           "node": None,
                                           "rust": "cargo install cargo-audit"
                                           }[eco_name]})
                continue
            try:
                proc = subprocess.run(
                    spec["argv"], cwd=str(root),
                    capture_output=True, text=True, timeout=120,
                    creationflags=no_window_flags(),
                    shell=(spec["exe"] == "npm" and
                           __import__("os").name == "nt"), encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                audits.append({"ecosystem": eco_name,
                               "status": "timeout"})
                continue
            findings = proc.stdout or ""
            try:
                parsed = json.loads(findings)
            except Exception:
                parsed = findings[:8000]
            audits.append({"ecosystem": eco_name,
                           "manifests": matched,
                           "status": "ran",
                           "exit_code": proc.returncode,
                           "result": parsed})
        return json.dumps({"root": str(root), "audits": audits},
                          ensure_ascii=False)

    registry.register(ToolSpec(
        "dep_list",
        "List declared dependencies parsed from project manifests (requirements.txt, pyproject.toml, package.json, Cargo.toml). No network; pure manifest parse.",
        {
            "type": "object",
            "properties": {"path": {"type": "string",
                                    "description": "workspace-relative directory"}},
        },
        "filesystem.read",
        dep_list,
        category="coding",
        capabilities=["dep_list", "repository_search"],
    ))
    registry.register(ToolSpec(
        "project_audit",
        "Run the real dependency vulnerability auditor for each detected ecosystem (pip-audit, npm audit, cargo audit). Reports auditor_unavailable when the tool isn't installed — never fabricates findings.",
        {
            "type": "object",
            "properties": {"path": {"type": "string",
                                    "description": "workspace-relative directory"}},
        },
        "shell.execute",
        project_audit,
        category="coding",
        capabilities=["project_audit", "terminal_run"],
    ))
