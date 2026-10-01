"""Code intelligence tools: symbol maps and outlines.

Uses a lightweight per-language regex scanner today; a tree-sitter backend can
replace ``extract_symbols`` behind the same interface without touching callers.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

MAX_FILES = 400
MAX_FILE_BYTES = 512_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".agent"}

_PATTERNS: dict[str, list[tuple[str, str]]] = {
    ".py": [
        ("class", r"^\s*class\s+([A-Za-z_]\w*)"),
        ("function", r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
        ("import", r"^[ \t]*(?:from[ \t]+[\w.]+[ \t]+)?import[ \t]+([\w., \t]+)"),
    ],
    ".js": [
        ("class", r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"),
        ("function", r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
        ("function", r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
        ("import", r"^\s*import\s+.*?from\s+['\"]([^'\"]+)['\"]"),
    ],
    ".ts": [],
    ".jsx": [],
    ".tsx": [],
    ".cs": [
        ("class", r"^\s*(?:public|internal|private|protected|sealed|abstract|static|partial|\s)*\s*(?:class|interface|struct|record|enum)\s+([A-Za-z_]\w*)"),
        ("function", r"^\s*(?:public|private|protected|internal|static|async|virtual|override|sealed|partial|\s)*[\w<>\[\],? ]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?\s*$"),
        ("import", r"^\s*using\s+([\w.]+)"),
    ],
    ".c": [("function", r"^[A-Za-z_][\w\s\*]*\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{"), ("import", r"^\s*#\s*include\s*[<\"]([^>\"]+)")],
    ".cpp": [],
    ".h": [],
    ".hpp": [],
    ".go": [
        ("function", r"^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_]\w*)"),
        ("class", r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)"),
        ("import", r"^\s*import\s+\"([^\"]+)\""),
    ],
    ".rs": [
        ("function", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
        ("class", r"^\s*(?:pub\s+)?(?:struct|enum|trait|impl)\s+([A-Za-z_]\w*)"),
        ("import", r"^\s*use\s+([\w:]+)"),
    ],
    ".java": [
        ("class", r"^\s*(?:public|private|protected|abstract|final|static|\s)*\s*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)"),
        ("function", r"^\s*(?:public|private|protected|static|final|synchronized|abstract|\s)*[\w<>\[\],? ]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:throws\s+[\w,\s]+)?\{"),
        ("import", r"^\s*import\s+([\w.]+)"),
    ],
}
_PATTERNS[".ts"] = _PATTERNS[".js"]
_PATTERNS[".jsx"] = _PATTERNS[".js"]
_PATTERNS[".tsx"] = _PATTERNS[".js"]
_PATTERNS[".cpp"] = _PATTERNS[".c"]
_PATTERNS[".h"] = _PATTERNS[".c"]
_PATTERNS[".hpp"] = _PATTERNS[".c"]

_COMPILED = {
    ext: [(kind, re.compile(pat, re.MULTILINE)) for kind, pat in pats]
    for ext, pats in _PATTERNS.items()
}


def extract_symbols(path: Path) -> dict[str, Any]:
    ext = path.suffix.lower()
    rules = _COMPILED.get(ext)
    if not rules:
        return {"file": str(path), "symbols": [], "error": "unsupported_language"}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"file": str(path), "symbols": [], "error": str(exc)}
    if len(text.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
        return {"file": str(path), "symbols": [], "error": "file_too_large"}
    symbols = []
    for kind, regex in rules:
        for m in regex.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            symbols.append({"kind": kind, "name": m.group(1).strip()[:120], "line": line})
    symbols.sort(key=lambda s: s["line"])
    return {"file": str(path), "symbols": symbols, "lines": text.count("\n") + 1}


def register_codeintel_tools(registry: ToolRegistry, workspace: Path) -> None:
    workspace = workspace.resolve()

    def _resolve(raw: str) -> Path | None:
        p = (workspace / str(raw or "")).resolve()
        if not p.is_relative_to(workspace):
            return None
        return p

    def code_symbols(args: dict[str, Any]) -> str:
        p = _resolve(str(args.get("path", "")))
        if p is None:
            return "ERROR: path must stay inside the workspace"
        if not p.is_file():
            return f"ERROR: file not found: {p}"
        return json.dumps(extract_symbols(p), ensure_ascii=False)

    def code_map(args: dict[str, Any]) -> str:
        sub = _resolve(str(args.get("path", ""))) or workspace
        if not sub.is_dir():
            return "ERROR: path must be a directory inside the workspace"
        kinds = set(args.get("kinds") or ["class", "function"])
        limit = max(1, min(int(args.get("max_files", MAX_FILES)), 2000))
        files: dict[str, Any] = {}
        count = 0
        for root, dirs, names in os.walk(sub):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in sorted(names):
                if count >= limit:
                    break
                p = Path(root) / name
                if p.suffix.lower() not in _COMPILED:
                    continue
                info = extract_symbols(p)
                symbols = [s for s in info["symbols"] if s["kind"] in kinds]
                if symbols:
                    rel = str(p.relative_to(workspace))
                    files[rel] = symbols[:80]
                    count += 1
        return json.dumps({"root": str(sub.relative_to(workspace)) if sub != workspace else ".",
                           "files": len(files), "symbols": files}, ensure_ascii=False)

    registry.register(ToolSpec(
        "code_symbols",
        "Extract classes, functions, methods, and imports from a source file with line numbers. Use before editing to understand structure.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "filesystem.read",
        code_symbols,
        category="coding",
        capabilities=["code_symbols", "repository_search"],
    ))
    registry.register(ToolSpec(
        "code_map",
        "Map classes/functions across a workspace directory tree. Returns a JSON symbol index for navigation before deep code reads.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "kinds": {"type": "array", "items": {"type": "string"}},
                "max_files": {"type": "integer", "default": 400},
            },
        },
        "filesystem.read",
        code_map,
        category="coding",
        capabilities=["code_map", "repository_search"],
    ))
