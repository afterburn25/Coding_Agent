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
        self.assertIn("OutputBaseFilename=NexusCore-Setup-{#AppVersion}-Windows-x64", self.installer)
        self.assertIn("PrivilegesRequired=lowest", self.installer)
        # Flat <drive>:\Nexus_Core default — the drive picked by free space.
        self.assertIn(r"DefaultDirName={code:PreferredInstallDir}", self.installer)
        # The code constant already yields the leaf dir; appending the app
        # name again would recreate the nested Nexus_Core\Nexus_Core bug.
        self.assertIn("AppendDefaultDirName=no", self.installer)
        self.assertIn("function PreferredInstallDir", self.installer)

    def test_update_detection_prompts_and_uses_previous_install_directory(self):
        self.assertIn("UsePreviousAppDir=yes", self.installer)
        self.assertIn("DetectExistingInstall()", self.installer)
        self.assertIn("Update now?", self.installer)
        self.assertIn("Update detected", self.installer)
        self.assertIn("MB_YESNO", self.installer)
        self.assertIn("WizardSilent()", self.installer)
        self.assertIn("WizardForm.NextButton.Caption := '&Update'", self.installer)
        self.assertIn("Update Nexus Core", self.installer)

    def test_update_contract_preserves_mutable_user_state(self):
        # The top-level [Files] entry must NOT recurse: a bare-name Excludes
        # pattern matches at ANY depth and silently stripped nested package
        # files (backend\_internal\kokoro_onnx\config.json — killed voice;
        # backend\_internal\tools\manifests; onnxruntime\transformers\models).
        # Exclusions live only on the non-recursive top-level entry; payload
        # subtrees install explicitly with no exclusions at all.
        files_sec = self.installer.split("[Files]")[1].split("[", 1)[0]
        top = [l for l in files_sec.splitlines()
               if 'Source: "..\\dist\\ChatNexus\\*"' in l][0]
        self.assertNotIn("recursesubdirs", top)
        self.assertIn('Excludes: "config.json"', top)
        self.assertIn(
            r'Source: "..\dist\ChatNexus\backend\*"; DestDir: "{app}\backend"; Flags: ignoreversion recursesubdirs',
            self.installer)
        self.assertIn("onlyifdoesntexist", self.installer)
        self.assertIn("ShouldInstallBundledSource", self.installer)
        self.assertIn("PrepareToInstall", self.installer)
        self.assertIn("InstallBundledSource", self.installer)
        self.assertIn(r"{app}\Source\.git\HEAD", self.installer)
        self.assertIn(r'Source: "..\dist\ChatNexus\Source\.git\*"', self.installer)
        self.assertIn("if (not FileExists(UserConfig))", self.installer)
        self.assertIn("CopyFile(ExampleConfig, UserConfig, False)", self.installer)

    def test_installer_bootstraps_missing_default_models_with_hash_verification(self):
        self.assertIn("Qwen3-14B-Q4_K_M.gguf", self.installer)
        self.assertIn("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", self.installer)
        self.assertIn("huggingface.co/Qwen/Qwen3-14B-GGUF", self.installer)
        self.assertIn("huggingface.co/lm-kit/qwen3-coder-30b-a3b-instruct-gguf", self.installer)
        self.assertIn("500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0", self.installer)
        self.assertIn("956682fa9d36d4d0e5a80eb90ff8a001f2c48f988a497e565ae4d0c42af4fe44", self.installer)
        # Models download on TDownloadWizardPage (Inno's built-in responsive
        # download UI) when Install is clicked; silent installs fall back to
        # DownloadTemporaryFile. [Files] `download` has no script-visible
        # progress, and synchronous PrepareToInstall downloads froze the UI.
        self.assertIn("TDownloadWizardPage", self.installer)
        self.assertIn("DownloadTemporaryFile(", self.installer)
        self.assertIn("procedure QueueModelDownloads", self.installer)
        self.assertIn("ShouldDownloadQwen4()", self.installer)
        self.assertIn("ShouldDownloadQwen8()", self.installer)
        self.assertIn("ShouldDownloadQwen14()", self.installer)
        self.assertIn("ShouldDownloadQwen30()", self.installer)
        self.assertIn("ModelIsInstalledAndTrusted", self.installer)
        self.assertIn("GetSHA256OfFile", self.installer)
        self.assertIn("WriteCatalogMetadata", self.installer)
        self.assertIn("CHAT_NEXUS_SKIP_MODEL_DOWNLOADS", self.installer)

    def test_installer_shows_live_download_progress(self):
        # Downloads run on TDownloadWizardPage — Inno's built-in page with a
        # live per-file bar, its own message pump (window stays movable),
        # and a working Abort button. A second bar on the page tracks total
        # bytes across the whole queue. Synchronous PrepareToInstall
        # downloads were tried and froze the wizard — must not come back.
        self.assertIn("DownloadPage := CreateDownloadPage", self.installer)
        self.assertIn("DownloadTotalBar: TNewProgressBar", self.installer)
        self.assertIn("DownloadTotalLabel", self.installer)
        self.assertIn("DownloadPage.Add(", self.installer)
        self.assertIn("DownloadPage.Download", self.installer)
        self.assertIn("DownloadPage.AbortedByUser", self.installer)
        self.assertIn("function NextButtonClick", self.installer)
        # Live bytes stream in through the TOnDownloadProgress callback —
        # polling temp files was a dead end because [Files] `download`
        # reports no progress to script at all.
        self.assertIn("function ModelDlProgress", self.installer)
        self.assertIn("const Progress, ProgressMax: Int64", self.installer)
        # The page must be created when the wizard is initialized.
        wiz = self.installer.split("procedure InitializeWizard")[1]
        wiz = wiz.split("end;", 1)[0]
        self.assertIn("InitializeDownloadPage", wiz)
        # Verified downloads are staged into the models dir by
        # StageVerifiedDownloads on every download-page exit (success, abort,
        # failure) — not [Files] copies, which would double-copy ~35 GB on
        # the success path and skip aborted runs entirely.
        self.assertNotIn('Source: "{tmp}\\{#Qwen4FileName}"', self.installer)
        # Completed downloads are preserved on abort: StageVerifiedDownloads
        # moves every verified staged file into models\ + stamps catalog
        # metadata on the download-page exit path (success, abort, failure).
        # Queued basenames carry the 'dl\' staging-prefix (a junction onto
        # the install drive — see EnsureDlStaging), so the leaf name is what
        # gets tracked.
        self.assertIn("procedure StageVerifiedDownloads", self.installer)
        self.assertIn("DlCompleted.Add(ExtractFileName(FileName))", self.installer)
        # Download bytes must land on the install drive, not {tmp} on the
        # system drive — a junction redirects {tmp}\dl into
        # {ModelsDir}\.dl, and a pre-flight check refuses to start on a
        # drive that cannot fit the queue.
        self.assertIn("EnsureDlStaging", self.installer)
        self.assertIn("mklink /J", self.installer)
        self.assertIn("function EnsureDlDiskSpace", self.installer)
        self.assertIn("GetSpaceOnDisk64", self.installer)
        # Inno 6.4+ enables Windows RedirectionGuard in enforcing mode, which
        # refuses to traverse the staging junction ("untrusted mount point").
        # Our own junction must be traversable — the mitigation stays off.
        self.assertIn("RedirectionGuard=no", self.installer)
        # ...and since the guard is off, EnsureDlStaging must never trust a
        # pre-existing dl entry: unlink reparse points, delete real dirs,
        # then always link to the install-drive target.
        staging = self.installer.split("procedure EnsureDlStaging")[1]
        staging = staging.split("end;", 1)[0]
        self.assertIn("RemoveDir(Link)", staging)
        self.assertIn("DelTree(Link, True, True, True)", staging)
        # An abort must stop the whole queue and offer to exit — never retry
        # the file like a transient failure did (Break inside except does not
        # exit a for loop in Pascal Script).
        perform = self.installer.split("function PerformModelDownloads")[1]
        perform = perform.split("function DetectExistingInstall", 1)[0]
        self.assertIn("while not Done do", perform)
        self.assertIn("WizardForm.Close", perform)

    def test_aborted_install_cleans_partial_progress(self):
        # Canceling mid-install must not leave a half-installed app that
        # the next setup (or the app itself) treats as installed. After
        # Inno's tracked-file rollback, DeinitializeSetup sweeps untracked
        # leftovers — but never preserved payloads (models/tools/state/
        # Source) — and clears the stale uninstall registry entry.
        self.assertIn("procedure DeinitializeSetup", self.installer)
        deinit = self.installer.split("procedure DeinitializeSetup")[1]
        deinit = deinit.split("end;", 1)[0]
        self.assertIn("InstallFilesWritten", deinit)
        self.assertIn("not InstallCompleted", deinit)
        self.assertIn("CleanupAbortedInstall", deinit)
        self.assertIn("RegDeleteKeyIncludingSubkeys", deinit)
        cleanup = self.installer.split("procedure CleanupAbortedInstall")[1]
        cleanup = cleanup.split("procedure DeinitializeSetup", 1)[0]
        for keep in ("models", "tools", "data", ".agent", "output", "Source", "workflows"):
            self.assertIn(f"'{keep}'", self.installer.split("function IsPreservedPayload")[1])
        self.assertIn("RemoveDir(AppDir)", cleanup)
        # The marker must flip only after ssPostInstall completes.
        self.assertIn("InstallCompleted := True", self.installer)

    def test_leftover_files_do_not_count_as_registered_install(self):
        # A partial uninstall that leaves NexusCore.exe behind must NOT
        # brand the next run "Update Nexus Core" — only a live uninstall
        # registry entry marks a real install. File-presence detection may
        # still log the leftover location for diagnostics.
        detect = self.installer.split("function DetectExistingInstall")[1]
        detect = detect.split("function InitializeSetup", 1)[0]
        self.assertIn("RegQueryStringValue(HKCU", detect)
        self.assertIn("RegQueryStringValue(HKLM", detect)
        fallback = detect.split("ChatNexus.exe", 1)[0]
        # The file-presence tail sets a location for logging but never
        # Result := True — fresh installs stay fresh.
        tail = detect.rsplit("DefaultPath := ExpandConstant('{localappdata}\\Programs\\Chat Nexus')", 1)[1]
        self.assertNotIn("Result := True", tail)

    def test_installer_reports_busy_stages_during_blocking_update_work(self):
        # Update-time blocking work (process shutdown, multi-GB SHA-256
        # checks) previously ran with a static wizard and read as frozen;
        # every stage must push explicit status text to the wizard.
        self.assertIn("procedure ShowBusyStatus", self.installer)
        self.assertIn("WizardForm.CurPageID = wpReady", self.installer)
        self.assertIn("'Closing Nexus Core...'", self.installer)
        self.assertIn("'Waiting for Nexus Core to exit...'", self.installer)
        self.assertIn("'Stopping remaining Nexus Core processes...'", self.installer)
        self.assertIn("'Preparing update...'", self.installer)
        self.assertIn("'Verifying existing model file...'", self.installer)
        self.assertIn("'Analyzing files to update...'", self.installer)
        # Sleeps must be chunked through BusySleep so the wizard keeps
        # repainting instead of freezing for the whole grace period.
        self.assertIn("procedure BusySleep", self.installer)
        self.assertIn("BusySleep(1500);", self.installer)
        self.assertIn("BusySleep(500);", self.installer)
        shutdown = self.installer.split("procedure StopRunningNexusCore")[1]
        shutdown = shutdown.split("function PrepareToInstall")[0]
        self.assertNotRegex(shutdown, r"(?m)^\s*Sleep\(")
        # The uninstaller wait loop must surface elapsed time while polling.
        self.assertIn("'Uninstalling... ' + IntToStr(WaitCount div 2) + 's'", self.installer)

    def test_installer_releases_locked_files_before_replacing(self):
        # A leftover backend once survived the image-name taskkill sweep and
        # produced "DeleteFile failed; code 5" mid-update. The script must
        # escalate beyond name matching and verify files are actually
        # unlocked before Inno starts replacing them.
        self.assertIn("backend.pid", self.installer)
        self.assertIn("procedure KillBackendFromPidFile", self.installer)
        self.assertIn("procedure StopProcessesUnderInstallDir", self.installer)
        self.assertIn("Get-Process", self.installer)
        self.assertIn("Stop-Process -Force", self.installer)
        self.assertIn("function FileIsWriteLocked", self.installer)
        self.assertIn("fmOpenReadWrite", self.installer)
        self.assertIn("procedure WaitForInstallFilesUnlock", self.installer)
        self.assertIn("'Waiting for Nexus Core files to be released...'", self.installer)

    def test_process_kill_runs_on_uninstall_reinstall_path(self):
        # A running NexusCore.exe survives its own uninstaller (locked
        # exes can't be deleted). UninstallButtonClick then clears
        # UpgradeDetected, so the sweep must not be gated on it — the
        # real-world failure was "DeleteFile failed; code 5" on
        # NexusCore.exe during the follow-on fresh install.
        stop = self.installer.split("procedure StopRunningNexusCore")[1]
        stop = stop.split("end;", 1)[0]
        self.assertNotIn("if not UpgradeDetected then", stop)
        self.assertNotIn("if UpgradeDetected then", stop)
        prep = self.installer.split("function PrepareToInstall")[1]
        prep = prep.split("InstallBundledSource :=", 1)[0]
        self.assertNotIn("if UpgradeDetected then\n    WaitForInstallFilesUnlock", prep)
        self.assertIn("StopRunningNexusCore", prep)
        self.assertIn("WaitForInstallFilesUnlock", prep)
        # And the uninstaller must stop the app before deleting files so
        # no locked exe survives in the first place.
        self.assertIn("function InitializeUninstall", self.installer)
        uninst = self.installer.split("function InitializeUninstall")[1]
        uninst = uninst.split("end;", 1)[0]
        self.assertIn("{#AppExeName}", uninst)
        self.assertIn("ChatNexus.Backend.exe", uninst)

    def test_uninstall_purges_leftover_install_dir(self):
        # Inno only deletes files it tracked at install time; junctions the
        # host creates (data/.agent/output) plus generated files kept {app}
        # alive — user-visible symptom was "uninstall only removes the
        # registry entry". A post-uninstall sweep must remove untracked
        # leftovers, unlink junctions without traversing them (targets hold
        # user state), and skip the still-running unins000.* so Inno's own
        # final cleanup can drop {app}.
        self.assertIn("procedure CurUninstallStepChanged", self.installer)
        hook = self.installer.split("procedure CurUninstallStepChanged")[1]
        hook = hook.split("end;", 1)[0]
        self.assertIn("usPostUninstall", hook)
        self.assertIn("{#AppExeName}", hook)
        self.assertIn("PurgeLeftoverInstallDir", hook)
        purge = self.installer.split("procedure PurgeLeftoverInstallDir")[1]
        purge = purge.split("procedure CurUninstallStepChanged", 1)[0]
        self.assertIn("unins000.exe", purge)
        self.assertIn("unins000.dat", purge)
        self.assertIn("and $400", purge)
        self.assertIn("RemoveDir(ItemPath)", purge)
        self.assertIn("DelTree(ItemPath, True, True, True)", purge)
        self.assertIn("RemoveDir(AppDir)", purge)

    def test_uninstall_offers_data_scope_checkboxes(self):
        # Uninstall must let the user keep profile, brain files, models, or
        # tools via checkboxes — the categories map to real paths: profiles/
        # brain+nexus_brain under %LOCALAPPDATA%\NexusCore, models/ and
        # tools/ inside {app}.
        self.assertIn("procedure AskUninstallScope", self.installer)
        dlg = self.installer.split("procedure AskUninstallScope")[1]
        dlg = dlg.split("function InitializeUninstall", 1)[0]
        for label in ("Profile", "Brain files", "Models", "Tools"):
            self.assertIn(label, dlg)
        self.assertEqual(dlg.count("TCheckBox.Create"), 4)
        self.assertEqual(dlg.count(".Checked := True"), 4)
        # The hook must run the state purge after junction unlinking, and
        # both purges must be gated on the checkbox globals.
        hook = self.installer.split("procedure CurUninstallStepChanged")[1]
        self.assertIn("PurgeUserState", hook)
        purge = self.installer.split("procedure PurgeUserState")[1]
        purge = purge.split("procedure CurUninstallStepChanged", 1)[0]
        self.assertIn("localappdata", purge)
        self.assertIn("UnDelBrain", purge)
        self.assertIn("UnDelModels", purge)
        self.assertIn("UnDelProfile", self.installer.split("function KeepStateEntry")[1])

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

    def test_windows_build_collects_native_extension_dependencies(self):
        # cryptography's _rust.pyd and py7zr's codec deps are extension
        # modules the frozen backend imports at startup/install time —
        # partial bundling produces ModuleNotFoundError crashes that surface
        # to the user only as a startup timeout.
        self.assertIn("--collect-all cryptography", self.build)
        self.assertIn("--collect-submodules py7zr", self.build)
        for dep in ("pybcj", "pyppmd", "pyzstd", "Cryptodome"):
            self.assertIn(f"--hidden-import {dep}", self.build)

    def test_update_uses_installer_owned_process_shutdown(self):
        self.assertIn("CloseApplications=no", self.installer)
        self.assertNotIn("CloseApplicationsFilter=", self.installer)
        self.assertIn("procedure TaskKillImage", self.installer)
        self.assertIn("procedure StopRunningNexusCore", self.installer)
        self.assertIn("taskkill.exe", self.installer)
        self.assertIn("TaskKillImage('{#AppExeName}', False)", self.installer)
        self.assertIn("TaskKillImage('ChatNexus.exe', False)", self.installer)
        self.assertIn('Type: files; Name: "{app}\ChatNexus.exe"', self.installer)
        self.assertIn("TaskKillImage('ChatNexus.Backend.exe', True)", self.installer)
        self.assertIn("TaskKillImage('llama-server.exe', True)", self.installer)
        self.assertIn("StopRunningNexusCore();", self.installer)

    def test_ci_compiles_real_installer_and_runs_update_twice(self):
        self.assertIn('installer\\ChatNexus.iss', self.workflow)
        self.assertIn("NexusCore-Setup-${env:APP_VERSION}-Windows-x64.exe", self.workflow)
        # Package versions must come from the canonical VERSION file, not a
        # hardcoded string that drifts behind releases.
        self.assertIn("APP_VERSION=$version", self.workflow)
        self.assertNotIn("0.6.0-dev", self.workflow)
        self.assertIn("Smoke-test fresh install and update preservation", self.workflow)
        self.assertIn("UPGRADE_PRESERVE.marker", self.workflow)
        self.assertIn("upgrade_preserve_marker", self.workflow)
        self.assertIn("second run intentionally omits /DIR", self.workflow)
        self.assertIn("Update installer run failed", self.workflow)
        self.assertIn("Update removed imported workflows", self.workflow)
        self.assertIn("Build native Nexus Core desktop app", self.workflow)
        self.assertIn("Installed Source workspace is incomplete", self.workflow)
        self.assertIn("CHAT_NEXUS_SKIP_MODEL_DOWNLOADS", self.workflow)
        self.assertIn("Fake Nexus Core Process", self.workflow)
        self.assertIn("Installer did not close the running NexusCore.exe process", self.workflow)
        self.assertIn("Update left the legacy ChatNexus.exe behind", self.workflow)
        self.assertNotIn("Chat-Nexus-Setup", self.workflow)

    def test_legacy_duplicate_installer_definition_is_removed(self):
        self.assertFalse((ROOT / "packaging" / "ChatNexus.iss").exists())


if __name__ == "__main__":
    unittest.main()
