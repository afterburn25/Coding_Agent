"""BackgroundProvisioningManager — plan, resume, retry, honesty, caps."""
from __future__ import annotations

import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from localcodeagent.provisioning import (
    MAX_ATTEMPTS, ProvisionItem, ProvisioningManager,
    classify_setup_error)


def _config(**over):
    base = dict(
        provisioning_enabled=True,
        provisioning_parallel=2,
        provisioning_auto_retry=True,
        provisioning_voice_notifications=True,
        provisioning_disk_reserve_bytes=0,
        voice_assets_dir="data/voice/assets",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _manager(root: Path, **kw) -> ProvisioningManager:
    kw.setdefault("image_manager", SimpleNamespace(
        invokeai_runtime=SimpleNamespace(ensure_ready=lambda: None),
        invokeai_backend=None))
    return ProvisioningManager(root, _config(), **kw)


def _item(item_id: str = "x", **kw) -> ProvisionItem:
    kw.setdefault("kind", "tool")
    kw.setdefault("payload", {"tool_id": item_id})
    return ProvisionItem(id=item_id, label=item_id, **kw)


class ErrorClassificationTests(unittest.TestCase):
    def test_network(self):
        self.assertEqual(
            classify_setup_error(urllib.error.URLError("timed out")),
            "network_failure")
        self.assertEqual(
            classify_setup_error("connection reset by peer"),
            "network_failure")

    def test_disk_and_checksum(self):
        self.assertEqual(classify_setup_error("no space left on device"),
                         "disk_full")
        self.assertEqual(classify_setup_error("SHA-256 mismatch for x"),
                         "checksum_failed")

    def test_python_and_permission(self):
        self.assertEqual(
            classify_setup_error("python 3.14 not a supported version"),
            "unsupported_python")
        self.assertEqual(
            classify_setup_error(PermissionError("access is denied")),
            "permission_denied")

    def test_backend_health(self):
        self.assertEqual(
            classify_setup_error("InvokeAI backend is offline: refused"),
            "backend_health_failed")


class PlanTests(unittest.TestCase):
    def test_declared_plan_shape(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            items = {i.id: i for i in m._items.values()}
            for iid in ("voice-assets", "invokeai", "whisper-stt",
                        "model-juggernaut-xl-v9",
                        "model-cyberrealistic-xl-v9", "model-realvisxl-v5",
                        "comfyui"):
                self.assertIn(iid, items, iid)
            # Fleet models depend on the InvokeAI backend.
            self.assertEqual(items["model-juggernaut-xl-v9"].depends_on,
                             ["invokeai"])
            # Verified sizes flow from the fleet spec, not invented.
            jugs = items["model-juggernaut-xl-v9"]
            self.assertEqual(jugs.est_bytes, 7105348188)
            self.assertEqual(jugs.payload["sha256"][:8], "c9e3e68f")
            # Juggernaut (general) installs before the specialists.
            self.assertLess(items["model-juggernaut-xl-v9"].priority,
                            items["model-realvisxl-v5"].priority)

    def test_status_totals(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            st = m.status()
            self.assertEqual(st["total"], len(st["items"]))
            self.assertGreater(st["total_download_bytes"], 0)
            self.assertFalse(st["complete"])


class DispatchTests(unittest.TestCase):
    def test_tool_item_runs_install_hook(self):
        done = []

        def hook(tid, approve=False):
            done.append(tid)
            return {"ok": True}  # deduped/already-installed shape

        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), install_tool_hook=hook)
            m._items = {"invokeai": _item("invokeai",
                                          payload={"tool_id": "invokeai"})}
            m._run_item("invokeai")
            self.assertEqual(done, ["invokeai"])
            self.assertEqual(m._items["invokeai"].state, "completed")
            self.assertTrue(m._items["invokeai"].verified)

    def test_failed_install_marks_item(self):
        notes, spoken = [], []

        def hook(tid, approve=False):
            return {"ok": False, "error": "installer not found"}

        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), install_tool_hook=hook)
            m.on_notify = notes.append
            m.on_speak = lambda k, t: spoken.append(t)
            m._items = {"t1": _item("t1", payload={"tool_id": "t1"})}
            for _ in range(MAX_ATTEMPTS):
                m._items["t1"].attempts += 1
                m._run_item("t1")
                m._items["t1"].next_retry_at = 0
            it = m._items["t1"]
            self.assertEqual(it.state, "failed")
            self.assertTrue(it.error_code)
            # One incident → exactly one spoken line, however many retries.
            self.assertEqual(len(spoken), 1)
            self.assertTrue(any(n.get("kind") == "provision_failed"
                                for n in notes))

    def test_transient_network_retry(self):
        calls = []

        def hook(tid, approve=False):
            calls.append(tid)
            if len(calls) < 2:
                raise urllib.error.URLError("timed out")
            return {"ok": True}

        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), install_tool_hook=hook)
            it = _item("t1", payload={"tool_id": "t1"})
            m._items = {"t1": it}
            m._run_item("t1")
            self.assertEqual(it.state, "waiting")
            self.assertEqual(it.error_code, "network_failure")
            self.assertGreater(it.next_retry_at, time.time())
            it.next_retry_at = 0
            it.attempts += 1
            m._run_item("t1")
            self.assertEqual(it.state, "completed")

    def test_permission_denied_retries_then_clears(self):
        # Windows AV on-access scans transiently lock freshly-extracted
        # exes mid-install — the failure must retry, not fail terminal.
        calls = []

        def hook(tid, approve=False):
            calls.append(tid)
            if len(calls) < 2:
                raise PermissionError(
                    13, "Permission denied", "tools/x/Scripts/python.exe")
            return {"ok": True}

        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), install_tool_hook=hook)
            it = _item("t1", payload={"tool_id": "t1"})
            m._items = {"t1": it}
            m._run_item("t1")
            self.assertEqual(it.state, "waiting")
            self.assertEqual(it.error_code, "permission_denied")
            self.assertGreater(it.next_retry_at, time.time())
            it.next_retry_at = 0
            it.attempts += 1
            m._run_item("t1")
            self.assertEqual(it.state, "completed")

    def test_tool_verify_rejects_undetected_install(self):
        # A job that reports success but leaves nothing on disk must not
        # mark the item completed — verification is the honesty boundary.
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td),
                         install_tool_hook=lambda t, approve=False:
                             {"ok": True},
                         tool_installed_hook=lambda tid: False)
            it = _item("t1", payload={"tool_id": "t1"})
            m._items = {"t1": it}
            m._run_item("t1")
            self.assertNotEqual(it.state, "completed")
            self.assertIn("not detected", it.error_message)

    def test_capability_ready_fires(self):
        ready = []

        def hook(tid, approve=False):
            return {"ok": True}

        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), install_tool_hook=hook)
            m.on_capability_ready = ready.append
            it = _item("invokeai", payload={"tool_id": "invokeai"},
                       provides="image_generation")
            m._items = {"invokeai": it}
            self.assertEqual(m.capability_state("image_generation"),
                             "setup_required")
            m._run_item("invokeai")
            self.assertEqual(ready, ["image_generation"])
            self.assertEqual(m.capability_state("image_generation"),
                             "available")

    def test_cancel_and_retry_item(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            self.assertTrue(m.cancel_item("invokeai"))
            self.assertEqual(m._items["invokeai"].state, "cancelled")
            self.assertTrue(m.retry_item("invokeai"))
            self.assertEqual(m._items["invokeai"].state, "waiting")


class InvokeAIModelItemTests(unittest.TestCase):
    """Fleet-model items: healthy installs are never re-downloaded, and a
    backend crash mid-install surfaces instead of hanging (both learned
    from live dogfood on InvokeAI 6.14.2)."""

    class _FakeBackend:
        def __init__(self, rows=(), jobs=None, job_id=0):
            self._rows = list(rows)
            self._jobs = jobs if jobs is not None else {}
            self._job_id = job_id
            self.installs = []

        def models(self):
            return list(self._rows)

        def install_model(self, source):
            self.installs.append(source)
            return {"id": self._job_id}

        def model_install_job(self, job_id):
            return self._jobs.get(job_id)

    def _manager_with_backend(self, root, backend):
        return _manager(root, image_manager=SimpleNamespace(
            invokeai_runtime=SimpleNamespace(ensure_ready=lambda: None),
            invokeai_backend=backend))

    def _fleet_item(self):
        return _item("model-juggernaut-xl-v9",
                     kind="invokeai_model", provides="image_model",
                     payload={"fleet_id": "juggernaut-xl-v9",
                              "source": "https://huggingface.co/x/y",
                              "sha256": "0" * 64, "license": "test"})

    def test_already_registered_skips_download(self):
        backend = self._FakeBackend(rows=[{
            "name": "Juggernaut-XL_v9_RunDiffusionPhoto_v2",
            "type": "main", "base": "sdxl"}])
        with tempfile.TemporaryDirectory() as td:
            m = self._manager_with_backend(Path(td), backend)
            it = self._fleet_item()
            m._items = {it.id: it}
            m._run_item(it.id)
            self.assertEqual(backend.installs, [])
            self.assertEqual(it.state, "completed")
            self.assertTrue(it.verified)

    def test_vanished_job_fails_not_hangs(self):
        # invokeai-web crash/restart wipes in-memory job rows — polling a
        # vanished job must raise (retryable) instead of looping forever.
        backend = self._FakeBackend(jobs={})
        with tempfile.TemporaryDirectory() as td:
            m = self._manager_with_backend(Path(td), backend)
            it = self._fleet_item()
            m._items = {it.id: it}
            m._run_item(it.id)
            self.assertEqual(backend.installs, ["https://huggingface.co/x/y"])
            self.assertNotEqual(it.state, "running")
            self.assertIn("disappeared", it.error_message or "")

    def test_completed_job_verifies_registration(self):
        backend = self._FakeBackend(
            rows=[{"name": "Juggernaut-XL_v9_RunDiffusionPhoto_v2",
                   "type": "main", "base": "sdxl"}],
            jobs={0: {"status": "completed", "bytes": 10,
                      "bytes_total": 10}})
        # For the completed path we need models() to MISS the fleet row
        # pre-install (forces a submit) then present it at verify time.
        calls = {"n": 0}
        orig_models = backend.models
        def models():
            calls["n"] += 1
            return [] if calls["n"] == 1 else orig_models()
        backend.models = models
        with tempfile.TemporaryDirectory() as td:
            m = self._manager_with_backend(Path(td), backend)
            it = self._fleet_item()
            m._items = {it.id: it}
            m._run_item(it.id)
            self.assertEqual(backend.installs, ["https://huggingface.co/x/y"])
            self.assertEqual(it.state, "completed")


class PersistenceTests(unittest.TestCase):
    def test_state_persists_and_midflight_requeues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m1 = _manager(root)
            m1._items["invokeai"].state = "running"
            m1._items["comfyui"].state = "completed"
            m1._items["comfyui"].verified = True
            m1._save()
            m2 = _manager(root)
            # Mid-flight work resumes rather than being lost or claimed done.
            self.assertEqual(m2._items["invokeai"].state, "waiting")
            self.assertEqual(m2._items["invokeai"].detail,
                             "resumed after restart")
            self.assertEqual(m2._items["comfyui"].state, "completed")

    def test_completed_tool_reverified_requeues_missing(self):
        """A persisted 'completed' tool whose payload vanished must be
        requeued — the plan never overrides ground truth forever."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            present = {"invokeai"}
            hook = lambda tid: tid in present
            m1 = _manager(root, tool_installed_hook=hook)
            m1._items["invokeai"].state = "completed"
            m1._items["invokeai"].verified = True
            m1._save()
            m2 = _manager(root, tool_installed_hook=hook)
            self.assertEqual(m2._items["invokeai"].state, "completed")
            # Payload disappears between runs → honest requeue.
            present.clear()
            m2._save()
            m3 = _manager(root, tool_installed_hook=hook)
            self.assertEqual(m3._items["invokeai"].state, "waiting")
            self.assertFalse(m3._items["invokeai"].verified)

    def test_invokeai_model_reverified_via_offline_registry(self):
        """A completed invokeai_model item is re-checked against
        InvokeAI's SQLite registry — no live backend needed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = SimpleNamespace(
                ensure_ready=lambda: None,
                discover=lambda: (root / "tools" / "InvokeAI", ["x"]),
                registry_models=lambda: [],  # installed but empty registry
                invalidate_discovery=lambda: None)
            kw = dict(image_manager=SimpleNamespace(
                invokeai_runtime=rt, invokeai_backend=None),
                tool_installed_hook=lambda tid: True)
            m1 = _manager(root, **kw)
            it = m1._items["model-juggernaut-xl-v9"]
            it.state = "completed"; it.verified = True
            m1._save()
            m2 = _manager(root, **kw)
            self.assertEqual(m2._items["model-juggernaut-xl-v9"].state,
                             "waiting")

    def test_voice_reverify_requeues_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m1 = _manager(root)
            m1._items["voice-assets"].state = "completed"
            m1._items["voice-assets"].verified = True
            m1._save()
            # No real assets on disk → inventory requeues honestly.
            m2 = _manager(root)
            self.assertEqual(m2._items["voice-assets"].state, "waiting")

    def test_voice_progress_contract_matches_ensure_assets(self):
        # Regression: the real ensure_assets calls progress(name, done) —
        # two args. A provisioning callback expecting (name, done, total)
        # crashed on first download with a TypeError ("missing 'total'").
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            it = m._items["voice-assets"]

            def fake_ensure(asset_dir, progress=None):
                if progress:
                    progress("kokoro-v1.0.onnx", 1024)
                return {}

            with patch("localcodeagent.voice.assets.ensure_assets",
                       fake_ensure), \
                 patch("localcodeagent.voice.assets.asset_status",
                       return_value={"a": {"verified": True}}):
                m._run_voice_assets(it)
            self.assertEqual(it.progress["bytes_done"], 1024)
            self.assertEqual(it.progress["current_file"], "kokoro-v1.0.onnx")


