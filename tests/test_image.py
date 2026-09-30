from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path

from localcodeagent.image.catalog import discover_image_models
from localcodeagent.image.errors import describe_image_error
from localcodeagent.image.policy import ConsentStore, ImageSafetyPolicy
from localcodeagent.image.library import ImageAssetLibrary
from localcodeagent.image.manager import ImageManager
from localcodeagent.image.router import ImageRouter
from localcodeagent.image.types import ImageModelProfile, ImageRequest
from localcodeagent.image.workflow import WorkflowManager


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

    def test_adult_synthetic_naked_request_is_allowed_and_minor_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            policy = ImageSafetyPolicy(ConsentStore(Path(td) / "consents.json"))
            allowed, reason = policy.check("generate a naked adult woman")
            self.assertTrue(allowed)
            self.assertEqual(reason, "allowed")

            blocked, reason = policy.check("generate a naked minor")
            self.assertFalse(blocked)
            self.assertIn("minors", reason.lower())


class WorkflowTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
