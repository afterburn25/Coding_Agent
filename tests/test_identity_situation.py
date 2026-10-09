"""Phase A: Identity Manager + Capability Truth Graph + Situation Model.

These cover the milestone's contract:

- Identity: credentials live only in the vault (identity.json carries
  key *names*), accounts are 'active' only after a verified login probe,
  human-only challenges park the workflow, account creation is a
  distinct permission.
- Capability truth: dependency edges explain degradation precisely,
  new states map to honest dispositions, selftests are bounded and
  rate-limited.
- Situation: a single live snapshot + compact natural-language answer
  for "what's going on?".
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class FakeVault:
    """Vault-shaped stand-in — stores values, exposes names."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, name: str, value: str, *, description: str = "") -> dict:
        self.store[name] = value
        return {"name": name}

    def get(self, name: str):
        return self.store.get(name)

    def has(self, name: str) -> bool:
        return name in self.store

    def delete(self, name: str) -> bool:
        return self.store.pop(name, None) is not None


def _manager(td: str, **kw):
    from localcodeagent.identity_mgr import IdentityManager
    kw.setdefault("vault", FakeVault())
    return IdentityManager(Path(td), **kw)


class IdentityManagerTests(unittest.TestCase):

    def test_vault_isolation(self):
        """Credential VALUES never land in identity.json — only the
        vault key name (credential_ref) is recorded."""
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td)
            out = mgr.store_credential("github", "github_token",
                                       "ghp_SUPERSECRET123")
            self.assertTrue(out["ok"])
            self.assertEqual(out["credential_ref"], "github_token")
            self.assertEqual(mgr._vault.get("github_token"),
                             "ghp_SUPERSECRET123")
            raw = (Path(td) / "identity.json").read_text()
            self.assertNotIn("ghp_SUPERSECRET123", raw)
            self.assertIn("github_token", raw)  # the *name* only

    def test_active_requires_verified_login(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td)
            # Claiming 'active' without proof records honestly as unverified.
            out = mgr.record_account("bluesky", handle="nexus.bsky.social",
                                     state="active")
            self.assertTrue(out["ok"])
            svc = mgr._find("bluesky")[1]
            self.assertNotEqual(svc["state"], "active")
            # set_state("active") is refused outright.
            out = mgr.set_state("bluesky", "active")
            self.assertFalse(out["ok"])
            # verify_login with a passing probe → active.
            out = mgr.verify_login("bluesky", probe=lambda: True)
            self.assertTrue(out["ok"])
            self.assertEqual(mgr._find("bluesky")[1]["state"], "active")

    def test_failed_probe_stays_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td)
            mgr.record_account("x", handle="nexus", state="awaiting_verification")
            out = mgr.verify_login("x", probe=lambda: False)
            self.assertFalse(out["ok"])
            self.assertNotEqual(mgr._find("x")[1]["state"], "active")

    def test_creation_permission_gate(self):
        """identity.account_create is its own authority — denied/ask
        verdicts block the workflow before any state is written."""
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td, permission_check=lambda p: "ask")
            out = mgr.begin_account_creation("bluesky")
            self.assertFalse(out["ok"])
            self.assertTrue(out["needs_approval"])
            self.assertEqual(out["permission"], "identity.account_create")
            self.assertEqual(mgr._find("bluesky")[1], {})

    def test_human_challenge_parks_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td)
            mgr.begin_account_creation("bluesky")
            out = mgr.pause_for_human("bluesky", "captcha")
            self.assertTrue(out["ok"])
            rec = mgr._find("bluesky")[1]
            self.assertEqual(rec["state"], "awaiting_human")
            self.assertEqual(rec["workflow"]["human_challenge"], "captcha")
            out = mgr.resume_creation("bluesky")
            self.assertTrue(out["ok"])
            self.assertEqual(rec["state"], "verifying_login")

    def test_recovery_owner_is_user(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td)
            st = mgr.status()
            self.assertEqual(st["recovery_owner"], "user")

    def test_live_merge_wins_over_stale_row(self):
        """A durable row saying 'awaiting_verification' must yield to a
        live probe saying the account is connected."""
        class FakeGitHub:
            def status(self, **kw):
                return {"state": "connected", "login": "nexus-core",
                        "scopes": ["repo"], "source": "vault"}
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td, github_account=FakeGitHub())
            mgr.record_account("github", state="awaiting_verification")
            st = mgr.status()
            gh = next(a for a in st["accounts"] if a["service"] == "github")
            self.assertEqual(gh["state"], "active")
            self.assertEqual(gh["handle"], "nexus-core")

    def test_remove_account_explicit_credential_delete(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td)
            mgr.store_credential("svc", "svc_key", "secret-value")
            out = mgr.remove_account("svc")  # default: keep credential
            self.assertTrue(out["ok"])
            self.assertTrue(mgr._vault.has("svc_key"))
            mgr.store_credential("svc", "svc_key", "secret-value")
            mgr.remove_account("svc", delete_credential=True)
            self.assertFalse(mgr._vault.has("svc_key"))

    def test_audit_trail(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _manager(td)
            mgr.begin_account_creation("x")
            mgr.pause_for_human("x", "phone_verification")
            audit = mgr.audit()
            actions = [a["action"] for a in audit]
            self.assertIn("creation_started", actions)
            self.assertIn("awaiting_human", actions)


class CapabilityGraphTests(unittest.TestCase):

    def _registry(self, env=None, specs=None):
        from localcodeagent.capabilities import CapabilityRegistry
        return CapabilityRegistry(env or {}, specs=specs)

    def test_graph_nodes_and_edges(self):
        reg = self._registry()
        g = reg.graph()
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("github", ids)
        self.assertIn("web_access", ids)
        edges = {(e["from"], e["to"]) for e in g["edges"]}
        self.assertIn(("github", "web_access"), edges)
        self.assertIn(("moltbook", "web_access"), edges)

    def test_blockers_walk_dependencies(self):
        """Offline policy → web_access down → github degraded — the
        blocker chain must name the real root, not just 'github down'."""
        reg = self._registry({"network_offline": lambda: True})
        blk = reg.blockers("github")
        caps = {b["capability"] for b in blk}
        self.assertIn("web_access", caps)
        web = next(b for b in blk if b["capability"] == "web_access")
        self.assertEqual(web["state"], "unavailable")
        self.assertIn("offline", web["detail"])

    def test_new_states_dispositions(self):
        from localcodeagent.capabilities import _STATE_DISPOSITION
        self.assertEqual(_STATE_DISPOSITION["permission_required"],
                         "authorization_required")
        self.assertEqual(_STATE_DISPOSITION["disconnected"],
                         "explanation_only")
        self.assertEqual(_STATE_DISPOSITION["unsupported"],
                         "explanation_only")
        self.assertEqual(_STATE_DISPOSITION["temporarily_unavailable"],
                         "explanation_only")

    def test_selftest_verify_and_ratelimit(self):
        from localcodeagent.capabilities import (CapabilityRegistry,
                                                 CapabilitySpec,
                                                 CapabilityReport)
        calls = []

        def probe(env, r: CapabilityReport):
            r.state, r.detail = "available", "idle"

        def selftest(env):
            calls.append(1)
            return True, "navigated a test page"

        reg = CapabilityRegistry({}, specs=[
            CapabilitySpec("browser_preview", "Browser", probe,
                           selftest=selftest)])
        out = reg.run_selftest("browser_preview")
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "verified")
        self.assertEqual(reg.evaluate_one("browser_preview").state,
                         "verified")
        # Second call inside the window is rate-limited, not re-run.
        out = reg.run_selftest("browser_preview")
        self.assertFalse(out["ok"])
        self.assertIn("rate limited", out["error"])
        self.assertEqual(len(calls), 1)

    def test_selftest_failure_softens_not_breaks(self):
        from localcodeagent.capabilities import (CapabilityRegistry,
                                                 CapabilitySpec,
                                                 CapabilityReport)

        def probe(env, r: CapabilityReport):
            r.state = "available"

        reg = CapabilityRegistry({}, specs=[
            CapabilitySpec("x", "X", probe,
                           selftest=lambda env: (False, "timeout"))])
        out = reg.run_selftest("x")
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "temporarily_unavailable")

    def test_stale_self_description_still_cannot_override(self):
        """denied() must keep flagging 'I don't have a browser' while
        the probe says ready."""
        env = {"browser_state": lambda: "ready"}
        reg = self._registry(env)
        hits = reg.denied("I don't have a browser to open that.")
        self.assertTrue(any(h.id == "browser_preview" for h in hits))


