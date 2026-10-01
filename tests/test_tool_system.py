import json
import tempfile
import time
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
from localcodeagent.tools.plugins import install_command
from localcodeagent.secrets import SecretVault
from localcodeagent.tools.api import register_api_tools
from localcodeagent.tools.codeintel import extract_symbols, register_codeintel_tools
from localcodeagent.tools.data import profile_source, query_source, register_data_tools, render_chart
from localcodeagent.tools.media import (build_add_subtitles, build_convert, build_extract_audio,
                                        build_merge, build_normalize, build_thumbnail, build_trim,
                                        find_ffmpeg, find_ffprobe, register_media_tools)
from localcodeagent.tools.documents import extract_document_text, register_document_tools
from localcodeagent.tools.knowledge import KnowledgeIndex, register_knowledge_tools
from localcodeagent.tools.sandbox import run_python, register_sandbox_tools
from localcodeagent.events import EventBus, make_emitter
from localcodeagent.tools.workflows import load_workflows, register_workflow_tools, run_workflow
from localcodeagent.tools.blender3d import build_scene_script, register_blender_tools
from localcodeagent.tools.docker_tool import register_docker_tools
from localcodeagent.tools.git import register_git_tools


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
            self.assertIn("TOOL_NOT_INSTALLED", reg.execute("blender", {}))

    def test_installed_manifest_without_invoke_is_not_callable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mdir = root / "manifests"
            mdir.mkdir()
            self._write(mdir, "catalog_only.json", {
                "id": "comfyui",
                "name": "ComfyUI",
                "category": "images",
            })
            reg = _registry(**{"shell.execute": "allow"})
            load_plugin_manifests(mdir, reg)
            manifest = reg.manifest("comfyui")
            self.assertEqual(manifest["install_status"], "installed")
            self.assertFalse(manifest["callable"])
            self.assertNotIn("comfyui", {s["function"]["name"] for s in reg.schemas()})
            self.assertIn("ERROR", reg.execute("comfyui", {}))

    def test_invalid_manifest_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            mdir = Path(td)
            self._write(mdir, "bad.json", {"name": "no id"})
            result = load_plugin_manifests(mdir, _registry())
            self.assertEqual(result["loaded"], [])
            self.assertEqual(len(result["errors"]), 1)

    def test_install_command_mapping(self):
        self.assertEqual(install_command({"method": "winget", "package": "Gyan.FFmpeg"})[:4],
                         ["winget", "install", "--id", "Gyan.FFmpeg"])
        self.assertEqual(install_command({"method": "pip", "package": "duckdb"})[-4:],
                         ["-m", "pip", "install", "duckdb"])
        self.assertIsNone(install_command({"method": "build", "notes": "compile it"}))
        self.assertIsNone(install_command({"method": "winget"}))
        self.assertIsNone(install_command({}))

    def test_bundled_manifests_load(self):
        root = Path(__file__).resolve().parent.parent / "tools" / "manifests"
        reg = _registry(**{"shell.execute": "allow", "docker.access": "ask"})
        report = load_plugin_manifests(root, reg, workspace=Path(tempfile.mkdtemp()))
        self.assertFalse(report["errors"])
        for expected in ("ffmpeg", "ffprobe", "whisper", "tesseract", "pandoc", "duckdb", "docker", "blender", "ripgrep"):
            self.assertIn(expected, report["loaded"])
        docker = reg.manifest("docker")
        self.assertEqual(docker["permission"], "docker.access")

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

    def test_secret_env_resolution(self):
        from localcodeagent.secrets import SecretVault
        tmp = Path(tempfile.mkdtemp())
        vault = SecretVault(tmp / "secrets.vault")
        vault.set("MY_KEY", "sk-test-123")
        server_path = tmp / "env_server.py"
        server_path.write_text(
            "import sys, json, os\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    if msg.get('id') is None: continue\n"
            "    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'tools':[]}}), flush=True)\n",
            encoding="utf-8")
        cfg = MCPServerConfig.from_dict({
            "id": "envsrv", "command": [sys.executable, str(server_path)],
            "env": {"API_TOKEN": "secret:MY_KEY", "PLAIN": "value"}})
        # resolve via a client directly
        from localcodeagent.mcp import MCPClient
        c = MCPClient(cfg, env_resolver=vault.get)
        env = c._resolved_env()
        self.assertEqual(env["API_TOKEN"], "sk-test-123")
        self.assertEqual(env["PLAIN"], "value")
        # unresolvable secret → launch fails
        cfg2 = MCPServerConfig.from_dict({
            "id": "bad", "command": [sys.executable, "-c", "pass"],
            "env": {"X": "secret:MISSING"}})
        c2 = MCPClient(cfg2, env_resolver=vault.get)
        with self.assertRaises(Exception):
            c2.start()


