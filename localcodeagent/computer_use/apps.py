"""Application resolution and lifecycle primitives (Windows).

Deterministic app control: resolve a friendly name to an executable
(explicit path -> PATH -> well-known table -> App Paths registry ->
Start Menu shortcuts), find owning processes, and verify outcomes with
evidence (pid alive / window present). Everything is a pure function —
the ``ComputerUse`` class wraps these with audit recording.

Nothing here claims success on intent: ``launch_verified`` polls the
pid and window; ``close_app`` waits for the process to actually exit.
"""
from __future__ import annotations

import csv
import ctypes
import os
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

if os.name == "nt":
    import winreg
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
else:  # primitives report unsupported cleanly on other platforms
    winreg = None  # type: ignore[assignment]
    user32 = None  # type: ignore[assignment]
    kernel32 = None  # type: ignore[assignment]

WM_CLOSE = 0x0010
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 6, 3, 9

# Common Windows built-ins users name without a path. Values resolve
# through PATH so the entries stay honest when an app is absent.
_WELL_KNOWN = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "mspaint": "mspaint.exe",
    "command prompt": "cmd.exe", "cmd": "cmd.exe",
    "powershell": "powershell.exe", "pwsh": "pwsh.exe",
    "terminal": "wt.exe", "windows terminal": "wt.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "task manager": "taskmgr.exe", "taskmgr": "taskmgr.exe",
    "snipping tool": "snippingtool.exe", "snippingtool": "snippingtool.exe",
    "wordpad": "wordpad.exe",
    "control panel": "control.exe",
    "registry editor": "regedit.exe", "regedit": "regedit.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
}

# Friendly aliases -> App Paths/registry keys worth probing first.
_APP_PATHS_HINTS = {
    "visual studio": ("devenv.exe",),
    "vs code": ("Code.exe",), "code": ("Code.exe",),
    "vscode": ("Code.exe",), "visual studio code": ("Code.exe",),
    "chrome": ("chrome.exe",), "google chrome": ("chrome.exe",),
    "firefox": ("firefox.exe",),
    "word": ("WINWORD.EXE",), "excel": ("EXCEL.EXE",),
    "outlook": ("OUTLOOK.EXE",), "powerpoint": ("POWERPNT.EXE",),
    "7-zip": ("7zFM.exe", "7z.exe"), "7z": ("7zFM.exe", "7z.exe"),
}


def _start_menu_dirs() -> list[Path]:
    out = []
    for env in ("PROGRAMDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            out.append(Path(base) / "Microsoft" / "Windows"
                       / "Start Menu" / "Programs")
    return [d for d in out if d.is_dir()]


def _app_paths_lookup(key: str) -> str:
    """HKLM/HKCU App Paths default value -> exe path, or ''."""
    if winreg is None:
        return ""
    sub = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + key)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (0, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, sub, 0,
                                    winreg.KEY_READ | view) as h:
                    value, _ = winreg.QueryValueEx(h, "")
                    value = str(value).strip().strip('"')
                    if value and Path(value).is_file():
                        return value
            except (OSError, ValueError):
                continue
    return ""


def _resolve_lnk(path: Path, timeout_s: float = 8.0) -> str:
    """Resolve a .lnk target via WScript.Shell (bounded PowerShell)."""
    if os.name != "nt":
        return ""
    ps = (
        "$s=(New-Object -COM WScript.Shell).CreateShortcut("
        f"'{str(path).replace(chr(39), '')}');"
        "if ($s.TargetPath) { Write-Output $s.TargetPath }")
    try:
        from ..procutil import no_window_flags
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=timeout_s,
            creationflags=no_window_flags())
        target = (r.stdout or "").strip().splitlines()
        return target[0].strip() if target else ""
    except Exception:
        return ""


