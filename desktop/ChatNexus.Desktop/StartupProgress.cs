using System.Diagnostics;
using System.Text.Json;

namespace ChatNexus.Desktop;

/// <summary>
/// Persistent per-machine startup timing profile (data/startup_profile.json).
/// Records how long each real startup phase takes so the splash can pace the
/// predictive progress bar to THIS machine. Timings are advisory only — they
/// never gate readiness. An EMA with outlier damping keeps one bad startup
/// (AV scan, cold cache) from wrecking the estimate.
/// </summary>
internal sealed class StartupProfile
{
    public const int SchemaVersion = 1;
    private static readonly TimeSpan MaxAge = TimeSpan.FromDays(90);
    private const double EmaAlpha = 0.30;
    private const double OutlierAlpha = 0.08;
    private const double OutlierFactor = 4.0;

    public string AppVersion = "";
    public int CpuCores;
    public double RamGb;
    public int TotalSamples;
    public DateTimeOffset UpdatedUtc;
    public readonly Dictionary<string, PhaseStat> Phases = new(StringComparer.Ordinal);

    internal sealed class PhaseStat
    {
        public double Ema;
        public double Min = double.MaxValue;
        public double Max;
        public double Last;
        public int Samples;
    }

    /// <summary>
    /// Expected duration of a phase: EMA when we have enough samples,
    /// otherwise the conservative default. Clamped so absurd values can
    /// never stall or race the animation.
    /// </summary>
    public double ExpectedSeconds(string key, double fallback)
    {
        if (Phases.TryGetValue(key, out var stat) && stat.Samples >= 3 && stat.Ema > 0.05)
        {
            return Math.Clamp(stat.Ema, 0.05, fallback * 20.0);
        }
        return fallback;
    }

    public void Record(string key, double seconds)
    {
        if (seconds <= 0.0 || double.IsNaN(seconds) || double.IsInfinity(seconds))
        {
            return;
        }
        seconds = Math.Min(seconds, 600.0); // a stalled phase is not "typical"
        if (!Phases.TryGetValue(key, out var stat))
        {
            stat = new PhaseStat();
            Phases[key] = stat;
        }
        // Outlier damping: a one-off slow start (e.g. first-run AV scan)
        // contributes a fraction of a normal sample instead of dragging the
        // whole estimate up.
        var alpha = stat.Samples >= 4 && seconds > stat.Ema * OutlierFactor
            ? OutlierAlpha
            : EmaAlpha;
        stat.Ema = stat.Samples == 0 ? seconds : stat.Ema + (seconds - stat.Ema) * alpha;
        stat.Min = Math.Min(stat.Min, seconds);
        stat.Max = Math.Max(stat.Max, seconds);
        stat.Last = seconds;
        stat.Samples += 1;
        TotalSamples += 1;
    }

    public static StartupProfile Load(string path)
    {
        var profile = new StartupProfile();
        try
        {
            if (!File.Exists(path))
            {
                return profile;
            }
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.TryGetProperty("schema", out var schema) && schema.GetInt32() != SchemaVersion)
            {
                return profile; // incompatible layout — start fresh
            }
            if (root.TryGetProperty("updated_utc", out var upd) &&
                upd.ValueKind == JsonValueKind.Number &&
                DateTimeOffset.FromUnixTimeSeconds(upd.GetInt64()) < DateTimeOffset.UtcNow - MaxAge)
            {
                return profile; // extremely stale — discard
            }
            profile.AppVersion = root.TryGetProperty("app_version", out var v) ? v.GetString() ?? "" : "";
            profile.CpuCores = root.TryGetProperty("cpu_cores", out var c) ? c.GetInt32() : 0;
            profile.RamGb = root.TryGetProperty("ram_gb", out var r) ? r.GetDouble() : 0;
            profile.TotalSamples = root.TryGetProperty("samples", out var s) ? s.GetInt32() : 0;
            if (root.TryGetProperty("phases", out var phases))
            {
                foreach (var prop in phases.EnumerateObject())
                {
                    var el = prop.Value;
                    var stat = new PhaseStat
                    {
                        Ema = el.TryGetProperty("ema", out var e) ? e.GetDouble() : 0,
                        Min = el.TryGetProperty("min", out var mn) ? mn.GetDouble() : double.MaxValue,
                        Max = el.TryGetProperty("max", out var mx) ? mx.GetDouble() : 0,
                        Last = el.TryGetProperty("last", out var l) ? l.GetDouble() : 0,
                        Samples = el.TryGetProperty("n", out var n) ? n.GetInt32() : 0,
                    };
                    if (stat.Samples > 0 && stat.Ema > 0)
                    {
                        profile.Phases[prop.Name] = stat;
                    }
                }
            }
        }
        catch
        {
            // Corrupt or unreadable profile → sane defaults.
            return new StartupProfile();
        }

