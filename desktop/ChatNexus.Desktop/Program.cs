using System.Diagnostics;
using System.Drawing.Drawing2D;
using System.Net;
using System.Security.Cryptography;
using System.Text.Json;
using System.Net.Http;
using System.Net.Sockets;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace ChatNexus.Desktop;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        var appDir = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        var selfTest = args.Any(a => string.Equals(a, "--self-test", StringComparison.OrdinalIgnoreCase));
        var testFault = args.Any(a => string.Equals(a, "--test-fault", StringComparison.OrdinalIgnoreCase));

        // First-breath marker — if the host ever dies before the backend
        // launch path (splash/WebView2 init), this is the line that tells us
        // the process at least reached managed code.
        string? earlyLogDir = null;
        try
        {
            earlyLogDir = Path.Combine(appDir, "data", "logs");
            Directory.CreateDirectory(earlyLogDir);
            BackendProcess.NoteStartup(earlyLogDir, $"host process started (pid {Environment.ProcessId})");
        }
        catch { }

        // Single instance — a second host's orphan sweep would kill the
        // running backend by exe-path match, and two backends would fight
        // over backend.pid, state dirs, and the GPU anyway.
        using var singleInstance = new Mutex(true, @"Local\NexusCore.Desktop.Host", out var createdNew);
        if (!createdNew)
        {
            try
            {
                if (earlyLogDir != null)
                {
                    BackendProcess.NoteStartup(
                        earlyLogDir,
                        $"second instance exited (pid {Environment.ProcessId}) — host already running");
                }
            }
            catch { }
            if (!selfTest)
            {
                MessageBox.Show(
                    "Nexus Core is already running.",
                    "Nexus Core",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information);
            }
            return 0;
        }

        try
        {
            if (selfTest)
            {
                using var backend = BackendProcess.Start(appDir);
                backend.WaitUntilHealthyAsync(TimeSpan.FromSeconds(120)).GetAwaiter().GetResult();
                backend.ProbeUiAsync().GetAwaiter().GetResult();
                return 0;
            }

            ApplicationConfiguration.Initialize();
            Application.Run(new NexusCoreApplicationContext(appDir, testFault));
            return 0;
        }
        catch (Exception ex)
        {
            if (selfTest)
            {
                Console.Error.WriteLine(ex);
            }
            else
            {
                MessageBox.Show(
                    ex.ToString(),
                    "Nexus Core startup error",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
            return 1;
        }
    }
}

/// <summary>
/// Borderless splash rendered from the official Nexus Core artwork with a
/// real progress bar and status line driven by StartupProgress. Fatal
/// startup failure swaps to an actionable failure state (Retry/Open Log/Exit)
/// instead of dying silently.
/// </summary>
internal sealed class SplashForm : Form
{
    private readonly StartupProgress _progress;
    private readonly System.Windows.Forms.Timer _timer = new();
    private readonly System.Windows.Forms.Timer _topmostTimer = new();
    private readonly Image? _artwork;
    private readonly Panel _cover;
    private readonly string _appDir;

    private Panel? _failurePanel;
    private string? _failureMessage;
    public event Action? RetryRequested;
    public event Action? ExitRequested;
    /// <summary>Playback channel for narration bytes — web audio with ducking.</summary>
    public Func<byte[], string, Task>? VoiceSink { get; set; }

    // Cinematic layer — WebView2 hosting splash/web/index.html. Pure
    // presentation: the static WinForms render below stays the fallback and
    // readiness authority lives in StartupProgress/RunStartupAsync.
    private WebView2? _web;
    private volatile bool _webReady;
    /// <summary>id, started, seconds — the splash's real playback-start ack.</summary>
    public event Action<string, bool, double>? VoicePlaybackResult;
    /// <summary>id — the splash reports audio actually finished playing.</summary>
    public event Action<string>? VoicePlaybackEnded;
    private volatile bool _webFailed;
    private volatile bool _sequenceComplete;
    private int _lastGateIdx = -1;

    /// <summary>The cinematic posted its online frame — the sequence ran to completion.</summary>
    public bool SequenceComplete => _sequenceComplete;
    /// <summary>Cinematic is live and fault-free — its completion is worth waiting for.</summary>
    public bool CinematicActive => _webReady && !_webFailed;

    // Manifest gates released at real startup milestones (StartupProgress
    // ladder anchors) — the timeline can never outrun reality.
    private static readonly (string Gate, double At)[] GateMap =
    {
        ("services", 0.15), ("authorization", 0.30), ("unlock", 0.55),
        ("open", 0.72), ("charge", 0.85), ("ready", 0.93),
    };

    public SplashForm(string appDir, StartupProgress progress)
    {
        _appDir = appDir;
        _progress = progress;

        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        ShowInTaskbar = true;
        TopMost = true;
        BackColor = Color.FromArgb(4, 8, 18);
        DoubleBuffered = true;
        ClientSize = new Size(1024, 576);
        Text = "Nexus Core";

        var iconPath = Path.Combine(appDir, "nexus-core.ico");
        if (File.Exists(iconPath))
        {
            Icon = new Icon(iconPath);
        }

        var splashPath = Path.Combine(appDir, "nexus-core-splash.png");
        if (File.Exists(splashPath))
        {
            _artwork = Image.FromFile(splashPath);
        }

        // Cover panel: paints the identical static surface, shown on top of
        // the cinematic during its first ~200ms so the WebView2's late first
        // frames never read as a black flash over the artwork.
        _cover = new Panel
        {
            Dock = DockStyle.Fill,
            Visible = false,
            BackColor = Color.FromArgb(4, 9, 19),
        };
        _cover.Paint += (_, pe) => PaintSurface(pe.Graphics, _cover.ClientSize);
        Controls.Add(_cover);

        _timer.Interval = 33;
        _timer.Tick += (_, _) =>
        {
            _progress.Tick();
            PumpCinematic();
            if (!_webReady) Invalidate();
        };
        _timer.Start();

        // The splash must ride the topmost band for its whole life — boot
        // runs long enough that the user WILL click elsewhere. Windows can
        // silently demote a window shown without foreground rights, so the
        // property alone isn't sufficient: a slow watchdog re-asserts
        // HWND_TOPMOST without ever stealing activation.
        _topmostTimer.Interval = 500;
        _topmostTimer.Tick += (_, _) =>
        {
            if (IsHandleCreated)
            {
                Win32.SetWindowPos(Handle, Win32.HwndTopmost,
                    0, 0, 0, 0,
                    Win32.SwpNomove | Win32.SwpNosize | Win32.SwpNoactivate);
            }
        };
        _topmostTimer.Start();

        _ = InitCinematicAsync();
    }

    /// <summary>
    /// Will the backend actually build a provisioning plan this launch?
    /// Mirrors server-side rules: packaged installs honor
    /// provisioning_enabled (default on); source checkouts stay quiet unless
    /// provisioning_dev_enable is explicitly set. Drives the workstation
    /// status text — never a reason to start downloads early.
    /// </summary>
    internal static bool ProvisioningPlanned(string appDir)
    {
        var frozen = File.Exists(Path.Combine(appDir, "backend", "ChatNexus.Backend.exe"));
        return CfgBool(appDir, frozen ? "provisioning_enabled" : "provisioning_dev_enable",
            fallback: frozen);
    }

    private static bool CfgBool(string appDir, string key, bool fallback)
    {
        try
        {
            var p = Path.Combine(appDir, "config.json");
            if (!File.Exists(p)) return fallback;
            using var doc = JsonDocument.Parse(File.ReadAllText(p));
            if (doc.RootElement.TryGetProperty(key, out var v))
            {
                if (v.ValueKind == JsonValueKind.True) return true;
                if (v.ValueKind == JsonValueKind.False) return false;
            }
        }
        catch { }
        return fallback;
    }

    private async Task InitCinematicAsync()
    {
        try
        {
            var splashDir = Path.Combine(_appDir, "splash");
            var indexFile = Path.Combine(splashDir, "web", "index.html");
            var manifestFile = Path.Combine(splashDir, "animation_manifest.json");
            if (!File.Exists(indexFile) || !File.Exists(manifestFile))
            {
                _webFailed = true;
                return;
            }

            // Same autoplay exemption the main WebView2 gets — the
            // cinematic is unattended, so AudioContext must not start
            // suspended waiting for a user gesture that never comes.
            var env = await CoreWebView2Environment.CreateAsync(
                browserExecutableFolder: null,
                userDataFolder: Path.Combine(_appDir, "data", "webview2-splash"),
                options: new CoreWebView2EnvironmentOptions(
                    additionalBrowserArguments: "--autoplay-policy=no-user-gesture-required"));
            if (IsDisposed) return;
            _web = new WebView2
            {
                Dock = DockStyle.Fill,
                Visible = false,
                DefaultBackgroundColor = Color.FromArgb(4, 9, 19), // exact #stage bg
            };
            Controls.Add(_web);
            _web.BringToFront();
            await _web.EnsureCoreWebView2Async(env);
            if (IsDisposed) return;
            var cwv = _web.CoreWebView2!;
            cwv.SetVirtualHostNameToFolderMapping(
                "nexus.splash", splashDir, CoreWebView2HostResourceAccessKind.Allow);
            cwv.WebMessageReceived += OnCinematicMessage;

            var query = new List<string>();
            if (!CfgBool(_appDir, "splash_audio_enabled", true)
                || CfgBool(_appDir, "silent_startup", false)) query.Add("silent");
            if (CfgBool(_appDir, "reduced_motion", false)) query.Add("reduced");
            var url = "https://nexus.splash/web/index.html"
                + (query.Count > 0 ? "?" + string.Join("&", query) : "");
            cwv.Navigate(url);

            VoiceSink = async (bytes, _key) =>
            {
                // Throwing routes the narrator to its SoundPlayer fallback —
                // a line that arrives as the splash tears down should still
                // be heard rather than silently dropped.
                if (_web?.CoreWebView2 is null || !_webReady)
                    throw new InvalidOperationException("splash channel unavailable");
                var b64 = Convert.ToBase64String(bytes);
                _web.CoreWebView2.PostWebMessageAsJson(
                    JsonSerializer.Serialize(new { type = "play-voice", id = _key, b64, duck = 0.35 }));
                BackendProcess.NoteStartup(Path.Combine(_appDir, "data", "logs"),
                    $"splash voice posted {_key} ({bytes.Length}B)");
                await Task.CompletedTask;
            };
        }
        catch
        {
            _webFailed = true;
            try { _web?.Dispose(); } catch { }
            _web = null;
        }
    }

    private void PostToWeb(object message)
    {
        try
        {
            if (_webReady && _web?.CoreWebView2 is { } cwv)
                cwv.PostWebMessageAsJson(JsonSerializer.Serialize(message));
        }
        catch { }
    }

