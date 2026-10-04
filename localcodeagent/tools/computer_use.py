"""Computer Use tool registration — desktop control through permissions.

Observation and input injection use separate, granular permission keys
(``desktop.view``, ``screen.capture``, ``mouse.control``,
``keyboard.control``, ``application.launch``, clipboard read/write). All are
approval-tier by default, and the older broad ``computer.observe`` /
``computer.control`` settings remain compatible aliases. Every action is
recorded on the ComputerUse audit list and echoed into the activity timeline
with a terminal completed/failed state.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec
from ..computer_use import ComputerUse


def register_computer_use_tools(registry: ToolRegistry, workspace: Path,
                                *, activity: Any = None) -> ComputerUse:
    def _audit(action: str, detail: str) -> str | None:
        if activity is None:
            return None
        try:
            row = activity.open("computer-use", "tool",
                                f"computer use: {action}", summary=detail,
                                details={"action": action})
            return str(row.get("id") or "") or None
        except Exception:
            return None

    def _audit_done(activity_id: str | None, action: str,
                    result: dict[str, Any]) -> None:
        if activity is None or not activity_id:
            return
        try:
            state = "completed" if result.get("ok") else "failed"
            summary = str(result.get("error") or
                          (f"{action} completed" if result.get("ok") else f"{action} failed"))
            activity.update("computer-use", str(activity_id), state=state,
                            summary=summary[:300],
                            details={"result": dict(result)})
        except Exception:
            pass

    cu = ComputerUse(audit=_audit, audit_done=_audit_done)
    shot_dir = workspace / "data" / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    def _cap(fn):
        def inner(args: dict[str, Any]) -> str:
            return json.dumps(fn(args), ensure_ascii=False)
        return inner

    def _shot_path() -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return shot_dir / f"shot-{stamp}-{uuid.uuid4().hex[:10]}.png"

    registry.register(ToolSpec(
        "computer_screenshot",
        "Capture the virtual screen to a PNG under data/screenshots and return its path/size. Use to understand the current desktop state.",
        {"type": "object", "properties": {}},
        "screen.capture",
        _cap(lambda a: cu.screenshot(_shot_path())),
        category="utilities", capabilities=["screenshot", "observe_screen"]))

    registry.register(ToolSpec(
        "computer_windows",
        "List visible desktop windows with titles, handles, and positions.",
        {"type": "object", "properties": {}},
        "desktop.view",
        _cap(lambda a: cu.list_windows()),
        category="utilities", capabilities=["window_detection"]))

    registry.register(ToolSpec(
        "computer_active_window",
        "Identify the foreground desktop window and owning process id.",
        {"type": "object", "properties": {}},
        "desktop.view",
        _cap(lambda a: cu.active_window()),
        category="utilities", capabilities=["window_detection", "active_window"]))

    registry.register(ToolSpec(
        "computer_focus",
        "Focus a visible window by title substring or hwnd. The target handle is revalidated before focus.",
        {"type": "object", "properties": {
            "title": {"type": "string"}, "hwnd": {"type": "integer"}}},
        "desktop.control",
        _cap(lambda a: cu.focus_window(str(a.get("title") or ""),
                                       int(a.get("hwnd") or 0))),
        category="utilities", capabilities=["application_focus"]))

    registry.register(ToolSpec(
        "computer_launch",
        "Launch a local executable directly (no shell) with optional bounded arguments.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"},
                     "default": []}},
         "required": ["path"]},
        "application.launch",
        _cap(lambda a: cu.launch_app(str(a.get("path") or ""),
                                     [str(x) for x in a.get("args") or []])),
        category="utilities", capabilities=["application_launch"]))

    registry.register(ToolSpec(
        "computer_click",
        "Move the mouse to x,y and click (left/right). Coordinates are absolute screen pixels and are checked against the virtual desktop.",
        {"type": "object", "properties": {
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right"],
                       "default": "left"}},
         "required": ["x", "y"]},
        "mouse.control",
        _cap(lambda a: cu.click(int(a["x"]), int(a["y"]),
                                button=str(a.get("button", "left")))),
        category="utilities", capabilities=["mouse_control", "clicking"]))

    registry.register(ToolSpec(
        "computer_type",
        "Type text via simulated keystrokes (Unicode-safe). Typed content is never written to the desktop audit log.",
        {"type": "object", "properties": {"text": {"type": "string"}},
         "required": ["text"]},
        "keyboard.control",
        _cap(lambda a: cu.type_text(str(a.get("text") or ""))),
        category="utilities", capabilities=["typing", "keyboard"]))

    registry.register(ToolSpec(
        "computer_keys",
        "Press a key combination, e.g. keys=['ctrl','s'] or ['enter'].",
        {"type": "object", "properties": {
            "keys": {"type": "array", "items": {"type": "string"}}},
         "required": ["keys"]},
        "keyboard.control",
        _cap(lambda a: cu.key_combo([str(k) for k in a.get("keys", [])])),
        category="utilities", capabilities=["keyboard_shortcuts"]))

    registry.register(ToolSpec(
        "computer_scroll",
        "Scroll the mouse wheel at x,y by N detents (positive=up).",
        {"type": "object", "properties": {
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "clicks": {"type": "integer", "default": 3}},
         "required": ["x", "y"]},
        "mouse.control",
        _cap(lambda a: cu.scroll(int(a["x"]), int(a["y"]),
                                 int(a.get("clicks", 3)))),
        category="utilities", capabilities=["scrolling"]))

    registry.register(ToolSpec(
        "computer_clipboard_get",
        "Read the Windows clipboard text. Clipboard contents are returned to the approved caller but never stored in the audit timeline.",
        {"type": "object", "properties": {}},
        "clipboard.read",
        _cap(lambda a: cu.clipboard_get()),
        category="utilities", capabilities=["clipboard"]))

    registry.register(ToolSpec(
        "computer_clipboard_set",
        "Write text to the Windows clipboard. Audit records only the character count.",
        {"type": "object", "properties": {"text": {"type": "string"}},
         "required": ["text"]},
        "clipboard.write",
        _cap(lambda a: cu.clipboard_set(str(a.get("text") or ""))),
        category="utilities", capabilities=["clipboard"]))

    return cu
