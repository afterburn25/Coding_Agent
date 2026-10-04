using System.Diagnostics;
using System.Drawing.Drawing2D;
using System.Net;
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
            Application.Run(new NexusCoreApplicationContext(appDir));
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
    private readonly Image? _artwork;
    private readonly string _appDir;

    private Panel? _failurePanel;
    public event Action? RetryRequested;
    public event Action? ExitRequested;

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

        _timer.Interval = 33;
        _timer.Tick += (_, _) =>
        {
            _progress.Tick();
            Invalidate();
        };
        _timer.Start();
    }

    public void ShowFailure(string message)
    {
        _timer.Stop();
        _progress.MarkFailed();
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
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;

        if (_artwork is not null)
        {
            // Cover-fit the artwork.
            var scale = Math.Max((float)ClientSize.Width / _artwork.Width,
                                 (float)ClientSize.Height / _artwork.Height);
            var w = _artwork.Width * scale;
            var h = _artwork.Height * scale;
            g.DrawImage(_artwork, (ClientSize.Width - w) / 2, (ClientSize.Height - h) / 2, w, h);
        }
        else
        {
            using var bg = new LinearGradientBrush(ClientRectangle,
                Color.FromArgb(4, 8, 18), Color.FromArgb(10, 20, 44), 90f);
            g.FillRectangle(bg, ClientRectangle);
            using var font = new Font("Segoe UI", 30f, FontStyle.Bold);
            TextRenderer.DrawText(g, "NEXUS CORE", font, ClientRectangle,
                Color.FromArgb(120, 200, 255),
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
        }

        // Progress bar — covers the artwork's own baked-in bar (x ~390–660,
        // y ~502–509 on the 1024×576 source). The artwork ships with a static
        // half-lit grey fill, so this track is fully opaque and slightly
        // oversized: the baked bar disappears entirely and only the live
        // gradient fill reads as the progress indicator.
        var barWidth = (int)(ClientSize.Width * 0.284);
        var barHeight = 7;
        var barX = (int)(ClientSize.Width * 0.372);
        var barY = (int)(ClientSize.Height * 0.873);
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
            var coreX = ClientSize.Width * 0.5f;
            var coreY = ClientSize.Height * 0.34f;
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
        var isReady = _progress.ReadyToDismiss || completion > 0;
        var primaryRect = new Rectangle(0, ClientSize.Height - 46, ClientSize.Width, 20);
        TextRenderer.DrawText(g, _progress.Primary, primaryFont, primaryRect,
            isReady ? Color.FromArgb(140, 240, 200) : Color.FromArgb(90, 215, 255),
            TextFormatFlags.HorizontalCenter);
        var secondaryRect = new Rectangle(0, ClientSize.Height - 27, ClientSize.Width, 18);
        TextRenderer.DrawText(g, _progress.Secondary, secondaryFont, secondaryRect,
            Color.FromArgb(150, 170, 200),
            TextFormatFlags.HorizontalCenter);

        base.OnPaint(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _timer.Dispose();
            _artwork?.Dispose();
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

    public NexusCoreApplicationContext(string appDir)
    {
        _appDir = appDir;
        _progress = CreateProgress();
        _splash = new SplashForm(appDir, _progress);
        _splash.RetryRequested += OnRetry;
        _splash.ExitRequested += () => Application.Exit();
        _splash.Show();
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
        _splash.Show();
        _ = RunStartupAsync();
    }

    private async Task RunStartupAsync()
    {
        try
        {
            _progress.Report(0.06, "INITIALIZING · NEXUS CORE", "Preparing local application environment");

            // Build the real main window now, still invisible.
            _main = new MainForm(_appDir);
            _main.CreateControl(); // handle exists without showing the window

            _progress.Report(0.15, "STARTING · CORE SERVICES", "Launching Nexus Core backend services");
            await _main.PrepareAsync(_progress);

            // The interface posted its ready handshake; the app is genuinely
            // usable. Now hold the splash until the minimum display time too,
            // then play the brief READY + core-glow completion effect before
            // handing off — still no blank intermediate state.
            _progress.MarkAppReady();
            while (!_progress.ReadyToDismiss)
            {
                await Task.Delay(60);
            }
            _progress.BeginCompletion();
            while (!_progress.CompletionFinished)
            {
                await Task.Delay(33);
            }

            _splash?.Close();
            _splash?.Dispose();
            _splash = null;
            _main.Show();
            _main.Activate();
            _main.BringToFront();
            MainForm = _main;
        }
        catch (Exception ex)
        {
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
    private readonly TextWriter _logWriter;
    private readonly object _logLock = new();
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
        _logWriter = TextWriter.Synchronized(new StreamWriter(logPath, append: true) { AutoFlush = true });

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
                File.AppendAllText(
                    Path.Combine(dataDir, "crash_history.jsonl"),
                    entry + Environment.NewLine);
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
            }
        }
        catch
        {
            // Logging must never crash the desktop host.
        }
    }

    /// Append a HOST line before a BackendProcess exists. The failure screen
    /// points users at this log — a launch that dies before Process.Start
    /// must still leave evidence here.
    private static void AppendHostLog(string logPath, string line)
    {
        try
        {
            File.AppendAllText(
                logPath,
                $"{DateTimeOffset.Now:O} [HOST] {line}{Environment.NewLine}");
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
                _process.Kill(entireProcessTree: true);
                _process.WaitForExit(5000);
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

        FormClosing += (_, _) =>
        {
            _closing = true;
            _backend?.Dispose();
        };
    }

    /// <summary>
    /// Backend + WebView2 startup that runs while the form is still hidden.
    /// Reports real milestones into the splash progress and only completes
    /// once the interface posts its ready handshake (or a bounded fallback).
    /// </summary>
    public async Task PrepareAsync(StartupProgress progress)
    {
        AttachBackend(BackendProcess.Start(_appDir));
        // Backend-internal init phases arrive on stdout as [nexus-boot] markers
        // while the HTTP server is still coming up; map them into the band the
        // host owns between "backend launched" and "backend healthy".
        _backend!.BootPhase += (pct, primary, secondary) =>
            progress.Report(0.30 + Math.Clamp(pct, 0.0, 100.0) / 100.0 * 0.24, primary, secondary);
        progress.Report(0.30, "STARTING · CORE SERVICES", "Waiting for backend health");
        // Cold starts on machines scanning a fresh unsigned exe (AV) can
        // exceed 60s even when the backend is healthy — the PyInstaller
        // bundle with onnxruntime/kokoro/numpy is ~200MB to scan.
        await _backend!.WaitUntilHealthyAsync(TimeSpan.FromSeconds(180));
        progress.Report(0.55, "CONNECTING · LOCAL AI RUNTIME", "Backend healthy — synchronizing runtime state");

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
        progress.Report(0.72, "INITIALIZING · NEXUS INTERFACE", "Initializing WebView2");

        var ready = WaitForInterfaceReadyAsync();
        _webView.Source = new Uri(_backend.BaseUrl);
        progress.Report(0.85, "LOADING · NEXUS INTERFACE", "Rendering the Nexus Core application shell");
        progress.Report(0.93, "CONNECTING · INTERFACE TO CORE", "Waiting for application readiness handshake");
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
                if (args.TryGetWebMessageAsString().Contains("nexus-core-ready", StringComparison.Ordinal))
                {
                    _interfaceReady.TrySetResult(true);
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