class MCPHTTPTests(unittest.TestCase):
    def _http_server(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                msg = json.loads(self.rfile.read(length) or b"{}")
                if msg.get("id") is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                method = msg.get("method")
                if method == "tools/list":
                    result = {"tools": [{"name": "ping", "description": "pong",
                                         "inputSchema": {"type": "object", "properties": {}}}]}
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": "pong"}]}
                else:
                    result = {"capabilities": {"tools": {}}, "serverInfo": {"name": "fake-http"}}
                body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "sess-1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_port}/mcp"

    def test_http_transport_connects_and_calls(self):
        url = self._http_server()
        reg = ToolRegistry({"external_api.call": "allow"})
        cfg = MCPServerConfig.from_dict({"id": "webmcp", "url": url})
        self.assertEqual(cfg.transport, "http")
        self.assertEqual(cfg.permission, "external_api.call")
        mgr = MCPManager(reg, [cfg], timeout=10)
        self.addCleanup(mgr.shutdown)
        row = mgr.connect("webmcp")
        self.assertEqual(row["state"], "connected", row)
        self.assertEqual(row["tools"], 1)
        spec = reg.get("mcp__webmcp__ping")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.permission, "external_api.call")
        out = reg.execute("mcp__webmcp__ping", {})
        self.assertEqual(out, "pong")
        status = mgr.status()["servers"][0]
        self.assertEqual(status["transport"], "http")


class BrowserRunnerTests(unittest.TestCase):
    def test_session_path_sanitized(self):
        from localcodeagent.webtools.browser import BrowserRunner
        with tempfile.TemporaryDirectory() as td:
            runner = BrowserRunner(artifacts_dir=Path(td))
            p = runner.session_path("my session/../evil")
            self.assertEqual(p.parent, Path(td))
            self.assertNotIn("..", p.name)
            with self.assertRaises(ValueError):
                runner.session_path("")

    def test_health_reports_playwright_state(self):
        from localcodeagent.webtools.browser import BrowserRunner
        with tempfile.TemporaryDirectory() as td:
            runner = BrowserRunner(artifacts_dir=Path(td))
            h = runner.health()
            self.assertIn("ok", h)
            self.assertEqual(h["ok"], runner.available())


