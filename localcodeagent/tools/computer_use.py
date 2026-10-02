"""Computer Use tool registration — desktop control through permissions.

Observation tools (screenshot, window list) sit behind
``computer.observe``; anything that injects input, moves focus, or writes
the clipboard sits behind ``computer.control`` — approval-tier by
default in every permission profile. Every action is recorded on the
ComputerUse audit list and echoed into the activity timeline.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec
from ..computer_use import ComputerUse


def register_computer_use_tools(registry: ToolRegistry, workspace: Path,
                                *, activity: Any = None) -> ComputerUse:
    def _audit(action: str, detail: str) -> None:
        if activity is None:
            return
        try:
            activity.open("computer-use", "tool",
                          f"computer use: {action}", summary=detail)
        except Exception:
            pass
    cu = ComputerUse(audit=_audit)
    shot_dir = workspace / "data" / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    def _cap(fn):
        def inner(args: dict[str, Any]) -> str:
            return json.dumps(fn(args), ensure_ascii=False)
        return inner

    registry.register(ToolSpec(
        "computer_screenshot",
        "Capture the primary screen to a PNG under data/screenshots and return its path/size. Use to understand the current desktop state.",
        {"type": "object", "properties": {}},
        "computer.observe",
        _cap(lambda a: cu.screenshot(shot_dir / f"shot-{int(__import__('time').time())}.png")),
        category="utilities", capabilities=["screenshot", "observe_screen"]))

    registry.register(ToolSpec(
        "computer_windows",
        "List visible desktop windows with titles, handles, and positions.",
        {"type": "object", "properties": {}},
        "computer.observe",
        _cap(lambda a: cu.list_windows()),
        category="utilities", capabilities=["window_detection"]))

    registry.register(ToolSpec(
        "computer_focus",
        "Focus a window by title substring or hwnd.",
        {"type": "object", "properties": {
            "title": {"type": "string"}, "hwnd": {"type": "integer"}}},
        "computer.control",
        _cap(lambda a: cu.focus_window(str(a.get("title") or ""),
                                       int(a.get("hwnd") or 0))),
        category="utilities", capabilities=["application_focus"]))

    registry.register(ToolSpec(
        "computer_click",
        "Move the mouse to x,y and click (left/right). Coordinates are absolute screen pixels.",
        {"type": "object", "properties": {
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right"],
                       "default": "left"}},
         "required": ["x", "y"]},
        "computer.control",
        _cap(lambda a: cu.click(int(a["x"]), int(a["y"]),
                                button=str(a.get("button", "left")))),
        category="utilities", capabilities=["mouse_control", "clicking"]))

    registry.register(ToolSpec(
        "computer_type",
        "Type text via simulated keystrokes (Unicode-safe).",
        {"type": "object", "properties": {"text": {"type": "string"}},
         "required": ["text"]},
        "computer.control",
        _cap(lambda a: cu.type_text(str(a.get("text") or ""))),
        category="utilities", capabilities=["typing", "keyboard"]))

    registry.register(ToolSpec(
        "computer_keys",
        "Press a key combination, e.g. keys=['ctrl','s'] or ['enter'].",
        {"type": "object", "properties": {
            "keys": {"type": "array", "items": {"type": "string"}}},
         "required": ["keys"]},
        "computer.control",
        _cap(lambda a: cu.key_combo([str(k) for k in a.get("keys", [])])),
        category="utilities", capabilities=["keyboard_shortcuts"]))

    registry.register(ToolSpec(
        "computer_scroll",
        "Scroll the mouse wheel at x,y by N detents (positive=up).",
        {"type": "object", "properties": {
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "clicks": {"type": "integer", "default": 3}},
         "required": ["x", "y"]},
        "computer.control",
        _cap(lambda a: cu.scroll(int(a["x"]), int(a["y"]),
                                 int(a.get("clicks", 3)))),
        category="utilities", capabilities=["scrolling"]))

    registry.register(ToolSpec(
        "computer_clipboard_get",
        "Read the Windows clipboard text.",
        {"type": "object", "properties": {}},
        "computer.observe",
        _cap(lambda a: cu.clipboard_get()),
        category="utilities", capabilities=["clipboard"]))

    registry.register(ToolSpec(
        "computer_clipboard_set",
        "Write text to the Windows clipboard.",
        {"type": "object", "properties": {"text": {"type": "string"}},
         "required": ["text"]},
        "computer.control",
        _cap(lambda a: cu.clipboard_set(str(a.get("text") or ""))),
        category="utilities", capabilities=["clipboard"]))

    return cu
