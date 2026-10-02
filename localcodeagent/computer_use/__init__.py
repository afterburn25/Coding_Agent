"""Computer Use — desktop observation and input control (Part A).

Windows implementation via ctypes user32/kernel32. Every operation is a
discrete, auditable action: callers must check permissions
(``computer.observe`` for screenshots/window listing, ``computer.control``
for input injection/focus/clipboard) before invoking — the ``ComputerUse``
class itself records each action to an audit list for the activity
timeline.

On non-Windows platforms the primitives report ``ok: False`` cleanly —
nothing pretends to work.
"""
from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

user32 = ctypes.windll.user32 if os.name == "nt" else None

# input flags
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "enter": 0x0D, "return": 0x0D, "esc": 0x1B,
    "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pgup": 0x21,
    "pgdn": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}


def _send_inputs(inputs: list[Any]) -> bool:
    if user32 is None:
        return False
    array_type = type("INPUT_ARRAY", (), {})  # placeholder
    return _send_input_list(inputs)


def _send_input_list(inputs: list) -> bool:
    """Low-level SendInput helper (INPUT struct, 64-bit layout)."""
    if user32 is None:
        return False

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    arr = (INPUT * len(inputs))()
    for i, spec in enumerate(inputs):
        if spec[0] == "key":
            _, vk, scan, flags = spec
            arr[i].type = 1
            arr[i].u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        else:
            _, dx, dy, data, flags = spec
            arr[i].type = 0
            arr[i].u.mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    return sent == len(inputs)