        // Compatibility guard: a major/minor version change or a very
        // different machine halves the trust in old timings rather than
        // discarding them outright (patch builds keep full history).
        try
        {
            var current = CurrentVersion();
            if (!SameMajorMinor(profile.AppVersion, current))
            {
                foreach (var stat in profile.Phases.Values)
                {
                    stat.Samples /= 2;
                }
            }
            var cores = Environment.ProcessorCount;
            if (profile.CpuCores > 0 &&
                (profile.CpuCores > cores * 2 || cores > profile.CpuCores * 2))
            {
                foreach (var stat in profile.Phases.Values)
                {
                    stat.Samples /= 2;
                }
            }
        }
        catch { /* advisory only */ }
        return profile;
    }

    public void Save(string path, string appVersion)
    {
        try
        {
            AppVersion = appVersion;
            CpuCores = Environment.ProcessorCount;
            RamGb = TotalRamGb();
            UpdatedUtc = DateTimeOffset.UtcNow;
            var obj = new Dictionary<string, object?>
            {
                ["schema"] = SchemaVersion,
                ["app_version"] = AppVersion,
                ["cpu_cores"] = CpuCores,
                ["ram_gb"] = RamGb,
                ["samples"] = TotalSamples,
                ["updated_utc"] = UpdatedUtc.ToUnixTimeSeconds(),
                ["phases"] = Phases.ToDictionary(
                    kv => kv.Key,
                    kv => (object)new Dictionary<string, object>
                    {
                        ["ema"] = Math.Round(kv.Value.Ema, 3),
                        ["min"] = kv.Value.Min == double.MaxValue ? 0 : Math.Round(kv.Value.Min, 3),
                        ["max"] = Math.Round(kv.Value.Max, 3),
                        ["last"] = Math.Round(kv.Value.Last, 3),
                        ["n"] = kv.Value.Samples,
                    }),
            };
            var json = JsonSerializer.Serialize(obj, new JsonSerializerOptions { WriteIndented = true });
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, json);
            File.Move(tmp, path, overwrite: true);
        }
        catch { /* timing persistence must never break startup */ }
    }

    public static string CurrentVersion()
    {
        try
        {
            var asm = typeof(StartupProfile).Assembly.GetName().Version;
            return asm is null ? "0.0.0" : $"{asm.Major}.{asm.Minor}.{asm.Build}";
        }
        catch { return "0.0.0"; }
    }

    private static bool SameMajorMinor(string a, string b)
    {
        var pa = a.Split('.');
        var pb = b.Split('.');
        return pa.Length >= 2 && pb.Length >= 2 && pa[0] == pb[0] && pa[1] == pb[1];
    }

    private static double TotalRamGb()
    {
        try
        {
            var info = GC.GetGCMemoryInfo();
            if (info.TotalAvailableMemoryBytes > 0)
            {
                return Math.Round(info.TotalAvailableMemoryBytes / 1073741824.0, 1);
            }
        }
        catch { }
        return 0;
    }
}