class SituationModelTests(unittest.TestCase):

    def test_snapshot_sections(self):
        from localcodeagent.nexus_state import build_situation

        class Missions:
            def list(self):
                return [{"id": "m1", "title": "Fix installer",
                         "status": "active",
                         "workstreams": [{"status": "active"},
                                         {"status": "paused"}]},
                        {"id": "m2", "title": "Old", "status": "paused"}]

        class Jobs:
            def list_jobs(self):
                return [{"id": "j1", "title": "install invokeai",
                         "state": "running", "kind": "install"},
                        {"id": "j2", "title": "download model",
                         "state": "running", "kind": "download"}]

        env = {
            "conversation": lambda: {"active": True,
                                     "topic": "image models",
                                     "goal": "install RealVisXL"},
            "missions": Missions(),
            "jobs": Jobs(),
            "models_resident": lambda: {"resident": "qwen3-14b"},
            "pending_approvals": lambda: ["install InvokeAI"],
            "waiting_consults": lambda: [{"id": "c1"}],
            "recent_failures": lambda: ["fetch failed"],
            "current_project": lambda: "chat-nexus",
        }
        sit = build_situation(env=env, base={"state": "focused"})
        self.assertTrue(sit["conversation"]["active"])
        self.assertEqual(len(sit["missions"]["active"]), 1)
        self.assertEqual(sit["missions"]["workstreams_active"], 1)
        self.assertEqual(len(sit["missions"]["paused"]), 1)
        self.assertEqual(len(sit["activity"]["installs"]), 1)
        self.assertEqual(len(sit["activity"]["downloads"]), 1)
        self.assertEqual(sit["models"]["resident"], "qwen3-14b")
        self.assertEqual(sit["approvals"]["count"], 1)
        self.assertEqual(sit["social"]["waiting_consults"], 1)
        self.assertEqual(sit["project"], "chat-nexus")

    def test_situation_text_compact(self):
        from localcodeagent.nexus_state import (build_situation,
                                                situation_text)
        env = {
            "conversation": lambda: {"active": True, "goal": "fix bug"},
            "missions": None,
            "pending_approvals": lambda: ["one", "two"],
            "models_resident": lambda: {"resident": "qwen3-14b"},
        }
        text = situation_text(build_situation(env=env, base={}))
        self.assertIn("fix bug", text)
        self.assertIn("2 approvals", text)
        self.assertIn("qwen3-14b", text)
        self.assertNotIn("{", text)  # never raw JSON

    def test_situation_text_idle(self):
        from localcodeagent.nexus_state import (build_situation,
                                                situation_text)
        text = situation_text(build_situation(env={}, base={}))
        self.assertIn("nothing", text.lower())

    def test_missing_sources_degrade_sections_not_snapshot(self):
        """Every env callable raising must not take the whole model down."""
        from localcodeagent.nexus_state import build_situation
        env = {"conversation": lambda: (_ for _ in ()).throw(
                   RuntimeError("boom")),
               "pending_approvals": lambda: (_ for _ in ()).throw(
                   ValueError("x"))}
        sit = build_situation(env=env, base={"state": "idle"})
        self.assertIn("approvals", sit)
        self.assertIn("conversation", sit)


if __name__ == "__main__":
    unittest.main()
