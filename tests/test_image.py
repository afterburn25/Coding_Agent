from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path

from localcodeagent.image.catalog import discover_image_models
from localcodeagent.image.errors import describe_image_error
from localcodeagent.image.policy import ConsentStore, ImageSafetyPolicy
from localcodeagent.image.library import ImageAssetLibrary
from localcodeagent.image.manager import ImageManager
from localcodeagent.image.router import ImageRouter
from localcodeagent.image.runtime import ComfyUIRuntime
from localcodeagent.image.types import ImageModelProfile, ImageRequest
from localcodeagent.image.workflow import WorkflowManager


ROOT = Path(__file__).resolve().parents[1]


def _stub_comfy(root: Path) -> None:
    """A discoverable-but-not-running ComfyUI — satisfies the installed check
    so create_job still exercises the offline/backend_starting paths."""
    comfy = root / "ComfyUI"
    comfy.mkdir(exist_ok=True)
    (comfy / "main.py").write_text("# stub", encoding="utf-8")


class ImageErrorTests(unittest.TestCase):
    def test_error_classifier_maps_common_failures(self):
        cases = [
            (RuntimeError("CUDA out of memory while allocating tensor"), "cuda_out_of_memory"),
            (RuntimeError("ComfyUI backend is offline: connection refused"), "backend_offline"),
            (RuntimeError("VAE not found"), "missing_vae"),
            (ValueError("LoRA Alice is not marked compatible with flux"), "incompatible_lora"),
            (RuntimeError("Image model is not fully installed. Missing/invalid: diffusion_model"), "missing_model"),
            (RuntimeError("ComfyUI workflow is missing: qwen.json"), "unsupported_workflow"),
            (RuntimeError("ComfyUI is missing required node(s): Foo"), "failed_dependency"),
            (OSError("No space left on device"), "insufficient_disk_space"),
            (TimeoutError("Timed out waiting for ComfyUI"), "timeout"),
        ]
        for exc, code in cases:
            with self.subTest(code=code):
                row = describe_image_error(exc)
                self.assertEqual(row["code"], code)
                self.assertTrue(row["message"])
                self.assertIn(type(exc).__name__, row["technical_details"])

    def test_unknown_error_keeps_short_message_and_separate_details(self):
        row = describe_image_error(RuntimeError("unexpected backend response"))
        self.assertEqual(row["code"], "generation_failed")
        self.assertEqual(row["message"], "unexpected backend response")
        self.assertEqual(row["technical_details"], "RuntimeError: unexpected backend response")


class ImageRouterTests(unittest.TestCase):
    def setUp(self):
        self.qwen = ImageModelProfile(
            id="qwen", family="qwen-image-2.1",
            capabilities=["text_to_image", "image_edit", "inpaint", "outpaint", "background_removal", "multi_reference"],
            priority=80, quality_tier="high", speed_tier="balanced", max_reference_images=10, supports_transparency=True,
        )
        self.flux = ImageModelProfile(
            id="flux", family="flux.2-klein",
            capabilities=["text_to_image", "image_edit", "multi_reference"],
            priority=70, quality_tier="balanced", speed_tier="fast", max_reference_images=4,
        )
        self.router = ImageRouter([self.qwen, self.flux])

    def test_fast_preview_routes_flux(self):
        decision = self.router.choose(ImageRequest(prompt="quick concept", quality="preview"))
        self.assertEqual(decision.model_id, "flux")
        self.assertEqual(decision.operation, "text_to_image")

    def test_high_quality_routes_qwen(self):
        decision = self.router.choose(ImageRequest(prompt="photorealistic portrait", quality="high"))
        self.assertEqual(decision.model_id, "qwen")

    def test_background_removal_routes_capable_model(self):
        decision = self.router.choose(ImageRequest(prompt="remove background", source_image="a.png"))
        self.assertEqual(decision.operation, "remove_background")
        self.assertEqual(decision.model_id, "qwen")

    def test_reference_limit(self):
        req = ImageRequest(prompt="edit", reference_images=[f"{i}.png" for i in range(5)])
        decision = self.router.choose(req)
        self.assertEqual(decision.model_id, "qwen")


