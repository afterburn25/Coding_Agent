from __future__ import annotations

import difflib
import os
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import TYPE_CHECKING, Any

from .base import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from ..workflow.checkpoint import CheckpointManager
    from ..workflow.tasks import TaskStore


IGNORED_DIRS = {".git", ".agent", "node_modules", ".venv", "venv", "__pycache__"}


def _safe_path(workspace: Path, raw: str) -> Path:
    candidate = (workspace / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    root = workspace.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes the selected workspace")
    try:
        rel = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes the selected workspace") from exc
    if rel.parts and rel.parts[0] == ".agent":
        raise ValueError(".agent contains Local Code Agent metadata and cannot be modified through workspace tools")
    return candidate


def register_filesystem_tools(
    registry: ToolRegistry,
    workspace: Path,
    *,
    checkpoints: CheckpointManager | None = None,
    tasks: TaskStore | None = None,
) -> None:
    root = workspace.resolve()

    def track_mutation(path: Path) -> None:
        tls = registry.context.get("task_tls")
        task_id = str(getattr(tls, "task_id", "") or registry.context.get("task_id", ""))
        if not task_id:
            return
        if checkpoints:
            checkpoints.snapshot(task_id, path)
        if tasks:
            tasks.add_changed_file(task_id, path.relative_to(root).as_posix())

    def list_files(args: dict) -> str:
        path = _safe_path(workspace, args.get("path", "."))
        depth = max(0, min(int(args.get("depth", 2)), 5))
        lines: list[str] = []
        base_depth = len(path.parts)
        for p in sorted(path.rglob("*")):
            relative_depth = len(p.parts) - base_depth
            if relative_depth > depth:
                continue
            rel_to_root = p.relative_to(root)
            if any(part in IGNORED_DIRS for part in rel_to_root.parts):
                continue
            lines.append(str(rel_to_root) + ("/" if p.is_dir() else ""))
            if len(lines) >= 1000:
                lines.append("...truncated...")
                break
        return "\n".join(lines) or "(empty)"

    def read_file(args: dict) -> str:
        path = _safe_path(workspace, args["path"])
        text = path.read_text(encoding="utf-8", errors="replace")
        start = max(1, int(args.get("start_line", 1)))
        end = int(args.get("end_line", start + 399))
        rows = text.splitlines()
        return "\n".join(f"{i+1}: {rows[i]}" for i in range(start-1, min(end, len(rows))))

    def write_file(args: dict) -> str:
        path = _safe_path(workspace, args["path"])
        track_mutation(path)
        atomic_write_text(path, args.get("content", ""))
        return f"WROTE {path.relative_to(root)} ({path.stat().st_size} bytes)"

    def apply_patch(args: dict) -> str:
        changes = args.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError("changes must be a non-empty array")

        staged: list[tuple[Path, str | None, str]] = []
        # Validate and stage the complete patchset before touching disk.
        for item in changes:
            if not isinstance(item, dict) or not item.get("path"):
                raise ValueError("every patch change requires a path")
            path = _safe_path(workspace, str(item["path"]))
            existed = path.exists()
            old_text = path.read_text(encoding="utf-8", errors="replace") if existed and path.is_file() else ""
            if path.exists() and not path.is_file():
                raise ValueError(f"cannot patch directory: {path.relative_to(root)}")

            modes = sum(1 for key in ("content", "replacements", "delete") if key in item and item.get(key) not in (None, False, []))
            if modes != 1:
                raise ValueError(f"{path.relative_to(root)} must specify exactly one of content, replacements, or delete")

            if item.get("delete"):
                if not existed:
                    raise ValueError(f"cannot delete missing file: {path.relative_to(root)}")
                new_text: str | None = None
            elif "content" in item:
                new_text = str(item.get("content", ""))
            else:
                if not existed:
                    raise ValueError(f"cannot apply replacements to missing file: {path.relative_to(root)}")
                new_text = old_text
                replacements = item.get("replacements")
                if not isinstance(replacements, list) or not replacements:
                    raise ValueError(f"{path.relative_to(root)} replacements must be non-empty")
                for replacement in replacements:
                    old = str(replacement.get("old", ""))
                    new = str(replacement.get("new", ""))
                    expected = max(1, int(replacement.get("expected_count", 1)))
                    if not old:
                        raise ValueError(f"{path.relative_to(root)} replacement old text cannot be empty")
                    actual = new_text.count(old)
                    if actual != expected:
                        raise ValueError(
                            f"{path.relative_to(root)} replacement expected {expected} exact match(es) but found {actual}"
                        )
                    new_text = new_text.replace(old, new, expected)
            staged.append((path, new_text, old_text))

        diff_lines: list[str] = []
        for path, new_text, old_text in staged:
            track_mutation(path)
            rel = path.relative_to(root).as_posix()
            if new_text is None:
                path.unlink()
                new_lines: list[str] = []
            else:
                atomic_write_text(path, new_text)
                new_lines = new_text.splitlines(keepends=True)
            diff_lines.extend(difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            ))
        diff = "".join(diff_lines)
        if len(diff) > 24000:
            diff = diff[:24000] + "\n...diff truncated..."
        return f"PATCH_APPLIED files={len(staged)}\n{diff or '(content unchanged)'}"

    def search_text(args: dict) -> str:
        query = str(args["query"])
        glob = str(args.get("glob", "*"))
        out: list[str] = []
        for p in workspace.rglob(glob):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if any(part in IGNORED_DIRS for part in rel.parts):
                continue
            try:
                for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if query.lower() in line.lower():
                        out.append(f"{rel}:{n}: {line[:300]}")
                        if len(out) >= 200:
                            return "\n".join(out) + "\n...truncated..."
            except OSError:
                pass
        return "\n".join(out) or "NO_MATCHES"

    registry.register(ToolSpec("list_files", "List files and directories in the current workspace.", {
        "type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}
    }, "filesystem.read", list_files))
    registry.register(ToolSpec("read_file", "Read a text file from the current workspace with line numbers.", {
        "type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]
    }, "filesystem.read", read_file))
    registry.register(ToolSpec("write_file", "Create or replace a UTF-8 text file inside the workspace. Prefer apply_patch for edits to existing files.", {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]
    }, "filesystem.write", write_file))
    registry.register(ToolSpec("apply_patch", "Atomically apply validated exact-text patches to one or more workspace files. The whole patchset is validated before any file is modified.", {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "delete": {"type": "boolean"},
                        "replacements": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old": {"type": "string"},
                                    "new": {"type": "string"},
                                    "expected_count": {"type": "integer"}
                                },
                                "required": ["old", "new"]
                            }
                        }
                    },
                    "required": ["path"]
                }
            }
        },
        "required": ["changes"]
    }, "filesystem.write", apply_patch))
    registry.register(ToolSpec("search_text", "Search text across workspace files.", {
        "type": "object", "properties": {"query": {"type": "string"}, "glob": {"type": "string"}}, "required": ["query"]
    }, "filesystem.read", search_text))
