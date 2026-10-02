from __future__ import annotations

import re
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = (ROOT / "desktop" / "ChatNexus.Desktop" / "Program.cs").read_text(encoding="utf-8")
CSPROJ = (ROOT / "desktop" / "ChatNexus.Desktop" / "ChatNexus.Desktop.csproj").read_text(encoding="utf-8")
INSTALLER = (ROOT / "installer" / "ChatNexus.iss").read_text(encoding="utf-8")
BUILD = (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def ico_sizes(path: Path) -> set[tuple[int, int]]:
    """Parse the ICONDIR directory without external dependencies."""
    raw = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    assert reserved == 0 and kind == 1, "not a .ico file"
    sizes = set()
    for i in range(count):
        w, h = raw[6 + i * 16], raw[6 + i * 16 + 1]
        sizes.add((w or 256, h or 256))
    return sizes


class OfficialBrandAssetTests(unittest.TestCase):
    def test_supplied_artwork_is_committed_in_canonical_locations(self):
        for rel in (
            "web/assets/nexus-core-logo.png",
            "web/assets/nexus-core-icon.png",
            "web/assets/nexus-core-splash.png",
            "desktop/ChatNexus.Desktop/nexus-core-icon.png",
            "desktop/ChatNexus.Desktop/nexus-core-splash.png",
            "desktop/ChatNexus.Desktop/nexus-core.ico",
        ):
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"missing {rel}")
            self.assertGreater(path.stat().st_size, 1000, f"{rel} looks empty")

    def test_windows_icon_has_all_required_layers(self):
        sizes = ico_sizes(ROOT / "desktop" / "ChatNexus.Desktop" / "nexus-core.ico")
        required = {(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)}
        self.assertFalse(required - sizes, f"ico missing {required - sizes}")

    def test_retired_orbital_logo_is_not_referenced(self):
        for path in list(ROOT.glob("web/*")) + list(ROOT.glob("desktop/**/*.cs")) + [
            ROOT / "packaging" / "build_windows.ps1", ROOT / "installer" / "ChatNexus.iss",
        ]:
            if path.is_file():
                self.assertNotIn("chat-nexus-emblem", path.read_text(encoding="utf-8"), path)

    def test_official_icon_embedded_in_executable_and_installer(self):
        self.assertIn("<ApplicationIcon>nexus-core.ico</ApplicationIcon>", CSPROJ)
        self.assertIn("SetupIconFile=..\\desktop\\ChatNexus.Desktop\\nexus-core.ico", INSTALLER)
        self.assertIn("nexus-core.ico", BUILD)


class ProductNamingTests(unittest.TestCase):
    def test_windows_product_identity_is_nexus_core(self):
        self.assertIn("<AssemblyName>NexusCore</AssemblyName>", CSPROJ)
        self.assertIn("<Product>Nexus Core</Product>", CSPROJ)
        self.assertIn('Text = "Nexus Core";', PROGRAM)
        self.assertIn('"Nexus Core startup error"', PROGRAM)
        self.assertIn("NexusCore.exe", BUILD)
        self.assertIn('#define AppExeName "NexusCore.exe"', INSTALLER)
        self.assertIn('#define AppName "Nexus Core"', INSTALLER)
        self.assertIn("OutputBaseFilename=NexusCore-Setup", INSTALLER)
        self.assertIn('Name: "{autoprograms}\\Nexus Core"', INSTALLER)
        self.assertIn('Name: "{autodesktop}\\Nexus Core"', INSTALLER)
        zip_builder = (ROOT / "packaging" / "make_windows_zip.py").read_text(encoding="utf-8")
        self.assertIn('"NexusCore.exe"', zip_builder)
        self.assertIn('"ChatNexus/NexusCore.exe"', zip_builder)

    def test_no_user_facing_chat_nexus_branding_remains(self):
        # Legacy mentions are only allowed where they preserve upgrade
        # compatibility (stable AppId, legacy exe cleanup, old install dir
        # detection, internal backend/tooling identifiers).
        allowed = re.compile(
            r"ChatNexus\.Afterburn25|ChatNexus\.Backend|ChatNexus\.Desktop|"
            r"ChatNexusHandler|chat_nexus|is_chat_nexus_tree|chat-nexus-test|"
            r"ChatNexus\.iss|dist\\ChatNexus|dist/ChatNexus|CHAT_NEXUS_|"
            r"legacy|pre-rebrand|Chat Nexus dir|ChatNexus\.exe|\"ChatNexus\"|"
            r"Programs\\Chat Nexus",
        )
        offenders = []
        for base in ("web", "localcodeagent", "desktop", "installer"):
            for path in (ROOT / base).rglob("*"):
                if path.suffix not in (".html", ".js", ".css", ".cs", ".py", ".iss"):
                    continue
                for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if ("Chat Nexus" in line or "ChatNexus" in line) and not allowed.search(line):
                        offenders.append(f"{path}:{lineno}: {line.strip()[:90]}")
        self.assertEqual(offenders, [])