class ImagePolicyTests(unittest.TestCase):
    def test_explicit_real_person_requires_consent(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConsentStore(Path(td) / "consents.json")
            policy = ImageSafetyPolicy(store)
            allowed, _ = policy.check("make this explicit nude", real_person=True, subject="Alex")
            self.assertFalse(allowed)
            store.save(ConsentStore.create("Alex", scope="explicit image editing"))
            allowed, _ = policy.check("make this explicit nude", real_person=True, subject="Alex")
            self.assertTrue(allowed)

    def test_minor_sexual_request_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            policy = ImageSafetyPolicy(ConsentStore(Path(td) / "consents.json"))
            allowed, _ = policy.check("explicit nude minor")
            self.assertFalse(allowed)

    def test_explicit_detection_is_reusable_for_brain_gate(self):
        self.assertTrue(ImageSafetyPolicy.is_explicit("generate a naked adult woman"))
        self.assertTrue(ImageSafetyPolicy.is_explicit("explicit adult portrait"))
        self.assertFalse(ImageSafetyPolicy.is_explicit("portrait of an adult woman in a blue dress"))

    def test_adult_synthetic_naked_request_is_allowed_and_minor_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            policy = ImageSafetyPolicy(ConsentStore(Path(td) / "consents.json"))
            allowed, reason = policy.check("generate a naked adult woman")
            self.assertTrue(allowed)
            self.assertEqual(reason, "allowed")

            blocked, reason = policy.check("generate a naked minor")
            self.assertFalse(blocked)
            self.assertIn("minors", reason.lower())


class ComfyRuntimeOnDemandTests(unittest.TestCase):
    class _Backend:
        endpoint = "http://127.0.0.1:8188"

        def health(self):
            return False, "connection refused"

    def test_image_request_can_start_discoverable_comfyui_on_demand(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comfy = root / "ComfyUI"
            comfy.mkdir()
            (comfy / "main.py").write_text("# test", encoding="utf-8")
            config = SimpleNamespace(
                comfyui_auto_start=False,
                comfyui_start_on_image_request=True,
                comfyui_dir=str(comfy),
                comfyui_python=sys.executable,
                comfyui_logs_dir=".agent/runtime",
                comfyui_startup_timeout=10,
                comfyui_extra_args=[],
            )
            runtime = ComfyUIRuntime(base_dir=root, backend=self._Backend(), config=config)
            with patch.object(runtime, "start") as start:
                runtime.ensure_ready()
                start.assert_called_once()

    def test_healthy_orphaned_managed_comfyui_is_reclaimed(self):
        # Backend crash left our spawned ComfyUI alive: it answers health as
        # "external" and would escape idle eviction forever while holding VRAM.
        # The pid marker identifies it as ours → kill and respawn managed.
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comfy = root / "ComfyUI"
            comfy.mkdir()
            (comfy / "main.py").write_text("# test", encoding="utf-8")
            config = SimpleNamespace(
                comfyui_auto_start=False,
                comfyui_start_on_image_request=True,
                comfyui_dir=str(comfy),
                comfyui_python=sys.executable,
                comfyui_logs_dir=".agent/runtime",
                comfyui_startup_timeout=10,
                comfyui_extra_args=[],
            )

            class _HealthyBackend:
                endpoint = "http://127.0.0.1:8188"
                calls = 0

                def health(self):
                    self.calls += 1
                    return (self.calls == 1), "ok"  # healthy, then dead after kill

            runtime = ComfyUIRuntime(base_dir=root, backend=_HealthyBackend(), config=config)
            marker_dir = root / ".agent" / "runtime"
            marker_dir.mkdir(parents=True)
            (marker_dir / "comfyui-managed.json").write_text(
                json.dumps({"pid": 12345, "exe": sys.executable}), encoding="utf-8"
            )
            runtime._pid_cmdline_matches = lambda pid, exe: pid == 12345 and exe == sys.executable
            killed: list[int] = []
            runtime._kill_orphan = killed.append
            with patch.object(runtime, "start") as start:
                runtime.ensure_ready()
            self.assertEqual(killed, [12345])
            start.assert_called_once()

    def test_healthy_foreign_comfyui_stays_external(self):
        # A user's own ComfyUI on the endpoint (no marker / different exe) is
        # adopted as external — never killed, never respawned.
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = SimpleNamespace(
                comfyui_auto_start=False,
                comfyui_start_on_image_request=True,
                comfyui_dir=str(root / "ComfyUI"),
                comfyui_python=sys.executable,
                comfyui_logs_dir=".agent/runtime",
                comfyui_startup_timeout=10,
                comfyui_extra_args=[],
            )

            class _HealthyBackend:
                endpoint = "http://127.0.0.1:8188"

                def health(self):
                    return True, "ok"

            runtime = ComfyUIRuntime(base_dir=root, backend=_HealthyBackend(), config=config)
            marker_dir = root / ".agent" / "runtime"
            marker_dir.mkdir(parents=True)
            (marker_dir / "comfyui-managed.json").write_text(
                json.dumps({"pid": 12345, "exe": "D:\\other\\python.exe"}), encoding="utf-8"
            )
            runtime._pid_cmdline_matches = lambda pid, exe: False
            killed: list[int] = []
            runtime._kill_orphan = killed.append
            runtime.ensure_ready()
            self.assertEqual(killed, [])
            self.assertEqual(runtime.status.state, "external")

    def test_missing_comfyui_reports_setup_required(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = SimpleNamespace(
                comfyui_auto_start=False,
                comfyui_start_on_image_request=True,
                comfyui_dir=str(root / "missing-ComfyUI"),
                comfyui_python=sys.executable,
                comfyui_logs_dir=".agent/runtime",
                comfyui_startup_timeout=10,
                comfyui_extra_args=[],
            )
            runtime = ComfyUIRuntime(base_dir=root, backend=self._Backend(), config=config)
            with self.assertRaisesRegex(RuntimeError, "no local ComfyUI checkout"):
                runtime.ensure_ready()

    def test_discovers_bundled_comfyui_portable_python(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comfy = root / "ComfyUI_windows_portable" / "ComfyUI"
            python = root / "ComfyUI_windows_portable" / "python_embeded" / "python.exe"
            comfy.mkdir(parents=True)
            python.parent.mkdir(parents=True)
            (comfy / "main.py").write_text("# test", encoding="utf-8")
            python.write_bytes(b"MZ")
            config = SimpleNamespace(
                comfyui_auto_start=False,
                comfyui_start_on_image_request=True,
                comfyui_dir="ComfyUI",
                comfyui_python="",
            )
            runtime = ComfyUIRuntime(base_dir=root, backend=self._Backend(), config=config)
            directory, executable = runtime.discover()
            self.assertEqual(directory, comfy.resolve())
            self.assertEqual(Path(executable), python.resolve())


class WorkflowTests(unittest.TestCase):
    def test_bundled_default_image_workflows_are_api_format(self):
        manager = WorkflowManager(ROOT / "workflows" / "image")
        expected = (
            "qwen/qwen-image-2.1-t2i-api.json",
            "qwen/qwen-image-2.1-edit-api.json",
            "qwen/qwen-image-2.1-inpaint-api.json",
            "qwen/qwen-image-2.1-background-removal-api.json",
            "flux/flux2-klein-4b-t2i-api.json",
            "flux/flux2-klein-4b-edit-api.json",
        )
        for name in expected:
            with self.subTest(name=name):
                status = manager.inspect(name)
                self.assertTrue(status["exists"])
                self.assertTrue(status["valid"], status["errors"])
                self.assertGreater(status["node_count"], 0)

    def test_default_image_component_catalog_has_sizes_and_hashes(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        models = {row["id"]: row for row in config["image_models"]}
        for model_id in ("qwen-image-2.1", "flux2-klein-4b"):
            required = [row for row in models[model_id]["components"] if row.get("required", True)]
            self.assertEqual(len(required), 3)
            for component in required:
                self.assertGreater(int(component.get("size_bytes", 0)), 0)
                self.assertEqual(len(str(component.get("sha256", ""))), 64)

    def test_workflow_render_preserves_types(self):
        wf = {"1": {"inputs": {"text": "${prompt}", "seed": "${seed}", "label": "hello ${prompt}"}}}
        rendered = WorkflowManager.render(wf, {"prompt": "cat", "seed": 42})
        self.assertEqual(rendered["1"]["inputs"]["text"], "cat")
        self.assertEqual(rendered["1"]["inputs"]["seed"], 42)
        self.assertEqual(rendered["1"]["inputs"]["label"], "hello cat")

    def test_api_workflow_validation_rejects_ui_export(self):
        ui_workflow = {"nodes": [{"id": 1, "type": "KSampler"}], "links": []}
        status = WorkflowManager.validate_api(ui_workflow)
        self.assertFalse(status["valid"])
        self.assertEqual(status["format"], "ui")
        self.assertTrue(any("API format" in message for message in status["errors"]))

    def test_api_workflow_validation_tracks_template_variables(self):
        api_workflow = {
            "1": {"class_type": "ExampleNode", "inputs": {"prompt": "${prompt}", "seed": "${seed}"}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        }
        status = WorkflowManager.validate_api(api_workflow)
        self.assertTrue(status["valid"])
        self.assertEqual(status["node_count"], 2)
        self.assertEqual(status["unresolved_tokens"], ["prompt", "seed"])

    def test_save_api_workflow_is_atomic_and_rejects_ui_export(self):
        with tempfile.TemporaryDirectory() as td:
            manager = WorkflowManager(Path(td))
            valid = {"1": {"class_type": "KSampler", "inputs": {"seed": "${seed}"}}}
            saved = manager.save_api("qwen/generate.json", valid)
            self.assertTrue(saved["valid"])
            target = Path(td) / "qwen" / "generate.json"
            original = target.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"Export \(API\)"):
                manager.save_api("qwen/generate.json", {"nodes": [{"id": 1, "type": "KSampler"}]})
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_save_api_workflow_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as td:
            manager = WorkflowManager(Path(td) / "workflows")
            valid = {"1": {"class_type": "KSampler", "inputs": {}}}
            with self.assertRaises(PermissionError):
                manager.save_api("../escape.json", valid)

    def test_catalog_discovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "qwen").mkdir()
            (root / "qwen" / "Qwen-Image-2.1-Q4.gguf").write_bytes(b"1234")
            rows = discover_image_models(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["family"], "qwen-image")


class ImageLibraryTests(unittest.TestCase):
    def test_per_operation_workflow_selection(self):
        profile = ImageModelProfile(
            id="qwen", family="qwen-image", workflow="default.json",
            workflows={"image_edit": "edit.json", "inpaint": "inpaint.json"},
        )
        self.assertEqual(profile.workflow_for("image_edit"), "edit.json")
        self.assertEqual(profile.workflow_for("text_to_image"), "default.json")
        legacy = ImageModelProfile(id="legacy", family="qwen-image", workflows={"background_removal": "bg.json"})
        self.assertEqual(legacy.workflow_for("remove_background"), "bg.json")

    def test_model_verification_and_lora_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models" / "image"
            workflows = root / "workflows" / "image"
            model_path = models / "qwen" / "model.gguf"
            model_path.parent.mkdir(parents=True)
            model_path.write_bytes(b"model")
            workflows.mkdir(parents=True)
            (workflows / "generate.json").write_text("{}", encoding="utf-8")
            lib = ImageAssetLibrary(base_dir=root, models_dir=models, workflows_dir=workflows)
            profile = ImageModelProfile(
                id="qwen", family="qwen-image",
                components=[{"key": "model", "path": "models/image/qwen/model.gguf", "required": True}],
                workflows={"text_to_image": "generate.json"},
            )
            status = lib.verify_model(profile)
            self.assertTrue(status["installed"])
            self.assertTrue(status["workflows"][0]["exists"])

            lora = models / "loras" / "portrait.safetensors"
            lora.write_bytes(b"lora")
            saved = lib.save_lora_metadata(str(lora), {"name": "Portrait", "version": "1"})
            self.assertEqual(saved["name"], "Portrait")
            rows = lib.list_loras()
            self.assertEqual(rows[0]["name"], "Portrait")

    def test_image_manager_imports_only_configured_operation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ImageModelProfile(
                id="qwen", family="qwen-image",
                workflows={"text_to_image": "qwen/generate.json"},
                capabilities=["text_to_image"],
            )
            config = SimpleNamespace(
                image_models_dir="models/image", image_data_dir="data/image",
                image_workflows_dir="workflows/image", comfyui_endpoint="http://127.0.0.1:8188",
                comfyui_auto_start=False, image_resource_mode="balanced",
            )
            manager = ImageManager(base_dir=root, models=[profile], config=config, workspace=root / "workspace")
            valid = {"1": {"class_type": "KSampler", "inputs": {"seed": "${seed}"}}}
            result = manager.import_workflow("qwen", "text_to_image", valid)
            self.assertTrue(result["workflow"]["valid"])
            self.assertTrue((root / "workflows/image/qwen/generate.json").is_file())
            with self.assertRaisesRegex(ValueError, "no configured workflow"):
                manager.import_workflow("qwen", "inpaint", valid)

    def test_lora_resolution_enforces_compatibility_version_and_strength(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models" / "image"
            workflows = root / "workflows" / "image"
            lora = models / "loras" / "characters" / "alice.safetensors"
            lora.parent.mkdir(parents=True)
            lora.write_bytes(b"lora")
            lib = ImageAssetLibrary(base_dir=root, models_dir=models, workflows_dir=workflows)
            lib.save_lora_metadata(str(lora), {
                "id": "alice-v2", "name": "Alice", "version": "2",
                "compatible_families": ["qwen-image-2.1"], "strength": 0.75,
            })
            profile = ImageModelProfile(id="qwen", family="qwen-image-2.1", max_loras=2)
            rows = lib.resolve_loras([{"id": "alice-v2"}], profile)
            self.assertEqual(rows[0]["name"], "characters/alice.safetensors")
            self.assertEqual(rows[0]["strength"], 0.75)
            rows = lib.resolve_loras([{"name": "Alice", "version": "2", "strength": 1.2}], profile)
            self.assertEqual(rows[0]["strength"], 1.2)
            with self.assertRaisesRegex(ValueError, "not marked compatible"):
                lib.resolve_loras([{"name": "Alice"}], ImageModelProfile(id="flux", family="flux.2-klein"))
            with self.assertRaisesRegex(ValueError, "installed version"):
                lib.resolve_loras([{"name": "Alice", "version": "1"}], profile)
            with self.assertRaisesRegex(ValueError, "between -4 and 4"):
                lib.resolve_loras([{"name": "Alice", "strength": 9}], profile)

    def test_creator_locked_adult_subroutine_gates_explicit_images_inside_manager(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ImageModelProfile(
                id="qwen", family="qwen-image-2.1", capabilities=["text_to_image"],
                workflows={"text_to_image": "qwen/generate.json"},
            )
            config = SimpleNamespace(
                image_models_dir="models/image", image_data_dir="data/image", image_workflows_dir="workflows/image",
                comfyui_endpoint="http://127.0.0.1:8188", comfyui_auto_start=False, image_resource_mode="balanced",
                image_auto_run_jobs=False,
            )
            _stub_comfy(root)
            manager = ImageManager(base_dir=root, models=[profile], config=config, workspace=root / "workspace")
            manager.adult_content_allowed = lambda: False
            with self.assertRaisesRegex(PermissionError, "creator-locked Nexus Brain"):
                manager.create_job(ImageRequest(prompt="generate a naked adult woman"))

            safe = manager.create_job(ImageRequest(prompt="portrait of an adult woman in a blue dress"))
            self.assertEqual(safe.state, "queued")

    def test_subject_profile_applies_references_loras_model_and_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ImageModelProfile(
                id="qwen", family="qwen-image-2.1", capabilities=["image_edit", "text_to_image"],
                max_reference_images=10,
            )
            config = SimpleNamespace(
                image_models_dir="models/image", image_data_dir="data/image", image_workflows_dir="workflows/image",
                comfyui_endpoint="http://127.0.0.1:8188", comfyui_auto_start=False, image_resource_mode="balanced",
                image_auto_run_jobs=False,
            )
            _stub_comfy(root)
            manager = ImageManager(base_dir=root, models=[profile], config=config, workspace=root / "workspace")
            manager.profiles.save({
                "id":"alice", "display_name":"Alice", "preferred_model":"qwen",
                "reference_images":[str(root / "data/image/references/a.png")],
                "assigned_lora":"Alice", "lora_version":"2", "lora_strength":0.8,
                "generation_defaults":{"quality":"high", "width":768, "height":1152},
            })
            req = ImageRequest(prompt="portrait", subject_profile="alice")
            job = manager.create_job(req)
            saved = job.request
            self.assertEqual(saved["model_override"], "qwen")
            self.assertEqual(saved["quality"], "high")
            self.assertEqual(saved["width"], 768)
            self.assertEqual(saved["height"], 1152)
            self.assertEqual(saved["reference_images"], [str(root / "data/image/references/a.png")])
            self.assertEqual(saved["loras"][0]["name"], "Alice")
            self.assertEqual(saved["loras"][0]["version"], "2")
            self.assertEqual(saved["loras"][0]["strength"], 0.8)

    def test_lora_workflow_slots_are_required_for_selected_loras(self):
        status = {"unresolved_tokens": ["prompt", "lora_1_name", "lora_1_strength"]}
        ImageManager._validate_lora_slots("generate.json", status, [{"name":"a.safetensors"}])
        with self.assertRaisesRegex(RuntimeError, "LoRA slot 2"):
            ImageManager._validate_lora_slots("generate.json", status, [{"name":"a.safetensors"},{"name":"b.safetensors"}])
        with self.assertRaisesRegex(RuntimeError, "lora_1_strength"):
            ImageManager._validate_lora_slots("generate.json", {"unresolved_tokens":["lora_1_name"]}, [{"name":"a.safetensors"}])

    def test_image_errors_are_user_friendly_and_keep_technical_details(self):
        row = describe_image_error(RuntimeError("CUDA out of memory while allocating tensor"))
        self.assertEqual(row["code"], "cuda_out_of_memory")
        self.assertIn("GPU ran out of memory", row["message"])
        self.assertIn("RuntimeError", row["technical_details"])

    def test_image_error_maps_lora_and_backend_failures(self):
        lora = describe_image_error(ValueError("LoRA Alice is not marked compatible with flux.2-klein"))
        self.assertEqual(lora["code"], "incompatible_lora")
        backend = describe_image_error(ConnectionError("connection refused by ComfyUI backend"))
        self.assertEqual(backend["code"], "backend_offline")

    def test_transport_reset_keeps_friendly_message_and_diagnostic(self):
        from localcodeagent.netdiag import BackendConnectionError
        exc = BackendConnectionError(
            ConnectionResetError(10054, "forcibly closed"),
            subsystem="comfyui", url="http://127.0.0.1:8188/prompt")
        row = describe_image_error(exc)
        self.assertEqual(row["code"], "transport_connection_reset")
        self.assertIn("ComfyUI", row["message"])
        self.assertEqual(row["diagnostic"]["kind"], "connection_reset")
        self.assertEqual(row["diagnostic"]["port"], 8188)

    def test_unknown_image_error_is_bounded_for_normal_ui(self):
        row = describe_image_error(RuntimeError("x" * 800))
        self.assertEqual(row["code"], "generation_failed")
        self.assertLessEqual(len(row["message"]), 360)
        self.assertTrue(row["technical_details"].startswith("RuntimeError:"))

    def test_existing_verified_model_is_not_replaced_without_repair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models" / "image"
            workflows = root / "workflows" / "image"
            target = models / "qwen" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"existing")
            lib = ImageAssetLibrary(base_dir=root, models_dir=models, workflows_dir=workflows)
            spec = {
                "path": "models/image/qwen/model.gguf",
                "url": "https://example.invalid/model.gguf",
                "size_bytes": len(b"existing"),
            }
            result = lib._download_component(spec, repair=False, progress=lambda *_: None)
            self.assertEqual(result["status"], "existing")
            self.assertEqual(target.read_bytes(), b"existing")


class ComfyUIProgressListenerTests(unittest.TestCase):
    def _fake_ws_server(self, messages):
        """Serve one WS connection: 101 handshake then unmasked text frames."""
        import base64
        import hashlib
        import socket
        import struct
        import threading

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def serve():
            conn, _ = sock.accept()
            try:
                req = b""
                while b"\r\n\r\n" not in req:
                    req += conn.recv(4096)
                key = [l.split(":", 1)[1].strip() for l in req.decode().split("\r\n")
                       if l.lower().startswith("sec-websocket-key")][0]
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                conn.sendall((
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                for msg in messages:
                    payload = json.dumps(msg).encode()
                    conn.sendall(bytes([0x81, len(payload)]) + payload)
                import time
                time.sleep(0.5)  # keep socket open so the listener can read
            except OSError:
                pass
            finally:
                conn.close()
                sock.close()

        threading.Thread(target=serve, daemon=True).start()
        return port

    def test_progress_events_tracked_per_prompt(self):
        from localcodeagent.image.ws import ComfyUIProgressListener
        port = self._fake_ws_server([
            {"type": "executing", "data": {"prompt_id": "p1", "node": "KSampler"}},
            {"type": "progress", "data": {"prompt_id": "p1", "value": 3, "max": 10}},
            {"type": "executed", "data": {"prompt_id": "p2", "node": "Save"}},
        ])
        listener = ComfyUIProgressListener(f"http://127.0.0.1:{port}")
        listener.start()
        try:
            deadline = __import__("time").time() + 5
            frac = None
            while __import__("time").time() < deadline:
                frac, node = listener.progress_for("p1")
                if frac is not None:
                    break
                __import__("time").sleep(0.05)
            frac, node = listener.progress_for("p1")
            self.assertAlmostEqual(frac, 0.3)
            self.assertEqual(node, "KSampler")
            done, _ = listener.progress_for("p2")
            self.assertEqual(done, 1.0)
            self.assertEqual(listener.progress_for("nope"), (None, None))
        finally:
            listener.stop()


class JuggernautDefaultTests(unittest.TestCase):
    """Juggernaut X v10 (RunDiffusion SDXL) is the default text-to-image model."""

    PINNED_REVISION = "e53841ec9fc47ad9b803d6bfcfb3c00bdd815023"
    PINNED_SHA256 = "d91d35736d8f2be038f760a9b0009a771ecf0a417e9b38c244a84ea4cb9c0c45"
    CHECKPOINT_REL = "models/image/stable-diffusion/checkpoints/Juggernaut-X-RunDiffusion-NSFW.safetensors"
    WORKFLOW_REL = "sdxl/juggernaut-x-v10-t2i-api.json"

    def setUp(self):
        from localcodeagent.config import default_config
        self.models = {m.id: m for m in default_config().image_models}
        self.jug = self.models["juggernaut-x-v10"]
        self.router = ImageRouter(list(self.models.values()))

    def test_profile_exists(self):
        self.assertEqual(self.jug.family, "stable-diffusion-xl")
        self.assertEqual(self.jug.capabilities, ["text_to_image"])
        self.assertGreater(self.jug.priority, self.models["qwen-image-2.1"].priority)
        self.assertGreater(self.models["qwen-image-2.1"].priority, self.models["flux2-klein-4b"].priority)

    def test_checkpoint_path(self):
        self.assertEqual(self.jug.model_path, self.CHECKPOINT_REL)
        self.assertEqual(self.jug.components[0]["key"], "checkpoint")
        self.assertEqual(self.jug.components[0]["path"], self.CHECKPOINT_REL)

    def test_pinned_source_and_sha256(self):
        component = self.jug.components[0]
        self.assertIn(f"/resolve/{self.PINNED_REVISION}/", component["url"])
        self.assertEqual(component["sha256"], self.PINNED_SHA256)
        self.assertEqual(int(component["size_bytes"]), 7105348672)
        self.assertIn("OpenRAIL", self.jug.license_name)

    def test_sdxl_workflow_uses_checkpoint_loader(self):
        manager = WorkflowManager(ROOT / "workflows" / "image")
        self.assertEqual(self.jug.workflow_for("text_to_image"), self.WORKFLOW_REL)
        status = manager.inspect(self.WORKFLOW_REL)
        self.assertTrue(status["exists"])
        self.assertTrue(status["valid"], status["errors"])
        self.assertEqual(status["format"], "api")
        for required in ("CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage", "KSampler", "VAEDecode", "SaveImage"):
            self.assertIn(required, status["class_types"])
        # The checkpoint resolves through the component system, not a hardcoded path.
        self.assertIn("component_checkpoint", status["unresolved_tokens"])

    def test_workflow_negative_prompt_is_quality_not_censorship(self):
        manager = WorkflowManager(ROOT / "workflows" / "image")
        workflow = manager.load(self.WORKFLOW_REL)
        texts = [n["inputs"].get("text", "") for n in workflow.values()
                 if n.get("class_type") == "CLIPTextEncode" and isinstance(n.get("inputs"), dict)]
        negative = next(t for t in texts if "low quality" in str(t).lower())
        for banned in ("nsfw", "nude", "nudity", "genitals", "explicit"):
            self.assertNotIn(banned, negative.lower())

    def test_text_to_image_auto_routes_juggernaut(self):
        decision = self.router.choose(ImageRequest(prompt="a castle on a hill"))
        self.assertEqual(decision.operation, "text_to_image")
        self.assertEqual(decision.model_id, "juggernaut-x-v10")

    def test_high_quality_text_to_image_routes_juggernaut(self):
        decision = self.router.choose(ImageRequest(prompt="photorealistic portrait", quality="high"))
        self.assertEqual(decision.model_id, "juggernaut-x-v10")

    def test_edit_operations_stay_on_qwen(self):
        for op, req in (
            ("edit_image", ImageRequest(prompt="make it darker", source_image="a.png")),
            ("inpaint", ImageRequest(prompt="fill the mask", mask_path="m.png")),
            ("outpaint", ImageRequest(prompt="extend this image")),
            ("remove_background", ImageRequest(prompt="remove background", source_image="a.png")),
        ):
            with self.subTest(op=op):
                decision = self.router.choose(req)
                self.assertEqual(decision.operation, op)
                self.assertEqual(decision.model_id, "qwen-image-2.1")

    def test_fast_draft_routes_flux(self):
        for quality in ("preview", "fast", "draft"):
            with self.subTest(quality=quality):
                decision = self.router.choose(ImageRequest(prompt="quick concept", quality=quality))
                self.assertEqual(decision.model_id, "flux2-klein-4b")

    def test_manual_model_overrides(self):
        for model_id in ("qwen-image-2.1", "flux2-klein-4b", "juggernaut-x-v10"):
            with self.subTest(model_id=model_id):
                decision = self.router.choose(ImageRequest(prompt="test", model_override=model_id))
                self.assertEqual(decision.model_id, model_id)

    def _manager(self, root: Path) -> ImageManager:
        config = SimpleNamespace(
            image_models_dir="models/image", image_data_dir="data/image",
            image_workflows_dir="workflows/image", comfyui_endpoint="http://127.0.0.1:8188",
            comfyui_auto_start=False, image_resource_mode="balanced",
        )
        return ImageManager(base_dir=root, models=list(self.models.values()), config=config, workspace=root / "workspace")

    def test_missing_checkpoint_reports_friendly_missing(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(Path(td))
            status = manager.library.verify_model(self.jug)
            self.assertFalse(status["installed"])
            checkpoint = next(c for c in status["components"] if c["key"] == "checkpoint")
            self.assertFalse(checkpoint["ok"])

    def test_corrupt_checkpoint_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / self.CHECKPOINT_REL
            target.parent.mkdir(parents=True)
            target.write_bytes(b"corrupt")
            manager = self._manager(root)
            status = manager.library.verify_model(self.jug, deep_hash=True)
            checkpoint = next(c for c in status["components"] if c["key"] == "checkpoint")
            self.assertFalse(checkpoint["size_ok"])
            self.assertFalse(checkpoint["hash_ok"])
            self.assertEqual(checkpoint["sha256"], __import__("hashlib").sha256(b"corrupt").hexdigest())

    def test_policy_approved_adult_prompt_reaches_workflow_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = self._manager(root)
            manager.adult_content_allowed = lambda: True
            prompt = "tasteful nude portrait of a confident 30-year-old adult woman, studio lighting"
            allowed, _ = manager.policy.check(prompt)
            self.assertTrue(allowed)
            request = ImageRequest(prompt=prompt)
            variables = manager._workflow_variables(request, self.jug)
            self.assertEqual(variables["prompt"], prompt)
            rendered = WorkflowManager.render(
                WorkflowManager(ROOT / "workflows" / "image").load(self.WORKFLOW_REL), variables)
            positive = next(n for n in rendered.values() if n["class_type"] == "CLIPTextEncode"
                            and n["inputs"]["text"] == prompt)
            self.assertIn("nude", positive["inputs"]["text"])

    def test_existing_config_gains_juggernaut_without_losing_models(self):
        from localcodeagent.config import load_config
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(json.dumps({
                "image_models": [
                    {"id": "qwen-image-2.1", "family": "qwen-image-2.1", "priority": 80},
                    {"id": "flux2-klein-4b", "family": "flux.2-klein", "priority": 70},
                ],
            }), encoding="utf-8")
            config = load_config(cfg_path)
            ids = [m.id for m in config.image_models]
            self.assertIn("juggernaut-x-v10", ids)
            self.assertIn("qwen-image-2.1", ids)
            self.assertIn("flux2-klein-4b", ids)
            # User's own entry wins over the merged default.
            qwen = next(m for m in config.image_models if m.id == "qwen-image-2.1")
            self.assertEqual(qwen.components, [])

    def test_image_studio_renders_friendly_labels(self):
        js = (ROOT / "web" / "image.js").read_text(encoding="utf-8")
        self.assertIn("display_name", js)
        self.assertIn("tagline", js)


class BackendStartingFlagTests(unittest.TestCase):
    def _manager(self, root: Path) -> ImageManager:
        profile = ImageModelProfile(
            id="qwen", family="qwen-image", capabilities=["text_to_image"],
            workflows={"text_to_image": "qwen/generate.json"},
        )
        config = SimpleNamespace(
            image_models_dir="models/image", image_data_dir="data/image",
            image_workflows_dir="workflows/image",
            comfyui_endpoint="http://127.0.0.1:8188", comfyui_auto_start=False,
            image_resource_mode="balanced", image_auto_run_jobs=False,
        )
        _stub_comfy(root)
        return ImageManager(base_dir=root, models=[profile], config=config,
                            workspace=root / "workspace")

    def test_backend_starting_set_when_comfyui_offline(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(Path(td))
            manager.backend = SimpleNamespace(health=lambda: (False, "down"))
            job = manager.create_job(ImageRequest(prompt="a cat"))
            self.assertTrue(job.backend_starting)
            self.assertTrue(manager.get_job(job.id).as_dict()["backend_starting"])

    def test_backend_starting_clear_when_comfyui_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(Path(td))
            manager.backend = SimpleNamespace(health=lambda: (True, "ok"))
            job = manager.create_job(ImageRequest(prompt="a cat"))
            self.assertFalse(job.backend_starting)

    def test_health_probe_cached_across_batch_creation(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(Path(td))
            calls = []
            def health():
                calls.append(1)
                return (False, "down")
            manager.backend = SimpleNamespace(health=health)
            manager.create_job(ImageRequest(prompt="a cat"))
            manager.create_job(ImageRequest(prompt="a dog"))
            manager.create_job(ImageRequest(prompt="a sunset"))
            self.assertEqual(len(calls), 1)

    def test_health_probe_exception_treated_as_offline(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(Path(td))
            def boom():
                raise RuntimeError("connection refused")
            manager.backend = SimpleNamespace(health=boom)
            job = manager.create_job(ImageRequest(prompt="a cat"))
            self.assertTrue(job.backend_starting)


class MultiPromptToolTests(unittest.TestCase):
    def _manager(self, root: Path) -> ImageManager:
        profile = ImageModelProfile(
            id="qwen", family="qwen-image", capabilities=["text_to_image"],
            workflows={"text_to_image": "qwen/generate.json"},
        )
        config = SimpleNamespace(
            image_models_dir="models/image", image_data_dir="data/image",
            image_workflows_dir="workflows/image",
            comfyui_endpoint="http://127.0.0.1:8188", comfyui_auto_start=False,
            image_resource_mode="balanced", image_auto_run_jobs=False,
        )
        return ImageManager(base_dir=root, models=[profile], config=config,
                            workspace=root / "workspace")

    def test_prompts_array_creates_one_job_per_prompt(self):
        from localcodeagent.tools.base import ToolRegistry
        from localcodeagent.tools.image import register_image_tools
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(Path(td))
            manager.backend = SimpleNamespace(health=lambda: (True, "ok"))
            registry = ToolRegistry({"image.generate": "allow"})
            register_image_tools(registry, manager)
            result = json.loads(registry.execute("generate_image", {
                "prompt": "a cat", "prompts": ["a cat", "a dog", "a sunset"],
            }))
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["jobs"]), 3)
            prompts = [j["request"]["prompt"] for j in result["jobs"]]
            self.assertEqual(prompts, ["a cat", "a dog", "a sunset"])

    def test_single_prompts_entry_falls_back_to_one_job(self):
        from localcodeagent.tools.base import ToolRegistry
        from localcodeagent.tools.image import register_image_tools
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(Path(td))
            manager.backend = SimpleNamespace(health=lambda: (True, "ok"))
            registry = ToolRegistry({"image.generate": "allow"})
            register_image_tools(registry, manager)
            result = json.loads(registry.execute("generate_image", {
                "prompt": "a cat", "prompts": ["a cat"],
            }))
            self.assertTrue(result["ok"])
            self.assertIn("job", result)


class InterruptedJobResumeTests(unittest.TestCase):
    def _manager(self, root: Path, auto_run: bool) -> ImageManager:
        profile = ImageModelProfile(
            id="qwen", family="qwen-image", capabilities=["text_to_image"],
            workflows={"text_to_image": "qwen/generate.json"},
        )
        config = SimpleNamespace(
            image_models_dir="models/image", image_data_dir="data/image",
            image_workflows_dir="workflows/image",
            comfyui_endpoint="http://127.0.0.1:8188", comfyui_auto_start=False,
            image_resource_mode="balanced", image_auto_run_jobs=auto_run,
        )
        return ImageManager(base_dir=root, models=[profile], config=config,
                            workspace=root / "workspace")

    def test_active_job_requeues_and_resumes_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m1 = self._manager(root, auto_run=False)
            m1.backend = SimpleNamespace(health=lambda: (True, "ok"))
            job = m1.create_job(ImageRequest(prompt="a cat"))
            job.state = "generating"
            job.error_message = "stale error"
            m1._save_jobs(job)

            started = []
            with patch.object(ImageManager, "_backend_up", lambda self: False), \
                 patch.object(ImageManager, "_run_job",
                              lambda self, jid: started.append(jid)):
                m2 = self._manager(root, auto_run=True)
                deadline = time.time() + 3
                while not started and time.time() < deadline:
                    time.sleep(0.02)
            self.assertEqual(started, [job.id])
            resumed = m2.get_job(job.id)
            self.assertEqual(resumed.state, "queued")
            self.assertEqual(resumed.resume_count, 1)
            self.assertEqual(resumed.error_message, "")
            self.assertTrue(resumed.backend_starting)

    def test_queued_job_stays_queued_in_manual_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m1 = self._manager(root, auto_run=False)
            m1.backend = SimpleNamespace(health=lambda: (True, "ok"))
            job = m1.create_job(ImageRequest(prompt="a cat"))
            m2 = self._manager(root, auto_run=False)
            reloaded = m2.get_job(job.id)
            self.assertEqual(reloaded.state, "queued")
            self.assertEqual(reloaded.stage, "queued")

    def test_resume_bound_fails_crash_looping_job(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m1 = self._manager(root, auto_run=False)
            m1.backend = SimpleNamespace(health=lambda: (True, "ok"))
            job = m1.create_job(ImageRequest(prompt="a cat"))
            job.state = "generating"
            job.resume_count = 2
            m1._save_jobs(job)
            with patch.object(ImageManager, "_backend_up", lambda self: False):
                m2 = self._manager(root, auto_run=True)
            failed = m2.get_job(job.id)
            self.assertEqual(failed.state, "failed")
            self.assertEqual(failed.error_code, "application_restarted")


class SplitImagePromptTests(unittest.TestCase):
    def test_explicit_count_list_splits(self):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        parts = AgentOrchestrator._split_image_prompts(
            "generate 3 images of a cat, a dog, and a sunset")
        self.assertEqual(parts, ["a cat", "a dog", "a sunset"])

    def test_ambiguous_and_stays_single(self):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        self.assertEqual(
            AgentOrchestrator._split_image_prompts(
                "generate an image of a cat and a dog in a park"), [])

    def test_repeated_image_of_construction_splits(self):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        parts = AgentOrchestrator._split_image_prompts(
            "generate an image of a cat and an image of a dog")
        self.assertEqual(parts, ["a cat", "a dog"])

    def test_jobs_list_extracted_from_tool_events(self):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        events = [{
            "name": "generate_image",
            "result": json.dumps({"ok": True, "jobs": [
                {"id": "a"}, {"id": "b"}]}),
        }]
        self.assertEqual(
            AgentOrchestrator._image_job_ids_from_events(events), ["a", "b"])


class ImageSetupTests(unittest.TestCase):
    """The bundled 'install ComfyUI + all models' flow used by the chat offer
    and the Image workspace setup card."""

    def _config(self, **over):
        base = dict(
            image_models_dir="models/image", image_data_dir="data/image",
            image_workflows_dir="workflows/image",
            # Dead endpoint — a real ComfyUI may be listening on 8188 in dev.
            comfyui_endpoint="http://127.0.0.1:9", comfyui_auto_start=False,
            comfyui_dir="", comfyui_python="", comfyui_logs_dir=".agent/runtime",
            comfyui_extra_args=[], image_resource_mode="balanced",
            image_auto_run_jobs=True, image_max_resumes=1,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def _manager(self, root: Path, models=None, **kw):
        return ImageManager(base_dir=root, models=models or [],
                            config=self._config(), workspace=root / "ws", **kw)

    def _wait_state(self, mgr, timeout=15.0) -> str:
        deadline = time.time() + timeout
        state = ""
        while time.time() < deadline:
            state = mgr.setup_state()["state"]
            if state in {"done", "failed"}:
                return state
            time.sleep(0.1)
        return state

    def _installed_model(self, root: Path) -> ImageModelProfile:
        target = root / "models" / "image" / "sdxl" / "m.safetensors"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"weights")
        return ImageModelProfile(
            id="sdxl-m", family="stable-diffusion-xl", display_name="Test SDXL",
            components=[{"key": "checkpoint", "path": "models/image/sdxl/m.safetensors", "required": True}],
            capabilities=["text_to_image"])

    def test_probe_reports_installed_flag(self):
        class _B:
            endpoint = "http://127.0.0.1:8188"
            def health(self): return False, "connection refused"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = ComfyUIRuntime(base_dir=root, backend=_B(), config=self._config())
            self.assertFalse(rt.probe()["installed"])
            comfy = root / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
            comfy.mkdir(parents=True)
            (comfy / "main.py").write_text("# stub", encoding="utf-8")
            self.assertTrue(rt.probe()["installed"])

    def test_create_job_raises_and_offers_when_backend_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fired = []
            mgr = self._manager(root, models=[self._installed_model(root)],
                                comfy_installer=lambda: {"ok": True, "job_id": "j1"})
            mgr.on_missing_backend = lambda: fired.append(True)
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                mgr.create_job(ImageRequest(prompt="a cat", operation="text_to_image"))
            self.assertEqual(fired, [True])

    def test_error_classifier_marks_missing_backend(self):
        row = describe_image_error(RuntimeError("ComfyUI is not installed — install it"))
        self.assertEqual(row["code"], "backend_not_installed")
        self.assertIn("install", row["message"].lower())
        row = describe_image_error(RuntimeError("ComfyUI was not found. Set comfyui_dir"))
        self.assertEqual(row["code"], "backend_not_installed")

    def test_setup_installs_backend_then_models(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = self._installed_model(root)

            def install_comfy():
                comfy = root / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
                comfy.mkdir(parents=True)
                (comfy / "main.py").write_text("# stub", encoding="utf-8")
                return {"ok": True, "job_id": "job-1"}

            mgr = self._manager(root, models=[model], comfy_installer=install_comfy,
                                job_lookup=lambda jid: {"state": "completed", "progress": 1.0})
            state = mgr.start_setup()
            self.assertIn(state["state"], {"installing_backend", "installing_models"})
            self.assertEqual(self._wait_state(mgr), "done")
            row = mgr.setup_state()
            self.assertTrue(row["backend_installed"])
            self.assertTrue(all(m["installed"] for m in row["models"]))
            self.assertAlmostEqual(row["progress"], 1.0)

    def test_setup_resumes_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = self._installed_model(root)
            # Simulate a setup that was mid-backend-install when the app died.
            setup_path = root / "data" / "image" / "setup.json"
            setup_path.parent.mkdir(parents=True)
            setup_path.write_text(json.dumps({
                "state": "installing_backend", "model_ids": [model.id],
                "started_at": time.time(), "updated_at": time.time()}), encoding="utf-8")

            def install_comfy():
                comfy = root / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
                comfy.mkdir(parents=True)
                (comfy / "main.py").write_text("# stub", encoding="utf-8")
                return {"ok": True, "job_id": "job-1"}

            mgr = self._manager(root, models=[model], comfy_installer=install_comfy,
                                job_lookup=lambda jid: {"state": "completed", "progress": 1.0})
            mgr.resume_setup()
            self.assertEqual(self._wait_state(mgr), "done")

    def test_setup_fails_cleanly_without_installer(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = self._manager(Path(td), models=[self._installed_model(Path(td))])
            mgr.start_setup()
            self.assertEqual(self._wait_state(mgr), "failed")
            self.assertIn("no installer", mgr.setup_state()["error"])

    def test_model_install_dedupes_live_job(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = ImageAssetLibrary(base_dir=root, models_dir=root / "models/image",
                                    workflows_dir=root / "workflows/image")
            profile = ImageModelProfile(
                id="m", family="stable-diffusion",
                components=[{"key": "ckpt", "path": "models/image/m.safetensors",
                             "url": "https://example.invalid/m.safetensors", "required": True}])
            gate = threading.Event()
            try:
                def slow_download(spec, *, repair, progress):
                    gate.wait(5)
                    return {"path": spec["path"], "status": "downloaded", "size_bytes": 0}
                lib._download_component = slow_download
                first = lib.start_install(profile)
                second = lib.start_install(profile)
                self.assertEqual(first["id"], second["id"])
            finally:
                gate.set()


if __name__ == "__main__":
    unittest.main()


class SamplingControlsTests(unittest.TestCase):
    def test_cfg_steps_seed_extracted(self):
        from localcodeagent.image.router import parse_sampling_controls
        clean, c = parse_sampling_controls(
            "a girl in a suit, cfg 4, 30 steps, seed 42")
        self.assertEqual(clean, "a girl in a suit")
        self.assertEqual(c["guidance"], 4.0)
        self.assertEqual(c["steps"], 30)
        self.assertEqual(c["seed"], 42)

    def test_sampler_and_scheduler_named(self):
        from localcodeagent.image.router import parse_sampling_controls
        clean, c = parse_sampling_controls(
            "a cat, euler a sampler, karras scheduler")
        self.assertEqual(c["sampler_name"], "euler_ancestral")
        self.assertEqual(c["scheduler"], "karras")
        clean2, c2 = parse_sampling_controls("a cat, sampler dpmpp 2m sde")
        self.assertEqual(c2["sampler_name"], "dpmpp_2m_sde")

    def test_denoise_absolute_and_percent(self):
        from localcodeagent.image.router import parse_sampling_controls
        self.assertEqual(
            parse_sampling_controls("make transparent, denoise 0.6")[1]["denoise_strength"], 0.6)
        self.assertEqual(
            parse_sampling_controls("make transparent, denoise 60%")[1]["denoise_strength"], 0.6)

    def test_dangling_connectors_trimmed(self):
        from localcodeagent.image.router import parse_sampling_controls
        clean, _ = parse_sampling_controls("make this transparent with denoise 0.6")
        self.assertEqual(clean, "make this transparent")

    def test_plain_prompt_untouched(self):
        from localcodeagent.image.router import parse_sampling_controls
        clean, c = parse_sampling_controls("a woman standing in a field")
        self.assertEqual(clean, "a woman standing in a field")
        self.assertEqual(c, {})


class OperationRoutingTests(unittest.TestCase):
    def test_remove_background_phrasings(self):
        from localcodeagent.image.router import ImageRouter
        from localcodeagent.image.types import ImageRequest
        for text in ("can you remove background from this image?",
                     "make this image transparent", "cut her out",
                     "erase the background", "isolate the subject"):
            op, _ = ImageRouter.infer_operation(
                ImageRequest(prompt=text, source_image="x.png"))
            self.assertEqual(op, "remove_background", text)

    def test_transparent_requires_source(self):
        from localcodeagent.image.router import ImageRouter
        from localcodeagent.image.types import ImageRequest
        op, _ = ImageRouter.infer_operation(
            ImageRequest(prompt="a transparent crystal", source_image=""))
        self.assertEqual(op, "text_to_image")

    def test_qwen_guidance_default(self):
        from localcodeagent.image.manager import ImageManager
        from localcodeagent.image.types import ImageModelProfile
        p = ImageModelProfile(id="q", family="qwen-image-2.1", backend="comfyui",
                              model_path="", capabilities=[])
        self.assertEqual(ImageManager._default_guidance(p), 4.0)
        p2 = ImageModelProfile(id="j", family="stable-diffusion-xl", backend="c",
                               model_path="", capabilities=[])
        self.assertEqual(ImageManager._default_guidance(p2), 6.5)
        p3 = ImageModelProfile(id="f", family="flux.2-klein", backend="c",
                               model_path="", capabilities=[])
        self.assertEqual(ImageManager._default_guidance(p3), 1.0)


class VocalizationCapsTests(unittest.TestCase):
    def test_caps_vocalizations_hum(self):
        # The filter now canonicalizes; the VocalizationEngine renders the
        # final spoken form downstream (tests/test_vocalizations.py).
        from localcodeagent.voice.speech_filter import SpeechTextFilter
        f = SpeechTextFilter()
        self.assertEqual(f.filter("MMM"), "mmm")
        self.assertEqual(f.filter("MMMM yes"), "mmmm yes")
        self.assertEqual(f.filter("MMHMM"), "mm-hmm")

    def test_abbreviations_untouched(self):
        from localcodeagent.voice.speech_filter import SpeechTextFilter
        f = SpeechTextFilter()
        self.assertEqual(f.filter("50MM lens"), "50MM lens")
        self.assertEqual(f.filter("recommend"), "recommend")
        self.assertEqual(f.filter("the MM format"), "the MM format")


class SamplingAdvisorTests(unittest.TestCase):
    def _adv(self):
        import tempfile
        from pathlib import Path
        from localcodeagent.image.sampling import SamplingAdvisor
        return SamplingAdvisor(Path(tempfile.mkdtemp()) / "s.json")

    def _profile(self, family):
        from localcodeagent.image.types import ImageModelProfile
        return ImageModelProfile(id="m", family=family, backend="c",
                                 model_path="", capabilities=[])

    def test_qwen_gets_real_cfg(self):
        from localcodeagent.image.types import ImageRequest
        r = ImageRequest(prompt="a castle", operation="text_to_image")
        self._adv().apply(r, self._profile("qwen-image-2.1"), "text_to_image")
        self.assertEqual(r.guidance, 4.0)
        self.assertEqual(r.sampler_name, "euler")
        self.assertEqual(r.scheduler, "simple")
        self.assertEqual(r.steps, 25)

    def test_sdxl_defaults_and_high_quality_steps(self):
        from localcodeagent.image.types import ImageRequest
        r = ImageRequest(prompt="a castle", operation="text_to_image",
                         quality="high")
        self._adv().apply(r, self._profile("stable-diffusion-xl"), "text_to_image")
        self.assertEqual(r.guidance, 6.5)
        self.assertEqual(r.sampler_name, "dpmpp_2m")
        self.assertEqual(r.steps, 35)

    def test_distilled_keeps_cfg_one(self):
        from localcodeagent.image.types import ImageRequest
        r = ImageRequest(prompt="a castle", operation="text_to_image")
        self._adv().apply(r, self._profile("flux.2-klein"), "text_to_image")
        self.assertEqual(r.guidance, 1.0)
        self.assertEqual(r.steps, 8)

    def test_subtle_edit_lowers_denoise(self):
        from localcodeagent.image.types import ImageRequest
        r = ImageRequest(prompt="a subtle color tweak",
                         operation="edit_image", source_image="x.png")
        self._adv().apply(r, self._profile("qwen-image-2.1"), "edit_image")
        self.assertEqual(r.denoise_strength, 0.6)

    def test_detail_cue_raises_steps(self):
        from localcodeagent.image.types import ImageRequest
        r = ImageRequest(prompt="a portrait, sharper and more detailed",
                         operation="text_to_image")
        self._adv().apply(r, self._profile("stable-diffusion-xl"), "text_to_image")
        self.assertEqual(r.steps, 35)

    def test_explicit_controls_never_overwritten(self):
        from localcodeagent.image.types import ImageRequest
        r = ImageRequest(prompt="a castle", operation="text_to_image",
                         steps=12, guidance=2.5, sampler_name="lcm",
                         scheduler="beta", denoise_strength=0.4)
        self._adv().apply(r, self._profile("qwen-image-2.1"), "text_to_image")
        self.assertEqual((r.steps, r.guidance, r.sampler_name,
                          r.scheduler, r.denoise_strength),
                         (12, 2.5, "lcm", "beta", 0.4))

    def test_learning_biases_toward_wins(self):
        from localcodeagent.image.types import ImageRequest
        adv = self._adv()
        prof = self._profile("qwen-image-2.1")
        win_params = {"steps": 40, "guidance": 5.0,
                      "sampler_name": "dpmpp_2m", "scheduler": "karras"}
        adv.record_outcome(prof, "text_to_image", win_params, "up")
        r = ImageRequest(prompt="x", operation="text_to_image")
        adv.apply(r, prof, "text_to_image")
        self.assertEqual(r.steps, 40)
        self.assertEqual(r.guidance, 5.0)
        self.assertEqual(r.sampler_name, "dpmpp_2m")
        self.assertEqual(r.scheduler, "karras")

    def test_learning_persists_across_instances(self):
        import tempfile, json
        from pathlib import Path
        from localcodeagent.image.sampling import SamplingAdvisor
        from localcodeagent.image.types import ImageRequest
        p = Path(tempfile.mkdtemp()) / "s.json"
        adv = SamplingAdvisor(p)
        prof = self._profile("qwen-image-2.1")
        adv.record_outcome(prof, "edit_image", {"steps": 33}, "up")
        adv2 = SamplingAdvisor(p)
        r = ImageRequest(prompt="x", operation="edit_image")
        adv2.apply(r, prof, "edit_image")
        self.assertEqual(r.steps, 33)

    def test_repeated_losses_trigger_exploration(self):
        from localcodeagent.image.types import ImageRequest
        adv = self._adv()
        prof = self._profile("qwen-image-2.1")
        bad = {"steps": 25, "guidance": 4.0}
        adv.record_outcome(prof, "text_to_image", bad, "down")
        adv.record_outcome(prof, "text_to_image", bad, "down")
        r = ImageRequest(prompt="x", operation="text_to_image")
        adv.apply(r, prof, "text_to_image")
        self.assertEqual(r.steps, 35)   # 25 + 10 exploration
        self.assertEqual(r.guidance, 3.5)  # 4.0 - 0.5
