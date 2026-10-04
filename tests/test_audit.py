"""dep_list + project_audit — manifest parsing, boundary enforcement,
and honest auditor_unavailable reporting."""

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.audit import register_audit_tools


PERMS = {"filesystem.read": "allow", "shell.execute": "allow"}


class AuditToolTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.reg = ToolRegistry(dict(PERMS))
        register_audit_tools(self.reg, self.ws)

    def _run(self, name, **args):
        return json.loads(self.reg.get(name).handler(args))

    def test_dep_list_requirements(self):
        (self.ws / "requirements.txt").write_text(
            "requests==2.31.0\nflask>=3.0\n# comment\n", encoding="utf-8")
        out = self._run("dep_list")
        deps = out["manifests"][0]["dependencies"]
        self.assertEqual(deps[0], {"name": "requests", "spec": "==2.31.0"})
        self.assertEqual(deps[1]["name"], "flask")

    def test_dep_list_package_json(self):
        (self.ws / "package.json").write_text(json.dumps({
            "dependencies": {"react": "^18.0"},
            "devDependencies": {"vite": "^5.0"}}), encoding="utf-8")
        out = self._run("dep_list")
        deps = {d["name"]: d for d in out["manifests"][0]["dependencies"]}
        self.assertEqual(deps["react"]["spec"], "^18.0")
        self.assertTrue(deps["vite"]["dev"])

    def test_dep_list_rejects_outside_root(self):
        out = self._run("dep_list", path=str(Path(self.ws).parent.parent))
        self.assertIn("error", out)

    def test_audit_reports_unavailable_when_no_auditor(self):
        (self.ws / "requirements.txt").write_text("requests==2.31.0\n")
        import shutil
        if shutil.which("pip-audit") or shutil.which("npm") or \
                shutil.which("cargo"):
            # Auditors installed — just verify the shape is honest.
            out = self._run("project_audit")
            self.assertIn(out["audits"][0]["status"],
                          ("ran", "auditor_unavailable", "timeout"))
            return
        out = self._run("project_audit")
        self.assertEqual(out["audits"][0]["status"], "auditor_unavailable")

    def test_audit_no_manifests(self):
        out = self._run("project_audit")
        self.assertEqual(out["audits"], [])
        self.assertIn("no dependency manifests", out["note"])


if __name__ == "__main__":
    unittest.main()
