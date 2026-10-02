"""Platform foundations: sandbox, artifacts, backups, vault, health, twin."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from localcodeagent.sandbox import Sandbox
from localcodeagent.artifacts import ArtifactManager
from localcodeagent.backups import BackupService
from localcodeagent.health import HealthService
from localcodeagent.twin import DigitalTwin


class SandboxTests(unittest.TestCase):
    def test_python_exec_ok(self):
        with Sandbox() as sb:
            r = sb.run_python("print('hello-sandbox')")
        self.assertTrue(r["ok"])
        self.assertIn("hello-sandbox", r["stdout"])

    def test_python_failure_captured(self):
        with Sandbox() as sb:
            r = sb.run_python("import sys; sys.exit(3)")
        self.assertFalse(r["ok"])
        self.assertEqual(r["exit"], 3)

    def test_timeout_bounded(self):
        with Sandbox() as sb:
            r = sb.run_python("import time; time.sleep(30)", timeout=1)
        self.assertTrue(r["timed_out"])
        self.assertFalse(r["ok"])

    def test_cleanup_removes_workspace(self):
        sb = Sandbox()
        root = sb.root
        sb.run_python("open('out.txt','w').write('x')")
        sb.cleanup()
        self.assertFalse(root.exists())

    def test_artifact_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            with Sandbox() as sb:
                sb.run_python("open('result.txt','w').write('artifact')")
                arts = sb.artifacts()
                self.assertIn("result.txt", [a["path"] for a in arts])
                moved = sb.extract(Path(td))
            self.assertIn("result.txt", moved)
            self.assertEqual((Path(td) / "result.txt").read_text(), "artifact")

    def test_network_denied_records_flag(self):
        with Sandbox() as sb:
            r = sb.run_shell("echo net-test", allow_network=False)
        self.assertTrue(r["ok"])
        self.assertFalse(r["network_allowed"])


class ArtifactTests(unittest.TestCase):
    def test_register_and_list(self):
        with tempfile.TemporaryDirectory() as td:
            am = ArtifactManager(Path(td) / "arts")
            f = Path(td) / "report.md"
            f.write_text("# report")
            rec = am.register(f, creator="test", mission_id="m-1")
            self.assertEqual(rec["kind"], "report")
            self.assertTrue(rec["sha256"])
            rows = am.list(mission_id="m-1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(am.get(rec["id"])["name"], "report.md")

    def test_versions_increment(self):
        with tempfile.TemporaryDirectory() as td:
            am = ArtifactManager(Path(td) / "arts")
            f = Path(td) / "out.txt"
            f.write_text("v1")
            r1 = am.register(f)
            f.write_text("v2")
            r2 = am.register(f)
            self.assertEqual(r2["version"], r1["version"] + 1)

    def test_store_and_verify(self):
        with tempfile.TemporaryDirectory() as td:
            am = ArtifactManager(Path(td) / "arts")
            f = Path(td) / "code.py"
            f.write_text("print(1)")
            rec = am.register(f, store=True)
            f.unlink()
            self.assertTrue(am.verify(rec["id"])["ok"])  # stored copy lives
            Path(rec["path"]).write_text("tampered")
            self.assertFalse(am.verify(rec["id"])["ok"])

    def test_registry_persists(self):
        with tempfile.TemporaryDirectory() as td:
            am = ArtifactManager(Path(td) / "arts")
            f = Path(td) / "a.txt"
            f.write_text("x")
            am.register(f, task_id="t-9")
            am2 = ArtifactManager(Path(td) / "arts")
            self.assertEqual(len(am2.list(task_id="t-9")), 1)


class BackupTests(unittest.TestCase):
    def _ws(self, td: str) -> Path:
        ws = Path(td)
        (ws / "data").mkdir(exist_ok=True)
        (ws / "data" / "nexus_brain.json").write_text('{"facts": 1}')
        (ws / "config.json").write_text('{"mode": "auto"}')
        return ws

    def test_create_and_restore(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            svc = BackupService(ws)
            out = svc.create()
            self.assertTrue(out["ok"])
            # Damage live state, then restore.
            (ws / "config.json").write_text('{"mode": "broken"}')
            res = svc.restore(out["backup"])
            self.assertTrue(res["ok"])
            self.assertEqual(json.loads((ws / "config.json").read_text())
                             ["mode"], "auto")

    def test_corrupt_backup_refused(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            svc = BackupService(ws)
            out = svc.create()
            # Corrupt a backed-up file.
            target = next((Path(out["path"]) / "files").rglob("*.json"))
            target.write_text("tampered")
            res = svc.restore(out["backup"])
            self.assertFalse(res["ok"])

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            svc = BackupService(ws)
            out = svc.create()
            res = svc.restore(out["backup"], dry_run=True)
            self.assertTrue(res["ok"])
            self.assertGreater(res["would_restore"], 0)

    def test_prune_never_deletes_last_valid(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            svc = BackupService(ws, keep=2)
            names = [svc.create()["backup"] for _ in range(4)]
            remaining = [b["name"] for b in svc.list() if not b.get("corrupt")]
            self.assertLessEqual(len(remaining), 2)
            self.assertIn(names[-1], remaining)


class VaultTests(unittest.TestCase):
    def test_dpapi_key_protection_windows(self):
        from localcodeagent.secrets import SecretVault
        with tempfile.TemporaryDirectory() as td:
            v = SecretVault(Path(td) / "vault.json")
            v.set("api.key", "super-secret-value-123")
            key_text = v.key_path.read_text()
            if os.name == "nt":
                self.assertTrue(key_text.startswith("dpapi:"),
                                "key file should be DPAPI-wrapped on Windows")
            # Vault still reads secrets transparently.
            v2 = SecretVault(Path(td) / "vault.json")
            self.assertEqual(v2.get("api.key"), "super-secret-value-123")

    def test_pattern_redaction(self):
        from localcodeagent.secrets import SecretVault
        text = "key is sk-abcdef0123456789XYZ and ghp_0123456789abcdefghij done"
        out = SecretVault.redact_patterns(text)
        self.assertNotIn("sk-abcdef0123456789", out)
        self.assertNotIn("ghp_0123456789", out)

    def test_vault_redact(self):
        from localcodeagent.secrets import SecretVault
        with tempfile.TemporaryDirectory() as td:
            v = SecretVault(Path(td) / "v.json")
            v.set("tok", "tok-value-abcdef")
            self.assertNotIn("tok-value-abcdef",
                             v.redact("leaked tok-value-abcdef in log"))


class HealthTests(unittest.TestCase):
    def test_state_transitions(self):
        h = HealthService()
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            return "healthy" if calls["n"] > 1 else "crashed"

        recovered = {"n": 0}

        def recover():
            recovered["n"] += 1
            return True

        h.register("svc", flaky, recover=recover, cooldown_s=0)
        self.assertEqual(h.check("svc"), "healthy")  # crashed → recover → probe
        self.assertEqual(recovered["n"], 1)

    def test_recovery_bounded(self):
        h = HealthService()
        recovered = {"n": 0}
        h.register("svc", lambda: "crashed",
                   recover=lambda: recovered.__setitem__("n", recovered["n"] + 1) or False,
                   max_recovery_attempts=2, cooldown_s=0)
        for _ in range(5):
            h.check("svc")
        self.assertEqual(recovered["n"], 2)  # bounded, never infinite

    def test_report_pushes_state_and_recovers(self):
        h = HealthService()
        recovered = {"n": 0}
        h.register("svc", lambda: "healthy",
                   recover=lambda: recovered.__setitem__("n", recovered["n"] + 1) or True,
                   cooldown_s=0)
        self.assertEqual(h.report("svc", "crashed", "socket reset"), "healthy")
        self.assertEqual(recovered["n"], 1)
        self.assertEqual(h.report("nope", "crashed"), "unknown")

    def test_overall_and_persist(self):
        with tempfile.TemporaryDirectory() as td:
            h = HealthService(Path(td) / "health.json")
            h.register("a", lambda: "healthy")
            h.register("b", lambda: "degraded")
            states = h.tick()
            self.assertEqual(states["a"], "healthy")
            self.assertEqual(h.overall(), "degraded")
            self.assertTrue((Path(td) / "health.json").is_file())


class TwinTests(unittest.TestCase):
    FAKE_HW = {"gpus": [{"name": "RTX", "total_vram_mb": 24000,
                       "free_vram_mb": 22000}],
               "ram_total_gb": 64.0, "ram_free_gb": 48.0}

    def test_predict_fit(self):
        with tempfile.TemporaryDirectory() as td:
            twin = DigitalTwin(Path(td) / "twin.json",
                               detect=lambda: dict(self.FAKE_HW))
            p = twin.predict_model(size_gb=9.0, quant="Q4_K_M")
            self.assertTrue(p["fits"])
            p2 = twin.predict_model(size_gb=200.0)
            self.assertFalse(p2["fits"])

    def test_measured_basis(self):
        with tempfile.TemporaryDirectory() as td:
            twin = DigitalTwin(Path(td) / "twin.json",
                               detect=lambda: dict(self.FAKE_HW))
            twin.record_model_measure(model_id="qwen-14b", size_gb=9.0,
                                      tps=42.0, ttft_s=1.2, ram_used_gb=11.0)
            p = twin.predict_model(size_gb=9.5)
            self.assertEqual(p["basis"], "measured")
            self.assertIn("expected_tps", p)

    def test_fingerprint_invalidates(self):
        with tempfile.TemporaryDirectory() as td:
            hw = dict(self.FAKE_HW)
            twin = DigitalTwin(Path(td) / "twin.json", detect=lambda: hw)
            twin.record_model_measure(model_id="m", size_gb=9.0)
            self.assertEqual(len(twin.data["model_measures"]), 1)
            # GPU changed → measurements invalidated.
            hw["gpus"] = [{"name": "NewGPU", "total_vram_mb": 48000,
                           "free_vram_mb": 46000}]
            twin._validate_fingerprint()
            self.assertEqual(twin.data["model_measures"], [])

    def test_must_unload_first(self):
        with tempfile.TemporaryDirectory() as td:
            low = {"gpus": [{"name": "RTX", "total_vram_mb": 8000,
                             "free_vram_mb": 500}],
                   "ram_total_gb": 32.0, "ram_free_gb": 6.0}
            twin = DigitalTwin(Path(td) / "twin.json", detect=lambda: low)
            out = twin.must_unload_first(
                size_gb=9.0,
                resident=[{"model": "old", "vram_mb": 7000, "ram_gb": 8}])
            self.assertTrue(out["needed"])


if __name__ == "__main__":
    unittest.main()
