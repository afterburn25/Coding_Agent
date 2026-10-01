import io
import os
import tempfile
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
