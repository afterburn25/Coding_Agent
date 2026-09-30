from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.runtime.hardware import GPUInfo, HardwareSnapshot
from localcodeagent.runtime.manager import RuntimeManager
from localcodeagent.runtime.setup import suggest_model_profiles, write_suggested_models


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

    def test_missing_managed_model_reports_one_clear_missing_file_issue(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "localcodeagent.runtime.manager.detect_hardware", return_value=hardware()
        ):
            root = Path(td)
            server = root / ("llama-server.exe" if __import__("os").name == "nt" else "llama-server")
            server.write_text("placeholder", encoding="utf-8")
            profile = ModelProfile(
                id="missing-coder",
                endpoint="",
                model="coder",
                roles=["primary_coder"],
                runtime="llama_cpp",
                model_path="models/missing.gguf",
                executable=str(server),
                estimated_vram_gb=8.0,
                estimated_ram_gb=16.0,
            )
            manager = RuntimeManager(AgentConfig(models=[profile]), base_dir=root)
            result = manager.readiness(probe_external=False)

        issues = result["models"][0]["issues"]
        self.assertEqual(len([x for x in issues if "GGUF" in x]), 1)
        self.assertIn("GGUF model file is missing", issues[0])

    def test_main_ui_surfaces_readiness_endpoint_and_states(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")

        self.assertIn('id="readinessPanel"', html)
        self.assertIn("/api/readiness", js)
        self.assertIn("Self-host ready", js)
        self.assertIn("Ready to code", js)
        self.assertIn("Setup required", js)
        self.assertIn("Install recommended 14B", js)
        self.assertIn("Install full 14B + 30B stack", js)
        self.assertIn("Add 30B deep coder", js)
        self.assertIn("installModelPlan", js)
        self.assertIn("configureDownloadedModels", js)
        self.assertIn('if path == "/api/readiness":', server)


class ModelSetupPlannerTests(unittest.TestCase):
    def test_single_gguf_gets_all_core_coding_roles(self):
        suggestions = suggest_model_profiles([{
            "name": "coder-q4.gguf",
            "path": "/models/coder-q4.gguf",
            "size_gb": 8.2,
        }])
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["id"], "primary-local")
        self.assertTrue({"fast_coder", "primary_coder", "deep_reasoner", "reviewer"}.issubset(set(suggestions[0]["roles"])))

    def test_two_ggufs_keep_small_primary_and_large_deep(self):
        suggestions = suggest_model_profiles([
            {"name": "Qwen3-14B-Q4_K_M.gguf", "path": "/models/qwen14.gguf", "size_gb": 9.0},
            {"name": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "path": "/models/qwen30.gguf", "size_gb": 18.6},
        ])
        self.assertEqual([row["id"] for row in suggestions], ["qwen3-14b", "qwen3-coder-30b"])
        self.assertTrue({"utility", "fast_coder", "primary_coder"}.issubset(set(suggestions[0]["roles"])))
        self.assertEqual(set(suggestions[1]["roles"]), {"deep_reasoner", "reviewer"})

    def test_catalog_pair_is_preferred_even_with_other_ggufs_present(self):
        suggestions = suggest_model_profiles([
            {"name": "tiny-other.gguf", "path": "/models/tiny.gguf", "size_gb": 2.0},
            {"name": "Qwen3-14B-Q4_K_M.gguf", "path": "/models/qwen14.gguf", "size_gb": 9.0},
            {"name": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "path": "/models/qwen30.gguf", "size_gb": 18.6},
        ])
        self.assertEqual([row["id"] for row in suggestions], ["qwen3-14b", "qwen3-coder-30b"])
        self.assertEqual(set(suggestions[0]["roles"]), {"utility", "fast_coder", "primary_coder"})
        self.assertEqual(set(suggestions[1]["roles"]), {"deep_reasoner", "reviewer"})

    def test_three_ggufs_are_split_across_fast_primary_and_deep_roles(self):
        suggestions = suggest_model_profiles([
            {"name": "small.gguf", "path": "/models/small.gguf", "size_gb": 4.0},
            {"name": "medium.gguf", "path": "/models/medium.gguf", "size_gb": 9.0},
            {"name": "large.gguf", "path": "/models/large.gguf", "size_gb": 20.0},
        ])
        self.assertEqual([row["id"] for row in suggestions], ["fast-coder", "primary-coder", "deep-reasoner"])
        self.assertIn("fast_coder", suggestions[0]["roles"])
        self.assertIn("primary_coder", suggestions[1]["roles"])
        self.assertIn("deep_reasoner", suggestions[2]["roles"])

    def test_setup_writer_preserves_non_model_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text('{"research_enabled": false, "permissions": {"filesystem.read": "allow"}, "models": []}\n', encoding="utf-8")
            suggestions = suggest_model_profiles([{
                "name": "coder.gguf",
                "path": str(Path(td) / "coder.gguf"),
                "size_gb": 7.0,
            }])
            result = write_suggested_models(path, suggestions)
            import json
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(saved["research_enabled"])
            self.assertEqual(saved["permissions"]["filesystem.read"], "allow")
            self.assertEqual(saved["models"][0]["model_path"], suggestions[0]["model_path"])
            self.assertTrue(result["restart_required"])

    def test_setup_requires_explicit_apply_in_server_and_ui(self):
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('if path == "/api/readiness/configure":', server)
        self.assertIn('body.get("apply") is not True', server)
        self.assertIn("Use discovered models", app)
        self.assertIn("apply:true", app)


class LiveFirstRunSetupTests(unittest.TestCase):
    def test_first_run_setup_activates_models_without_restart(self):
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("reload_model_configuration", server)
        self.assertIn('saved["restart_required"] = False', server)
        self.assertIn("Coding models configured and activated.", server)
        self.assertIn("ready to use without restarting", app)
        self.assertNotIn("Restart Chat Nexus once to activate", app)


    def test_first_run_setup_hides_raw_missing_paths_behind_details(self):
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Technical model status", app)
        self.assertIn("const modelStatus=quickSetup?", app)
        self.assertEqual(app.count("function formatBytes(value)"), 1)


if __name__ == "__main__":
    unittest.main()
