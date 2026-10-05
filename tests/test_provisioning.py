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


if __name__ == "__main__":
    unittest.main()
