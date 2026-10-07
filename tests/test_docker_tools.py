from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.docker_tool import register_docker_tools


def _reg(workspace: Path) -> ToolRegistry:
    reg = ToolRegistry({})
    register_docker_tools(reg, workspace)
    return reg


class DockerComposeDetectTests(unittest.TestCase):
    def test_detects_compose_services_and_ports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docker-compose.yml").write_text(
                "services:\n"
                "  web:\n"
                "    image: nginx\n"
                "    ports:\n"
                "      - \"8080:80\"\n"
                "  db:\n"
                "    image: postgres:16\n")
            reg = _reg(root)
            out = json.loads(reg.execute(
                "docker_compose_detect", {}, approved=True))
            self.assertEqual(out["count"], 1)
            svcs = {s["name"]: s for s in
                    out["compose_files"][0]["services"]}
            self.assertEqual(set(svcs), {"web", "db"})
            self.assertEqual(svcs["web"]["ports"], ["8080:80"])
            self.assertEqual(svcs["web"]["image"], "nginx")

    def test_empty_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            reg = _reg(Path(td))
            out = json.loads(reg.execute(
                "docker_compose_detect", {}, approved=True))
            self.assertEqual(out["count"], 0)


class DockerDevserversTests(unittest.TestCase):
    def test_parses_published_ports(self):
        with tempfile.TemporaryDirectory() as td:
            reg = _reg(Path(td))
            spec = reg.get("docker_devservers")
            self.assertIsNotNone(spec)
            # Feed a fake docker envelope through a stubbed manifest tool.
            envelope = json.dumps({
                "exit_code": 0,
                "stdout": ("web\t0.0.0.0:8080->80/tcp\tnginx\tUp 2 hours\n"
                           "db\t\tpostgres:16\tUp 2 hours\n"),
                "stderr": ""})
            # Patch registry.execute to return the envelope.
            orig = reg.execute
            reg.execute = lambda *a, **k: envelope  # type: ignore
            out = json.loads(spec.handler({"approved": True}))
            reg.execute = orig  # type: ignore
            self.assertEqual(out["count"], 2)
            self.assertEqual(out["servers"][0]["urls"],
                             ["http://localhost:8080"])
            self.assertEqual(out["servers"][1]["urls"], [])


if __name__ == "__main__":
    unittest.main()
