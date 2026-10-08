"""GitHub release assets + Actions artifacts — fake REST client, real
download pipeline (local HTTP server) for the durable path."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from localcodeagent.artifacts import ArtifactManager
from localcodeagent.jobs import JobManager
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.downloads import UserDownloadManager
from localcodeagent.tools import github as gh


PERMS = {"github.read": "allow", "github.write": "allow",
         "network.read": "allow", "filesystem.read": "allow",
         "filesystem.write": "allow"}

ASSET_BYTES = b"release-asset-bytes" * 500


class FakeClient:
    """Route-table fake: (method, path) -> (data, headers) | Exception.
    upload_file is served by a stub that records the streamed body."""

    def __init__(self, routes=None, base_url="https://api.github.test"):
        self.routes = dict(routes or {})
        self.calls = []
        self.token = "tok"
        self.token_env = "T"
        self.base_url = base_url
        self.uploads = []

    @property
    def authenticated(self):
        return bool(self.token)

    def request_meta(self, method, path, *, params=None, body=None,
                     require_auth=False):
        self.calls.append((method, path))
        key = (method.upper(), path)
        if key not in self.routes:
            # try ignoring query — tests register paths bare
            key = (method.upper(), path.split("?")[0])
        val = self.routes.get(key)
        if val is None:
            raise RuntimeError(f"no route {method} {path}")
        if isinstance(val, Exception):
            raise val
        data, headers = val
        return data, headers

    def request(self, method, path, **kw):
        return self.request_meta(method, path, **kw)[0]

    def api_url(self, path):
        return self.base_url + path

    def upload_file(self, url, path, *, content_type, progress=None):
        self.uploads.append({"url": url, "path": str(path)})
        # The real client streams; the fake verifies size like the API.
        size = Path(path).stat().st_size
        route = self.routes.get(("UPLOAD", url))
        if route is None:
            return {"id": 501, "name": url.split("name=")[-1],
                    "size": size, "state": "uploaded",
                    "url": f"https://api.github.test/assets/501",
                    "browser_download_url":
                        "https://github.com/o/r/releases/download/t/f"}
        if isinstance(route, Exception):
            raise route
        return route[0]


RELEASE = {
    "id": 42, "tag_name": "v0.34.0", "name": "v0.34.0",
    "html_url": "https://github.com/o/r/releases/tag/v0.34.0",
    "upload_url": "https://uploads.github.test/repos/o/r/releases/42/"
                  "assets{?name,label}",
    "assets": [{"id": 77, "name": "Setup.exe", "size": len(ASSET_BYTES),
                "state": "uploaded", "content_type": "application/x-msdownload",
                "browser_download_url":
                    "https://github.com/o/r/releases/download/v0.34.0/Setup.exe",
                "url": "https://api.github.test/assets/77"}],
}


class _Cfg:
    github_api_url = "https://api.github.test"
    github_api_version = "v"
    github_token_env = "T"
    github_timeout = 5
    github_default_remote = "origin"


def _reg(ws, client, artifacts=None, downloads=None):
    reg = ToolRegistry(dict(PERMS))
    gh.register_github_tools(reg, ws, _Cfg(), client=client,
                             artifacts=artifacts, downloads=downloads)
    return reg


def _exec(reg, name, args):
    return json.loads(reg.get(name).handler(args))


class ReleaseToolTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ws = Path(self._td.name)
        self.artifacts = ArtifactManager(self.ws / "arts")

    def tearDown(self):
        self._td.cleanup()

    def test_list_and_get_release(self):
        client = FakeClient({
            ("GET", "/repos/o/r/releases"): ([RELEASE], {}),
            ("GET", "/repos/o/r/releases/tags/v0.34.0"): (RELEASE, {}),
            ("GET", "/repos/o/r/releases/42/assets"):
                (RELEASE["assets"], {}),
        })
        reg = _reg(self.ws, client)
        out = _exec(reg, "github_list_releases", {"repo": "o/r"})
        self.assertEqual(out["releases"][0]["tag"], "v0.34.0")
        self.assertEqual(out["releases"][0]["assets"][0]["name"],
                         "Setup.exe")
        out = _exec(reg, "github_get_release",
                    {"repo": "o/r", "tag": "v0.34.0"})
        self.assertEqual(out["id"], 42)
        out = _exec(reg, "github_list_release_assets",
                    {"repo": "o/r", "tag": "v0.34.0"})
        self.assertEqual(out["assets"][0]["id"], 77)

    def test_create_release_journals(self):
        client = FakeClient({
            ("POST", "/repos/o/r/releases"): (
                {"id": 43, "tag_name": "v1.0.0", "name": "v1.0.0",
                 "html_url": "https://github.com/o/r/releases/tag/v1.0.0"},
                {}),
        })
        reg = _reg(self.ws, client)
        out = _exec(reg, "github_create_release",
                    {"repo": "o/r", "tag": "v1.0.0"})
        self.assertEqual(out["id"], 43)

    def test_upload_verifies_and_links_provenance(self):
        f = self.ws / "Setup.exe"
        f.write_bytes(ASSET_BYTES)
        rec = self.artifacts.register(f, kind="installer")
        client = FakeClient({
            ("GET", "/repos/o/r/releases/tags/v0.34.0"): (RELEASE, {}),
            ("GET", "/repos/o/r/releases/42/assets"): ([], {}),
            ("GET", "/repos/o/r/releases/assets/501"): (
                {"id": 501, "name": "Setup.exe",
                 "size": len(ASSET_BYTES), "state": "uploaded",
                 "browser_download_url": "https://github.com/dl/Setup.exe",
                 "created_at": "2025-01-01T00:00:00Z"}, {}),
        })
        reg = _reg(self.ws, client, artifacts=self.artifacts)
        out = _exec(reg, "github_upload_release_asset",
                    {"repo": "o/r", "tag": "v0.34.0",
                     "artifact_id": rec["id"]})
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["asset"]["id"], 501)
        # Provenance: the local artifact now knows its remote copy.
        remote = self.artifacts.get(rec["id"])["remote"]
        self.assertEqual(remote["kind"], "release_asset")
        self.assertEqual(remote["asset_id"], 501)
        self.assertEqual(remote["tag"], "v0.34.0")

    def test_upload_refuses_duplicate(self):
        f = self.ws / "Setup.exe"
        f.write_bytes(ASSET_BYTES)
        rec = self.artifacts.register(f)
        client = FakeClient({
            ("GET", "/repos/o/r/releases/tags/v0.34.0"): (RELEASE, {}),
            ("GET", "/repos/o/r/releases/42/assets"):
                (RELEASE["assets"], {}),
        })
        reg = _reg(self.ws, client, artifacts=self.artifacts)
        out = _exec(reg, "github_upload_release_asset",
                    {"repo": "o/r", "tag": "v0.34.0",
                     "artifact_id": rec["id"]})
        self.assertFalse(out["ok"])
        self.assertIn("already exists", out["error"])
        self.assertEqual(client.uploads, [])  # nothing was sent

    def test_upload_replace_deletes_first(self):
        f = self.ws / "Setup.exe"
        f.write_bytes(ASSET_BYTES)
        rec = self.artifacts.register(f)
        client = FakeClient({
            ("GET", "/repos/o/r/releases/tags/v0.34.0"): (RELEASE, {}),
            ("GET", "/repos/o/r/releases/42/assets"):
                (RELEASE["assets"], {}),
            ("DELETE", "/repos/o/r/releases/assets/77"): (None, {}),
            ("GET", "/repos/o/r/releases/assets/501"): (
                {"id": 501, "name": "Setup.exe",
                 "size": len(ASSET_BYTES), "state": "uploaded",
                 "browser_download_url": "https://github.com/dl/Setup.exe"},
                {}),
        })
        reg = _reg(self.ws, client, artifacts=self.artifacts)
        out = _exec(reg, "github_upload_release_asset",
                    {"repo": "o/r", "tag": "v0.34.0",
                     "artifact_id": rec["id"], "replace": True})
        self.assertTrue(out["ok"])
        self.assertIn(("DELETE", "/repos/o/r/releases/assets/77"),
                      client.calls)

    def test_upload_rejects_tampered_artifact(self):
        f = self.ws / "Setup.exe"
        f.write_bytes(ASSET_BYTES)
        rec = self.artifacts.register(f)
        f.write_bytes(b"tampered")  # hash no longer matches
        client = FakeClient({
            ("GET", "/repos/o/r/releases/tags/v0.34.0"): (RELEASE, {}),
        })
        reg = _reg(self.ws, client, artifacts=self.artifacts)
        with self.assertRaises(RuntimeError):
            reg.get("github_upload_release_asset").handler(
                {"repo": "o/r", "tag": "v0.34.0",
                 "artifact_id": rec["id"]})
        self.assertEqual(client.uploads, [])

    def test_delete_asset_requires_confirm(self):
        client = FakeClient({
            ("DELETE", "/repos/o/r/releases/assets/77"): (None, {}),
        })
        reg = _reg(self.ws, client)
        with self.assertRaises(ValueError):
            reg.get("github_delete_release_asset").handler(
                {"repo": "o/r", "asset_id": 77})
        out = _exec(reg, "github_delete_release_asset",
                    {"repo": "o/r", "asset_id": 77, "confirm": True})
        self.assertTrue(out["ok"])

    def test_permission_gates(self):
        reg = ToolRegistry({"github.write": "deny"})
        gh.register_github_tools(reg, self.ws, _Cfg(),
                                 client=FakeClient())
        out = reg.execute("github_upload_release_asset", {"path": "x"})
        self.assertTrue(out.startswith("PERMISSION_DENIED"))


class AuthFailureTests(unittest.TestCase):
    """Disconnected / invalid token / scope / rate-limit surface as
    honest errors — never a fake success link."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ws = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, name, args, client=None):
        reg = _reg(self.ws, client or FakeClient())
        return reg.execute(name, args)

    def test_disconnected_account(self):
        client = FakeClient()
        client.token = ""
        out = self._run("github_list_releases",
                        {"repo": "o/r"}, client)
        self.assertIn("ERROR", out)
        out = self._run("github_upload_release_asset",
                        {"repo": "o/r", "tag": "v1", "path": "x"}, client)
        self.assertTrue(
            out.startswith(("ERROR", "PERMISSION_DENIED")))

    def test_invalid_token_401(self):
        client = FakeClient({
            ("GET", "/repos/o/r/releases"): (
                RuntimeError("GitHub API 401: Bad credentials"), {}),
        })
        client.routes[("GET", "/repos/o/r/releases")] = \
            RuntimeError("GitHub API 401: Bad credentials")
        out = self._run("github_list_releases", {"repo": "o/r"}, client)
        self.assertIn("401", out)
        self.assertIn("Bad credentials", out)

    def test_insufficient_scope_403(self):
        client = FakeClient()
        client.routes[("GET", "/repos/o/r/releases/tags/v1")] = \
            RuntimeError("GitHub API 403: Resource not accessible")
        out = self._run("github_get_release",
                        {"repo": "o/r", "tag": "v1"}, client)
        self.assertIn("403", out)

    def test_rate_limit_429_is_honest(self):
        client = FakeClient()
        client.routes[("GET", "/repos/o/r/releases")] = \
            RuntimeError("GitHub API 429: rate limit exceeded")
        out = self._run("github_list_releases", {"repo": "o/r"}, client)
        self.assertIn("429", out)
        self.assertNotIn('"ok": true', out)


