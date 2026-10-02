import io
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.router import ModelRouter
from localcodeagent.runtime.hardware import GPUInfo, HardwareSnapshot, parse_nvidia_smi_csv
from localcodeagent.runtime.manager import RuntimeManager, _ManagedProcess


class _FakeProcess:
    """Minimal stand-in for subprocess.Popen used by _stop_managed."""

    def __init__(self):
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False
        return 0


def _attach_fake_managed(manager: RuntimeManager, profile: ModelProfile, *, last_used: float | None = None) -> _FakeProcess:
    proc = _FakeProcess()
    status = manager._status[profile.id]
    status.managed = True
    status.state = "running"
    status.healthy = True
    manager._managed[profile.id] = _ManagedProcess(profile, proc, "http://x/v1", io.StringIO(), status)
    manager._last_used[profile.id] = time.time() if last_used is None else last_used
    return proc


class HardwareTests(unittest.TestCase):
    def test_parse_nvidia_smi(self):
        rows = parse_nvidia_smi_csv("0, NVIDIA GeForce RTX 3080, 12288, 2048, 10240, 7, 55\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "NVIDIA GeForce RTX 3080")
        self.assertEqual(rows[0].free_vram_mb, 10240)
        self.assertEqual(rows[0].utilization_percent, 7)


class RuntimeManagerTests(unittest.TestCase):
    def _profile(self, **overrides):
        values = dict(
            id="coder",
            endpoint="http://127.0.0.1:8080/v1",
            model="coder",
            roles=["primary_coder"],
            runtime="llama_cpp",
            model_path="models/coder.gguf",
        )
        values.update(overrides)
        return ModelProfile(**values)

    def test_bundled_runtime_is_preferred(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundled = root / "runtime" / "llama"
            bundled.mkdir(parents=True)
            exe = bundled / ("llama-server.exe" if os.name == "nt" else "llama-server")
            exe.write_text("bundled", encoding="utf-8")
            cfg = AgentConfig(models=[])
            manager = RuntimeManager(cfg, base_dir=root)
            self.assertEqual(manager.discover_llama_server(), str(exe.resolve()))

    def test_health_url_removes_v1(self):
        self.assertEqual(
            RuntimeManager._health_url("http://127.0.0.1:8080/v1"),
            "http://127.0.0.1:8080/health",
        )

    def test_build_llama_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "coder.gguf").write_bytes(b"GGUF")
            fake_server = root / "llama-server"
            fake_server.write_text("fake", encoding="utf-8")
            cfg = AgentConfig(models=[self._profile(executable=str(fake_server))])
            manager = RuntimeManager(cfg, base_dir=root)
            cmd = manager._build_command(cfg.models[0], 9123)
            self.assertIn("--model", cmd)
            self.assertIn(str(models / "coder.gguf"), cmd)
            self.assertIn("--gpu-layers", cmd)
            self.assertIn("auto", cmd)
            self.assertIn("9123", cmd)

    def test_qwen3_14b_defaults_to_non_thinking_runtime_for_old_configs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "Qwen3-14B-Q4_K_M.gguf").write_bytes(b"GGUF")
            fake_server = root / "llama-server.exe"
            fake_server.write_text("placeholder", encoding="utf-8")
            profile = self._profile(
                id="qwen3-14b",
                endpoint="",
                model="Qwen3-14B-Q4_K_M",
                model_path="models/Qwen3-14B-Q4_K_M.gguf",
                executable=str(fake_server),
                roles=["utility", "fast_coder", "primary_coder"],
                extra_args=[],
            )
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=root)
            cmd = manager._build_command(profile, 8081)
            reasoning_index = cmd.index("--reasoning")
            self.assertEqual(cmd[reasoning_index + 1], "off")

    def test_explicit_reasoning_override_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "Qwen3-14B-Q4_K_M.gguf").write_bytes(b"GGUF")
            fake_server = root / "llama-server.exe"
            fake_server.write_text("placeholder", encoding="utf-8")
            profile = self._profile(
                id="qwen3-14b",
                endpoint="",
                model="Qwen3-14B-Q4_K_M",
                model_path="models/Qwen3-14B-Q4_K_M.gguf",
                executable=str(fake_server),
                extra_args=["--reasoning", "auto"],
            )
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=root)
            cmd = manager._build_command(profile, 8081)
            self.assertEqual(cmd.count("--reasoning"), 1)
            reasoning_index = cmd.index("--reasoning")
            self.assertEqual(cmd[reasoning_index + 1], "auto")

    def test_resource_aware_router_avoids_model_that_does_not_fit(self):
        with tempfile.TemporaryDirectory() as td:
            big = self._profile(id="big", runtime="external", priority=100, estimated_vram_gb=24, estimated_ram_gb=70)
            small = self._profile(id="small", runtime="external", priority=50, estimated_vram_gb=8, estimated_ram_gb=16)
            cfg = AgentConfig(models=[big, small])
            manager = RuntimeManager(cfg, base_dir=Path(td))
            manager.hardware = HardwareSnapshot(
                platform="test",
                total_ram_gb=64,
                available_ram_gb=48,
                gpus=[GPUInfo(0, "GPU", 12288, 2048, 10240)],
                nvidia_smi_available=True,
            )
            router = ModelRouter(cfg.models, resource_advisor=manager.resource_fit)
            decision = router.choose("Implement the backend and frontend for this feature")
            self.assertEqual(decision.model_id, "small")
            self.assertTrue(any("free VRAM" in reason or "CPU offload" in reason for reason in decision.reasons))


    def test_managed_cpu_offload_near_ram_fit_is_allowed_to_try_runtime_autofit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "deep.gguf").write_bytes(b"GGUF")
            fake_server = root / "llama-server.exe"
            fake_server.write_text("placeholder", encoding="utf-8")
            deep = self._profile(
                id="qwen3-coder-30b",
                endpoint="",
                executable=str(fake_server),
                model="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M",
                model_path="models/deep.gguf",
                roles=["deep_reasoner", "reviewer"],
                estimated_vram_gb=18.6,
                estimated_ram_gb=30.0,
                allow_cpu_offload=True,
            )
            manager = RuntimeManager(AgentConfig(models=[deep]), base_dir=root)
            manager.hardware = HardwareSnapshot(
                platform="test",
                total_ram_gb=64.0,
                available_ram_gb=29.8,
                gpus=[GPUInfo(0, "RTX 3080", 12288, 3072, 9216)],
                nvidia_smi_available=True,
            )

            fits, score, reason = manager.resource_fit(deep)

            self.assertTrue(fits)
            self.assertLess(score, 0)
            self.assertIn("CPU offload", reason)
            self.assertIn("tight estimated RAM fit 30.0 GB vs 29.8 GB available", reason)
            self.assertIn("runtime auto-fit allowed", reason)

    def test_managed_cpu_offload_still_rejects_clearly_oversized_ram_need(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "huge.gguf").write_bytes(b"GGUF")
            fake_server = root / "llama-server.exe"
            fake_server.write_text("placeholder", encoding="utf-8")
            huge = self._profile(
                id="huge",
                endpoint="",
                executable=str(fake_server),
                model_path="models/huge.gguf",
                roles=["deep_reasoner"],
                estimated_vram_gb=24.0,
                estimated_ram_gb=42.0,
                allow_cpu_offload=True,
            )
            manager = RuntimeManager(AgentConfig(models=[huge]), base_dir=root)
            manager.hardware = HardwareSnapshot(
                platform="test",
                total_ram_gb=64.0,
                available_ram_gb=29.8,
                gpus=[GPUInfo(0, "RTX 3080", 12288, 3072, 9216)],
                nvidia_smi_available=True,
            )

            fits, score, reason = manager.resource_fit(huge)

            self.assertFalse(fits)
            self.assertEqual(score, -100)
            self.assertIn("auto-fit margin", reason)

    def test_resource_fit_prefers_measured_twin_footprint(self):
        # Static profile estimate says "oversized" but measured history for
        # this exact model says it fits — measured basis must win.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "models").mkdir()
            gguf = root / "models" / "big.gguf"
            gguf.write_bytes(b"GGUF")
            fake_server = root / "llama-server.exe"
            fake_server.write_text("placeholder", encoding="utf-8")
            big = self._profile(
                id="big", endpoint="", executable=str(fake_server),
                model_path="models/big.gguf",
                estimated_vram_gb=24.0, estimated_ram_gb=42.0,
                allow_cpu_offload=True)
            manager = RuntimeManager(AgentConfig(models=[big]), base_dir=root)
            manager.hardware = HardwareSnapshot(
                platform="test", total_ram_gb=64.0, available_ram_gb=29.8,
                gpus=[GPUInfo(0, "RTX", 12288, 9216, 3072)],
                nvidia_smi_available=True)
            without_twin = manager.resource_fit(big)
            self.assertFalse(without_twin[0])
            from localcodeagent.twin import DigitalTwin
            twin = DigitalTwin(root / "twin.json", detect=lambda: {
                "ram_free_gb": 29.8,
                "gpus": [{"free_vram_mb": 9216}]})
            size_gb = gguf.stat().st_size / 1e9
            twin.record_model_measure(model_id="big", size_gb=size_gb,
                                      ram_used_gb=8.0, vram_used_mb=4000)
            manager.twin = twin
            fits, score, reason = manager.resource_fit(big)
            self.assertTrue(fits, reason)
            # Exact model_id wins over a closer-by-size measure: a tiny
            # "other" measure would predict ~1 GB, but the big model's own
            # record (8 GB) must be used.
            twin2 = DigitalTwin(root / "twin2.json", detect=lambda: {
                "ram_free_gb": 29.8, "gpus": [{"free_vram_mb": 9216}]})
            twin2.record_model_measure(model_id="big", size_gb=size_gb,
                                       ram_used_gb=8.0, vram_used_mb=4000)
            twin2.record_model_measure(model_id="other", size_gb=0.000005,
                                       ram_used_gb=1.0)
            manager.twin = twin2
            fits2, _, _ = manager.resource_fit(big)
            self.assertTrue(fits2)

    def test_measure_resident_reports_rss_and_file_size(self):
        import subprocess as sp, sys
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "models").mkdir()
            gguf = root / "models" / "m.gguf"
            gguf.write_bytes(b"x" * 12345)
            manager = RuntimeManager(AgentConfig(models=[]), base_dir=root)
            self.assertEqual(manager.measure_resident("none"), {})
            proc = sp.Popen([sys.executable, "-c",
                             "x=bytearray(64*1024*1024);"
                             "import time;time.sleep(60)"])
            try:
                manager._managed["m"] = _ManagedProcess(
                    profile=SimpleNamespace(model_path="models/m.gguf"),
                    process=proc, endpoint="", log_handle=None, status=None)
                meas = {}
                for _ in range(20):
                    meas = manager.measure_resident("m")
                    if meas.get("ram_used_gb", 0) > 0:
                        break
                    time.sleep(0.1)
                self.assertIn("ram_used_gb", meas)
                self.assertGreater(meas["ram_used_gb"], 0)
                self.assertAlmostEqual(meas["model_size_gb"], 12345 / 1e9,
                                       places=3)
            finally:
                proc.terminate()
                proc.wait(timeout=10)

    def test_resource_fit_counts_memory_released_by_resident_model_switch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "fast.gguf").write_bytes(b"GGUF")
            (models / "deep.gguf").write_bytes(b"GGUF")
            fake_server = root / "llama-server.exe"
            fake_server.write_text("placeholder", encoding="utf-8")
            fast = self._profile(
                id="fast",
                endpoint="",
                executable=str(fake_server),
                model_path="models/fast.gguf",
                roles=["primary_coder"],
                estimated_vram_gb=10,
                estimated_ram_gb=16,
            )
            deep = self._profile(
                id="deep",
                endpoint="",
                executable=str(fake_server),
                model_path="models/deep.gguf",
                roles=["deep_reasoner", "reviewer"],
                estimated_vram_gb=18.6,
                estimated_ram_gb=30,
                allow_cpu_offload=True,
            )
            cfg = AgentConfig(models=[fast, deep], max_resident_models=1)
            manager = RuntimeManager(cfg, base_dir=root)
            manager.hardware = HardwareSnapshot(
                platform="test",
                total_ram_gb=64,
                available_ram_gb=29.3,
                gpus=[GPUInfo(0, "GPU", 12288, 10240, 2048)],
                nvidia_smi_available=True,
            )
            manager.resident_model_ids = lambda: ["fast"]
            manager._last_used["fast"] = 1.0

            fits, score, reason = manager.resource_fit(deep)

            self.assertTrue(fits)
            self.assertLess(score, 0)
            self.assertIn("after releasing resident fast", reason)
            self.assertIn("CPU offload", reason)

    def test_evict_idle_stops_model_past_idle_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile(keep_loaded=False)
            cfg = AgentConfig(models=[profile], model_idle_unload_seconds=60)
            manager = RuntimeManager(cfg, base_dir=Path(td))
            proc = _attach_fake_managed(manager, profile, last_used=time.time() - 3600)

            stopped = manager.evict_idle()

            self.assertEqual(stopped, ["coder"])
            self.assertTrue(proc.terminated)
            self.assertEqual(manager._status["coder"].state, "stopped")

    def test_evict_idle_skips_recent_busy_and_keep_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            fresh = self._profile(id="fresh", model_path="models/fresh.gguf")
            stale_busy = self._profile(id="busy", model_path="models/busy.gguf")
            pinned = self._profile(id="pinned", model_path="models/pinned.gguf", keep_loaded=True)
            cfg = AgentConfig(models=[fresh, stale_busy, pinned], model_idle_unload_seconds=60)
            manager = RuntimeManager(cfg, base_dir=Path(td))
            _attach_fake_managed(manager, fresh)
            _attach_fake_managed(manager, stale_busy, last_used=time.time() - 3600)
            _attach_fake_managed(manager, pinned, last_used=time.time() - 3600)

            stopped = manager.evict_idle(busy_models={"busy"})

            self.assertEqual(stopped, [])
            self.assertEqual(sorted(manager.resident_model_ids()), ["busy", "fresh", "pinned"])

    def test_evict_idle_disabled_when_threshold_zero(self):
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            cfg = AgentConfig(models=[profile], model_idle_unload_seconds=0)
            manager = RuntimeManager(cfg, base_dir=Path(td))
            _attach_fake_managed(manager, profile, last_used=1.0)

            self.assertEqual(manager.evict_idle(), [])
            self.assertIn("coder", manager.resident_model_ids())

    def test_evict_idle_pressure_evicts_lru_not_busy(self):
        with tempfile.TemporaryDirectory() as td:
            old = self._profile(id="old", model_path="models/old.gguf")
            new = self._profile(id="new", model_path="models/new.gguf")
            cfg = AgentConfig(
                models=[old, new],
                model_idle_unload_seconds=0,
                memory_pressure_ram_gb=512,
            )
            manager = RuntimeManager(cfg, base_dir=Path(td))
            manager.hardware = HardwareSnapshot(
                platform="test", total_ram_gb=64, available_ram_gb=2,
                gpus=[], nvidia_smi_available=False,
            )
            manager.refresh_hardware = lambda: manager.hardware
            _attach_fake_managed(manager, old, last_used=time.time() - 100)
            _attach_fake_managed(manager, new)

            stopped = manager.evict_idle(busy_models={"new"})

            self.assertEqual(stopped, ["old"])
            self.assertEqual(manager.resident_model_ids(), ["new"])

    def test_evict_idle_pressure_never_evicts_keep_loaded(self):
        # Regression: keep_loaded residents are exempt from ambient pressure
        # eviction. On a GPU where the resident baseline itself leaves less
        # free VRAM than the floor, evicting it just to rewarm it is a thrash
        # loop (evict -> rewarm -> pressure -> evict every watchdog tick).
        with tempfile.TemporaryDirectory() as td:
            pinned = self._profile(id="pinned", model_path="models/pinned.gguf", keep_loaded=True)
            cfg = AgentConfig(
                models=[pinned],
                model_idle_unload_seconds=0,
                memory_pressure_vram_gb=4,
            )
            manager = RuntimeManager(cfg, base_dir=Path(td))
            manager.hardware = HardwareSnapshot(
                platform="test", total_ram_gb=64, available_ram_gb=32,
                gpus=[GPUInfo(0, "GPU", 12288, 1024, 11264)],
                nvidia_smi_available=True,
            )
            manager.refresh_hardware = lambda: manager.hardware
            proc = _attach_fake_managed(manager, pinned, last_used=time.time() - 3600)

            stopped = manager.evict_idle()

            self.assertEqual(stopped, [])
            self.assertFalse(proc.terminated)
            self.assertEqual(manager.resident_model_ids(), ["pinned"])
            self.assertEqual(manager._pending_rewarm, set())

    def test_reclaim_orphaned_port_kills_only_llama_orphans(self):
        # Backend crash leaves an unmanaged llama-server holding the model's
        # port; a managed sibling sharing the port must never be killed, and
        # neither must a foreign process squatting there.
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile(keep_loaded=False)
            cfg = AgentConfig(models=[profile])
            manager = RuntimeManager(cfg, base_dir=Path(td))
            sibling = _attach_fake_managed(manager, profile)
            sibling_pid = 4242
            manager._managed[profile.id].process.pid = sibling_pid

            killed: list[int] = []
            manager._listening_pids = lambda port: {sibling_pid, 7777, 8888}
            manager._process_image_name = lambda pid: {
                sibling_pid: "llama-server.exe",   # ours — managed, must survive
                7777: "llama-server.exe",          # orphan — kill
                8888: "chrome.exe",                # foreign — leave alone
            }[pid]
            manager._kill_pid = killed.append

            manager._reclaim_orphaned_port(8080)

            self.assertEqual(killed, [7777])

    def test_prewarm_retries_until_model_fits(self):
        # A failed resource_fit at boot must not leave the app cold — VRAM is
        # often still draining the previous session's models for the first
        # few seconds. Bounded retry warms the model once it fits.
        from types import SimpleNamespace
        from localcodeagent.server import AppState

        calls = {"fit": 0, "ready": 0}

        def fit(_p):
            calls["fit"] += 1
            return (calls["fit"] >= 3, 0, "")

        def ready(_p):
            calls["ready"] += 1

        state = AppState.__new__(AppState)
        state.runtime = SimpleNamespace(resource_fit=fit, ensure_ready=ready)
        AppState._prewarm_with_retry(state, self._profile(), attempts=5, delay=0)

        self.assertEqual(calls, {"fit": 3, "ready": 1})

    def test_launch_fallback_marks_tuned_bad(self):
        # Tuned launch crashes → heuristic retry succeeds; tuned args are
        # blacklisted so the next launch never re-picks them.
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            cfg = AgentConfig(models=[profile])
            manager = RuntimeManager(cfg, base_dir=Path(td))
            tuned = ["--batch-size", "2048", "--ubatch-size", "1024"]
            manager.tuner.record_result(profile, tuned, {"predicted_per_second": 99.0})
            calls: list[dict] = []

            def spawn(p, port, endpoint, *, apply_tuning, extra_args, ctx_override):
                calls.append({"tuned": apply_tuning, "extra": extra_args})
                if apply_tuning:
                    raise RuntimeError("llama-server exited: out of memory")
                return endpoint

            manager._spawn_and_wait = spawn
            out = manager._launch_with_fallback(profile, 18080, "http://127.0.0.1:18080/v1")
            self.assertTrue(out.endswith("/v1"))
            self.assertEqual([c["tuned"] for c in calls], [True, False])
            # tuned args now blacklisted — tuned_flags falls back to heuristics
            self.assertNotEqual(manager.tuner.tuned_flags(profile), tuned)

    def test_recover_marks_repeatedly_crashing_tuned_config_bad(self):
        # A tuned config that launches fine but dies mid-generation is real
        # evidence the config is broken — recover() must blacklist it rather
        # than relaunch the same args forever.
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=Path(td))
            tuned = ["--batch-size", "2048", "--ubatch-size", "1024"]
            manager.tuner.record_result(profile, tuned, {"predicted_per_second": 99.0})
            proc = _attach_fake_managed(manager, profile)
            manager._launch_tuning[profile.id] = list(tuned)
            log = Path(td) / "coder.log"
            log.write_text("llama-server: cudaMalloc failed" + chr(10), encoding="utf-8")
            manager._status[profile.id].log_path = str(log)
            manager._status[profile.id].restarts = 1
            proc.alive = False  # died mid-run

            manager._start_llama_cpp = lambda p, ctx_override=None: "http://127.0.0.1:9/v1"
            manager.recover(profile)

            bad = manager.tuner._data.get("bad_results", {}).get(profile.id, [])
            self.assertTrue(any(tuple(r.get("args") or ()) == tuple(tuned) for r in bad))
            self.assertNotEqual(manager.tuner.tuned_flags(profile), tuned)

    def test_recover_does_not_blacklist_on_first_crash(self):
        # One mid-run crash is not proof the config is bad — only repeated
        # failures blacklists, so a transient OOM does not permanently
        # discard a good tuned config.
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=Path(td))
            tuned = ["--batch-size", "2048"]
            proc = _attach_fake_managed(manager, profile)
            manager._launch_tuning[profile.id] = list(tuned)
            manager._status[profile.id].restarts = 0
            proc.alive = False

            manager._start_llama_cpp = lambda p, ctx_override=None: "http://127.0.0.1:9/v1"
            manager.recover(profile)

            bad = manager.tuner._data.get("bad_results", {}).get(profile.id, [])
            self.assertFalse(any(tuple(r.get("args") or ()) == tuple(tuned) for r in bad))

    def test_launch_fallback_bare_when_heuristic_also_fails(self):
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=Path(td))
            manager.tuner._find_llama = lambda: "llama-server"
            calls = []

            def spawn(p, port, endpoint, *, apply_tuning, extra_args, ctx_override):
                calls.append((apply_tuning, list(extra_args or [])))
                if len(calls) < 3:
                    raise RuntimeError("crash")
                return endpoint

            manager._spawn_and_wait = spawn
            manager._launch_with_fallback(profile, 18080, "http://x/v1")
            self.assertEqual(calls[-1], (False, []))  # bare last resort

    def test_context_growth_relaunches_resident(self):
        # Resident server at 8k; a task needing 32k must relaunch, not truncate.
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=Path(td))
            proc = _attach_fake_managed(manager, profile)
            manager._launch_ctx[profile.id] = 8192
            manager._health = lambda ep, timeout=1.5: (True, "ok")
            spawns: list[int | None] = []
            manager._enforce_residency = lambda p: None
            manager._reclaim_orphaned_port = lambda port: None

            def spawn(p, port, endpoint, *, apply_tuning, extra_args, ctx_override):
                spawns.append(ctx_override)
                manager._launch_ctx[p.id] = ctx_override or 8192
                return endpoint

            manager._spawn_and_wait = spawn
            manager._start_llama_cpp(profile, ctx_override=32768)
            self.assertTrue(proc.terminated)
            self.assertEqual(spawns, [32768])

    def test_resident_kept_when_ctx_sufficient(self):
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=Path(td))
            proc = _attach_fake_managed(manager, profile)
            manager._launch_ctx[profile.id] = 24576
            manager._health = lambda ep, timeout=1.5: (True, "ok")
            manager._spawn_and_wait = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no relaunch"))
            ep = manager._start_llama_cpp(profile, ctx_override=8192)
            self.assertFalse(proc.terminated)
            self.assertEqual(ep, "http://x/v1")

    def test_shrink_oversized_context_relaunches_idle_resident(self):
        # Resident grown to 32k by a big task, now idle past the grace
        # period — relaunch at the 16k role recommendation, not evict.
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=Path(td))
            proc = _attach_fake_managed(
                manager, profile, last_used=time.time() - 1200)
            manager._launch_ctx[profile.id] = 49152
            manager._health = lambda ep, timeout=1.5: (True, "ok")
            manager._enforce_residency = lambda p: None
            manager._reclaim_orphaned_port = lambda port: None
            spawns: list[int | None] = []

            def spawn(p, port, endpoint, *, apply_tuning, extra_args, ctx_override):
                spawns.append(ctx_override)
                manager._launch_ctx[p.id] = 16384
                return endpoint

            manager._spawn_and_wait = spawn
            shrunk = manager.shrink_oversized_context()
            self.assertEqual(shrunk, [profile.id])
            self.assertTrue(proc.terminated)
            self.assertEqual(spawns, [None])  # role default, no override

    def test_shrink_skips_busy_fresh_and_normal_size(self):
        with tempfile.TemporaryDirectory() as td:
            big = self._profile(id="big")
            busy = self._profile(id="busy")
            small = self._profile(id="small")
            manager = RuntimeManager(
                AgentConfig(models=[big, busy, small]), base_dir=Path(td))
            old = time.time() - 1200
            p_big = _attach_fake_managed(manager, big, last_used=old)
            p_busy = _attach_fake_managed(manager, busy, last_used=old)
            p_small = _attach_fake_managed(manager, small, last_used=old)
            manager._launch_ctx["big"] = 49152
            manager._launch_ctx["busy"] = 49152
            manager._launch_ctx["small"] = 16384  # at recommended size
            manager._health = lambda ep, timeout=1.5: (True, "ok")
            manager._enforce_residency = lambda p: None
            manager._reclaim_orphaned_port = lambda port: None
            manager._spawn_and_wait = lambda p, port, endpoint, **k: endpoint
            shrunk = manager.shrink_oversized_context(busy_models={"busy"})
            self.assertEqual(shrunk, ["big"])
            self.assertTrue(p_big.terminated)
            self.assertFalse(p_busy.terminated)
            self.assertFalse(p_small.terminated)
            # A recently-used oversized model is inside the grace window.
            p_fresh = _attach_fake_managed(manager, big)
            manager._launch_ctx["big"] = 49152
            self.assertEqual(
                manager.shrink_oversized_context(busy_models={"busy"}), [])
            self.assertFalse(p_fresh.terminated)

    def test_auto_tune_disabled_when_config_off(self):
        from types import SimpleNamespace
        from localcodeagent.server import AppState
        with tempfile.TemporaryDirectory() as td:
            state = AppState.__new__(AppState)
            state.config = AgentConfig(
                models=[self._profile()], runtime_auto_tune=False)
            state.runtime = SimpleNamespace(tuner=None)
            # The worker loops for the app's lifetime — earlier tests may
            # still have one alive, so assert no NEW thread spawns.
            before = sum(t.name == "runtime-auto-tune" and t.is_alive()
                         for t in threading.enumerate())
            AppState._start_auto_tune(state)
            after = sum(t.name == "runtime-auto-tune" and t.is_alive()
                        for t in threading.enumerate())
            self.assertEqual(after, before)

    def test_auto_tune_benchmarks_untuned_profile(self):
        from types import SimpleNamespace
        from localcodeagent.server import AppState
        import threading as _th
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            state = AppState.__new__(AppState)
            state.config = AgentConfig(
                models=[profile],
                runtime_auto_tune=True,
                runtime_auto_tune_idle_seconds=0.01)
            state.tasks = SimpleNamespace(recent=lambda n: [])
            state.events = SimpleNamespace(publish=lambda *a, **k: None)
            benchmarked: list[str] = []

            tuner = SimpleNamespace(
                _data={"results": {}},
                fingerprint=lambda p: "fp",
                benchmark=lambda p: benchmarked.append(p.id)
                    or {"status": "ok", "best": {"metrics": {"predicted_per_second": 42.0}}},
            )
            state.runtime = SimpleNamespace(tuner=tuner)
            AppState._start_auto_tune(state)
            deadline = time.time() + 10
            while not benchmarked and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(benchmarked, [profile.id])

    def test_auto_tune_skips_tuned_profile(self):
        from types import SimpleNamespace
        from localcodeagent.server import AppState
        with tempfile.TemporaryDirectory() as td:
            profile = self._profile()
            state = AppState.__new__(AppState)
            state.config = AgentConfig(
                models=[profile],
                runtime_auto_tune=True,
                runtime_auto_tune_idle_seconds=0.01)
            state.tasks = SimpleNamespace(recent=lambda n: [])
            state.events = SimpleNamespace(publish=lambda *a, **k: None)
            benchmarked: list[str] = []
            tuner = SimpleNamespace(
                _data={"results": {profile.id: {"fingerprint": "fp", "args": []}}},
                fingerprint=lambda p: "fp",
                benchmark=lambda p: benchmarked.append(p.id) or {},
            )
            state.runtime = SimpleNamespace(tuner=tuner)
            AppState._start_auto_tune(state)
            time.sleep(0.5)
            self.assertEqual(benchmarked, [])

    def test_prewarm_gives_up_after_bounded_attempts(self):
        from types import SimpleNamespace
        from localcodeagent.server import AppState

        calls = {"fit": 0, "ready": 0}
        state = AppState.__new__(AppState)
        state.runtime = SimpleNamespace(
            resource_fit=lambda _p: (calls.__setitem__("fit", calls["fit"] + 1) or (False, 0, "no room")),
            ensure_ready=lambda _p: calls.__setitem__("ready", calls["ready"] + 1),
        )
        AppState._prewarm_with_retry(state, self._profile(), attempts=3, delay=0)

        self.assertEqual(calls["fit"], 3)
        self.assertEqual(calls["ready"], 0)

    @unittest.skipIf(os.name == "nt", "fake executable uses POSIX permissions")
    def test_missing_deep_model_falls_back_to_available_primary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "fast.gguf").write_bytes(b"GGUF")
            fake_server = root / "llama-server"
            fake_server.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_server.chmod(0o755)
            fast = self._profile(
                id="fast",
                endpoint="",
                executable=str(fake_server),
                model_path="models/fast.gguf",
                roles=["utility", "fast_coder", "primary_coder"],
                estimated_vram_gb=8,
                estimated_ram_gb=16,
            )
            deep = self._profile(
                id="deep",
                endpoint="",
                executable=str(fake_server),
                model_path="models/missing-30b.gguf",
                roles=["deep_reasoner", "reviewer"],
                estimated_vram_gb=18,
                estimated_ram_gb=30,
                priority=100,
            )
            cfg = AgentConfig(models=[fast, deep])
            manager = RuntimeManager(cfg, base_dir=root)
            manager.hardware = HardwareSnapshot(
                platform="test",
                total_ram_gb=64,
                available_ram_gb=48,
                gpus=[GPUInfo(0, "GPU", 12288, 2048, 10240)],
                nvidia_smi_available=True,
            )
            router = ModelRouter(cfg.models, resource_advisor=manager.resource_fit)
            decision = router.choose("Investigate a race condition and refactor the architecture")
            self.assertEqual(decision.role, "deep_reasoner")
            self.assertEqual(decision.model_id, "fast")
            self.assertTrue(any("runnable primary-coder fallback" in reason for reason in decision.reasons))



    @unittest.skipIf(os.name == "nt", "fake executable test uses a POSIX shebang")
    def test_managed_runtime_start_health_and_stop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "coder.gguf").write_bytes(b"GGUF")
            fake_server = root / "llama-server"
            fake_server.write_text("""#!/usr/bin/env python3
import argparse, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
p=argparse.ArgumentParser(add_help=False)
p.add_argument('--port', type=int, required=True)
p.add_argument('--host', default='127.0.0.1')
args,_=p.parse_known_args()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in {'/health','/v1/health'}:
            data=b'{\"status\":\"ok\"}'
            self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        else: self.send_response(404); self.end_headers()
    def log_message(self,*a): pass
ThreadingHTTPServer((args.host,args.port),H).serve_forever()
""", encoding="utf-8")
            fake_server.chmod(0o755)
            profile = self._profile(endpoint="", executable=str(fake_server), startup_timeout=5)
            cfg = AgentConfig(models=[profile], max_resident_models=1)
            manager = RuntimeManager(cfg, base_dir=root)
            try:
                endpoint = manager.ensure_ready(profile)
                self.assertIn("/v1", endpoint)
                status = manager.statuses()[0]
                self.assertEqual(status["state"], "running")
                self.assertTrue(status["healthy"])
                self.assertIsNotNone(status["pid"])
                manager.stop_model(profile.id)
                self.assertEqual(manager.statuses()[0]["state"], "stopped")
            finally:
                manager.stop_all()

    def test_inventory_lists_gguf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir()
            (models / "a.gguf").write_bytes(b"x" * 1024)
            cfg = AgentConfig(models=[self._profile(runtime="external")])
            manager = RuntimeManager(cfg, base_dir=root)
            rows = manager.inventory()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "a.gguf")


