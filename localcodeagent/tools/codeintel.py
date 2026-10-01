"""Code intelligence tools: symbol maps and outlines.

Python files are parsed with the stdlib ``ast`` module (precise: nested
classes, methods, imports). Other languages use an optional tree-sitter
backend when the `tree-sitter` core pack plus a language pack are installed
(lazy-loaded, no hard dependency), falling back to a per-language regex
scanner. The result's ``backend`` field reports which path ran.
"""
from __future__ import annotations

import ast
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

# Optional tree-sitter upgrade: ext -> (pip module, language-factory attr).
# Loaded lazily; missing packs simply fall back to the regex scanner.
_TS_LANG_SPECS = {
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".c": ("tree_sitter_c", "language"),
    ".h": ("tree_sitter_c", "language"),
    ".cpp": ("tree_sitter_cpp", "language"),
    ".hpp": ("tree_sitter_cpp", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".go": ("tree_sitter_go", "language"),
    ".java": ("tree_sitter_java", "language"),
}
_TS_FUNCTION_NODES = {
    "function_definition", "function_declaration", "function_item",
    "method_definition", "method_declaration", "function_signature",
}
_TS_CLASS_NODES = {
    "class_definition", "class_declaration", "struct_item", "impl_item",
    "interface_declaration", "enum_declaration", "enum_item",
}
_TS_IMPORT_NODES = {
    "import_statement", "import_from_statement", "import_declaration",
    "use_declaration", "use_clause",
}
_TS_LANGUAGES: dict[str, Any] = {}


def _ts_language(ext: str):
    """Return a tree-sitter Language for `ext`, or None when unavailable."""
    if ext in _TS_LANGUAGES:
        return _TS_LANGUAGES[ext]
    lang = None
    spec = _TS_LANG_SPECS.get(ext)
    if spec:
        try:
            import importlib
            import tree_sitter
            module = importlib.import_module(spec[0])
            factory = getattr(module, spec[1])
            lang = tree_sitter.Language(factory())
        except Exception:
            lang = None
    _TS_LANGUAGES[ext] = lang
    return lang


def _extract_treesitter(text: str, ext: str) -> list[dict[str, Any]] | None:
    """Tree-sitter extraction for non-Python languages; None → regex fallback."""
    lang = _ts_language(ext)
    if lang is None:
        return None
    try:
        import tree_sitter
        parser = tree_sitter.Parser(lang)
        tree = parser.parse(text.encode("utf-8", errors="ignore"))
    except Exception:
        return None
    symbols: list[dict[str, Any]] = []

    def node_name(node) -> str:
        named = node.child_by_field_name("name")
        if named is not None:
            return text[named.start_byte:named.end_byte][:120]
        for child in node.children:
            if child.type in {"identifier", "type_identifier", "field_identifier",
                              "property_identifier", "qualified_identifier"}:
                return text[child.start_byte:child.end_byte][:120]
        return "?"

    def walk(node) -> None:
        for child in node.children:
            if child.type in _TS_FUNCTION_NODES:
                symbols.append({"kind": "function", "name": node_name(child),
                                "line": child.start_point[0] + 1})
                walk(child)
            elif child.type in _TS_CLASS_NODES:
                symbols.append({"kind": "class", "name": node_name(child),
                                "line": child.start_point[0] + 1})
                walk(child)
            elif child.type in _TS_IMPORT_NODES:
                symbols.append({"kind": "import",
                                "name": text[child.start_byte:child.end_byte].split("\n")[0].strip()[:120],
                                "line": child.start_point[0] + 1})
            else:
                walk(child)

    walk(tree.root_node)
    symbols.sort(key=lambda s: s["line"])
    return symbols


def _extract_python(text: str) -> list[dict[str, Any]] | None:
    """AST-based extraction for Python; None signals fallback to regex."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    symbols: list[dict[str, Any]] = []

    def walk(node: ast.AST, depth: int = 0) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if depth else "function"
                entry: dict[str, Any] = {"kind": kind, "name": child.name[:120], "line": child.lineno}
                decorators = [ast.unparse(d) for d in child.decorator_list if d is not None]
                if decorators:
                    entry["decorators"] = decorators[:5]
                symbols.append(entry)
                walk(child, depth + 1)
            elif isinstance(child, ast.ClassDef):
                bases = [ast.unparse(b) for b in child.bases if b is not None]
                entry = {"kind": "class", "name": child.name[:120], "line": child.lineno}
                if bases:
                    entry["bases"] = bases[:5]
                symbols.append(entry)
                walk(child, depth + 1)
            elif isinstance(child, ast.Import):
                symbols.append({"kind": "import", "name": ", ".join(a.name for a in child.names)[:120],
                                "line": child.lineno})
            elif isinstance(child, ast.ImportFrom):
                mod = "." * child.level + (child.module or "")
                symbols.append({"kind": "import", "name": f"{mod}: {', '.join(a.name for a in child.names)}"[:120],
                                "line": child.lineno})
            elif depth == 0:
                walk(child, depth)

    walk(tree)
    symbols.sort(key=lambda s: s["line"])
    return symbols


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
    if ext == ".py":
        ast_symbols = _extract_python(text)
        if ast_symbols is not None:
            return {"file": str(path), "symbols": ast_symbols, "lines": text.count("\n") + 1,
                    "backend": "ast"}
    else:
        ts_symbols = _extract_treesitter(text, ext)
        if ts_symbols is not None:
            return {"file": str(path), "symbols": ts_symbols, "lines": text.count("\n") + 1,
                    "backend": "tree-sitter"}
    symbols = []
    for kind, regex in rules:
        for m in regex.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            symbols.append({"kind": kind, "name": m.group(1).strip()[:120], "line": line})
    symbols.sort(key=lambda s: s["line"])
    return {"file": str(path), "symbols": symbols, "lines": text.count("\n") + 1,
            "backend": "regex"}


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
        kinds = set(args.get("kinds") or ["class", "function", "method"])
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
