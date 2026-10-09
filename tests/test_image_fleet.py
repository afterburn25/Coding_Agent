"""Fleet (InvokeAI) model integration — catalog visibility, the configured
adult-content default, on-disk dedup, and verification."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from localcodeagent.image.fleet import (
    FLEET, FLEET_BY_ID, find_fleet_checkpoint, fleet_tags)
from localcodeagent.image.manager import ImageManager
from localcodeagent.image.router import ImageRouter
from localcodeagent.image.types import ImageModelProfile, ImageRequest


def _fleet_profile(fid: str, key: str = "k") -> ImageModelProfile:
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


def _manager(root: Path, *, adult_default: str = "realvisxl-v5") -> ImageManager:
    config = SimpleNamespace(
        image_models_dir="models/image", image_data_dir="data/image",
        image_workflows_dir="workflows/image",
        comfyui_endpoint="http://127.0.0.1:8188",
        invokeai_endpoint="http://127.0.0.1:9",
        comfyui_auto_start=False, image_resource_mode="balanced",
        image_adult_default_model=adult_default,
    )
    return ImageManager(base_dir=root, models=[], config=config,
                        workspace=root / "workspace")


def _registry_row(spec_id: str) -> dict:
    spec = FLEET_BY_ID[spec_id]
    return {"key": f"k-{spec_id}", "name": spec["source_file"],
            "type": "main", "base": "sdxl", "format": "checkpoint",
            "hash": "abc123", "path": f"main/{spec['source_file']}",
            "source": spec["invokeai_source"]}


class CatalogVisibilityTests(unittest.TestCase):
    def test_realvis_listed_before_install(self):
        """Requirement: the catalog must surface RealVisXL even when no
        checkpoint exists — the manager renders it as installable."""
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m._invokeai_models = lambda **kw: []
            m._backend_up = lambda name="comfyui": False
            rows = {r["id"]: r for r in m.fleet_status()}
            rv = rows["realvisxl-v5"]
            self.assertEqual(rv["state"], "missing")
            self.assertFalse(rv["installed"])
            self.assertEqual(rv["display_name"], "RealVisXL V5.0")
            self.assertTrue(rv["adult_capable"])
            self.assertTrue(rv["adult_default"])
            self.assertEqual(rv["size_bytes"], 6938065488)
            for tag in ("SDXL", "fp16", "Photoreal", "Adult-capable",
                        "High quality"):
                self.assertIn(tag, rv["tags"])

    def test_realvis_installed_when_registered(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m._invokeai_models = lambda **kw: [_registry_row("realvisxl-v5")]
            m._backend_up = lambda name="comfyui": False
            rv = next(r for r in m.fleet_status() if r["id"] == "realvisxl-v5")
            self.assertEqual(rv["state"], "installed")
            self.assertTrue(rv["installed"])
            self.assertEqual(rv["backend_model"]["key"], "k-realvisxl-v5")

    def test_remote_install_job_surfaces_as_downloading(self):
        """Restart/resume: a provisioning- or InvokeAI-side install keeps
        reporting Downloading even though this manager never started it."""
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m._invokeai_models = lambda **kw: []
            m._backend_up = lambda name="comfyui": True
            spec = FLEET_BY_ID["realvisxl-v5"]
            m.invokeai_backend = SimpleNamespace(
                model_install_jobs=lambda: [{
                    "id": 7, "status": "downloading",
                    "source": spec["invokeai_source"],
                    "bytes": spec["size_bytes"] // 2,
                    "bytes_total": spec["size_bytes"]}])
            rv = next(r for r in m.fleet_status() if r["id"] == "realvisxl-v5")
            self.assertEqual(rv["state"], "downloading")
            self.assertEqual(rv["progress"]["bytes_done"],
                             spec["size_bytes"] // 2)

    def test_adult_default_flag_moves_with_config(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td), adult_default="cyberrealistic-xl-v9")
            m._invokeai_models = lambda **kw: []
            m._backend_up = lambda name="comfyui": False
            rows = {r["id"]: r for r in m.fleet_status()}
            self.assertFalse(rows["realvisxl-v5"]["adult_default"])
            self.assertTrue(rows["cyberrealistic-xl-v9"]["adult_default"])

    def test_describe_fleet_defaults_honest_state(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m._invokeai_models = lambda **kw: []
            m._backend_up = lambda name="comfyui": False
            line = m.describe_fleet_defaults()
            self.assertIn("RealVisXL V5.0", line)
            self.assertIn("not installed", line)


class ConfigMergeTests(unittest.TestCase):
    def test_old_config_gains_adult_default(self):
        """Upgrade merge: a config written before the setting exists
        gains the realvisxl-v5 default without erasing user settings."""
        from localcodeagent.config import load_config
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({"image_backend": "comfyui",
                                     "image_resource_mode": "light"}),
                         encoding="utf-8")
            cfg = load_config(p)
            self.assertEqual(cfg.image_adult_default_model, "realvisxl-v5")
            self.assertEqual(cfg.image_backend, "comfyui")
            self.assertEqual(cfg.image_resource_mode, "light")

    def test_explicit_choice_preserved(self):
        from localcodeagent.config import load_config
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            for value in ("auto", "cyberrealistic-xl-v9"):
                p.write_text(json.dumps(
                    {"image_adult_default_model": value}),
                    encoding="utf-8")
                self.assertEqual(
                    load_config(p).image_adult_default_model, value)


class AdultDefaultRoutingTests(unittest.TestCase):
    def _pool(self, *fids: str) -> list[ImageModelProfile]:
        keys = {"juggernaut-xl-v9": "k1", "realvisxl-v5": "k2",
                "cyberrealistic-xl-v9": "k3"}
        return [_fleet_profile(f, keys[f]) for f in fids]

    def _router(self, *fids: str, adult_default="realvisxl-v5",
                fit_fail: str = "") -> ImageRouter:
        def fit(profile):
            if fit_fail and profile.metadata.get("fleet_id") == fit_fail:
                return False, 0, "vram reserved"
            return True, 0, ""
        return ImageRouter(models=self._pool(*fids), resource_fit=fit,
                           adult_default=adult_default)

    def test_adult_request_prefers_realvis(self):
        d = self._router("juggernaut-xl-v9", "realvisxl-v5",
                         "cyberrealistic-xl-v9").choose(
            ImageRequest(prompt="tasteful nude portrait of a confident "
                                "30-year-old adult woman"),
            backend="invokeai")
        self.assertIn("realvisxl-v5", d.model_id)
        self.assertTrue(any("adult-content preference" in r
                            for r in d.reasons))

    def test_adult_default_missing_falls_back(self):
        d = self._router("juggernaut-xl-v9",
                         "cyberrealistic-xl-v9").choose(
            ImageRequest(prompt="clearly adult nude boudoir photograph"),
            backend="invokeai")
        self.assertNotIn("realvisxl-v5", d.model_id)
        self.assertTrue(any("not installed" in r for r in d.reasons))

    def test_adult_default_unfit_falls_back(self):
        d = self._router("juggernaut-xl-v9", "realvisxl-v5",
                         fit_fail="realvisxl-v5").choose(
            ImageRequest(prompt="clearly adult nude glamour photograph"),
            backend="invokeai")
        self.assertNotIn("realvisxl-v5", d.model_id)
        self.assertTrue(any("skipped" in r for r in d.reasons))

    def test_manual_override_beats_adult_default(self):
        d = self._router("juggernaut-xl-v9", "realvisxl-v5",
                         "cyberrealistic-xl-v9").choose(
            ImageRequest(prompt="clearly adult nude photograph",
                         model_override="cyberrealistic-xl-v9"),
            backend="invokeai")
        self.assertIn("cyberrealistic-xl-v9", d.model_id)
        self.assertTrue(any("override" in r for r in d.reasons))

    def test_non_adult_routing_unchanged(self):
        """A landscape prompt must never be hijacked by the adult default
        even though RealVis is installed."""
        d = self._router("juggernaut-xl-v9", "realvisxl-v5",
                         "cyberrealistic-xl-v9").choose(
            ImageRequest(prompt="photorealistic mountain landscape at "
                                "dawn, wide vista"), backend="invokeai")
        self.assertNotIn("realvisxl-v5", d.model_id)
        self.assertFalse(any("adult-content preference" in r
                             for r in d.reasons))

    def test_auto_default_uses_trait_scoring(self):
        d = self._router("juggernaut-xl-v9", "realvisxl-v5",
                         adult_default="auto").choose(
            ImageRequest(prompt="clearly adult glamour boudoir "
                                "photograph"), backend="invokeai")
        self.assertIn("realvisxl-v5", d.model_id)
        self.assertFalse(any("adult-content preference" in r
                             for r in d.reasons))

    def test_specialized_op_not_hijacked(self):
        """Operations outside the fleet set (upscale, background removal)
        never see the adult preference — the operation-capable model
        wins even on an adult-classified prompt."""
        upscaler = ImageModelProfile(
            id="invokeai:up-1", family="upscaler", backend="invokeai",
            model_path="up/up.safetensors", display_name="Upscaler",
            capabilities=["upscale"])
        router = ImageRouter(
            models=[*self._pool("juggernaut-xl-v9", "realvisxl-v5"),
                    upscaler],
            adult_default="realvisxl-v5")
        d = router.choose(
            ImageRequest(prompt="upscale this nude photo",
                         operation="upscale", source_image="a.png"),
            backend="invokeai")
        self.assertEqual(d.model_id, "invokeai:up-1")
        self.assertFalse(any("adult-content preference" in r
                             for r in d.reasons))


class DedupTests(unittest.TestCase):
    def test_find_fleet_checkpoint_reuses_existing_file(self):
        spec = FLEET_BY_ID["realvisxl-v5"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "models" / "main" / spec["source_file"]
            target.parent.mkdir(parents=True)
            # Sparse file — stat() reports the full expected size.
            with target.open("wb") as f:
                f.seek(int(spec["size_bytes"]) - 1)
                f.write(b"\0")
            found = find_fleet_checkpoint(spec, [root])
            self.assertEqual(found, target.resolve())

    def test_find_fleet_checkpoint_rejects_short_file(self):
        spec = FLEET_BY_ID["realvisxl-v5"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / spec["source_file"]
            target.write_bytes(b"partial")
            self.assertIsNone(find_fleet_checkpoint(spec, [root]))

    def test_find_fleet_checkpoint_ignores_other_models(self):
        spec = FLEET_BY_ID["realvisxl-v5"]
        with tempfile.TemporaryDirectory() as td:
            other = Path(td) / "SomeOther_v1.safetensors"
            other.write_bytes(b"x" * 100)
            self.assertIsNone(find_fleet_checkpoint(spec, [Path(td)]))


class VerificationTests(unittest.TestCase):
    def test_corrupt_checkpoint_never_counts_as_installed(self):
        """Registration + a wrong-size file = not ok; routing may only
        trust a checkpoint whose size/hash match the spec."""
        spec = FLEET_BY_ID["realvisxl-v5"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m = _manager(root)
            m._invokeai_models = lambda **kw: [_registry_row("realvisxl-v5")]
            target = m._invokeai_models_root() / "main" / spec["source_file"]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"corrupt")
            out = m.verify_fleet_model("realvisxl-v5")
            self.assertTrue(out["registered"])
            self.assertTrue(out["file_found"])
            self.assertFalse(out["size_ok"])
            self.assertFalse(out["ok"])

    def test_verified_checkpoint_is_ok(self):
        spec = FLEET_BY_ID["realvisxl-v5"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m = _manager(root)
            m._invokeai_models = lambda **kw: [_registry_row("realvisxl-v5")]
            target = m._invokeai_models_root() / "main" / spec["source_file"]
            target.parent.mkdir(parents=True)
            with target.open("wb") as f:
                f.seek(int(spec["size_bytes"]) - 1)
                f.write(b"\0")
            out = m.verify_fleet_model("realvisxl-v5")
            self.assertTrue(out["ok"], out)


class ManagerInstallTests(unittest.TestCase):
    def test_start_fleet_install_completes_when_registered(self):
        """Dedup: an already-registered checkpoint short-circuits to
        completed — no download job is ever queued."""
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m._fleet_installed_row = lambda fid, **kw: _registry_row(fid)
            job = m.start_fleet_install("realvisxl-v5")
            deadline = time.time() + 10
            state = ""
            while time.time() < deadline:
                state = str(m._fleet_jobs["realvisxl-v5"]["state"])
                if state == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(state, "completed")

    def test_start_fleet_install_unknown_id(self):
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            with self.assertRaises(KeyError):
                m.start_fleet_install("not-a-model")


class ResumeDownloadTests(unittest.TestCase):
    """`_download_component` must resume `.part` files via Range requests —
    a restart mid-download continues instead of re-fetching from zero."""

    CONTENT = b"checkpoint-bytes-" * 400  # 6 800 bytes

    def _lib(self, root: Path):
        from localcodeagent.image.library import ImageAssetLibrary
        return ImageAssetLibrary(base_dir=root, models_dir=root / "models",
                                 workflows_dir=root / "workflows")

    def _spec(self) -> dict:
        return {"key": "model", "path": "models/m.safetensors",
                "url": "https://example.test/m.safetensors", "required": True,
                "size_bytes": len(self.CONTENT),
                "sha256": hashlib.sha256(self.CONTENT).hexdigest()}

    def _fake_urlopen(self, support_range: bool):
        seen = {}

        class _Resp(io.BytesIO):
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=0):
            rng = req.headers.get("Range")
            seen["range"] = rng
            if support_range and rng:
                start = int(rng.split("=")[1].split("-")[0])
                r = _Resp(self.CONTENT[start:])
                r.status = 206
                r.headers = {"Content-Length": str(len(self.CONTENT) - start)}
                return r
            r = _Resp(self.CONTENT)
            r.headers = {"Content-Length": str(len(self.CONTENT))}
            return r

        return urlopen, seen

    def test_partial_part_resumes_with_range(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(Path(td))
            part = Path(td) / "models" / "m.safetensors.part"
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(self.CONTENT[:3000])
            urlopen, seen = self._fake_urlopen(support_range=True)
            with mock.patch.object(urllib.request, "urlopen", urlopen):
                out = lib._download_component(self._spec(), repair=False,
                                              progress=lambda d, t: None)
            self.assertEqual(seen["range"], "bytes=3000-")
            self.assertEqual(out["size_bytes"], len(self.CONTENT))
            self.assertFalse(part.exists())
            self.assertEqual((Path(td) / "models" / "m.safetensors").read_bytes(),
                             self.CONTENT)

    def test_server_without_range_restarts_clean(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(Path(td))
            part = Path(td) / "models" / "m.safetensors.part"
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(self.CONTENT[:3000])
            urlopen, _ = self._fake_urlopen(support_range=False)
            with mock.patch.object(urllib.request, "urlopen", urlopen):
                out = lib._download_component(self._spec(), repair=False,
                                              progress=lambda d, t: None)
            self.assertEqual(out["size_bytes"], len(self.CONTENT))

    def test_failed_attempt_keeps_part_for_next_resume(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(Path(td))

            def die(req, timeout=0):
                raise OSError("network drop")

            with mock.patch.object(urllib.request, "urlopen", die):
                with self.assertRaises(OSError):
                    lib._download_component(self._spec(), repair=False,
                                            progress=lambda d, t: None)
            part = Path(td) / "models" / "m.safetensors.part"
            part.write_bytes(self.CONTENT[:2000])  # simulate an aborted job's part
            urlopen, seen = self._fake_urlopen(support_range=True)
            with mock.patch.object(urllib.request, "urlopen", urlopen):
                lib._download_component(self._spec(), repair=False,
                                        progress=lambda d, t: None)
            self.assertEqual(seen["range"], "bytes=2000-")

    def test_full_size_part_verifies_without_download(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(Path(td))
            part = Path(td) / "models" / "m.safetensors.part"
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(self.CONTENT)
            with mock.patch.object(urllib.request, "urlopen",
                                   side_effect=AssertionError("must not download")):
                out = lib._download_component(self._spec(), repair=False,
                                              progress=lambda d, t: None)
            self.assertEqual(out["size_bytes"], len(self.CONTENT))

    def test_corrupt_full_part_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(Path(td))
            part = Path(td) / "models" / "m.safetensors.part"
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"x" * len(self.CONTENT))  # right size, wrong bytes
            urlopen, seen = self._fake_urlopen(support_range=True)
            with mock.patch.object(urllib.request, "urlopen", urlopen):
                lib._download_component(self._spec(), repair=False,
                                        progress=lambda d, t: None)
            self.assertIsNone(seen["range"])


class InvokeAIRuntimeGuardTests(unittest.TestCase):
    """Venv patches the runtime applies on every managed start — a guard
    that silently no-ops (moved anchor) must fail loudly in tests."""

    def _venv(self, td: str) -> Path:
        root = Path(td)
        sp = root / "Lib" / "site-packages" / "invokeai" / "backend" / "model_manager"
        sp.mkdir(parents=True)
        return root

    def test_load_file_guard_patches_and_is_idempotent(self):
        from localcodeagent.image.invokeai_runtime import InvokeAIRuntime
        with tempfile.TemporaryDirectory() as td:
            root = self._venv(td)
            mod = (root / "Lib/site-packages/invokeai/backend/model_manager/"
                   "model_on_disk.py")
            mod.write_text(
                '            elif path.suffix.endswith(".safetensors"):\n'
                '                if _is_sdnq_safetensors(path):\n'
                '                    checkpoint = sdnq_sd_loader(path, compute_dtype=torch.float32)\n'
                '                else:\n'
                '                    checkpoint = safetensors.torch.load_file(path)\n',
                encoding="utf-8")
            InvokeAIRuntime._apply_load_file_guard(root)
            out = mod.read_text(encoding="utf-8")
            self.assertIn("NEXUS PATCH", out)
            self.assertIn("safe_open", out)
            self.assertIn("2 * 1024**3", out)
            InvokeAIRuntime._apply_load_file_guard(root)  # idempotent
            self.assertEqual(mod.read_text(encoding="utf-8"), out)

    def test_load_file_guard_noop_on_missing_anchor(self):
        from localcodeagent.image.invokeai_runtime import InvokeAIRuntime
        with tempfile.TemporaryDirectory() as td:
            root = self._venv(td)
            mod = (root / "Lib/site-packages/invokeai/backend/model_manager/"
                   "model_on_disk.py")
            mod.write_text("# unrelated future layout\n", encoding="utf-8")
            InvokeAIRuntime._apply_load_file_guard(root)
            self.assertEqual(mod.read_text(encoding="utf-8"),
                             "# unrelated future layout\n")

    def test_schema_version_seeded_for_managed_only_yaml(self):
        """A yaml missing schema_version crashes InvokeAI's loader —
        the managed-block writer must seed it (live-verified: fresh
        invokeai.yaml → KeyError: 'schema_version')."""
        from localcodeagent.image.invokeai_runtime import InvokeAIRuntime
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "invokeai.example.yaml").write_text(
                "schema_version: 4.0.3\n", encoding="utf-8")
            self.assertEqual(
                InvokeAIRuntime._invokeai_schema_version(root), "4.0.3")
            (root / "invokeai.example.yaml").unlink()
            self.assertEqual(
                InvokeAIRuntime._invokeai_schema_version(root), "4.0.0")


class SafetyGateTests(unittest.TestCase):
    def test_adult_default_cannot_bypass_creator_gate(self):
        """The configured adult default is routing metadata only — the
        creator-locked setting still rejects explicit requests before
        any model selection runs."""
        with tempfile.TemporaryDirectory() as td:
            m = _manager(Path(td))
            m.adult_content_allowed = lambda: False
            with self.assertRaises(PermissionError):
                m.create_job(ImageRequest(
                    prompt="explicit nude photograph"))


if __name__ == "__main__":
    unittest.main()
