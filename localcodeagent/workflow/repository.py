from __future__ import annotations

import ast
import json
import re
import threading
import time
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import Any


IGNORED_DIRS = {".git", ".agent", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cs", ".java",
    ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".kts", ".html", ".css", ".scss", ".json", ".toml",
    ".yaml", ".yml", ".md", ".txt", ".sql", ".sh", ".ps1", ".bat", ".cmake",
}


class RepositoryIndex:
    """Dependency-free lightweight repository index for local retrieval."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.path = self.workspace / ".agent" / "repo_index.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "generated_at": 0.0, "files": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, ValueError):
            pass

    @staticmethod
    def _symbols_for(path: Path, text: str) -> list[str]:
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(text)
                return [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))][:200]
            except SyntaxError:
                return []
        pattern = re.compile(r"\b(?:class|struct|interface|enum|function|def|fn|func)\s+([A-Za-z_][A-Za-z0-9_]*)")
        return pattern.findall(text)[:200]

    def build(self, max_files: int = 8000, max_file_bytes: int = 1_500_000) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.workspace.rglob("*")):
            if len(rows) >= max_files:
                break
            if not path.is_file() or any(part in IGNORED_DIRS for part in path.relative_to(self.workspace).parts):
                continue
            if path.suffix.lower() not in TEXT_EXTENSIONS and path.name not in {"Dockerfile", "Makefile", "CMakeLists.txt"}:
                continue
            try:
                stat = path.stat()
                if stat.st_size > max_file_bytes:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = path.relative_to(self.workspace).as_posix()
            rows.append({
                "path": rel,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "symbols": self._symbols_for(path, text),
                "preview": " ".join(text[:1200].split()),
            })
        with self._lock:
            self._data = {"version": 1, "generated_at": time.time(), "files": rows}
            atomic_write_text(self.path, json.dumps(self._data, ensure_ascii=False))
        return self.summary()

    def ensure(self) -> dict[str, Any]:
        if not self._data.get("files"):
            return self.build()
        return self.summary()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generated_at": self._data.get("generated_at", 0),
                "file_count": len(self._data.get("files", [])),
            }

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_./-]+", query) if len(t) >= 2]
        with self._lock:
            files = list(self._data.get("files", []))
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in files:
            path = row["path"].lower()
            symbols = " ".join(row.get("symbols", [])).lower()
            preview = row.get("preview", "").lower()
            score = 0
            for term in terms:
                if term in path:
                    score += 8
                if term in symbols:
                    score += 5
                if term in preview:
                    score += 1
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["path"]))
        return [row | {"score": score} for score, row in scored[:max(1, min(limit, 100))]]