/// <summary>
/// Canonical startup status vocabulary — the single source of truth for the
/// two-line (PRIMARY / SECONDARY) text shown on BOTH the cinematic splash and
/// the WinForms fallback (both render <see cref="StartupProgress.Primary"/>/
/// <see cref="StartupProgress.Secondary"/>, which is what these keys resolve
/// to). Labels only ever describe work that is actually happening — a key may
/// legitimately never be reported on a given launch.
/// </summary>
internal static class StartupStatus
{
    public static readonly IReadOnlyDictionary<string, (string Primary, string Secondary)> Map =
        new Dictionary<string, (string, string)>(StringComparer.Ordinal)
    {
        ["init"]          = ("INITIALIZING · NEXUS CORE",    "Starting native host and loading configuration"),
        ["restore"]       = ("RESTORING · SYSTEM STATE",     "Loading profiles, settings and protected state"),
        ["brain"]         = ("SYNCHRONIZING · NEXUS BRAIN",  "Restoring memory, knowledge and continuity"),
        ["services"]      = ("STARTING · CORE SERVICES",     "Launching Nexus agent and service runtime"),
        ["models"]        = ("CALIBRATING · MODEL RUNTIME",  "Detecting models, hardware and available resources"),
        ["capabilities"]  = ("VERIFYING · CAPABILITIES",     "Checking tools, permissions and managed services"),
        ["voice"]         = ("INITIALIZING · VOICE SYSTEM",  "Preparing speech and audio services"),
        ["visual"]        = ("CHECKING · VISUAL SYSTEMS",    "Verifying image backends and model availability"),
        ["workspace"]     = ("LOADING · COMMAND INTERFACE",  "Starting the Nexus workspace"),
        ["interface"]     = ("SYNCHRONIZING · INTERFACE",    "Connecting interface to core services"),
        ["online"]        = ("CORE SYSTEMS · ONLINE",        "Nexus Core ready"),
        ["language_core"] = ("ACTIVATING · LANGUAGE CORE",   "Loading the primary conversational model"),
        ["dev_core"]      = ("ACTIVATING · DEVELOPMENT CORE","Loading coding and reasoning runtime"),
        ["model_memory"]  = ("CALIBRATING · MODEL MEMORY",   "Optimizing RAM and VRAM allocation"),
        ["workstation"]   = ("PREPARING · WORKSTATION",      "Building the background tool and model setup plan"),
        ["bg_setup"]      = ("BACKGROUND SETUP · SCHEDULED", "Additional tools and models will continue installing after launch"),
        ["anomaly"]       = ("ANOMALY DETECTED · CORE SERVICES", "Startup verification did not complete"),
        ["containment"]   = ("CONTAINMENT · ENGAGED",        "Isolating the failed subsystem"),
        ["analyzing"]     = ("RECOVERY · ANALYZING",         "Diagnosing startup failure"),
        ["repair"]        = ("REPAIR · IN PROGRESS",         "Attempting automatic recovery"),
        ["lkg"]           = ("RESTORING · LAST KNOWN GOOD",  "Rolling back to a verified system state"),
        ["safemode"]      = ("SAFE MODE · INITIALIZING",     "Starting essential systems only"),
        ["fatal"]         = ("NEXUS CORE · COULD NOT START", "Automatic recovery was unable to restore core services"),
    };

    /// <summary>Resolve a status key; unknown keys pass through as primary text.</summary>
    public static (string Primary, string Secondary) Get(string key) =>
        Map.TryGetValue(key, out var v) ? v : (key, string.Empty);
}

/// <summary>
/// Three-layer startup progress model:
///
///   RealProgress      — last milestone reported by the real system (never
///                       rendered directly; drives phases and readiness)
///   PredictedProgress — where the bar should be given phase elapsed time
///                       vs. the learned/expected phase duration
///   DisplayedProgress — the only value the bar renders: velocity-smoothed,
///                       dt-based, monotonic, ceiling-bounded, <1.0 until
///                       the app is genuinely ready AND the minimum display
///                       time has elapsed.
///
/// The splash may only dismiss when minimum display time AND genuine app
/// readiness are both satisfied — never before 100% is earned, never backward.
/// </summary>
internal sealed class StartupProgress
{
    public static readonly TimeSpan MinimumDisplayTime = TimeSpan.FromSeconds(7);

    private readonly Stopwatch _clock;
    private readonly Func<TimeSpan> _now;
    private readonly StartupProfile _profile;
    private readonly string? _logDir;
    private readonly object _sync = new();

