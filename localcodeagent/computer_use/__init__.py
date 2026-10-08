"""Computer Use — desktop observation and input control.

Windows implementation via ctypes user32/kernel32. Every operation is a
discrete, auditable action: callers must check permissions before invoking —
the ``ComputerUse`` class itself records each action and its terminal result
for the activity timeline.

On non-Windows platforms the primitives report ``ok: False`` cleanly —
nothing pretends to work.
"""
from __future__ import annotations

import ctypes
import os
import re
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

# Fields that are safe to put in durable action history. Clipboard text and
# typed payloads are deliberately absent.
_RESULT_FIELDS = {
    "backend", "button", "chars", "clicks", "count", "error", "height",
    "hwnd", "path", "pid", "size", "title", "truncated", "width", "x", "y",
    "w", "h",
}


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

    def __init__(self, audit=None, audit_done=None) -> None:
        # audit(action, detail) -> optional activity id; called before action.
        self._audit = audit
        # audit_done(activity_id, action, result_summary); called once the
        # primitive returns so timeline rows never stay permanently running.
        self._audit_done = audit_done
        self.actions: list[dict[str, Any]] = []
        from .uia import LoopGuard
        self._loop_guard = LoopGuard()

    def _record(self, action: str, detail: str = "") -> dict[str, Any]:
        row = {"ts": time.time(), "action": action,
               "detail": str(detail)[:200], "ok": None, "result": {}}
        self.actions.append(row)
        self.actions = self.actions[-500:]
        if self._audit:
            try:
                activity_id = self._audit(action, row["detail"])
                if isinstance(activity_id, dict):
                    activity_id = activity_id.get("id")
                if activity_id is not None:
                    row["activity_id"] = str(activity_id)
            except Exception:
                pass
        return row

    def _finish(self, record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        result = dict(result or {})
        summary = {"ok": bool(result.get("ok"))}
        for key in _RESULT_FIELDS:
            if key in result and result[key] is not None:
                value = result[key]
                summary[key] = str(value)[:300] if isinstance(value, str) else value
        if result.get("error"):
            summary["error"] = str(result["error"])[:300]
        record["ok"] = summary["ok"]
        record["result"] = summary
        if self._audit_done:
            try:
                self._audit_done(record.get("activity_id"), record["action"], dict(summary))
            except Exception:
                pass
        return result

    def _fail(self, record: dict[str, Any], error: str) -> dict[str, Any]:
        return self._finish(record, {"ok": False, "error": str(error)[:300]})

    @staticmethod
    def _virtual_bounds() -> tuple[int, int, int, int] | None:
        if user32 is None:
            return None
        try:
            left = int(user32.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
            top = int(user32.GetSystemMetrics(77))    # SM_YVIRTUALSCREEN
            width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            height = int(user32.GetSystemMetrics(79)) # SM_CYVIRTUALSCREEN
        except Exception:
            return None
        if width <= 0 or height <= 0:
            return None
        return left, top, left + width - 1, top + height - 1

    def _point_error(self, x: Any, y: Any) -> str | None:
        try:
            px, py = int(x), int(y)
        except (TypeError, ValueError):
            return "coordinates must be integers"
        bounds = self._virtual_bounds()
        if bounds and not (bounds[0] <= px <= bounds[2] and bounds[1] <= py <= bounds[3]):
            return f"coordinates outside virtual screen {bounds}"
        return None

    @staticmethod
    def _window_ok(hwnd: Any) -> bool:
        try:
            handle = int(hwnd)
        except (TypeError, ValueError):
            return False
        return bool(handle and user32 and user32.IsWindow(handle))

    @staticmethod
    def _window_row(hwnd: int) -> dict[str, Any]:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value[:200]
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {"hwnd": int(hwnd), "title": title, "pid": int(pid.value),
                "x": rect.left, "y": rect.top,
                "w": rect.right - rect.left,
                "h": rect.bottom - rect.top}

    @staticmethod
    def _visible_windows() -> list[dict[str, Any]]:
        out = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            out.append(ComputerUse._window_row(hwnd))
            return True

        user32.EnumWindows(enum_cb, 0)
        return out

    # -- observation ----------------------------------------------------

    def screenshot(self, out_path: Path | None = None) -> dict[str, Any]:
        """Capture the virtual screen → PNG. PIL ImageGrab when present,
        else a PowerShell System.Drawing fallback on Windows."""
        record = self._record("screenshot")
        try:
            if out_path is None:
                fd, temp_name = tempfile.mkstemp(prefix="nexus-shot-", suffix=".png")
                os.close(fd)
                out_path = Path(temp_name)
        except Exception as exc:
            return self._fail(record, f"screenshot path: {exc}")
        try:
            from PIL import ImageGrab  # type: ignore
            img = ImageGrab.grab()
            img.save(str(out_path), "PNG")
            return self._finish(record, {"ok": True, "path": str(out_path),
                                         "size": out_path.stat().st_size,
                                         "width": img.width, "height": img.height})
        except ImportError:
            pass
        except Exception as exc:
            return self._fail(record, f"screenshot: {exc}")
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
            try:
                from ..procutil import no_window_flags
                r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                   capture_output=True, text=True, timeout=30,
                                   creationflags=no_window_flags())
            except Exception as exc:
                return self._fail(record, f"powershell capture: {exc}")
            if out_path.is_file() and out_path.stat().st_size > 0:
                return self._finish(record, {"ok": True, "path": str(out_path),
                                             "size": out_path.stat().st_size,
                                             "backend": "powershell"})
            return self._fail(record, (r.stderr or "powershell capture failed")[:300])
        return self._fail(record, "screenshot unsupported on this platform")

    def list_windows(self) -> dict[str, Any]:
        record = self._record("list_windows")
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            windows = self._visible_windows()
            return self._finish(record, {"ok": True, "windows": windows[:200],
                                         "count": len(windows)})
        except Exception as exc:
            return self._fail(record, f"window enumeration: {exc}")

    def active_window(self) -> dict[str, Any]:
        """Return the foreground window title, handle, position, and pid."""
        record = self._record("active_window")
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            hwnd = int(user32.GetForegroundWindow() or 0)
            if not hwnd or not self._window_ok(hwnd):
                return self._fail(record, "no active window")
            row = self._window_row(hwnd)
            row["ok"] = True
            return self._finish(record, row)
        except Exception as exc:
            return self._fail(record, f"active window: {exc}")

    # -- window/application control ---------------------------------------

    def focus_window(self, title_substr: str = "", hwnd: int = 0) -> dict:
        title_substr = str(title_substr or "")
        record = self._record("focus_window", title_substr[:80] or str(hwnd))
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            target = 0
            if hwnd:
                try:
                    target = int(hwnd)
                except (TypeError, ValueError):
                    return self._fail(record, "hwnd must be an integer")
                if not self._window_ok(target):
                    return self._fail(record, "target window no longer exists")
            elif title_substr:
                needle = str(title_substr)[:200].lower()
                for window in self._visible_windows():
                    if needle in window["title"].lower():
                        target = int(window["hwnd"])
                        break
            if not target:
                return self._fail(record, "no matching window")
            SW_RESTORE = 9
            user32.ShowWindow(target, SW_RESTORE)
            user32.SetForegroundWindow(target)
            return self._finish(record, {"ok": True, "hwnd": int(target)})
        except Exception as exc:
            return self._fail(record, f"focus window: {exc}")

    def launch_app(self, path: str, args: list[str] | None = None) -> dict[str, Any]:
        """Launch an executable directly (no shell) and return its pid."""
        path = str(path or "").strip()
        if args is not None and not isinstance(args, list):
            arg_list = None
        else:
            arg_list = list(args or [])
        detail = f"{path[:120]} ({len(arg_list)} args)" if arg_list is not None else path[:120]
        record = self._record("launch_app", detail)
        if arg_list is None:
            return self._fail(record, "launch arguments must be a list")
        if os.name != "nt":
            return self._fail(record, "unsupported platform")
        if not path:
            return self._fail(record, "executable path is required")
        if len(path) > 500:
            return self._fail(record, "executable path is too long")
        if len(arg_list) > 20:
            return self._fail(record, "too many launch arguments")
        clean_args = []
        for arg in arg_list:
            arg = str(arg)
            if len(arg) > 300:
                return self._fail(record, "launch argument is too long")
            clean_args.append(arg)
        try:
            proc = subprocess.Popen([path, *clean_args], close_fds=True)
        except Exception as exc:
            return self._fail(record, f"launch failed: {exc}")
        return self._finish(record, {"ok": True, "pid": int(proc.pid), "path": path})

    # -- verified application lifecycle (apps.py primitives) ---------------

    def app_resolve(self, name: str) -> dict[str, Any]:
        """Resolve a friendly app name/path to an executable."""
        from . import apps
        record = self._record("app_resolve", str(name)[:120])
        return self._finish(record, apps.resolve_app(name))

    def app_status(self, name_or_path: str) -> dict[str, Any]:
        """Is the app running? Evidence: matching pids + their windows."""
        from . import apps
        name_or_path = str(name_or_path or "").strip()
        record = self._record("app_status", name_or_path[:120])
        resolved = apps.resolve_app(name_or_path)
        image = resolved["path"] if resolved.get("ok") else name_or_path
        procs = apps.find_processes(image)
        rows = []
        for proc in procs:
            rows.append({"pid": proc["pid"], "image": proc["image"],
                         "windows": apps.windows_for_pid(proc["pid"])})
        return self._finish(record, {
            "ok": True, "running": bool(procs),
            "path": resolved.get("path", ""), "processes": rows,
            "count": len(rows)})

    def app_launch(self, name_or_path: str,
                   args: list[str] | None = None,
                   wait_s: float = 10.0) -> dict[str, Any]:
        """Resolve + launch + verify (pid alive, window awaited)."""
        from . import apps
        name_or_path = str(name_or_path or "").strip()
        record = self._record("app_launch", name_or_path[:120])
        # URI schemes — https:, ms-settings:, mailto: — open via shell
        # association. No child pid exists; the open request itself is
        # the evidence (os.startfile raises on failure).
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", name_or_path) and \
                not re.match(r"^[a-zA-Z]:[\\/]", name_or_path):
            try:
                os.startfile(name_or_path)  # type: ignore[attr-defined]
            except OSError as exc:
                return self._fail(record, f"shell open failed: {exc}")
            return self._finish(record, {
                "ok": True, "shell": True, "path": name_or_path,
                "verified": False,
                "note": "opened via shell association"})
        resolved = apps.resolve_app(name_or_path)
        if not resolved.get("ok"):
            candidates = resolved.get("candidates") or []
            hint = (f" Did you mean: {', '.join(candidates[:3])}?"
                    if candidates else "")
            return self._fail(
                record,
                f"could not find an application for "
                f"'{name_or_path}'{hint}")
        if args is not None and not isinstance(args, list):
            return self._fail(record, "launch arguments must be a list")
        clean_args = [str(a)[:300] for a in (args or [])][:20]
        # Already running with a window? Bring it forward instead of
        # spawning a duplicate instance.
        if not clean_args:
            for proc in apps.find_processes(resolved["path"]):
                wins = apps.windows_for_pid(proc["pid"])
                if wins:
                    hwnd = int(wins[0]["hwnd"])
                    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    return self._finish(record, {
                        "ok": True, "pid": proc["pid"],
                        "path": resolved["path"], "verified": True,
                        "already_running": True, "focused": True,
                        "window": wins[0], "app": resolved["name"],
                        "resolved_via": resolved["source"]})
        result = apps.launch_verified(resolved["path"], clean_args,
                                      proc_wait_s=wait_s)
        result["app"] = resolved["name"]
        result["resolved_via"] = resolved["source"]
        if not result.get("ok"):
            return self._fail(
                record, str(result.get("error") or
                            f"process exited with code "
                            f"{result.get('exit_code')}"))
        return self._finish(record, result)

    def app_close(self, name_or_path: str,
                  timeout_s: float = 10.0) -> dict[str, Any]:
        """Gracefully close every instance matching the app — WM_CLOSE to
        owned windows, then wait for exit. Never force-kills."""
        from . import apps
        name_or_path = str(name_or_path or "").strip()
        record = self._record("app_close", name_or_path[:120])
        resolved = apps.resolve_app(name_or_path)
        image = resolved["path"] if resolved.get("ok") else name_or_path
        procs = apps.find_processes(image)
        if not procs:
            return self._finish(record, {"ok": True, "closed": True,
                                         "already": True, "count": 0,
                                         "path": resolved.get("path", "")})
        results, failed = [], []
        for proc in procs:
            r = apps.close_windows(proc["pid"], timeout_s=timeout_s)
            results.append(r)
            if not r.get("closed"):
                failed.append(proc["pid"])
        ok = not failed
        payload = {"ok": ok, "closed": ok, "count": len(procs),
                   "results": results,
                   "path": resolved.get("path", "")}
        if not ok:
            payload["error"] = (
                f"{len(failed)} instance(s) did not close "
                "(unsaved work or a hung window); not force-killed")
            return self._fail(record, payload["error"])
        return self._finish(record, payload)

    def app_restart(self, name_or_path: str,
                    timeout_s: float = 10.0) -> dict[str, Any]:
        """Close all matching instances, then relaunch verified."""
        from . import apps
        name_or_path = str(name_or_path or "").strip()
        record = self._record("app_restart", name_or_path[:120])
        resolved = apps.resolve_app(name_or_path)
        if not resolved.get("ok"):
            return self._fail(
                record, f"could not find an application for "
                f"'{name_or_path}'")
        procs = apps.find_processes(resolved["path"])
        for proc in procs:
            r = apps.close_windows(proc["pid"], timeout_s=timeout_s)
            if not r.get("closed"):
                return self._fail(
                    record, f"pid {proc['pid']} did not close; "
                            "restart aborted (not force-killed)")
        result = apps.launch_verified(resolved["path"], [])
        result["app"] = resolved["name"]
        if not result.get("ok"):
            return self._fail(record, str(result.get("error") or
                                          "relaunch failed"))
        return self._finish(record, result)

    def app_install(self, path: str, *, name_hint: str = "",
                    timeout_s: float = 600.0) -> dict[str, Any]:
        """Run an installer and verify what it installed. Elevation
        prompts are shown to the user — never bypassed."""
        from . import apps
        path = str(path or "").strip()
        record = self._record("app_install", path[:160])
        if not path:
            return self._fail(record, "installer path is required")
        result = apps.run_installer(
            path, name_hint=name_hint, timeout_s=timeout_s)
        if not result.get("ok"):
            return self._fail(
                record, str(result.get("error") or "install failed"))
        return self._finish(record, result)

    def app_window_state(self, state: str, *, hwnd: int = 0,
                         pid: int = 0,
                         title_substr: str = "") -> dict[str, Any]:
        """Minimize/maximize/restore a window by hwnd, pid, or title."""
        from . import apps
        record = self._record("app_window_state",
                              f"{state} {title_substr or hwnd or pid}")
        if user32 is None:
            return self._fail(record, "unsupported platform")
        target = 0
        try:
            if hwnd:
                target = int(hwnd)
            elif pid:
                wins = apps.windows_for_pid(int(pid))
                if wins:
                    target = int(wins[0]["hwnd"])
            elif title_substr:
                needle = str(title_substr)[:200].lower()
                for w in self._visible_windows():
                    if needle in w["title"].lower():
                        target = int(w["hwnd"])
                        break
        except (TypeError, ValueError):
            return self._fail(record, "invalid window identifier")
        if not target:
            return self._fail(record, "no matching window")
        result = apps.set_window_state(target, state)
        if not result.get("ok"):
            return self._fail(record, str(result.get("error")))
        return self._finish(record, result)

    # -- UI automation (observe -> act -> re-observe -> verify) -----------

    def ui_observe(self, *, hwnd: int = 0, title_substr: str = "",
                   max_items: int = 400) -> dict[str, Any]:
        """Enumerate a window's interactive elements via UIA — the
        observe step. Elements carry name/type/id/rect/enabled."""
        from . import uia
        record = self._record("ui_observe",
                              title_substr or str(hwnd))
        result = uia.ui_elements(hwnd=int(hwnd or 0),
                                 title_substr=str(title_substr or ""),
                                 max_items=int(max_items or 400))
        if not result.get("ok"):
            return self._fail(record, str(result.get("error")))
        return self._finish(record, result)

    def ui_click(self, name: str, *, hwnd: int = 0,
                 title_substr: str = "",
                 control_type: str = "") -> dict[str, Any]:
        """Click a named UI element. UIA-first: Invoke/Toggle/Select
        patterns before pixels; when no pattern exists the element's
        rect center is clicked via the mouse. Re-observes afterward and
        reports whether the screen fingerprint actually changed —
        'clicked' without a state change is evidence, not success."""
        from . import uia
        name = str(name or "").strip()
        record = self._record("ui_click", name[:160])
        if not name:
            return self._fail(record, "element name required")
        before = uia.screen_hash(hwnd=int(hwnd or 0),
                                 title_substr=str(title_substr or ""))
        loop_err = self._loop_guard.check("click", name, before)
        if loop_err:
            return self._fail(record, loop_err)
        result = uia.ui_act(name, hwnd=int(hwnd or 0),
                            title_substr=str(title_substr or ""),
                            action="invoke",
                            control_type=str(control_type or ""))
        used = str(result.get("pattern") or "")
        if not result.get("ok"):
            rect = result.get("rect") or result.get("rect_hint")
            center = uia.element_center(rect or {})
            if center is None:
                return self._fail(record, str(result.get("error") or
                                              "element not found"))
            err = self._point_error(*center)
            if err:
                return self._fail(record, err)
            click_result = self.click(center[0], center[1])
            if not click_result.get("ok"):
                return self._fail(record, str(click_result.get("error")))
            result = {"ok": True, "action": "invoke",
                      "element": result.get("element") or
                                 {"name": name},
                      "pattern": "mouse-fallback",
                      "rect": rect}
        time.sleep(0.4)  # let the UI settle before re-observing
        after = uia.screen_hash(hwnd=int(hwnd or 0),
                                title_substr=str(title_substr or ""))
        result["state_changed"] = bool(before and after
                                       and before != after)
        result["element_name"] = name
        result["pattern_used"] = used or result.get("pattern", "")
        return self._finish(record, result)

    def ui_set_text(self, name: str, value: str, *, hwnd: int = 0,
                    title_substr: str = "") -> dict[str, Any]:
        """Set a text field's value via ValuePattern and verify by
        reading it back — never assumes SetValue landed."""
        from . import uia
        name = str(name or "").strip()
        record = self._record("ui_set_text", name[:160])
        if not name:
            return self._fail(record, "element name required")
        result = uia.ui_act(name, hwnd=int(hwnd or 0),
                            title_substr=str(title_substr or ""),
                            action="setvalue", value=str(value or ""))
        if not result.get("ok"):
            return self._fail(record, str(result.get("error") or
                                          "no ValuePattern on element"))
        readback = str(result.get("readback", ""))
        result["verified"] = readback == str(value or "")
        if not result["verified"]:
            result["ok"] = False
            result["error"] = (f"setvalue readback mismatch: got "
                               f"'{readback[:60]}'")
            return self._fail(record, result["error"])
        return self._finish(record, result)

    def ui_find(self, name: str, *, hwnd: int = 0, title_substr: str = "",
                control_type: str = "") -> dict[str, Any]:
        """Locate one element by name/AutomationId — observation only."""
        from . import uia
        record = self._record("ui_find", name[:160])
        result = uia.ui_find(name, hwnd=int(hwnd or 0),
                             title_substr=str(title_substr or ""),
                             control_type=str(control_type or ""))
        if not result.get("ok"):
            return self._fail(record, str(result.get("error")))
        return self._finish(record, result)

    def ui_wait(self, name: str, *, hwnd: int = 0, title_substr: str = "",
                timeout_s: float = 8.0) -> dict[str, Any]:
        """Re-observe until an element appears (post-action check)."""
        from . import uia
        record = self._record("ui_wait", name[:160])
        result = uia.wait_for_element(
            name, hwnd=int(hwnd or 0),
            title_substr=str(title_substr or ""),
            timeout_s=float(timeout_s or 8.0))
        if not result.get("ok"):
            return self._fail(record, str(result.get("error") or
                                          "element never appeared"))
        return self._finish(record, result)

    # -- input ------------------------------------------------------------

    def mouse_move(self, x: int, y: int) -> dict:
        record = self._record("mouse_move", f"{x},{y}")
        if error := self._point_error(x, y):
            return self._fail(record, error)
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            user32.SetCursorPos(int(x), int(y))
            return self._finish(record, {"ok": True, "x": int(x), "y": int(y)})
        except Exception as exc:
            return self._fail(record, f"mouse move: {exc}")

    def click(self, x: int, y: int, *, button: str = "left") -> dict:
        button = str(button or "left").lower()
        record = self._record("click", f"{button}@{x},{y}")
        if button not in {"left", "right"}:
            return self._fail(record, "button must be 'left' or 'right'")
        if error := self._point_error(x, y):
            return self._fail(record, error)
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            user32.SetCursorPos(int(x), int(y))
            down = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
            up = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
            ok = _send_input_list([("mouse", 0, 0, 0, down),
                                   ("mouse", 0, 0, 0, up)])
            return self._finish(record, {"ok": ok, "button": button,
                                         "x": int(x), "y": int(y)})
        except Exception as exc:
            return self._fail(record, f"click: {exc}")

    def scroll(self, x: int, y: int, clicks: int = 3) -> dict:
        try:
            clicks = int(clicks)
        except (TypeError, ValueError):
            clicks = 0
        record = self._record("scroll", f"{clicks}@{x},{y}")
        if clicks == 0 or abs(clicks) > 20:
            return self._fail(record, "clicks must be a non-zero value between -20 and 20")
        if error := self._point_error(x, y):
            return self._fail(record, error)
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            user32.SetCursorPos(int(x), int(y))
            delta = clicks * 120
            ok = _send_input_list([("mouse", 0, 0, delta, MOUSEEVENTF_WHEEL)])
            return self._finish(record, {"ok": ok, "clicks": clicks,
                                         "x": int(x), "y": int(y)})
        except Exception as exc:
            return self._fail(record, f"scroll: {exc}")

    def type_text(self, text: str) -> dict:
        text = str(text or "")
        record = self._record("type_text", f"unicode text, {len(text[:4000])} chars")
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            inputs = []
            for ch in text[:4000]:
                code = ord(ch)
                inputs.append(("key", 0, code, KEYEVENTF_UNICODE))
                inputs.append(("key", 0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            ok = _send_input_list(inputs) if inputs else True
            return self._finish(record, {"ok": ok, "chars": len(inputs) // 2,
                                         "truncated": len(text) > 4000})
        except Exception as exc:
            return self._fail(record, f"type text: {exc}")

    def key_combo(self, keys: list[str]) -> dict:
        """e.g. ["ctrl","c"], ["alt","f4"], ["enter"]."""
        if keys is not None and not isinstance(keys, list):
            keys = None
        else:
            keys = [str(k).lower() for k in (keys or [])]
        record = self._record("key_combo", "+".join(keys or []))
        if keys is None:
            return self._fail(record, "keys must be a list")
        if not keys:
            return self._fail(record, "at least one key is required")
        if len(keys) > 8:
            return self._fail(record, "too many keys")
        codes = []
        for key in keys:
            if key in VK:
                codes.append(VK[key])
            elif len(key) == 1:
                codes.append(ord(key.upper()))
            else:
                return self._fail(record, f"unknown key '{key}'")
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            inputs = [("key", c, 0, 0) for c in codes]
            inputs += [("key", c, 0, KEYEVENTF_KEYUP) for c in reversed(codes)]
            return self._finish(record, {"ok": _send_input_list(inputs)})
        except Exception as exc:
            return self._fail(record, f"key combo: {exc}")

    # -- clipboard ---------------------------------------------------------

    def clipboard_get(self) -> dict:
        record = self._record("clipboard_get")
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            CF_UNICODETEXT = 13
            kernel32 = ctypes.windll.kernel32
            kernel32.GlobalLock.restype = wintypes.LPVOID
            kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
            user32.GetClipboardData.restype = wintypes.HANDLE
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            if not user32.OpenClipboard(None):
                return self._fail(record, "clipboard busy")
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return self._finish(record, {"ok": True, "chars": 0})
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return self._fail(record, "clipboard lock failed")
                try:
                    text = ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
                result = {"ok": True, "text": text}
                self._finish(record, {"ok": True, "chars": len(text)})
                return result
            finally:
                user32.CloseClipboard()
        except Exception as exc:
            return self._fail(record, f"clipboard read: {exc}")

    def clipboard_set(self, text: str) -> dict:
        text = str(text or "")
        record = self._record("clipboard_set", f"{len(text)} chars")
        if len(text) > 20000:
            return self._fail(record, "clipboard text is too long")
        if user32 is None:
            return self._fail(record, "unsupported platform")
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel32.GlobalLock.restype = wintypes.LPVOID
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
            user32.SetClipboardData.restype = wintypes.HANDLE
            user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return self._fail(record, "GlobalAlloc failed")
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return self._fail(record, "GlobalLock failed")
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.OpenClipboard(None):
                kernel32.GlobalFree(handle)
                return self._fail(record, "clipboard busy")
            try:
                user32.EmptyClipboard()
                if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                    kernel32.GlobalFree(handle)
                    handle = None
                    return self._fail(record, "SetClipboardData failed")
                handle = None
                return self._finish(record, {"ok": True, "chars": len(text)})
            finally:
                user32.CloseClipboard()
                if handle:
                    kernel32.GlobalFree(handle)
        except Exception as exc:
            return self._fail(record, f"clipboard write: {exc}")
