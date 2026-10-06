"""InvokeAI backend adapter, runtime discovery, routing, and error tests."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from localcodeagent.image.catalog import classify_model
from localcodeagent.image.errors import describe_image_error
from localcodeagent.image.invokeai import InvokeAIBackend
from localcodeagent.image.invokeai_runtime import InvokeAIRuntime
from localcodeagent.image.manager import ImageManager
from localcodeagent.image.sampling import SamplingAdvisor
from localcodeagent.image.types import ImageModelProfile, ImageRequest


class _InvokeStub(BaseHTTPRequestHandler):
    """Minimal InvokeAI API stub — version probe, models, queue lifecycle."""

    uploads: list[dict] = []
    enqueued: list[dict] = []
    enqueued_items: list[str] = []
    items: dict[str, dict] = {}
    model_rows: list[dict] = []
    cancelled: list[str] = []

    def log_message(self, *args):  # silence
        pass

    def _send(self, payload, code=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    install_jobs: dict[int, dict] = {}
    _install_next: list[int] = [100]

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/v2/models/install":
            self._send(list(self.install_jobs.values()))
            return
        if path.startswith("/api/v2/models/install/"):
            jid = int(path.rsplit("/", 1)[-1])
            self._send(self.install_jobs.get(jid) or {"error": "gone"},
                       200 if jid in self.install_jobs else 404)
            return
        if path == "/api/v1/app/version":
            self._send({"version": "6.5.0"})
            return
        if path == "/api/v1/app/runtime_config":
            self._send({"version": "6.5.0", "outputs_path": "outputs"})
            return
        if path.startswith("/api/v2/models") or path.startswith("/api/v1/models"):
            self._send({"models": self.model_rows})
            return
        if path.startswith("/api/v1/queue/default/i/"):
            item_id = path.rsplit("/", 1)[-1]
            self._send(self.items.get(item_id) or {"status": "canceled"})
            return
        if path.startswith("/api/v1/images/i/") and path.endswith("/full"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            body = b"\x89PNG fake"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/v1/images/upload":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            self.uploads.append({"length": length})
            self._send({"image_name": "uploaded.png"})
            return
        if path == "/api/v2/models/install":
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            jid = self._install_next[0]
            self._install_next[0] += 1
            source = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query).get("source", [""])[0]
            self.install_jobs[jid] = {
                "id": jid, "status": "completed", "bytes": 100,
                "bytes_total": 100, "source": source}
            self._send(self.install_jobs[jid])
            return
        if path == "/api/v1/queue/default/enqueue_batch":
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            runs = max(1, int(payload.get("runs") or 1))
            item_ids = []
            for _ in range(runs):
                item_id = f"item-{len(self.enqueued_items) + 1}"
                item_ids.append(len(self.enqueued_items) + 1)
                self.enqueued_items.append(item_id)
                self.items[item_id] = {
                    "status": "completed",
                    "session": {"results": {"l2i": {"image": {"image_name": "out.png"}}}},
                }
            self.enqueued.append(payload)
            self._send({"item_ids": item_ids,
                        "batch": {"batch_id": "b1"}, "enqueued": runs})
            return
        self._send({"error": "not found"}, 404)

    def do_PUT(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/v1/queue/default/i/") and path.endswith("/cancel"):
            self.cancelled.append(path.rsplit("/", 2)[-2])
            self._send({})
            return
        if path == "/api/v1/queue/default/clear":
            self.cancelled.append("*clear*")
            self._send({})
            return
        self._send({"error": "not found"}, 404)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/v2/models/install/"):
            jid = int(path.rsplit("/", 1)[-1])
            if jid in self.install_jobs:
                self.install_jobs[jid]["status"] = "cancelled"
            self._send({})
            return
        self._send({"error": "not found"}, 404)


class _ServerMixin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _InvokeStub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _InvokeStub.uploads.clear()
        _InvokeStub.enqueued.clear()
        _InvokeStub.enqueued_items.clear()
        _InvokeStub.items.clear()
        _InvokeStub.cancelled.clear()
        _InvokeStub.install_jobs.clear()
        _InvokeStub._install_next[0] = 100
        _InvokeStub.model_rows = [
            {"key": "k1", "hash": "h1", "name": "Juggernaut XL", "base": "sdxl",
             "type": "main", "format": "checkpoint", "path": "main/jugg.safetensors"},
            {"key": "k2", "hash": "h2", "name": "nsfw-realism", "base": "sd-1",
             "type": "main", "format": "checkpoint"},
            {"key": "l1", "hash": "lh", "name": "style-lora", "base": "sdxl",
             "type": "lora", "format": "lycoris"},
        ]


class BackendAdapterTests(_ServerMixin):
    def setUp(self):
        super().setUp()
        self.backend = InvokeAIBackend(self.endpoint)

    def test_health_reports_version(self):
        ok, detail = self.backend.health()
        self.assertTrue(ok)
        self.assertIn("6.5.0", detail)

    def test_health_offline_is_false_not_exception(self):
        dead = InvokeAIBackend("http://127.0.0.1:9")
        ok, detail = dead.health()
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_models_listing(self):
        models = self.backend.models()
        self.assertEqual(len(models), 3)
        self.assertEqual(models[0]["name"], "Juggernaut XL")

    def test_submit_builds_sdxl_graph(self):
        spec = {"prompt": "a cat", "model": _InvokeStub.model_rows[0],
                "width": 1024, "height": 1024, "seed": 7, "steps": 20,
                "guidance": 5.0, "count": 2}
        item_id = self.backend.submit(spec)
        self.assertTrue(item_id)
        # count=2 fans out into two batches so each gets a distinct seed.
        self.assertEqual(len(_InvokeStub.enqueued), 2)
        seeds = [b["batch"]["graph"]["nodes"]["noise"]["seed"]
                 for b in _InvokeStub.enqueued]
        self.assertEqual(len(set(seeds)), 2)
        graph = _InvokeStub.enqueued[0]["batch"]["graph"]
        self.assertEqual(graph["nodes"]["model_loader"]["type"], "sdxl_model_loader")
        self.assertEqual(graph["nodes"]["noise"]["seed"], 7)
        self.assertEqual(graph["nodes"]["denoise"]["steps"], 20)

    def test_submit_img2img_uses_i2l_and_denoise_start(self):
        spec = {"prompt": "darker bg", "model": _InvokeStub.model_rows[0],
                "source_image_name": "src.png", "denoise_strength": 0.6}
        self.backend.submit(spec)
        nodes = _InvokeStub.enqueued[0]["batch"]["graph"]["nodes"]
        self.assertIn("i2l", nodes)
        self.assertAlmostEqual(nodes["denoise"]["denoising_start"], 0.4)

    def test_submit_inpaint_adds_mask_node(self):
        spec = {"prompt": "fix region", "model": _InvokeStub.model_rows[0],
                "source_image_name": "src.png", "mask_image_name": "mask.png"}
        self.backend.submit(spec)
        nodes = _InvokeStub.enqueued[0]["batch"]["graph"]["nodes"]
        self.assertEqual(nodes["mask"]["type"], "create_denoise_mask")

    def test_submit_with_lora_adds_loader(self):
        spec = {"prompt": "styled", "model": _InvokeStub.model_rows[0],
                "loras": [{"model": _InvokeStub.model_rows[2], "strength": 0.7}]}
        self.backend.submit(spec)
        nodes = _InvokeStub.enqueued[0]["batch"]["graph"]["nodes"]
        self.assertEqual(nodes["lora_0"]["type"], "sdxl_lora_loader")
        self.assertAlmostEqual(nodes["lora_0"]["weight"], 0.7)

    def test_flux_base_is_honestly_unsupported(self):
        spec = {"prompt": "x", "model": {"key": "f", "base": "flux", "type": "main"}}
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.backend.submit(spec)

    def test_status_mapping_and_outputs(self):
        _InvokeStub.items["j1"] = {"status": "in_progress"}
        self.assertEqual(self.backend.status("j1")["state"], "running")
        _InvokeStub.items["j1"] = {"status": "failed", "error_message": "boom"}
        st = self.backend.status("j1")
        self.assertEqual(st["state"], "failed")
        self.assertIn("boom", st["error"])
        _InvokeStub.items["j1"] = {
            "status": "completed",
            "session": {"results": {"a": {"image": {"image_name": "x.png"}},
                                     "b": {"image": {"image_name": "y.png"}}}},
        }
        with tempfile.TemporaryDirectory() as td:
            outs = self.backend.fetch_outputs("j1", Path(td))
            self.assertEqual(len(outs), 2)
            self.assertTrue(all(p.is_file() for p in outs))

    def test_cancel_deletes_queue_item(self):
        self.backend.cancel("item-9")
        self.assertEqual(_InvokeStub.cancelled, ["item-9"])

    def test_capabilities_advertised(self):
        caps = self.backend.capabilities()
        for op in ("text_to_image", "edit_image", "inpaint", "variation"):
            self.assertIn(op, caps)
        self.assertNotIn("outpaint", caps)

    def test_scheduler_mapping(self):
        self.assertEqual(
            InvokeAIBackend._scheduler({"sampler_name": "euler_ancestral", "scheduler": "karras"}),
            "euler_a")
        self.assertEqual(
            InvokeAIBackend._scheduler({"sampler_name": "dpmpp_2m", "scheduler": "karras"}),
            "dpmpp_2m_k")
        # InvokeAI-native names pass through.
        self.assertEqual(InvokeAIBackend._scheduler({"scheduler": "unipc"}), "unipc")


class RuntimeDiscoveryTests(unittest.TestCase):
    def test_not_installed_reports_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            backend = InvokeAIBackend("http://127.0.0.1:9")
            cfg = SimpleNamespace(invokeai_dir="", invokeai_python="",
                                  invokeai_auto_start=False)
            rt = InvokeAIRuntime(base_dir=Path(td), backend=backend, config=cfg)
            with patch("localcodeagent.image.invokeai_runtime.shutil.which",
                       return_value=None):
                root, prefix = rt.discover()
            self.assertIsNone(root)
            self.assertIsNone(prefix)

    def test_discovers_venv_script(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scripts = root / "tools" / "InvokeAI" / "Scripts"
            scripts.mkdir(parents=True)
            exe = scripts / "invokeai-web.exe"
            exe.write_text("@echo off\n")
            backend = InvokeAIBackend("http://127.0.0.1:9")
            cfg = SimpleNamespace(invokeai_dir="", invokeai_python="",
                                  invokeai_auto_start=False)
            rt = InvokeAIRuntime(base_dir=root, backend=backend, config=cfg)
            found, prefix = rt.discover()
            self.assertIsNotNone(found)
            self.assertIn("invokeai-web", prefix[0])

    def test_probe_reports_installed_flag(self):
        with tempfile.TemporaryDirectory() as td:
            backend = InvokeAIBackend("http://127.0.0.1:9")
            cfg = SimpleNamespace(invokeai_dir="", invokeai_python="",
                                  invokeai_auto_start=False)
            rt = InvokeAIRuntime(base_dir=Path(td), backend=backend, config=cfg)
            with patch.object(InvokeAIRuntime, "discover", return_value=(None, None)):
                status = rt.probe()
            self.assertFalse(status["installed"])
            self.assertFalse(status["healthy"])

    def _write_registry(self, root: Path, rows: list[dict]) -> None:
        import sqlite3
        dbdir = root / "data" / "invokeai" / "databases"
        dbdir.mkdir(parents=True)
        con = sqlite3.connect(str(dbdir / "invokeai.db"))
        con.execute("create table models (id text primary key, config text)")
        for i, cfg in enumerate(rows):
            con.execute("insert into models values (?, ?)",
                        (f"m{i}", json.dumps(cfg)))
        con.commit()
        con.close()

    def _runtime(self, root: Path) -> InvokeAIRuntime:
        cfg = SimpleNamespace(invokeai_dir="", invokeai_python="",
                              invokeai_auto_start=False)
        return InvokeAIRuntime(base_dir=root,
                               backend=InvokeAIBackend("http://127.0.0.1:9"),
                               config=cfg)

    def test_registry_models_reads_offline_db(self):
        """The model pool must reflect InvokeAI's registry even while the
        server is stopped — otherwise a stopped InvokeAI is invisible to
        Auto routing and every request falls back to ComfyUI."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_registry(root, [{
                "key": "dafe3f01-8986-49ef-8ee0-2951bd47d31f",
                "name": "Juggernaut-XL_v9_RunDiffusionPhoto_v2",
                "base": "sdxl", "type": "main", "format": "checkpoint",
                "hash": "blake3:abc"}])
            rt = self._runtime(root)
            rows = rt.registry_models()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"],
                             "Juggernaut-XL_v9_RunDiffusionPhoto_v2")
            self.assertEqual(rows[0]["base"], "sdxl")

    def test_registry_models_missing_db(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._runtime(Path(td)).registry_models(), [])

    def test_invalidate_discovery_drops_cache(self):
        with tempfile.TemporaryDirectory() as td:
            rt = self._runtime(Path(td))
            with patch("localcodeagent.image.invokeai_runtime.shutil.which",
                       return_value=None):
                self.assertEqual(rt.discover(), (None, None))
            # Install lands after the first probe — invalidate and rescan.
            scripts = Path(td) / "tools" / "InvokeAI" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "invokeai-web.exe").write_text("@echo off\n")
            rt.invalidate_discovery()
            root, prefix = rt.discover()
            self.assertIsNotNone(root)
            self.assertIn("invokeai-web", prefix[0])

    def test_installed_version_from_distinfo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            site = root / "tools" / "InvokeAI" / "Lib" / "site-packages"
            (site / "invokeai-6.14.2.dist-info").mkdir(parents=True)
            (root / "tools" / "InvokeAI" / "Scripts").mkdir(parents=True)
            (root / "tools" / "InvokeAI" / "Scripts"
             / "invokeai-web.exe").write_text("@echo off\n")
            rt = self._runtime(root)
            self.assertEqual(rt.installed_version(), "6.14.2")


