"""File discovery / inspect / archive intents in the action lane.

'find files named X', 'search files for X', 'inspect X', 'hash X',
'zip X [to Y]', 'extract X [to Y]' — parsed deterministically and
executed through the real filesystem tools, so every success string is
backed by an on-disk post-condition.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from localcodeagent.action_ledger import ActionLedger
from localcodeagent.action_ops import execute_plan, parse_local_action
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.filesystem import register_filesystem_tools
from localcodeagent.tools.search import register_search_tools


def make_env():
    td = tempfile.TemporaryDirectory()
    ws = Path(td.name) / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello nexus", encoding="utf-8")
    (ws / "docs").mkdir()
    (ws / "docs" / "report.log").write_text(
        "line one\nTODO fix this\nline three", encoding="utf-8")
    reg = ToolRegistry({"filesystem.read": "allow",
                        "filesystem.write": "allow",
                        "filesystem.delete": "allow"})
    register_filesystem_tools(reg, ws)
    register_search_tools(reg, ws)
    ledger = ActionLedger(Path(td.name) / "ledger.json")
    return td, ws, reg, ledger


class TestDiscoveryParse(unittest.TestCase):
    def setUp(self):
        td, self.ws, _, _ = make_env()
        self.addCleanup(td.cleanup)

    def test_find_named(self):
        p = parse_local_action("find files named *.log", workspace=self.ws)
        self.assertEqual((p.kind, p.tool), ("search", "search_filename"))
        self.assertEqual(p.params["pattern"], "*.log")
        self.assertEqual(p.permission, "filesystem.read")

    def test_find_bare_glob(self):
        p = parse_local_action("find notes.txt", workspace=self.ws)
        self.assertEqual(p.kind, "search")

    def test_find_non_file_falls_through(self):
        self.assertIsNone(parse_local_action(
            "find my keys", workspace=self.ws))

    def test_search_text(self):
        p = parse_local_action("search files for TODO", workspace=self.ws)
        self.assertEqual((p.kind, p.tool), ("search_text", "search_text"))
        self.assertEqual(p.params["query"], "TODO")

    def test_find_containing(self):
        p = parse_local_action("find files containing password",
                               workspace=self.ws)
        self.assertEqual(p.kind, "search_text")
        self.assertEqual(p.params["query"], "password")

    def test_find_empty_clarifies(self):
        p = parse_local_action("find files", workspace=self.ws)
        self.assertEqual(p.kind, "search")
        self.assertTrue(p.clarify)

    def test_inspect(self):
        p = parse_local_action("inspect notes.txt", workspace=self.ws)
        self.assertEqual((p.kind, p.tool), ("inspect", "fs_stat"))
        self.assertNotIn("hash", p.params)

    def test_checksum_sets_hash(self):
        p = parse_local_action("hash notes.txt", workspace=self.ws)
        self.assertEqual(p.kind, "inspect")
        self.assertEqual(p.params["hash"], "true")

    def test_archive_default_dest(self):
        p = parse_local_action("zip docs", workspace=self.ws)
        self.assertEqual((p.kind, p.tool), ("archive", "fs_archive"))
        self.assertTrue(p.params["dst"].endswith("docs.zip"))

    def test_archive_explicit_dest(self):
        p = parse_local_action("archive docs to backup.zip",
                               workspace=self.ws)
        self.assertTrue(p.params["dst"].endswith("backup.zip"))

    def test_extract_default_dest(self):
        p = parse_local_action("extract bundle.zip", workspace=self.ws)
        self.assertEqual(p.kind, "extract")
        self.assertTrue(p.params["dst"].endswith("bundle"))

    def test_extract_explicit_dest(self):
        p = parse_local_action("unzip bundle.zip to out",
                               workspace=self.ws)
        self.assertTrue(p.params["dst"].endswith("out"))

    def test_pronoun_falls_through(self):
        self.assertIsNone(parse_local_action("zip it", workspace=self.ws))
        self.assertIsNone(parse_local_action(
            "unzip your lip", workspace=self.ws))

    def test_bare_verb_clarifies(self):
        p = parse_local_action("archive the folder", workspace=self.ws)
        self.assertEqual(p.kind, "archive")
        self.assertTrue(p.clarify)

    def test_outside_root_archive_gated(self):
        p = parse_local_action("zip docs to D:\\somewhere\\b.zip",
                               workspace=self.ws)
        self.assertTrue(p.outside_root)


class TestDiscoveryExecute(unittest.TestCase):
    def test_find_and_report(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        p = parse_local_action("find files named *.log", workspace=ws)
        out = execute_plan(p, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("report.log", out["text"])

    def test_find_no_match_truthful(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        p = parse_local_action("find files named *.nope", workspace=ws)
        out = execute_plan(p, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("No files matched", out["text"])

    def test_search_text_reports_lines(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        p = parse_local_action("search files for TODO", workspace=ws)
        out = execute_plan(p, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("TODO", out["text"])

    def test_inspect_file(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        p = parse_local_action("inspect notes.txt", workspace=ws)
        out = execute_plan(p, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("file", out["text"])
        self.assertIn("bytes", out["text"])

    def test_inspect_missing_truthful(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        p = parse_local_action("inspect ghost.bin", workspace=ws)
        out = execute_plan(p, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("doesn't exist", out["text"])

    def test_hash_reports_sha(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        p = parse_local_action("hash notes.txt", workspace=ws)
        out = execute_plan(p, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        self.assertIn("sha256", out["text"])

    def test_archive_roundtrip_verified(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        out = execute_plan(parse_local_action("zip docs", workspace=ws),
                           tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        arc = ws / "docs.zip"
        self.assertTrue(arc.is_file())
        with zipfile.ZipFile(arc) as zf:
            self.assertEqual(zf.namelist(), ["report.log"])

        # Now extract it elsewhere — verified on disk.
        out = execute_plan(
            parse_local_action("extract docs.zip to restored",
                               workspace=ws),
            tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        self.assertEqual((ws / "restored" / "report.log").read_text(),
                         "line one\nTODO fix this\nline three")

    def test_archive_refuses_self_containment(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        out = execute_plan(
            parse_local_action("zip docs to docs\\inner.zip",
                               workspace=ws),
            tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "failed")
        self.assertFalse((ws / "docs" / "inner.zip").exists())

    def test_extract_zip_slip_blocked(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        evil = ws / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape.txt", "pwned")
        out = execute_plan(
            parse_local_action("extract evil.zip to outdir",
                               workspace=ws),
            tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "failed")
        self.assertFalse((ws / "escape.txt").exists())
        self.assertFalse((ws.parent / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
