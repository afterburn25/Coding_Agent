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
