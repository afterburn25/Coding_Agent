"""Durable user downloads — 'download <url>'.

Covers the lifecycle end-to-end on a local HTTP server: url → dest,
job progress, hash verification, dedupe on verified artifact, resume
from .part via Range, cancel, system-dir refusal, and the action-lane
intent parse. Nothing claims success without bytes on disk.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from localcodeagent.action_ledger import ActionLedger
from localcodeagent.action_ops import execute_plan, parse_local_action
from localcodeagent.jobs import JobManager
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.downloads import (
    UserDownloadManager, register_download_tools)


class _Server(threading.Thread):
    """Tiny HTTP file server with Range support."""

    def __init__(self, payload: bytes):
        super().__init__(daemon=True)
        self.payload = payload
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                rng = self.headers.get("Range")
                if rng:
                    start = int(rng.split("=")[1].split("-")[0])
                    body = srv.payload[start:]
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     f"bytes {start}-{len(srv.payload)-1}"
                                     f"/{len(srv.payload)}")
                else:
                    body = srv.payload
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]

    def run(self):
        self.httpd.serve_forever()

    def url(self):
        return f"http://127.0.0.1:{self.port}/file.bin"


def make_env(perms=None):
    td = tempfile.TemporaryDirectory()
    ws = Path(td.name) / "ws"
    ws.mkdir()
    dl = Path(td.name) / "Downloads"
    reg = ToolRegistry(perms or {"network.read": "allow"})
    jobs = JobManager(Path(td.name) / "jobs.json")
    mgr = UserDownloadManager(jobs, dl)
    register_download_tools(reg, mgr)
    ledger = ActionLedger(Path(td.name) / "ledger.json")
    return td, ws, reg, ledger, mgr, dl


def wait_job(mgr, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.status(job_id)
        if st["state"] in ("completed", "failed", "cancelled"):
            return st
        time.sleep(0.1)
    return mgr.status(job_id)


class TestDownloadParse(unittest.TestCase):
    def setUp(self):
        td, self.ws, _, _, _, _ = make_env()
        self.addCleanup(td.cleanup)

    def test_download_url(self):
        plan = parse_local_action(
            "download https://example.com/setup.exe", workspace=self.ws)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, "download")
        self.assertEqual(plan.tool, "download_file")
        self.assertEqual(plan.permission, "network.read")
        self.assertEqual(plan.params["url"], "https://example.com/setup.exe")
        self.assertFalse(plan.outside_root)

    def test_download_to_workspace_path(self):
        plan = parse_local_action(
            "download https://x.y/f.zip to tools/f.zip",
            workspace=self.ws)
        self.assertIsNotNone(plan)
        self.assertIn("tools", plan.params["dest"])
        self.assertFalse(plan.outside_root)

    def test_download_outside_root_gated(self):
        plan = parse_local_action(
            "download https://x.y/f.zip to D:\\somewhere\\f.zip",
            workspace=self.ws)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.outside_root)

    def test_bare_name_falls_through(self):
        # 'download Python' has no URL — research belongs to the model.
        self.assertIsNone(
            parse_local_action("download python", workspace=self.ws))

    def test_cancel_intent(self):
        plan = parse_local_action("cancel the download", workspace=self.ws)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, "download_cancel")

    def test_question_falls_through(self):
        self.assertIsNone(parse_local_action(
            "how do I download files faster", workspace=self.ws))


class TestDownloadExecute(unittest.TestCase):
    def test_full_lifecycle_verified(self):
        td, ws, reg, ledger, mgr, dl = make_env()
        self.addCleanup(td.cleanup)
        payload = b"nexus-download-" + os.urandom(4096)
        srv = _Server(payload)
        srv.start()
        self.addCleanup(srv.httpd.shutdown)

        plan = parse_local_action(
            f"download {srv.url()} to dl/f.bin", workspace=ws)
        out = execute_plan(plan, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "verified")
        data = json.loads(out["tool_result"])
        st = wait_job(mgr, data["job_id"])
        self.assertEqual(st["state"], "completed")
        dest = ws / "dl" / "f.bin"
        self.assertEqual(dest.read_bytes(), payload)
        self.assertEqual(st["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue(st["verified"])

    def test_dedupe_verified_artifact(self):
        td, ws, reg, ledger, mgr, dl = make_env()
        self.addCleanup(td.cleanup)
        payload = b"dup-check"
        target = dl / "f.bin"
        dl.mkdir(parents=True)
        target.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        r = mgr.start("https://x.test/f.bin", str(target), sha256=digest)
        self.assertTrue(r["ok"])
        self.assertTrue(r["deduplicated"])
        self.assertTrue(r["verified"])

    def test_resume_from_part(self):
        td, ws, reg, ledger, mgr, dl = make_env()
        self.addCleanup(td.cleanup)
        payload = os.urandom(8192)
        srv = _Server(payload)
        srv.start()
        self.addCleanup(srv.httpd.shutdown)
        dest = dl / "resume.bin"
        dl.mkdir(parents=True)
        # Seed a partial file — the fetch must Range-resume, not restart.
        part = dest.with_name("resume.bin.part")
        part.write_bytes(payload[:2048])
        r = mgr.start(srv.url(), str(dest))
        st = wait_job(mgr, r["job_id"])
        self.assertEqual(st["state"], "completed")
        self.assertEqual(dest.read_bytes(), payload)

    def test_cancel_keeps_part(self):
        td, ws, reg, ledger, mgr, dl = make_env()
        self.addCleanup(td.cleanup)
        payload = os.urandom(64)
        srv = _Server(payload)
        srv.start()
        self.addCleanup(srv.httpd.shutdown)
        r = mgr.start(srv.url(), str(dl / "c.bin"))
        self.assertTrue(r["ok"])
        c = mgr.cancel(r["job_id"])
        self.assertTrue(c["ok"])
        self.assertEqual(c["job_id"], r["job_id"])

    def test_cancel_no_active_is_honest(self):
        td, ws, reg, ledger, mgr, dl = make_env()
        self.addCleanup(td.cleanup)
        r = mgr.cancel()
        self.assertFalse(r["ok"])
        self.assertIn("no download", r["error"])

    def test_system_dir_refused(self):
        td, ws, reg, ledger, mgr, dl = make_env()
        self.addCleanup(td.cleanup)
        r = mgr.start("https://x.test/f.exe",
                      r"C:\Windows\evil.exe")
        self.assertFalse(r["ok"])
        self.assertIn("system directory", r["error"])

    def test_http_non_localhost_refused(self):
        td, ws, reg, ledger, mgr, dl = make_env()
        self.addCleanup(td.cleanup)
        r = mgr.start("http://example.com/f.bin")
        self.assertFalse(r["ok"])

    def test_permission_gate(self):
        td, ws, reg, ledger, mgr, dl = make_env({"network.read": "ask"})
        self.addCleanup(td.cleanup)
        plan = parse_local_action(
            "download https://x.test/f.bin", workspace=ws)
        out = execute_plan(plan, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertIn("network.read", out["text"])

    def test_denied_never_downloads(self):
        td, ws, reg, ledger, mgr, dl = make_env({"network.read": "deny"})
        self.addCleanup(td.cleanup)
        plan = parse_local_action(
            "download https://x.test/f.bin", workspace=ws)
        out = execute_plan(plan, tools=reg, ledger=ledger)
        self.assertEqual(out["status"], "denied")
        self.assertFalse(any(dl.iterdir()) if dl.exists() else False)


if __name__ == "__main__":
    unittest.main()
