"""package_release / release_verify — real zip + sha256 registration."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from localcodeagent.artifacts import ArtifactManager
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.release import register_release_tools


class ReleaseToolTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.art = ArtifactManager(self.ws / ".runtime" / "artifacts")
        self.reg = ToolRegistry(
            {"filesystem.read": "allow", "filesystem.write": "allow"})
        register_release_tools(
            self.reg, self.ws, artifacts=lambda: self.art)

    def _run(self, tool_name, **args):
        return json.loads(self.reg.get(tool_name).handler(args))

    def test_package_zips_and_registers(self):
        dist = self.ws / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<h1>hi</h1>")
        (dist / "app.js").write_text("console.log(1)")
        out = self._run("package_release", path="dist", name="site")
        self.assertTrue(out["ok"])
        self.assertEqual(out["files"], 2)
        self.assertTrue(out["archive"].endswith(".zip"))
        # zip really contains the files
        with zipfile.ZipFile(out["archive"]) as zf:
            self.assertEqual(sorted(zf.namelist()),
                             ["app.js", "index.html"])
        # registered + verifiable
        v = self._run("release_verify", artifact_id=out["artifact_id"])
        self.assertTrue(v["ok"])

    def test_empty_dir_errors(self):
        (self.ws / "empty").mkdir()
        out = self._run("package_release", path="empty")
        self.assertIn("error", out)

    def test_outside_root_refused(self):
        out = self._run("package_release",
                        path=str(Path(self.ws).parent.parent))
        self.assertIn("error", out)

    def test_verify_tamper_detected(self):
        dist = self.ws / "dist"
        dist.mkdir()
        (dist / "a.txt").write_text("v1")
        out = self._run("package_release", path="dist")
        Path(out["archive"]).write_bytes(b"tampered")
        v = self._run("release_verify", artifact_id=out["artifact_id"])
        self.assertFalse(v["ok"])


if __name__ == "__main__":
    unittest.main()