    private void PumpCinematic()
    {
        if (!_webReady || _webFailed) return;
        var milestone = _progress.RealProgress;
        for (var i = _lastGateIdx + 1; i < GateMap.Length; i++)
        {
            if (milestone >= GateMap[i].At)
            {
                _lastGateIdx = i;
                PostToWeb(new { type = "set-gate", id = GateMap[i].Gate, released = true });
            }
            else break;
        }
        PostToWeb(new
        {
            type = "set-progress",
            value = _progress.DisplayedProgress,
            primary = _progress.Primary,
            secondary = _progress.Secondary,
        });
    }

    private void OnCinematicMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            var type = doc.RootElement.GetProperty("type").GetString();
            switch (type)
            {
                case "splash-ready":
                    _webReady = true;
                    BeginInvoke(() =>
                    {
                        if (_web is not null)
                        {
                            _web.Visible = true;
                            // Hide the placeholder's first frames behind an
                            // identical static frame, then reveal the live
                            // cinematic once the compositor is painting.
                            _cover.Visible = true;
                            _cover.BringToFront();
                            _cover.Invalidate();
                            var cover = _cover;
                            _ = Task.Delay(200).ContinueWith(_ =>
                            {
                                try { BeginInvoke(() => cover.Visible = false); }
                                catch { }
                            });
                        }
                        // A fault that fired before the cinematic booted
                        // locked in the static fallback — hand the same
                        // failure to the real containment animation +
                        // recovery UI now that the surface exists.
                        if (_failureMessage is not null && _failurePanel is not null)
                        {
                            _failurePanel.Dispose();
                            _failurePanel = null;
                            PostToWeb(new { type = "trigger-fault", message = _failureMessage });
                            PostToWeb(new { type = "show-recovery" });
                        }
                    });
                    break;
                case "voice-result":
                    // Narration delivery was previously invisible — a
                    // dropped play-voice looked identical to a played one.
                    {
                        var vid = doc.RootElement.GetProperty("id").GetString() ?? "";
                        var started = doc.RootElement.TryGetProperty("started", out var s)
                                      && s.ValueKind == JsonValueKind.True;
                        var secs = doc.RootElement.TryGetProperty("seconds", out var sec)
                                   ? sec.GetDouble() : 0.0;
                        VoicePlaybackResult?.Invoke(vid, started, secs);
                    }
                    break;
                case "voice-ended":
                    VoicePlaybackEnded?.Invoke(
                        doc.RootElement.GetProperty("id").GetString() ?? "");
                    break;
                case "sequence-complete":
                    _sequenceComplete = true;
                    break;
                case "splash-error":
                    _webFailed = true;
                    BeginInvoke(() =>
                    {
                        if (_web is not null) { _web.Visible = false; }
                        _cover.Visible = false;
                        Invalidate();
                    });
                    break;
                case "recovery-action":
                    var action = doc.RootElement.TryGetProperty("action", out var a)
                        ? a.GetString() : null;
                    BeginInvoke(() =>
                    {
                        if (action == "retry") RetryRequested?.Invoke();
                        else if (action == "exit") ExitRequested?.Invoke();
                        else if (action == "rollback") ScheduleRecoveryRollback();
                        else if (action == "safe-mode") EnterSafeModeAndRestart();
                        else if (action == "open-log")
                        {
                            var log = Path.Combine(_appDir, "data", "logs", "backend-host.log");
                            try
                            {
                                Process.Start(new ProcessStartInfo(
                                    File.Exists(log) ? log : "notepad.exe",
                                    File.Exists(log) ? "" : Path.Combine(_appDir, "data", "logs"))
                                { UseShellExecute = true });
                            }
                            catch { }
                        }
                    });
                    break;
            }
        }
        catch { }
    }

    private void RecoveryFeedback(string text) =>
        PostToWeb(new { type = "action-feedback", text });

    /// <summary>The app is ready — tell the cinematic to converge its tail
    /// onto the online state instead of free-running to its fixed duration.</summary>
    public void RequestSequenceFinish() =>
        PostToWeb(new { type = "complete-sequence" });

    /// <summary>
    /// Recovery "Rollback" — writes data/lkg/rollback.flag naming the
    /// newest snapshot (latest.txt first, newest snap-* otherwise) and
    /// restarts so ApplyLkgFlags restores it on the next boot.
    /// </summary>
    private void ScheduleRecoveryRollback()
    {
        try
        {
            var lkg = Path.Combine(_appDir, "data", "lkg");
            Directory.CreateDirectory(lkg);
            var name = "";
            var latestTxt = Path.Combine(lkg, "latest.txt");
            if (File.Exists(latestTxt))
                name = (File.ReadAllText(latestTxt) ?? "").Trim();
            if (name.Length == 0 || !Directory.Exists(Path.Combine(lkg, name)))
            {
                var newest = Directory.GetDirectories(lkg, "snap-*")
                    .OrderByDescending(d => d, StringComparer.Ordinal)
                    .FirstOrDefault();
                name = newest is null ? "" : Path.GetFileName(newest);
            }
            if (name.Length == 0)
            {
                RecoveryFeedback("No rollback snapshot is available.");
                return;
            }
            File.WriteAllText(Path.Combine(lkg, "rollback.flag"),
                JsonSerializer.Serialize(new
                {
                    name,
                    reason = "user requested from splash recovery",
                }));
            PostToWeb(new { type = "recovery-state", state = "ROLLBACK" });
            RecoveryFeedback($"Rolling back to {name} — restarting.");
            _ = Task.Delay(1200).ContinueWith(
                _ => BeginInvoke(new Action(Application.Restart)));
        }
        catch (Exception ex)
        {
            RecoveryFeedback($"Rollback could not be scheduled: {ex.Message}");
        }
    }

    /// <summary>
    /// Recovery "Safe Mode" — sets data/safe_mode.json active (same
    /// schema SafeModeStore writes) and restarts so the backend boots
    /// with heavy startup paths suppressed.
    /// </summary>
    private void EnterSafeModeAndRestart()
    {
        try
        {
            var dataDir = Path.Combine(_appDir, "data");
            Directory.CreateDirectory(dataDir);
            var path = Path.Combine(dataDir, "safe_mode.json");
            var now = (double)DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            var consec = 0;
            JsonElement history = default;
            try
            {
                using var d = JsonDocument.Parse(File.ReadAllText(path));
                if (d.RootElement.TryGetProperty("consecutive_failures", out var c)
                    && c.ValueKind == JsonValueKind.Number)
                    consec = c.GetInt32();
                if (d.RootElement.TryGetProperty("history", out var h)
                    && h.ValueKind == JsonValueKind.Array)
                    history = h.Clone();
            }
            catch { /* missing/corrupt file — write a clean record */ }
            var entries = new List<object>();
            if (history.ValueKind == JsonValueKind.Array)
                foreach (var el in history.EnumerateArray()) entries.Add(el.Clone());
            entries.Add(new Dictionary<string, object?>
            {
                ["event"] = "enter",
                ["reason"] = "user requested from splash recovery",
                ["time"] = now,
            });
            File.WriteAllText(path, JsonSerializer.Serialize(
                new Dictionary<string, object?>
                {
                    ["version"] = 1,
                    ["active"] = true,
                    ["reason"] = "user requested from splash recovery",
                    ["since"] = now,
                    ["consecutive_failures"] = consec,
                    ["history"] = entries.Count > 50
                        ? entries[^50..] : entries,
                }));
            PostToWeb(new { type = "recovery-state", state = "SAFE_MODE" });
            RecoveryFeedback("Safe Mode enabled — restarting with essential systems only.");
            _ = Task.Delay(1200).ContinueWith(
                _ => BeginInvoke(new Action(Application.Restart)));
        }
        catch (Exception ex)
        {
            RecoveryFeedback($"Safe Mode could not be enabled: {ex.Message}");
        }
    }

    public void ShowFailure(string message)
    {
        _timer.Stop();
        _progress.MarkFailed();
        _failureMessage = message;
        if (_webReady && !_webFailed)
        {
            // Cinematic containment + recovery UI own the fault surface;
            // nothing here blocks the real recovery path.
            PostToWeb(new { type = "trigger-fault", message });
            PostToWeb(new { type = "show-recovery" });
            return;
        }
        _failurePanel = new Panel
        {
            Dock = DockStyle.Fill,
            BackColor = Color.FromArgb(10, 14, 26),
        };

        var title = new Label
        {
            Text = "NEXUS CORE COULD NOT START",
            ForeColor = Color.FromArgb(255, 120, 120),
            Font = new Font("Segoe UI Semibold", 20f, FontStyle.Bold),
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleCenter,
            Dock = DockStyle.Top,
            Height = 140,
        };
        var detail = new Label
        {
            Text = message,
            ForeColor = Color.FromArgb(150, 170, 200),
            Font = new Font("Segoe UI", 10f),
            AutoSize = false,
            TextAlign = ContentAlignment.TopCenter,
            Dock = DockStyle.Top,
            Height = 120,
        };
        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            Height = 70,
            FlowDirection = FlowDirection.LeftToRight,
            Padding = new Padding(0, 10, 0, 0),
        };
        buttons.WrapContents = false;

        Button MakeButton(string text)
        {
            var b = new Button
            {
                Text = text,
                Width = 130,
                Height = 36,
                FlatStyle = FlatStyle.Flat,
                ForeColor = Color.FromArgb(220, 235, 255),
                BackColor = Color.FromArgb(22, 34, 54),
                Font = new Font("Segoe UI", 10f),
            };
            b.FlatAppearance.BorderColor = Color.FromArgb(60, 120, 220);
            return b;
        }

        var retry = MakeButton("Retry");
        retry.Click += (_, _) => RetryRequested?.Invoke();
        var openLog = MakeButton("Open Log");
        openLog.Click += (_, _) =>
        {
            var log = Path.Combine(_appDir, "data", "logs", "backend-host.log");
            try
            {
                Process.Start(new ProcessStartInfo(
                    File.Exists(log) ? log : "notepad.exe",
                    File.Exists(log) ? "" : Path.Combine(_appDir, "data", "logs"))
                { UseShellExecute = true });
            }
            catch { }
        };
        var exit = MakeButton("Exit");
        exit.Click += (_, _) => ExitRequested?.Invoke();

        buttons.Controls.Add(retry);
        buttons.Controls.Add(openLog);
        buttons.Controls.Add(exit);
        // Center the button row.
        buttons.PerformLayout();
        buttons.Left = (ClientSize.Width - buttons.PreferredSize.Width) / 2;
        buttons.Dock = DockStyle.None;
        buttons.Top = 300;
        buttons.Anchor = AnchorStyles.None;

        _failurePanel.Controls.Add(buttons);
        _failurePanel.Controls.Add(detail);
        _failurePanel.Controls.Add(title);
        Controls.Add(_failurePanel);
        _failurePanel.BringToFront();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        PaintSurface(e.Graphics, ClientSize);
        base.OnPaint(e);
    }

    /// <summary>
    /// The full static splash surface — artwork, progress track, status
    /// lines. Shared by the form's OnPaint and the cover panel that hides
    /// the cinematic's first composited frames (they arrive ~a frame late
    /// and would otherwise read as a black flash over the artwork).
    /// </summary>
    private void PaintSurface(Graphics g, Size size)
    {
        g.SmoothingMode = SmoothingMode.AntiAlias;
        var client = new Rectangle(Point.Empty, size);

        if (_artwork is not null)
        {
            // Cover-fit the artwork.
            var scale = Math.Max((float)size.Width / _artwork.Width,
                                 (float)size.Height / _artwork.Height);
            var w = _artwork.Width * scale;
            var h = _artwork.Height * scale;
            g.DrawImage(_artwork, (size.Width - w) / 2, (size.Height - h) / 2, w, h);
        }
        else
        {
            using var bg = new LinearGradientBrush(client,
                Color.FromArgb(4, 8, 18), Color.FromArgb(10, 20, 44), 90f);
            g.FillRectangle(bg, client);
            using var font = new Font("Segoe UI", 30f, FontStyle.Bold);
            TextRenderer.DrawText(g, "NEXUS CORE", font, client,
                Color.FromArgb(120, 200, 255),
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
        }

        // Progress bar — covers the artwork's own baked-in bar (x ~390–660,
        // y ~502–509 on the 1024×576 source). The artwork ships with a static
        // half-lit grey fill, so this track is fully opaque and slightly
        // oversized: the baked bar disappears entirely and only the live
        // gradient fill reads as the progress indicator.
        var barWidth = (int)(size.Width * 0.284);
        var barHeight = 7;
        var barX = (int)(size.Width * 0.372);
        var barY = (int)(size.Height * 0.873);
        var track = new Rectangle(barX, barY - 1, barWidth, barHeight + 2);
        using (var trackBrush = new SolidBrush(Color.FromArgb(255, 6, 12, 26)))
        {
            g.FillRectangle(trackBrush, track);
        }
        using (var edge = new Pen(Color.FromArgb(80, 60, 110, 160)))
        {
            g.DrawRectangle(edge, track);
        }
        var fillWidth = (int)(barWidth * Math.Clamp(_progress.DisplayedProgress, 0.0, 1.0));
        if (fillWidth > 0)
        {
            var fill = new Rectangle(barX, barY, fillWidth, barHeight);
            using var fillBrush = new LinearGradientBrush(fill,
                Color.FromArgb(0, 160, 255), Color.FromArgb(140, 80, 255), 0f);
            g.FillRectangle(fillBrush, fill);
            using var glow = new SolidBrush(Color.FromArgb(60, 80, 180, 255));
            g.FillRectangle(glow, new Rectangle(barX, barY - 2, fillWidth, barHeight + 4));
        }

        // Brief brightening of the shield's central core on completion —
        // a soft radial bloom over the artwork's core position (~512,195
        // on the 1024×576 source, cover-fitted to the client area).
        var completion = _progress.CompletionPhase;
        if (completion > 0)
        {
            var coreX = size.Width * 0.5f;
            var coreY = size.Height * 0.34f;
            var radius = 130f * (0.6f + 0.4f * (float)Math.Sin(completion * Math.PI));
            var alpha = (int)(150 * Math.Sin(completion * Math.PI));
            using var glowPath = new GraphicsPath();
            glowPath.AddEllipse(coreX - radius, coreY - radius, radius * 2, radius * 2);
            using var glow = new PathGradientBrush(glowPath)
            {
                CenterColor = Color.FromArgb(alpha, 120, 200, 255),
                SurroundColors = new[] { Color.FromArgb(0, 120, 200, 255) },
            };
            g.FillEllipse(glow, coreX - radius, coreY - radius, radius * 2, radius * 2);
        }

        // Two-line status under the progress bar. The artwork's baked-in
        // caption sits at ~0.91·H, so both lines live along the bottom edge.
        using var primaryFont = new Font("Segoe UI", 10f, FontStyle.Bold);
        using var secondaryFont = new Font("Segoe UI", 8.5f);
        var isReady = _progress.ReadyToDismiss
            || string.Equals(_progress.Primary, "CORE SYSTEMS · ONLINE", StringComparison.Ordinal);
        var primaryRect = new Rectangle(0, size.Height - 46, size.Width, 20);
        // Ready state: green + pulsating — the timer already repaints at
        // 33ms cadence, so a clock-driven alpha sine gives the same pulse
        // the cinematic's #status.online keyframes produce.
        var pulse = (float)(0.5 + 0.5 * Math.Sin(DateTime.UtcNow.TimeOfDay.TotalSeconds * Math.PI * 2 / 1.6));
        var readyColor = Color.FromArgb(70 + (int)(185 * pulse), 90, 255, 160);
        TextRenderer.DrawText(g, _progress.Primary, primaryFont, primaryRect,
            isReady ? readyColor : Color.FromArgb(90, 215, 255),
            TextFormatFlags.HorizontalCenter);
        var secondaryRect = new Rectangle(0, size.Height - 27, size.Width, 18);
        TextRenderer.DrawText(g, _progress.Secondary, secondaryFont, secondaryRect,
            isReady ? Color.FromArgb(61, 215, 127) : Color.FromArgb(150, 170, 200),
            TextFormatFlags.HorizontalCenter);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _timer.Dispose();
            _artwork?.Dispose();
            try { _web?.Dispose(); } catch { }
        }
        base.Dispose(disposing);
    }
}

