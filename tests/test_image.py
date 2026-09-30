from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.image.catalog import discover_image_models
from localcodeagent.image.policy import ConsentStore, ImageSafetyPolicy
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

    def test_catalog_discovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "qwen").mkdir()
            (root / "qwen" / "Qwen-Image-2.1-Q4.gguf").write_bytes(b"1234")
            rows = discover_image_models(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["family"], "qwen-image")


if __name__ == "__main__":
    unittest.main()