class UnifiedLlamaRuntimeTests(unittest.TestCase):
    def test_discovers_unified_llama_command(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "localcodeagent.runtime.manager.shutil.which",
            side_effect=lambda name: "/usr/local/bin/llama" if name == "llama" else None,
        ):
            manager = RuntimeManager(AgentConfig(models=[]), base_dir=Path(td))
            self.assertEqual(manager.discover_llama_server(), "/usr/local/bin/llama")

    def test_unified_llama_build_command_inserts_serve_subcommand(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "model.gguf"
            model.write_bytes(b"GGUF")
            llama = root / "llama"
            llama.write_text("placeholder", encoding="utf-8")
            profile = ModelProfile(
                id="local",
                runtime="llama_cpp",
                endpoint="",
                model="test",
                model_path=str(model),
                executable=str(llama),
                roles=["primary_coder"],
            )
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=root)
            command = manager._build_command(profile, 8081)
            self.assertEqual(command[:2], [str(llama), "serve"])
            self.assertIn("--model", command)
            self.assertIn(str(model), command)

    def test_windows_runtime_guidance_uses_winget_without_auto_execution(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "localcodeagent.runtime.manager.platform.system", return_value="Windows"
        ), patch(
            "localcodeagent.runtime.manager.shutil.which", return_value=None
        ):
            manager = RuntimeManager(AgentConfig(models=[]), base_dir=Path(td))
            guidance = manager.runtime_install_guidance()
            self.assertFalse(guidance["installed"])
            self.assertEqual(guidance["platform"], "Windows")
            self.assertTrue(any(row["command"] == "winget install llama.cpp" for row in guidance["commands"]))
            self.assertIn("never executed automatically", guidance["note"])


class LiveRuntimeReconfigurationTests(unittest.TestCase):
    def test_reconfigure_models_rebuilds_runtime_state_without_new_manager(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = ModelProfile(
                id="old",
                endpoint="http://127.0.0.1:8081/v1",
                model="old",
                roles=["primary_coder"],
                runtime="external",
            )
            manager = RuntimeManager(AgentConfig(models=[first]), base_dir=root)
            identity = id(manager)

            second = ModelProfile(
                id="qwen3-14b",
                endpoint="http://127.0.0.1:8082/v1",
                model="Qwen3-14B-Q4_K_M",
                roles=["utility", "fast_coder", "primary_coder"],
                runtime="external",
            )
            new_config = AgentConfig(models=[second])
            manager.reconfigure_models(new_config)

            self.assertEqual(id(manager), identity)
            self.assertIs(manager.config, new_config)
            statuses = manager.statuses(probe_external=False)
            self.assertEqual([row["model_id"] for row in statuses], ["qwen3-14b"])
            self.assertEqual(statuses[0]["endpoint"], "http://127.0.0.1:8082/v1")


if __name__ == "__main__":
    unittest.main()
