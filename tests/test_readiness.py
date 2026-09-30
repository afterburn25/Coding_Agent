from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.runtime.hardware import GPUInfo, HardwareSnapshot
from localcodeagent.runtime.manager import RuntimeManager


ROOT = Path(__file__).resolve().parents[1]


def hardware(*, free_vram_mb: int = 10240, available_ram_gb: float = 48.0) -> HardwareSnapshot:
    return HardwareSnapshot(
        platform="test",
        total_ram_gb=64.0,
        available_ram_gb=available_ram_gb,
        gpus=[GPUInfo(0, "RTX 3080", 12288, 12288 - free_vram_mb, free_vram_mb)],
        nvidia_smi_available=True,
    )


class CodingReadinessTests(unittest.TestCase):
    def test_healthy_external_coding_endpoint_is_ready(self):
        profile = ModelProfile(
            id="external-coder",
            endpoint="http://127.0.0.1:8080/v1",
            model="coder",
            roles=["primary_coder", "deep_reasoner", "reviewer"],
            runtime="external",
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "localcodeagent.runtime.manager.detect_hardware", return_value=hardware()
        ):
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=Path(td))
            manager._health = lambda endpoint, timeout=1.5: (True, "ok")
            result = manager.readiness(probe_external=True)

        self.assertTrue(result["ready_to_code"])
        self.assertTrue(result["auto_routing_ready"])
        self.assertEqual(result["models"][0]["state"], "running")
        self.assertEqual(result["recommendations"], [])

    def test_unreachable_external_endpoint_reports_setup_required(self):
        profile = ModelProfile(
            id="external-coder",
            endpoint="http://127.0.0.1:8080/v1",
            model="coder",
            roles=["primary_coder"],
            runtime="external",
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "localcodeagent.runtime.manager.detect_hardware", return_value=hardware()
        ):
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=Path(td))
            manager._health = lambda endpoint, timeout=1.5: (False, "connection refused")
            result = manager.readiness(probe_external=True)

        self.assertFalse(result["ready_to_code"])
        self.assertIn("external endpoint is not reachable", result["models"][0]["issues"])
        self.assertTrue(any("Start the configured" in item for item in result["recommendations"]))

    def test_managed_gguf_and_llama_server_can_be_ready_before_launch(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "localcodeagent.runtime.manager.detect_hardware", return_value=hardware()
        ):
            root = Path(td)
            (root / "models").mkdir()
            model = root / "models" / "coder.gguf"
            model.write_bytes(b"GGUF")
            server = root / "llama-server"
            server.write_text("placeholder", encoding="utf-8")
            profile = ModelProfile(
                id="managed-coder",
                endpoint="",
                model="coder",
                roles=["fast_coder", "primary_coder", "deep_reasoner", "reviewer"],
                runtime="llama_cpp",
                model_path="models/coder.gguf",
                executable=str(server),
                estimated_vram_gb=8.0,
                estimated_ram_gb=16.0,
            )
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=root)
            result = manager.readiness(probe_external=False)

        self.assertTrue(result["ready_to_code"])
        self.assertTrue(result["auto_routing_ready"])
        self.assertTrue(result["models"][0]["runnable"])
        self.assertEqual(result["models"][0]["model_file"], str(model))
        self.assertEqual(result["models"][0]["issues"], [])

    def test_main_ui_surfaces_readiness_endpoint_and_states(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")

        self.assertIn('id="readinessPanel"', html)
        self.assertIn("/api/readiness", js)
        self.assertIn("Self-host ready", js)
        self.assertIn("Ready to code", js)
        self.assertIn("Setup required", js)
        self.assertIn('if path == "/api/readiness":', server)


if __name__ == "__main__":
    unittest.main()
