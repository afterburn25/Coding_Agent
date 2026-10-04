"""Capability Registry — probed honest states for what Nexus can do."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.capabilities import (
    CapabilityRegistry, CapabilityReport, CapabilitySpec,
    DISPOSITIONS, STATES,
)


def _env(**overrides):
    env = {
        "tool_manifest": lambda name: None,
        "command": lambda c: None,
        "workspace": lambda: None,
        "workspace_writable": lambda: False,
        "github_enabled": lambda: False,
        "github_authorized": lambda: False,
        "image_enabled": lambda: False,
        "image_backend_state": lambda: "",
        "stt_enabled": lambda: False,
        "stt_engine_state": lambda: "",
        "voice_enabled": lambda: False,
        "voice_present": lambda: False,
        "voice_ready": lambda: False,
        "llm_ready": lambda: None,
    }
    env.update(overrides)
    return env


def _tool(name: str, *, enabled=True, install="installed", callable=True):
    return {"name": name, "enabled": enabled,
            "install_status": install, "callable": callable}


class CapabilityProbeTests(unittest.TestCase):

    def test_filesystem_unavailable_without_workspace(self):
        reg = CapabilityRegistry(_env())
        r = reg.evaluate_one("filesystem")
        self.assertEqual(r.state, "unavailable")
        self.assertEqual(r.disposition, "explanation_only")

    def test_filesystem_verified_when_writable(self):
        with tempfile.TemporaryDirectory() as td:
            reg = CapabilityRegistry(_env(
                workspace=lambda: Path(td),
                workspace_writable=lambda: True))
            r = reg.evaluate_one("filesystem")
            self.assertEqual(r.state, "verified")
            self.assertEqual(r.disposition, "can_do_now")

    def test_git_setup_required_without_binary(self):
        reg = CapabilityRegistry(_env())
        r = reg.evaluate_one("git")
        self.assertEqual(r.state, "setup_required")
        self.assertIn("git_binary", r.requirements_unmet)

    def test_git_verified_with_binary_and_tool(self):
        reg = CapabilityRegistry(_env(
            command=lambda c: "/usr/bin/git" if c == "git" else None,
            tool_manifest=lambda n: _tool(n) if n == "git_status" else None))
        r = reg.evaluate_one("git")
        self.assertEqual(r.state, "verified")

    def test_github_unauthorized_without_token(self):
        reg = CapabilityRegistry(_env(github_enabled=lambda: True))
        r = reg.evaluate_one("github")
        self.assertEqual(r.state, "unauthorized")
        self.assertEqual(r.disposition, "authorization_required")

    def test_github_verified_with_token(self):
        reg = CapabilityRegistry(_env(
            github_enabled=lambda: True,
            github_authorized=lambda: True))
        self.assertEqual(reg.evaluate_one("github").state, "verified")

    def test_github_disabled_is_setup_required(self):
        reg = CapabilityRegistry(_env())
        self.assertEqual(reg.evaluate_one("github").state, "setup_required")

    def test_terminal_missing_tool(self):
        reg = CapabilityRegistry(_env())
        self.assertEqual(reg.evaluate_one("terminal").state, "unavailable")

    def test_terminal_verified_with_tool(self):
        reg = CapabilityRegistry(_env(
            tool_manifest=lambda n: _tool(n) if n == "run_terminal" else None))
        self.assertEqual(reg.evaluate_one("terminal").state, "verified")

    def test_disabled_tool_does_not_count(self):
        reg = CapabilityRegistry(_env(
            tool_manifest=lambda n: _tool(n, enabled=False)
            if n == "run_terminal" else None))
        self.assertEqual(reg.evaluate_one("terminal").state, "unavailable")

    def test_missing_tool_does_not_count(self):
        reg = CapabilityRegistry(_env(
            tool_manifest=lambda n: _tool(n, install="missing")
            if n == "run_terminal" else None))
        self.assertEqual(reg.evaluate_one("terminal").state, "unavailable")

    def test_tts_verified_when_voice_ready(self):
        reg = CapabilityRegistry(_env(
            voice_enabled=lambda: True, voice_ready=lambda: True))
        self.assertEqual(reg.evaluate_one("tts").state, "verified")

    def test_tts_setup_required_without_engine(self):
        reg = CapabilityRegistry(_env(voice_enabled=lambda: True))
        self.assertEqual(reg.evaluate_one("tts").state, "setup_required")

    def test_stt_available_when_lazy(self):
        reg = CapabilityRegistry(_env(stt_enabled=lambda: True))
        self.assertEqual(reg.evaluate_one("stt").state, "available")

    def test_image_backend_crashed_is_broken(self):
        reg = CapabilityRegistry(_env(
            image_enabled=lambda: True,
            image_backend_state=lambda: "crashed"))
        self.assertEqual(reg.evaluate_one("image_generation").state, "broken")

    def test_deployment_experimental_with_docker(self):
        reg = CapabilityRegistry(_env(
            command=lambda c: "/usr/bin/docker" if c == "docker" else None))
        r = reg.evaluate_one("deployment")
        self.assertEqual(r.state, "experimental")
        self.assertEqual(r.disposition, "explanation_only")

    def test_probe_exception_is_broken_not_fatal(self):
        def boom(env, r):
            raise RuntimeError("kaboom")
        reg = CapabilityRegistry(_env(), specs=[
            CapabilitySpec("x", "X", boom)])
        self.assertEqual(reg.evaluate_one("x").state, "broken")

    def test_unknown_capability(self):
        reg = CapabilityRegistry(_env())
        self.assertEqual(
            reg.evaluate_one("nonexistent").state, "unavailable")


class CapabilityHonestyTests(unittest.TestCase):

    def test_prompt_note_empty_when_all_clear(self):
        with tempfile.TemporaryDirectory() as td:
            reg = CapabilityRegistry(_env(
                workspace=lambda: Path(td),
                workspace_writable=lambda: True))
            # Only filesystem/code_editing specs — force a registry with a
            # single clear capability.
            from localcodeagent.capabilities import _probe_filesystem
            reg = CapabilityRegistry(
                _env(workspace=lambda: Path(td),
                     workspace_writable=lambda: True),
                specs=[CapabilitySpec("filesystem", "fs", _probe_filesystem)])
            self.assertEqual(reg.prompt_note(), "")

    def test_prompt_note_lists_blocked(self):
        reg = CapabilityRegistry(_env())
        note = reg.prompt_note()
        self.assertIn("GitHub", note)
        self.assertIn("unavailable", note.lower())
        self.assertIn("Do not promise", note)

    def test_contradicted_flags_github_claim(self):
        reg = CapabilityRegistry(_env(github_enabled=lambda: True))
        hits = reg.contradicted("I pushed the branch to GitHub.")
        self.assertTrue(any(r.id == "github" for r in hits))

    def test_contradicted_ignores_unrelated_text(self):
        reg = CapabilityRegistry(_env(github_enabled=lambda: True))
        hits = reg.contradicted("Here is how a queue works.")
        self.assertFalse(any(r.id == "github" for r in hits))

    def test_contradicted_ignores_healthy_capability(self):
        reg = CapabilityRegistry(_env(
            command=lambda c: "/usr/bin/git" if c == "git" else None,
            tool_manifest=lambda n: _tool(n) if n == "git_status" else None))
        hits = reg.contradicted("I committed the staged changes with git.")
        self.assertFalse(any(r.id == "git" for r in hits))

    def test_cache_reuse_and_invalidation(self):
        calls = {"n": 0}

        def counting_probe(env, r):
            calls["n"] += 1
            r.state = "verified"

        reg = CapabilityRegistry(_env(), specs=[
            CapabilitySpec("c", "C", counting_probe)])
        reg.evaluate_one("c")
        reg.evaluate_one("c")
        self.assertEqual(calls["n"], 1)
        reg.invalidate("c")
        reg.evaluate_one("c")
        self.assertEqual(calls["n"], 2)
        reg.evaluate_one("c", force=True)
        self.assertEqual(calls["n"], 3)

    def test_summary_shape(self):
        reg = CapabilityRegistry(_env())
        s = reg.summary()
        self.assertIn("capabilities", s)
        self.assertIn("blocked", s)
        self.assertEqual(sorted(STATES), sorted(s["states"]))
        self.assertEqual(sorted(DISPOSITIONS), sorted(s["dispositions"]))
        for r in s["capabilities"].values():
            self.assertIn(r["state"], STATES)
            self.assertIn(r["disposition"], DISPOSITIONS)


if __name__ == "__main__":
    unittest.main()
