from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

DEFAULT_MAX_RESULTS = 200
BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".exe", ".dll", ".so", ".dylib",
               ".zip", ".gz", ".tar", ".7z", ".pdf", ".gguf", ".bin", ".pyc", ".woff", ".woff2"}
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".agent"}


def find_ripgrep() -> str | None:
    exe = "rg.exe" if os.name == "nt" else "rg"
    return shutil.which(exe) or shutil.which("rg")


def _is_skipped(path: Path, workspace: Path) -> bool:
    try:
        rel = path.relative_to(workspace)
    except ValueError:
        return True
    return any(part in SKIP_DIRS for part in rel.parts[:-1])


def _run_rg(workspace: Path, argv: list[str]) -> tuple[list[str], int]:
    rg = find_ripgrep()
    if not rg:
        return [], -1
    try:
        proc = subprocess.run([rg, *argv], cwd=str(workspace), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return [], -1
    return proc.stdout.splitlines(), proc.returncode


def _rg_search(workspace: Path, query: str, *, glob_pat: str | None, max_results: int, insensitive: bool, fixed: bool) -> list[dict[str, Any]] | None:
    argv = ["--line-number", "--with-filename", "--color", "never", "--max-count", str(max_results)]
    if insensitive:
        argv.append("-i")
    if fixed:
        argv.append("--fixed-strings")
    if glob_pat:
        argv.extend(["-g", glob_pat])
    for d in SKIP_DIRS:
        argv.extend(["-g", f"!{d}/**"])
    argv.extend(["-e", query, "."])
    lines, code = _run_rg(workspace, argv)
    if code < 0:
        return None
    rows = []
    for line in lines[:max_results]:
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            rows.append({"path": parts[0].lstrip(".\\/"), "line": int(parts[1]), "text": parts[2][:400]})
    return rows


def _py_search(workspace: Path, query: str, *, glob_pat: str | None, max_results: int, insensitive: bool) -> list[dict[str, Any]]:
    regex = re.compile(re.escape(query), re.IGNORECASE if insensitive else 0)
    rows: list[dict[str, Any]] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if len(rows) >= max_results:
                return rows
            if glob_pat and not fnmatch.fnmatch(name, glob_pat):
                continue
            p = Path(root) / name
            if p.suffix.lower() in BINARY_EXTS:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rows.append({"path": str(p.relative_to(workspace)), "line": i, "text": line.strip()[:400]})
                    break
            if len(rows) >= max_results:
                break
    return rows


def _extract_error_terms(text: str) -> list[str]:
    terms: list[str] = []
    for tok in re.findall(r"[A-Za-z_][\w.]*Error[A-Za-z_]*|error CS\d+|TS\d{4,5}|FATAL: .+|Exception: .+", text):
        terms.append(tok.strip())
    if not terms:
        for line in text.splitlines():
            line = line.strip()
            if line and len(line) >= 12:
                terms.append(line[:160])
                break
    return terms[:5]


def register_search_tools(registry: ToolRegistry, workspace: Path) -> None:

    def search_code(args: dict[str, Any]) -> str:
        query = str(args["query"])
        glob_pat = args.get("glob")
        max_results = max(1, min(int(args.get("max_results", 50)), DEFAULT_MAX_RESULTS))
        insensitive = bool(args.get("case_insensitive", False))
        rows = _rg_search(workspace, query, glob_pat=glob_pat, max_results=max_results, insensitive=insensitive, fixed=False)
        engine = "ripgrep"
        if rows is None:
            rows = _py_search(workspace, query, glob_pat=glob_pat, max_results=max_results, insensitive=insensitive)
            engine = "python"
        return json.dumps({"engine": engine, "query": query, "count": len(rows), "matches": rows}, ensure_ascii=False)

    def search_filename(args: dict[str, Any]) -> str:
        pattern = str(args["pattern"])
        max_results = max(1, min(int(args.get("max_results", 100)), 500))
        rows: list[str] = []
        lines, code = _run_rg(workspace, ["--files"])
        if code >= 0:
            for line in lines:
                if fnmatch.fnmatch(Path(line).name, pattern) or fnmatch.fnmatch(line, pattern):
                    rows.append(line)
        else:
            for root, dirs, files in os.walk(workspace):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in files:
                    if fnmatch.fnmatch(name, pattern):
                        rows.append(str((Path(root) / name).relative_to(workspace)))
        return json.dumps({"pattern": pattern, "count": len(rows), "files": rows[:max_results]}, ensure_ascii=False)

    def search_error(args: dict[str, Any]) -> str:
        text = str(args["error_text"])
        terms = _extract_error_terms(text)
        if not terms:
            return json.dumps({"terms": [], "count": 0, "matches": []})
        all_rows: list[dict[str, Any]] = []
        for term in terms:
            rows = _rg_search(workspace, term, glob_pat=None, max_results=25, insensitive=False, fixed=True)
            if rows is None:
                rows = _py_search(workspace, term, glob_pat=None, max_results=25, insensitive=False)
            for row in rows:
                row = dict(row, term=term)
                if row not in all_rows:
                    all_rows.append(row)
        return json.dumps({"terms": terms, "count": len(all_rows), "matches": all_rows[:DEFAULT_MAX_RESULTS]}, ensure_ascii=False)

    registry.register(ToolSpec(
        "search_code",
        "High-speed code search across the workspace using ripgrep (falls back to a built-in scanner). Returns JSON matches with path/line/text.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "regex pattern (ripgrep syntax) or literal text"},
                "glob": {"type": "string", "description": "file glob filter, e.g. '*.py'"},
                "max_results": {"type": "integer", "default": 50},
                "case_insensitive": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
        "filesystem.read",
        search_code,
        category="coding",
        capabilities=["search_code", "search_text", "repository_search"],
    ))
    registry.register(ToolSpec(
        "search_filename",
        "Find files by filename glob across the workspace. Returns JSON file paths.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob like '*.py' or 'test_*.py'"},
                "max_results": {"type": "integer", "default": 100},
            },
            "required": ["pattern"],
        },
        "filesystem.read",
        search_filename,
        category="coding",
        capabilities=["search_filename", "repository_search"],
    ))
    registry.register(ToolSpec(
        "search_error",
        "Extract distinctive error terms from pasted build/test output and search the workspace for their source locations.",
        {
            "type": "object",
            "properties": {"error_text": {"type": "string"}},
            "required": ["error_text"],
        },
        "filesystem.read",
        search_error,
        category="coding",
        capabilities=["search_error", "repository_search"],
    ))
