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
    private bool _ready, _fallback, _closing;
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
        ClientSize = new Size(1120, 870); MinimumSize = new Size(900, 740);
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
                        case "prototype-fatal": ShowStatic("Animation failed; static artwork remains available"); break;
                        case "verification-result": _report.TrySetResult(message.RootElement.GetProperty("report").Clone()); break;
                    }
                }
                catch (Exception ex) { ShowStatic($"Preview error: {ex.Message}"); }
            };
            var query = new List<string>();
            if (_args.Contains("--silent")) query.Add("silent=1");
            if (_args.Contains("--reduced-motion")) query.Add("reduced=1");
            if (_args.Contains("--autoplay")) query.Add("autoplay=1");
            core.Navigate("https://nexus-splash.example/web/index.html?" + string.Join('&', query));
        }
        catch (Exception ex) { ShowStatic($"Animation unavailable: {ex.GetType().Name}"); }
    }

    private async Task WatchdogAsync()
    {
        await Task.Delay(10000);
        if (!_ready && !_closing) ShowStatic("Animation timed out; static artwork remains available");
    }

    private void ShowStatic(string reason)
    {
        if (_closing || _fallback) return;
        _fallback = true;
        // Disposing the renderer also stops any audio. Failure never exits a production host.
        _web?.Dispose(); _web = null;
        _static.Visible = true; _status.Visible = true;
        _static.BringToFront(); _status.Text = $"NEXUS CORE · {reason}"; _status.BringToFront();
        if (_verificationPath is not null)
        {
            Directory.CreateDirectory(_verificationPath);
            File.WriteAllText(Path.Combine(_verificationPath, "fallback.json"), JsonSerializer.Serialize(new { staticFallback = true, reason }));
            Environment.ExitCode = _args.Contains("--static") ? 0 : 1;
            BeginInvoke(Close);
        }
    }

    private async Task VerifyAsync()
    {
        var output = _verificationPath!;
        Directory.CreateDirectory(output);
        var initializationMs = _startup.Elapsed.TotalMilliseconds;
        var process = Process.GetCurrentProcess();
        var cpuStart = process.TotalProcessorTime;
        var browserCpuStart = BrowserProcesses().ToDictionary(p => p.Id, p => p.CpuSeconds);
        var watch = Stopwatch.StartNew();
        await _web!.CoreWebView2.ExecuteScriptAsync("void window.preview.verifyPlayback()");
        var report = await _report.Task.WaitAsync(TimeSpan.FromSeconds(30));
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