/// <summary>
/// Application context that owns the startup lifecycle: splash first, hidden
/// main form prepares behind it, then a seamless swap once both the real
/// readiness handshake and the minimum display time have elapsed.
/// </summary>
internal sealed class NexusCoreApplicationContext : ApplicationContext
{
    private readonly string _appDir;
    private StartupProgress _progress;
    private SplashForm? _splash;
    private MainForm? _main;
    private StartupNarrator? _narrator;

    /// <summary>Backend URL once launched — narration needs the voice API.</summary>
    private string BackendUrl => _main?.BackendUrl ?? "http://127.0.0.1:8765/";

    private bool _testFault;

    public NexusCoreApplicationContext(string appDir, bool testFault = false)
    {
        _appDir = appDir;
        _testFault = testFault;
        _progress = CreateProgress();
        _splash = new SplashForm(appDir, _progress);
        _splash.RetryRequested += OnRetry;
        _splash.ExitRequested += () => Application.Exit();
        // Throwing (instead of ?? Task.CompletedTask) routes the narrator
        // to its SoundPlayer fallback when the splash channel isn't wired
        // yet — a completed no-op task silently swallowed early lines.
        _narrator = new StartupNarrator(appDir,
            playBytes: (bytes, key) => _splash?.VoiceSink?.Invoke(bytes, key)
                ?? throw new InvalidOperationException("voice channel unavailable"),
            log: line => BackendProcess.NoteStartup(
                Path.Combine(appDir, "data", "logs"), line));
        // The splash reports real playback lifecycle back — the narrator's
        // 2-second transition gate is keyed to audio actually ending, not
        // to the moment play-voice was posted.
        _splash.VoicePlaybackResult += (id, started, secs) =>
            _narrator.NotifyVoiceResult(id, started, secs);
        _splash.VoicePlaybackEnded += id => _narrator.NotifyVoiceEnded(id);
        _splash.Show();
        // Launched from a shell/IDE we may not hold foreground rights —
        // TopMost keeps the splash above other windows, but it still needs
        // an explicit Activate or it opens behind the focused app.
        _splash.Activate();
        // Narration starts alongside startup — never blocks it. First
        // launch speaks the welcome; later launches the short line.
        _narrator.StartupBegan(() => _main?.BackendUrl);
        _ = RunStartupAsync();
    }

    /// <summary>
    /// Fresh progress model wired to this install's data dirs — the timing
    /// profile at data/startup_profile.json paces the predictive bar and is
    /// rewritten at completion. Never gates readiness; advisory only.
    /// </summary>
    private StartupProgress CreateProgress()
    {
        var dataDir = Path.Combine(_appDir, "data");
        var profile = StartupProfile.Load(Path.Combine(dataDir, "startup_profile.json"));
        return new StartupProgress(profile, Path.Combine(dataDir, "logs"));
    }

    private void OnRetry()
    {
        _splash?.Dispose();
        _main?.DisposeBackend();
        _main?.Dispose();
        _main = null;
        _progress = CreateProgress();
        _splash = new SplashForm(_appDir, _progress);
        _splash.RetryRequested += OnRetry;
        _splash.ExitRequested += () => Application.Exit();
        if (_narrator is not null)
        {
            _splash.VoicePlaybackResult += (id, started, secs) =>
                _narrator.NotifyVoiceResult(id, started, secs);
            _splash.VoicePlaybackEnded += id => _narrator.NotifyVoiceEnded(id);
        }
        _splash.Show();
        _splash.Activate();
        _ = RunStartupAsync();
    }

