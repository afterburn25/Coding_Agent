"""Phase 12 — predictive trends, idle cleanup, benchmark lab,
temporary specialists."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.trends import TrendAnalyzer
from localcodeagent.cleanup import IdleCleaner
from localcodeagent.benchmarks import BenchmarkLab
from localcodeagent.temp_specialists import TempSpecialistStore
from localcodeagent.regressions import BaselineStore


class TrendTests(unittest.TestCase):
    def test_insufficient_data_is_honest(self):
        with tempfile.TemporaryDirectory() as td:
            t = TrendAnalyzer(Path(td) / "t.json")
            t.record("ram_mb", 100)
            out = t.analyze("ram_mb")
            self.assertEqual(out["trend"], "insufficient_data")

    def test_rising_trend_with_projection(self):
        with tempfile.TemporaryDirectory() as td:
            t = TrendAnalyzer(Path(td) / "t.json")
            base = time.time() - 3600
            for i in range(10):
                t.record("ram_mb", 1000 + i * 100, ts=base + i * 360)
            out = t.analyze("ram_mb", limit=3000)
            self.assertEqual(out["trend"], "rising")
            self.assertIn("projection", out)
            self.assertIn("may reach", out["projection"])
            # Persists.
            self.assertEqual(
                TrendAnalyzer(Path(td) / "t.json").analyze(
                    "ram_mb")["trend"], "rising")

    def test_noise_is_stable(self):
        with tempfile.TemporaryDirectory() as td:
            t = TrendAnalyzer(Path(td) / "t.json")
            base = time.time() - 600
            for i, v in enumerate([100, 101, 99, 100.5, 100.2, 99.8,
                                   100.1, 100.0]):
                t.record("queue", v, ts=base + i * 60)
            out = t.analyze("queue", limit=500)
            self.assertEqual(out["trend"], "stable")
            self.assertNotIn("eta_hours", out)


class CleanupTests(unittest.TestCase):
    def test_scan_and_bounded_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wt = root / "worktrees" / "stale"
            wt.mkdir(parents=True)
            (wt / "f.bin").write_bytes(b"x" * 1024)
            # Make it stale.
            old = time.time() - 48 * 3600
            import os
            os.utime(wt, (old, old))
            big = root / "data" / "logs"
            big.mkdir(parents=True)
            (big / "huge.log").write_bytes(b"x" * (30 * 1024 * 1024))
            c = IdleCleaner(root)
            cands = c.scan()
            kinds = {k["kind"] for k in cands}
            self.assertIn("stale_dir", kinds)
            self.assertIn("big_file", kinds)
            # Dry run deletes nothing.
            out = c.run()
            self.assertTrue(out["dry_run"])
            self.assertTrue(wt.exists())
            self.assertTrue((big / "huge.log").exists())
            # Real run reclaims.
            out = c.run(dry_run=False)
            self.assertGreater(out["reclaimed_bytes"], 0)
            self.assertFalse(wt.exists())
            self.assertFalse((big / "huge.log").exists())

    def test_never_deletes_outside_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = IdleCleaner(root / "nonexistent")
            self.assertEqual(c.scan(), [])
            out = c.run(dry_run=False)
            self.assertEqual(out["deleted"], [])


class BenchmarkTests(unittest.TestCase):
    def test_run_and_record_feed_baselines(self):
        with tempfile.TemporaryDirectory() as td:
            bs = BaselineStore(Path(td) / "baselines.json")
            lab = BenchmarkLab(Path(td) / "bench.json", baselines=bs)
            row = lab.run("utility_4b", lambda: time.sleep(0.001),
                          iterations=3)
            self.assertTrue(row["success"])
            self.assertGreater(row["latency_ms"], 0)
            # Failure path — a raising callable is a failed run.
            row = lab.run("image_gen", lambda: 1 / 0)
            self.assertFalse(row["success"])
            lab.record_result("tts", latency_ms=42.0, throughput=5.0,
                              quality=0.9)
            s = lab.summary()
            self.assertEqual(s["benchmarks"]["tts"]["runs"], 1)
            # Baselines received the latency metric.
            self.assertIsNotNone(
                bs.data["metrics"].get("bench.tts.latency_ms"))


class TempSpecialistTests(unittest.TestCase):
    def test_scope_caps_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            s = TempSpecialistStore(Path(td) / "tsp.json")
            spec = s.spawn(
                "Windows Installer Investigator",
                task="Diagnose installer failure",
                capabilities=["read_file", "run_shell", "bogus_tool"],
                context_refs=["proj-1"],
                acceptance_criteria=["installer log analyzed"],
                mission_id="m-9",
                ttl_seconds=3600,
                valid_capabilities={"read_file", "run_shell"})
            # Bogus capability dropped — scope can't exceed real tools.
            self.assertEqual(sorted(spec["capabilities"]),
                             ["read_file", "run_shell"])
            self.assertIsNotNone(s.dispatchable(spec["id"]))
            row = s.complete(spec["id"], result="log parsed",
                             met_criteria=True)
            self.assertEqual(row["status"], "completed")
            self.assertIsNone(s.dispatchable(spec["id"]))
            self.assertTrue(s.list(active_only=False))
            self.assertFalse(s.list(active_only=True))

    def test_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            s = TempSpecialistStore(Path(td) / "tsp.json")
            spec = s.spawn("Ephemeral", task="t", ttl_seconds=0.01)
            time.sleep(0.02)
            self.assertIsNone(s.dispatchable(spec["id"]))
            self.assertEqual(s.reap_expired(), 1)
            self.assertEqual(s.list()[0]["status"], "expired")


class Phase12HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True)
        ws = Path(cls._td.name)
        cfg = AgentConfig(
            profiles_onboarding_gate=False,
            models=[ModelProfile(id="fake",
                                 endpoint="http://127.0.0.1:1/v1",
                                 model="m", roles=["utility"],
                                 runtime="external")],
            process_watchdog=False, research_enabled=False)
        cls.server, cls.state = create_server(
            cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.addClassCleanup(lambda: (cls.server.shutdown(),
                                     cls.server.server_close(),
                                     stop_state(cls.state)))

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _get(self, path):
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read())

    def _post(self, path, body):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_trends_benchmarks_over_http(self):
        for i in range(8):
            self._post("/api/trends/record",
                       {"metric": "ram_mb", "value": 500 + i * 50})
        out = self._get("/api/trends/ram_mb?limit=1500")
        self.assertIn(out["trend"], ("rising", "stable"))
        st, out = self._post("/api/benchmarks/record",
                             {"name": "stt", "latency_ms": 30.0})
        self.assertTrue(out["ok"])
        s = self._get("/api/benchmarks")
        self.assertIn("stt", s["benchmarks"])
        st, out = self._post("/api/cleanup/run", {"execute": False})
        self.assertTrue(out["dry_run"])

    def test_specialists_over_http(self):
        st, out = self._post("/api/specialists/spawn", {
            "name": "CUDA Performance Analyst",
            "task": "profile VRAM churn",
            "capabilities": ["read_file", "no_such_tool"],
            "acceptance_criteria": ["report produced"],
            "ttl_seconds": 3600})
        self.assertEqual(st, 200)
        spec = out["specialist"]
        self.assertNotIn("no_such_tool", spec["capabilities"])
        sid = spec["id"]
        listing = self._get("/api/specialists?active=1")
        self.assertTrue(any(s["id"] == sid
                            for s in listing["specialists"]))
        st, out = self._post("/api/specialists/complete", {
            "id": sid, "result": "done", "met_criteria": True})
        self.assertEqual(out["specialist"]["status"], "completed")
        st, _ = self._post("/api/specialists/retire", {"id": sid})
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