    private double _milestone;          // real progress (highest reported)
    private double _displayed;          // rendered value
    private double _velocity;           // progress-units per second
    private int _phaseIndex;
    private TimeSpan _phaseStart;
    private TimeSpan _lastTick;
    private TimeSpan? _readyAt;         // clock time when AppReady fired
    private double _readyBase;          // displayed value at that instant
    private string _primary = "INITIALIZING · NEXUS CORE";
    private string _phaseSecondary = "Preparing local application environment";
    private DateTimeOffset? _completionStarted;
    private readonly Dictionary<string, double> _phaseDurations = new(StringComparer.Ordinal);
    private bool _diagnosticsWritten;

    /// <summary>How long the READY state + core glow plays before the swap.</summary>
    public static readonly TimeSpan CompletionEffectTime = TimeSpan.FromMilliseconds(850);

    /// <summary>Above this with no milestone movement, the status line admits a slow phase.</summary>
    private static readonly double StallFactor = 2.5;
    private static readonly double StallMinSeconds = 6.0;
    private static readonly double HardStallFactor = 6.0;

    /// <summary>
    /// Startup ladder: the phase key covers [anchor, next anchor).
    /// Each Report() fraction must land on or between these anchors.
    /// </summary>
    private static readonly (double Anchor, string Key)[] Ladder =
    {
        (0.00, "boot"),
        (0.06, "desktop_init"),
        (0.15, "backend_launch"),
        (0.30, "backend_health"),
        (0.55, "runtime_sync"),
        (0.72, "webview_init"),
        (0.85, "interface_nav"),
        (0.93, "interface_ready"),
    };

    /// <summary>Conservative first-run estimates, replaced by the profile's EMA.</summary>
    private static readonly Dictionary<string, double> DefaultPhaseSeconds = new(StringComparer.Ordinal)
    {
        ["boot"] = 0.4,
        ["desktop_init"] = 0.5,
        ["backend_launch"] = 1.2,
        ["backend_health"] = 4.5,
        ["runtime_sync"] = 1.2,
        ["webview_init"] = 1.0,
        ["interface_nav"] = 0.6,
        ["interface_ready"] = 1.5,
    };

    private static readonly Dictionary<string, string> StallText = new(StringComparer.Ordinal)
    {
        ["backend_launch"] = "Backend startup is taking longer than usual",
        ["backend_health"] = "Still waiting for backend health",
        ["runtime_sync"] = "Runtime synchronization is taking longer than usual",
        ["webview_init"] = "Interface initialization is taking longer than usual",
        ["interface_nav"] = "Interface loading is taking longer than usual",
        ["interface_ready"] = "Still waiting for the interface readiness handshake",
    };

    public bool AppReady { get; private set; }
    public bool Failed { get; private set; }
    /// <summary>The only value the progress bar renders.</summary>
    public double DisplayedProgress { get { lock (_sync) { return _displayed; } } }
    /// <summary>Real milestone progress — never rendered directly.</summary>
    public double RealProgress { get { lock (_sync) { return _milestone; } } }
    /// <summary>Predictive target inside the current phase (capped by the phase ceiling).</summary>
    public double PredictedProgress { get { lock (_sync) { return PredictedTarget(); } } }
    /// <summary>Current smoothed velocity, progress-units per second (diagnostics/tests).</summary>
    public double Velocity { get { lock (_sync) { return _velocity; } } }
    public string PhaseKey => Ladder[_phaseIndex].Key;
    public TimeSpan Elapsed => _now();
    public bool MinimumElapsed => Elapsed >= MinimumDisplayTime;
    /// <summary>
    /// Dismiss only when the app is genuinely ready, the minimum display
    /// time has elapsed, AND the bar has smoothly completed to 100%.
    /// </summary>
    public bool ReadyToDismiss => AppReady && MinimumElapsed && _displayed >= 0.9995;
    /// <summary>0→1 while the READY state + shield-core glow plays.</summary>
    public double CompletionPhase =>
        _completionStarted is null ? 0.0 :
        Math.Clamp((DateTimeOffset.Now - _completionStarted.Value) / CompletionEffectTime, 0.0, 1.0);
    public bool CompletionFinished => _completionStarted is not null && CompletionPhase >= 1.0;

    /// <summary>True while a phase is taking well beyond its expected duration.</summary>
    public bool Stalled
    {
        get
        {
            lock (_sync)
            {
                return StalledLevel() > 0;
            }
        }
    }

