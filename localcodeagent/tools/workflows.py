"""Declarative multi-step tool workflows.

Workflow JSON files under ``workflows/`` describe step sequences that chain
through the Tool Registry — each step is a normal `registry.execute` call, so
permissions, install gates, and usage tracking apply per step.

Format::

    {
      "id": "video_to_subtitled_clip",
      "name": "Transcribe + subtitle a clip",
      "description": "...",
      "params": {"source": "video.mp4", "language": "auto"},
      "steps": [
        {"tool": "media_transcribe", "args": {"source": "{params.source}", "language": "{params.language}"}, "save_as": "transcript"},
        {"tool": "add_subtitles", "args": {"source": "{params.source}", "subtitles": "{steps.transcript.subtitles}", "output": "out.mp4"}}
      ]
    }

Templating: ``{params.<name>}`` reads caller params; ``{steps.<save_as>}``
reads a previous step's raw output, ``{steps.<save_as>.<field>}`` reads a
field from a JSON step output. Steps stop on the first error.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
MAX_OUTPUT_CHARS = 20000


def load_workflows(directory: Path) -> dict[str, dict[str, Any]]:
    workflows: dict[str, dict[str, Any]] = {}
    if not directory.is_dir():
        return workflows
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or not raw.get("id") or not isinstance(raw.get("steps"), list):
            continue
        raw["_path"] = str(path)
        workflows[str(raw["id"])] = raw
    return workflows


def _lookup(context: dict[str, Any], dotted: str) -> Any:
    node: Any = context
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise KeyError(dotted)
    return node


def _render(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole:
            try:
                return _lookup(context, whole.group(1))
            except KeyError:
                return value
        def repl(match: re.Match) -> str:
            try:
                return str(_lookup(context, match.group(1)))
            except KeyError:
                return match.group(0)
        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, list):
        return [_render(v, context) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, context) for k, v in value.items()}
    return value


def _resume_file(resume_dir: Path | None, workflow_id: str) -> Path | None:
    if resume_dir is None or not workflow_id:
        return None
    return Path(resume_dir) / f"{workflow_id}.resume.json"


def _save_resume(path: Path | None, *, workflow_id: str, next_step: int,
                 params: dict[str, Any], steps: dict[str, Any], step_log: list[dict[str, Any]]) -> str:
    """Persist enough context to re-enter the workflow at `next_step`."""
    if path is None:
        return ""
    try:
        capped = {
            name: {k: (v[:MAX_OUTPUT_CHARS] if k == "_raw" and isinstance(v, str) else v)
                   for k, v in value.items()}
            if isinstance(value, dict) else value
            for name, value in steps.items()
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "workflow_id": workflow_id,
            "next_step": next_step,
            "params": params,
            "steps": capped,
            "step_log": step_log,
            "saved_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return str(path)
    except OSError:
        return ""


def _load_resume(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("next_step"), int) else None


def run_workflow(registry: ToolRegistry, workflow: dict[str, Any], params: dict[str, Any], *,
                 jobs=None, approved: bool = False,
                 resume_dir: Path | None = None, resume: bool = False) -> dict[str, Any]:
    """Execute a workflow's steps through the registry, threading outputs.

    On failure or cancellation a resume checkpoint is written under
    ``resume_dir``; calling again with ``resume=True`` re-enters at the
    failed step with prior step outputs restored as template context.
    """
    workflow_id = str(workflow.get("id") or "workflow")
    resume_path = _resume_file(resume_dir, workflow_id)
    merged = dict(workflow.get("params") or {})
    context: dict[str, Any] = {"params": merged, "steps": {}}
    step_log: list[dict[str, Any]] = []
    start_index = 0
    if resume:
        prior = _load_resume(resume_path)
        if prior:
            merged.update(prior.get("params") or {})
            context["steps"] = prior.get("steps") or {}
            step_log = list(prior.get("step_log") or [])
            start_index = prior.get("next_step", 0)
    merged.update(params or {})
    job = jobs.submit("workflow", f"{workflow.get('name') or workflow['id']}") if jobs is not None else None
    if job:
        jobs.update(job.id, cancellable=True)
    started = time.time()

    def _halt(i: int, **extra: Any) -> dict[str, Any]:
        resume_file = _save_resume(
            resume_path, workflow_id=workflow_id, next_step=i,
            params=merged, steps=context["steps"], step_log=step_log)
        out = {"ok": False, "workflow": workflow_id, "steps": step_log, **extra}
        if resume_file:
            out["resume_file"] = resume_file
        return out

    for i, step in enumerate(workflow["steps"]):
        if i < start_index:
            continue
        if job and jobs.get(job.id).state == "cancelled":
            step_log.append({"index": i, "tool": str(step.get("tool", "")), "ok": False,
                             "output": "cancelled before step start"})
            return _halt(i, cancelled=True, cancelled_at_step=i)
        tool_name = str(step.get("tool", ""))
        if not tool_name:
            return _halt(i, failed_step=i, error=f"step {i} has no tool")
        try:
            args = _render(dict(step.get("args") or {}), context)
        except Exception as exc:  # noqa: BLE001
            return _halt(i, failed_step=i, error=f"template error: {exc}")
        if job:
            jobs.update(job.id, state="running", detail=f"step {i + 1}/{len(workflow['steps'])}: {tool_name}")
        result = registry.execute(tool_name, args if isinstance(args, dict) else {"value": args}, approved=approved)
        failed = result.startswith(("ERROR", "PERMISSION", "TOOL_", "APPROVAL"))
        step_log.append({"index": i, "tool": tool_name, "ok": not failed, "output": result[:2000]})
        if failed:
            if job:
                jobs.update(job.id, state="failed", error=f"step {i} ({tool_name}): {result[:200]}")
            return _halt(i, failed_step=i, failed_tool=tool_name, error=result[:1000])
        save_as = str(step.get("save_as") or f"step{i}")
        parsed: Any = result
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            pass
        if isinstance(parsed, dict):
            context["steps"][save_as] = {**parsed, "_raw": result}
        else:
            context["steps"][save_as] = {"_raw": result}

    if resume_path is not None:
        resume_path.unlink(missing_ok=True)
    elapsed = round(time.time() - started, 3)
    if job:
        jobs.update(job.id, state="completed", detail=f"{len(step_log)} steps in {elapsed}s")
    return {"ok": True, "workflow": workflow_id, "elapsed_seconds": elapsed,
            "resumed_from_step": start_index if start_index else None,
            "steps": step_log,
            "outputs": {k: v.get("_raw", "")[:MAX_OUTPUT_CHARS] for k, v in context["steps"].items()}}


def register_workflow_tools(registry: ToolRegistry, workspace: Path, *,
                            workflows_dir: Path | None = None, jobs=None) -> None:
    directory = Path(workflows_dir) if workflows_dir else workspace / "workflows"
    resume_dir = workspace / ".agent" / "workflow_runs"

    def list_workflows(args: dict[str, Any]) -> str:
        workflows = load_workflows(directory)
        rows = []
        for w in workflows.values():
            prior = _load_resume(_resume_file(resume_dir, str(w["id"])))
            rows.append({"id": w["id"], "name": w.get("name", w["id"]),
                         "description": (w.get("description") or "")[:200],
                         "steps": len(w.get("steps") or []),
                         "params": list((w.get("params") or {}).keys()),
                         "resumable": prior is not None,
                         "resume_step": prior.get("next_step") if prior else None,
                         "file": w.get("_path", "")})
        return json.dumps({"workflows": rows, "directory": str(directory)}, ensure_ascii=False)

    def run_workflow_tool(args: dict[str, Any]) -> str:
        workflow_id = str(args.get("workflow", "")).strip()
        workflows = load_workflows(directory)
        workflow = workflows.get(workflow_id)
        if workflow is None:
            known = ", ".join(sorted(workflows)) or "(none)"
            return f"ERROR: unknown workflow '{workflow_id}' — available: {known} ({directory})"
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        result = run_workflow(
            registry, workflow, params, jobs=jobs,
            approved=bool(args.get("approved", False)),
            resume_dir=resume_dir,
            resume=bool(args.get("resume", False)),
        )
        return json.dumps(result, ensure_ascii=False)

    registry.register(ToolSpec(
        "list_workflows",
        "List declarative multi-step tool workflows defined under workflows/*.json.",
        {"type": "object", "properties": {}},
        "filesystem.read", list_workflows,
        category="utilities",
        capabilities=["list_workflows", "automation"],
    ))
    registry.register(ToolSpec(
        "run_workflow",
        "Run a declarative multi-step workflow (workflows/*.json) — chains registered tools with {params.*}/{steps.*} templating; every step passes the permission gate and tracks as a Job. Failed/cancelled runs write a resume checkpoint; pass resume=true to re-enter at the failed step.",
        {
            "type": "object",
            "properties": {
                "workflow": {"type": "string", "description": "workflow id"},
                "params": {"type": "object", "description": "workflow parameters"},
                "resume": {"type": "boolean", "description": "resume from the last failed/cancelled checkpoint"},
            },
            "required": ["workflow"],
        },
        "shell.execute", run_workflow_tool,
        category="utilities",
        capabilities=["run_workflow", "multi_step_automation", "orchestrate_tools"],
    ))