class DiskGateTests(unittest.TestCase):
    def test_low_disk_blocks_item(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            it = _item("big", est_disk_bytes=10**15)
            m._items = {"big": it}
            with patch("localcodeagent.provisioning.shutil.disk_usage") as du:
                du.return_value = SimpleNamespace(free=1024)
                m._dispatch()
            self.assertEqual(it.state, "waiting")
            self.assertIn("free", it.blocked_reason)


class WaitForCapabilityTests(unittest.TestCase):
    def test_wait_unblocks_on_complete(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(
                Path(td),
                install_tool_hook=lambda t, approve=False: {"ok": True})
            it = _item("invokeai", payload={"tool_id": "invokeai"},
                       provides="image_generation")
            m._items = {"invokeai": it}
            import threading
            result = []
            t = threading.Thread(
                target=lambda: result.append(
                    m.wait_for_capability("image_generation", 10)),
                daemon=True)
            t.start()
            m._run_item("invokeai")
            t.join(timeout=10)
            self.assertEqual(result, [True])


class ReconcileTests(unittest.TestCase):
    """Terminal-state healing: a failed tool that now detects on disk is
    completed, and items skipped over a failed dependency requeue once the
    dependency is satisfied."""

    def test_failed_tool_now_detected_completes_and_unskips_dependents(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            installed = []
            m = _manager(root, tool_installed_hook=lambda tid: tid in installed)
            invokeai = m._items["invokeai"]
            invokeai.state = "failed"
            invokeai.error_code = "install_failed"
            invokeai.error_message = "not detected on disk"
            invokeai.attempts = 1
            for iid in ("model-juggernaut-xl-v9", "model-cyberrealistic-xl-v9",
                        "model-realvisxl-v5"):
                dep = m._items[iid]
                dep.state = "skipped"
                dep.detail = "dependency did not install"
            installed.append("invokeai")
            m._reconcile()
            self.assertEqual(invokeai.state, "completed")
            self.assertTrue(invokeai.verified)
            for iid in ("model-juggernaut-xl-v9", "model-cyberrealistic-xl-v9",
                        "model-realvisxl-v5"):
                self.assertEqual(m._items[iid].state, "waiting", iid)

    def test_failed_tool_still_missing_stays_failed_after_attempts(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), tool_installed_hook=lambda tid: False)
            it = m._items["invokeai"]
            it.state = "failed"
            it.attempts = MAX_ATTEMPTS
            m._reconcile()
            self.assertEqual(it.state, "failed")

    def test_failed_tool_under_attempt_cap_requeues_for_retry(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), tool_installed_hook=lambda tid: False)
            it = m._items["invokeai"]
            it.state = "failed"
            it.attempts = 1
            m._reconcile()
            self.assertEqual(it.state, "waiting")
            self.assertEqual(it.next_retry_at, 0.0)

    def test_skipped_item_without_satisfied_deps_stays_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), tool_installed_hook=lambda tid: False)
            dep = m._items["model-juggernaut-xl-v9"]
            dep.state = "skipped"
            dep.detail = "dependency did not install"
            m._items["invokeai"].state = "failed"
            m._items["invokeai"].attempts = MAX_ATTEMPTS
            m._reconcile()
            self.assertEqual(dep.state, "skipped")

    def test_auto_retry_disabled_keeps_failed_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(provisioning_auto_retry=False)
            m = ProvisioningManager(
                Path(td), cfg,
                image_manager=SimpleNamespace(
                    invokeai_runtime=SimpleNamespace(ensure_ready=lambda: None),
                    invokeai_backend=None),
                tool_installed_hook=lambda tid: False)
            it = m._items["invokeai"]
            it.state = "failed"
            it.attempts = 1
            m._reconcile()
            self.assertEqual(it.state, "failed")


