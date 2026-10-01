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
from localcodeagent.tools.buildsys import detect_build_systems, register_build_tools
from localcodeagent.tools.plugins import PluginManifest, load_plugin_manifests
from localcodeagent.tools.search import find_ripgrep, register_search_tools
from localcodeagent.tools.terminal import TerminalTracker, register_terminal_tools, resolve_shell
from localcodeagent.tool_router import ToolRouter
from localcodeagent.mcp import MCPManager, MCPServerConfig, load_mcp_configs


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


class TerminalToolTests(unittest.TestCase):
    def _reg(self, ws: Path, jobs=None):
        reg = ToolRegistry({"shell.execute": "allow"})
        tracker = register_terminal_tools(reg, ws, jobs=jobs, log_dir=ws / ".logs")
        return reg, tracker

    def test_resolve_shell_auto_and_invalid(self):
        shell_id, argv = resolve_shell("auto")
        self.assertIn(shell_id, {"powershell", "cmd", "bash"})
        self.assertTrue(argv)
        with self.assertRaises(ValueError):
            resolve_shell("fakeshell")

    def test_terminal_run_returns_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            reg, _ = self._reg(ws)
            out = reg.execute("terminal_run", {"command": "echo hello"})
            data = json.loads(out)
            self.assertEqual(data["exit_code"], 0)
            self.assertIn("hello", data["stdout"])
            self.assertIn("shell", data)

    def test_terminal_run_rejects_cwd_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            reg, _ = self._reg(ws)
            out = reg.execute("terminal_run", {"command": "echo x", "cwd": ".."})
            self.assertTrue(out.startswith("ERROR"))

    def test_terminal_background_tracked_and_killable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            jobs = JobManager(ws / "jobs.json")
            reg, tracker = self._reg(ws, jobs=jobs)
            shell_id, _ = resolve_shell("auto")
            command = "ping -n 30 127.0.0.1" if shell_id in {"powershell", "cmd"} else "sleep 30"
            out = json.loads(reg.execute("terminal_run", {"command": command, "background": True, "shell": shell_id}))
            self.assertTrue(out["ok"])
            rows = tracker.list()
            self.assertEqual(rows[0]["state"], "running")
            killed = json.loads(reg.execute("terminal_kill", {"job_id": out["job_id"]}))
            self.assertTrue(killed["ok"])
            self.assertEqual(jobs.get(out["job_id"]).state, "cancelled")

    def test_permission_denied_blocks_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ToolRegistry({"shell.execute": "deny"})
            register_terminal_tools(reg, Path(tmp), log_dir=Path(tmp) / ".logs")
            out = reg.execute("terminal_run", {"command": "echo nope"})
            self.assertTrue(out.startswith("PERMISSION_DENIED"))


class SearchToolTests(unittest.TestCase):
    def _ws(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "app.py").write_text("def main():\n    raise ValueError('boom')\n", encoding="utf-8")
        (tmp / "lib").mkdir()
        (tmp / "lib" / "helper.py").write_text("VALUE_ERROR_MSG = 'boom'\n", encoding="utf-8")
        return tmp

    def _reg(self, ws):
        reg = ToolRegistry({"filesystem.read": "allow"})
        register_search_tools(reg, ws)
        return reg

    def test_search_code_finds_matches(self):
        ws = self._ws()
        out = json.loads(self._reg(ws).execute("search_code", {"query": "boom"}))
        self.assertGreaterEqual(out["count"], 2)
        self.assertIn(out["engine"], {"ripgrep", "python"})
        paths = {m["path"].replace("\\", "/") for m in out["matches"]}
        self.assertTrue(any(p.endswith("app.py") for p in paths))

    def test_search_filename(self):
        ws = self._ws()
        out = json.loads(self._reg(ws).execute("search_filename", {"pattern": "*.py"}))
        self.assertEqual(out["count"], 2)

    def test_search_error_extracts_terms(self):
        ws = self._ws()
        out = json.loads(self._reg(ws).execute(
            "search_error", {"error_text": "Traceback...\nValueError: boom\n"}))
        self.assertTrue(out["terms"])
        self.assertGreaterEqual(out["count"], 1)


