"""Canonical version consistency — VERSION file drives every artifact."""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class VersionTests(unittest.TestCase):
    def test_version_file_valid_semver(self):
        raw = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(raw, r"^\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$")

    def test_version_module_reads_file(self):
        from localcodeagent.version import version, version_tuple
        raw = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version(), raw)
        self.assertEqual(version_tuple()[:3],
                         tuple(int(x) for x in raw.split("-")[0].split(".")[:3]))

    def test_all_artifacts_in_sync(self):
        out = subprocess.run(
            [sys.executable, "scripts/sync_version.py", "--check"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0,
                         f"version artifacts out of sync: {out.stdout} {out.stderr}")

    def test_server_uses_canonical_version(self):
        from localcodeagent import server
        raw = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(server.VERSION, raw)

    def test_pyproject_matches(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        raw = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertIn(f'version = "{raw}"', text)
        self.assertIn(
            'dependencies = ["cryptography==50.0.1", "py7zr==1.1.3"]',
            text)

    def test_installer_defaults_match(self):
        text = (ROOT / "installer" / "ChatNexus.iss").read_text(encoding="utf-8")
        raw = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        numeric = raw.split("-")[0] + ".0"
        self.assertIn(f'#define AppVersion "{raw}"', text)
        self.assertIn(f'#define AppNumericVersion "{numeric}"', text)


class CheckpointConsistencyTests(unittest.TestCase):
    """The docs broadcast a single "current" test count in four places —
    this pins them so a partial sync fails the suite instead of drifting.
    Update the actual count after any test-count change (same commit).
    """

    EXPECTED = 1664

    def _counts(self):
        checks = {
            "PROJECT_STATUS.md": r"Current automated checkpoint:\s*\*\*(\d+) tests passing",
            "README.md": r"Current expected result:\s*\*\*(\d+) tests passing",
            "SESSION_HANDOFF.md": r"Expected at this checkpoint:\s*`(\d+) tests`",
        }
        out = {}
        for name, pat in checks.items():
            text = (ROOT / name).read_text(encoding="utf-8")
            m = re.search(pat, text)
            self.assertIsNotNone(m, f"{name}: checkpoint line missing/changed")
            out[name] = int(m.group(1))
        import json
        pj = json.loads((ROOT / ".agent" / "project.json").read_text(
            encoding="utf-8"))
        out[".agent/project.json"] = int(pj["test_checkpoint"])
        return out

    def test_checkpoint_counts_agree(self):
        counts = self._counts()
        for name, n in counts.items():
            self.assertEqual(n, self.EXPECTED,
                             f"{name} reports {n}, expected {self.EXPECTED}")


if __name__ == "__main__":
    unittest.main()