class SecretVaultTests(unittest.TestCase):
    def test_set_get_list_delete_and_persist(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "secrets.vault"
            vault = SecretVault(path)
            vault.set("github.personal", "ghp_secret_value_123", description="GitHub PAT")
            vault.set("openai.api", "sk-test")
            names = [r["name"] for r in vault.list()]
            self.assertEqual(names, ["github.personal", "openai.api"])
            self.assertNotIn("ghp_secret_value_123", json.dumps(vault.list()))
            # values live only behind get()
            self.assertEqual(vault.get("github.personal"), "ghp_secret_value_123")
            # encrypted at rest
            raw = path.read_text()
            self.assertNotIn("ghp_secret_value_123", raw)
            self.assertIn('"data"', raw)
            # reload
            vault2 = SecretVault(path)
            self.assertEqual(vault2.get("github.personal"), "ghp_secret_value_123")
            self.assertTrue(vault2.delete("openai.api"))
            self.assertIsNone(vault2.get("openai.api"))

    def test_name_validation(self):
        with tempfile.TemporaryDirectory() as td:
            vault = SecretVault(Path(td) / "s.vault")
            with self.assertRaises(ValueError):
                vault.set("../evil", "x")
            with self.assertRaises(ValueError):
                vault.set("name with spaces", "x")
            with self.assertRaises(ValueError):
                vault.set("ok.name", "")


class CodeIntelTests(unittest.TestCase):
    def test_extract_symbols_python(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mod.py"
            p.write_text("import os\n\nclass Foo:\n    pass\n\ndef bar():\n    pass\n", encoding="utf-8")
            info = extract_symbols(p)
            kinds = {(s["kind"], s["name"]) for s in info["symbols"]}
            self.assertIn(("class", "Foo"), kinds)
            self.assertIn(("function", "bar"), kinds)
            self.assertIn(("import", "os"), kinds)

    def test_code_map_scans_tree(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
            (ws / "skip.txt").write_text("class X", encoding="utf-8")
            reg = ToolRegistry({"filesystem.read": "allow"})
            register_codeintel_tools(reg, ws)
            out = json.loads(reg.execute("code_map", {}))
            self.assertEqual(out["files"], 1)
            self.assertIn("a.py", out["symbols"])
            self.assertTrue(reg.execute("code_symbols", {"path": "../outside.py"}).startswith("ERROR"))


class ApiToolTests(unittest.TestCase):
    def test_method_and_url_validation(self):
        reg = ToolRegistry({"external_api.call": "allow"})
        register_api_tools(reg, vault=None)
        out = json.loads(reg.execute("api_request", {"method": "TRACE", "url": "https://x"}))
        self.assertIn("error", out)
        out = json.loads(reg.execute("api_request", {"url": "file:///etc/passwd"}))
        self.assertIn("error", out)

    def test_auth_secret_reference(self):
        with tempfile.TemporaryDirectory() as td:
            vault = SecretVault(Path(td) / "v.vault")
            vault.set("svc.key", "supersecret")
            reg = ToolRegistry({"external_api.call": "allow", "credentials.use": "allow"})
            register_api_tools(reg, vault=vault)
            # unknown secret name is reported, real ones resolve at call time
            out = json.loads(reg.execute("api_request", {"url": "https://x", "auth": {"secret": "nope"}}))
            self.assertIn("unknown secret", out["error"])
            out = json.loads(reg.execute("secrets_list", {}))
            self.assertEqual(out["secrets"][0]["name"], "svc.key")
            self.assertNotIn("supersecret", json.dumps(out))


class DataToolTests(unittest.TestCase):
    def _csv_ws(self):
        ws = Path(tempfile.mkdtemp())
        (ws / "sales.csv").write_text("region,amount\nwest,10\neast,30\nwest,15\n", encoding="utf-8")
        return ws

    def test_csv_query_and_profile(self):
        ws = self._csv_ws()
        out = query_source(ws, "sales.csv", "SELECT region, SUM(CAST(amount AS REAL)) AS total FROM data GROUP BY region ORDER BY total DESC")
        rows = out["rows"]
        self.assertEqual(rows[0]["region"], "east")
        self.assertIn(out["engine"], {"sqlite", "duckdb"})
        prof = profile_source(ws, "sales.csv")
        self.assertEqual(prof["source"], "sales.csv")
        self.assertTrue(prof.get("columns"))

    def test_query_rejects_outside_workspace(self):
        ws = self._csv_ws()
        with self.assertRaises(ValueError):
            query_source(ws, "../outside.csv", "SELECT 1")

    def test_registry_tool_returns_json(self):
        ws = self._csv_ws()
        reg = ToolRegistry({"filesystem.read": "allow"})
        register_data_tools(reg, ws, artifacts_dir=ws / ".charts")
        out = reg.execute("data_query", {"source": "sales.csv", "sql": "SELECT COUNT(*) AS n FROM data"})
        self.assertIn('"rows"', out)

    def test_chart_svg_written(self):
        with tempfile.TemporaryDirectory() as td:
            out = render_chart({"type": "bar", "labels": ["a", "b"], "values": [3, 7], "title": "T"}, Path(td) / "c.svg")
            text = out.read_text(encoding="utf-8")
            self.assertIn("<svg", text)
            self.assertIn("<rect", text)
            out2 = render_chart({"type": "pie", "labels": ["x", "y"], "values": [1, 1]}, Path(td) / "p.svg")
            self.assertIn("<path", out2.read_text())
            with self.assertRaises(ValueError):
                render_chart({"type": "nope", "values": [1]}, Path(td) / "n.svg")


class MediaToolTests(unittest.TestCase):
    def test_argv_builders(self):
        s, d = Path("in.mp4"), Path("out.mp3")
        self.assertIn("-vn", build_extract_audio(s, d))
        self.assertIn("mp3", build_extract_audio(s, d))
        trim = build_trim(s, Path("clip.mp4"), 5.0, 10.0)
        self.assertIn("-ss", trim)
        self.assertIn("copy", trim)
        conv = build_convert(s, Path("out.webm"), "128k")
        self.assertIn("-b:a", conv)
        self.assertIn("-frames:v", build_thumbnail(s, Path("t.png"), 3.0))
        self.assertIn("loudnorm", " ".join(build_normalize(s, Path("n.wav"))))
        soft = build_add_subtitles(s, Path("subs.srt"), Path("out.mp4"), burn=False)
        self.assertIn("mov_text", soft)
        burned = build_add_subtitles(s, Path("subs.srt"), Path("out.mp4"), burn=True)
        self.assertIn("subtitles", " ".join(burned))
        merged = build_merge([Path("a.mp4"), Path("b.mp4")], Path("out.mp4"))
        self.assertIn("concat=n=2", " ".join(merged))

    def test_media_tools_report_missing_ffmpeg(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "in.mp4").write_bytes(b"fake")
            reg = ToolRegistry({"filesystem.read": "allow", "filesystem.write": "allow", "shell.execute": "allow"})
            register_media_tools(reg, ws)
            out = reg.execute("media_probe", {"source": "in.mp4"})
            # Either ffprobe ran (installed) or a clean missing-dependency error
            if not find_ffprobe():
                self.assertTrue(out.startswith("ERROR"), out)
            out = reg.execute("trim_video", {"source": "../nope.mp4", "output": "x.mp4"})
            self.assertTrue(out.startswith("ERROR"))

    def test_media_transcribe_pipeline_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "clip.mp4").write_bytes(b"fake")
            reg = ToolRegistry({"shell.execute": "allow", "filesystem.write": "allow"})
            register_media_tools(reg, ws, jobs=JobManager(ws / "jobs"))
            raw = reg.execute("media_transcribe", {"source": "clip.mp4"})
            if raw.startswith("ERROR"):
                self.assertIn("model", raw.lower())  # no whisper model in fixture
            else:
                out = json.loads(raw)
                self.assertFalse(out["ok"])
                self.assertEqual(out.get("failed_step"), "extract_audio")

    def test_speak_text_requires_voice_model(self):
        with tempfile.TemporaryDirectory() as td:
            reg = ToolRegistry({"shell.execute": "allow"})
            register_media_tools(reg, Path(td))
            out = reg.execute("speak_text", {"text": "hello"})
            self.assertTrue(out.startswith("ERROR"))
            self.assertIn("voice model", out)

    def test_manifest_invoker_stdin(self):
        import sys
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            manifest = {
                "id": "echo_in", "name": "Echo In", "executables": [sys.executable],
                "permissions": ["shell.execute"],
                "invoke": {
                    "command": [sys.executable, "-c", "import sys;print(sys.stdin.read().upper())"],
                    "stdin": "text",
                    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                },
            }
            (ws / "echo_in.json").write_text(json.dumps(manifest))
            reg = ToolRegistry({"shell.execute": "allow"})
            result = load_plugin_manifests(ws, reg, workspace=ws)
            self.assertIn("echo_in", result["loaded"])
            out = json.loads(reg.execute("echo_in", {"text": "hello piper"}))
            self.assertIn("HELLO PIPER", out["stdout"])


class DocumentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.reg = ToolRegistry({"filesystem.read": "allow", "filesystem.write": "allow"})
        register_document_tools(self.reg, self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_markdown_and_csv(self):
        (self.ws / "note.md").write_text("# Hi\n\nsome text", encoding="utf-8")
        out = json.loads(self.reg.execute("extract_text", {"file": "note.md"}))
        self.assertEqual(out["kind"], "text")
        self.assertIn("some text", out["text"])
        (self.ws / "t.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        out = json.loads(self.reg.execute("extract_text", {"file": "t.csv"}))
        self.assertEqual(out["kind"], "csv")
        self.assertIn("columns (2): a, b", out["text"])

    def test_extract_html_strips_tags(self):
        (self.ws / "p.html").write_text("<html><style>x{}</style><p>Hello <b>world</b></p><script>bad()</script></html>")
        out = json.loads(self.reg.execute("extract_text", {"file": "p.html"}))
        self.assertEqual(out["kind"], "html")
        self.assertIn("Hello world", out["text"])
        self.assertNotIn("bad()", out["text"])

    def test_extract_errors(self):
        self.assertIn("ERROR", self.reg.execute("extract_text", {"file": "../x.txt"}))
        self.assertIn("ERROR", self.reg.execute("extract_text", {"file": "missing.txt"}))
        (self.ws / "f.docx").write_bytes(b"PK")
        out = self.reg.execute("extract_text", {"file": "f.docx"})
        self.assertIn("ERROR", out)
        self.assertIn("convert_document", out)

    def test_ocr_and_convert_delegate(self):
        # Tesseract/pandoc manifests aren't registered here → clean delegation errors
        out = self.reg.execute("ocr_image", {"image": "missing.png"})
        self.assertTrue(out.startswith("ERROR"))
        (self.ws / "img.png").write_bytes(b"png")
        out = self.reg.execute("ocr_image", {"image": "img.png"})
        self.assertTrue(out.startswith(("TOOL_NOT_FOUND", "ERROR", "PERMISSION")))
        out = self.reg.execute("convert_document", {"input": "note.md", "output": "note.html"})
        self.assertTrue(out.startswith(("TOOL_NOT_FOUND", "ERROR", "PERMISSION")))


class KnowledgeToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.reg = ToolRegistry({"filesystem.read": "allow", "filesystem.write": "allow"})
        self.index = register_knowledge_tools(self.reg, self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_and_search(self):
        (self.ws / "notes.md").write_text("Python asyncio uses coroutines and event loops for concurrency. " * 30)
        (self.ws / "rust.md").write_text("Rust uses ownership and borrowing for memory safety. " * 30)
        out = json.loads(self.reg.execute("knowledge_index", {}))
        self.assertEqual(out["files_indexed"], 2)
        out = json.loads(self.reg.execute("knowledge_search", {"query": "asyncio coroutines"}))
        self.assertEqual(out["results"][0]["file"], "notes.md")
        out = json.loads(self.reg.execute("knowledge_search", {"query": "ownership borrowing"}))
        self.assertEqual(out["results"][0]["file"], "rust.md")

    def test_incremental_index(self):
        f = self.ws / "a.md"
        f.write_text("alpha beta gamma " * 50)
        json.loads(self.reg.execute("knowledge_index", {}))
        first = self.index.entries["a.md"]["mtime"]
        out = json.loads(self.reg.execute("knowledge_index", {}))
        self.assertEqual(out["files_indexed"], 0)  # unchanged → skipped
        f.write_text("delta epsilon " * 50)
        out = json.loads(self.reg.execute("knowledge_index", {}))
        self.assertEqual(out["files_indexed"], 1)

    def test_forget_and_empty_query(self):
        (self.ws / "x.md").write_text("unique zzq token " * 20)
        json.loads(self.reg.execute("knowledge_index", {}))
        self.assertIn("removed", self.reg.execute("knowledge_forget", {"file": "x.md"}))
        self.assertIn("ERROR", self.reg.execute("knowledge_forget", {"file": "x.md"}))
        self.assertIn("ERROR", self.reg.execute("knowledge_search", {"query": ""}))


class SandboxToolTests(unittest.TestCase):
    def test_python_exec(self):
        with tempfile.TemporaryDirectory() as td:
            reg = ToolRegistry({"shell.execute": "allow"})
            register_sandbox_tools(reg, Path(td))
            out = json.loads(reg.execute("python_exec", {"code": "print(6*7)"}))
            self.assertTrue(out["ok"])
            self.assertIn("42", out["stdout"])
            out = json.loads(reg.execute("python_exec", {"code": "raise SystemExit(3)"}))
            self.assertFalse(out["ok"])
            self.assertEqual(out["exit_code"], 3)

    def test_python_exec_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            out = run_python("import sys, json; print(json.dumps(sys.flags.isolated))", Path(td) / "sb")
            self.assertTrue(out["ok"])
            self.assertIn("1", out["stdout"])


class EventBusTests(unittest.TestCase):
    def test_publish_subscribe_replay(self):
        bus = EventBus(history=50)
        bus.publish("job", {"id": "1"})
        sub = bus.subscribe(replay=10)
        first = sub.get(timeout=1)
        self.assertEqual(first["type"], "job")
        bus.publish("tool", {"tool": "x"})
        second = sub.get(timeout=1)
        self.assertEqual(second["type"], "tool")
        bus.unsubscribe(sub)
        self.assertEqual(bus.subscriber_count(), 0)

    def test_jobs_and_registry_emit(self):
        bus = EventBus()
        with tempfile.TemporaryDirectory() as td:
            jobs = JobManager(Path(td) / "jobs.json")
            jobs.on_change = make_emitter(bus, "job")
            reg = ToolRegistry({"filesystem.read": "allow"})
            reg.on_event = make_emitter(bus, "tool")
            reg.register(ToolSpec("ok_tool", "d", {"type": "object", "properties": {}},
                                  "filesystem.read", lambda a: "done"))
            sub = bus.subscribe(replay=0)
            job = jobs.submit("media", "test")
            jobs.update(job.id, state="completed")
            reg.execute("ok_tool", {})
            seen = [sub.get(timeout=1)["type"] for _ in range(3)]
            self.assertEqual(seen, ["job", "job", "tool"])


class WorkflowToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.wdir = self.ws / "workflows"
        self.wdir.mkdir()
        self.reg = ToolRegistry({"filesystem.read": "allow", "shell.execute": "allow"})
        self.jobs = JobManager(self.ws / "jobs.json")
        register_workflow_tools(self.reg, self.ws, workflows_dir=self.wdir,
                                jobs=self.jobs)

    def tearDown(self):
        self.tmp.cleanup()

    def _stub(self, name, result):
        self.reg.register(ToolSpec(name, "d", {"type": "object", "properties": {}},
                                   "filesystem.read", (lambda a, r=result: r)))

    def test_load_and_list(self):
        (self.wdir / "wf.json").write_text(json.dumps({
            "id": "demo", "name": "Demo", "params": {"x": "1"},
            "steps": [{"tool": "a", "args": {}}]}))
        out = json.loads(self.reg.execute("list_workflows", {}))
        self.assertEqual(out["workflows"][0]["id"], "demo")

    def test_run_workflow_templates_and_outputs(self):
        self._stub("make", json.dumps({"made": "thing-42"}))
        captured = {}
        def use(args):
            captured.update(args)
            return "ok"
        self.reg.register(ToolSpec("use", "d", {"type": "object", "properties": {}},
                                   "filesystem.read", use))
        wf = {"id": "w", "steps": [
            {"tool": "make", "args": {"p": "{params.seed}"}, "save_as": "first"},
            {"tool": "use", "args": {"v": "{steps.first.made}"}},
        ]}
        out = run_workflow(self.reg, wf, {"seed": "x"})
        self.assertTrue(out["ok"])
        self.assertEqual(captured["v"], "thing-42")
        self.assertEqual(out["outputs"]["first"], '{"made": "thing-42"}')

    def test_run_workflow_stops_on_error(self):
        self._stub("boom", "ERROR: failed hard")
        self._stub("never", "ok")
        wf = {"id": "w", "steps": [{"tool": "boom", "args": {}}, {"tool": "never", "args": {}}]}
        out = run_workflow(self.reg, wf, {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_tool"], "boom")
        self.assertEqual(len(out["steps"]), 1)

    def test_unknown_workflow(self):
        out = self.reg.execute("run_workflow", {"workflow": "nope"})
        self.assertIn("ERROR", out)
        self.assertIn("unknown workflow", out)

    def test_workflow_cancellation_between_steps(self):
        self._stub("ok_step", "done")

        def cancel_step(args):
            for j in self.jobs.list_jobs():
                if j["kind"] == "workflow" and j["state"] == "running":
                    self.jobs.cancel(j["id"])
            return "ok"

        self.reg.register(ToolSpec("cancel_step", "d", {"type": "object", "properties": {}},
                                   "filesystem.read", cancel_step))
        wf = {"id": "c", "steps": [{"tool": "ok_step"}, {"tool": "cancel_step"}, {"tool": "ok_step"}]}
        out = run_workflow(self.reg, wf, {}, jobs=self.jobs)
        self.assertFalse(out["ok"])
        self.assertTrue(out["cancelled"])
        self.assertEqual(out["cancelled_at_step"], 2)
        self.assertEqual(out["steps"][-1]["output"], "cancelled before step start")
        self.assertEqual(self.jobs.get([j["id"] for j in self.jobs.list_jobs()
                                        if j["kind"] == "workflow"][0]).state, "cancelled")


class BlenderToolTests(unittest.TestCase):
    def test_scene_script_builder(self):
        script = build_scene_script({
            "objects": [
                {"type": "cube", "location": [0, 0, 1], "scale": [2, 1, 1], "color": [1, 0, 0]},
                {"type": "sphere", "location": [3, 0, 1]},
                {"type": "bogus"},  # unknown type skipped
            ],
            "resolution": [320, 240],
        }, Path("/tmp/out.png"))
        self.assertIn("primitive_cube_add", script)
        self.assertIn("primitive_uv_sphere_add", script)
        self.assertNotIn("bogus", script)
        self.assertIn("resolution_x = 320", script)
        self.assertIn("camera_add", script)
        self.assertIn("out.png", script)
        compile(script, "<blender-scene>", "exec")  # generated script is valid Python

    def test_blender_render_delegates_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            reg = ToolRegistry({"shell.execute": "allow", "filesystem.write": "allow"})
            register_blender_tools(reg, ws, jobs=JobManager(ws / "jobs.json"))
            out = json.loads(reg.execute("blender_render", {"objects": [{"type": "cube"}]}))
            self.assertFalse(out["ok"])  # blender manifest not registered here
            self.assertTrue((ws / out["script"]).exists())
            self.assertIn("ERROR", out["detail"])


class DockerToolTests(unittest.TestCase):
    def _registry_with_docker(self, ws: Path):
        # Register a stub 'docker' manifest tool to capture delegated argv
        captured = {}
        reg = ToolRegistry({"docker.access": "allow"})
        register_docker_tools(reg, ws)
        reg.register(ToolSpec("docker", "d", {"type": "object", "properties": {}},
                              "docker.access",
                              lambda a: captured.setdefault("argv", a.get("args")) or "ok"))
        return reg, captured

    def test_docker_run_builds_safe_argv(self):
        with tempfile.TemporaryDirectory() as td:
            reg, captured = self._registry_with_docker(Path(td))
            reg.execute("docker_run", {"image": "python:3.12", "command": "python -V"})
            argv = captured["argv"]
            self.assertEqual(argv[:2], ["run", "--rm"])
            self.assertIn("--network", argv)
            self.assertIn("none", argv)
            self.assertIn("/work", argv[argv.index("-w") + 1])
            self.assertIn("python:3.12", argv)
            self.assertEqual(argv[-3:], ["sh", "-c", "python -V"])

    def test_docker_run_requires_image_and_command(self):
        with tempfile.TemporaryDirectory() as td:
            reg, _ = self._registry_with_docker(Path(td))
            self.assertIn("image", reg.execute("docker_run", {"command": "x"}))
            self.assertIn("command", reg.execute("docker_run", {"image": "python"}))

    def test_docker_network_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            reg, captured = self._registry_with_docker(Path(td))
            reg.execute("docker_run", {"image": "alpine", "command": ["ls"], "network": True})
            self.assertNotIn("--network", captured["argv"])


class RouterTelemetryTests(unittest.TestCase):
    def _registry(self):
        reg = ToolRegistry({"filesystem.read": "allow"})
        reg.register(ToolSpec("tool_a", "d", {"type": "object", "properties": {}},
                              "filesystem.read", lambda a: "ok", capabilities=["cap_x"]))
        reg.register(ToolSpec("tool_b", "d", {"type": "object", "properties": {}},
                              "filesystem.read", lambda a: "ERROR: boom", capabilities=["cap_x"]))
        return reg

    def test_learned_score_ranks_reliable_tool_first(self):
        reg = self._registry()
        router = ToolRouter(reg)
        for _ in range(3):
            router.execute("cap_x", {}, approved=True)  # tool_a wins, tool_b fails
        scored = {c["tool"]: c["score"] for c in router.explain("cap_x")["candidates"]}
        self.assertGreater(scored["tool_a"], scored["tool_b"])

    def test_telemetry_persists_and_stats(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            reg = self._registry()
            router = ToolRouter(reg, telemetry_path=path)
            for _ in range(3):
                router.execute("cap_x", {}, approved=True)
            self.assertTrue(path.is_file())
            # New router instance replays history
            router2 = ToolRouter(self._registry(), telemetry_path=path)
            self.assertEqual(len(router2.recent(10)), 3)
            stats = router2.stats()
            self.assertEqual(stats["total_events"], 3)
            row = next(r for r in stats["routes"] if r["tool"] == "tool_a")
            self.assertEqual(row["success_rate"], 1.0)


class CodeIntelAstTests(unittest.TestCase):
    def test_python_ast_backend(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mod.py"
            p.write_text(
                "import os\nfrom typing import Any\n\n"
                "class Foo(Base):\n    def method(self):\n        pass\n\n"
                "@decorator\ndef top():\n    pass\n",
                encoding="utf-8")
            out = extract_symbols(p)
            self.assertEqual(out.get("backend"), "ast")
            kinds = {s["name"]: s["kind"] for s in out["symbols"]}
            self.assertEqual(kinds.get("Foo"), "class")
            self.assertEqual(kinds.get("method"), "method")
            self.assertEqual(kinds.get("top"), "function")
            foo = next(s for s in out["symbols"] if s["name"] == "Foo")
            self.assertEqual(foo.get("bases"), ["Base"])

    def test_python_syntax_error_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.py"
            p.write_text("class Broken(:\n    def nope(\n", encoding="utf-8")
            out = extract_symbols(p)
            # regex fallback still returns something rather than crashing
            self.assertIn("symbols", out)


class AppStateWiringTests(unittest.TestCase):
    """Full-stack registry wiring — guards against registration regressions."""

    def _state(self, td: str):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState
        cfg = AgentConfig(models=[ModelProfile(
            id="ext", endpoint="http://x/v1", model="m",
            roles=["primary_coder"], runtime="external")])
        return AppState(cfg, Path(td), Path(td) / ".runtime")

    def test_core_tool_families_registered(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            names = {m["name"] for m in state.tools.manifests()}
            for expected in (
                "read_file", "write_file", "terminal_run", "search_code",
                "build_project", "git_status", "git_commit", "browser_run",
                "research_topic", "web_search", "api_request", "secrets_list",
                "code_symbols", "code_map", "data_query", "chart_generate",
                "media_probe", "extract_audio", "media_transcribe", "speak_text",
                "extract_text", "ocr_image", "convert_document",
                "knowledge_index", "knowledge_search", "python_exec",
                "docker_run", "blender_render", "list_workflows", "run_workflow",
                "find_tools", "use_capability", "install_tool", "system_resources",
            ):
                self.assertIn(expected, names, f"missing registered tool: {expected}")

    def test_use_capability_routes_through_router(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            out = json.loads(state.tools.execute(
                "use_capability",
                {"capability": "read_file", "arguments": {}},
            ))
            # capability resolves to a real tool (read_file) — args may error but routing works
            self.assertTrue(out["ok"] or out.get("attempts"), out)

    def test_events_bus_wired(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            sub = state.events.subscribe(replay=0)
            job = state.jobs.submit("test", "probe")
            event = sub.get(timeout=2)
            self.assertEqual(event["type"], "job")
            self.assertEqual(event["job"]["id"], job.id)


class GitWorktreeTests(unittest.TestCase):
    def _repo(self, td: str) -> Path:
        ws = Path(td)
        import subprocess as sp
        sp.run(["git", "init", "-q"], cwd=ws, check=True)
        sp.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True)
        sp.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
        (ws / "f.txt").write_text("x")
        sp.run(["git", "add", "."], cwd=ws, check=True)
        sp.run(["git", "commit", "-qm", "init"], cwd=ws, check=True)
        return ws

    def test_worktree_lifecycle(self):
        import shutil
        if not shutil.which("git"):
            self.skipTest("git not installed")
        with tempfile.TemporaryDirectory() as td:
            ws = self._repo(td)
            reg = ToolRegistry({"filesystem.read": "allow", "git.execute": "allow"})
            register_git_tools(reg, ws)
            out = json.loads(reg.execute("git_worktree_add", {"branch": "agent/test"}))
            self.assertIn("agent", out["path"])
            self.assertTrue(Path(out["path"]).is_dir())
            listed = json.loads(reg.execute("git_worktree_list", {}))
            self.assertTrue(any("agent" in w.get("worktree", "") for w in listed["worktrees"]))
            out = json.loads(reg.execute("git_worktree_remove",
                                         {"path": "agent/test", "force": True}))
            self.assertIn("removed", out)
            self.assertIn("ERROR", reg.execute("git_worktree_add", {"branch": "../bad"}))


class ProcessWatchdogTests(unittest.TestCase):
    def test_watchdog_restarts_crashed_not_stopped(self):
        from localcodeagent.processes import ProcessManager, ManagedService
        mgr = ProcessManager()
        states = {"crashed": 0, "stopped": 0}
        descs = {"crashy": {"state": "crashed"}, "calm": {"state": "stopped"}}
        mgr.register(ManagedService(
            id="crashy", name="c", kind="internal",
            describe=lambda: dict(descs["crashy"]),
            restart=lambda: states.__setitem__("crashed", states["crashed"] + 1),
            metadata={"auto_restart": True}))
        mgr.register(ManagedService(
            id="calm", name="s", kind="internal",
            describe=lambda: dict(descs["calm"]),
            restart=lambda: states.__setitem__("stopped", states["stopped"] + 1),
            metadata={"auto_restart": True}))
        events = []
        mgr.on_event = events.append
        mgr.start_watchdog(interval=0.05, window_seconds=60, max_restarts=2)
        try:
            time.sleep(0.35)
        finally:
            mgr.stop_watchdog()
        self.assertEqual(states["crashed"], 2)   # bounded by max_restarts
        self.assertEqual(states["stopped"], 0)   # user-stopped never restarted
        self.assertTrue(any(e.get("event") == "auto_restart" for e in events))


if __name__ == "__main__":
    unittest.main()