    private async Task RunStartupAsync()
    {
        try
        {
            _progress.Report(0.06, "init");

            // --test-fault: one-shot fault for recovery dogfooding — fires
            // once the cinematic/recovery surface has had time to boot, so
            // the REAL ShowFailure path (fault narration, containment
            // animation, recovery panel, host actions) is exercised end to
            // end. Retry relaunches startup with the flag already spent.
            if (_testFault)
            {
                _testFault = false;
                await Task.Delay(4000);
                throw new InvalidOperationException("test fault — recovery dogfood");
            }

            // Build the real main window now, still invisible.
            _main = new MainForm(_appDir);
            _main.CreateControl(); // handle exists without showing the window
            // Shutdown narration: "Shutting down the core." + the persona
            // farewell — the window holds until playback actually ends.
            _main.FarewellHook = () =>
                _narrator?.FarewellAsync(
                    () => _main?.BackendUrl, TimeSpan.FromSeconds(45))
                ?? Task.CompletedTask;

            _progress.Report(0.15, "services");
            await _main.PrepareAsync(_progress);

            // The interface posted its ready handshake; the app is genuinely
            // usable. Now hold the splash until the minimum display time too,
            // then play the brief READY + core-glow completion effect before
            // handing off — still no blank intermediate state.
            _progress.MarkAppReady();

            // The app is genuinely ready — now converge the cinematic's
            // remaining tail onto its online state instead of letting it
            // free-run behind. While it converges the bar parks just under
            // 100% and the status stays "FINALIZING" so the text never
            // claims online ahead of the visual. Bounded so a dead WebView
            // can never hang startup.
            var splash = _splash;
            if (splash is not null && splash.CinematicActive && !splash.SequenceComplete)
            {
                _progress.AwaitingSequence = true;
                splash.RequestSequenceFinish();
                var seqDeadline = DateTimeOffset.Now + TimeSpan.FromSeconds(20);
                while (!splash.SequenceComplete && DateTimeOffset.Now < seqDeadline)
                {
                    await Task.Delay(50);
                }
                _progress.AwaitingSequence = false;
            }

            // "Core systems online." lands on the visual online moment —
            // truthful and synchronized instead of early.
            _narrator?.NearlyReady(() => _main?.BackendUrl);
            while (!_progress.ReadyToDismiss)
            {
                await Task.Delay(60);
            }
            _progress.BeginCompletion();
            while (!_progress.CompletionFinished)
            {
                await Task.Delay(33);
            }

            // Dwell on the fully-loaded state before the swap — a few
            // seconds at stable online so the completion actually reads.
            await Task.Delay(TimeSpan.FromSeconds(2));

            // Voice gate: the splash stays up until the last startup
            // narration has ACTUALLY finished playing (voice-ended ack, not
            // message-posted) plus a 2-second quiet buffer — then the main
            // app may greet. Bounded so a lost ack can't hang startup.
            if (_narrator is not null)
            {
                await _narrator.VoiceGateAsync(TimeSpan.FromSeconds(45));
            }
            _narrator?.Cancel();
            _splash?.Close();
            _splash?.Dispose();
            _splash = null;
            // Without foreground rights Show() can leave the window behind
            // (looks minimized). Attach to the foreground thread's input
            // queue so SetForegroundWindow is honored, ride it to the top
            // via a brief TopMost hold, then release so the app isn't
            // pinned above other windows.
            _main.WindowState = FormWindowState.Normal;
            _main.Show();
            _main.TopMost = true;
            _main.Activate();
            _main.BringToFront();
            Win32.ForceForeground(_main.Handle);
            _main.TopMost = false;
            // Release the held startup greeting — the splash owned the
            // audio stage until narration + quiet buffer finished.
            _main.SignalStartupTransition();
            MainForm = _main;
        }
        catch (Exception ex)
        {
            // Fault narration supersedes any friendly line immediately —
            // then recovery diagnostics are already running. The exception
            // itself MUST reach the host log — a silent catch leaves a
            // windowless, unexplained failure.
            BackendProcess.NoteStartup(
                Path.Combine(_appDir, "data", "logs"),
                $"startup failed: {ex.GetType().Name}: {ex.Message}");
            _narrator?.Fault(() => _main?.BackendUrl);
            _splash?.ShowFailure(
                $"{ex.Message}\n\nDetails are in data\\logs\\backend-host.log");
        }
    }

    protected override void ExitThreadCore()
    {
        _splash?.Dispose();
        _main?.DisposeBackend();
        base.ExitThreadCore();
    }
}

internal sealed class BackendProcess : IDisposable
{
    private readonly Process _process;
    private TextWriter _logWriter;
    private readonly object _logLock = new();
    private int _logWrites;
    // Unattended installs run for weeks — bound the host log the same
    // way the backend bounds its audit stream (trim to a tail, keep the
    // newest evidence, never grow without limit).
    private const long LogMaxBytes = 8L * 1024 * 1024;
    private const long LogKeepBytes = 4L * 1024 * 1024;
    private readonly int _requestedPort;
    private volatile int _announcedPort;
    private volatile bool _disposing;

    /// <summary>
    /// The port the backend actually bound. Normally the requested port;
    /// if the backend reported a fallback via its "[nexus-port]" stdout
    /// marker, that port wins so health checks never hit a foreign process
    /// squatting on the requested port.
    /// </summary>
    public int Port => _announcedPort > 0 ? _announcedPort : _requestedPort;
    public string BaseUrl => $"http://127.0.0.1:{Port}/";
    public string LogPath { get; }
    public event Action<int>? UnexpectedExit;
    /// <summary>(pct 0-100, primary, secondary) — real backend-internal phase.</summary>
    public event Action<double, string, string>? BootPhase;

    private BackendProcess(Process process, int port, string logPath)
    {
        _process = process;
        _requestedPort = port;
        LogPath = logPath;
        // FileShare.ReadWrite — AppendHostLog/NoteStartup and the startup
        // diagnostics append to the SAME file from outside this writer.
        // The default share mode made every one of those appends throw a
        // sharing violation that was silently swallowed, hiding narration
        // delivery and [STARTUP] diagnostics entirely.
        var logStream = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
        _logWriter = TextWriter.Synchronized(new StreamWriter(logStream) { AutoFlush = true });

        _process.EnableRaisingEvents = true;
        _process.OutputDataReceived += (_, e) =>
        {
            WriteLog("OUT", e.Data);
            if (TryParseBootMarker(e.Data, out var pct, out var primary, out var secondary))
            {
                try { BootPhase?.Invoke(pct, primary, secondary); }
                catch { /* splash updates must never break the backend host */ }
            }
            if (TryParsePortMarker(e.Data, out var announced))
            {
                _announcedPort = announced;
                if (announced != _requestedPort)
                {
                    WriteLog("HOST", $"backend bound fallback port {announced} (requested {_requestedPort})");
                }
            }
        };
        _process.ErrorDataReceived += (_, e) => WriteLog("ERR", e.Data);
        _process.Exited += (_, _) =>
        {
            var code = SafeExitCode();
            WriteLog("HOST", $"backend exited with code {code}");
            if (!_disposing)
            {
                RecordCrashExit(code);
                UnexpectedExit?.Invoke(code);
            }
        };
    }

    /// <summary>
    /// Parse a "[nexus-boot] {json}" stdout marker emitted by the backend's
    /// startup reporter. Ordinary log lines return false.
    /// </summary>
    internal static bool TryParseBootMarker(
        string? line, out double pct, out string primary, out string secondary)
    {
        pct = 0.0;
        primary = string.Empty;
        secondary = string.Empty;
        const string prefix = "[nexus-boot] ";
        if (line is null || !line.StartsWith(prefix, StringComparison.Ordinal))
        {
            return false;
        }
        try
        {
            using var doc = JsonDocument.Parse(line[prefix.Length..]);
            var root = doc.RootElement;
            pct = root.TryGetProperty("pct", out var p) ? p.GetDouble() : 0.0;
            primary = root.TryGetProperty("primary", out var pr) ? pr.GetString() ?? "" : "";
            secondary = root.TryGetProperty("secondary", out var s) ? s.GetString() ?? "" : "";
            return !string.IsNullOrEmpty(primary);
        }
        catch (JsonException)
        {
            return false;
        }
    }

    /// <summary>
    /// Parse a "[nexus-port] N" stdout line emitted right after the backend
    /// binds its socket. The backend may legitimately bind a different port
    /// than requested (the candidate can be taken between the host's free-
    /// port probe and the backend's bind); the announced port is the only
    /// trustworthy target for health checks.
    /// </summary>
    internal static bool TryParsePortMarker(string? line, out int port)
    {
        port = 0;
        const string prefix = "[nexus-port] ";
        if (line is null || !line.StartsWith(prefix, StringComparison.Ordinal))
        {
            return false;
        }
        return int.TryParse(line[prefix.Length..].Trim(), out port)
            && port > 0 && port <= 65535;
    }

    /// <summary>
    /// Persist backend process exits into the same JSONL crash history the
    /// backend writes (data/crash_history.jsonl) — a whole-backend crash can
    /// never record itself, so the host does. /api/diagnostics reads this
    /// file; data/ is junctioned to the per-user state root.
    /// </summary>
    private void RecordCrashExit(int code)
    {
        try
        {
            // LogPath is <appDir>/data/logs/backend-host.log.
            var dataDir = Directory.GetParent(Path.GetDirectoryName(LogPath)!)?.FullName;
            if (string.IsNullOrEmpty(dataDir))
            {
                return;
            }
            var entry = System.Text.Json.JsonSerializer.Serialize(new Dictionary<string, object?>
            {
                ["subsystem"] = "backend",
                ["kind"] = "process_exit",
                ["detail"] = $"backend exited with code {code}",
                ["exit_code"] = code,
                ["time"] = DateTimeOffset.Now.ToUnixTimeSeconds(),
                ["recovery"] = "host_restart",
            });
            lock (_logLock)
            {
                var crashLog = Path.Combine(dataDir, "crash_history.jsonl");
                File.AppendAllText(crashLog, entry + Environment.NewLine);
                // Bounded history — 1 MB cap, keep the newest half.
                var info = new FileInfo(crashLog);
                if (info.Length > 1024 * 1024)
                {
                    var raw = File.ReadAllBytes(crashLog);
                    var keep = raw.Skip(raw.Length - 512 * 1024).ToArray();
                    var nl = Array.IndexOf(keep, (byte)'\n');
                    File.WriteAllBytes(crashLog,
                        nl >= 0 ? keep[(nl + 1)..] : Array.Empty<byte>());
                }
            }
        }
        catch { /* crash history is best-effort — never block restart */ }
    }

    private int SafeExitCode()
    {
        try { return _process.ExitCode; }
        catch { return -1; }
    }

    private void WriteLog(string stream, string? line)
    {
        if (string.IsNullOrWhiteSpace(line))
        {
            return;
        }

        try
        {
            lock (_logLock)
            {
                _logWriter.WriteLine($"{DateTimeOffset.Now:O} [{stream}] {line}");
                if (++_logWrites % 200 == 0)
                {
                    TrimLogIfOversized();
                }
            }
        }
        catch
        {
            // Logging must never crash the desktop host.
        }
    }

