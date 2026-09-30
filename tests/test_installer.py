from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerContractTests(unittest.TestCase):
    def setUp(self):
        self.installer = (ROOT / "installer" / "ChatNexus.iss").read_text(encoding="utf-8")
        self.workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_single_exe_installer_uses_stable_app_identity_and_compression(self):
        self.assertIn('#define StableAppId "ChatNexus.Afterburn25"', self.installer)
        self.assertIn("AppId={#StableAppId}", self.installer)
        self.assertIn("Compression=lzma2/ultra64", self.installer)
        self.assertIn("SolidCompression=yes", self.installer)
        self.assertIn("OutputBaseFilename=Chat-Nexus-Setup-{#AppVersion}-Windows-x64", self.installer)
        self.assertIn("PrivilegesRequired=lowest", self.installer)
        self.assertIn(r"DefaultDirName={localappdata}\Programs\Chat Nexus", self.installer)

    def test_upgrade_detection_prompts_and_uses_previous_install_directory(self):
        self.assertIn("UsePreviousAppDir=yes", self.installer)
        self.assertIn("DetectExistingInstall()", self.installer)
        self.assertIn("Upgrade now?", self.installer)
        self.assertIn("Upgrade detected", self.installer)
        self.assertIn("MB_YESNO", self.installer)
        self.assertIn("WizardSilent()", self.installer)

    def test_upgrade_contract_preserves_mutable_user_state(self):
        self.assertIn(r'Excludes: "Source\*,models\*,data\*,config.json"', self.installer)
        self.assertIn("ShouldInstallBundledSource", self.installer)
        self.assertIn("PrepareToInstall", self.installer)
        self.assertIn("InstallBundledSource", self.installer)
        self.assertIn(r"{app}\Source\.git\HEAD", self.installer)
        self.assertIn(r'Source: "..\dist\ChatNexus\Source\.git\*"', self.installer)
        self.assertIn("if (not FileExists(UserConfig))", self.installer)
        self.assertIn("FileCopy(ExampleConfig, UserConfig, False)", self.installer)

    def test_ci_compiles_real_installer_and_runs_upgrade_twice(self):
        self.assertIn('installer\\ChatNexus.iss', self.workflow)
        self.assertIn("Chat-Nexus-Setup-0.6.0-dev-Windows-x64.exe", self.workflow)
        self.assertIn("Smoke-test fresh install and upgrade preservation", self.workflow)
        self.assertIn("UPGRADE_PRESERVE.marker", self.workflow)
        self.assertIn("upgrade_preserve_marker", self.workflow)
        self.assertIn("second run intentionally omits /DIR", self.workflow)
        self.assertIn("Upgrade installer run failed", self.workflow)
        self.assertIn("Build native Chat Nexus desktop app", self.workflow)
        self.assertIn("Installed Source workspace is incomplete", self.workflow)
        self.assertNotIn("Chat-Nexus-Setup-v0.6.0-dev.exe", self.workflow)

    def test_legacy_duplicate_installer_definition_is_removed(self):
        self.assertFalse((ROOT / "packaging" / "ChatNexus.iss").exists())


if __name__ == "__main__":
    unittest.main()