    /// <summary>
    /// Presentation minimum per status — real milestones can land within
    /// milliseconds of each other and flashing a label nobody can read is
    /// worse than coalescing to the latest one. The newest status always
    /// wins once the hold elapses; readiness/failure overrides still apply.
    /// </summary>
    internal static TimeSpan StatusHold = TimeSpan.FromMilliseconds(350);

    private (string Primary, string Secondary) _shown;
    private TimeSpan _shownAt;

    private (string Primary, string Secondary) RawStatus()
    {
        if (_completionStarted is not null || (AppReady && MinimumElapsed))
        {
            return StartupStatus.Map["online"];
        }
        if (AppReady)
        {
            return ("FINALIZING · NEXUS CORE", "Preparing interface");
        }
        lock (_sync)
        {
            var secondary = StalledLevel() switch
            {
                >= 2 => SecondStallText(),
                1 => StallText.TryGetValue(PhaseKey, out var t) ? t : "Startup is taking longer than usual",
                _ => _phaseSecondary,
            };
            return (_primary, secondary);
        }
    }

    private (string Primary, string Secondary) Status()
    {
        var raw = RawStatus();
        lock (_sync)
        {
            if (raw == _shown) return _shown;
            if (_now() - _shownAt < StatusHold) return _shown;
            _shown = raw;
            _shownAt = _now();
            return _shown;
        }
    }

    /// <summary>PRIMARY · SUBSYSTEM line.</summary>
    public string Primary => Status().Primary;

    /// <summary>Dim secondary explanation line — factual, stall-aware.</summary>
    public string Secondary => Status().Secondary;

    private string SecondStallText()
    {
        return PhaseKey switch
        {
            "backend_launch" => "Still waiting for backend startup",
            "backend_health" => "Still waiting for backend health",
            _ => "Still waiting — Nexus Core is still starting",
        };
    }

    public StartupProgress(
        StartupProfile? profile = null,
        string? logDir = null,
        Func<TimeSpan>? clock = null)
    {
        _profile = profile ?? new StartupProfile();
        _logDir = logDir;
        if (clock is null)
        {
            _clock = Stopwatch.StartNew();
            _now = () => _clock.Elapsed;
        }
        else
        {
            _clock = null!;
            _now = clock;
        }
        _phaseStart = _now();
        _lastTick = _phaseStart;
        _shown = (_primary, _phaseSecondary);
        _shownAt = _phaseStart;
    }

    /// <summary>Report a milestone using a canonical <see cref="StartupStatus"/> key.</summary>
    public void Report(double fraction, string statusKey)
    {
        var (primary, secondary) = StartupStatus.Get(statusKey);
        Report(fraction, primary, secondary);
    }

    public void Report(double fraction, string primary, string secondary)
    {
        lock (_sync)
        {
            var clamped = Math.Clamp(fraction, 0.0, 1.0);
            if (clamped > _milestone)
            {
                _milestone = clamped;
            }
            _primary = primary;
            _phaseSecondary = secondary;

            var index = PhaseIndexFor(clamped);
            if (index > _phaseIndex)
            {
                var now = _now();
                var elapsed = (now - _phaseStart).TotalSeconds;
                var finishedKey = Ladder[_phaseIndex].Key;
                _phaseDurations[finishedKey] = elapsed;
                _profile.Record(finishedKey, elapsed);
                _phaseIndex = index;
                _phaseStart = now;
            }
        }
    }

    public void MarkAppReady()
    {
        lock (_sync)
        {
            AppReady = true;
            var now = _now();
            _readyAt = now;
            _readyBase = _displayed;
            var elapsed = (now - _phaseStart).TotalSeconds;
            var key = Ladder[_phaseIndex].Key;
            _phaseDurations[key] = elapsed;
            _profile.Record(key, elapsed);
            if (_milestone < 1.0)
            {
                _milestone = 1.0;
            }
        }
    }

    /// <summary>Freeze the bar on a confirmed fatal failure — no endless crawl.</summary>
    public void MarkFailed()
    {
        lock (_sync)
        {
            Failed = true;
            _velocity = 0;
        }
    }