class StartupSplashLifecycleTests(unittest.TestCase):
    """Contract tests for the real startup splash state machine."""

    def test_splash_launches_before_main_window(self):
        context_start = PROGRAM.index("NexusCoreApplicationContext(string appDir)")
        ctor = PROGRAM[context_start:context_start + 1200]
        self.assertLess(ctor.index("_splash.Show()"), ctor.index("RunStartupAsync()"))
        # The main window is only created inside the async startup run.
        self.assertIn("_main = new MainForm(_appDir);", PROGRAM)
        self.assertIn("_main.CreateControl();", PROGRAM)  # hidden, handle only

    def test_splash_uses_minimum_seven_second_rule(self):
        self.assertIn("MinimumDisplayTime = TimeSpan.FromSeconds(7)", PROGRAM)
        # Dismissal requires BOTH readiness AND the minimum time — not a timer.
        self.assertRegex(PROGRAM, r"ReadyToDismiss\s*=>\s*AppReady\s*&&\s*MinimumElapsed")

    def test_readiness_requires_real_frontend_handshake(self):
        self.assertIn("nexus-core-ready", PROGRAM)
        self.assertIn("WebMessageReceived", PROGRAM)
        self.assertIn("WaitForInterfaceReadyAsync", PROGRAM)
        # The frontend posts the handshake only after shell initialization.
        self.assertIn("chrome?.webview?.postMessage({type:'nexus-core-ready'})", APP_JS)

    def test_splash_closes_only_after_main_ready_and_transition_is_atomic(self):
        run_start = PROGRAM.index("private async Task RunStartupAsync()")
        body = PROGRAM[run_start:run_start + 2200]
        self.assertIn("await _main.PrepareAsync(_progress);", body)
        self.assertIn("_progress.MarkAppReady();", body)
        self.assertLess(body.index("MarkAppReady"), body.index("ReadyToDismiss"))
        # Splash closes and main shows in the same UI turn — no blank state.
        self.assertLess(body.index("_splash?.Close()"), body.index("_main.Show()"))

    def test_splash_shows_real_milestone_progress(self):
        for primary, secondary in (
            ("INITIALIZING · NEXUS CORE", "Preparing local application environment"),
            ("STARTING · CORE SERVICES", "Waiting for backend health"),
            ("CONNECTING · LOCAL AI RUNTIME", "Backend healthy"),
            ("INITIALIZING · NEXUS INTERFACE", "Initializing WebView2"),
            ("LOADING · NEXUS INTERFACE", "Rendering the Nexus Core application shell"),
            ("CONNECTING · INTERFACE TO CORE", "Waiting for application readiness handshake"),
            ("READY · NEXUS CORE", "All startup-critical systems online"),
        ):
            self.assertIn(primary, PROGRAM)
            self.assertIn(secondary, PROGRAM)
        # Progress never reaches 100% before genuine readiness.
        self.assertIn("0.985", PROGRAM)

    def test_two_line_status_and_hold_state(self):
        # Primary/secondary lines are separate fields driven by the coordinator.
        self.assertIn("public string Primary =>", PROGRAM)
        self.assertIn("public string Secondary =>", PROGRAM)
        # Ready-but-before-7s holds on FINALIZING, never READY.
        self.assertIn('AppReady ? "FINALIZING · NEXUS CORE"', PROGRAM)
        self.assertIn('AppReady ? "Preparing interface"', PROGRAM)

    def test_completion_effect_and_immediate_transition(self):
        # READY + core glow plays briefly, then splash closes and main shows.
        self.assertIn("BeginCompletion()", PROGRAM)
        self.assertIn("CompletionEffectTime", PROGRAM)
        self.assertIn("CompletionPhase", PROGRAM)
        self.assertIn("PathGradientBrush", PROGRAM)  # core glow bloom
        body = PROGRAM[PROGRAM.index("private async Task RunStartupAsync()"):]
        body = body[:2500]
        self.assertLess(body.index("BeginCompletion"), body.index("_splash?.Close()"))
        self.assertLess(body.index("_splash?.Close()"), body.index("_main.Show()"))

    def test_fatal_startup_failure_keeps_actionable_screen(self):
        self.assertIn("ShowFailure(", PROGRAM)
        self.assertIn("NEXUS CORE COULD NOT START", PROGRAM)
        for label in ('"Retry"', '"Open Log"', '"Exit"'):
            self.assertIn(label, PROGRAM)
        self.assertIn("RetryRequested", PROGRAM)
        self.assertIn("backend-host.log", PROGRAM)

    def test_main_form_reports_real_phases_to_progress(self):
        for fraction in ("0.30", "0.55", "0.72", "0.85"):
            self.assertIn(f"progress.Report({fraction}", PROGRAM)
        self.assertIn("WaitUntilHealthyAsync(TimeSpan.FromSeconds(180))", PROGRAM)

    def test_backend_watchdog_recovery_is_preserved(self):
        self.assertIn("UnexpectedExit", PROGRAM)
        self.assertIn("RecoverBackendAsync", PROGRAM)
        self.assertIn("_backendRestartCount > 3", PROGRAM)


