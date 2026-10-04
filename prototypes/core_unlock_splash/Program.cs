using System.Diagnostics;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace NexusCore.SplashPreview;

internal static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new PreviewForm(args));
    }
}

// Independent process. It does not reference, launch or modify the production host/backend.
internal sealed class PreviewForm : Form
{
    private readonly string[] _args;
    private readonly PictureBox _static = new() { Dock = DockStyle.Fill, SizeMode = PictureBoxSizeMode.Zoom, BackColor = Color.FromArgb(4, 8, 18) };
    private readonly Label _status = new() { Dock = DockStyle.Bottom, Height = 40, TextAlign = ContentAlignment.MiddleCenter, ForeColor = Color.LightSteelBlue, BackColor = Color.FromArgb(4, 8, 18), Text = "NEXUS CORE · Preparing standalone preview" };
    private WebView2? _web;
    private bool _ready, _fallback, _closing, _startupFault;
    private string _faultMessage = "Core initialization could not complete.";
    private Panel? _recovery;
    private bool _recoveryShown;
    private int _faultEpoch;
    private readonly TaskCompletionSource<JsonElement> _report = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly string? _verificationPath;
    private readonly Stopwatch _startup = Stopwatch.StartNew();

    public PreviewForm(string[] args)
    {
        _args = args;
        var verifyIndex = Array.IndexOf(args, "--verify");
        if (verifyIndex >= 0) _verificationPath = Path.GetFullPath(verifyIndex + 1 < args.Length ? args[verifyIndex + 1] : "verification-local");
        Text = "Nexus Core — Containment Preview";
        BackColor = Color.FromArgb(4, 8, 18);
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(1120, 960); MinimumSize = new Size(900, 740);
        Controls.Add(_static); Controls.Add(_status);
        var imagePath = Path.Combine(AppContext.BaseDirectory, "assets", "nexus-core-splash.png");
        try { _static.Image = Image.FromFile(imagePath); }
        catch { _status.Text = "NEXUS CORE · INTELLIGENCE PROTECTED"; }
        Shown += async (_, _) => await InitializeAsync();
        FormClosing += (_, _) => { _closing = true; };
    }

    private async Task InitializeAsync()
    {
        if (_args.Contains("--static")) { ShowStatic("Static / recovery preview"); return; }
        _ = WatchdogAsync();
        try
        {
            _web = new WebView2 { Dock = DockStyle.Fill, Visible = false, DefaultBackgroundColor = BackColor };
            Controls.Add(_web);
            var profile = Path.Combine(Path.GetTempPath(), "NexusCore-SplashPreview-WebView2");
            var options = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required");
            var env = await CoreWebView2Environment.CreateAsync(null, profile, options);
            if (_closing || _fallback) return;
            await _web.EnsureCoreWebView2Async(env);
            if (_closing || _fallback) return;
            var core = _web.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreDevToolsEnabled = false;
            core.SetVirtualHostNameToFolderMapping("nexus-splash.example", AppContext.BaseDirectory, CoreWebView2HostResourceAccessKind.DenyCors);
            core.NewWindowRequested += (_, e) => e.Handled = true;
            core.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            core.NavigationStarting += (_, e) => { if (!e.Uri.StartsWith("https://nexus-splash.example/", StringComparison.Ordinal)) e.Cancel = true; };
            core.ProcessFailed += (_, e) => ShowStatic($"Renderer unavailable ({e.ProcessFailedKind})");
            core.WebMessageReceived += async (_, e) =>
            {
                try
                {
                    using var message = JsonDocument.Parse(e.WebMessageAsJson);
                    switch (message.RootElement.GetProperty("type").GetString())
                    {
                        case "prototype-ready":
                            if (_fallback || _closing) return;
                            _ready = true; _status.Visible = false; _static.Visible = false;
                            _web.Visible = true; _web.BringToFront();
                            if (_verificationPath is not null) await VerifyAsync();
                            break;
                        case "startup-fault":
                            if (!_startupFault) { _recoveryShown = false; _ = RecoveryWatchdogAsync(++_faultEpoch); }
                            _startupFault = true;
                            _faultMessage = message.RootElement.GetProperty("message").GetString() ?? _faultMessage;
                            break;
                        case "recovery-visible": _recoveryShown = true; break;
                        case "startup-normal": _startupFault = false; _faultEpoch++; break;
                        case "prototype-fatal":
                            _startupFault |= message.RootElement.TryGetProperty("startupFault", out var fault) && fault.GetBoolean();
                            ShowStatic("Animation failed; static recovery remains available"); break;
                        case "recovery-action":
                            // Deliberately no repair, process launch, rollback or production wiring.
                            // These are presentation hooks for Devin, not a recovery worker.
                            break;
                        case "verification-result": _report.TrySetResult(message.RootElement.GetProperty("report").Clone()); break;
                    }
                }
                catch (Exception ex) { ShowStatic($"Preview error: {ex.Message}"); }
            };
            var query = new List<string>();
            if (_args.Contains("--silent")) query.Add("silent=1");
            if (_args.Contains("--reduced-motion")) query.Add("reduced=1");
            if (_args.Contains("--autoplay")) query.Add("autoplay=1");
            if (_args.Contains("--safe-mode")) query.Add("safe-mode=1");
            core.Navigate("https://nexus-splash.example/web/index.html?" + string.Join('&', query));
        }
        catch (Exception ex) { ShowStatic($"Animation unavailable: {ex.GetType().Name}"); }
    }