    /// <summary>
    /// Both conditions met: start the brief READY + core-glow completion
    /// effect, after which the splash may hand off to the main window.
    /// </summary>
    public void BeginCompletion()
    {
        lock (_sync)
        {
            _completionStarted ??= DateTimeOffset.Now;
            _displayed = 1.0; // bar is already visually complete (≥0.9995)
        }
        WriteDiagnostics();
    }

    /// <summary>Phase index of the ladder anchor at or below fraction.</summary>
    private static int PhaseIndexFor(double fraction)
    {
        var index = 0;
        for (var i = 0; i < Ladder.Length; i++)
        {
            if (fraction >= Ladder[i].Anchor - 1e-9)
            {
                index = i;
            }
        }
        return index;
    }

    private string CurrentPhaseKey() => Ladder[_phaseIndex].Key;

    /// <summary>
    /// The bar may approach the next milestone but never reach it early —
    /// and never reach 1.0 before genuine readiness. During the final
    /// handshake phase and while waiting out the minimum display time it
    /// creeps to 0.985 / 0.992 respectively.
    /// </summary>
    private double PhaseCeiling()
    {
        if (AppReady)
        {
            return MinimumElapsed ? 1.0 : 0.992;
        }
        var next = _phaseIndex + 1 < Ladder.Length
            ? Ladder[_phaseIndex + 1].Anchor
            : 1.0;
        var ceiling = next - 0.008;
        return Math.Min(ceiling, 0.985);
    }

    private double ExpectedPhaseSeconds()
    {
        var key = CurrentPhaseKey();
        var fallback = DefaultPhaseSeconds.TryGetValue(key, out var d) ? d : 1.0;
        return _profile.ExpectedSeconds(key, fallback);
    }

    /// <summary>
    /// Predictive target: asymptotic approach toward the phase ceiling so
    /// the bar keeps moving while waiting, decelerating as expected time is
    /// exceeded. Never below the real milestone, never past the ceiling.
    /// </summary>
    private double PredictedTarget()
    {
        var ceiling = PhaseCeiling();
        if (AppReady)
        {
            if (MinimumElapsed)
            {
                return 1.0;
            }
            // Ready early: keep drifting toward 0.992 across the remaining
            // minimum-display window instead of parking on a frozen bar.
            var wait = Math.Max(0.0, (Elapsed - (_readyAt ?? Elapsed)).TotalSeconds);
            var remaining = Math.Max(0.5,
                (MinimumDisplayTime - (_readyAt ?? Elapsed)).TotalSeconds);
            var drift = Math.Max(0.0, ceiling - _readyBase);
            var target = _readyBase + drift * (1.0 - Math.Exp(-1.6 * wait / remaining));
            return Math.Min(ceiling, Math.Max(_readyBase, target));
        }
        var expected = ExpectedPhaseSeconds();
        var t = Math.Max(0.0, (_now() - _phaseStart).TotalSeconds);
        var gap = Math.Max(0.0, ceiling - _milestone);
        var predicted = _milestone + gap * (1.0 - Math.Exp(-1.6 * t / expected));
        return Math.Min(ceiling, Math.Max(_milestone, predicted));
    }

    /// <summary>0 = normal, 1 = slow, 2 = very slow.</summary>
    private int StalledLevel()
    {
        if (AppReady || Failed)
        {
            return 0;
        }
        var expected = ExpectedPhaseSeconds();
        var t = (_now() - _phaseStart).TotalSeconds;
        if (t >= Math.Max(HardStallFactor * expected, StallMinSeconds * 2))
        {
            return 2;
        }
        if (t >= Math.Max(StallFactor * expected, StallMinSeconds))
        {
            return 1;
        }
        return 0;
    }

