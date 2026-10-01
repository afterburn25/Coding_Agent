"""Tests for the Settings > Permissions backend surface and enriched tool manifests."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from localcodeagent.permissions import AUDIT_LIMIT, PermissionManager, permission_info


class CreatorLevelTests(unittest.TestCase):
    def test_creator_level_blocks_until_verified(self):
        m = PermissionManager({"filesystem.write": "creator"})
        self.assertEqual(m.effective("filesystem.write"), "creator")
        m.creator_verified = lambda: True
        self.assertEqual(m.effective("filesystem.write"), "ask")

    def test_creator_fail_closed_without_verifier(self):
        m = PermissionManager({"x.y": "creator"})
        self.assertFalse(m.creator_ok())
        self.assertEqual(m.effective("x.y"), "creator")

    def test_creator_is_a_valid_level(self):
        m = PermissionManager({})
        self.assertEqual(m.set_level("browser.control", "creator"), "creator")
        self.assertEqual(m.level("browser.control"), "creator")

    def test_execute_creator_requires_unlocked_brain(self):
        from localcodeagent.tools.base import ToolRegistry, ToolSpec
        m = PermissionManager({"filesystem.write": "creator"})
        reg = ToolRegistry(m)
        reg.register(ToolSpec(name="write_file", description="d", parameters={}, permission="filesystem.write",
                              handler=lambda a: "ok"))
        # Not approved -> approval required; approved but brain locked -> creator required.
        self.assertIn("APPROVAL_REQUIRED", reg.execute("write_file", {}))
        self.assertIn("CREATOR_APPROVAL_REQUIRED", reg.execute("write_file", {}, approved=True))
        m.creator_verified = lambda: True
        self.assertIn("APPROVAL_REQUIRED", reg.execute("write_file", {}))  # still per-action ask
        self.assertEqual(reg.execute("write_file", {}, approved=True), "ok")


class AuditTests(unittest.TestCase):
    def _manager(self, td: str) -> PermissionManager:
        return PermissionManager({}, audit_path=Path(td) / "audit.jsonl")

    def test_mutations_are_audited_and_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._manager(td)
            m.set_level("git.push", "deny")
            m.apply_profile("offline")
            m.grant_session("network.read")
            m.set_autonomous(True)
            events = [e["event"] for e in m.audit_entries()]
            for expected in ("level_changed", "profile_changed", "session_granted", "autonomous_on"):
                self.assertIn(expected, events)
            # New manager over the same file reloads entries + last_used.
            m2 = self._manager(td)
            self.assertTrue(m2.audit_entries())
            self.assertIsNotNone(m2.last_used("git.push"))

    def test_audit_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._manager(td)
            for i in range(AUDIT_LIMIT + 50):
                m.record_event("denied", "k.x")
            self.assertLessEqual(len(m.audit_entries(limit=AUDIT_LIMIT + 100)), AUDIT_LIMIT)

    def test_audit_never_stores_large_or_secret_payloads(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._manager(td)
            m.record_event("denied", "k", "x" * 1000)
            entry = m.audit_entries()[0]
            self.assertLessEqual(len(entry["detail"]), 160)
            self.assertEqual(set(entry), {"ts", "event", "permission", "detail"})

    def test_execute_denials_and_approvals_are_audited(self):
        from localcodeagent.tools.base import ToolRegistry, ToolSpec
        with tempfile.TemporaryDirectory() as td:
            m = PermissionManager({"filesystem.write": "deny"},
                                  audit_path=Path(td) / "audit.jsonl")
            reg = ToolRegistry(m)
            reg.register(ToolSpec(name="write_file", description="d", parameters={},
                                  permission="filesystem.write", handler=lambda a: "ok"))
            reg.execute("write_file", {})
            m.set_level("filesystem.write", "ask")
            reg.execute("write_file", {})  # -> approval_requested
            events = [e["event"] for e in m.audit_entries()]
            self.assertIn("denied", events)
            self.assertIn("approval_requested", events)

    def test_record_use_tracks_last_used_without_audit_write(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._manager(td)
            m.record_use("shell.execute")
            self.assertIsNotNone(m.last_used("shell.execute"))
            self.assertFalse((Path(td) / "audit.jsonl").exists())


class ScopeTests(unittest.TestCase):
    def test_domain_block_and_allow_lists(self):
        m = PermissionManager({}, scopes={"network.read": {"blocked_domains": ["bad.example"]}})
        self.assertIsNotNone(m.check_url("network.read", "https://sub.bad.example/x"))
        self.assertIsNone(m.check_url("network.read", "https://fine.example/"))
        m.set_scope("network.read", {"allowed_domains": ["ok.example"]})
        self.assertIsNotNone(m.check_url("network.read", "https://other.example/"))
        self.assertIsNone(m.check_url("network.read", "https://api.ok.example/"))

    def test_set_scope_validates(self):
        m = PermissionManager({})
        with self.assertRaises(ValueError):
            m.set_scope("", {})
        with self.assertRaises(ValueError):
            m.set_scope("k", {"allowed_domains": "not-a-list"})
        m.set_scope("k", {"workspace_only": True})
        self.assertEqual(m.scope("k"), {"workspace_only": True})

    def test_execute_enforces_domain_scope_on_url_args(self):
        from localcodeagent.tools.base import ToolRegistry, ToolSpec
        m = PermissionManager({"network.read": "allow"},
                              scopes={"network.read": {"blocked_domains": ["blocked.example"]}})
        reg = ToolRegistry(m)
        reg.register(ToolSpec(name="fetch_url", description="d", parameters={},
                              permission="network.read", handler=lambda a: "fetched"))
        self.assertIn("SCOPE_DENIED", reg.execute("fetch_url", {"url": "https://blocked.example/a"}))
        self.assertEqual(reg.execute("fetch_url", {"url": "https://fine.example/a"}), "fetched")


class SummaryTests(unittest.TestCase):
    def test_summary_exposes_info_categories_scopes_last_used(self):
        m = PermissionManager({"network.read": "allow"})
        s = m.summary()
        for key in ("info", "categories", "scopes", "last_used"):
            self.assertIn(key, s)
        self.assertIn("creator", s["levels"])
        info = s["info"]["browser.control"]
        self.assertEqual(info["category"], "Browser Automation")
        cats = {c["category"] for c in s["categories"]}
        self.assertIn("Filesystem", cats)
        self.assertIn("Tool Installation", cats)

    def test_unknown_keys_get_synthesized_info(self):
        info = permission_info("custom_tool.special_action")
        self.assertIn(info["category"], {"Other", "Tool Installation"})
        self.assertTrue(info["label"])


class ManifestEnrichmentTests(unittest.TestCase):
    def _registry_with_archive(self, td: str):
        from localcodeagent.tools.base import ToolRegistry
        root = Path(td) / "install"
        root.mkdir()
        payload = root / "payload_dir"
        payload.mkdir()
        (payload / "main.py").write_text("x" * 100)
        reg = ToolRegistry({})
        reg.install_root = root
        from localcodeagent.tools.base import ToolSpec
        spec = ToolSpec(name="demo", description="d", parameters={}, permission="filesystem.read",
                        handler=lambda a: "ok", tool_id="demo", version="1.2")
        reg.register(spec)
        reg._plugin_meta["demo"] = {
            "install": {"method": "archive", "dest": "."},
            "detect_files": ["payload_dir/main.py"],
            "process": "demo-svc",
            "dependencies": ["python"],
            "executables": [],
        }
        spec.install_status = "installed"
        return reg

    def test_manifest_reports_path_size_process_deps_latest(self):
        with tempfile.TemporaryDirectory() as td:
            reg = self._registry_with_archive(td)
            m = reg.manifest("demo")
            self.assertTrue(m["install_path"].endswith("payload_dir"))
            self.assertGreaterEqual(m["install_size_bytes"], 100)
            self.assertEqual(m["latest_version"], "1.2")
            self.assertEqual(m["process_id"], "demo-svc")
            self.assertEqual(m["dependencies"], ["python"])
            self.assertIn("health", m)

    def test_health_result_cached_into_manifest(self):
        from localcodeagent.tools.base import ToolRegistry, ToolSpec
        reg = ToolRegistry({})
        reg.register(ToolSpec(name="demo", description="d", parameters={}, permission="filesystem.read",
                              handler=lambda a: "ok", health_check=lambda: {"ok": True, "status": "healthy", "detail": "fine"}))
        before = reg.manifest("demo")["health"]
        self.assertEqual(before, {})  # not yet checked
        reg.health("demo")
        after = reg.manifest("demo")["health"]
        self.assertEqual(after["result"]["status"], "healthy")
        self.assertTrue(after["at"])


class PermissionApiTests(unittest.TestCase):
    """Endpoint-level coverage via a real HTTP server instance."""

    @classmethod
    def setUpClass(cls):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory()
        td = Path(cls._td.name)
        cfg = AgentConfig(
            models=[ModelProfile(id="fake", endpoint="http://127.0.0.1:1/v1", model="m",
                                 roles=["utility"], runtime="external")],
            process_watchdog=False, research_enabled=False)
        cls.server, cls.state = create_server(cfg, td, "127.0.0.1", 0, td / "web", td / ".runtime")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.addClassCleanup(lambda: (cls.server.shutdown(), cls.server.server_close(), stop_state(cls.state)))

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def _post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_permissions_summary_shape(self):
        data = self._get("/api/permissions")
        for key in ("profile", "available_profiles", "levels", "permissions",
                    "session_grants", "info", "categories", "scopes", "last_used"):
            self.assertIn(key, data)

    def test_level_change_and_audit_endpoint(self):
        out = self._post("/api/permissions/level", {"permission": "browser.control", "level": "session"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["level"], "session")
        audit = self._get("/api/permissions/audit")
        self.assertTrue(any(e["event"] == "level_changed" and e["permission"] == "browser.control"
                            for e in audit["entries"]))
        # restore
        self._post("/api/permissions/level", {"permission": "browser.control", "level": "ask"})

    def test_scope_endpoint_persists(self):
        out = self._post("/api/permissions/scope",
                         {"permission": "network.read", "scope": {"blocked_domains": ["t.example"]}})
        self.assertTrue(out["ok"])
        self.assertIn("t.example", out["scope"]["blocked_domains"])
        self.assertIn("permission_scopes",
                      json.loads((self.state.config_path).read_text()))
        self._post("/api/permissions/scope", {"permission": "network.read", "scope": {}})

    def test_process_log_unknown_service_404(self):
        try:
            self._get("/api/processes/log?id=nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_tools_payload_includes_partials_and_enriched_fields(self):
        data = self._get("/api/tools")
        self.assertIn("partials", data)
        tool = data["tools"][0]
        for key in ("install_path", "install_size_bytes", "latest_version",
                    "installed_at", "process_id", "dependencies", "health", "mcp_server"):
            self.assertIn(key, tool)

    def test_creator_level_via_api_gates_install(self):
        self._post("/api/permissions/level", {"permission": "packages.install", "level": "creator"})
        try:
            # needs_approval with needs_creator flag
            out = self._post("/api/tools/install", {"tool": "comfyui"})
            self.assertTrue(out.get("needs_approval"))
            self.assertTrue(out.get("needs_creator"))
            # approve=True but brain locked -> needs_creator error
            out = self._post("/api/tools/install", {"tool": "comfyui", "approve": True})
            self.assertTrue(out.get("needs_creator"))
        finally:
            self._post("/api/permissions/level", {"permission": "packages.install", "level": "ask"})


if __name__ == "__main__":
    unittest.main()