    private async Task WatchdogAsync()
    {
        await Task.Delay(10000);
        if (!_ready && !_closing) ShowStatic("Animation timed out; static artwork remains available");
    }

    private async Task RecoveryWatchdogAsync(int epoch)
    {
        await Task.Delay(6000);
        if (_startupFault && !_recoveryShown && epoch == _faultEpoch && !_closing && !_fallback)
            ShowStatic("Containment presentation timed out; recovery is available");
    }

    private void ShowStatic(string reason)
    {
        if (_closing || _fallback) return;
        _fallback = true;
        // Disposing the renderer also stops any audio. Failure never exits a production host.
        _web?.Dispose(); _web = null;
        _static.Visible = true; _status.Visible = true;
        _static.BringToFront(); _status.Text = $"NEXUS CORE · {reason}"; _status.BringToFront();
        if (_startupFault || _args.Contains("--safe-mode")) ShowStaticRecovery();
        if (_verificationPath is not null)
        {
            Directory.CreateDirectory(_verificationPath);
            File.WriteAllText(Path.Combine(_verificationPath, "fallback.json"), JsonSerializer.Serialize(new { staticFallback = true, recoveryUi = _recovery?.Visible == true, reason }));
            Environment.ExitCode = _args.Contains("--static") ? 0 : 1;
            BeginInvoke(Close);
        }
    }

    private void ShowStaticRecovery()
    {
        // Native controls survive a failed Canvas, script or WebView2 process.
        // No animation, timer, or Web Audio is required to reach recovery actions.
        _recovery = new Panel { Width = 390, Height = 280, BackColor = Color.FromArgb(12, 22, 35), BorderStyle = BorderStyle.FixedSingle };
        var title = new Label { Text = "NEXUS CORE · STARTUP RECOVERY", ForeColor = Color.Wheat, AutoSize = false, Bounds = new Rectangle(22, 20, 345, 30) };
        var detail = new Label { Text = _faultMessage + "\n\nStatic recovery is available. The cinematic renderer is not required.", ForeColor = Color.LightSteelBlue, Bounds = new Rectangle(22, 62, 345, 90) };
        var feedback = new Label { Text = "Preview only · recovery actions are not connected.", ForeColor = Color.SlateGray, Bounds = new Rectangle(22, 233, 345, 32) };
        _recovery.Controls.AddRange([title, detail, feedback]);
        string[] actions = ["Details", "Open Log", "Retry", "Rollback", "Safe Mode", "Exit"];
        for (var i = 0; i < actions.Length; i++)
        {
            var action = actions[i];
            var button = new Button { Text = action, Bounds = new Rectangle(22 + i % 3 * 117, 157 + i / 3 * 35, 109, 29), FlatStyle = FlatStyle.Flat, ForeColor = Color.LightSteelBlue };
            button.Click += (_, _) => { if (action == "Exit") Close(); else feedback.Text = $"Preview request: {action}. Connect during integration."; };
            _recovery.Controls.Add(button);
        }
        Controls.Add(_recovery);
        void PositionPanel() { if (_recovery is not null) _recovery.Location = new Point(Math.Max(0, (ClientSize.Width - _recovery.Width) / 2), Math.Max(0, (ClientSize.Height - _recovery.Height) / 2)); }
        Resize += (_, _) => PositionPanel(); PositionPanel(); _recovery.BringToFront();
    }

