"""Slash-command system — parser, registry, executor, orchestrator gate."""
from __future__ import annotations

import unittest

from localcodeagent.commands import (
    CommandExecutor, CommandRegistry, CommandResult, CommandSpec,
    parse_command, register_core_commands)


class ParserTests(unittest.TestCase):
    def test_leading_slash_parses(self):
        p = parse_command("/shutdown")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "shutdown")
        self.assertEqual(p.raw_args, "")

    def test_leading_whitespace_ok(self):
        p = parse_command("   /workers 4")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "workers")
        self.assertEqual(p.raw_args, "4")

    def test_free_text_args(self):
        p = parse_command("/research latest Python release")
        self.assertEqual(p.raw_args, "latest Python release")

    def test_mid_sentence_not_command(self):
        self.assertIsNone(parse_command("please /shutdown"))
        self.assertIsNone(parse_command("what does /shutdown do?"))
        self.assertIsNone(parse_command("I use /model auto sometimes"))

    def test_url_and_paths_not_commands(self):
        self.assertIsNone(parse_command("https://example.com"))
        self.assertIsNone(parse_command("/home/user/project"))
        self.assertIsNone(parse_command("/api/models"))
        self.assertIsNone(parse_command("C:/Users/after"))

    def test_punctuation_after_token_not_command(self):
        self.assertIsNone(parse_command("/shutdown."))
        self.assertIsNone(parse_command("/shutdown,now"))

    def test_empty_and_bare_slash(self):
        self.assertIsNone(parse_command(""))
        self.assertIsNone(parse_command("/"))
        self.assertIsNone(parse_command("// x"))
        self.assertIsNone(parse_command("/123abc"))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.reg = register_core_commands(CommandRegistry())

    def test_alias_resolution(self):
        self.assertEqual(self.reg.get("search").name, "research")
        self.assertEqual(self.reg.get("commands").name, "help")

    def test_suggest_unknown(self):
        hints = self.reg.suggest("shutdonw")
        self.assertIn("shutdown", hints)

    def test_complete_prefix(self):
        out = self.reg.complete("st")
        self.assertIn("status", out)
        self.assertIn("stop", out)

    def test_help_generated(self):
        text = self.reg.help_text()
        self.assertIn("/research", text)
        self.assertIn("/think", text)

    def test_describe(self):
        text = self.reg.describe("think")
        self.assertIn("Usage:", text)
        text = self.reg.describe("research")
        self.assertIn("Aliases:", text)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.reg = register_core_commands(CommandRegistry())
        self.audit = []
        self.env = {
            "registry": self.reg,
            "version": lambda: "0.23.0",
            "status": lambda: {"version": "0.23.0", "model": "m1"},
            "think_get": lambda conv: "auto",
            "think_set": lambda conv, mode: None,
            "run_research": lambda q, ctx: {"summary": "S", "evidence": {},
                                            "sources": []},
            "last_research": lambda conv: None,
            "format_sources": lambda s: "sources!",
            "model_info": lambda: {"active": "m1", "roles": {"fast": "m1"}},
            "stop_active": lambda ex: False,
            "desktop": True,
            "shutdown": lambda restart=False: self._shutdown_calls.append(restart),
        }
        self._shutdown_calls = []
        self.ex = CommandExecutor(
            self.reg, env=self.env, audit=self.audit.append)

    def _run(self, text):
        return self.ex.execute(parse_command(text), ctx={})

    def test_unknown_command_suggests(self):
        r = self._run("/shutdonw")
        self.assertFalse(r.ok)
        self.assertIn("Unknown command: /shutdonw", r.text)
        self.assertIn("/shutdown", r.text)

    def test_help_runs(self):
        r = self._run("/help")
        self.assertTrue(r.ok)
        self.assertIn("/research", r.text)

    def test_help_topic(self):
        r = self._run("/help think")
        self.assertTrue(r.ok)
        self.assertIn("Usage:", r.text)

    def test_status(self):
        r = self._run("/status")
        self.assertTrue(r.ok)
        self.assertIn("0.23.0", r.text)

    def test_think_set_and_show(self):
        r = self._run("/think deep")
        self.assertTrue(r.ok)
        self.assertIn("deep", r.text)
        bad = self._run("/think sideways")
        self.assertFalse(bad.ok)

    def test_research(self):
        r = self._run("/research latest python")
        self.assertTrue(r.ok)
        self.assertIn("S", r.text)

    def test_sources_empty(self):
        r = self._run("/sources")
        self.assertTrue(r.ok)
        self.assertIn("recent research", r.text)

    def test_confirm_gate(self):
        r = self._run("/shutdown")
        self.assertFalse(r.ok)
        self.assertIn("yes", r.text)
        r2 = self._run("/shutdown yes")
        self.assertTrue(r2.ok)
        self.assertEqual(self._shutdown_calls, [False])
        r3 = self._run("/restart yes")
        self.assertTrue(r3.ok)
        self.assertEqual(self._shutdown_calls, [False, True])

    def test_desktop_gate(self):
        self.env["desktop"] = False
        self.ex = CommandExecutor(
            self.reg, env=self.env, audit=self.audit.append)
        r = self._run("/shutdown yes")
        self.assertFalse(r.ok)
        self.assertIn("desktop", r.text)

    def test_audit_logged(self):
        self._run("/status")
        self._run("/bogus")
        self.assertEqual(len(self.audit), 2)
        self.assertEqual(self.audit[0]["matched"], "status")
        self.assertEqual(self.audit[1]["matched"], "")

    def test_action_id_delegates(self):
        class FakeActions:
            def execute(self, action_id, params, confirmed=False):
                return type("R", (), {
                    "ok": True, "message": "voice on", "detail": "",
                    "links": [], "verified": True})()
        reg = CommandRegistry()
        reg.register(CommandSpec("voice", action_id="voice.enable"))
        ex = CommandExecutor(reg, env={"actions": FakeActions()})
        r = ex.execute(parse_command("/voice"), ctx={})
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "voice on")
        self.assertTrue(r.data["verified"])


if __name__ == "__main__":
    unittest.main()
