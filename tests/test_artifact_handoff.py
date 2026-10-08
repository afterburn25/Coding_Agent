"""Artifact handoff — registry client views, remote provenance, the
download endpoint, and card extraction from tool results."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from localcodeagent.action_ops import (
    ActionPlan, execute_plan, parse_local_action)
from localcodeagent.artifacts import ArtifactManager


class IntentParseTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ws = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _parse(self, text):
        return parse_local_action(text, workspace=self.ws)

    def test_give_me_the_file(self):
        for text in ("give me the installer", "send me the zip",
                     "hand me the report.pdf",
                     "where's the installer",
                     "give me a download link"):
            plan = self._parse(text)
            self.assertIsNotNone(plan, text)
            self.assertEqual(plan.kind, "artifact_show", text)
            self.assertEqual(plan.tool, "artifact_show", text)

    def test_give_me_nonfile_falls_through(self):
        for text in ("give me a hint", "give me some ideas",
                     "hand me a wrench"):
            self.assertIsNone(self._parse(text), text)

    def test_upload_to_github(self):
        plan = self._parse("upload the installer to github")
        self.assertEqual(plan.kind, "github_upload")
        self.assertEqual(plan.tool, "github_upload_release_asset")
        self.assertEqual(plan.permission, "github.write")
        self.assertEqual(plan.params["name"], "installer")
        plan = self._parse(
            "upload the zip to github release v0.34.0")
        self.assertEqual(plan.params["tag"], "v0.34.0")
        plan = self._parse("put the installer on the release")
        self.assertEqual(plan.kind, "github_upload")
        # Explicit repo targets win over the connected remote.
        plan = self._parse(
            "upload the zip to github repo afterburn25/nexus-dogfood "
            "release v0.0.1")
        self.assertEqual(plan.params["repo"], "afterburn25/nexus-dogfood")
        self.assertEqual(plan.params["tag"], "v0.0.1")

    def test_cancel_the_upload(self):
        for text in ("cancel the upload", "stop the github upload",
                     "abort the upload"):
            plan = self._parse(text)
            self.assertIsNotNone(plan, text)
            self.assertEqual(plan.kind, "github_upload_cancel", text)
            self.assertEqual(plan.tool, "github_cancel_upload", text)
            # Aborting a side-effect must never pause for write approval.
            self.assertEqual(plan.permission, "github.read", text)
        # Unrelated cancels stay out of this lane.
        self.assertNotEqual(
            (self._parse("cancel the download") or
             type("_", (), {"kind": ""})).kind, "github_upload_cancel")

    def test_download_from_github(self):
        plan = self._parse(
            "download the installer from github release v0.34.0")
        self.assertEqual(plan.kind, "github_download")
        self.assertEqual(plan.tool, "github_download_release_asset")
        self.assertEqual(plan.params["tag"], "v0.34.0")
        plan = self._parse("get the artifact from github")
        self.assertEqual(plan.kind, "github_download")
        plan = self._parse(
            "download the zip from github repo afterburn25/nexus-dogfood "
            "release v0.0.1")
        self.assertEqual(plan.params["repo"], "afterburn25/nexus-dogfood")

    def test_run_artifact_from_build(self):
        plan = self._parse(
            "download the installer artifact from the latest build")
        self.assertEqual(plan.kind, "github_download")
        self.assertEqual(plan.tool, "github_download_run_artifact")
        plan = self._parse("get the artifact from the latest run")
        self.assertEqual(plan.tool, "github_download_run_artifact")

    def test_repo_file(self):
        plan = self._parse("get README.md from the repository")
        self.assertEqual(plan.kind, "github_file")
        self.assertEqual(plan.tool, "github_get_repo_file")
        self.assertEqual(plan.params["path"], "README.md")


def _make_file(root: Path, name: str, data: bytes) -> Path:
    p = root / name
    p.write_bytes(data)
    return p


class ArtifactManagerViewTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._td.name)
        self.mgr = ArtifactManager(root / "arts")
        self.file = _make_file(root, "report.txt", b"hello world")

    def tearDown(self):
        self._td.cleanup()

    def test_client_view_hides_path(self):
        rec = self.mgr.register(self.file, creator="agent")
        view = self.mgr.client_view(rec["id"])
        self.assertEqual(view["filename"], "report.txt")
        self.assertTrue(view["verified"])
        self.assertEqual(view["size"], 11)
        self.assertEqual(view["download_url"],
                         f"/api/artifacts/{rec['id']}/download")
        self.assertNotIn(str(self.file), json.dumps(view))

    def test_attach_remote_sanitizes(self):
        rec = self.mgr.register(self.file)
        out = self.mgr.attach_remote(rec["id"], {
            "provider": "github", "repository": "o/r",
            "kind": "release_asset", "asset_id": 5,
            "web_url": "https://github.com/o/r/releases/download/t/f",
            "download_url": "javascript:alert(1)",  # dropped
            "token": "secret"})  # dropped
        remote = out["remote"]
        self.assertEqual(remote["repository"], "o/r")
        self.assertIn("github.com", remote["web_url"])
        self.assertNotIn("download_url", remote)
        self.assertNotIn("token", remote)

    def test_retention_classes(self):
        rec = self.mgr.register(self.file)
        self.assertEqual(rec["retention"], "user")
        view = self.mgr.client_view(rec)
        self.assertEqual(view["retention"], "user")
        self.assertFalse(view["pinned"])
        # Publishing promotes to release; pinning wins over everything.
        self.mgr.attach_remote(rec["id"], {
            "provider": "github", "web_url": "https://github.com/o/r"})
        self.assertEqual(self.mgr.get(rec["id"])["retention"], "release")
        self.mgr.pin(rec["id"])
        row = self.mgr.get(rec["id"])
        self.assertEqual(row["retention"], "pinned")
        self.assertTrue(row["pinned"])
        self.assertTrue(self.mgr.client_view(rec["id"])["pinned"])

    def test_find_latest_and_name(self):
        self.mgr.register(self.file)
        other = _make_file(Path(self._td.name), "Setup.exe", b"x" * 4)
        self.mgr.register(other, kind="installer")
        self.assertEqual(self.mgr.find("latest")[0]["name"], "Setup.exe")
        self.assertEqual(self.mgr.find("setup")[0]["name"], "Setup.exe")
        self.assertEqual(self.mgr.find("installer")[0]["name"],
                         "Setup.exe")
        self.assertEqual(self.mgr.find("nothing-here"), [])

    def test_verify_detects_tamper_and_missing(self):
        rec = self.mgr.register(self.file)
        self.assertTrue(self.mgr.verify(rec["id"])["ok"])
        self.file.write_bytes(b"tampered")
        self.assertFalse(self.mgr.verify(rec["id"])["ok"])
        self.file.unlink()
        self.assertEqual(self.mgr.verify(rec["id"])["reason"],
                         "file missing")


class ExecutePlanCardTests(unittest.TestCase):
    """execute_plan surfaces artifact cards for card-bearing results."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ws = Path(self._td.name)
        self.mgr = ArtifactManager(self.ws / "arts")

    def tearDown(self):
        self._td.cleanup()

    def _tools(self, result: str):
        from localcodeagent.tools.base import ToolRegistry, ToolSpec
        reg = ToolRegistry({"filesystem.read": "allow",
                            "filesystem.write": "allow"})
        reg.register(ToolSpec("artifact_show", "t", {},
                              "filesystem.read", lambda a: result))
        return reg

    def test_artifact_show_attaches_views(self):
        f = _make_file(self.ws, "Setup.exe", b"binary-bytes")
        rec = self.mgr.register(f, kind="installer")
        result = json.dumps({"ok": True,
                             "artifacts": [self.mgr.client_view(rec)]})
        plan = ActionPlan(kind="artifact_show", tool="artifact_show",
                          permission="filesystem.read",
                          params={"name": "setup"})
        out = execute_plan(plan, tools=self._tools(result),
                           artifacts=self.mgr)
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["artifacts"][0]["id"], rec["id"])
        self.assertIn("Setup.exe", out["text"])

    def test_artifact_show_empty_is_honest(self):
        result = json.dumps({"ok": False, "error": "no matching artifact"})
        plan = ActionPlan(kind="artifact_show", tool="artifact_show",
                          permission="filesystem.read",
                          params={"name": "ghost"})
        out = execute_plan(plan, tools=self._tools(result),
                           artifacts=self.mgr)
        self.assertEqual(out["status"], "failed")
        self.assertIn("no matching artifact", out["text"])

    def test_github_upload_card_resolves(self):
        f = _make_file(self.ws, "x.zip", b"zipdata")
        rec = self.mgr.register(f, kind="archive")
        from localcodeagent.tools.base import ToolRegistry, ToolSpec
        reg = ToolRegistry({"github.write": "allow"})
        result = json.dumps({
            "ok": True, "repository": "o/r",
            "artifact_id": rec["id"],
            "asset": {"id": 7, "name": "x.zip", "size": 7},
            "release": {"tag": "v1", "url": "https://github.com/o/r"}})
        reg.register(ToolSpec("github_upload_release_asset", "t", {},
                              "github.write", lambda a: result))
        plan = ActionPlan(kind="github_upload",
                          tool="github_upload_release_asset",
                          permission="github.write", params={})
        out = execute_plan(plan, tools=reg, artifacts=self.mgr)
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["artifacts"][0]["id"], rec["id"])
        self.assertIn("Published x.zip", out["text"])

    def test_github_upload_failure_is_honest(self):
        from localcodeagent.tools.base import ToolRegistry, ToolSpec
        reg = ToolRegistry({"github.write": "allow"})
        result = json.dumps({"ok": False, "error": "asset 'x' already "
                             "exists on release v1"})
        reg.register(ToolSpec("github_upload_release_asset", "t", {},
                              "github.write", lambda a: result))
        plan = ActionPlan(kind="github_upload",
                          tool="github_upload_release_asset",
                          permission="github.write", params={})
        out = execute_plan(plan, tools=reg, artifacts=self.mgr)
        self.assertEqual(out["status"], "failed")
        self.assertIn("already exists", out["text"])

    def test_upload_requires_approval_under_ask(self):
        from localcodeagent.tools.base import ToolRegistry, ToolSpec
        reg = ToolRegistry({"github.write": "ask"})
        reg.register(ToolSpec("github_upload_release_asset", "t", {},
                              "github.write", lambda a: "{}"))
        plan = ActionPlan(kind="github_upload",
                          tool="github_upload_release_asset",
                          permission="github.write", params={})
        out = execute_plan(plan, tools=reg, artifacts=self.mgr)
        self.assertEqual(out["status"], "awaiting_approval")
        # After approval the same plan executes — no re-ask.
        out2 = execute_plan(plan, tools=reg, approved=True,
                            artifacts=self.mgr)
        self.assertNotEqual(out2["status"], "awaiting_approval")


