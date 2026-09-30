from __future__ import annotations

import hashlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from localcodeagent.runtime.catalog import (
    CODING_MODEL_CATALOG,
    CodingModelAsset,
    CodingModelCatalogManager,
)


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, data: bytes):
        self._stream = io.BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return self._stream.read(size)


def tiny_asset(data: bytes = b"GGUF-test-model") -> CodingModelAsset:
    return CodingModelAsset(
        id="tiny",
        title="Tiny Test",
        filename="tiny.gguf",
        url="https://example.invalid/tiny.gguf",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        license="test",
        source_repo="test/tiny",
        source_type="test fixture",
        roles=("primary_coder",),
        hardware_note="test",
        description="test",
    )


class CodingModelCatalogTests(unittest.TestCase):
    def test_catalog_has_official_starter_and_code_focused_option(self):
        rows = {item.id: item for item in CODING_MODEL_CATALOG}
        starter = rows["qwen3-14b-q4-k-m"]
        coder = rows["qwen3-coder-30b-a3b-q4-k-m"]
        self.assertEqual(starter.source_type, "official")
        self.assertEqual(starter.size_bytes, 9_001_752_960)
        self.assertEqual(starter.sha256, "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0")
        self.assertIn("community quantization", coder.source_type)
        self.assertIn("deep_reasoner", coder.roles)

    def test_install_downloads_to_temp_verifies_and_atomically_finishes(self):
        data = b"GGUF-test-model"
        asset = tiny_asset(data)
        with tempfile.TemporaryDirectory() as td:
            manager = CodingModelCatalogManager(Path(td))
            with patch("localcodeagent.runtime.catalog.catalog_by_id", return_value={asset.id: asset}), patch(
                "localcodeagent.runtime.catalog.urllib.request.urlopen",
                return_value=_Response(data),
            ):
                job = manager.start_install(asset.id)
                deadline = time.time() + 3
                while time.time() < deadline:
                    job = manager.get_job(job["id"])
                    if job["state"] in {"finished", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "finished", msg=job.get("error"))
                self.assertEqual((Path(td) / asset.filename).read_bytes(), data)
                verified = manager.verify(asset.id, deep_hash=False)
                self.assertTrue(verified["verified"])
                self.assertFalse(list(Path(td).glob("*.part")))

    def test_duplicate_install_request_reuses_active_job(self):
        asset = tiny_asset(b"x" * 1024)

        class SlowResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self, size=-1):
                time.sleep(0.05)
                return b""

        with tempfile.TemporaryDirectory() as td:
            manager = CodingModelCatalogManager(Path(td))
            with patch("localcodeagent.runtime.catalog.catalog_by_id", return_value={asset.id: asset}), patch(
                "localcodeagent.runtime.catalog.urllib.request.urlopen",
                return_value=SlowResponse(),
            ):
                first = manager.start_install(asset.id)
                second = manager.start_install(asset.id)
                self.assertEqual(first["id"], second["id"])

    def test_existing_untrusted_model_is_never_silently_overwritten(self):
        asset = tiny_asset()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / asset.filename).write_bytes(b"bad")
            manager = CodingModelCatalogManager(root)
            with patch("localcodeagent.runtime.catalog.catalog_by_id", return_value={asset.id: asset}):
                with self.assertRaises(FileExistsError):
                    manager.start_install(asset.id, repair=False)

    def test_catalog_api_and_ui_require_explicit_install_action(self):
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('if path == "/api/models/install":', server)
        self.assertIn('if path == "/api/models/install/cancel":', server)
        self.assertIn("/api/models/catalog", app)
        self.assertIn("Download and install", app)
        self.assertIn("Large model downloads can use significant disk space", app)


if __name__ == "__main__":
    unittest.main()