    /// <summary>
    /// Advance the displayed bar using real elapsed dt — velocity-smoothed
    /// toward the predictive target, never backward, never past the ceiling.
    /// Frame-rate independent: a delayed timer produces one larger dt step,
    /// not a different trajectory.
    /// </summary>
    public void Tick()
    {
        lock (_sync)
        {
            if (Failed)
            {
                return;
            }

            var now = _now();
            var dt = Math.Clamp((now - _lastTick).TotalSeconds, 0.0, 0.25);
            _lastTick = now;
            if (dt <= 0.0)
            {
                return;
            }

            var target = PredictedTarget();
            var finishing = AppReady && MinimumElapsed;

            // Velocity smoothing: desired speed is distance/response-time;
            // actual velocity chases it with a first-order lag so milestone
            // jumps accelerate (never snap) and approaches decelerate.
            var response = finishing ? 0.14 : 0.40;
            var velTau = finishing ? 0.09 : 0.30;
            if (ReducedMotion())
            {
                response *= 1.8;
                velTau *= 1.6;
            }
            var desiredVelocity = (target - _displayed) / response;
            _velocity += (desiredVelocity - _velocity) * (1.0 - Math.Exp(-dt / velTau));
            _displayed += _velocity * dt;

            // No overshoot, no backward, no early 100%.
            if (_displayed > target)
            {
                _displayed = target;
                _velocity *= 0.15; // bleed residual speed instead of a hard stop
            }
            if (_displayed < 0.0)
            {
                _displayed = 0.0;
            }
            // Visually snap the last invisible epsilon once ready so the bar
            // hits exactly 100% instead of lingering at 99.95%.
            if (finishing && target - _displayed < 0.0005)
            {
                _displayed = 1.0;
            }
        }
    }

    private static bool? _animationsEnabled;
    /// <summary>SPI_GETCLIENTAREAANIMATION — honor reduced-motion by softening the easing.</summary>
    private static bool ReducedMotion()
    {
        if (_animationsEnabled is null)
        {
            var enabled = true;
            try
            {
                var value = 1;
                if (SystemParametersInfo(0x1042, 0, ref value, 0))
                {
                    enabled = value != 0;
                }
            }
            catch { enabled = true; }
            _animationsEnabled = enabled;
        }
        return _animationsEnabled == false;
    }

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool SystemParametersInfo(int uiAction, int uiParam, ref int pvParam, int fWinIni);

    /// <summary>Structured per-phase durations for diagnostics display.</summary>
    public IReadOnlyDictionary<string, double> PhaseDurations
    {
        get { lock (_sync) { return new Dictionary<string, double>(_phaseDurations); } }
    }

    /// <summary>
    /// One-time flush of structured startup timings: [startup] k=v ms lines
    /// in the host log plus data/startup_profile.json persistence. Fires at
    /// completion — never per frame.
    /// </summary>
    private void WriteDiagnostics()
    {
        if (_diagnosticsWritten)
        {
            return;
        }
        _diagnosticsWritten = true;
        try
        {
            Dictionary<string, double> durations;
            double total;
            lock (_sync)
            {
                durations = new Dictionary<string, double>(_phaseDurations);
                total = _now().TotalSeconds;
            }
            var parts = durations
                .OrderBy(kv => Array.FindIndex(Ladder, a => a.Key == kv.Key))
                .Select(kv => $"{kv.Key}={(int)Math.Round(kv.Value * 1000)}ms");
            var line = $"[startup] {string.Join(' ', parts)} total={(int)Math.Round(total * 1000)}ms";
            if (_logDir is not null)
            {
                Directory.CreateDirectory(_logDir);
                File.AppendAllText(Path.Combine(_logDir, "backend-host.log"), $"{DateTimeOffset.Now:O} [STARTUP] {line}{Environment.NewLine}");
            }
            System.Diagnostics.Debug.WriteLine(line);

            var dataDir = _logDir is null ? null : Directory.GetParent(_logDir)?.FullName;
            if (dataDir is not null)
            {
                _profile.Save(Path.Combine(dataDir, "startup_profile.json"), StartupProfile.CurrentVersion());
                File.WriteAllText(
                    Path.Combine(dataDir, "startup_last.json"),
                    JsonSerializer.Serialize(new Dictionary<string, object?>
                    {
                        ["phases_ms"] = durations.ToDictionary(
                            kv => kv.Key, kv => (int)Math.Round(kv.Value * 1000)),
                        ["total_ms"] = (int)Math.Round(total * 1000),
                        ["at_utc"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                    }, new JsonSerializerOptions { WriteIndented = true }));
            }
        }
        catch { /* diagnostics must never break the splash */ }
    }
}
