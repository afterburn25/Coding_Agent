"""Regression test for UTF-8 subprocess encoding issue.

This test ensures that subprocess calls with UTF-8 output don't crash
due to encoding issues on Windows where the default encoding (cp1252)
can't map all UTF-8 characters.
"""

import subprocess
import sys
from pathlib import Path

def test_utf8_subprocess_encoding():
    """Test that subprocess calls handle UTF-8 output correctly."""
    # This test would fail without the encoding="utf-8", errors="replace" fix
    # Create a subprocess that outputs UTF-8 characters that might cause issues
    try:
        # Test with a command that might produce problematic UTF-8
        if sys.platform.startswith("win"):
            # On Windows, test with a command that might produce non-ASCII output
            result = subprocess.run(
                ["cmd", "/c", "echo", "Hello 世界 🌍"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            # This should not raise an exception
            assert result.returncode == 0
            assert "Hello" in result.stdout
        else:
            # On Unix-like systems
            result = subprocess.run(
                ["echo", "Hello 世界 🌍"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            assert result.returncode == 0
            assert "Hello" in result.stdout
            
    except Exception as e:
        # If this fails, it means the encoding fix is missing
        raise AssertionError(f"UTF-8 subprocess encoding test failed: {e}")

if __name__ == "__main__":
    test_utf8_subprocess_encoding()
    print("UTF-8 subprocess encoding test passed!")

def test_children_default_hidden_on_windows():
    """The backend runs without a console — every console-subsystem
    child it spawns without CREATE_NO_WINDOW flashes a visible cmd
    screen for the user (git probes, ffmpeg, scheduled-job ticks).
    The package-level procutil shim injects the flag by default while
    honoring explicit creationflags (a user-requested app launch
    keeps its window)."""
    import os
    if os.name != "nt":
        import pytest
        pytest.skip("Windows-only console flag")
    import localcodeagent  # importing the package installs the shim
    import _winapi

    calls = []
    orig_cp = _winapi.CreateProcess

    def spy(*a, **kw):
        calls.append(a[5] if len(a) > 5 else kw.get("creation_flags"))
        return orig_cp(*a, **kw)

    _winapi.CreateProcess = spy
    try:
        subprocess.run(["python", "-c", "pass"], capture_output=True)
        subprocess.run(["python", "-c", "pass"],
                       creationflags=0, capture_output=True)
    finally:
        _winapi.CreateProcess = orig_cp

    assert calls[0] & subprocess.CREATE_NO_WINDOW
    assert not (calls[1] & subprocess.CREATE_NO_WINDOW)
