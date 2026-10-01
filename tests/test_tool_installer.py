from __future__ import annotations

import hashlib
import json
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from localcodeagent.jobs import JobManager
from localcodeagent.tools.downloads import ToolDownloadManager
from localcodeagent.tools.plugins import PluginManifest


class _FileHandler(BaseHTTPRequestHandler):
    """Serves test fixtures; /slow streams bytes with delays for cancel tests.
    Honors Range requests so resume behavior is observable."""

    payload: bytes = b""
    chunk_delay: float = 0.0
    ranges_seen: list = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        offset = 0
        range_header = self.headers.get("Range") or ""
        if range_header.startswith("bytes="):
            try:
                offset = int(range_header[6:].split("-")[0])
            except ValueError:
                offset = 0
            if offset:
                type(self).ranges_seen.append(range_header)
        slow = self.path.startswith("/slow")
        body = self.payload[offset:]
        self.send_response(206 if offset else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        step = 4096 if slow else len(body) or 1
        for i in range(0, len(body), step):
            if slow and self.chunk_delay:
                time.sleep(self.chunk_delay)
            try:
                self.wfile.write(body[i:i + step])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

    def log_message(self, *args):  # silence
        pass


def _make_zip(path: Path, members: dict[str, bytes]) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path.read_bytes()


def _wait(pred, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.03)
    return False


class ToolDownloadManagerTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.jobs = JobManager(self.root / "jobs.json")
        self.mgr = ToolDownloadManager(self.jobs, install_root=self.root)
        self.addCleanup(self.mgr.shutdown)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _FileHandler.chunk_delay = 0.0

    def _install(self, payload: bytes, **overrides):
        _FileHandler.payload = payload
        install = {
            "method": "archive",
            "url": f"http://127.0.0.1:{self.port}/pkg.zip",
            "format": "zip",
            "dest": "tools_x",
        }
        install.update(overrides)
        return self.mgr.install("demo", "Demo Tool", install, version="1.0"), install

    def _wait_job(self, job_id: str, states=("completed", "failed", "cancelled"), timeout=15.0):
        ok = _wait(lambda: self.jobs.get(job_id).state in states, timeout)
        self.assertTrue(ok, f"job {job_id} never reached {states}")
        return self.jobs.get(job_id)

    def test_archive_install_downloads_extracts_and_marks_installed(self):
        blob = _make_zip(self.root / "pkg.zip", {
            "demo/main.py": b"print('hi')\n",
            "demo/lib/util.txt": b"data",
        })
        result, _ = self._install(blob, sha256=hashlib.sha256(blob).hexdigest())
        self.assertTrue(result["ok"])
        job = self._wait_job(result["job_id"])
        self.assertEqual(job.state, "completed")
        self.assertEqual((self.root / "tools_x" / "demo" / "main.py").read_text(), "print('hi')\n")
        self.assertEqual((self.root / "tools_x" / "demo" / "lib" / "util.txt").read_text(), "data")
        marker = self.root / "tools_x" / ".chatnexus-version"
        self.assertTrue(marker.is_file())
        self.assertIn("1.0", marker.read_text())

    def test_extraction_reports_current_file_and_progress_phases(self):
        seen_phases = set()
        seen_files = set()
        self.jobs.on_change = lambda evt: (
            seen_phases.add((evt["job"].get("metadata") or {}).get("phase")),
            seen_files.add((evt["job"].get("metadata") or {}).get("current_file")),
        )
        blob = _make_zip(self.root / "pkg.zip", {f"f{i}.txt": b"x" * 64 for i in range(5)})
        result, _ = self._install(blob)
        self._wait_job(result["job_id"])
        self.assertIn("downloading", seen_phases)
        self.assertIn("extracting", seen_phases)
        self.assertTrue(any(f and f.startswith("f") for f in seen_files))

    def test_sha256_mismatch_fails_without_extracting(self):
        blob = _make_zip(self.root / "pkg.zip", {"a.txt": b"x"})
        result, _ = self._install(blob, sha256="0" * 64)
        job = self._wait_job(result["job_id"])
        self.assertEqual(job.state, "failed")
        self.assertIn("SHA-256", job.error)
        self.assertFalse((self.root / "tools_x" / "a.txt").exists())

    def test_path_traversal_members_are_skipped(self):
        blob = _make_zip(self.root / "evil.zip", {
            "../escape.txt": b"bad",
            "safe/ok.txt": b"fine",
        })
        result, _ = self._install(blob)
        self._wait_job(result["job_id"])
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertEqual((self.root / "tools_x" / "safe" / "ok.txt").read_text(), "fine")

    def test_duplicate_install_reuses_active_job(self):
        blob = _make_zip(self.root / "pkg.zip", {"a.txt": b"x" * 4096})
        _FileHandler.chunk_delay = 0.05
        first, _ = self._install(blob)
        second, _ = self._install(blob)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second.get("deduplicated"))
        job = self._wait_job(first["job_id"], timeout=30)
        self.assertEqual(job.state, "completed")

    def test_cancel_stops_download_and_keeps_part_for_resume(self):
        _FileHandler.chunk_delay = 0.05
        _FileHandler.payload = b"x" * (1024 * 1024)
        result, _ = self._install(
            _FileHandler.payload, url=f"http://127.0.0.1:{self.port}/slow")
        job_id = result["job_id"]
        self.assertTrue(_wait(lambda: self.jobs.get(job_id).state == "running", 10))
        self.assertTrue(self.mgr.cancel(job_id))
        job = self._wait_job(job_id, states=("cancelled",), timeout=15)
        self.assertEqual(job.state, "cancelled")
        # The .part survives so a retried install can resume with Range.
        self.assertTrue((self.root / ".agent" / "downloads" / "demo.part").is_file())

    def test_retry_resumes_partial_download(self):
        part = self.root / ".agent" / "downloads" / "demo.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        blob = _make_zip(self.root / "pkg.zip", {"demo/a.txt": b"payload"})
        # Seed a truncated .part — the retry must request a Range and append.
        part.write_bytes(blob[: len(blob) // 2])
        _FileHandler.ranges_seen.clear()
        result, _ = self._install(blob, size_bytes=len(blob),
                                  sha256=hashlib.sha256(blob).hexdigest())
        job = self._wait_job(result["job_id"])
        self.assertEqual(job.state, "completed", job.error)
        self.assertIn(f"bytes={len(blob) // 2}-", _FileHandler.ranges_seen)
        self.assertEqual((self.root / "tools_x" / "demo" / "a.txt").read_bytes(), b"payload")

    def test_uninstall_removes_dest_marker_and_part(self):
        blob = _make_zip(self.root / "pkg.zip", {"demo/a.txt": b"x"})
        result, _ = self._install(blob)
        self._wait_job(result["job_id"])
        dest = self.root / "tools_x"
        self.assertTrue((dest / ".chatnexus-version").is_file())
        part = self.root / ".agent" / "downloads" / "demo.part"
        part.write_bytes(b"partial")
        res = self.mgr.uninstall("demo", "Demo Tool", {"dest": "tools_x"})
        self.assertTrue(res["ok"], res)
        job = self._wait_job(res["job_id"])
        self.assertEqual(job.state, "completed")
        self.assertFalse(dest.exists())
        self.assertFalse(part.exists())

    def test_uninstall_refuses_during_active_install(self):
        _FileHandler.chunk_delay = 0.05
        _FileHandler.payload = b"x" * (1024 * 1024)
        result, _ = self._install(
            _FileHandler.payload, url=f"http://127.0.0.1:{self.port}/slow")
        self.assertTrue(
            _wait(lambda: self.jobs.get(result["job_id"]).state == "running", 10))
        res = self.mgr.uninstall("demo", "Demo Tool", {"dest": "tools_x"})
        self.assertFalse(res["ok"])
        self.assertIn("install is running", res["error"])
        self.mgr.cancel(result["job_id"])
        self._wait_job(result["job_id"])

    def test_uninstall_dest_must_stay_inside_install_root(self):
        res = self.mgr.uninstall("demo", "Demo Tool", {"dest": "../escape"})
        self.assertFalse(res["ok"])
        self.assertTrue((self.root.parent).is_dir())  # nothing deleted

    def _make_7z(self) -> bytes:
        py7zr = __import__("py7zr")
        src = self.root / "mk"
        src.mkdir(exist_ok=True)
        (src / "hello.txt").write_text("hi from 7z")
        with py7zr.SevenZipFile(self.root / "pkg.7z", "w") as zf:
            zf.write(src / "hello.txt", "mk/hello.txt")
        return (self.root / "pkg.7z").read_bytes()

    def test_7z_extracts_and_completes(self):
        try:
            blob = self._make_7z()
        except ImportError:
            self.skipTest("py7zr not installed")
        result, _ = self._install(
            blob, url=f"http://127.0.0.1:{self.port}/pkg.7z", format="7z",
            sha256=hashlib.sha256(blob).hexdigest())
        job = self._wait_job(result["job_id"])
        self.assertEqual(job.state, "completed", job.error)
        self.assertEqual((self.root / "tools_x" / "mk" / "hello.txt").read_text(), "hi from 7z")

    def test_7z_py7zr_fallback_uses_valid_callback(self):
        """Regression: py7zr >=1.0 requires an ExtractCallback subclass."""
        try:
            blob = self._make_7z()
        except ImportError:
            self.skipTest("py7zr not installed")
        from unittest.mock import patch
        with patch.object(ToolDownloadManager, "_system_tar", lambda: None):
            result, _ = self._install(
                blob, url=f"http://127.0.0.1:{self.port}/pkg.7z", format="7z",
                sha256=hashlib.sha256(blob).hexdigest())
        job = self._wait_job(result["job_id"])
        self.assertEqual(job.state, "completed", job.error)
        self.assertEqual((self.root / "tools_x" / "mk" / "hello.txt").read_text(), "hi from 7z")

    def test_http_urls_require_localhost(self):
        result, _ = self._install(b"x", url="http://example.com/pkg.zip")
        self.assertFalse(result["ok"])
        self.assertIn("HTTPS", result["error"])

    def test_detect_files_marks_manifest_installed(self):
        blob = _make_zip(self.root / "pkg.zip", {"demo/main.py": b"x", "demo/py.exe": b"y"})
        result, _ = self._install(blob)
        self._wait_job(result["job_id"])
        manifest = PluginManifest.from_dict({
            "id": "demo", "name": "Demo",
            "detect": {"files": ["tools_x/demo/main.py", "tools_x/demo/py.exe"]},
        })
        self.assertTrue(manifest.is_installed(self.root))
        missing = PluginManifest.from_dict({
            "id": "demo", "name": "Demo",
            "detect": {"files": ["tools_x/demo/main.py", "tools_x/absent.bin"]},
        })
        self.assertFalse(missing.is_installed(self.root))
        self.assertFalse(manifest.is_installed(self.root / "nonexistent"))


class ManagedPythonTests(unittest.TestCase):
    def test_pip_install_uses_sys_executable_when_not_frozen(self):
        import sys
        from localcodeagent.tools.plugins import install_command
        cmd = install_command({"method": "pip", "package": "demo-pkg"})
        self.assertEqual(cmd[:3], [sys.executable, "-m", "pip"])
        self.assertEqual(cmd[-1], "demo-pkg")

    def test_frozen_build_uses_managed_runtime_python(self):
        import sys
        from localcodeagent.tools import plugins
        with TemporaryDirectory() as td:
            root = Path(td)
            comfy_py = root / "ComfyUI_windows_portable" / "python_embeded" / "python.exe"
            comfy_py.parent.mkdir(parents=True)
            comfy_py.write_text("")
            old = getattr(sys, "frozen", None)
            sys.frozen = True
            try:
                self.assertEqual(plugins.managed_python(root), str(comfy_py))
                cmd = plugins.install_command(
                    {"method": "pip", "package": "piper-tts"}, install_root=root)
                self.assertEqual(cmd[:3], [str(comfy_py), "-m", "pip"])
            finally:
                if old is None:
                    del sys.frozen
                else:
                    sys.frozen = old

    def test_frozen_build_without_runtime_returns_none(self):
        import sys
        from localcodeagent.tools import plugins
        with TemporaryDirectory() as td:
            old = getattr(sys, "frozen", None)
            sys.frozen = True
            try:
                self.assertIsNone(plugins.managed_python(Path(td)))
                self.assertIsNone(plugins.install_command(
                    {"method": "pip", "package": "x"}, install_root=Path(td)))
            finally:
                if old is None:
                    del sys.frozen
                else:
                    sys.frozen = old


class ServerInstallFlowTests(unittest.TestCase):
    """AppState.install_tool → archive job → registry install_status flips."""

    def test_archive_install_via_app_state_marks_tool_installed(self):
        from localcodeagent.config import AgentConfig
        from localcodeagent.server import AppState

        with TemporaryDirectory() as td:
            root = Path(td)
            blob = _make_zip(root / "pkg.zip", {"demo/main.py": b"x"})
            _FileHandler.payload = blob
            _FileHandler.chunk_delay = 0.0
            server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                manifests = root / "tools" / "manifests"
                manifests.mkdir(parents=True)
                (manifests / "demo.json").write_text(json.dumps({
                    "id": "demo", "name": "Demo Tool", "version": "2.0",
                    "install": {
                        "method": "archive",
                        "url": f"http://127.0.0.1:{port}/pkg.zip",
                        "format": "zip", "dest": "demo_tool",
                    },
                    "detect": {"files": ["demo_tool/demo/main.py"]},
                }), encoding="utf-8")
                from localcodeagent.config import ModelProfile
                state = AppState(
                    AgentConfig(
                        models=[ModelProfile(
                            id="fake", endpoint="http://127.0.0.1:1/v1",
                            model="fake-model",
                            roles=["primary_coder", "utility", "fast_coder"],
                            runtime="external")],
                        process_watchdog=False, research_enabled=False),
                    root / "workspace", root)
            except Exception:
                server.shutdown()
                server.server_close()
                raise
            try:
                spec = state.tools.get("demo")
                self.assertIsNotNone(spec)
                self.assertEqual(spec.install_status, "missing")

                first = state.install_tool("demo")
                if first.get("needs_approval"):
                    first = state.install_tool("demo", approve=True)
                self.assertTrue(first.get("ok"), first)
                job_id = first["job_id"]

                ok = _wait(
                    lambda: state.jobs.get(job_id).state in {"completed", "failed"},
                    15)
                self.assertTrue(ok)
                self.assertEqual(state.jobs.get(job_id).state, "completed",
                                 state.jobs.get(job_id).error)
                # on_done refreshes install status asynchronously
                self.assertTrue(_wait(
                    lambda: state.tools.get("demo").install_status == "installed", 5))
                self.assertTrue((root / "demo_tool" / ".chatnexus-version").is_file())

                # Update detection: marker version vs manifest version.
                payload = state.tools.manifest("demo")
                self.assertEqual(payload["installed_version"], "2.0")
                self.assertFalse(payload["update_available"])
                (root / "demo_tool" / ".chatnexus-version").write_text("1.0\n", encoding="utf-8")
                self.assertTrue(state.tools.manifest("demo")["update_available"])

                # Disk preflight: absurd size_bytes refuses before any download.
                orig_manifest = state.tools.manifest
                huge_install = {**orig_manifest("demo")["install"], "size_bytes": 10**15}
                state.tools.manifest = lambda name: {
                    **orig_manifest(name), "install": huge_install}
                try:
                    denied = state.install_tool("demo", approve=True)
                finally:
                    state.tools.manifest = orig_manifest
                self.assertFalse(denied["ok"])
                self.assertIn("disk space", denied["error"])

                # Full lifecycle: uninstall removes files and status flips back.
                rm = state.uninstall_tool("demo")
                if rm.get("needs_approval"):
                    rm = state.uninstall_tool("demo", approve=True)
                self.assertTrue(rm.get("ok"), rm)
                ok = _wait(
                    lambda: state.jobs.get(rm["job_id"]).state in {"completed", "failed"},
                    15)
                self.assertTrue(ok)
                self.assertEqual(state.jobs.get(rm["job_id"]).state, "completed",
                                 state.jobs.get(rm["job_id"]).error)
                self.assertTrue(_wait(
                    lambda: state.tools.get("demo").install_status == "missing", 5))
                self.assertFalse((root / "demo_tool").exists())
            finally:
                state.tool_downloads.shutdown()
                server.shutdown()
                server.server_close()


class ManifestDetectTests(unittest.TestCase):
    def test_comfyui_manifest_has_archive_install_and_detection(self):
        raw = json.loads(
            (Path(__file__).resolve().parents[1] / "tools" / "manifests" / "comfyui.json")
            .read_text(encoding="utf-8"))
        m = PluginManifest.from_dict(raw)
        self.assertEqual(m.install.get("method"), "archive")
        self.assertEqual(m.install.get("format"), "7z")
        self.assertTrue(m.install.get("url", "").startswith("https://"))
        self.assertTrue(m.install.get("sha256"))
        self.assertIn("ComfyUI_windows_portable/ComfyUI/main.py", m.detect_files)


if __name__ == "__main__":
    unittest.main()