def _manager(root: Path, *, backend_override: str = "", config_backend: str = "auto") -> ImageManager:
    profile = ImageModelProfile(
        id="qwen", family="qwen-image", backend="comfyui",
        capabilities=["text_to_image", "image_edit", "inpaint", "background_removal"],
        workflows={"text_to_image": "qwen/generate.json",
                   "remove_background": "qwen/rembg.json"},
    )
    config = SimpleNamespace(
        image_models_dir="models/image", image_data_dir="data/image",
        image_workflows_dir="workflows/image",
        comfyui_endpoint="http://127.0.0.1:8188", comfyui_auto_start=False,
        comfyui_start_on_image_request=True, comfyui_dir="ComfyUI",
        comfyui_logs_dir=".agent/runtime", comfyui_startup_timeout=60,
        comfyui_extra_args=[],
        invokeai_endpoint="http://127.0.0.1:9", invokeai_auto_start=False,
        invokeai_start_on_image_request=True, invokeai_dir="",
        invokeai_python="", invokeai_logs_dir=".agent/runtime",
        invokeai_extra_args=[], invokeai_startup_timeout=60,
        image_resource_mode="balanced", image_auto_run_jobs=False,
        image_backend=config_backend,
    )
    return ImageManager(base_dir=root, models=[profile], config=config,
                        workspace=root / "workspace")


