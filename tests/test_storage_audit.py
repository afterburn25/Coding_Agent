"""§23 — storage audit tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.storage_audit import audit_storage


class StorageAuditTests(unittest.TestCase):

    def test_classifies_by_size_and_records(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "small.json").write_text(json.dumps({"a": 1}))
            (root / "big.json").write_text(json.dumps(
                {"rows": [{"x": i} for i in range(600)]}))
            (root / "huge.json").write_bytes(b" " * 1_200_000)
            out = audit_storage(root)
            classes = {r["path"]: r["class"] for r in out["largest"]
                       + out["sqlite_candidates"]}
            self.assertEqual(classes["small.json"], "bounded_ok")
            self.assertEqual(classes["big.json"], "sqlite_candidate")
            self.assertEqual(classes["huge.json"], "sqlite_candidate")
            self.assertEqual(out["by_class"]["sqlite_candidate"], 2)

    def test_skips_noise_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            noisy = root / "node_modules"
            noisy.mkdir()
            (noisy / "dep.json").write_text("{}")
            out = audit_storage(root)
            self.assertEqual(out["files_scanned"], 0)

    def test_invalid_json_still_classified_by_size(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "broken.json").write_bytes(b"{" * 1_100_000)
            out = audit_storage(root)
            cand = out["sqlite_candidates"]
            self.assertEqual(len(cand), 1)
            self.assertEqual(cand[0]["records"], -1)

    def test_truncation_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(5):
                (root / f"f{i}.json").write_text("{}")
            out = audit_storage(root, max_files=3)
            self.assertTrue(out["truncated"])
            self.assertEqual(out["files_scanned"], 3)


if __name__ == "__main__":
    unittest.main()