class UpgradeIdentityTests(unittest.TestCase):
    def test_stable_appid_preserved_for_upgrade_not_duplicate(self):
        self.assertIn('#define StableAppId "ChatNexus.Afterburn25"', INSTALLER)
        self.assertIn("AppId={#StableAppId}", INSTALLER)
        self.assertIn("UsePreviousAppDir=yes", INSTALLER)
        self.assertIn("Update Nexus Core", INSTALLER)
        # Legacy Chat Nexus installs are detected at the old default dir.
        self.assertIn(r"{localappdata}\Programs\Chat Nexus", INSTALLER)
        self.assertIn("ChatNexus.exe", INSTALLER)  # legacy exe detection

    def test_update_removes_legacy_exe_and_kills_legacy_process(self):
        self.assertIn('Type: files; Name: "{app}\\ChatNexus.exe"', INSTALLER)
        self.assertIn("TaskKillImage('ChatNexus.exe', False)", INSTALLER)
        self.assertIn("TaskKillImage('ChatNexus.exe', True)", INSTALLER)

    def test_ci_verifies_legacy_upgrade_cleanup(self):
        self.assertIn('Copy-Item $App (Join-Path $InstallDir "ChatNexus.exe")', WORKFLOW)
        self.assertIn("Update left the legacy ChatNexus.exe behind", WORKFLOW)
        self.assertIn("legacy ChatNexus.exe process", WORKFLOW)
        self.assertIn("NexusCore-Setup-${env:APP_VERSION}-Windows-x64.exe", WORKFLOW)

    def test_upgrade_preserves_user_data(self):
        self.assertIn(r'Excludes: "Source\*,models\*,data\*,workflows\*,config.json"', INSTALLER)
        self.assertIn("onlyifdoesntexist", INSTALLER)
        self.assertIn("UPGRADE_PRESERVE.marker", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
