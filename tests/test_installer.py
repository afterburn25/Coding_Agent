from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        self.iss = (ROOT / "packaging" / "ChatNexus.iss").read_text(encoding="utf-8")
        self.workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_installer_is_single_compressed_setup_exe(self):
        self.assertIn("Compression=lzma2/ultra64", self.iss)
        self.assertIn("SolidCompression=yes", self.iss)
        self.assertIn("OutputBaseFilename=Chat-Nexus-Setup-v{#MyAppVersion}", self.iss)

    def test_existing_install_prompts_for_upgrade(self):
        self.assertIn("ReadInstalledVersion", self.iss)
        self.assertIn("is already installed", self.iss)
        self.assertIn("Upgrade this installation to Chat Nexus", self.iss)
        self.assertIn("MB_YESNO", self.iss)
        self.assertIn("UsePreviousAppDir=yes", self.iss)

    def test_upgrade_preserves_config_models_and_source_workspace(self):
        self.assertIn('DestName: "config.json"; Flags: onlyifdoesntexist', self.iss)
        self.assertIn('Name: "{app}\\models"', self.iss)
        self.assertIn("ShouldInstallSource", self.iss)
        self.assertIn("Source Git workspace will be preserved", self.iss)

    def test_ci_builds_and_exercises_installer_upgrade(self):
        self.assertIn("choco install innosetup", self.workflow)
        self.assertIn("Build Chat Nexus installer", self.workflow)
        self.assertIn("Test first install and upgrade-in-place", self.workflow)
        self.assertIn("Upgrade overwrote config.json", self.workflow)
        self.assertIn("Upgrade removed model data", self.workflow)
        self.assertIn("Chat-Nexus-Setup-v0.6.0-dev.exe", self.workflow)


if __name__ == "__main__":
    unittest.main()
