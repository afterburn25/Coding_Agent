import json
import tempfile
import unittest
from pathlib import Path

import sys

from localcodeagent.jobs import JobManager, normalize_state
from localcodeagent.permissions import KNOWN_PERMISSIONS, PROFILES, PermissionManager
from localcodeagent.processes import ManagedService, ProcessManager
from localcodeagent.tools.base import TOOL_CATEGORIES, ToolRegistry, ToolSpec
from localcodeagent.tools.filesystem import register_filesystem_tools
from localcodeagent.tools.plugins import PluginManifest, load_plugin_manifests


def _registry(**permissions):
    perms = {"filesystem.read": "allow", "filesystem.write": "allow"}
    perms.update(permissions)
    return ToolRegistry(perms)


class ToolManifestTests(unittest.TestCase):
    def test_builtin_manifest_metadata_applied(self):
        reg = _registry()
        register_filesystem_tools(reg, Path(tempfile.mkdtemp()))
        manifest = reg.manifest("read_file")
        self.assertEqual(manifest["category"], "coding")
        self.assertIn("read_file", manifest["capabilities"])
        self.assertEqual(manifest["permission"], "filesystem.read")
        self.assertEqual(manifest["permission_mode"], "allow")
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["source"], "builtin")
        self.assertIn("input_schema", manifest)

    def test_unknown_tool_category_defaults_to_utilities(self):
        reg = _registry()
        reg.register(ToolSpec("custom_thing", "x", {}, "filesystem.read", lambda a: "ok"))
        self.assertEqual(reg.manifest("custom_thing")["category"], "utilities")
        self.assertIn("utilities", TOOL_CATEGORIES)

    def test_find_by_capability(self):
        reg = _registry()
        register_filesystem_tools(reg, Path(tempfile.mkdtemp()))
        hits = reg.find_by_capability("workspace_read")
        self.assertIn("read_file", hits)
        self.assertNotIn("write_file", hits)

    def test_disabled_tool_is_blocked_and_hidden(self):
        reg = _registry()
        register_filesystem_tools(reg, Path(tempfile.mkdtemp()))
        reg.set_enabled("read_file", False)
        result = reg.execute("read_file", {"path": "x"})
        self.assertTrue(result.startswith("TOOL_DISABLED"))
        schema_names = {s["function"]["name"] for s in reg.schemas()}
        self.assertNotIn("read_file", schema_names)
        self.assertIn("write_file", schema_names)
        reg.set_enabled("read_file", True)
        self.assertIn("read_file", {s["function"]["name"] for s in reg.schemas()})

    def test_disabled_state_persists(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "tools_state.json"
            reg = _registry()
            reg.state_path = state
            register_filesystem_tools(reg, Path(td))
            reg.set_enabled("write_file", False)
            self.assertTrue(state.is_file())

            reg2 = _registry()
            reg2.state_path = state
            reg2._load_state()
            register_filesystem_tools(reg2, Path(td))
            self.assertFalse(reg2.is_enabled("write_file"))

    def test_usage_is_recorded(self):
        reg = _registry()
        register_filesystem_tools(reg, Path(tempfile.mkdtemp()))
        reg.execute("list_files", {"path": "."})
        self.assertEqual(reg.manifest("list_files")["use_count"], 1)


class PermissionManagerTests(unittest.TestCase):
    def test_default_mode_is_ask(self):
        mgr = PermissionManager({})
        self.assertEqual(mgr.effective("shell.execute"), "ask")

    def test_session_level_asks_once_then_allows(self):
        perms = {"filesystem.write": "session"}
        mgr = PermissionManager(perms)
        reg = ToolRegistry(mgr)
        with tempfile.TemporaryDirectory() as td:
            register_filesystem_tools(reg, Path(td))
            denied = reg.execute("write_file", {"path": "a.txt", "content": "x"})
            self.assertTrue(denied.startswith("APPROVAL_REQUIRED"))
            ok = reg.execute("write_file", {"path": "a.txt", "content": "x"}, approved=True)
            self.assertIn("WROTE", ok)
            again = reg.execute("write_file", {"path": "b.txt", "content": "y"})
            self.assertIn("WROTE", again)

    def test_set_level_validates_and_switches_to_custom(self):
        mgr = PermissionManager({}, profile="developer")
        with self.assertRaises(ValueError):
            mgr.set_level("shell.execute", "sometimes")
        mgr.set_level("shell.execute", "deny")
        self.assertEqual(mgr.effective("shell.execute"), "deny")
        self.assertEqual(mgr.profile, "custom")

    def test_profiles_apply_and_clear_session_grants(self):
        perms = {"browser.control": "session"}
        mgr = PermissionManager(perms)
        mgr.grant_session("browser.control")
        mgr.apply_profile("offline")
        self.assertEqual(mgr.effective("network.read"), "deny")
        self.assertEqual(mgr.effective("browser.control"), "deny")
        self.assertEqual(mgr.profile, "offline")
        self.assertEqual(mgr.summary()["session_grants"], [])

    def test_profiles_cover_known_permissions(self):
        for name, mapping in PROFILES.items():
            for key in KNOWN_PERMISSIONS:
                self.assertIn(key, mapping, f"{name} missing {key}")


class JobManagerTests(unittest.TestCase):
    def test_submit_update_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = JobManager(Path(td) / "jobs.json")
            job = mgr.submit("transcription", "Transcribe video.mp4")
            self.assertEqual(job.state, "queued")
            mgr.update(job.id, state="running", progress=0.4)
            self.assertIsNotNone(mgr.get(job.id).started_at)
            mgr.cancel(job.id)
            done = mgr.get(job.id)
            self.assertEqual(done.state, "cancelled")
            self.assertIsNotNone(done.finished_at)
            self.assertTrue((Path(td) / "jobs.json").is_file())

    def test_restart_marks_active_jobs_failed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.json"
            mgr = JobManager(path)
            job = mgr.submit("render", "Render scene")
            mgr.update(job.id, state="running")
            mgr2 = JobManager(path)
            revived = mgr2.get(job.id)
            self.assertEqual(revived.state, "failed")
            self.assertIn("restarted", revived.error)

    def test_aggregate_normalizes_task_states(self):
        mgr = JobManager()
        payload = {
            "recent": [
                {"id": "t1", "prompt": "fix bug", "status": "done", "created_at": 1, "updated_at": 2},
                {"id": "t2", "prompt": "big change", "status": "waiting_approval", "created_at": 3, "updated_at": 3},
            ]
        }
        jobs = mgr.aggregate(tasks_payload=payload)
        states = {j["metadata"]["task_id"]: j["state"] for j in jobs}
        self.assertEqual(states["t1"], "completed")
        self.assertEqual(states["t2"], "waiting_for_permission")

    def test_normalize_state(self):
        self.assertEqual(normalize_state("generating"), "running")
        self.assertEqual(normalize_state("finished"), "completed")
        self.assertEqual(normalize_state(""), "queued")


class PluginManifestTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, payload: dict) -> Path:
        path = directory / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_manifest_requires_id_and_name(self):
        with self.assertRaises(ValueError):
            PluginManifest.from_dict({"id": "x"})
        manifest = PluginManifest.from_dict({"id": "ffmpeg", "name": "FFmpeg"})
        self.assertEqual(manifest.category, "utilities")

    def test_manifest_tool_registers_and_invokes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mdir = root / "manifests"
            mdir.mkdir()
            self._write(mdir, "echo.json", {
                "id": "echo_tool",
                "name": "Echo Tool",
                "version": "1.2.3",
                "category": "utilities",
                "capabilities": ["echo"],
                "executables": [sys.executable],
                "permissions": ["shell.execute"],
                "invoke": {
                    "command": [sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:]))", "{args}"],
                    "input_schema": {"type": "object", "properties": {"args": {"type": "array"}}},
                    "timeout_seconds": 30,
                },
            })
            reg = _registry(**{"shell.execute": "allow"})
            result = load_plugin_manifests(mdir, reg, workspace=root)
            self.assertEqual(result["loaded"], ["echo_tool"])
            self.assertEqual(result["errors"], [])

            manifest = reg.manifest("echo_tool")
            self.assertEqual(manifest["source"], "manifest")
            self.assertEqual(manifest["version"], "1.2.3")
            self.assertEqual(manifest["install_status"], "installed")
            self.assertTrue(manifest["callable"])

            out = json.loads(reg.execute("echo_tool", {"args": ["hello", "nexus"]}))
            self.assertEqual(out["exit_code"], 0)
            self.assertIn("hello nexus", out["stdout"])

    def test_manifest_without_invoke_is_not_callable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mdir = root / "manifests"
            mdir.mkdir()
            self._write(mdir, "catalog_only.json", {
                "id": "blender",
                "name": "Blender",
                "category": "3d",
                "executables": ["definitely-not-a-real-exe-xyz"],
            })
            reg = _registry(**{"shell.execute": "allow"})
            result = load_plugin_manifests(mdir, reg)
            self.assertEqual(result["loaded"], ["blender"])
            manifest = reg.manifest("blender")
            self.assertEqual(manifest["category"], "3d")
            self.assertEqual(manifest["install_status"], "missing")
            self.assertFalse(manifest["callable"])
            self.assertNotIn("blender", {s["function"]["name"] for s in reg.schemas()})
            self.assertIn("ERROR", reg.execute("blender", {}))

    def test_invalid_manifest_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            mdir = Path(td)
            self._write(mdir, "bad.json", {"name": "no id"})
            result = load_plugin_manifests(mdir, _registry())
            self.assertEqual(result["loaded"], [])
            self.assertEqual(len(result["errors"]), 1)

    def test_missing_directory_is_clean(self):
        result = load_plugin_manifests(Path("does/not/exist"), _registry())
        self.assertEqual(result, {"loaded": [], "errors": []})


class ProcessManagerTests(unittest.TestCase):
    def test_register_describe_action(self):
        mgr = ProcessManager()
        calls = []
        mgr.register(ManagedService(
            id="svc:test",
            name="Test service",
            kind="internal",
            port=1234,
            describe=lambda: {"state": "running", "pid": 4321, "healthy": True, "started_at": None},
            start=lambda: calls.append("start") or {"state": "running"},
            stop=lambda: calls.append("stop") or {"state": "stopped"},
        ))
        rows = mgr.list()
        self.assertEqual(rows[0]["id"], "svc:test")
        self.assertEqual(rows[0]["state"], "running")
        self.assertTrue(rows[0]["can_restart"])  # synthesized from start+stop

        result = mgr.action("svc:test", "restart")
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["stop", "start"])

    def test_missing_service_raises(self):
        mgr = ProcessManager()
        with self.assertRaises(KeyError):
            mgr.action("nope", "start")
        mgr.register(ManagedService(id="svc:x", name="x", kind="internal"))
        with self.assertRaises(ValueError):
            mgr.action("svc:x", "start")


if __name__ == "__main__":
    unittest.main()