    private async Task VerifyAsync()
    {
        var output = _verificationPath!;
        Directory.CreateDirectory(output);
        if (_args.Contains("--verify-renderer-fault"))
        {
            _startupFault = true;
            await _web!.CoreWebView2.ExecuteScriptAsync("window.preview.simulateRendererFailure()");
            return;
        }
        if (_args.Contains("--verify-recovery-timeout"))
        {
            await _web!.CoreWebView2.ExecuteScriptAsync("window.preview.triggerFault(); window.preview.pause()");
            return;
        }
        var initializationMs = _startup.Elapsed.TotalMilliseconds;
        var process = Process.GetCurrentProcess();
        var cpuStart = process.TotalProcessorTime;
        var browserCpuStart = BrowserProcesses().ToDictionary(p => p.Id, p => p.CpuSeconds);
        var watch = Stopwatch.StartNew();
        var failureVerification = _args.Contains("--verify-failure");
        await _web!.CoreWebView2.ExecuteScriptAsync(failureVerification ? "void window.preview.verifyFailurePlayback()" : "void window.preview.verifyPlayback()");
        var report = await _report.Task.WaitAsync(TimeSpan.FromSeconds(40));
        var browserProcesses = BrowserProcesses();
        var native = new { initializationMs, hostCpuSeconds = (process.TotalProcessorTime - cpuStart).TotalSeconds, elapsedSeconds = watch.Elapsed.TotalSeconds, hostWorkingSetMb = process.WorkingSet64 / 1048576.0,
            browserCpuSeconds = browserProcesses.Sum(p => Math.Max(0, p.CpuSeconds - browserCpuStart.GetValueOrDefault(p.Id))),
            browserWorkingSetMb = browserProcesses.Sum(p => p.WorkingSetMb), browserProcesses,
            note = "Working sets may include shared pages. CPU covers the host and WebView2 processes; GPU utilization and simultaneous model loading need target-hardware profiling.", renderer = report };
        File.WriteAllText(Path.Combine(output, "playback-report.json"), JsonSerializer.Serialize(native, new JsonSerializerOptions { WriteIndented = true }));
        MinimumSize = new Size(640, 400);
        ClientSize = new Size(1024, 576);
        foreach (var (label, time, reduced) in new[] { ("01-locked", 0.0, false), ("02-authorized", 2.25, false), ("03-pins", 3.8, false), ("04-opening", 5.15, false), ("05-core", 7.8, false), ("06-online", 12.6, false), ("07-reduced", 12.6, true), ("08-charged-hold", 10.5, false) })
        {
            await _web.CoreWebView2.ExecuteScriptAsync($"window.preview.capture({time.ToString(System.Globalization.CultureInfo.InvariantCulture)}, {reduced.ToString().ToLowerInvariant()})");
            await Task.Delay(100);
            await using var stream = File.Create(Path.Combine(output, label + ".png"));
            await _web.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);
        }
        if (failureVerification)
        {
            foreach (var (label, at, elapsed, state, minimal) in new[] {
                ("09-instability", 9.6, 1.5, "null", false),
                ("10-power-drop", 9.6, 3.18, "null", false),
                ("11-emergency-closure", 9.6, 3.78, "null", false),
                ("12-contained", 9.6, 5.25, "null", false),
                ("13-recovery", 9.6, 5.6, "null", false),
                ("14-rollback", 9.6, 5.6, "'ROLLBACK'", false),
                ("15-safe-mode", 7.8, 0.1, "'SAFE_MODE'", false),
                ("16-repair-attempt", 7.8, 5.6, "'REPAIR_ATTEMPT'", false),
                ("17-half-open-fault", 5.15, .01, "null", false),
                ("18-reduced-fault", 7.8, 5.6, "null", true) })
            {
                await _web.CoreWebView2.ExecuteScriptAsync(FormattableString.Invariant($"window.preview.captureFailure({at}, {elapsed}, {state}, {minimal.ToString().ToLowerInvariant()})"));
                await Task.Delay(100);
                await using var stream = File.Create(Path.Combine(output, label + ".png"));
                await _web.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);
            }
        }
        Environment.ExitCode = report.GetProperty("passed").GetBoolean() ? 0 : 1;
        Close();
    }

    private sealed record ProcessMetric(int Id, string Kind, double CpuSeconds, double WorkingSetMb);
    private List<ProcessMetric> BrowserProcesses()
    {
        var result = new List<ProcessMetric>();
        if (_web?.CoreWebView2 is null) return result;
        foreach (var info in _web.CoreWebView2.Environment.GetProcessInfos())
        {
            try
            {
                using var p = Process.GetProcessById(info.ProcessId);
                result.Add(new(p.Id, info.Kind.ToString(), p.TotalProcessorTime.TotalSeconds, p.WorkingSet64 / 1048576.0));
            }
            catch (Exception) { /* Short-lived utility process may have already exited. */ }
        }
        return result;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { _web?.Dispose(); _static.Image?.Dispose(); }
        base.Dispose(disposing);
    }
}
