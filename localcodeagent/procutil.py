"""Subprocess helpers shared across the codebase.

The backend runs frozen (``pythonw``-style, no console) behind the
desktop shell. Any console-subsystem child spawned without
``CREATE_NO_WINDOW`` flashes a visible cmd window on Windows — pip
installs, git probes, ``nvidia-smi``, version checks. Pass
:func:`no_window_flags` as ``creationflags`` everywhere so helper
processes stay hidden. The flag is a no-op on POSIX.

Text-mode captures must also pass ``encoding="utf-8", errors="replace"``.
The Windows default (cp1252) crashes the stdlib ``_readerthread`` on any
UTF-8 byte it cannot map — e.g. winget's progress output — killing the
read and silently losing all captured output.
"""
from __future__ import annotations

import os
import subprocess


def no_window_flags() -> int:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return int(subprocess.CREATE_NO_WINDOW)
    return 0


_PATCHED = False


def install_hidden_children() -> None:
    """Give every subprocess ``CREATE_NO_WINDOW`` by default.

    The backend runs frozen/no-console; any console-subsystem child
    spawned without the flag allocates a fresh console window and the
    user sees cmd screens flashing on every git probe, ffmpeg call,
    or scheduled-job tick. Wrapping ``Popen.__init__`` covers
    ``run``/``call``/``check_output`` uniformly — far safer than
    auditing a hundred call sites. An explicit ``creationflags``
    (including 0) is honored as-is, so intentional console launches
    (the computer_use terminal opener) still show a window.
    Idempotent and POSIX-no-op.
    """
    global _PATCHED
    if _PATCHED or os.name != "nt" or not hasattr(
            subprocess, "CREATE_NO_WINDOW"):
        return
    _PATCHED = True
    orig_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        if kwargs.get("creationflags") is None:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init  # type: ignore[method-assign]
