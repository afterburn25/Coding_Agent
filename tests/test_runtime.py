import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.router import ModelRouter
from localcodeagent.runtime.hardware import GPUInfo, HardwareSnapshot, parse_nvidia_smi_csv
from localcodeagent.runtime.manager import RuntimeManager


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

    def test_resource_aware_router_avoids_model_that_does_not_fit(self):
        with tempfile.TemporaryDirectory() as td:
            big = self._profile(id="big", priority=100, estimated_vram_gb=24, estimated_ram_gb=70)
            small = self._profile(id="small", priority=50, estimated_vram_gb=8, estimated_ram_gb=16)
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


if __name__ == "__main__":
    unittest.main()