class ComfyImageModelItemTests(unittest.TestCase):
    """The configured ComfyUI image model profiles join the post-install
    download plan after the ComfyUI runtime item."""

    def _profiles(self):
        return [SimpleNamespace(
            id="juggernaut-x-v10", display_name="Juggernaut X v10",
            enabled=True,
            components=[{"size_bytes": 7105348672, "required": True}]),
            SimpleNamespace(
            id="flux2-klein-4b", display_name="FLUX.2 Klein 4B",
            enabled=False,
            components=[{"size_bytes": 100, "required": True}])]

    def test_plan_includes_enabled_comfy_models_after_comfyui(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(image_models=self._profiles())
            m = ProvisioningManager(
                Path(td), cfg,
                image_manager=SimpleNamespace(
                    invokeai_runtime=SimpleNamespace(ensure_ready=lambda: None),
                    invokeai_backend=None))
            items = m._items
            self.assertIn("imagemodel-juggernaut-x-v10", items)
            self.assertNotIn("imagemodel-flux2-klein-4b", items)  # disabled
            jug = items["imagemodel-juggernaut-x-v10"]
            self.assertEqual(jug.depends_on, ["comfyui"])
            self.assertEqual(jug.est_bytes, 7105348672)
            self.assertGreater(items["comfyui"].priority, 0)
            self.assertGreater(jug.priority, items["comfyui"].priority)

    def test_image_model_download_verify_and_finish(self):
        job = {"id": "j1", "state": "queued", "progress": 0.0,
               "current_file": "", "error": ""}

        class FakeLibrary:
            def __init__(self):
                self.calls = 0
            def verify_model(self, profile):
                return {"installed": job["state"] == "finished"}
            def install_jobs(self):
                return [job]

        class FakeManager:
            def __init__(self):
                self.router = SimpleNamespace(
                    get_profile=lambda mid: SimpleNamespace(id=mid))
                self.library = FakeLibrary()
            def start_model_install(self, model_id, repair=False):
                job["state"] = "downloading"
                job["state"] = "finished"
                return dict(job)

        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), image_manager=FakeManager())
            it = _item("imagemodel-x", kind="image_model",
                       payload={"model_id": "x"})
            m._items = {"imagemodel-x": it}
            m._run_item("imagemodel-x")
            self.assertEqual(it.state, "completed")
            self.assertTrue(it.verified)


if __name__ == "__main__":
    unittest.main()
