from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeDesktopArchitectureTests(unittest.TestCase):
    def test_desktop_host_is_native_dotnet_webview2(self):
        project = (ROOT / "desktop" / "ChatNexus.Desktop" / "ChatNexus.Desktop.csproj").read_text(encoding="utf-8")
        program = (ROOT / "desktop" / "ChatNexus.Desktop" / "Program.cs").read_text(encoding="utf-8")
        self.assertIn("<OutputType>WinExe</OutputType>", project)
        self.assertIn("<UseWindowsForms>true</UseWindowsForms>", project)
        self.assertIn("Microsoft.Web.WebView2", project)
        self.assertIn("WebView2", program)
        self.assertIn("CreateNoWindow = true", program)
        self.assertIn("Kill(entireProcessTree: true)", program)

    def test_python_backend_no_longer_owns_desktop_window(self):
        main = (ROOT / "localcodeagent" / "__main__.py").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("pywebview", pyproject.lower())
        self.assertNotIn("pythonnet", pyproject.lower())
        self.assertNotIn("run_desktop", main)
        self.assertNotIn("webview", main.lower())

    def test_windows_build_does_not_package_pythonnet_desktop_stack(self):
        build = (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("ChatNexus.Backend.exe", build)
        self.assertIn("dotnet publish", build)
        self.assertIn("--self-test", build)
        self.assertNotIn("pywebview", build.lower())
        self.assertNotIn("pythonnet", build.lower())
        self.assertNotIn("pywebview", workflow.lower())
        self.assertNotIn("pythonnet", workflow.lower())

    def test_chat_preflight_blocks_broken_stream_when_models_are_unavailable(self):
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("coding_model_setup_required", server)
        self.assertIn("ready_to_code", server)
        self.assertIn("coding_model_setup_required", app)
        self.assertIn("loadReadiness()", app)


if __name__ == "__main__":
    unittest.main()