class ServerRouteTests(unittest.TestCase):
    """The /api/artifacts/<id> + /download routes on a real server."""

    def setUp(self):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        ws = Path(self._td.name)
        cfg = AgentConfig(
            profiles_onboarding_gate=False,
            models=[ModelProfile(id="fake", endpoint="http://127.0.0.1:1",
                                 model="m", roles=["utility"],
                                 runtime="external")],
            process_watchdog=False, research_enabled=False,
            autonomy_enabled=False)
        self.server, self.state = create_server(
            cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
        self._t = threading.Thread(target=self.server.serve_forever,
                                   daemon=True)
        self._t.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(
            lambda: (self.server.shutdown(), self.server.server_close(),
                     stop_state(self.state)))

    def tearDown(self):
        self._td.cleanup()

    def _get(self, path: str):
        return urllib.request.urlopen(self.base + path, timeout=10)

    def test_download_roundtrip(self):
        data = b"installer-bytes" * 1000
        f = _make_file(Path(self._td.name), "NexusCore-Setup.exe", data)
        rec = self.state.artifacts.register(f, kind="installer")
        # metadata view
        meta = json.loads(self._get(
            f"/api/artifacts/{rec['id']}").read())
        self.assertEqual(meta["filename"], "NexusCore-Setup.exe")
        self.assertTrue(meta["verified"])
        self.assertNotIn(str(f), json.dumps(meta))
        # bytes
        resp = self._get(f"/api/artifacts/{rec['id']}/download")
        body = resp.read()
        self.assertEqual(body, data)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn("NexusCore-Setup.exe",
                      resp.headers["Content-Disposition"])
        self.assertEqual(resp.headers["X-Content-SHA256"],
                         hashlib.sha256(data).hexdigest())

    def test_unknown_and_traversal_rejected(self):
        for path in ("/api/artifacts/art-000000000000",
                     "/api/artifacts/art-000000000000/download",
                     "/api/artifacts/..%2F..%2Fetc",
                     "/api/artifacts/%2e%2e/download"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(path)
            self.assertEqual(ctx.exception.code, 404, path)

    def test_tampered_artifact_409(self):
        f = _make_file(Path(self._td.name), "r.txt", b"original")
        rec = self.state.artifacts.register(f)
        f.write_bytes(b"tampered!")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(f"/api/artifacts/{rec['id']}/download")
        self.assertEqual(ctx.exception.code, 409)

    def test_missing_file_404(self):
        f = _make_file(Path(self._td.name), "gone.txt", b"x")
        rec = self.state.artifacts.register(f)
        f.unlink()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(f"/api/artifacts/{rec['id']}/download")
        self.assertEqual(ctx.exception.code, 404)
        meta = json.loads(self._get(
            f"/api/artifacts/{rec['id']}").read())
        self.assertFalse(meta["available"])

    def _post(self, path: str, body: dict | None = None):
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=10)

    def test_reveal_resolves_by_id_never_client_path(self):
        f = _make_file(Path(self._td.name), "show-me.txt", b"hi")
        rec = self.state.artifacts.register(f)
        with mock.patch("subprocess.Popen") as pop:
            out = json.loads(self._post(
                f"/api/artifacts/{rec['id']}/reveal",
                {"path": "C:\\Windows\\System32"}).read())
        self.assertTrue(out["ok"])
        self.assertTrue(pop.called)
        # The launched path resolves server-side from the artifact id —
        # Windows selects the file; POSIX opens its parent folder.
        # The body's "C:\Windows\System32" must never appear.
        argv = [str(x) for x in pop.call_args[0][0]]
        want = str(f) if sys.platform.startswith("win") else str(f.parent)
        self.assertIn(want, argv)
        self.assertNotIn("C:\\Windows\\System32", argv)

    def test_open_verifies_before_launch(self):
        f = _make_file(Path(self._td.name), "tool.exe", b"MZ-original")
        rec = self.state.artifacts.register(f)
        f.write_bytes(b"tampered-binary")
        with mock.patch("subprocess.Popen") as pop, \
                mock.patch.object(os, "startfile", create=True) as sf:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post(f"/api/artifacts/{rec['id']}/open")
            self.assertEqual(ctx.exception.code, 409)
        self.assertFalse(pop.called)
        self.assertFalse(sf.called)

    def test_list_never_leaks_paths(self):
        f = _make_file(Path(self._td.name), "listed.txt", b"x")
        rec = self.state.artifacts.register(f)
        out = json.loads(self._get("/api/artifacts").read())
        hit = [a for a in out["artifacts"] if a["id"] == rec["id"]]
        self.assertEqual(len(hit), 1)
        self.assertNotIn(str(f), json.dumps(out))
        self.assertNotIn("path", hit[0])
        self.assertEqual(hit[0]["download_url"],
                         f"/api/artifacts/{rec['id']}/download")

    def test_verify_cache_bypassed_by_fresh(self):
        f = _make_file(Path(self._td.name), "cache.bin", b"round-one")
        rec = self.state.artifacts.register(f)
        mgr = self.state.artifacts
        self.assertTrue(mgr.verify(rec["id"]).get("ok"))
        st = f.stat()
        # Rewrite with identical size, then restore the recorded mtime —
        # the (mtime, size) signature is unchanged, so the cached view
        # still reads clean while a fresh check sees the tampering.
        f.write_bytes(b"round-two")
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertTrue(mgr.verify(rec["id"]).get("ok"))
        self.assertFalse(mgr.verify(rec["id"], fresh=True).get("ok"))

    def test_open_reveal_unknown_and_bogus_routes(self):
        f = _make_file(Path(self._td.name), "ok.txt", b"x")
        rec = self.state.artifacts.register(f)
        f.unlink()
        for path in (f"/api/artifacts/art-000000000000/open",
                     f"/api/artifacts/art-000000000000/reveal",
                     f"/api/artifacts/{rec['id']}/open",
                     f"/api/artifacts/{rec['id']}/reveal",
                     f"/api/artifacts/{rec['id']}/bogus",
                     "/api/artifacts/%2e%2e/open"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post(path)
            self.assertEqual(ctx.exception.code, 404, path)


class ArtifactPersistenceTests(unittest.TestCase):
    """Artifact ids survive conversation save/reload for card rehydration."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        self._td.cleanup()

    def test_record_exchange_stores_artifact_ids(self):
        from localcodeagent.workflow.conversation_manager import (
            ConversationManager)
        cm = ConversationManager(Path(self._td.name) / "conv.json")
        cm.record_exchange("build it", "done",
                           artifact_ids=["art-0123456789ab"])
        msgs = cm.active().get("messages", [])
        self.assertEqual(msgs[-1]["artifact_ids"], ["art-0123456789ab"])
        # Role/content history for the model stays clean.
        hist = cm.history()
        self.assertNotIn("artifact_ids", hist[-1])

    def test_ids_extracted_from_tool_results(self):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        events = [
            {"name": "fs_archive", "result":
             "ARCHIVE_OK path=x.zip artifact_id=art-abcdef012345"},
            {"name": "release_package", "result": json.dumps(
                {"ok": True, "artifact_id": "art-111aaa222bbb"})},
        ]
        ids = AgentOrchestrator._artifact_ids_from_events(events)
        self.assertEqual(ids, ["art-abcdef012345", "art-111aaa222bbb"])


if __name__ == "__main__":
    unittest.main()