def resolve_app(name: str, *, timeout_s: float = 8.0) -> dict[str, Any]:
    """Resolve a friendly name or path to an executable.

    Returns {ok, path, source, name, candidates}. ``source`` records how
    the executable was found (evidence, not guesswork); ``candidates``
    lists the Start Menu matches when resolution is ambiguous.
    """
    raw = str(name or "").strip().strip('"').strip("'")
    if not raw:
        return {"ok": False, "path": "", "source": "", "name": raw,
                "candidates": []}
    if len(raw) > 260:
        return {"ok": False, "path": "", "source": "", "name": raw,
                "candidates": [], "error": "name too long"}

    # 1. Explicit path.
    if os.sep in raw or (len(raw) > 1 and raw[1] == ":"):
        p = Path(raw)
        if p.is_file():
            return {"ok": True, "path": str(p), "source": "path",
                    "name": p.stem, "candidates": []}
        return {"ok": False, "path": "", "source": "path",
                "name": raw, "candidates": [],
                "error": f"no file at {raw}"}

    lower = raw.lower()

    # 2. PATH lookup (bare name and name.exe).
    for probe in (raw, raw if raw.lower().endswith(".exe")
                  else raw + ".exe"):
        found = shutil.which(probe)
        if found:
            return {"ok": True, "path": found, "source": "PATH",
                    "name": Path(found).stem, "candidates": []}

    # 3. Well-known Windows built-ins.
    mapped = _WELL_KNOWN.get(lower)
    if mapped:
        found = shutil.which(mapped) or _app_paths_lookup(mapped)
        if found:
            return {"ok": True, "path": found, "source": "well-known",
                    "name": Path(found).stem, "candidates": []}

    # 4. App Paths registry — hinted aliases, then literal name keys.
    keys = list(_APP_PATHS_HINTS.get(lower, ()))
    keys += [raw + ".exe", raw]
    for key in keys:
        found = _app_paths_lookup(key)
        if found:
            return {"ok": True, "path": found, "source": "app-paths",
                    "name": Path(found).stem, "candidates": []}

    # 5. Start Menu shortcuts — stem substring match on *.lnk files.
    matches: list[Path] = []
    for root in _start_menu_dirs():
        try:
            for lnk in root.rglob("*.lnk"):
                if lower in lnk.stem.lower():
                    matches.append(lnk)
        except OSError:
            continue
    if matches:
        matches.sort(key=lambda p: (len(p.stem), str(p)))
        target = _resolve_lnk(matches[0], timeout_s=timeout_s)
        if target and Path(target).is_file():
            return {"ok": True, "path": target,
                    "source": "start-menu",
                    "name": matches[0].stem,
                    "candidates": [m.stem for m in matches[:5]]}
        if len(matches) > 1:
            return {"ok": False, "path": "", "source": "start-menu",
                    "name": raw,
                    "candidates": [m.stem for m in matches[:5]],
                    "error": "shortcut target could not be resolved"}

    return {"ok": False, "path": "", "source": "", "name": raw,
            "candidates": [], "error": f"no application found for {raw}"}


def find_processes(image_or_path: str, *, timeout_s: float = 15.0
                   ) -> list[dict[str, Any]]:
    """tasklist image-name match -> [{pid, image}]. Exact stem match so
    'notepad' never hits 'notepad2'."""
    name = Path(str(image_or_path or "")).name
    if not name or os.name != "nt":
        return []
    if not name.lower().endswith(".exe"):
        name += ".exe"
    try:
        from ..procutil import no_window_flags
        r = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI",
             f"IMAGENAME eq {name}"],
            capture_output=True, text=True, timeout=timeout_s,
            creationflags=no_window_flags())
    except Exception:
        return []
    out = []
    for row in csv.reader((r.stdout or "").splitlines()):
        if len(row) >= 2 and row[1].isdigit():
            out.append({"pid": int(row[1]), "image": row[0]})
    return out


def pid_alive(pid: int) -> bool:
    """True while the process exists and has not exited."""
    if kernel32 is None or not pid:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                  False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def windows_for_pid(pid: int) -> list[dict[str, Any]]:
    """Visible, titled top-level windows owned by ``pid``."""
    if user32 is None or not pid:
        return []
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(hwnd, _lparam):
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) == int(pid) and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value[:200]
            if title:
                out.append({"hwnd": int(hwnd), "title": title,
                            "pid": int(pid)})
        return True

    user32.EnumWindows(enum_cb, 0)
    return out


