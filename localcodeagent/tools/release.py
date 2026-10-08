"""Release packaging — zip a workspace build output into a verified,
registered archive artifact.

`package_release` zips a workspace-bounded directory into
``.agent/releases/`` inside that workspace, then registers it in the
artifact store with sha256 + provenance so every release is
content-addressed and traceable to its task/mission. `release_verify`
re-hashes a registered archive — corruption detection, not vibes.
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from .base import ToolRegistry, ToolSpec

_SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv"}


def _zip_dir(src: Path, dest: Path) -> tuple[int, int]:
    """Zip src recursively into dest. Returns (file_count, raw_bytes)."""
    count = 0
    raw_bytes = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in __import__("os").walk(src):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                f = Path(dirpath) / fn
                try:
                    arc = f.relative_to(src).as_posix()
                    zf.write(f, arc)
                    count += 1
                    raw_bytes += f.stat().st_size
                except OSError:
                    continue
    return count, raw_bytes


def register_release_tools(registry: ToolRegistry, workspace: Path, *,
                           artifacts: Callable[[], Any],
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

    def _resolve(raw: str) -> Path | None:
        if not raw:
            return None
        cand = Path(raw)
        root = (workspace / cand).resolve() if not cand.is_absolute() \
            else cand.resolve()
        if not any(root == b or b in root.parents for b in _roots()):
            return None
        return root if root.is_dir() else None

    def package_release(args: dict[str, Any]) -> str:
        src = _resolve(str(args.get("path") or ""))
        if src is None:
            return json.dumps(
                {"error": "path must be a directory inside a registered workspace"})
        name = str(args.get("name") or src.name).strip() or src.name
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = src.parent / ".agent" / "releases"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{name}-{stamp}.zip"
        files, raw_bytes = _zip_dir(src, dest)
        if not files:
            dest.unlink(missing_ok=True)
            return json.dumps({"error": "nothing to package — directory "
                               "is empty or all files were skipped"})
        rec = artifacts().register(
            dest, kind="archive", tool="package_release",
            metadata={"source_dir": str(src), "files": files,
                      "raw_bytes": raw_bytes},
            provenance={"inputs": [str(src)]})
        return json.dumps({"ok": True, "archive": str(dest),
                           "files": files, "bytes": rec.get("size"),
                           "sha256": rec.get("sha256"),
                           "artifact_id": rec.get("id"),
                           "version": rec.get("version")},
                          ensure_ascii=False)

    def release_verify(args: dict[str, Any]) -> str:
        aid = str(args.get("artifact_id") or "").strip()
        if not aid:
            return json.dumps({"error": "artifact_id required"})
        return json.dumps(artifacts().verify(aid), ensure_ascii=False)

    registry.register(ToolSpec(
        "package_release",
        "Zip a workspace directory into .agent/releases/<name>-<timestamp>.zip and register it as a content-hashed archive artifact (sha256 + provenance). Use after a verified build to package a release.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "workspace directory to package"},
                "name": {"type": "string",
                         "description": "release name (default: dir name)"},
            },
            "required": ["path"],
        },
        "filesystem.write",
        package_release,
        category="coding",
        capabilities=["package_release", "build_project"],
    ))
    registry.register(ToolSpec(
        "release_verify",
        "Re-hash a registered release artifact and confirm it still matches its recorded sha256.",
        {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
            },
            "required": ["artifact_id"],
        },
        "filesystem.read",
        release_verify,
        category="coding",
        capabilities=["release_verify", "package_release"],
    ))

    def artifact_show(args: dict[str, Any]) -> str:
        """Resolve an artifact the user asks for by id, name fragment,
        or 'latest' — returns the client-facing card payload."""
        mgr = artifacts()
        name = str(args.get("name") or args.get("artifact_id")
                   or "").strip()
        task_id = str(args.get("task_id") or "").strip()
        hits = mgr.find(name, task_id=task_id)
        if not hits:
            return json.dumps({
                "ok": False,
                "error": "no matching artifact — nothing has been "
                         "produced yet, or it was cleaned up"})
        views = [mgr.client_view(r) for r in hits]
        views = [v for v in views if v]
        return json.dumps({"ok": True, "artifacts": views[:8]},
                          ensure_ascii=False)

    def artifact_list(args: dict[str, Any]) -> str:
        mgr = artifacts()
        rows = mgr.list(
            kind=str(args.get("kind") or ""),
            limit=max(1, min(50, int(args.get("limit") or 20))))
        views = [v for v in (mgr.client_view(r) for r in rows) if v]
        return json.dumps({"artifacts": views},
                          ensure_ascii=False)

    registry.register(ToolSpec(
        "artifact_show",
        "Hand the user a previously produced artifact — resolves by id, filename fragment ('installer', 'report'), or latest. Returns the download card payload.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "filename fragment, artifact id, or 'latest'"},
                "artifact_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
        },
        "filesystem.read",
        artifact_show,
        category="coding",
        capabilities=["artifact_handoff", "file_delivery"],
    ))
    registry.register(ToolSpec(
        "artifact_list",
        "List registered artifacts (downloadable outputs) with verification status.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
        "filesystem.read",
        artifact_list,
        category="coding",
        capabilities=["artifact_handoff", "file_delivery"],
    ))