class ModelInstallAdapterTests(_ServerMixin):
    def test_install_model_submits_source(self):
        b = InvokeAIBackend(endpoint=self.endpoint)
        job = b.install_model("owner/repo::file.safetensors")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["source"], "owner/repo::file.safetensors")

    def test_install_job_polling_and_list(self):
        b = InvokeAIBackend(endpoint=self.endpoint)
        job = b.install_model("owner/repo")
        self.assertEqual(b.model_install_job(job["id"])["status"],
                         "completed")
        self.assertTrue(any(j["id"] == job["id"]
                            for j in b.model_install_jobs()))

    def test_cancel_install(self):
        b = InvokeAIBackend(endpoint=self.endpoint)
        job = b.install_model("owner/repo")
        self.assertTrue(b.cancel_model_install(job["id"]))
        self.assertEqual(b.model_install_job(job["id"])["status"],
                         "cancelled")


class BackendSelectionTests(unittest.TestCase):
    def test_auto_falls_back_to_comfyui_when_invokeai_absent(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m.backend = SimpleNamespace(health=lambda: (True, "ok"))
            # ComfyUI installed marker — stub discover on the runtime.
            m.backend_runtime.discover = lambda: (Path(td), "python")
            job = m.create_job(ImageRequest(prompt="a cat"))
            self.assertEqual(job.backend, "comfyui")
            self.assertTrue(any("fallback" in r.lower() or "invokeai" in r.lower()
                                for r in job.routing_reasons))

    def test_auto_prefers_invokeai_when_ready(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m.invokeai_backend = SimpleNamespace(
                health=lambda: (True, "ok"),
                models=lambda **kw: _InvokeStub.model_rows,
                capabilities=lambda: {"text_to_image", "edit_image"},
                endpoint="http://x")
            m.backend = SimpleNamespace(health=lambda: (True, "ok"))
            job = m.create_job(ImageRequest(prompt="a cat"))
            self.assertEqual(job.backend, "invokeai")
            self.assertTrue(job.model_id.startswith("invokeai:"))

    def test_auto_picks_invokeai_when_installed_but_stopped(self):
        """Regression: a stopped InvokeAI must still be routable — the
        manager reads its SQLite model registry offline so Auto can pick
        an InvokeAI model, then the job cold-starts the server."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Installed venv marker.
            scripts = root / "tools" / "InvokeAI" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "invokeai-web.exe").write_text("@echo off\n")
            # Offline registry with one fleet model.
            import sqlite3
            dbdir = root / "data" / "invokeai" / "databases"
            dbdir.mkdir(parents=True)
            con = sqlite3.connect(str(dbdir / "invokeai.db"))
            con.execute("create table models (id text primary key, config text)")
            con.execute("insert into models values (?, ?)", ("m1", json.dumps({
                "key": "dafe3f01-8986-49ef-8ee0-2951bd47d31f",
                "name": "Juggernaut-XL_v9_RunDiffusionPhoto_v2",
                "base": "sdxl", "type": "main", "format": "checkpoint"})))
            con.commit(); con.close()

            m = _manager(root)  # endpoints :9/:8188 both down
            m.backend = SimpleNamespace(health=lambda: (True, "ok"))
            m.backend_runtime.discover = lambda: (root, "python")
            job = m.create_job(ImageRequest(prompt="a cat"))
            self.assertEqual(job.backend, "invokeai")
            self.assertTrue(job.model_id.startswith("invokeai:"))
            self.assertTrue(any("invokeai" in r.lower()
                                for r in job.routing_reasons))

    def test_manual_comfyui_override(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m.invokeai_backend = SimpleNamespace(health=lambda: (True, "ok"),
                                                 models=lambda **kw: [])
            m.backend = SimpleNamespace(health=lambda: (True, "ok"))
            m.backend_runtime.discover = lambda: (Path(td), "python")
            job = m.create_job(ImageRequest(prompt="a cat",
                                            backend_override="comfyui"))
            self.assertEqual(job.backend, "comfyui")

    def test_manual_invokeai_override_errors_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m.backend = SimpleNamespace(health=lambda: (True, "ok"))
            m.backend_runtime.discover = lambda: (Path(td), "python")
            with self.assertRaises(RuntimeError) as ctx:
                m.create_job(ImageRequest(prompt="a cat",
                                          backend_override="invokeai"))
            self.assertIn("InvokeAI", str(ctx.exception))

    def test_unsupported_op_routes_comfyui(self):
        # remove_background isn't in InvokeAI's native op set — Auto routes
        # to ComfyUI even when InvokeAI is healthy.
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m.invokeai_backend = SimpleNamespace(
                health=lambda: (True, "ok"), models=lambda **kw: [],
                capabilities=lambda: {"text_to_image"})
            m.backend = SimpleNamespace(health=lambda: (True, "ok"))
            m.backend_runtime.discover = lambda: (Path(td), "python")
            job = m.create_job(ImageRequest(
                prompt="remove the background", operation="remove_background",
                source_image="x.png"))
            self.assertEqual(job.backend, "comfyui")

    def test_unknown_backend_override_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            with self.assertRaises(ValueError):
                m.create_job(ImageRequest(prompt="x", backend_override="bogus"))


class SweptStateRecoveryTests(unittest.TestCase):
    """A second InvokeAI instance sharing the data root sweeps tmp* dirs
    at startup and deletes the running server's ephemeral tensor store —
    every subsequent generation fails with 'Parent directory ... does not
    exist'. The manager must restart the backend and resubmit once."""

    def _job_manager(self, td, statuses):
        m = _manager(Path(td))
        calls = {"ensure": 0, "stop": 0, "submit": 0}
        m.invokeai_runtime = SimpleNamespace(
            ensure_ready=lambda: calls.__setitem__("ensure", calls["ensure"] + 1),
            stop=lambda: calls.__setitem__("stop", calls["stop"] + 1),
            discover=lambda: (Path(td), ["invokeai-web"]))
        states = iter(statuses)

        def _status(jid):
            s = next(states)
            if isinstance(s, BaseException):
                raise s
            return s

        m.invokeai_backend = SimpleNamespace(
            health=lambda: (True, "ok"),
            models=lambda **kw: [{"id": "m1", "key": "k1", "name": "m",
                                  "type": "main", "base": "sdxl"}],
            capabilities=lambda: {"text_to_image"},
            submit=lambda spec: calls.__setitem__("submit", calls["submit"] + 1) or "b1",
            status=_status,
            fetch_outputs=lambda jid, dest: [],
            endpoint="http://x")
        m.backend = SimpleNamespace(health=lambda: (False, "down"))
        m._invokeai_spec = lambda j, r, p: {"model": "m"}
        m.router.get_profile = lambda mid: ImageModelProfile(
            id="invokeai:m1", family="sdxl", backend="invokeai",
            capabilities=["text_to_image"], workflows={})
        job = m.create_job(ImageRequest(prompt="a cat"))
        return m, job, calls

    def test_swept_tmpdir_restarts_backend_and_retries(self):
        with tempfile.TemporaryDirectory() as td:
            m, job, calls = self._job_manager(td, [
                {"state": "failed",
                 "error": "Parent directory D:\\x\\outputs\\tensors\\tmpabc does not exist"},
                {"state": "finished"},
            ])
            m._run_invokeai_job(job)
            self.assertEqual(job.state, "finished")
            self.assertEqual(calls["submit"], 2)
            self.assertEqual(calls["stop"], 1)
            self.assertEqual(calls["ensure"], 2)

    def test_unrelated_backend_error_does_not_retry(self):
        with tempfile.TemporaryDirectory() as td:
            m, job, calls = self._job_manager(td, [
                {"state": "failed", "error": "CUDA out of memory"},
            ])
            m._run_invokeai_job(job)
            self.assertEqual(job.state, "failed")
            self.assertEqual(calls["submit"], 1)
            self.assertEqual(calls["stop"], 0)

    def test_repeated_swept_state_fails_honestly(self):
        with tempfile.TemporaryDirectory() as td:
            m, job, calls = self._job_manager(td, [
                {"state": "failed",
                 "error": "Parent directory D:\\x\\outputs\\tensors\\tmpabc does not exist"},
                {"state": "failed",
                 "error": "Parent directory D:\\x\\outputs\\tensors\\tmpabc does not exist"},
            ])
            m._run_invokeai_job(job)
            self.assertEqual(job.state, "failed")
            self.assertEqual(calls["submit"], 2)

    def test_backend_crash_restarts_backend_and_retries(self):
        # A dead InvokeAI mid-job (connection refused/reset) gets one
        # bounded restart + resubmit — the real failure this covers was a
        # VRAM/RAM-starved InvokeAI dying while ComfyUI stayed resident.
        from localcodeagent.netdiag import BackendConnectionError
        crash = BackendConnectionError(
            ConnectionResetError(10054, "reset"), subsystem="invokeai",
            url="http://127.0.0.1:9090", phase="read")
        with tempfile.TemporaryDirectory() as td:
            m, job, calls = self._job_manager(td, [
                crash,
                {"state": "finished"},
            ])
            m._run_invokeai_job(job)
            self.assertEqual(job.state, "finished")
            self.assertEqual(calls["submit"], 2)
            self.assertEqual(calls["stop"], 1)
            self.assertEqual(calls["ensure"], 2)

    def test_repeated_backend_crash_fails_honestly(self):
        from localcodeagent.netdiag import BackendConnectionError
        crash = BackendConnectionError(
            ConnectionRefusedError(10061, "refused"), subsystem="invokeai",
            url="http://127.0.0.1:9090", phase="connect")
        with tempfile.TemporaryDirectory() as td:
            m, job, calls = self._job_manager(td, [crash, crash])
            m._run_invokeai_job(job)
            self.assertEqual(job.state, "failed")
            self.assertEqual(calls["submit"], 2)

    def test_peer_comfyui_evicted_under_memory_pressure(self):
        # Managed ComfyUI resident while InvokeAI needs more VRAM than is
        # free → arbitration parks the managed peer before submitting.
        with tempfile.TemporaryDirectory() as td:
            m, job, calls = self._job_manager(td, [{"state": "finished"}])
            evictions = {"n": 0}
            m.backend_runtime = SimpleNamespace(
                probe=lambda: {"healthy": True},
                evict_if_managed=lambda: evictions.__setitem__("n", evictions["n"] + 1) or True)
            m.router.get_profile = lambda mid: ImageModelProfile(
                id="invokeai:m1", family="sdxl", backend="invokeai",
                capabilities=["text_to_image"], workflows={},
                estimated_vram_gb=8.0, estimated_ram_gb=4.0)
            hw = SimpleNamespace(free_vram_gb=3.0, available_ram_gb=64.0)
            m.runtime = SimpleNamespace(
                hardware=hw, refresh_hardware=lambda: None,
                release_managed_models_for_vram=lambda **kw: [],
                release_managed_models_for_ram=lambda **kw: None,
                restore_managed_models=lambda s: None)
            m._run_invokeai_job(job)
            self.assertEqual(job.state, "finished")
            self.assertEqual(evictions["n"], 1)
            self.assertTrue(any("resource arbitration" in r
                                for r in job.routing_reasons))

    def test_external_peer_never_evicted(self):
        # A user-owned external ComfyUI (evict_if_managed → False) is
        # never killed — the job just proceeds under pressure.
        with tempfile.TemporaryDirectory() as td:
            m, job, calls = self._job_manager(td, [{"state": "finished"}])
            evictions = {"n": 0}
            m.backend_runtime = SimpleNamespace(
                probe=lambda: {"healthy": True},
                evict_if_managed=lambda: evictions.__setitem__("n", evictions["n"] + 1) or False)
            m.router.get_profile = lambda mid: ImageModelProfile(
                id="invokeai:m1", family="sdxl", backend="invokeai",
                capabilities=["text_to_image"], workflows={},
                estimated_vram_gb=8.0, estimated_ram_gb=4.0)
            hw = SimpleNamespace(free_vram_gb=3.0, available_ram_gb=64.0)
            m.runtime = SimpleNamespace(
                hardware=hw, refresh_hardware=lambda: None,
                release_managed_models_for_vram=lambda **kw: [],
                release_managed_models_for_ram=lambda **kw: None,
                restore_managed_models=lambda s: None)
            m._run_invokeai_job(job)
            self.assertEqual(job.state, "finished")
            self.assertEqual(evictions["n"], 1)
            self.assertFalse(any("resource arbitration" in r
                                 for r in job.routing_reasons))


class ClassificationTests(unittest.TestCase):
    def test_adult_capable_marker(self):
        r = classify_model("nsfw-realism-xl")
        self.assertEqual(r["capability_class"], "photoreal")
        self.assertEqual(r["restriction_status"], "adult_capable")

    def test_unfiltered_marker(self):
        r = classify_model("uncensored-diffusion")
        self.assertEqual(r["restriction_status"], "local_unfiltered_model")

    def test_unknown_is_honest(self):
        r = classify_model("generic-model")
        self.assertEqual(r["capability_class"], "general")
        self.assertEqual(r["restriction_status"], "unknown_capability")

    def test_upscale_and_background_classes(self):
        self.assertEqual(classify_model("RealESRGAN_x4")["capability_class"], "upscale")
        self.assertEqual(classify_model("birefnet-portrait")["capability_class"], "background_removal")
        self.assertEqual(classify_model("anime-lora", model_type="lora")["capability_class"], "lora")


class ErrorNormalizationTests(unittest.TestCase):
    def test_invokeai_not_installed(self):
        r = describe_image_error(RuntimeError("InvokeAI is not installed or running"))
        self.assertEqual(r["code"], "backend_not_installed")

    def test_invokeai_offline(self):
        r = describe_image_error(RuntimeError("InvokeAI backend is offline: refused"))
        self.assertEqual(r["code"], "backend_offline")

    def test_model_safety_rejection(self):
        r = describe_image_error(RuntimeError("content filter blocked prompt"))
        self.assertEqual(r["code"], "safety_rejected_by_model")

    def test_model_load_failure(self):
        r = describe_image_error(RuntimeError("model load failed: bad checkpoint"))
        self.assertEqual(r["code"], "model_load_failed")


class SpandrelGuardTests(unittest.TestCase):
    """`_apply_spandrel_guard` — venv patch for the InvokeAI probe segfault
    on multi-GB checkpoints (verified on 6.14.2, see docs/INVOKEAI.md)."""

    BODY = ("header\n"
            "    @classmethod\n"
            "    def _validate_spandrel_loads_model(cls, mod: ModelOnDisk) -> None:\n"
            "        try:\n"
            "            pass\n")

    def _venv(self, td: str) -> tuple[Path, Path]:
        root = Path(td)
        sp = (root / "Lib" / "site-packages" / "invokeai" / "backend"
              / "model_manager" / "configs" / "spandrel.py")
        sp.parent.mkdir(parents=True)
        sp.write_text(self.BODY, encoding="utf-8")
        return root, sp

    def test_patches_unpatched_probe(self):
        from localcodeagent.image.invokeai_runtime import InvokeAIRuntime
        with tempfile.TemporaryDirectory() as td:
            root, sp = self._venv(td)
            InvokeAIRuntime._apply_spandrel_guard(root)
            out = sp.read_text(encoding="utf-8")
            self.assertIn("NEXUS PATCH", out)
            self.assertIn("1024**3", out)
            self.assertIn("NotAMatchError", out)

    def test_idempotent_and_missing_safe(self):
        from localcodeagent.image.invokeai_runtime import InvokeAIRuntime
        with tempfile.TemporaryDirectory() as td:
            root, sp = self._venv(td)
            InvokeAIRuntime._apply_spandrel_guard(root)
            once = sp.read_text(encoding="utf-8")
            InvokeAIRuntime._apply_spandrel_guard(root)
            self.assertEqual(once, sp.read_text(encoding="utf-8"))
            InvokeAIRuntime._apply_spandrel_guard(Path(td) / "missing")


class SamplingBackendIsolationTests(unittest.TestCase):
    def test_learning_is_keyed_per_backend(self):
        with tempfile.TemporaryDirectory() as td:
            advisor = SamplingAdvisor(Path(td) / "s.json")
            profile = ImageModelProfile(id="p", family="stable-diffusion-xl")
            req1 = ImageRequest(prompt="x")
            req2 = ImageRequest(prompt="x")
            advisor.record_outcome(profile, "text_to_image",
                                   {"steps": 40, "guidance": 9.0}, "up",
                                   backend="comfyui")
            # ComfyUI win must not leak into an InvokeAI guess.
            advisor.apply(req2, profile, "text_to_image", backend="invokeai")
            self.assertNotEqual(req2.steps, 40)
            advisor.apply(req1, profile, "text_to_image", backend="comfyui")
            self.assertEqual(req1.steps, 40)


if __name__ == "__main__":
    unittest.main()