def wait_for_pid(pid: int, timeout_s: float, *, alive: bool = True,
                 interval: float = 0.25) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if pid_alive(pid) == alive:
            return True
        time.sleep(interval)
    return pid_alive(pid) == alive


def wait_for_window(pid: int, timeout_s: float,
                    interval: float = 0.4) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return None
        wins = windows_for_pid(pid)
        if wins:
            return wins[0]
        time.sleep(interval)
    wins = windows_for_pid(pid)
    return wins[0] if wins else None


def launch_verified(path: str, args: list[str] | None = None, *,
                    proc_wait_s: float = 10.0,
                    window_wait_s: float = 6.0) -> dict[str, Any]:
    """Launch ``path`` then verify: pid must stay alive past spawn and a
    window is awaited (optional — console apps may never show one).

    Returns evidence: pid, path, window hwnd/title, ``verified`` and
    ``exited``/``exit_code`` when the process dies early.
    """
    p = str(path or "").strip()
    if not p:
        return {"ok": False, "verified": False,
                "error": "executable path required"}
    args = list(args or [])
    try:
        if p.lower().endswith((".lnk", ".url")) or not \
                p.lower().endswith(".exe"):
            # Shortcuts / documents / URI handlers go through the shell;
            # no pid is available — verification is window-title only.
            os.startfile(p)  # type: ignore[attr-defined]
            return {"ok": True, "pid": 0, "path": p, "shell": True,
                    "verified": False,
                    "note": "opened via shell — no child process handle"}
        proc = subprocess.Popen([p, *args], close_fds=True)
    except OSError as exc:
        return {"ok": False, "verified": False,
                "error": f"launch failed: {exc}"}
    pid = int(proc.pid)
    time.sleep(0.3)
    code = proc.poll()
    if code is not None:
        return {"ok": False, "pid": pid, "path": p, "verified": False,
                "exited": True, "exit_code": int(code)}
    if not wait_for_pid(pid, proc_wait_s):
        code = proc.poll()
        return {"ok": False, "pid": pid, "path": p, "verified": False,
                "exited": True,
                "exit_code": int(code) if code is not None else -1}
    window = wait_for_window(pid, window_wait_s)
    result = {"ok": True, "pid": pid, "path": p, "verified": True,
              "window": window}
    return result


def close_windows(pid: int, timeout_s: float = 10.0) -> dict[str, Any]:
    """Post WM_CLOSE to every window owned by ``pid`` and wait for the
    process to exit. Graceful only — this never force-kills."""
    wins = windows_for_pid(pid)
    if user32 is None:
        return {"ok": False, "error": "unsupported platform"}
    if not pid_alive(pid):
        return {"ok": True, "pid": int(pid), "already": True,
                "closed": True}
    if not wins:
        return {"ok": False, "pid": int(pid), "closed": False,
                "error": "process has no window to close gracefully"}
    for w in wins:
        user32.PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
    exited = wait_for_pid(pid, timeout_s, alive=False)
    return {"ok": bool(exited), "pid": int(pid),
            "closed": bool(exited), "windows": wins,
            "error": "" if exited else "process still running after close request"}


def set_window_state(hwnd: int, state: str) -> dict[str, Any]:
    """minimize | maximize | restore a window handle."""
    if user32 is None:
        return {"ok": False, "error": "unsupported platform"}
    flag = {"minimize": SW_MINIMIZE, "min": SW_MINIMIZE,
            "maximize": SW_MAXIMIZE, "max": SW_MAXIMIZE,
            "restore": SW_RESTORE, "normal": SW_RESTORE}.get(
        str(state or "").strip().lower())
    if flag is None:
        return {"ok": False, "error": f"unknown window state: {state}"}
    try:
        handle = int(hwnd)
    except (TypeError, ValueError):
        return {"ok": False, "error": "hwnd must be an integer"}
    if not handle or not user32.IsWindow(handle):
        return {"ok": False, "error": "window no longer exists"}
    user32.ShowWindow(handle, flag)
    return {"ok": True, "hwnd": handle, "state": str(state).lower()}