class _AssetServer(BaseHTTPRequestHandler):
    bytes_to_serve = ASSET_BYTES

    def do_GET(self):
        body = self.bytes_to_serve
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class DownloadToolTests(unittest.TestCase):
    """Release-asset + run-artifact downloads through the real durable
    download manager against a local HTTP server."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ws = Path(self._td.name)
        self.dl_dir = self.ws / "Downloads"
        self.artifacts = ArtifactManager(self.ws / "arts")
        self.jobs = JobManager(self.ws / "jobs")
        self.downloads = UserDownloadManager(self.jobs, self.dl_dir)
        self.done = []
        self.downloads.on_done = self.done.append
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _AssetServer)
        threading.Thread(target=self.srv.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self._td.cleanup()

    def _client(self):
        # api_url() must resolve to the test server so the durable
        # downloader (localhost http allowed) can fetch.
        base = f"http://127.0.0.1:{self.port}"
        return FakeClient({
            ("GET", "/repos/o/r/releases/tags/v0.34.0"): (RELEASE, {}),
            ("GET", "/repos/o/r/releases/42/assets"):
                (RELEASE["assets"], {}),
            ("GET", "/repos/o/r/actions/runs"): (
                {"workflow_runs": [{"id": 9, "name": "build",
                                    "status": "completed",
                                    "conclusion": "success",
                                    "html_url": "https://github.com/o/r/"
                                                "actions/runs/9"}]}, {}),
            ("GET", "/repos/o/r/actions/runs/9/artifacts"): (
                {"artifacts": [{"id": 55, "name": "nexus-build",
                                "size_in_bytes": len(ASSET_BYTES),
                                "expired": False}]}, {}),
        }, base_url=base)

    def _wait(self, job_id, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            job = self.jobs.get(job_id)
            if job.state in ("completed", "failed", "cancelled"):
                return job
            time.sleep(0.1)
        raise AssertionError("job did not finish")

    def test_release_asset_download_registers_artifact(self):
        client = self._client()
        reg = _reg(self.ws, client, artifacts=self.artifacts,
                   downloads=self.downloads)
        out = _exec(reg, "github_download_release_asset",
                    {"repo": "o/r", "tag": "v0.34.0", "asset": "Setup.exe"})
        self.assertTrue(out["ok"])
        job = self._wait(out["job_id"])
        self.assertEqual(job.state, "completed")
        dest = Path(job.metadata["dest"])
        self.assertEqual(dest.read_bytes(), ASSET_BYTES)
        self.assertEqual(dest.name, "Setup.exe")
        # The on_done hook ran with provenance metadata.
        self.assertEqual(len(self.done), 1)
        self.assertEqual(self.done[0]["artifact"]["tool"],
                         "github_download_release_asset")
        self.assertEqual(
            self.done[0]["artifact"]["remote"]["kind"],
            "release_asset")

    def test_run_artifact_download(self):
        client = self._client()
        reg = _reg(self.ws, client, artifacts=self.artifacts,
                   downloads=self.downloads)
        out = _exec(reg, "github_list_run_artifacts", {"repo": "o/r"})
        self.assertEqual(out["run_id"], 9)
        self.assertEqual(out["artifacts"][0]["name"], "nexus-build")
        out = _exec(reg, "github_download_run_artifact",
                    {"repo": "o/r", "name": "nexus-build"})
        self.assertTrue(out["ok"])
        job = self._wait(out["job_id"])
        self.assertEqual(job.state, "completed")
        self.assertTrue(job.metadata["dest"].endswith("nexus-build.zip"))
        self.assertTrue(
            self.done[0]["artifact"]["extract_zip"])

    def test_expired_artifact_refused(self):
        client = self._client()
        client.routes[("GET", "/repos/o/r/actions/runs/9/artifacts")] = (
            {"artifacts": [{"id": 55, "name": "old",
                            "expired": True,
                            "expires_at": "2025-01-01"}]}, {})
        reg = _reg(self.ws, client, artifacts=self.artifacts,
                   downloads=self.downloads)
        with self.assertRaises(RuntimeError) as ctx:
            reg.get("github_download_run_artifact").handler(
                {"repo": "o/r", "name": "old"})
        self.assertIn("expired", str(ctx.exception))
        self.assertEqual(self.jobs.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()
