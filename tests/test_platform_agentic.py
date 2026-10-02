"""Computer Use, LSP client, multi-agent worktrees, voice session."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from localcodeagent.computer_use import ComputerUse
from localcodeagent.lsp import LspClient, LspPool
from localcodeagent.multiagent import AgentPool, is_repo
from localcodeagent.voice.stt import VoiceSession, SttEngine, list_input_devices

IS_WIN = os.name == "nt"


class ComputerUseTests(unittest.TestCase):
    def test_audit_records_actions(self):
        seen = []
        cu = ComputerUse(audit=lambda a, d: seen.append(a))
        cu.list_windows()
        self.assertEqual(seen, ["list_windows"])
        self.assertEqual(cu.actions[-1]["action"], "list_windows")

    @unittest.skipUnless(IS_WIN, "Windows-only")
    def test_list_windows_real(self):
        out = ComputerUse().list_windows()
        self.assertTrue(out["ok"])
        self.assertGreater(out["count"], 0)
        self.assertIn("title", out["windows"][0])

    @unittest.skipUnless(IS_WIN, "Windows-only")
    def test_clipboard_roundtrip(self):
        cu = ComputerUse()
        out = cu.clipboard_set("nexus-test-clip")
        if out["ok"]:
            back = cu.clipboard_get()
            self.assertEqual(back.get("text"), "nexus-test-clip")

    @unittest.skipUnless(IS_WIN, "Windows-only")
    def test_screenshot_real(self):
        with tempfile.TemporaryDirectory() as td:
            out = ComputerUse().screenshot(Path(td) / "s.png")
            self.assertTrue(out["ok"], out.get("error"))
            self.assertGreater(out["size"], 1000)

    def test_mouse_move_no_crash(self):
        cu = ComputerUse()
        out = cu.mouse_move(10, 10)
        self.assertIn("ok", out)  # False off-Windows, True on Windows


FAKE_LSP = r'''
import sys, json

def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.decode().partition(":")
        headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length", 0))
    return json.loads(sys.stdin.buffer.read(n).decode())

def send(payload):
    body = json.dumps(payload).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()

while True:
    msg = read_msg()
    if msg is None:
        break
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"capabilities": {"documentSymbolProvider": True,
                                          "definitionProvider": True}}})
    elif method == "textDocument/documentSymbol":
        send({"jsonrpc": "2.0", "id": mid, "result": [
            {"name": "handler", "kind": 12,
             "range": {"start": {"line": 0, "character": 0},
                       "end": {"line": 1, "character": 0}}}]})
    elif method == "textDocument/definition":
        send({"jsonrpc": "2.0", "id": mid, "result": [
            {"uri": "file:///fake.py",
             "range": {"start": {"line": 4, "character": 2},
                       "end": {"line": 4, "character": 10}}}]})
    elif method == "workspace/symbol":
        send({"jsonrpc": "2.0", "id": mid, "result": [
            {"name": "Widget", "kind": 5,
             "location": {"uri": "file:///w.py",
                          "range": {"start": {"line": 0, "character": 0},
                                    "end": {"line": 0, "character": 0}}}}]})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        break
'''


class LspTests(unittest.TestCase):
    def _client(self, td: str):
        fake = Path(td) / "fake_lsp.py"
        fake.write_text(FAKE_LSP)
        client = LspClient([sys.executable, str(fake)], Path(td))
        self.assertTrue(client.start())
        return client

    def test_initialize_and_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._client(td)
            try:
                out = client.document_symbols(Path(td) / "x.py")
                self.assertTrue(out["ok"], out.get("error"))
                self.assertEqual(out["result"][0]["name"], "handler")
            finally:
                client.shutdown()

    def test_definition_and_workspace_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._client(td)
            try:
                d = client.definition(Path(td) / "x.py", line=5, char=3)
                self.assertTrue(d["ok"])
                self.assertEqual(d["result"][0]["range"]["start"]["line"], 4)
                w = client.workspace_symbols("Wid")
                self.assertTrue(w["ok"])
                self.assertEqual(w["result"][0]["name"], "Widget")
            finally:
                client.shutdown()

    def test_pool_no_server_clean_failure(self):
        with tempfile.TemporaryDirectory() as td:
            pool = LspPool(Path(td), server_map={".py": [["definitely-not-an-lsp-server-xyz"]]})
            self.assertFalse(pool.supported(Path("x.py")))
            self.assertIsNone(pool.client_for(Path(td) / "x.py"))

    def test_pool_uses_registered_server(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake_lsp.py"
            fake.write_text(FAKE_LSP)
            pool = LspPool(Path(td), server_map={".py": [[sys.executable, str(fake)]]})
            try:
                client = pool.client_for(Path(td) / "x.py")
                self.assertIsNotNone(client)
                self.assertTrue(client.document_symbols(Path(td) / "x.py")["ok"])
            finally:
                pool.shutdown_all()


class WorktreeTests(unittest.TestCase):
    def _repo(self, td: str) -> Path:
        repo = Path(td) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "-c",
                        "user.name=t", "-c", "user.email=t@t",
                        "commit", "--allow-empty", "-m", "init"],
                       capture_output=True)
        return repo

    def test_spawn_commit_merge_teardown(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td)
            if not is_repo(repo):
                self.skipTest("git unavailable")
            pool = AgentPool(repo)
            out = pool.spawn("coder")
            self.assertTrue(out["ok"], out.get("error"))
            agent = pool.agents[out["id"]]
            # Agent works in ITS worktree — base repo stays clean.
            (agent.path / "feature.py").write_text("x = 1\n")
            self.assertTrue(agent.status()["dirty"])
            res = pool.collect()
            self.assertTrue(res["ok"], res)
            self.assertTrue((repo / "feature.py").is_file())
            self.assertTrue((agent.path / "feature.py").exists() is False
                            or agent.state == "merged")
            pool.teardown_all()
            self.assertFalse(agent.path.exists())

    def test_conflict_aborts_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td)
            if not is_repo(repo):
                self.skipTest("git unavailable")
            (repo / "shared.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                            "-c", "user.email=t@t", "add", "-A"],
                           capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                            "-c", "user.email=t@t", "commit", "-m", "add"],
                           capture_output=True)
            pool = AgentPool(repo)
            out = pool.spawn("coder")
            self.assertTrue(out["ok"], out.get("error"))
            agent = pool.agents[out["id"]]
            (agent.path / "shared.txt").write_text("agent change\n")
            (repo / "shared.txt").write_text("conflicting base change\n")
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                            "-c", "user.email=t@t", "commit", "-am", "c"],
                           capture_output=True)
            res = pool.collect()
            self.assertFalse(res["ok"])
            # Base repo not left in a half-merged state.
            st = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True, text=True)
            self.assertNotIn("UU", st.stdout)
            pool.teardown_all()


class VoiceSttTests(unittest.TestCase):
    def test_engine_unavailable_graceful(self):
        class Null(SttEngine):
            name = "null"
        sess = VoiceSession(Null(), voice_manager=None)
        st = sess.status()
        self.assertFalse(st["stt_available"])
        out = sess.start()
        self.assertFalse(out["ok"])

    def test_devices_listing_safe(self):
        devs = list_input_devices()
        self.assertIsInstance(devs, list)  # [] when sounddevice missing


if __name__ == "__main__":
    unittest.main()
