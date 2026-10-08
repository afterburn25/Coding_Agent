"""UIA observation + action layer for the GUI control loop.

Unit coverage: loop detection, element-center math, matching rules.
Live coverage (skipped when no window is available): enumerate a real
window's elements, find a named element, take a screen fingerprint.
Nothing here clicks or types on the user's desktop.
"""
from __future__ import annotations

import json
import os
import unittest

from localcodeagent.computer_use import uia


class TestLoopGuard(unittest.TestCase):
    def test_repeated_no_change_detected(self):
        g = uia.LoopGuard(max_repeats=2)
        self.assertEqual(g.check("click", "Save", "h1"), "")
        self.assertEqual(g.check("click", "Save", "h1"), "")
        err = g.check("click", "Save", "h1")
        self.assertIn("loop detected", err)

    def test_changing_state_resets(self):
        g = uia.LoopGuard(max_repeats=2)
        g.check("click", "Save", "h1")
        g.check("click", "Save", "h2")  # screen changed — progress
        self.assertEqual(g.check("click", "Save", "h3"), "")

    def test_different_targets_not_looped(self):
        g = uia.LoopGuard(max_repeats=2)
        g.check("click", "A", "h1")
        g.check("click", "B", "h1")
        g.check("click", "C", "h1")
        self.assertEqual(g.check("click", "D", "h1"), "")

    def test_alternating_targets_not_looped(self):
        g = uia.LoopGuard(max_repeats=2)
        g.check("click", "A", "h")
        g.check("click", "B", "h")
        g.check("click", "A", "h")
        g.check("click", "B", "h")
        self.assertEqual(g.check("click", "A", "h"), "")


class TestHelpers(unittest.TestCase):
    def test_element_center(self):
        self.assertEqual(
            uia.element_center({"x": 10, "y": 20, "w": 30, "h": 40}),
            (25, 40))

    def test_element_center_bad_input(self):
        self.assertIsNone(uia.element_center({}))
        self.assertIsNone(uia.element_center(None))

    def test_run_ps_rejects_non_windows(self):
        if os.name == "nt":
            self.skipTest("windows")
        r = uia._run_ps("echo hi")
        self.assertFalse(r["ok"])

    def test_control_chars_sanitized(self):
        # UIA element names can embed \x07 etc. — must not break JSON.
        import subprocess
        script = ("[Console]::Out.WriteLine('{\"name\":\"a\x07b\"}')")
        from localcodeagent.computer_use.uia import _run_ps
        if os.name != "nt":
            self.skipTest("windows only")
        r = _run_ps(script)
        self.assertTrue(r["ok"])
        self.assertIn("a", r["data"]["name"])


@unittest.skipUnless(os.name == "nt", "Windows-only")
class TestLiveObservation(unittest.TestCase):
    """Real UIA queries against whatever window is currently visible —
    read-only observation only (no clicks, no input)."""

    def _any_window(self):
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(h, _l):
            if u32.IsWindowVisible(h) and u32.GetWindowTextLengthW(h):
                buf = ctypes.create_unicode_buffer(256)
                u32.GetWindowTextW(h, buf, 256)
                found.append({"hwnd": int(h), "title": buf.value})
            return True

        u32.EnumWindows(cb, 0)
        return found

    def test_enumerate_real_window(self):
        wins = self._any_window()
        if not wins:
            self.skipTest("no visible windows")
        # Prefer a rich provider; fall back to any window.
        hwnd = wins[0]["hwnd"]
        r = uia.ui_elements(hwnd=hwnd, max_items=200)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertIn("elements", r)
        self.assertIn("window", r)

    def test_screen_hash_stable(self):
        wins = self._any_window()
        if not wins:
            self.skipTest("no visible windows")
        h1 = uia.screen_hash(hwnd=wins[0]["hwnd"])
        h2 = uia.screen_hash(hwnd=wins[0]["hwnd"])
        self.assertTrue(h1)
        self.assertEqual(h1, h2)

    def test_find_missing_element_honest(self):
        wins = self._any_window()
        if not wins:
            self.skipTest("no visible windows")
        r = uia.ui_find("definitely-no-such-element-xyz",
                        hwnd=wins[0]["hwnd"])
        self.assertFalse(r["ok"])
        self.assertIn("no element", r["error"])

    def test_garbage_hwnd_never_crashes(self):
        # UIA may resolve odd handles leniently — the contract is a
        # structured result, never an exception.
        r = uia.ui_elements(hwnd=0x7FFFFFFF)
        self.assertIsInstance(r, dict)
        self.assertIn("ok", r)
        if r["ok"]:
            self.assertIsInstance(r.get("elements"), list)


if __name__ == "__main__":
    unittest.main()
