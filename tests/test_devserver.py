import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.devserver import DevServerManager
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.devserver import register_devserver_tools
from localcodeagent.tools.terminal import TerminalTracker


PERMS = {"shell.execute": True, "filesystem.read": True}


def _python() -> str:
    return sys.executable


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server_cmd() -> str:
    # -u: http.server's banner is block-buffered when stdout is a file
    return f'"{_python()}" -u -m http.server {_free_port()} --bind 127.0.0.1'


class _FakeRootedTerminalTracker(TerminalTracker):
    pass


class DevServerManagerTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name) / "app"
        self.root.mkdir()
        (self.root / "index.html").write_text("<h1>hi</h1>")
        self.log_dir = Path(self._td.name) / "logs"
        self.tracker = TerminalTracker(self.log_dir)
        self.addCleanup(self.tracker.shutdown)
        self.mgr = DevServerManager(
            self.tracker, Path(self._td.name) / "devservers.json",
            roots_fn=lambda: [self.root])

    def test_boundary_refusal(self):
        out = self.mgr.start("D:/Windows", "anything")
        self.assertIn("error", out)

    def test_outside_root_tool_refuses(self):
        reg = ToolRegistry(dict(PERMS))
        register_devserver_tools(reg, self.mgr)
        out = reg.get("dev_server_start").handler(
            {"path": "D:/Windows", "command": "x", "wait": False})
        self.assertIn("ERROR", out)

    def test_real_server_detects_url_and_probes(self):
        # python -m http.server announces "Serving HTTP on 0.0.0.0 port N"
        cmd = _server_cmd()
        res = self.mgr.start(str(self.root), cmd)
        self.assertTrue(res.get("ok"), res)
        sid = res["server"]["id"]
        wait = self.mgr.wait_for_url(sid, timeout=20)
        self.assertTrue(wait.get("ok"), wait)
        self.assertTrue(wait["url"].startswith("http://127.0.0.1:"))
        self.assertGreater(wait["port"], 0)
        row = [s for s in self.mgr.list() if s["id"] == sid][0]
        self.assertEqual(row["state"], "serving")
        self.assertTrue(row["health"]["ok"])
        # duplicate start refused — same root + same command
        dup = self.mgr.start(str(self.root), cmd)
        self.assertIn("error", dup)
        # logs + stop
        logs = self.mgr.logs(sid)
        self.assertTrue(logs["lines"])
        stop = self.mgr.stop(sid)
        self.assertTrue(stop["ok"])
        time.sleep(0.3)
        row = [s for s in self.mgr.list() if s["id"] == sid][0]
        self.assertEqual(row["state"], "stopped")

    def test_dead_command_reports_exit(self):
        res = self.mgr.start(str(self.root), f'"{_python()}" -c "import sys; sys.exit(3)"')
        self.assertTrue(res.get("ok"), res)
        sid = res["server"]["id"]
        wait = self.mgr.wait_for_url(sid, timeout=10)
        self.assertFalse(wait["ok"])
        self.assertIn("exited", wait["error"])

    def test_persistence(self):
        self.mgr.start(str(self.root), "echo fake", name="fake")
        mgr2 = DevServerManager(
            self.tracker, Path(self._td.name) / "devservers.json",
            roots_fn=lambda: [self.root])
        self.assertTrue(mgr2.list())

    def test_tool_round_trip(self):
        reg = ToolRegistry(dict(PERMS))
        register_devserver_tools(reg, self.mgr)
        out = json.loads(reg.get("dev_server_start").handler(
            {"path": str(self.root),
             "command": _server_cmd(),
             "wait": True, "timeout": 20}))
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["wait"]["ok"], out)
        listed = json.loads(reg.get("dev_server_list").handler({}))
        self.assertEqual(len(listed["servers"]), 1)
        sid = listed["servers"][0]["id"]
        stopped = json.loads(reg.get("dev_server_stop").handler({"id": sid}))
        self.assertTrue(stopped["ok"])


if __name__ == "__main__":
    unittest.main()
