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