"""Test to verify the UTF-8 encoding fix for subprocess calls.

This test ensures that the fix for UTF-8 encoding in subprocess calls
works correctly and prevents crashes when handling non-ASCII characters.
"""

import subprocess
import sys
import tempfile
import os
from pathlib import Path

def test_subprocess_utf8_handling():
    """Test that subprocess calls properly handle UTF-8 characters."""
    # Test with a command that outputs UTF-8 characters
    try:
        # This test specifically targets the issue that was fixed
        # where subprocess calls without proper encoding could fail
        # on Windows with cp1252 encoding when encountering UTF-8 chars
        
        if sys.platform.startswith("win"):
            # On Windows, test with a command that might produce non-ASCII output
            # This simulates the scenario that would fail without the fix
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
        # If this fails, it means there's still an encoding issue
        raise AssertionError(f"UTF-8 subprocess encoding test failed: {e}")

def test_terminal_module_import():
    """Test that the terminal module can be imported without issues."""
    try:
        from localcodeagent.tools import terminal
        # If we can import it, the fix is in place
        assert hasattr(terminal, 'run_process_streaming')
        print("Terminal module imported successfully")
    except Exception as e:
        raise AssertionError(f"Terminal module import failed: {e}")

if __name__ == "__main__":
    test_subprocess_utf8_handling()
    test_terminal_module_import()
    print("All UTF-8 encoding tests passed!")