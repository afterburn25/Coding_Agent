from __future__ import annotations

import json

from .base import ToolRegistry, ToolSpec


def register_queue_tools(registry: ToolRegistry, queue) -> None:
    """Model-callable work-queue tools: the agent can schedule follow-up work.

    queue_task only *enqueues* — each dequeued task's own tool calls are still
    gated by their individual permissions.
    """

    def queue_task(args: dict) -> str:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return "ERROR: prompt is required"
        try:
            # Tools run on a pooled thread, so the mission lane's thread-local
            # marker can't reach here — the orchestrator stamps the owning
            # mission into task_tls instead.
            tls = registry.context.get("task_tls")
            mid = str(getattr(tls, "mission_id", "") or "") if tls else ""
            item = queue.enqueue(prompt, mode=str(args.get("mode") or "auto"),
                                 mission_id=mid)
        except ValueError as exc:
            return f"ERROR: {exc}"
        return json.dumps({"queued": item["id"], "position": len(queue), "prompt": item["prompt"]})

    def queue_list(args: dict) -> str:
        items = queue.list()
        return json.dumps({"size": len(items), "items": [
            {"id": i["id"], "prompt": i["prompt"][:200], "mode": i["mode"]} for i in items
        ]})

    def queue_cancel(args: dict) -> str:
        return json.dumps({"removed": queue.remove(str(args.get("id", "")).strip())})

    registry.register(ToolSpec(
        "queue_task",
        "Enqueue a follow-up task to run after the current work finishes. Use for related work the user asked to do next, or self-identified follow-ups that should run unattended.",
        {"type": "object", "properties": {"prompt": {"type": "string"}, "mode": {"type": "string"}}, "required": ["prompt"]},
        "tasks.queue",
        queue_task,
    ))
    registry.register(ToolSpec(
        "queue_list",
        "List prompts waiting in the durable work queue.",
        {"type": "object", "properties": {}},
        "filesystem.read",
        queue_list,
    ))
    registry.register(ToolSpec(
        "queue_cancel",
        "Remove a queued prompt by id (from queue_list).",
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        "tasks.queue",
        queue_cancel,
    ))
