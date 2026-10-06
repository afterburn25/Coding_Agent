from __future__ import annotations

import json
import re
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = (ROOT / "desktop" / "ChatNexus.Desktop" / "Program.cs").read_text(encoding="utf-8")
PROGRESS = (ROOT / "desktop" / "ChatNexus.Desktop" / "StartupProgress.cs").read_text(encoding="utf-8")
# Progress model lives in StartupProgress.cs; splash/window plumbing in Program.cs.
DESKTOP = PROGRAM + PROGRESS
CSPROJ = (ROOT / "desktop" / "ChatNexus.Desktop" / "ChatNexus.Desktop.csproj").read_text(encoding="utf-8")
INSTALLER = (ROOT / "installer" / "ChatNexus.iss").read_text(encoding="utf-8")
BUILD = (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def desktop_method_body(name: str) -> str:
    """Read a complete desktop method, independent of parameters and length."""
    match = re.search(
        rf"^    (?:public|private|protected|internal)\b[^\n{{]*\b{re.escape(name)}"
        rf"\([^)]*\)\s*\{{(?P<body>.*?)^    \}}",
        PROGRAM,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Desktop method not found: {name}")
    return match.group("body")


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
        ctor = desktop_method_body("NexusCoreApplicationContext")
        self.assertLess(ctor.index("_splash.Show()"), ctor.index("RunStartupAsync()"))
        # The main window is only created inside the async startup run.
        self.assertNotIn("_main = new MainForm", ctor)
        startup = desktop_method_body("RunStartupAsync")
        self.assertIn("_main = new MainForm(_appDir);", startup)
        self.assertIn("_main.CreateControl();", startup)  # hidden, handle only

    def test_splash_uses_minimum_seven_second_rule(self):
        self.assertIn("MinimumDisplayTime = TimeSpan.FromSeconds(7)", PROGRESS)
        # Dismissal requires BOTH readiness AND the minimum time — not a timer.
        self.assertRegex(PROGRESS, r"ReadyToDismiss\s*=>\s*AppReady\s*&&\s*MinimumElapsed")
        # And only once the bar has smoothly reached 100% — never a snap.
        self.assertIn("_displayed >= 0.9995", PROGRESS)

    def test_readiness_requires_real_frontend_handshake(self):
        self.assertIn("nexus-core-ready", PROGRAM)
        self.assertIn("WebMessageReceived", PROGRAM)
        self.assertIn("WaitForInterfaceReadyAsync", PROGRAM)
        # The frontend posts the handshake only after shell initialization.
        self.assertIn("chrome?.webview?.postMessage({type:'nexus-core-ready'})", APP_JS)

    def test_splash_closes_only_after_main_ready_and_transition_is_atomic(self):
        body = desktop_method_body("RunStartupAsync")
        self.assertIn("await _main.PrepareAsync(_progress);", body)
        self.assertIn("_progress.MarkAppReady();", body)
        self.assertLess(body.index("MarkAppReady"), body.index("ReadyToDismiss"))
        # Splash closes and main shows in the same UI turn — no blank state.
        self.assertLess(body.index("_splash?.Close()"), body.index("_main.Show()"))

    def test_splash_shows_real_milestone_progress(self):
        for primary, secondary in (
            ("INITIALIZING · NEXUS CORE", "Establishing core startup environment"),
            ("CORE CONTROL · ESTABLISHED", "Loading configuration and protected system state"),
            ("STARTING · CORE SERVICES", "Launching Nexus agent and service runtime"),
            ("VERIFYING · CORE INTEGRITY", "Confirming backend health and authorization"),
            ("SYNCHRONIZING · NEXUS BRAIN", "Restoring memory, models and system continuity"),
            ("OPENING · COMMAND INTERFACE", "Initializing the Nexus control environment"),
            ("LOADING · NEXUS WORKSPACE", "Connecting tools, profiles and workspace services"),
            ("SYNCHRONIZING · CORE INTERFACE", "Establishing communication with core systems"),
            ("FINALIZING · NEXUS CORE", "Verifying interface and system readiness"),
            ("CORE SYSTEMS · ONLINE", "Nexus Core ready"),
        ):
            self.assertIn(primary, DESKTOP)
            self.assertIn(secondary, DESKTOP)
        # Progress never reaches 100% before genuine readiness.
        self.assertIn("0.985", PROGRESS)

    def test_progress_model_is_three_layer_dt_based(self):
        # Real milestones, prediction, and rendering are independent values.
        self.assertIn("public double RealProgress", PROGRESS)
        self.assertIn("public double PredictedProgress", PROGRESS)
        self.assertIn("public double DisplayedProgress", PROGRESS)
        # Velocity-smoothed, frame-rate-independent animation.
        self.assertIn("_velocity", PROGRESS)
        self.assertRegex(PROGRESS, r"dt\s*=\s*Math\.Clamp")
        # Each phase has a ceiling the prediction may approach, never exceed.
        self.assertIn("PhaseCeiling()", PROGRESS)
        # Failure freezes the bar rather than crawling forever.
        self.assertIn("MarkFailed()", PROGRESS)
        self.assertIn("_progress.MarkFailed();", PROGRAM)

    def test_startup_timing_profile_paced_and_persisted(self):
        # Per-machine timing profile paces prediction; written once at completion.
        self.assertIn("startup_profile.json", DESKTOP)
        self.assertIn("class StartupProfile", PROGRESS)
        self.assertIn("Record(", PROGRESS)
        self.assertIn("ExpectedSeconds(", PROGRESS)
        # Stall-aware secondary status for genuinely slow phases.
        self.assertIn("taking longer than usual", PROGRESS)
        self.assertIn("Still waiting", PROGRESS)

    def test_two_line_status_and_hold_state(self):
        # Primary/secondary lines are separate fields driven by the coordinator.
        self.assertIn("public string Primary", PROGRESS)
        self.assertIn("public string Secondary", PROGRESS)
        # Ready-but-before-7s holds on FINALIZING, never READY.
        self.assertIn('"FINALIZING · NEXUS CORE"', PROGRESS)
        self.assertIn('"Verifying interface and system readiness"', PROGRESS)
        self.assertIn('"CORE SYSTEMS · ONLINE"', PROGRESS)

    def test_completion_effect_and_immediate_transition(self):
        # READY + core glow plays briefly, then splash closes and main shows.
        self.assertIn("BeginCompletion()", DESKTOP)
        self.assertIn("CompletionEffectTime", PROGRESS)
        self.assertIn("CompletionPhase", PROGRESS)
        self.assertIn("PathGradientBrush", PROGRAM)  # core glow bloom
        body = desktop_method_body("RunStartupAsync")
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
        # Mutable user state survives upgrades: the user config is excluded
        # from the top-level copy, imported workflows install only when
        # absent, and an existing Source workspace is never overwritten.
        self.assertIn(r'Excludes: "config.json"', INSTALLER)
        self.assertIn("onlyifdoesntexist", INSTALLER)
        self.assertIn("ShouldInstallBundledSource", INSTALLER)
        self.assertIn("UPGRADE_PRESERVE.marker", WORKFLOW)


class CinematicSplashTests(unittest.TestCase):
    """Contract tests for the production cinematic splash integration.

    The web layer under desktop/ChatNexus.Desktop/splash is pure
    presentation: readiness authority stays in StartupProgress +
    RunStartupAsync, and the static WinForms render is the fallback.
    """

    SPLASH_DIR = ROOT / "desktop" / "ChatNexus.Desktop" / "splash"
    SPLASH_MJS = SPLASH_DIR / "web" / "splash.mjs"
    NARRATOR = (ROOT / "desktop" / "ChatNexus.Desktop" / "StartupNarrator.cs")

    def test_cinematic_assets_ship_in_build(self):
        self.assertIn('splash\\**', CSPROJ)
        self.assertIn('DestDir: "{app}\\splash"', INSTALLER)
        self.assertTrue((self.SPLASH_DIR / "animation_manifest.json").is_file())
        self.assertTrue(self.SPLASH_MJS.is_file())
        self.assertGreater(
            len(list((self.SPLASH_DIR / "audio").glob("*.wav"))), 10)

    def test_manifest_is_valid_and_gated(self):
        import json
        m = json.loads(
            (self.SPLASH_DIR / "animation_manifest.json").read_text(encoding="utf-8"))
        gate_ids = {g["id"] for g in m["gates"]}
        # Host gate map in Program.cs must cover every manifest gate.
        for gate in ("services", "authorization", "unlock", "open", "charge", "ready"):
            self.assertIn(gate, gate_ids)
            self.assertIn(f'"{gate}"', PROGRAM)
        self.assertIn("failure", m)      # fault/containment sequence defined
        self.assertIn("recovery", str(m.get("recoveryHooks", "")) + str(m.keys()))

    def test_webview2_cinematic_layer_with_static_fallback(self):
        self.assertIn("SetVirtualHostNameToFolderMapping", PROGRAM)
        self.assertIn("nexus.splash", PROGRAM)
        self.assertIn("splash-ready", PROGRAM)
        self.assertIn("splash-error", PROGRAM)      # falls back to static art
        # Static artwork + progress bar still render beneath/instead.
        self.assertIn("nexus-core-splash.png", PROGRAM)

    def test_host_posts_progress_gates_and_faults(self):
        self.assertIn('"set-gate"', PROGRAM)
        self.assertIn('"set-progress"', PROGRAM)
        self.assertIn('"trigger-fault"', PROGRAM)
        self.assertIn('"show-recovery"', PROGRAM)
        self.assertIn("recovery-action", PROGRAM)
        self.assertIn("RetryRequested", PROGRAM)
        self.assertIn("ExitRequested", PROGRAM)

    def test_presentation_never_owns_startup(self):
        # The cinematic is gated by real milestones — PumpCinematic feeds
        # gates from _progress.RealProgress, not wall-clock time.
        self.assertIn("_progress.RealProgress", PROGRAM)
        self.assertIn("PumpCinematic", PROGRAM)
        # Splash dismissal is still the real readiness handshake.
        self.assertIn("ReadyToDismiss", DESKTOP)

    def test_narrator_exact_lines(self):
        text = self.NARRATOR.read_text(encoding="utf-8")
        self.assertIn("Nexus Core initializing.", text)
        self.assertIn("Core systems online.", text)
        # Exact fault line (split across C# string literals in source).
        self.assertIn("Startup fault detected. Core Destabilization Imminent.", text)
        self.assertIn("Core containment engaged. Beginning recovery diagnostics.", text)
        self.assertIn("Welcome to Nexus Core. I", text)
        # Welcome only persists after playback actually began.
        self.assertIn("welcome_played", text)
        self.assertIn("MarkWelcomePlayed", text)
        # Narration is async and cancellable — never blocks recovery.
        self.assertIn("Task.Run", text)
        self.assertIn("Cancel()", text)

    def test_narration_settings_keys(self):
        text = self.NARRATOR.read_text(encoding="utf-8")
        for key in ("voice_enabled", "voice_muted", "silent_startup",
                    "safe_mode", "startup_narration"):
            self.assertIn(key, text)
        self.assertIn("splash_volume", text)

    def test_splash_page_is_pure_presentation(self):
        src = self.SPLASH_MJS.read_text(encoding="utf-8")
        # No self-driven readiness: state arrives via host postMessage.
        self.assertIn("chrome?.webview?.postMessage", src)
        self.assertIn("'set-gate'", src)
        self.assertIn("'trigger-fault'", src)
        self.assertIn("'play-voice'", src)
        self.assertIn("recovery-action", src)

    def test_status_vocabulary_is_single_source_and_approved(self):
        # The canonical two-line status vocabulary lives in one map —
        # cinematic and WinForms fallback both render _progress.Primary/
        # Secondary, so neither surface can drift to different wording.
        self.assertIn("StartupStatus", DESKTOP)
        for label in (
            "INITIALIZING · NEXUS CORE", "CORE CONTROL · ESTABLISHED",
            "STARTING · CORE SERVICES", "VERIFYING · CORE INTEGRITY",
            "SYNCHRONIZING · NEXUS BRAIN", "OPENING · COMMAND INTERFACE",
            "LOADING · NEXUS WORKSPACE", "SYNCHRONIZING · CORE INTERFACE",
            "FINALIZING · NEXUS CORE", "CORE SYSTEMS · ONLINE",
            "CALIBRATING · MODEL RUNTIME", "VERIFYING · CAPABILITIES",
            "INITIALIZING · VOICE SYSTEM", "CHECKING · VISUAL SYSTEMS",
            "ACTIVATING · LANGUAGE CORE",
            "ACTIVATING · DEVELOPMENT CORE", "CALIBRATING · MODEL MEMORY",
            "PREPARING · WORKSTATION", "BACKGROUND SETUP · SCHEDULED",
            "NEXUS CORE · COULD NOT START", "SAFE MODE · INITIALIZING",
            "RESTORING · LAST KNOWN GOOD", "RECOVERY · ANALYZING",
            "REPAIR · IN PROGRESS", "CONTAINMENT · ENGAGED",
            "ANOMALY DETECTED · CORE SERVICES",
        ):
            self.assertIn(label, DESKTOP, label)
        # Same status source on both surfaces.
        self.assertIn("_progress.Primary", DESKTOP)
        self.assertIn("_progress.Secondary", DESKTOP)
        # Provisioning text only appears behind a real enablement check.
        self.assertIn("ProvisioningPlanned", PROGRAM)
        self.assertIn("provisioning_dev_enable", PROGRAM)
        # Status coalescing exists so fast milestones can't flash text.
        self.assertIn("StatusHold", DESKTOP)

    def test_startup_voice_completion_gate(self):
        text = self.NARRATOR.read_text(encoding="utf-8")
        # The gate keys on real playback lifecycle, never post/queue time.
        self.assertIn("VoiceGateAsync", text)
        self.assertIn("NotifyVoiceResult", text)
        self.assertIn("NotifyVoiceEnded", text)
        self.assertIn("_lastPlaybackEnd", text)
        # 2000 ms quiet buffer after final playback end.
        self.assertRegex(text, r"QuietBuffer\s*=\s*TimeSpan\.FromSeconds\(2\)")
        # Bounded failsafes: lost started/ended acks can't hang startup.
        self.assertIn("StartedAckTimeout", text)
        self.assertIn("LostEndWatchdogSlack", text)
        # Voice-off and fault paths bypass the buffer entirely.
        self.assertIn("_faulted", text)
        # Host holds the splash on the gate before tearing down.
        self.assertIn("VoiceGateAsync", PROGRAM)
        self.assertIn("VoicePlaybackResult", PROGRAM)
        self.assertIn("VoicePlaybackEnded", PROGRAM)

    def test_greeting_waits_for_startup_transition_release(self):
        # The desktop releases the interface greeting only after the
        # splash's voice gate completes — never under narration/buffer.
        self.assertIn("startup-transition-complete", PROGRAM)
        self.assertIn("SignalStartupTransition", PROGRAM)
        profile_js = (ROOT / "web" / "profile.js").read_text(encoding="utf-8")
        self.assertIn("startup-transition-complete", profile_js)
        self.assertIn("__nexusStartupGate", profile_js)
        self.assertIn("chrome.webview", profile_js)


class RecoverySequenceTests(unittest.TestCase):
    """Contract tests for the fault → intervention → retry → recovery and
    recovery-failed flows ported from prototypes/core_unlock_splash.

    The sequence clips are presentation references: stage captions may only
    ever display a milestone the host confirmed, the intervention state owns
    the real buttons, and every attempt is explicit-user-driven.
    """

    SPLASH = ROOT / "desktop" / "ChatNexus.Desktop" / "splash"
    PAGE = SPLASH / "web" / "splash.mjs"
    CONTROLLER = SPLASH / "web" / "controller.mjs"
    INDEX = SPLASH / "web" / "index.html"

    def test_sequence_clips_ship_and_wire(self):
        html = self.INDEX.read_text(encoding="utf-8")
        for clip in ("NexusCore-Startup-Glow-Only.mp4",
                     "NexusCore-Error-Red-Continuation.mp4",
                     "NexusCore-Recovery.mp4",
                     "NexusCore-Recovery-Failed.mp4"):
            self.assertTrue((self.SPLASH / "assets" / clip).is_file(), clip)
        for vid in ("bootvid", "errvid", "recvid", "failvid"):
            self.assertIn(vid, html)

    def test_recovery_stage_captions_single_source(self):
        src = self.CONTROLLER.read_text(encoding="utf-8")
        for label in (
            "EMERGENCY CONTAINMENT · ENGAGED", "NONESSENTIAL SYSTEMS · ISOLATED",
            "RECOVERY MATRIX · INITIALIZING", "FAULT SOURCE · LOCATED",
            "CORE RECONSTRUCTION · IN PROGRESS", "STABILITY THRESHOLD · RECOVERING",
            "CONTAINMENT · RELEASED", "CORE INTEGRITY · VERIFIED",
            "CORE SYSTEMS · ONLINE",
        ):
            self.assertIn(label, src, label)
        # Caption-entry gates inside the recovery clip.
        self.assertIn("RECOVERY_STAGE_TIMES", src)
        for label in (
            "RECOVERY ATTEMPT · FAILED", "AUTOMATIC RECOVERY · HALTED",
            "CORE CONTAINMENT · MAINTAINED", "USER INTERVENTION · REQUIRED",
        ):
            self.assertIn(label, src, label)

    def test_page_gates_recovery_on_confirmed_milestones(self):
        src = self.PAGE.read_text(encoding="utf-8")
        # Host messages that drive the attempt lifecycle.
        for msg in ("'recovery-begin'", "'recovery-stage'", "'recovery-failed'"):
            self.assertIn(msg, src, msg)
        # The clip free-runs through the attempt — the only pause is the
        # online boundary, the one claim that must wait for host confirmation.
        self.assertIn("confirmedStage < RECOVERY_STAGE_TIMES.length - 1", src)
        self.assertIn("RECOVERY_STAGE_TIMES[RECOVERY_STAGE_TIMES.length - 1]", src)
        # Narration fires on the footage's own visual beats, not confirms.
        self.assertIn("RECOVERY_NARRATION", src)
        self.assertIn("'recovery-stage-shown'", src)
        # One in-flight attempt; duplicate Retry coalesces.
        self.assertIn("attemptInFlight", src)
        self.assertIn("if (attemptInFlight || !clock.activeFault) return", src)
        # Intervention panel is suppressed while a clip owns the surface,
        # and returns when it ends contained.
        self.assertIn("overlay === null", src)
        self.assertIn("showIntervention", src)
        # Green online never survives into a fault/failed surface.
        self.assertIn("classList.remove('online')", src)
        # Early faults keep real partial geometry — DOM containment path.
        self.assertIn("useErrorClip", src)
        self.assertIn("videoMode = false", src)

    def test_host_runs_single_flight_real_attempt(self):
        # Retry coalescing on the host, not just the page.
        self.assertIn("Interlocked.CompareExchange(ref _recoveryInFlight", PROGRAM)
        self.assertIn("_recoveryInFlight", PROGRAM)
        # The attempt re-runs the REAL startup pipeline, posting stages only
        # after milestones actually complete.
        self.assertIn("RunRecoveryAttemptAsync", PROGRAM)
        self.assertIn('"recovery-begin"', PROGRAM)
        self.assertIn('"recovery-stage"', PROGRAM)
        self.assertIn('"recovery-failed"', PROGRAM)
        attempt = PROGRAM.split("RunRecoveryAttemptAsync()", 1)[1]
        self.assertLess(attempt.index("PrepareAsync"), attempt.index("VerifyLoadableAsync"))
        self.assertLess(attempt.index("VerifyLoadableAsync"), attempt.index("MarkAppReady"))
        self.assertLess(attempt.index("MarkAppReady"), attempt.index("ReadyToDismiss"))
        self.assertIn("RecoveryStage(8)", PROGRAM)   # online only after verified
        # Failure path: interrupted recovery, intervention again, no loop.
        self.assertIn("recovery-failed", attempt)
        # Native fallback panel still narrates attempts.
        self.assertIn("_failureDetail", PROGRAM)
        # The old splash-rebuild retry is gone — the same surface owns the flow.
        self.assertNotIn("_splash = new SplashForm(_appDir, _progress);", PROGRAM)

    def test_prototype_package_reference_present(self):
        pkg = ROOT / "prototypes" / "core_unlock_splash"
        self.assertTrue((pkg / "sequence_manifest.json").is_file())
        self.assertTrue((pkg / "tools" / "video_export" / "verify_package.py").is_file())
        self.assertTrue(
            (ROOT / "docs" / "SPLASH_SEQUENCES_DEVIN_HANDOFF.md").is_file())


class StartupCaptionTimelineTests(unittest.TestCase):
    """The startup clip is pure cinematic background — it plays forward at
    natural speed and loops the authored online tail. All on-screen truth
    (captions, progress bar, ONLINE state) is DOM-driven from real
    StartupProgress; startup_caption_timeline.json supplies the authored
    caption boundaries (for transition logging), the online gate, and the
    tail loop bounds.
    """

    SPLASH = ROOT / "desktop" / "ChatNexus.Desktop" / "splash"
    PAGE = SPLASH / "web" / "splash.mjs"

    @classmethod
    def setUpClass(cls):
        cls.src = cls.PAGE.read_text(encoding="utf-8")
        cls.timeline = json.loads(
            (cls.SPLASH / "startup_caption_timeline.json").read_text(encoding="utf-8"))
        cls.style = (cls.SPLASH / "web" / "style.css").read_text(encoding="utf-8")

    def test_timeline_defines_approved_ladder(self):
        tl = self.timeline
        self.assertEqual(tl["duration"], 30)
        captions = {c["id"]: c for c in tl["captions"]}
        gates = [c["progressGate"] for c in tl["captions"]]
        self.assertEqual(gates, sorted(gates))  # authored order is gate order
        for cid, gate, primary in (
            ("init", 0.00, "INITIALIZING · NEXUS CORE"),
            ("control", 0.06, "CORE CONTROL · ESTABLISHED"),
            ("services", 0.15, "STARTING · CORE SERVICES"),
            ("integrity", 0.30, "VERIFYING · CORE INTEGRITY"),
            ("brain", 0.55, "SYNCHRONIZING · NEXUS BRAIN"),
            ("command", 0.72, "OPENING · COMMAND INTERFACE"),
            ("workspace", 0.85, "LOADING · NEXUS WORKSPACE"),
            ("interface", 0.93, "SYNCHRONIZING · CORE INTERFACE"),
            ("finalizing", 0.98, "FINALIZING · NEXUS CORE"),
            ("online", 1.00, "CORE SYSTEMS · ONLINE"),
        ):
            c = captions[cid]
            self.assertEqual(c["progressGate"], gate, cid)
            self.assertEqual(c["primary"], primary, cid)
        self.assertEqual(captions["online"]["secondary"], "Nexus Core ready")
        # Online tail loop bounds + pulse period are authored in the file.
        tail = tl["onlineTail"]
        self.assertLess(captions["online"]["at"], tail["loopStart"])
        self.assertLess(tail["loopStart"], tail["loopEnd"])
        self.assertLessEqual(tail["loopEnd"], tl["duration"])
        self.assertGreater(tail["pulsePeriod"], 0)
        # The finalizing hold frame sits inside the finalizing caption.
        self.assertGreater(tl["finalizingHold"]["at"], captions["finalizing"]["at"])
        self.assertLess(tl["finalizingHold"]["at"], captions["online"]["at"])

    def test_page_consumes_timeline_not_hardcoded_times(self):
        src = self.src
        self.assertIn("startup_caption_timeline.json", src)
        # Caption boundaries come from the timeline for transition logging,
        # the online gate/boundary, and the tail loop bounds.
        self.assertIn("clipGates", src)
        self.assertRegex(src, r"clipGates\.find\(c => c\.id === 'online'\)")
        self.assertIn("onlineTail", src)
        for literal in ("13.8", "24.4"):
            self.assertNotIn(literal, src, f"hardcoded seam time {literal}")

    def test_clip_plays_forward_never_rewinds(self):
        src = self.src
        # The story plays at natural speed; play() is retried if a stall
        # leaves the element paused, and the rate is only ever reset to 1.
        self.assertIn("cur.paused && !cur.ended) void cur.play()", src)
        self.assertIn("cur.playbackRate = 1", src)
        # No footage is ever rewound, seeked backward, rate-shifted, or
        # crossfaded mid-story — looping past events is what replayed the
        # iris/unlock during milestone stalls.
        self.assertNotIn("holdSwap", src)
        self.assertNotIn("holdLoop", src)
        self.assertNotIn("progressToAuthoredTime", src)
        self.assertNotIn("clipTargetFor", src)
        self.assertNotIn("MAX_CATCHUP", src)
        self.assertNotIn("const drift", src)
        # The visible boot clip's position is never seeked — the only
        # seeks are the tail-loop handoff onto the hidden idle copy and
        # parking the just-hidden copy back on the loop frame for the
        # next pass (plus the fault/recovery clip resets, which are
        # separate elements).
        self.assertEqual(len(re.findall(r"nxt\.currentTime\s*=", src)), 1)
        self.assertEqual(len(re.findall(r"cur\.currentTime\s*=", src)), 1)
        self.assertNotIn("bootvid.currentTime =", src)

    def test_tail_loops_until_dismissal(self):
        src = self.src
        # End of footage hands off to the idle copy looping the authored
        # online tail — the only loop, and only past the unique events.
        self.assertIn("startBootSwap", src)
        self.assertIn("bootLoopStart", src)
        self.assertIn("bootLoopEnd", src)
        self.assertIn("nxt.currentTime = loopStart", src)

    def test_online_boundary_requires_true_completion(self):
        src = self.src
        # sequence-complete fires only when displayed progress reached the
        # online gate AND the clip physically crossed its authored boundary.
        self.assertIn("externalProgress?.value ?? 0) >= onlineGate", src)
        self.assertIn("currentTime ?? 0) >= onlineAt", src)
        self.assertIn("'sequence-complete'", src)
        # Green pulse keys off the canonical online caption text — which the
        # host only sends after real completion is confirmed.
        self.assertIn("externalProgress.primary === 'CORE SYSTEMS · ONLINE'", src)
        for cls in ("#status.online", "#detail.online"):
            self.assertIn(cls, self.style)
        self.assertIn("#59ffa0", self.style)
        self.assertIn("#3dd77f", self.style)

    def test_reduced_motion_keeps_green_without_pulse(self):
        reduced = re.search(
            r"@media \(prefers-reduced-motion:reduce\)\{[^}]*\}", self.style)
        self.assertIsNotNone(reduced)
        block = reduced.group(0)
        self.assertIn("#status.online", block)
        self.assertIn("#detail.online", block)
        self.assertIn("animation:none", block)

    def test_finalizing_ceiling_and_confirm_gate(self):
        # Bar parks just under 100% while the cinematic converges.
        self.assertIn("0.996", PROGRESS)
        self.assertIn("AwaitingSequence", PROGRESS)
        # ONLINE text requires the surface to confirm the boundary crossed.
        self.assertIn("_sequenceConfirmed", PROGRESS)
        self.assertIn("ConfirmSequence", PROGRESS)
        self.assertIn("ConfirmSequence", PROGRAM)
        self.assertIn('"sequence-complete"', PROGRAM)

    def test_transition_logging_is_milestone_level(self):
        self.assertIn("'caption-view'", self.src)
        self.assertIn("startup caption shown", PROGRAM)
        self.assertIn("LogMilestone", PROGRESS)

    def test_host_reports_ladder_keys_at_anchors(self):
        for fraction, key in (
            ("0.06", "desktop_init"), ("0.15", "backend_launch"),
            ("0.30", "backend_health"), ("0.55", "runtime_sync"),
            ("0.72", "webview_init"), ("0.85", "interface_nav"),
            ("0.93", "interface_ready"),
        ):
            self.assertIn(f'Report({fraction}, "{key}")', PROGRAM,
                          f"{fraction} → {key}")

    def test_fault_still_interrupts_normal_path(self):
        # Fault surfaces clear stale online styling and own the captions.
        self.assertIn("classList.remove('online')", self.src)
        self.assertIn("'trigger-fault'", self.src)
        self.assertIn("activeFault", self.src)


if __name__ == "__main__":
    unittest.main()
