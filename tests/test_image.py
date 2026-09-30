from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.image.catalog import discover_image_models
from localcodeagent.image.policy import ConsentStore, ImageSafetyPolicy
from localcodeagent.image.library import ImageAssetLibrary
from localcodeagent.image.router import ImageRouter
from localcodeagent.image.types import ImageModelProfile, ImageRequest
from localcodeagent.image.workflow import WorkflowManager


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