class BuildSystemToolTests(unittest.TestCase):
    def test_detect_python_and_npm(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "package.json").write_text("{}", encoding="utf-8")
        systems = detect_build_systems(tmp)
        ids = [s["id"] for s in systems]
        self.assertIn("npm", ids)
        (tmp / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        ids = [s["id"] for s in detect_build_systems(tmp)]
        self.assertIn("python", ids)
        # npm should outrank python in auto ordering (listed first)
        self.assertLess(ids.index("npm"), ids.index("python"))

    def test_detect_tool_and_unknown_system_error(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
        reg = ToolRegistry({"filesystem.read": "allow", "shell.execute": "allow"})
        register_build_tools(reg, tmp)
        detected = json.loads(reg.execute("detect_build_system", {}))
        self.assertEqual(detected["primary"], "make")
        out = reg.execute("build_project", {"system": "cargo"})
        self.assertTrue(out.startswith("ERROR"))

    def test_no_build_system_reports_error(self):
        tmp = Path(tempfile.mkdtemp())
        reg = ToolRegistry({"filesystem.read": "allow", "shell.execute": "allow"})
        register_build_tools(reg, tmp)
        self.assertTrue(reg.execute("build_project", {}).startswith("ERROR"))


class ToolRouterTests(unittest.TestCase):
    def _reg(self):
        reg = ToolRegistry({"filesystem.read": "allow", "shell.execute": "allow", "network.read": "allow"})
        reg.register(ToolSpec("tool_a", "first", {"type": "object"}, "filesystem.read",
                              lambda a: "ok-a", category="coding", capabilities=["do_x"], provider="prov_a"))
        reg.register(ToolSpec("tool_b", "second", {"type": "object"}, "filesystem.read",
                              lambda a: "ok-b", category="coding", capabilities=["do_x"], provider="prov_b"))
        return reg

    def test_route_picks_ranked_candidate(self):
        reg = self._reg()
        router = ToolRouter(reg, prefer=["prov_b"])
        self.assertEqual(router.route("do_x"), "tool_b")
        info = router.explain("do_x")
        self.assertEqual(len(info["candidates"]), 2)

    def test_exclusions(self):
        reg = self._reg()
        reg.set_enabled("tool_a", False)
        router = ToolRouter(reg)
        info = router.explain("do_x")
        self.assertEqual([c["tool"] for c in info["candidates"]], ["tool_b"])
        self.assertEqual(info["excluded"][0]["reason"], "disabled")

    def test_gpu_and_offline_gating(self):
        reg = self._reg()
        reg.register(ToolSpec("gpu_tool", "gpu", {"type": "object"}, "filesystem.read",
                              lambda a: "gpu", capabilities=["do_gpu"], requires_gpu=True))
        reg.register(ToolSpec("net_tool", "net", {"type": "object"}, "filesystem.read",
                              lambda a: "net", capabilities=["do_net"], requires_network=True))
        router = ToolRouter(reg, resources={"gpus": [], "total_vram_gb": 0})
        reasons = {e["tool"]: e["reason"] for e in router.explain("do_gpu")["excluded"]}
        self.assertEqual(reasons["gpu_tool"], "no_gpu")
        reg.permission_manager.set_level("network.read", "deny")
        reasons = {e["tool"]: e["reason"] for e in router.explain("do_net")["excluded"]}
        self.assertEqual(reasons["net_tool"], "offline")

    def test_execute_fallback_and_telemetry(self):
        reg = self._reg()
        reg.get("tool_a").handler = lambda a: "ERROR: blown up"
        router = ToolRouter(reg)
        out = router.execute("do_x", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], "ok-b")
        self.assertEqual(len(out["attempts"]), 1)
        self.assertTrue(router.recent(1)[0]["ok"])

    def test_approval_required_is_surfaced(self):
        reg = ToolRegistry({"filesystem.read": "ask"})
        reg.register(ToolSpec("ask_tool", "a", {"type": "object"}, "filesystem.read",
                              lambda a: "ok", capabilities=["do_y"]))
        router = ToolRouter(reg)
        out = router.execute("do_y", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "approval_required")

    def test_no_candidates(self):
        router = ToolRouter(self._reg())
        out = router.execute("nonexistent_cap", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "no_capable_tool")


FAKE_MCP_SERVER = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if msg.get("id") is None:  # notification
        continue
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-mcp", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "echoes text",
                             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        result = {"content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}]}
    else:
        reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": "method not found"}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


class MCPManagerTests(unittest.TestCase):
    def _setup(self):
        tmp = Path(tempfile.mkdtemp())
        server_path = tmp / "fake_mcp.py"
        server_path.write_text(FAKE_MCP_SERVER, encoding="utf-8")
        reg = ToolRegistry({"shell.execute": "allow"})
        cfg = MCPServerConfig.from_dict({
            "id": "fake",
            "name": "Fake MCP",
            "command": [sys.executable, str(server_path)],
        })
        mgr = MCPManager(reg, [cfg], timeout=15)
        self.addCleanup(mgr.shutdown)
        return reg, mgr

    def test_config_parsing(self):
        cfgs = load_mcp_configs([{"id": "a", "command": ["x"], "permission": "network.read"}, {"no_id": True}])
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].id, "a")
        self.assertEqual(cfgs[0].permission, "network.read")

    def test_connect_imports_and_invokes_tool(self):
        reg, mgr = self._setup()
        row = mgr.connect("fake")
        self.assertEqual(row["state"], "connected")
        self.assertEqual(row["tools"], 1)
        spec = reg.get("mcp__fake__echo")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.source, "mcp")
        self.assertEqual(spec.category, "mcp")
        self.assertIn("mcp__fake__echo", {s["function"]["name"] for s in reg.schemas()})
        out = reg.execute("mcp__fake__echo", {"text": "hello"})
        self.assertEqual(out, "echo:hello")

    def test_disconnect_and_status(self):
        reg, mgr = self._setup()
        mgr.connect("fake")
        row = mgr.disconnect("fake")
        self.assertEqual(row["state"], "disconnected")
        out = reg.execute("mcp__fake__echo", {"text": "x"})
        self.assertTrue(out.startswith("ERROR"))
        status = mgr.status()["servers"][0]
        self.assertEqual(status["id"], "fake")

    def test_failed_server_is_isolated(self):
        reg = ToolRegistry({})
        mgr = MCPManager(reg, [MCPServerConfig.from_dict({
            "id": "bad", "name": "Bad", "command": ["definitely-not-an-executable-xyz"]})])
        row = mgr.connect("bad")
        self.assertEqual(row["state"], "error")
        self.assertTrue(row["error"])
        self.assertNotIn("mcp__bad__x", reg.names())

    def test_unknown_server_raises(self):
        mgr = MCPManager(ToolRegistry({}))
        with self.assertRaises(KeyError):
            mgr.connect("nope")


if __name__ == "__main__":
    unittest.main()
