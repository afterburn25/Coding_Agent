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

    def test_update_detection_prompts_and_uses_previous_install_directory(self):
        self.assertIn("UsePreviousAppDir=yes", self.installer)
        self.assertIn("DetectExistingInstall()", self.installer)
        self.assertIn("Update now?", self.installer)
        self.assertIn("Update detected", self.installer)
        self.assertIn("MB_YESNO", self.installer)
        self.assertIn("WizardSilent()", self.installer)
        self.assertIn("WizardForm.NextButton.Caption := '&Update'", self.installer)
        self.assertIn("Update Chat Nexus", self.installer)

    def test_update_contract_preserves_mutable_user_state(self):
        self.assertIn(r'Excludes: "Source\*,models\*,data\*,workflows\*,config.json"', self.installer)
        self.assertIn("onlyifdoesntexist", self.installer)
        self.assertIn("ShouldInstallBundledSource", self.installer)
        self.assertIn("PrepareToInstall", self.installer)
        self.assertIn("InstallBundledSource", self.installer)
        self.assertIn(r"{app}\Source\.git\HEAD", self.installer)
        self.assertIn(r'Source: "..\dist\ChatNexus\Source\.git\*"', self.installer)
        self.assertIn("if (not FileExists(UserConfig))", self.installer)
        self.assertIn("FileCopy(ExampleConfig, UserConfig, False)", self.installer)

    def test_installer_bootstraps_missing_default_models_with_hash_verification(self):
        self.assertIn("Qwen3-14B-Q4_K_M.gguf", self.installer)
        self.assertIn("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", self.installer)
        self.assertIn("huggingface.co/Qwen/Qwen3-14B-GGUF", self.installer)
        self.assertIn("huggingface.co/lm-kit/qwen3-coder-30b-a3b-instruct-gguf", self.installer)
        self.assertIn("500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0", self.installer)
        self.assertIn("956682fa9d36d4d0e5a80eb90ff8a001f2c48f988a497e565ae4d0c42af4fe44", self.installer)
        self.assertGreaterEqual(self.installer.count("Flags: external download ignoreversion nocompression"), 2)
        self.assertIn("Check: ShouldDownloadQwen14", self.installer)
        self.assertIn("Check: ShouldDownloadQwen30", self.installer)
        self.assertIn("ModelIsInstalledAndTrusted", self.installer)
        self.assertIn("GetSHA256OfFile", self.installer)
        self.assertIn("WriteCatalogMetadata", self.installer)
        self.assertIn("CHAT_NEXUS_SKIP_MODEL_DOWNLOADS", self.installer)

    def test_installer_shows_overall_and_per_model_download_progress(self):
        self.assertIn("WizardForm.ProgressGauge", self.installer)
        self.assertIn("ModelProgressBar: TNewProgressBar", self.installer)
        self.assertIn("procedure CurInstallProgressChanged", self.installer)
        self.assertIn("Downloading coding model ' + IntToStr(ModelNumber) + ' of 2", self.installer)
        self.assertIn("LargestModelTemporaryFileSize", self.installer)
        self.assertIn(r"{app}\models\*.tmp", self.installer)
        self.assertIn("CurrentModelProgressNumber <> ModelNumber", self.installer)
        self.assertIn("Coding model downloads complete", self.installer)

    def test_update_uses_installer_owned_process_shutdown(self):
        self.assertIn("CloseApplications=no", self.installer)
        self.assertNotIn("CloseApplicationsFilter=", self.installer)
        self.assertIn("procedure TaskKillImage", self.installer)
        self.assertIn("procedure StopRunningChatNexus", self.installer)
        self.assertIn("taskkill.exe", self.installer)
        self.assertIn("TaskKillImage('{#AppExeName}', False)", self.installer)
        self.assertIn("TaskKillImage('ChatNexus.Backend.exe', True)", self.installer)
        self.assertIn("TaskKillImage('llama-server.exe', True)", self.installer)
        self.assertIn("StopRunningChatNexus();", self.installer)

    def test_ci_compiles_real_installer_and_runs_update_twice(self):
        self.assertIn('installer\\ChatNexus.iss', self.workflow)
        self.assertIn("Chat-Nexus-Setup-0.6.0-dev-Windows-x64.exe", self.workflow)
        self.assertIn("Smoke-test fresh install and update preservation", self.workflow)
        self.assertIn("UPGRADE_PRESERVE.marker", self.workflow)
        self.assertIn("upgrade_preserve_marker", self.workflow)
        self.assertIn("second run intentionally omits /DIR", self.workflow)
        self.assertIn("Update installer run failed", self.workflow)
        self.assertIn("Update removed imported workflows", self.workflow)
        self.assertIn("Build native Chat Nexus desktop app", self.workflow)
        self.assertIn("Installed Source workspace is incomplete", self.workflow)
        self.assertIn("CHAT_NEXUS_SKIP_MODEL_DOWNLOADS", self.workflow)
        self.assertIn("Fake Chat Nexus Process", self.workflow)
        self.assertIn("Installer did not close the running ChatNexus.exe process", self.workflow)
        self.assertNotIn("Chat-Nexus-Setup-v0.6.0-dev.exe", self.workflow)

    def test_legacy_duplicate_installer_definition_is_removed(self):
        self.assertFalse((ROOT / "packaging" / "ChatNexus.iss").exists())


if __name__ == "__main__":
    unittest.main()
