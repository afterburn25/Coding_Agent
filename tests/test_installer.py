from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerContractTests(unittest.TestCase):
    def setUp(self):
        self.installer = (ROOT / "installer" / "ChatNexus.iss").read_text(encoding="utf-8")
        self.workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.build = (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")

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

    def test_installer_shows_overall_and_per_component_download_progress(self):
        self.assertIn("WizardForm.ProgressGauge", self.installer)
        self.assertIn("ModelProgressBar: TNewProgressBar", self.installer)
        self.assertIn("procedure CurInstallProgressChanged", self.installer)
        self.assertIn("Downloading component ' + IntToStr(ModelNumber) + ' of 2", self.installer)
        self.assertIn("LargestTemporaryFileSize", self.installer)
        self.assertIn("CurrentModelProgressNumber <> ModelNumber", self.installer)
        self.assertIn("Bootstrap downloads complete", self.installer)
        self.assertIn("2 of 2 default model components ready", self.installer)

    def test_optional_tools_not_downloaded_by_installer(self):
        # ComfyUI and image model packs moved to the in-app Tools page so Setup
        # stays fast; none of these artifacts may ship as installer downloads.
        self.assertNotIn("extractarchive", self.installer)
        self.assertNotIn("ComfyUI_windows_portable_nvidia.7z", self.installer)
        for filename in (
            "qwen_image_2.1_int8_convrot.safetensors",
            "qwen3vl_8b_int8_convrot.safetensors",
            "qwen_image_2.1_vae_bf16.safetensors",
            "flux-2-klein-4b.safetensors",
            "qwen_3_4b.safetensors",
            "flux2-vae.safetensors",
        ):
            self.assertNotIn(filename, self.installer)
        for check in (
            "ShouldDownloadComfyUI",
            "ShouldDownloadQwenImageDiff",
            "ShouldDownloadQwenImageText",
            "ShouldDownloadQwenImageVae",
            "ShouldDownloadFluxDiff",
            "ShouldDownloadFluxText",
            "ShouldDownloadFluxVae",
        ):
            self.assertNotIn(check, self.installer)
        self.assertIn("in-app Tools page", self.installer)

    def test_windows_build_bundles_default_image_api_workflows(self):
        self.assertIn("Bundling default ComfyUI API workflows", self.build)
        for workflow in (
            "qwen-image-2.1-t2i-api.json",
            "qwen-image-2.1-edit-api.json",
            "qwen-image-2.1-inpaint-api.json",
            "qwen-image-2.1-background-removal-api.json",
            "flux2-klein-4b-t2i-api.json",
            "flux2-klein-4b-edit-api.json",
        ):
            self.assertIn(workflow, self.build)

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
