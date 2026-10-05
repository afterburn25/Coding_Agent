"""Photoreal fleet — request classification, scoring, routing, overrides."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from localcodeagent.image.fleet import (
    FLEET, FLEET_BY_ID, classify_request_traits, fleet_for_model_name,
    rank_fleet, score_fleet_model)
from localcodeagent.image.router import ImageRouter
from localcodeagent.image.types import ImageModelProfile, ImageRequest


def _fleet_profile(fid: str, key: str = "k") -> ImageModelProfile:
    """Synthesize the profile _refresh_invokeai_models builds for a
    fleet-matched InvokeAI row."""
    spec = FLEET_BY_ID[fid]
    p = ImageModelProfile(
        id=f"invokeai:{key}-{fid}", family="stable-diffusion-xl",
        backend="invokeai", model_path="main/model.safetensors",
        display_name=spec["display_name"],
        capabilities=["text_to_image", "image_edit", "inpaint"],
        capability_class="photoreal",
        restriction_status=spec["restriction_status"])
    p.metadata["fleet_id"] = spec["id"]
    p.metadata["fleet_role"] = spec["role"]
    p.metadata["fleet_display"] = spec["display_name"]
    p.metadata["sampling"] = dict(spec["sampling"], model_scope=spec["id"])
    return p


def _pool() -> list[ImageModelProfile]:
    return [_fleet_profile("juggernaut-xl-v9", "k1"),
            _fleet_profile("realvisxl-v5", "k2"),
            _fleet_profile("cyberrealistic-xl-v9", "k3")]


class ClassificationTests(unittest.TestCase):
    def test_portrait_traits(self):
        info = classify_request_traits(
            "professional close-up portrait, beauty photography")
        self.assertTrue(info["photoreal"])
        self.assertGreaterEqual(info["traits"].get("portrait", 0), 1)
        self.assertGreaterEqual(info["traits"].get("face", 0), 1)

    def test_scene_traits(self):
        info = classify_request_traits(
            "realistic woman standing in Times Square at night")
        self.assertTrue(info["photoreal"])
        self.assertTrue(info["traits"].get("scene"))
        self.assertTrue(info["traits"].get("people"))

    def test_adult_glamour(self):
        info = classify_request_traits(
            "adult glamour boudoir photo, clearly adult woman")
        self.assertTrue(info["adult"])
        self.assertTrue(info["traits"].get("glamour"))

    def test_illustrative_not_photoreal(self):
        info = classify_request_traits("anime watercolor painting")
        self.assertTrue(info["illustrative"])
        self.assertFalse(info["photoreal"])

    def test_empty_prompt_no_traits(self):
        info = classify_request_traits("")
        self.assertEqual(info["traits"], {})


class ScoringTests(unittest.TestCase):
    def test_portrait_prefers_cyberrealistic(self):
        info = classify_request_traits(
            "professional close-up beauty portrait headshot")
        ranked = rank_fleet(info["traits"], set(FLEET_BY_ID))
        self.assertEqual(ranked[0][1]["id"], "cyberrealistic-xl-v9")

    def test_general_scene_prefers_juggernaut(self):
        info = classify_request_traits(
            "realistic person in a futuristic control room, wide shot")
        ranked = rank_fleet(info["traits"], set(FLEET_BY_ID))
        self.assertEqual(ranked[0][1]["id"], "juggernaut-xl-v9")

    def test_adult_glamour_prefers_realvis(self):
        info = classify_request_traits(
            "clearly adult glamour boudoir photo with body focus")
        ranked = rank_fleet(info["traits"], set(FLEET_BY_ID))
        self.assertEqual(ranked[0][1]["id"], "realvisxl-v5")

    def test_installed_bias_beats_keyword_score(self):
        # CyberRealistic uninstalled → its keyword win must lose to the
        # installed Juggernaut rather than route to a missing model.
        info = classify_request_traits("close-up portrait")
        ranked = rank_fleet(info["traits"], {"juggernaut-xl-v9"})
        self.assertEqual(ranked[0][1]["id"], "juggernaut-xl-v9")

    def test_name_matching(self):
        self.assertEqual(
            fleet_for_model_name("Juggernaut-XL_v9_RunDiffusionPhoto_v2")
            ["id"], "juggernaut-xl-v9")
        self.assertEqual(
            fleet_for_model_name("RealVisXL_V5.0_fp16.safetensors")["id"],
            "realvisxl-v5")
        self.assertIsNone(fleet_for_model_name("dreamshaper-8"))


class RoutingTests(unittest.TestCase):
    def _router(self, fit_fail: str = "") -> ImageRouter:
        def fit(profile):
            if fit_fail and profile.metadata.get("fleet_id") == fit_fail:
                return False, 0, "vram reserved"
            return True, 0, ""
        return ImageRouter(models=_pool(), resource_fit=fit)

    def test_portrait_routes_cyberrealistic(self):
        d = self._router().choose(ImageRequest(
            prompt="professional close-up beauty portrait headshot"),
            backend="invokeai")
        self.assertIn("cyberrealistic-xl-v9", d.model_id)
        self.assertTrue(any("fleet" in r for r in d.reasons))

    def test_scene_routes_juggernaut(self):
        d = self._router().choose(ImageRequest(
            prompt="realistic woman standing in Times Square at night, "
                   "wide shot"), backend="invokeai")
        self.assertIn("juggernaut-xl-v9", d.model_id)

    def test_glamour_routes_realvis(self):
        d = self._router().choose(ImageRequest(
            prompt="clearly adult glamour boudoir photograph"),
            backend="invokeai")
        self.assertIn("realvisxl-v5", d.model_id)

    def test_fallback_when_winner_unavailable(self):
        d = self._router(fit_fail="cyberrealistic-xl-v9").choose(
            ImageRequest(prompt="close-up portrait headshot"),
            backend="invokeai")
        self.assertNotIn("cyberrealistic-xl-v9", d.model_id)
        self.assertTrue(any("skipped" in r for r in d.reasons))

    def test_no_traits_falls_to_generic(self):
        # No photoreal signal → fleet scoring never invents a pick.
        d = self._router().choose(ImageRequest(prompt="a cat"),
                                  backend="invokeai")
        self.assertTrue(d.model_id.startswith("invokeai:"))

    def test_fleet_id_override(self):
        d = self._router().choose(ImageRequest(
            prompt="a cat", model_override="realvisxl-v5"),
            backend="invokeai")
        self.assertIn("realvisxl-v5", d.model_id)
        self.assertTrue(any("override" in r for r in d.reasons))

    def test_wrong_backend_override_errors(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._router().choose(ImageRequest(
                prompt="x", model_override="juggernaut-xl-v9"),
                backend="comfyui")
        self.assertIn("invokeai", str(ctx.exception).lower())


class SamplingProfileTests(unittest.TestCase):
    def test_fleet_defaults_override_family(self):
        from localcodeagent.image.sampling import _family_defaults
        p = _fleet_profile("juggernaut-xl-v9")
        d = _family_defaults(p)
        self.assertEqual(d["steps_balanced"], 30)
        self.assertEqual(d["guidance"], 5.0)
        self.assertEqual(d["sampler_name"], "dpmpp_2m_sde")
        self.assertEqual(d["scheduler"], "karras")

    def test_learning_scoped_per_model(self):
        from localcodeagent.image.sampling import SamplingAdvisor
        jug = _fleet_profile("juggernaut-xl-v9", "k1")
        cyb = _fleet_profile("cyberrealistic-xl-v9", "k3")
        self.assertNotEqual(
            SamplingAdvisor._key(jug, "text_to_image", "invokeai"),
            SamplingAdvisor._key(cyb, "text_to_image", "invokeai"))
        self.assertIn("juggernaut",
                      SamplingAdvisor._key(jug, "text_to_image", "invokeai"))

    def test_advisor_applies_model_defaults(self):
        from localcodeagent.image.sampling import SamplingAdvisor
        with tempfile.TemporaryDirectory() as td:
            adv = SamplingAdvisor(Path(td) / "s.json")
            req = ImageRequest(prompt="portrait")
            adv.apply(req, _fleet_profile("juggernaut-xl-v9"),
                      "text_to_image", backend="invokeai")
            self.assertEqual(req.guidance, 5.0)
            self.assertEqual(req.steps, 30)
            self.assertEqual(req.sampler_name, "dpmpp_2m_sde")


if __name__ == "__main__":
    unittest.main()