    /// Rotation: caller holds _logLock. When the log exceeds LogMaxBytes,
    /// drop the writer, keep only the newest LogKeepBytes, reopen.
    private void TrimLogIfOversized()
    {
        try
        {
            if (new FileInfo(LogPath).Length <= LogMaxBytes)
            {
                return;
            }
            _logWriter.Dispose();
            var raw = File.ReadAllBytes(LogPath);
            var keep = raw.Skip(Math.Max(0, raw.Length - (int)LogKeepBytes)).ToArray();
            var nl = Array.IndexOf(keep, (byte)'\n');
            File.WriteAllBytes(LogPath, nl >= 0 ? keep[(nl + 1)..] : Array.Empty<byte>());
            File.AppendAllText(LogPath, $"{DateTimeOffset.Now:O} [HOST] "
                + $"log rotated — kept newest {LogKeepBytes / (1024 * 1024)} MB tail"
                + Environment.NewLine);
        }
        catch
        {
            // Rotation is best-effort — it must never interrupt logging.
        }
        finally
        {
            try
            {
                var fs = new FileStream(
                    LogPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                _logWriter = TextWriter.Synchronized(
                    new StreamWriter(fs) { AutoFlush = true });
            }
            catch { }
        }
    }

    /// Append a HOST line before a BackendProcess exists. The failure screen
    /// points users at this log — a launch that dies before Process.Start
    /// must still leave evidence here.
    private static void AppendHostLog(string logPath, string line)
    {
        try
        {
            // ReadWrite share — BackendProcess holds this file open for the
            // backend stdout writer; a Read-share open here collides with
            // that Write handle and every line is lost to a sharing
            // violation (silently, by the catch below).
            using var fs = new FileStream(
                logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
            using var sw = new StreamWriter(fs);
            sw.Write($"{DateTimeOffset.Now:O} [HOST] {line}{Environment.NewLine}");
        }
        catch
        {
            // Logging must never block startup.
        }
    }

    /// Writable before any BackendProcess instance exists — the earliest
    /// possible evidence that the host process reached managed code.
    public static void NoteStartup(string logDir, string line) =>
        AppendHostLog(Path.Combine(logDir, "backend-host.log"), line);

    public static BackendProcess Start(string appDir)
    {
        var logDir = Path.Combine(appDir, "data", "logs");
        var logPath = Path.Combine(logDir, "backend-host.log");
        try { Directory.CreateDirectory(logDir); } catch { }
        AppendHostLog(logPath, "backend start requested");

        // Staged update / LKG rollback — the running frozen exe cannot
        // replace itself, so the backend writes flags under data/lkg/ and
        // the host executes them here, before the new process launches.
        ApplyLkgFlags(appDir, logPath);

        // Right after an update the freshly-written backend exe can be
        // briefly invisible/inaccessible while Defender scans it — the
        // installer's post-install launch hits this window. Wait for the
        // file to settle before declaring it missing.
        var backendExe = Path.Combine(appDir, "backend", "ChatNexus.Backend.exe");
        var exeWaited = 0;
        while (!File.Exists(backendExe) && exeWaited < 30000)
        {
            AppendHostLog(logPath, "backend exe not yet visible — waiting for it to be released");
            System.Threading.Thread.Sleep(1000);
            exeWaited += 1000;
        }
        if (!File.Exists(backendExe))
        {
            AppendHostLog(logPath, $"backend exe missing after {exeWaited / 1000}s: {backendExe}");
            throw new FileNotFoundException("Nexus Core backend executable is missing.", backendExe);
        }

        var workspace = Path.Combine(appDir, "Source");
        if (!File.Exists(Path.Combine(workspace, "localcodeagent", "server.py")))
        {
            workspace = appDir;
        }

        var config = Path.Combine(appDir, "config.json");
        var port = FindFreePort();

        // Mutable user state (chat history, memory, diagnostics, generated
        // output) must not live inside the app directory — rebuilds,
        // updates, and reinstalls replace it wholesale, which has wiped
        // conversations before. Relocate those dirs under a per-user root
        // and leave junctions behind so backend paths keep resolving.
        // NEXUS_NO_STATE_REDIRECT=1 opts out (used by the build smoke test).
        var stateNotes = new List<string>();
        if (Environment.GetEnvironmentVariable("NEXUS_NO_STATE_REDIRECT") != "1")
        {
            try
            {
                EnsureStateJunctions(appDir, stateNotes);
            }
            catch (Exception ex)
            {
                // The failure screen points here — leave the reason behind.
                try { Directory.CreateDirectory(logDir); } catch { }
                AppendHostLog(logPath, $"state redirect failed: {ex.GetType().Name}: {ex.Message}");
                throw;
            }
        }
        Directory.CreateDirectory(logDir);
        foreach (var note in stateNotes)
        {
            AppendHostLog(logPath, note);
        }

        // If a previous host died without reaping its backend (force-kill,
        // crash), the orphaned backend keeps running forever — holding its
        // port, model servers, and VRAM. The pidfile identifies it as ours
        // (path must match this install) so the new instance can reap it
        // along with its whole child tree instead of double-running.
        try
        {
            ReapOrphanedBackend(appDir, backendExe, logDir);
        }
        catch (Exception ex)
        {
            AppendHostLog(logPath, $"orphan cleanup failed: {ex.GetType().Name}: {ex.Message}");
            throw;
        }
        // The host log appends every backend stdout/stderr line forever —
        // keep only a tail so long unattended sessions cannot grow it.
        try
        {
            var logInfo = new FileInfo(logPath);
            const long MaxLogBytes = 8L * 1024 * 1024;
            if (logInfo.Exists && logInfo.Length > MaxLogBytes)
            {
                using var src = new FileStream(logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                var keep = 4L * 1024 * 1024;
                src.Seek(-Math.Min(keep, src.Length), SeekOrigin.End);
                using var ms = new MemoryStream();
                src.CopyTo(ms);
                File.WriteAllBytes(logPath, ms.ToArray());
            }
        }
        catch
        {
            // Log maintenance must never block startup.
        }

        var start = new ProcessStartInfo
        {
            FileName = backendExe,
            WorkingDirectory = appDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        start.Environment["PYTHONUNBUFFERED"] = "1";
        // Backend emits structured "[nexus-boot] {json}" phase markers on
        // stdout so the splash can show real backend-internal progress.
        start.Environment["NEXUS_BOOT_MARKERS"] = "1";
        start.ArgumentList.Add("--server");
        start.ArgumentList.Add("--host");
        start.ArgumentList.Add("127.0.0.1");
        start.ArgumentList.Add("--port");
        start.ArgumentList.Add(port.ToString());
        start.ArgumentList.Add("--workspace");
        start.ArgumentList.Add(workspace);
        start.ArgumentList.Add("--config");
        start.ArgumentList.Add(config);

        AppendHostLog(
            logPath,
            $"launching backend {backendExe} --port {port} --workspace {workspace} --config {config}");

        Process process;
        try
        {
            process = Process.Start(start)
                ?? throw new InvalidOperationException("Process.Start returned null.");
        }
        catch (Exception ex)
        {
            // Antivirus scanning a freshly-updated unsigned exe can block
            // CreateProcess — record it so the failure log isn't empty.
            AppendHostLog(logPath, $"backend launch failed: {ex.GetType().Name}: {ex.Message}");
            throw new InvalidOperationException("Could not start Nexus Core backend.", ex);
        }
        try
        {
            File.WriteAllText(BackendPidPath(logDir), $"{process.Id}|{backendExe}");
        }
        catch
        {
            // Best-effort bookkeeping — never block startup on it.
        }
        var backend = new BackendProcess(process, port, logPath);
        backend.WriteLog("HOST", $"started backend pid {process.Id} on port {port}");
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return backend;
    }

    /// <summary>
    /// Execute pending LKG coordination flags the backend left under
    /// data/lkg/ — a rollback request restores a verified snapshot over
    /// backend/ + config.json + VERSION; an update request swaps the
    /// staged backend-new/ tree into place. The flag is always consumed
    /// first so a failed apply can never loop forever. Rollback wins
    /// when both are pending — it is the recovery action.
    /// </summary>
    private static void ApplyLkgFlags(string appDir, string logPath)
    {
        var lkgDir = Path.Combine(appDir, "data", "lkg");
        try
        {
            var rbFlag = Path.Combine(lkgDir, "rollback.flag");
            if (File.Exists(rbFlag))
            {
                string name;
                string reason;
                try
                {
                    using var doc = JsonDocument.Parse(File.ReadAllText(rbFlag));
                    name = doc.RootElement.TryGetProperty("name", out var n)
                        ? n.GetString() ?? "" : "";
                    reason = doc.RootElement.TryGetProperty("reason", out var r)
                        ? r.GetString() ?? "" : "";
                }
                catch { name = ""; reason = "unreadable flag"; }
                File.Delete(rbFlag);
                var snap = Path.Combine(lkgDir, name);
                if (name.Length > 0 && Directory.Exists(snap))
                {
                    var snapBackend = Path.Combine(snap, "backend");
                    var liveBackend = Path.Combine(appDir, "backend");
                    // Honor the snapshot manifest's recorded exe hash — a
                    // truncated or tampered snapshot must never replace
                    // the live backend (the backend-side store verifies
                    // the same hash before it will even stage the flag).
                    var snapExe = Path.Combine(snapBackend, "ChatNexus.Backend.exe");
                    var wantSha = "";
                    try
                    {
                        using var md = JsonDocument.Parse(
                            File.ReadAllText(Path.Combine(snap, "manifest.json")));
                        wantSha = md.RootElement.TryGetProperty("exe_sha256", out var es)
                            ? es.GetString() ?? "" : "";
                    }
                    catch { wantSha = ""; }
                    var verified = wantSha.Length == 0
                        || (File.Exists(snapExe)
                            && string.Equals(Sha256File(snapExe), wantSha,
                                             StringComparison.OrdinalIgnoreCase));
                    if (!verified)
                    {
                        AppendHostLog(logPath,
                            $"LKG rollback refused — snapshot '{name}' failed exe hash verification");
                    }
                    else if (Directory.Exists(snapBackend))
                    {
                        var spare = Path.Combine(appDir, "backend-replaced");
                        if (Directory.Exists(spare)) Directory.Delete(spare, true);
                        if (Directory.Exists(liveBackend))
                            Directory.Move(liveBackend, spare);
                        CopyTree(snapBackend, liveBackend);
                        foreach (var f in new[] { "config.json", "VERSION" })
                        {
                            var src = Path.Combine(snap, f);
                            if (File.Exists(src)) File.Copy(src, Path.Combine(appDir, f), true);
                        }
                        AppendHostLog(logPath, $"LKG rollback applied from {name} ({reason})");
                    }
                    else
                    {
                        AppendHostLog(logPath, $"rollback snapshot '{name}' has no backend tree — ignored");
                    }
                }
                else
                {
                    AppendHostLog(logPath, $"rollback flag named missing snapshot '{name}' — ignored");
                }
            }
            var upFlag = Path.Combine(lkgDir, "update.flag");
            if (File.Exists(upFlag))
            {
                string stagedDir;
                string version;
                try
                {
                    using var doc = JsonDocument.Parse(File.ReadAllText(upFlag));
                    stagedDir = doc.RootElement.TryGetProperty("staged_dir", out var s)
                        ? s.GetString() ?? "" : "";
                    version = doc.RootElement.TryGetProperty("version", out var v)
                        ? v.GetString() ?? "" : "";
                }
                catch { stagedDir = ""; version = ""; }
                File.Delete(upFlag);
                // Only allow a flat directory name — never a path escape.
                if (stagedDir.Length > 0
                    && stagedDir == Path.GetFileName(stagedDir))
                {
                    var staged = Path.Combine(appDir, stagedDir);
                    var liveBackend = Path.Combine(appDir, "backend");
                    if (Directory.Exists(staged)
                        && File.Exists(Path.Combine(staged, "ChatNexus.Backend.exe")))
                    {
                        var old = Path.Combine(appDir, "backend-old");
                        if (Directory.Exists(old)) Directory.Delete(old, true);
                        if (Directory.Exists(liveBackend))
                            Directory.Move(liveBackend, old);
                        Directory.Move(staged, liveBackend);
                        AppendHostLog(logPath, $"staged update applied ({version})");
                    }
                    else
                    {
                        AppendHostLog(logPath, $"update flag named incomplete staging '{stagedDir}' — ignored");
                    }
                }
            }
        }
        catch (Exception ex)
        {
            // LKG handling must never block the normal launch path.
            AppendHostLog(logPath, $"LKG flag handling failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    private static string Sha256File(string path)
    {
        using var sha = SHA256.Create();
        using var fs = File.OpenRead(path);
        return Convert.ToHexString(sha.ComputeHash(fs));
    }

    private static void CopyTree(string src, string dst)
    {
        Directory.CreateDirectory(dst);
        foreach (var f in Directory.GetFiles(src))
            File.Copy(f, Path.Combine(dst, Path.GetFileName(f)), true);
        foreach (var d in Directory.GetDirectories(src))
            CopyTree(d, Path.Combine(dst, Path.GetFileName(d)));
    }

    public async Task WaitUntilHealthyAsync(TimeSpan timeout)
    {
        // Per-request timeout must tolerate a warming backend: /api/status
        // can take 10s+ while model services spin up, and aborting early
        // just queues more work on an already busy server.
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        var deadline = DateTime.UtcNow + timeout;
        string? last = null;
        var lastLogged = DateTime.UtcNow;

        while (DateTime.UtcNow < deadline)
        {
            if (_process.HasExited)
            {
                throw new InvalidOperationException(
                    $"Nexus Core backend exited during startup with code {_process.ExitCode}."
                );
            }

            try
            {
                using var response = await client.GetAsync($"{BaseUrl}api/status");
                if (response.IsSuccessStatusCode)
                {
                    return;
                }
                last = $"HTTP {(int)response.StatusCode} {response.ReasonPhrase}";
            }
            catch (Exception ex)
            {
                last = $"{ex.GetType().Name}: {ex.Message}";
            }

            // Long stalls otherwise look identical to a dead backend in the
            // log; note what the health probe is actually seeing.
            if (DateTime.UtcNow - lastLogged > TimeSpan.FromSeconds(15))
            {
                WriteLog("HOST", $"still waiting for /api/status on port {Port}: {last}");
                lastLogged = DateTime.UtcNow;
            }

            await Task.Delay(150);
        }

        WriteLog("HOST", $"health check timed out after {timeout.TotalSeconds:0}s; last result: {last ?? "no request completed"}");
        throw new TimeoutException(
            $"Nexus Core backend did not become ready within {timeout.TotalSeconds:0} seconds. {last} " +
            $"Check {LogPath} for backend errors."
        );
    }

    public async Task ProbeUiAsync()
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        foreach (var route in new[] { "", "image.html", "research.html", "voice.html" })
        {
            var body = await client.GetStringAsync(BaseUrl + route);
            if (!body.Contains("Nexus Core", StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException($"UI smoke check failed for /{route}");
            }
        }
    }

    public void Dispose()
    {
        _disposing = true;
        try
        {
            if (!_process.HasExited)
            {
                // Ask the backend to close gracefully first — a clean exit
                // lets it mark the session, flush state, and stop model
                // runtimes. The kill stays as the fallback for a hung stop.
                try
                {
                    using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
                    client.PostAsync($"{BaseUrl}api/shutdown", new StringContent("{}", System.Text.Encoding.UTF8, "application/json")).Wait(3000);
                }
                catch { }
                if (!_process.WaitForExit(8000))
                {
                    _process.Kill(entireProcessTree: true);
                    _process.WaitForExit(5000);
                }
            }
        }
        catch
        {
            // Process teardown should never block application exit.
        }
        finally
        {
            try { _logWriter.Dispose(); } catch { }
            _process.Dispose();
        }
    }

    /// <summary>Mutable dirs that belong to the user, not the install.</summary>
    private static readonly string[] StateDirs = { "data", ".agent", "output" };

    /// <summary>
    /// Legacy layout redirected these to {drive}\NexusCore\{name} via
    /// junction — one app, two root dirs, which users rightly find
    /// confusing ("there shouldn't be two install directories"). They now
    /// live inside the install directory like everything else; installs
    /// that still have the junction get re-homed on first launch. Each
    /// entry is (legacy dir name, path inside the install dir) — models
    /// sit at the root, tool payloads under tools\.
    /// </summary>
    private static readonly (string Name, string Dest)[] RehomeDirs =
    {
        ("models", "models"),
        ("ComfyUI_windows_portable", Path.Combine("tools", "ComfyUI_windows_portable")),
    };

    private static string UserStateRoot() =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "NexusCore");

    /// <summary>
    /// Undo the legacy {drive}\NexusCore\{name} redirection: unlink the
    /// junction (target untouched), then move the external target's
    /// contents into the install dir. Same-drive moves stay renames —
    /// the legacy root sits on the install drive by construction.
    /// </summary>
    private static void RehomeDriveStateDirs(string appDir, List<string> notes)
    {
        var driveRoot = Path.GetPathRoot(Path.GetFullPath(appDir));
        foreach (var (name, dest) in RehomeDirs)
        {
            var link = Path.Combine(appDir, dest);
            var legacy = Path.Combine(driveRoot ?? appDir, "NexusCore", name);
            try
            {
                if (Directory.Exists(link) &&
                    (File.GetAttributes(link) & FileAttributes.ReparsePoint) != 0)
                {
                    // Directory.Delete on a junction removes the link
                    // itself — the target is not traversed.
                    Directory.Delete(link);
                    notes.Add($"removed legacy {name}/ junction");
                }
                if (Directory.Exists(legacy))
                {
                    var parent = Path.GetDirectoryName(link);
                    if (!string.IsNullOrEmpty(parent))
                    {
                        Directory.CreateDirectory(parent);
                    }
                    if (!Directory.Exists(link))
                    {
                        try
                        {
                            Directory.Move(legacy, link);
                            notes.Add($"moved {name}/ to {dest}/ in the install directory");
                        }
                        catch (IOException)
                        {
                            MigrateDirectoryContents(legacy, link);
                            try { Directory.Delete(legacy, recursive: true); } catch { }
                            notes.Add($"migrated {name}/ to {dest}/ in the install directory");
                        }
                    }
                    else
                    {
                        MigrateDirectoryContents(legacy, link);
                        try { Directory.Delete(legacy, recursive: true); } catch { }
                        notes.Add($"merged legacy {name}/ into {dest}/");
                    }
                }
                Directory.CreateDirectory(link);
            }
            catch (Exception ex)
            {
                notes.Add($"could not re-home {name}/ ({ex.Message})");
            }
        }
        // Installs before the tools/ layout kept tool payloads at the app
        // root (real dir, or a junction still pointing at the legacy
        // drive root). Relocate or unlink them under tools\ as well.
        foreach (var (name, dest) in RehomeDirs)
        {
            if (name == dest)
            {
                continue;
            }
            var rootPath = Path.Combine(appDir, name);
            var target = Path.Combine(appDir, dest);
            try
            {
                if (!Directory.Exists(rootPath))
                {
                    continue;
                }
                if ((File.GetAttributes(rootPath) & FileAttributes.ReparsePoint) != 0)
                {
                    Directory.Delete(rootPath); // link only — target untouched
                    notes.Add($"removed legacy {name}/ junction");
                    continue;
                }
                var parent = Path.GetDirectoryName(target);
                if (!string.IsNullOrEmpty(parent))
                {
                    Directory.CreateDirectory(parent);
                }
                if (!Directory.Exists(target))
                {
                    try
                    {
                        Directory.Move(rootPath, target);
                        notes.Add($"moved {name}/ under {dest.Split('\\', '/')[0]}/");
                    }
                    catch (IOException)
                    {
                        MigrateDirectoryContents(rootPath, target);
                        try { Directory.Delete(rootPath, recursive: true); } catch { }
                        notes.Add($"migrated {name}/ under tools/");
                    }
                }
                else
                {
                    MigrateDirectoryContents(rootPath, target);
                    try { Directory.Delete(rootPath, recursive: true); } catch { }
                    notes.Add($"merged {name}/ into {dest}/");
                }
            }
            catch (Exception ex)
            {
                notes.Add($"could not relocate {name}/ under tools/ ({ex.Message})");
            }
        }
        // The legacy drive root only existed to host those two dirs —
        // drop it once empty so no second Nexus-named root lingers.
        try
        {
            var legacyRoot = Path.Combine(driveRoot ?? appDir, "NexusCore");
            if (Directory.Exists(legacyRoot))
            {
                Directory.Delete(legacyRoot);
            }
        }
        catch { }
    }

    /// <summary>
    /// Redirect each mutable state dir under appDir to the per-user state
    /// root via a directory junction. Junctions are transparent to the
    /// backend — every runtime_root/"data" path resolves through them —
    /// and creating one needs no elevation. Existing content is merged
    /// into the state root first so an upgrade never drops history.
    /// </summary>
    private static void EnsureStateJunctions(string appDir, List<string> notes)
    {
        RehomeDriveStateDirs(appDir, notes);
        foreach (var name in StateDirs)
        {
            var link = Path.Combine(appDir, name);
            var target = Path.Combine(UserStateRoot(), name);
            try
            {
                if (Directory.Exists(link))
                {
                    if ((File.GetAttributes(link) & FileAttributes.ReparsePoint) != 0)
                    {
                        continue; // already redirected
                    }
                    MigrateDirectoryContents(link, target);
                    Directory.Delete(link, recursive: true);
                    notes.Add($"migrated {name}/ into per-user state at {target}");
                }
                Directory.CreateDirectory(target);
                CreateJunction(link, target);
                notes.Add($"redirected {name}/ to {target}");
            }
            catch (Exception ex)
            {
                // Redirection is a durability upgrade, never a startup
                // blocker — fall back to a plain in-place directory.
                notes.Add($"could not redirect {name}/ ({ex.Message}) — state stays in the install directory");
                try { Directory.CreateDirectory(link); } catch { }
            }
        }
    }

    /// <summary>
    /// Move source contents into target; existing target entries win.
    /// Moves keep model-size migrations instant on the same volume; the
    /// recursive fallback covers the rare cross-volume case.
    /// </summary>
    private static void MigrateDirectoryContents(string source, string target)
    {
        Directory.CreateDirectory(target);
        foreach (var file in Directory.EnumerateFiles(source))
        {
            var dest = Path.Combine(target, Path.GetFileName(file));
            if (!File.Exists(dest))
            {
                File.Move(file, dest);
            }
        }
        foreach (var dir in Directory.EnumerateDirectories(source))
        {
            var dest = Path.Combine(target, Path.GetFileName(dir));
            if (!Directory.Exists(dest))
            {
                try
                {
                    Directory.Move(dir, dest);
                    continue;
                }
                catch (IOException)
                {
                    // Cross-volume: fall through to the copy path.
                }
            }
            MigrateDirectoryContents(dir, dest);
            try { Directory.Delete(dir, recursive: true); } catch { }
        }
    }

    private static void CreateJunction(string link, string target)
    {
        using var p = Process.Start(new ProcessStartInfo
        {
            FileName = "cmd.exe",
            Arguments = $"/c mklink /J \"{link}\" \"{target}\"",
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        });
        p?.WaitForExit(10000);
        if (p is null || p.ExitCode != 0 || !Directory.Exists(link))
        {
            throw new InvalidOperationException($"mklink /J failed for {link}");
        }
    }

    private static string BackendPidPath(string logDir) =>
        Path.Combine(logDir, "backend.pid");

    private static void ReapOrphanedBackend(string appDir, string backendExe, string logDir)
    {
        try
        {
            var pidFile = BackendPidPath(logDir);
            if (!File.Exists(pidFile))
            {
                return;
            }
            var parts = (File.ReadAllText(pidFile).Trim()).Split('|');
            if (parts.Length < 2 || !int.TryParse(parts[0], out var pid))
            {
                return;
            }
            // Path check is the safety latch: a reused pid belonging to an
            // unrelated process is never killed.
            if (!string.Equals(parts[1], backendExe, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }
            using var orphan = Process.GetProcessById(pid);
            var orphanPath = orphan.MainModule?.FileName ?? "";
            if (string.Equals(orphanPath, backendExe, StringComparison.OrdinalIgnoreCase))
            {
                KillProcessTree(orphan);
            }
        }
        catch
        {
            // Stale pidfile, dead process, access denied — all fine; launch
            // proceeds regardless.
        }

        // The pidfile only covers backends this host launched. A backend
        // orphaned some other way (host killed mid-launch, manual start)
        // still holds shared state and can stall the next backend's
        // /api/status. Match on the exe path — never on name alone — so an
        // unrelated process is never touched.
        foreach (var candidate in Process.GetProcesses())
        {
            try
            {
                if (candidate.Id == Environment.ProcessId)
                {
                    continue;
                }
                var path = candidate.MainModule?.FileName ?? "";
                if (string.Equals(path, backendExe, StringComparison.OrdinalIgnoreCase))
                {
                    KillProcessTree(candidate);
                }
            }
            catch
            {
                // Exited mid-scan or module inaccessible — skip it.
            }
            finally
            {
                candidate.Dispose();
            }
        }
    }

    private static void KillProcessTree(Process process)
    {
        // taskkill /T takes the orphan's children (llama-server, ComfyUI
        // python, MCP servers) with it — killing only the parent would
        // orphan them holding ports/VRAM.
        try
        {
            var tk = Process.Start(new ProcessStartInfo
            {
                FileName = "taskkill",
                Arguments = $"/F /T /PID {process.Id}",
                CreateNoWindow = true,
                UseShellExecute = false,
            });
            tk?.WaitForExit(10000);
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch
        {
            // Already gone — nothing to reap.
        }
    }

    private static int FindFreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try
        {
            return ((IPEndPoint)listener.LocalEndpoint).Port;
        }
        finally
        {
            listener.Stop();
        }
    }
}

internal sealed class MainForm : Form
{
    private readonly string _appDir;
    private readonly WebView2 _webView = new();
    private BackendProcess? _backend;
    private bool _closing;
    private bool _backendRestarting;
    private int _backendRestartCount;
    private DateTimeOffset _lastBackendRestart = DateTimeOffset.MinValue;
    private readonly TaskCompletionSource<bool> _interfaceReady =
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    public MainForm(string appDir)
    {
        _appDir = appDir;

        Text = "Nexus Core";
        StartPosition = FormStartPosition.CenterScreen;
        Width = 1440;
        Height = 900;
        MinimumSize = new Size(980, 640);
        BackColor = Color.FromArgb(7, 16, 31);

        var iconPath = Path.Combine(_appDir, "nexus-core.ico");
        if (File.Exists(iconPath))
        {
            Icon = new Icon(iconPath);
        }

        _webView.Dock = DockStyle.Fill;
        // WebView2's default first-paint background is white — it flashes for
        // a frame when the window first becomes visible even with the page
        // already loaded. Match the app theme so the reveal is seamless.
        _webView.DefaultBackgroundColor = Color.FromArgb(7, 16, 31);
        Controls.Add(_webView);

    }

    /// <summary>
    /// Shutdown narration — runs before the window may close, awaited to
    /// real playback completion by the host. Set by the application
    /// context; null means close normally.
    /// </summary>
    public Func<Task>? FarewellHook { get; set; }
    private bool _farewellRunning;
    private bool _allowClose;
    // Interface voice-queue state, reported via webview messages. The
    // farewell drains this before speaking so shutdown never overlaps a
    // greeting/response already mid-playback.
    private volatile bool _webVoiceBusy;
    private TaskCompletionSource<bool> _webVoiceIdleTcs =
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        // Farewell holds for every graceful close request — X button,
        // CloseMainWindow, Task Manager "End task" (all WM_CLOSE-based).
        // A hard kill never reaches here. WindowsShutDown stays excluded:
        // the OS is going away and audio may already be gone.
        var farewellEligible = e.CloseReason is CloseReason.UserClosing
            or CloseReason.TaskManagerClosing
            or CloseReason.ApplicationExitCall or CloseReason.None
            or CloseReason.FormOwnerClosing or CloseReason.MdiFormClosing;
        try
        {
            BackendProcess.NoteStartup(Path.Combine(_appDir, "data", "logs"),
                $"closing: reason={e.CloseReason} eligible={farewellEligible} " +
                $"hook={(FarewellHook is not null)} running={_farewellRunning} allow={_allowClose}");
        }
        catch { }
        if (_allowClose)
        {
            _closing = true;
            _backend?.Dispose();
            base.OnFormClosing(e);
            return;
        }
        if (farewellEligible && FarewellHook is not null)
        {
            // Swallow every close while the farewell runs — a second X
            // click must not cut the goodbye short.
            e.Cancel = true;
            if (!_farewellRunning)
            {
                _farewellRunning = true;
                _ = RunFarewellThenCloseAsync();
            }
            return;
        }
        _closing = true;
        _backend?.Dispose();
        base.OnFormClosing(e);
    }

    private async Task RunFarewellThenCloseAsync()
    {
        try { await DrainWebVoiceAsync(TimeSpan.FromSeconds(75)); }
        catch { /* a stuck queue must never trap the exit */ }
        try { if (FarewellHook is not null) await FarewellHook(); }
        catch { /* a failed farewell must never trap the exit */ }
        _allowClose = true;
        if (!IsDisposed)
        {
            try { BeginInvoke(new Action(Close)); } catch { }
        }
    }

    /// <summary>
    /// Voice clips already playing in the interface must finish before the
    /// farewell speaks — the goodbye never talks over an in-flight
    /// greeting/response. Latch the page's queue so nothing new starts,
    /// then wait for busy→idle. Bounded so wedged audio can't hang exit.
    /// </summary>
    private async Task DrainWebVoiceAsync(TimeSpan budget)
    {
        try
        {
            if (_webView.CoreWebView2 is not null)
            {
                await _webView.CoreWebView2.ExecuteScriptAsync(
                    "window.NexusVoice&&(NexusVoice._draining=true," +
                    "NexusVoice._reportState&&NexusVoice._reportState())");
            }
        }
        catch { /* webview may already be gone — nothing to drain */ }
        if (!_webVoiceBusy)
        {
            return;
        }
        var deadline = System.Diagnostics.Stopwatch.StartNew();
        while (_webVoiceBusy && deadline.Elapsed < budget)
        {
            var remaining = budget - deadline.Elapsed;
            if (remaining <= TimeSpan.Zero)
            {
                break;
            }
            var tcs = _webVoiceIdleTcs;
            await Task.WhenAny(tcs.Task, Task.Delay(remaining));
        }
        try
        {
            BackendProcess.NoteStartup(Path.Combine(_appDir, "data", "logs"),
                $"web voice drain done busy={_webVoiceBusy} waited={deadline.ElapsedMilliseconds}ms");
        }
        catch { }
    }

    /// <summary>Backend base URL once the process is up — used by startup narration.</summary>
    public string? BackendUrl => _backend?.BaseUrl;

    /// <summary>
    /// Backend + WebView2 startup that runs while the form is still hidden.
    /// Reports real milestones into the splash progress and only completes
    /// once the interface posts its ready handshake (or a bounded fallback).
    /// </summary>
    /// <summary>
    /// Tell the interface the startup transition is complete — the splash
    /// is gone and startup narration + quiet buffer have fully finished.
    /// Greeting audio waits on this signal so it can never overlap Isabella
    /// or start inside the post-narration quiet window.
    /// </summary>
    public void SignalStartupTransition()
    {
        try
        {
            _webView.CoreWebView2?.PostWebMessageAsJson(
                JsonSerializer.Serialize(new { type = "startup-transition-complete" }));
        }
        catch { }
    }

    public async Task PrepareAsync(StartupProgress progress)
    {
        AttachBackend(BackendProcess.Start(_appDir));
        // Backend-internal init phases arrive on stdout as [nexus-boot] markers
        // while the HTTP server is still coming up; map them into the band the
        // host owns between "backend launched" and "backend healthy".
        _backend!.BootPhase += (pct, primary, secondary) =>
            progress.Report(0.30 + Math.Clamp(pct, 0.0, 100.0) / 100.0 * 0.24, primary, secondary);
        progress.Report(0.30, "services");
        // Cold starts on machines scanning a fresh unsigned exe (AV) can
        // exceed 60s even when the backend is healthy — the PyInstaller
        // bundle with onnxruntime/kokoro/numpy is ~200MB to scan.
        await _backend!.WaitUntilHealthyAsync(TimeSpan.FromSeconds(180));
        progress.Report(0.55, "interface");

        var userDataFolder = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "ChatNexus",
            "WebView2"
        );
        Directory.CreateDirectory(userDataFolder);

        // Desktop app, not a browser tab — greeting and voice playback
        // should not have to wait for a user gesture. The default autoplay
        // policy silently held queued voice segments until the first click.
        var environment = await CoreWebView2Environment.CreateAsync(
            browserExecutableFolder: null,
            userDataFolder: userDataFolder,
            options: new CoreWebView2EnvironmentOptions(
                additionalBrowserArguments: "--autoplay-policy=no-user-gesture-required")
        );

        await _webView.EnsureCoreWebView2Async(environment);
        ConfigureWebView();
        await ClearStaleWebCacheAsync(userDataFolder);
        progress.Report(0.72, "workspace");

        var ready = WaitForInterfaceReadyAsync();
        _webView.Source = new Uri(_backend.BaseUrl);
        progress.Report(0.85, "workspace");
        // First-run workstation messaging only when provisioning is real —
        // an enabled, frozen install builds and runs its setup plan in the
        // background; nothing here waits on downloads.
        if (SplashForm.ProvisioningPlanned(_appDir))
        {
            progress.Report(0.87, "workstation");
            progress.Report(0.90, "bg_setup");
        }
        progress.Report(0.93, "interface");
        await ready;
    }

    /// <summary>
    /// Clears the WebView2 disk cache once per backend payload change.
    /// After an update, a heuristic-cached page from the previous build can
    /// paint for a frame before the fresh (no-store) response replaces it —
    /// a visible old-page-then-new-page flash. Keyed on the backend exe's
    /// timestamp so it only runs when the payload actually changed.
    /// </summary>
    private async Task ClearStaleWebCacheAsync(string userDataFolder)
    {
        try
        {
            var marker = Path.Combine(userDataFolder, "nexus-core-webcache-stamp.txt");
            var backendExe = Path.Combine(_appDir, "backend", "ChatNexus.Backend.exe");
            var exeStamp = File.Exists(backendExe)
                ? File.GetLastWriteTimeUtc(backendExe).Ticks.ToString()
                : "unknown";
            // Web assets can update independently of the exe (asset-only
            // deploys). Fold the newest web file's timestamp into the stamp
            // so a fresh personality.js/app.js is never masked by a stale
            // WebView2 disk cache.
            var webStamp = "none";
            try
            {
                var webDir = Path.Combine(_appDir, "backend", "_internal", "web");
                if (Directory.Exists(webDir))
                {
                    webStamp = Directory.EnumerateFiles(webDir, "*", SearchOption.AllDirectories)
                        .Select(File.GetLastWriteTimeUtc)
                        .DefaultIfEmpty(DateTime.MinValue)
                        .Max().Ticks.ToString();
                }
            }
            catch { }
            var stamp = exeStamp + "|" + webStamp;
            if (File.Exists(marker) && string.Equals(File.ReadAllText(marker).Trim(), stamp, StringComparison.Ordinal))
            {
                return;
            }

            await _webView.CoreWebView2.Profile.ClearBrowsingDataAsync(
                CoreWebView2BrowsingDataKinds.DiskCache |
                CoreWebView2BrowsingDataKinds.CacheStorage |
                CoreWebView2BrowsingDataKinds.FileSystems);
            File.WriteAllText(marker, stamp);
        }
        catch
        {
            // Cache maintenance must never block or fail startup.
        }
    }

    /// <summary>
    /// Waits for the frontend's "nexus-core-ready" postMessage. Navigation
    /// alone is not sufficient: it only proves a page arrived, not that the
    /// shell initialized. A bounded NavigationCompleted fallback keeps a
    /// broken-but-loaded page from hanging startup forever.
    /// </summary>
    private async Task WaitForInterfaceReadyAsync()
    {
        var core = _webView.CoreWebView2;
        var navigated = new TaskCompletionSource<bool>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        EventHandler<CoreWebView2NavigationCompletedEventArgs> onNav = (_, args) =>
        {
            if (args.IsSuccess)
            {
                navigated.TrySetResult(true);
            }
        };
        core.NavigationCompleted += onNav;
        try
        {
            var ready = _interfaceReady.Task;
            var navTimeout = Task.Delay(TimeSpan.FromSeconds(45));
            // If the handshake never arrives but navigation succeeded, give
            // the page up to 15s to post it, then proceed rather than hang.
            var fallback = Task.Run(async () =>
            {
                await navigated.Task;
                await Task.Delay(TimeSpan.FromSeconds(15));
            });
            var winner = await Task.WhenAny(ready, fallback, navTimeout);
            if (winner == navTimeout)
            {
                throw new TimeoutException("Nexus Core interface did not load.");
            }
        }
        finally
        {
            core.NavigationCompleted -= onNav;
        }
    }

    public void DisposeBackend()
    {
        _backend?.Dispose();
        _backend = null;
    }

    private void AttachBackend(BackendProcess backend)
    {
        _backend = backend;
        backend.UnexpectedExit += exitCode =>
        {
            if (_closing || IsDisposed || !IsHandleCreated || !ReferenceEquals(_backend, backend))
            {
                return;
            }

            try
            {
                BeginInvoke(new Action(() => _ = RecoverBackendAsync(exitCode, backend.LogPath)));
            }
            catch
            {
                // The form may be closing while the backend exit event is raised.
            }
        };
    }

    private async Task RecoverBackendAsync(int exitCode, string logPath)
    {
        if (_closing || _backendRestarting)
        {
            return;
        }

        _backendRestarting = true;
        try
        {
            // Reset the counter when the previous crash was a while ago — a
            // stable backend that dies occasionally over a long session should
            // keep recovering; only a crash loop hits the limit.
            if (DateTimeOffset.Now - _lastBackendRestart > TimeSpan.FromMinutes(5))
            {
                _backendRestartCount = 0;
            }
            _backendRestartCount += 1;
            _lastBackendRestart = DateTimeOffset.Now;
            _backend?.Dispose();
            _backend = null;

            if (_backendRestartCount > 3)
            {
                MessageBox.Show(
                    $"Nexus Core backend stopped repeatedly (last exit code {exitCode}).\n\nBackend log: {logPath}",
                    "Nexus Core backend error",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return;
            }

            await Task.Delay(350);
            var replacement = BackendProcess.Start(_appDir);
            AttachBackend(replacement);
            await replacement.WaitUntilHealthyAsync(TimeSpan.FromSeconds(180));

            if (!_closing && _webView.CoreWebView2 is not null)
            {
                _webView.Source = new Uri(replacement.BaseUrl);
            }
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                $"Nexus Core could not restart its backend.\n\n{ex}\n\nBackend log: {logPath}",
                "Nexus Core backend restart error",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
        finally
        {
            _backendRestarting = false;
        }
    }

    private void ConfigureWebView()
    {
        var core = _webView.CoreWebView2;
        core.Settings.AreDevToolsEnabled = false;
        core.Settings.AreDefaultScriptDialogsEnabled = true;
        core.Settings.IsStatusBarEnabled = false;

        // Frontend readiness handshake — the shell posts "nexus-core-ready"
        // after its first real render cycle completes.
        core.WebMessageReceived += (_, args) =>
        {
            try
            {
                // Pages post structured objects — TryGetWebMessageAsString
                // only unwraps string posts, so WebMessageAsJson is the
                // only reliable channel. (The ready handshake previously
                // waited out its 15s fallback every boot for this reason.)
                var msg = args.WebMessageAsJson;
                using var doc = System.Text.Json.JsonDocument.Parse(msg);
                var type = doc.RootElement.TryGetProperty("type", out var t)
                    ? t.GetString()
                    : null;
                if (type == "nexus-core-ready")
                {
                    _interfaceReady.TrySetResult(true);
                }
                else if (type == "voice-state" &&
                         doc.RootElement.TryGetProperty("busy", out var busy))
                {
                    // The interface's voice queue reports busy/idle so the
                    // farewell can wait out in-flight speech instead of
                    // talking over it.
                    try
                    {
                        BackendProcess.NoteStartup(
                            Path.Combine(_appDir, "data", "logs"),
                            $"web voice-state busy={busy.GetBoolean()}");
                    }
                    catch { }
                    if (busy.GetBoolean())
                    {
                        _webVoiceBusy = true;
                        if (_webVoiceIdleTcs.Task.IsCompleted)
                        {
                            _webVoiceIdleTcs =
                                new TaskCompletionSource<bool>(
                                    TaskCreationOptions
                                        .RunContinuationsAsynchronously);
                        }
                    }
                    else
                    {
                        _webVoiceBusy = false;
                        _webVoiceIdleTcs.TrySetResult(true);
                    }
                }
            }
            catch
            {
                // A malformed message must never stall startup readiness.
            }
        };

        core.NewWindowRequested += (_, args) =>
        {
            args.Handled = true;
            OpenExternal(args.Uri);
        };

        core.NavigationStarting += (_, args) =>
        {
            if (_backend is null)
            {
                return;
            }

            if (!Uri.TryCreate(args.Uri, UriKind.Absolute, out var uri))
            {
                return;
            }

            var isLocal =
                uri.Host.Equals("127.0.0.1", StringComparison.OrdinalIgnoreCase) ||
                uri.Host.Equals("localhost", StringComparison.OrdinalIgnoreCase);

            if (!isLocal)
            {
                args.Cancel = true;
                OpenExternal(args.Uri);
            }
        };
    }

    private static void OpenExternal(string? uri)
    {
        if (string.IsNullOrWhiteSpace(uri))
        {
            return;
        }

        try
        {
            Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true });
        }
        catch
        {
            // External navigation failure should not crash Nexus Core.
        }
    }
}

/// <summary>
/// Win32 interop for z-order/foreground control — the pieces WinForms
/// can't express. Splash uses SetWindowPos(HWND_TOPMOST) as a watchdog
/// re-assert; the main-window transition uses the attach-thread-input
/// sequence so SetForegroundWindow is honored even when Nexus was
/// launched without foreground rights (start menu, scripts, self-update).
/// </summary>
internal static class Win32
{
    internal static readonly IntPtr HwndTopmost = new(-1);

    internal const uint SwpNomove = 0x0002;
    internal const uint SwpNosize = 0x0001;
    internal const uint SwpNoactivate = 0x0010;
    internal const uint SwpShowwindow = 0x0040;

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    internal static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
        int x, int y, int cx, int cy, uint flags);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    internal static extern bool SetForegroundWindow(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    internal static extern bool BringWindowToTop(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    internal static extern IntPtr GetForegroundWindow();

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    internal static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    internal static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    internal static extern uint GetCurrentThreadId();

    /// <summary>
    /// Pull <paramref name="hWnd"/> to the front even when this process
    /// lacks foreground rights: temporarily share the foreground thread's
    /// input state so Windows treats the request as user-initiated.
    /// No-op when nothing is foreground.
    /// </summary>
    internal static void ForceForeground(IntPtr hWnd)
    {
        var foreground = GetForegroundWindow();
        if (foreground == IntPtr.Zero)
        {
            SetForegroundWindow(hWnd);
            return;
        }
        var foregroundThread = GetWindowThreadProcessId(foreground, out _);
        var currentThread = GetCurrentThreadId();
        var attached = foregroundThread != currentThread
            && AttachThreadInput(currentThread, foregroundThread, true);
        try
        {
            BringWindowToTop(hWnd);
            SetForegroundWindow(hWnd);
        }
        finally
        {
            if (attached)
            {
                AttachThreadInput(currentThread, foregroundThread, false);
            }
        }
    }
}
