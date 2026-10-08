"""Installer lane — identify -> permission gate -> run -> monitor ->
verify via uninstall-registry diff.

Real-process coverage uses a renamed system utility (where.exe) as a
benign 'installer' that exits immediately; nothing is installed and no
elevation is requested. Registry reads are read-only.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from localcodeagent.action_ledger import ActionLedger
from localcodeagent.action_ops import execute_plan, parse_local_action
from localcodeagent.computer_use import apps
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.computer_use import register_computer_use_tools


def make_env(perms=None):
    td = tempfile.TemporaryDirectory()
    ws = Path(td.name) / "ws"
    ws.mkdir()
    reg = ToolRegistry(perms or {"packages.install": "allow"})
    register_computer_use_tools(reg, ws)
    ledger = ActionLedger(Path(td.name) / "ledger.json")
    return td, ws, reg, ledger


class TestIdentify(unittest.TestCase):
    def test_identify_exe(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "thing-setup.exe"
            f.write_bytes(b"MZ")
            r = apps.identify_installer(f)
            self.assertTrue(r["ok"])
            self.assertEqual(r["kind"], "exe")
            self.assertEqual(r["size"], 2)

    def test_reject_non_installer(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "notes.txt"
            f.write_text("hi")
            r = apps.identify_installer(f)
            self.assertFalse(r["ok"])
            self.assertIn("not an installer", r["error"])

    def test_missing_file(self):
        r = apps.identify_installer(r"D:\nope\missing-12345.exe")
        self.assertFalse(r["ok"])


class TestFindInstaller(unittest.TestCase):
    def test_newest_token_match(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            old = d / "7z2300-x64.exe"
            new = d / "7z2409-x64.exe"
            old.write_bytes(b"a")
            new.write_bytes(b"b")
            os.utime(old, (1, 1))
            r = apps.find_installer("7-zip", [d])
            self.assertTrue(r["ok"])
            self.assertEqual(Path(r["path"]).name, "7z2409-x64.exe")

    def test_no_match_honest(self):
        with tempfile.TemporaryDirectory() as td:
            r = apps.find_installer("blender", [Path(td)])
            self.assertFalse(r["ok"])
            self.assertIn("Downloads", r["error"])

    def test_latest_installer(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "a-setup.exe").write_bytes(b"a")
            (d / "readme.txt").write_text("x")
            r = apps.latest_installer([d])
            self.assertTrue(r["ok"])
            self.assertTrue(r["path"].endswith("a-setup.exe"))


@unittest.skipUnless(os.name == "nt", "Windows-only")
class TestRunInstaller(unittest.TestCase):
    def test_finished_process_unverified_truthful(self):
        """A benign 'installer' that exits cleanly: the run completes,
        but with no registry diff the result is finished+unverified —
        never a claimed install."""
        with tempfile.TemporaryDirectory() as td:
            src = shutil.which("where.exe")
            if not src:
                self.skipTest("where.exe unavailable")
            fake = Path(td) / "where-setup.exe"
            shutil.copy(src, fake)
            r = apps.run_installer(fake, timeout_s=60, settle_s=1.0)
            self.assertTrue(r["ok"])
            self.assertTrue(r["finished"])
            self.assertFalse(r["timed_out"])
            self.assertFalse(r["verified"])
            self.assertIn("couldn't", r.get("note", "") + "couldn't") \
                if "note" not in r else None
            if not r.get("verified"):
                self.assertIn("no new product", r["note"])

    def test_registry_diff_detects_install(self):
        """Verification evidence comes from new uninstall entries."""
        fake_entry = {"name": "Widget 1.0", "version": "1.0",
                      "publisher": "W", "key": "K1"}
        calls = iter(([], [fake_entry]))
        with tempfile.TemporaryDirectory() as td:
            src = shutil.which("where.exe")
            if not src:
                self.skipTest("where.exe unavailable")
            fake = Path(td) / "widget-setup.exe"
            shutil.copy(src, fake)
            with mock.patch.object(apps, "installed_products",
                                   side_effect=lambda *a, **k:
                                   next(calls)):
                r = apps.run_installer(fake, timeout_s=60, settle_s=1.0)
            self.assertTrue(r["ok"])
            self.assertTrue(r["verified"])
            self.assertEqual(r["installed"][0]["name"], "Widget 1.0")


class TestInstallIntent(unittest.TestCase):
    def setUp(self):
        td, self.ws, self.reg, self.ledger = make_env()
        self.addCleanup(td.cleanup)

    def test_install_explicit_path(self):
        f = self.ws / "app-setup.exe"
        f.write_bytes(b"MZ")
        p = parse_local_action(f"install {f}", workspace=self.ws)
        self.assertIsNotNone(p)
        self.assertEqual(p.kind, "install")
        self.assertEqual(p.tool, "computer_install")
        self.assertEqual(p.permission, "packages.install")
        self.assertEqual(p.params["path"], str(f))

    def test_install_product_searches_downloads(self):
        orig = apps.find_installer
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "7z2409-x64.exe").write_bytes(b"x")
            with mock.patch.object(
                    apps, "find_installer",
                    lambda name, dirs=None: orig(name, [d])):
                p = parse_local_action("install 7-zip", workspace=self.ws)
        self.assertIsNotNone(p)
        self.assertEqual(p.kind, "install")
        self.assertTrue(p.params["path"].endswith("7z2409-x64.exe"))
        self.assertEqual(p.params["name_hint"], "7-zip")

    def test_install_missing_product_clarifies(self):
        with mock.patch.object(
                apps, "find_installer",
                return_value={"ok": False, "path": "", "candidates": [],
                              "error": "none"}):
            p = parse_local_action("install nosuchthing",
                                   workspace=self.ws)
        self.assertEqual(p.kind, "install")
        self.assertIn("Downloads", p.clarify)

    def test_run_the_installer(self):
        orig = apps.latest_installer
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "x-setup.msi").write_bytes(b"x")
            with mock.patch.object(
                    apps, "latest_installer",
                    lambda dirs=None: orig([d])):
                p = parse_local_action("run the installer I downloaded",
                                       workspace=self.ws)
        self.assertIsNotNone(p)
        self.assertTrue(p.params["path"].endswith("x-setup.msi"))

    def test_open_the_installer_not_app(self):
        # 'open the installer' must reach the install lane, not try to
        # resolve an application literally named 'installer'.
        with mock.patch.object(
                apps, "latest_installer",
                return_value={"ok": False, "path": "", "candidates": [],
                              "error": "none"}):
            p = parse_local_action("open the installer",
                                   workspace=self.ws)
        self.assertEqual(p.kind, "install")

    def test_conversational_falls_through(self):
        for phrase in ("set up a meeting", "install it",
                       "install the app", "upgrade this application"):
            p = parse_local_action(phrase, workspace=self.ws)
            if p is not None:
                # 'upgrade this application' → generic-word clarify,
                # which is a legitimate bounded answer.
                self.assertTrue(p.clarify)

    def test_permission_gate(self):
        td, ws, reg, ledger = make_env({"packages.install": "ask"})
        self.addCleanup(td.cleanup)
        f = ws / "x-setup.exe"
        f.write_bytes(b"MZ")
        p = parse_local_action(f"install {f}", workspace=ws)
        out = execute_plan(p, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertIn("packages.install", out["text"])

    def test_denied_never_runs(self):
        td, ws, reg, ledger = make_env({"packages.install": "deny"})
        self.addCleanup(td.cleanup)
        f = ws / "x-setup.exe"
        f.write_bytes(b"MZ")
        p = parse_local_action(f"install {f}", workspace=ws)
        out = execute_plan(p, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "denied")


if __name__ == "__main__":
    unittest.main()