class ComputerUse:
    """Audited desktop control surface. Permission checks happen in the
    caller/tool layer — this records every action taken."""

    def __init__(self, audit=None) -> None:
        self._audit = audit  # callable(action, detail)
        self.actions: list[dict[str, Any]] = []

    def _record(self, action: str, detail: str = "") -> None:
        row = {"ts": time.time(), "action": action, "detail": detail[:200]}
        self.actions.append(row)
        self.actions = self.actions[-500:]
        if self._audit:
            try:
                self._audit(action, detail)
            except Exception:
                pass

    # -- observation ----------------------------------------------------

    def screenshot(self, out_path: Path | None = None) -> dict[str, Any]:
        """Capture the primary screen → PNG. PIL ImageGrab when present,
        else a PowerShell System.Drawing fallback on Windows."""
        self._record("screenshot")
        if out_path is None:
            out_path = Path(tempfile.mkstemp(prefix="nexus-shot-",
                                             suffix=".png")[1])
        try:
            from PIL import ImageGrab  # type: ignore
            img = ImageGrab.grab()
            img.save(str(out_path), "PNG")
            return {"ok": True, "path": str(out_path),
                    "size": out_path.stat().st_size,
                    "width": img.width, "height": img.height}
        except ImportError:
            pass
        except Exception as exc:
            return {"ok": False, "error": f"screenshot: {exc}"}
        if os.name == "nt":
            ps = (
                "Add-Type -AssemblyName System.Drawing,System.Windows.Forms;"
                "$b=New-Object System.Drawing.Bitmap("
                "[System.Windows.Forms.SystemInformation]::VirtualScreen.Width,"
                "[System.Windows.Forms.SystemInformation]::VirtualScreen.Height);"
                "$g=[System.Drawing.Graphics]::FromImage($b);"
                "$g.CopyFromScreen([System.Windows.Forms.SystemInformation]::VirtualScreen.X,"
                "[System.Windows.Forms.SystemInformation]::VirtualScreen.Y,0,0,$b.Size);"
                f"$b.Save('{str(out_path).replace(chr(39), '')}');")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=30)
            if out_path.is_file() and out_path.stat().st_size > 0:
                return {"ok": True, "path": str(out_path),
                        "size": out_path.stat().st_size, "backend": "powershell"}
            return {"ok": False,
                    "error": (r.stderr or "powershell capture failed")[:300]}
        return {"ok": False, "error": "screenshot unsupported on this platform"}

    def list_windows(self) -> dict[str, Any]:
        self._record("list_windows")
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        out = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            out.append({"hwnd": int(hwnd), "title": buf.value[:200],
                        "x": rect.left, "y": rect.top,
                        "w": rect.right - rect.left,
                        "h": rect.bottom - rect.top})
            return True

        user32.EnumWindows(enum_cb, 0)
        return {"ok": True, "windows": out[:200], "count": len(out)}

    def focus_window(self, title_substr: str = "", hwnd: int = 0) -> dict:
        self._record("focus_window", title_substr or str(hwnd))
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        target = hwnd
        if not target and title_substr:
            for w in self.list_windows().get("windows", []):
                if title_substr.lower() in w["title"].lower():
                    target = w["hwnd"]
                    break
        if not target:
            return {"ok": False, "error": "no matching window"}
        SW_RESTORE = 9
        user32.ShowWindow(target, SW_RESTORE)
        user32.SetForegroundWindow(target)
        return {"ok": True, "hwnd": int(target)}

    # -- input ------------------------------------------------------------

    def mouse_move(self, x: int, y: int) -> dict:
        self._record("mouse_move", f"{x},{y}")
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        user32.SetCursorPos(int(x), int(y))
        return {"ok": True}

    def click(self, x: int, y: int, *, button: str = "left") -> dict:
        self._record("click", f"{button}@{x},{y}")
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        user32.SetCursorPos(int(x), int(y))
        down = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
        up = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
        ok = _send_input_list([("mouse", 0, 0, 0, down),
                               ("mouse", 0, 0, 0, up)])
        return {"ok": ok}

    def scroll(self, x: int, y: int, clicks: int = 3) -> dict:
        self._record("scroll", f"{clicks}@{x},{y}")
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        user32.SetCursorPos(int(x), int(y))
        delta = int(clicks) * 120
        ok = _send_input_list([("mouse", 0, 0, delta, MOUSEEVENTF_WHEEL)])
        return {"ok": ok}

    def type_text(self, text: str) -> dict:
        self._record("type_text", text[:80])
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        inputs = []
        for ch in str(text)[:4000]:
            code = ord(ch)
            inputs.append(("key", 0, code, KEYEVENTF_UNICODE))
            inputs.append(("key", 0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        ok = _send_input_list(inputs) if inputs else True
        return {"ok": ok, "chars": len(inputs) // 2}

    def key_combo(self, keys: list[str]) -> dict:
        """e.g. ["ctrl","c"], ["alt","f4"], ["enter"]."""
        self._record("key_combo", "+".join(keys))
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        codes = []
        for k in keys:
            k = str(k).lower()
            if k in VK:
                codes.append(VK[k])
            elif len(k) == 1:
                codes.append(ord(k.upper()))
            else:
                return {"ok": False, "error": f"unknown key '{k}'"}
        inputs = [("key", c, 0, 0) for c in codes]
        inputs += [("key", c, 0, KEYEVENTF_KEYUP) for c in reversed(codes)]
        return {"ok": _send_input_list(inputs)}

    # -- clipboard ---------------------------------------------------------

    def clipboard_get(self) -> dict:
        self._record("clipboard_get")
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        CF_UNICODETEXT = 13
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        if not user32.OpenClipboard(None):
            return {"ok": False, "error": "clipboard busy"}
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return {"ok": True, "text": ""}
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return {"ok": False, "error": "clipboard lock failed"}
            try:
                text = ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(h)
            return {"ok": True, "text": text}
        finally:
            user32.CloseClipboard()

    def clipboard_set(self, text: str) -> dict:
        self._record("clipboard_set", f"{len(text)} chars")
        if user32 is None:
            return {"ok": False, "error": "unsupported platform"}
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        data = str(text).encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return {"ok": False, "error": "GlobalAlloc failed"}
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            kernel32.GlobalFree(h)
            return {"ok": False, "error": "GlobalLock failed"}
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(h)
            return {"ok": False, "error": "clipboard busy"}
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                kernel32.GlobalFree(h)
                return {"ok": False, "error": "SetClipboardData failed"}
            h = None
            return {"ok": True}
        finally:
            user32.CloseClipboard()
            if h:
                kernel32.GlobalFree(h)
