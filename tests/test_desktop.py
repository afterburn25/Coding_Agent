from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.desktop import smoke_test_desktop_backend


ROOT = Path(__file__).resolve().parents[1]


class DesktopHostTests(unittest.TestCase):
    def test_native_desktop_backend_smoke_test(self):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            config_path = temp / "config.json"
            config_path.write_text(json.dumps({
                "runtime_auto_start": False,
                "research_enabled": False,
                "image_enabled": False,
                "models": [{
                    "id": "desktop-smoke",
                    "runtime": "external",
                    "endpoint": "http://127.0.0.1:9/v1",
                    "model": "desktop-smoke",
                    "roles": ["primary_coder"],
                    "enabled": True,
                }],
            }), encoding="utf-8")
            config = AgentConfig(
                models=[ModelProfile(
                    id="desktop-smoke",
                    endpoint="http://127.0.0.1:9/v1",
                    model="desktop-smoke",
                    roles=["primary_coder"],
                    runtime="external",
                )],
                runtime_auto_start=False,
                research_enabled=False,
                image_enabled=False,
            )
            result = smoke_test_desktop_backend(
                config,
                ROOT,
                ROOT / "web",
                temp,
                config_path,
            )
            self.assertTrue(result["ok"], msg=result)
            self.assertTrue(result["checks"]["status"])
            self.assertTrue(result["checks"]["main"])
            self.assertTrue(result["checks"]["image"])
            self.assertTrue(result["checks"]["research"])

    def test_packaged_entry_defaults_to_native_desktop(self):
        main = (ROOT / "localcodeagent" / "__main__.py").read_text(encoding="utf-8")
        desktop = (ROOT / "localcodeagent" / "desktop.py").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("(_frozen() and not args.server) or args.desktop", main)
        self.assertIn("webview.create_window", desktop)
        self.assertIn('gui="edgechromium"', desktop)
        self.assertIn("--windowed", (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8"))
        self.assertIn("windows-desktop:", workflow)
        self.assertIn("Chat-Nexus-v0.6.0-dev-Windows-x64", workflow)


if __name__ == "__main__":
    unittest.main()
