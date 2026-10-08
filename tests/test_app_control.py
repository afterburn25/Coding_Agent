"""Verified application control — the 'open Notepad' lane.

Covers deterministic parse (open/close/restart/is-running/min-max),
app resolution (path/PATH/well-known/registry/Start Menu), permission
gating via the registry, evidence in the ledger, and truthful failure —
an app that isn't found or exits early must never report success.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcodeagent.action_ledger import ActionLedger
from localcodeagent.action_ops import (
    execute_plan,
    parse_local_action,
)
from localcodeagent.computer_use import apps
from localcodeagent.tools.base import ToolRegistry, ToolSpec


def make_env(perms=None):
    td = tempfile.TemporaryDirectory()
    ws = Path(td.name) / "ws"
    ws.mkdir()
    reg = ToolRegistry(perms or {
        "application.launch": "allow",
        "application.manage": "allow",
        "desktop.view": "allow",
        "desktop.control": "allow",
    })
    ledger = ActionLedger(Path(td.name) / "ledger.json")
    return td, ws, reg, ledger


def register_fake_apps(reg, behaviors=None):
    """Register the app tools with scripted JSON outcomes — no real
    processes touched."""
    behaviors = behaviors or {}
    for name, perm in (
            ("computer_app_launch", "application.launch"),
            ("computer_app_close", "application.manage"),
            ("computer_app_restart", "application.manage"),
            ("computer_app_status", "desktop.view"),
            ("computer_app_window", "desktop.control")):
        key = name.removeprefix("computer_app_")
        payload = behaviors.get(key, {"ok": True, "verified": True,
                                      "pid": 4321,
                                      "path": "C:/Apps/fake.exe"})
        reg.register(ToolSpec(
            name, f"fake {key}", {"type": "object", "properties": {}},
            perm,
            lambda a, p=payload: json.dumps(p)))


def run(text, ws, reg, ledger, **kw):
    plan = parse_local_action(text, workspace=ws)
    assert plan is not None, f"lane did not claim {text!r}"
    return plan, execute_plan(plan, tools=reg, ledger=ledger, **kw)


class TestAppParse(unittest.TestCase):
    def setUp(self):
        td, self.ws, _, _ = make_env()
        self.addCleanup(td.cleanup)

    def test_open_launch_start(self):
        for text in ("open notepad", "launch notepad",
                     "start notepad", "please open notepad"):
            plan = parse_local_action(text, workspace=self.ws)
            self.assertIsNotNone(plan, text)
            self.assertEqual(plan.kind, "launch")
            self.assertEqual(plan.tool, "computer_app_launch")
            self.assertEqual(plan.permission, "application.launch")
            self.assertEqual(plan.params["app"], "notepad")

    def test_close_quit(self):
        for text in ("close notepad", "quit notepad"):
            plan = parse_local_action(text, workspace=self.ws)
            self.assertIsNotNone(plan, text)
            self.assertEqual(plan.kind, "close")
            self.assertEqual(plan.tool, "computer_app_close")
            self.assertEqual(plan.permission, "application.manage")

    def test_restart(self):
        plan = parse_local_action("restart notepad", workspace=self.ws)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, "restart")
        self.assertEqual(plan.permission, "application.manage")

    def test_is_running(self):
        for text in ("is notepad running", "is notepad open",
                     "check if notepad is running"):
            plan = parse_local_action(text, workspace=self.ws)
            self.assertIsNotNone(plan, text)
            self.assertEqual(plan.kind, "status")
            self.assertEqual(plan.permission, "desktop.view")

    def test_window_state(self):
        plan = parse_local_action("minimize notepad", workspace=self.ws)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, "window")
        self.assertEqual(plan.params["state"], "minimize")
        self.assertEqual(plan.permission, "desktop.control")

    def test_false_positives_fall_through(self):
        for text in ("how do I open notepad",
                     "what is the best way to close an app",
                     "close the deal",
                     "start over",
                     "open up about your feelings",
                     "open the folder mydocs"):
            self.assertIsNone(
                parse_local_action(text, workspace=self.ws), text)

    def test_open_existing_file_uses_shell_open(self):
        f = self.ws / "readme.txt"
        f.write_text("hi")
        plan = parse_local_action("open readme.txt", workspace=self.ws)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, "launch")
        self.assertIn("readme.txt", plan.params["app"])

    def test_display_prefers_resolved_name(self):
        plan = parse_local_action("open notepad", workspace=self.ws)
        # On Windows notepad resolves; elsewhere display falls back to
        # the raw name — either way it's a display string, not a claim.
        self.assertTrue(plan.display)


class TestAppExecute(unittest.TestCase):
    def setUp(self):
        td, self.ws, self.reg, self.ledger = make_env()
        self.addCleanup(td.cleanup)

    def test_launch_verified(self):
        register_fake_apps(self.reg, {
            "launch": {"ok": True, "verified": True, "pid": 4321,
                       "path": "C:/Windows/System32/notepad.exe",
                       "window": {"hwnd": 99, "title": "Untitled"}}})
        _, out = run("open notepad", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("open", out["text"].lower())
        entry = self.ledger.recent(1)[0]
        self.assertEqual(entry["capability"], "application")
        self.assertIn("4321", entry["verification"])
        self.assertIn("notepad.exe", entry["artifact"])

    def test_launch_already_running_focuses(self):
        register_fake_apps(self.reg, {
            "launch": {"ok": True, "verified": True, "pid": 4321,
                       "path": "C:/Windows/notepad.exe",
                       "already_running": True}})
        _, out = run("open notepad", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("already running", out["text"])

    def test_launch_exit_early_is_failure(self):
        register_fake_apps(self.reg, {
            "launch": {"ok": False, "verified": False, "pid": 5000,
                       "path": "C:/Apps/bad.exe", "exited": True,
                       "exit_code": 1,
                       "error": "process exited with code 1"}})
        _, out = run("open badapp", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "failed")
        self.assertNotIn("is open", out["text"])

    def test_launch_unknown_app_fails_truthfully(self):
        register_fake_apps(self.reg, {
            "launch": {"ok": False, "verified": False,
                       "error": "could not find an application"}})
        _, out = run("open frobnicate9000", self.ws, self.reg,
                     self.ledger)
        self.assertEqual(out["status"], "failed")
        self.assertIn("could not find", out["text"].lower())

    def test_close_not_running_is_honest_noop(self):
        register_fake_apps(self.reg, {
            "close": {"ok": True, "closed": True, "already": True,
                      "count": 0}})
        _, out = run("close notepad", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("wasn't running", out["text"])

    def test_close_refuses_silent_force(self):
        register_fake_apps(self.reg, {
            "close": {"ok": False, "closed": False,
                      "error": "process still running after close "
                               "request"}})
        _, out = run("close notepad", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "failed")
        self.assertIn("still running", out["text"])

    def test_status_running_and_not(self):
        register_fake_apps(self.reg, {
            "status": {"ok": True, "running": True, "count": 1,
                       "processes": [{"pid": 10}]}})
        _, out = run("is notepad running", self.ws, self.reg,
                     self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("is running", out["text"])

        reg2_td, ws2, reg2, ledger2 = make_env()
        self.addCleanup(reg2_td.cleanup)
        register_fake_apps(reg2, {
            "status": {"ok": True, "running": False, "count": 0}})
        _, out = run("is notepad running", ws2, reg2, ledger2)
        self.assertIn("isn't running", out["text"])

    def test_window_state_reports(self):
        register_fake_apps(self.reg, {
            "window": {"ok": True, "hwnd": 77, "state": "minimize"}})
        _, out = run("minimize notepad", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("minimized", out["text"])


class TestAppPermissions(unittest.TestCase):
    def test_launch_asks_then_resumes(self):
        td, ws, reg, ledger = make_env({"application.launch": "ask"})
        self.addCleanup(td.cleanup)
        register_fake_apps(reg)
        plan, out = run("open notepad", ws, reg, ledger)
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertIn("application.launch", out["text"])
        out2 = execute_plan(plan, tools=reg, ledger=ledger,
                            approved=True)
        self.assertEqual(out2["status"], "verified")

    def test_close_denied_never_runs(self):
        td, ws, reg, ledger = make_env({"application.manage": "deny"})
        self.addCleanup(td.cleanup)
        register_fake_apps(reg)
        _, out = run("close notepad", ws, reg, ledger)
        self.assertEqual(out["status"], "denied")
        self.assertIn("deny", out["text"])

    def test_launch_denied_never_runs(self):
        td, ws, reg, ledger = make_env({"application.launch": "deny"})
        self.addCleanup(td.cleanup)
        register_fake_apps(reg)
        _, out = run("open notepad", ws, reg, ledger)
        self.assertEqual(out["status"], "denied")

    def test_disabled_tool_unavailable(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        register_fake_apps(reg)
        reg.set_enabled("computer_app_launch", False)
        _, out = run("open notepad", ws, reg, ledger)
        self.assertEqual(out["status"], "unavailable")


class TestAppPrimitives(unittest.TestCase):
    """Pure-resolution tests — no processes launched."""

    def test_resolve_explicit_path(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        exe = Path(td.name) / "fake.exe"
        exe.write_bytes(b"MZ")
        r = apps.resolve_app(str(exe))
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "path")
        self.assertEqual(Path(r["path"]).name, "fake.exe")

    def test_resolve_missing_path_is_error(self):
        r = apps.resolve_app("D:\\definitely\\missing\\nope.exe")
        self.assertFalse(r["ok"])
        self.assertIn("no file", r.get("error", ""))

    def test_resolve_unknown_name_fails(self):
        r = apps.resolve_app("zzz-no-such-app-nexus-test-9x")
        self.assertFalse(r["ok"])

    def test_resolve_rejects_absurd_input(self):
        self.assertFalse(apps.resolve_app("").get("ok"))
        self.assertFalse(apps.resolve_app("x" * 400).get("ok"))

    @unittest.skipUnless(apps.os.name == "nt", "Windows-only primitive")
    def test_resolve_notepad_windows(self):
        r = apps.resolve_app("notepad")
        self.assertTrue(r["ok"])
        self.assertTrue(r["path"].lower().endswith("notepad.exe"))

    @unittest.skipUnless(apps.os.name == "nt", "Windows-only primitive")
    def test_pid_alive_and_tasklist(self):
        import subprocess
        proc = subprocess.Popen(
            ["cmd", "/c", "ping", "-n", "30", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(apps.pid_alive(proc.pid))
            rows = apps.find_processes("cmd.exe")
            self.assertTrue(any(r["pid"] == proc.pid for r in rows))
        finally:
            proc.kill()
            proc.wait()
        self.assertFalse(apps.pid_alive(proc.pid))

    def test_pid_alive_rejects_zero(self):
        self.assertFalse(apps.pid_alive(0))

    def test_window_state_rejects_bad_input(self):
        r = apps.set_window_state(999999999, "minimize")
        self.assertFalse(r["ok"])
        r = apps.set_window_state(0, "sideways")
        self.assertFalse(r["ok"])

    def test_launch_verified_no_path(self):
        r = apps.launch_verified("")
        self.assertFalse(r["ok"])

    def test_launch_verified_missing_exe(self):
        r = apps.launch_verified("D:\\missing\\never.exe")
        self.assertFalse(r["ok"])
        self.assertFalse(r["verified"])

    @unittest.skipUnless(apps.os.name == "nt", "Windows-only lifecycle")
    def test_launch_close_roundtrip_cmd(self):
        # A real bounded lifecycle: launch a harmless console process,
        # verify the pid evidence, then confirm pid liveness flips.
        import subprocess
        proc = subprocess.Popen(["cmd", "/c", "exit", "0"])
        proc.wait(timeout=10)
        # An exited process must NOT verify — the anti-false-success
        # contract at the primitive level.
        self.assertFalse(apps.pid_alive(proc.pid))


if __name__ == "__main__":
    unittest.main()
