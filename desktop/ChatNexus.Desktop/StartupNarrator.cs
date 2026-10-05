using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;

namespace ChatNexus.Desktop;

/// <summary>
/// Isabella startup narration. Three lines, per the approved spec:
///   first successful launch — the welcome line (and only that);
///   later launches        — "Nexus Core initializing." then
///                           "Core systems online." near full charge;
///   fatal startup         — the exact containment line, immediately
///                           superseding any friendly narration.
/// Audio is fetched from the backend voice API once it answers; a
/// per-line wav cache means later launches don't wait on synthesis.
/// Narration is strictly asynchronous — it can never delay startup or
/// recovery, and failures degrade to silence.
/// </summary>
internal sealed class StartupNarrator
{
    public const string WelcomeLine =
        "Welcome to Nexus Core. I'm initializing local systems, models, voice, and tools. " +
        "Some capabilities may continue provisioning in the background after startup. " +
        "I'll let you know if anything needs your attention.";
    public const string InitializingLine = "Nexus Core initializing.";
    public const string OnlineLine = "Core systems online.";
    public const string FaultLine =
        "Startup fault detected. Core Destabilization Imminent. " +
        "Core containment engaged. Beginning recovery diagnostics.";

    private readonly string _appDir;
    private readonly string _statePath;
    private readonly string _cacheDir;
    private readonly Func<byte[], string, Task>? _playBytes;   // WebView2 channel
    private readonly Func<DateTimeOffset> _now;
    private readonly Action<string>? _log;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(30) };

    private volatile bool _cancelled;
    private volatile bool _faulted;
    private volatile bool _delivering;
    private volatile int _inflight;
    private DateTimeOffset? _lastPlaybackEnd;
    private bool _welcomePending;
    private bool _saidOnline;

    /// <summary>Quiet buffer after the last real narration playback end.</summary>
    internal TimeSpan QuietBuffer = TimeSpan.FromSeconds(2);
    /// <summary>How long to wait for the splash's playback-started ack.</summary>
    internal TimeSpan StartedAckTimeout = TimeSpan.FromSeconds(5);
    /// <summary>Grace past reported duration for a lost playback-ended event.</summary>
    internal TimeSpan LostEndWatchdogSlack = TimeSpan.FromSeconds(8);
    /// <summary>Injectable for tests — default plays via SoundPlayer.</summary>
    internal Func<string, Task> SoundFilePlayer;

    // key → in-flight playback posted to the splash, completed by the
    // voice-result / voice-ended acks the web layer posts back.
    private sealed class PendingPlayback
    {
        public readonly TaskCompletionSource<(bool Started, double Seconds)> Started =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        public readonly TaskCompletionSource<bool> Ended =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
    }
    private readonly ConcurrentDictionary<string, PendingPlayback> _pending =
        new(StringComparer.Ordinal);

    /// <summary>Splash reports whether audio actually started + its duration.</summary>
    public void NotifyVoiceResult(string key, bool started, double seconds)
    {
        Log($"voice-result '{key}' started={started} secs={seconds:F1}");
        if (_pending.TryGetValue(key, out var p))
        {
            p.Started.TrySetResult((started, seconds));
            if (!started) p.Ended.TrySetResult(false);
        }
    }

    /// <summary>Splash reports the audio really finished playing.</summary>
    public void NotifyVoiceEnded(string key)
    {
        Log($"voice-ended '{key}'");
        if (_pending.TryGetValue(key, out var p))
        {
            p.Ended.TrySetResult(true);
        }
    }

    public event Action<string>? NarrationStarted;
    public event Action<string>? NarrationEnded;

    public StartupNarrator(string appDir, Func<byte[], string, Task>? playBytes = null,
        Func<DateTimeOffset>? clock = null, Action<string>? log = null)
    {
        _appDir = appDir;
        _playBytes = playBytes;
        _now = clock ?? (() => DateTimeOffset.UtcNow);
        _log = log;
        var dataDir = Path.Combine(appDir, "data");
        _statePath = Path.Combine(dataDir, "startup-narration.json");
        _cacheDir = Path.Combine(dataDir, "voice", "startup");
        SoundFilePlayer = path => Task.Run(() =>
        {
            try
            {
                using var player = new System.Media.SoundPlayer(path);
                player.PlaySync();
            }
            catch { }
        });
    }

    private void Log(string line) { try { _log?.Invoke(line); } catch { } }

    // -- settings ------------------------------------------------------------

    private JsonElement LoadConfig()
    {
        try
        {
            var path = Path.Combine(_appDir, "config.json");
            if (File.Exists(path))
                return JsonDocument.Parse(File.ReadAllText(path)).RootElement.Clone();
        }
        catch { }
        return default;
    }

    public bool Enabled
    {
        get
        {
            var cfg = LoadConfig();
            if (cfg.ValueKind != JsonValueKind.Object) return true;
            if (cfg.TryGetProperty("voice_enabled", out var v) && v.ValueKind == JsonValueKind.False) return false;
            if (cfg.TryGetProperty("voice_muted", out v) && v.ValueKind == JsonValueKind.True) return false;
            if (cfg.TryGetProperty("silent_startup", out v) && v.ValueKind == JsonValueKind.True) return false;
            if (cfg.TryGetProperty("safe_mode", out v) && v.ValueKind == JsonValueKind.True) return false;
            if (cfg.TryGetProperty("startup_narration", out v) && v.ValueKind == JsonValueKind.False) return false;
            return true;
        }
    }

    private double Volume
    {
        get
        {
            var cfg = LoadConfig();
            if (cfg.ValueKind == JsonValueKind.Object
                && cfg.TryGetProperty("splash_volume", out var v)
                && v.ValueKind == JsonValueKind.Number)
                return Math.Clamp(v.GetDouble(), 0.0, 1.0);
            return 0.45;
        }
    }

    private bool FirstLaunchPending()
    {
        try
        {
            if (!File.Exists(_statePath)) return true;
            var doc = JsonDocument.Parse(File.ReadAllText(_statePath));
            return !doc.RootElement.TryGetProperty("welcome_played", out var v)
                   || v.ValueKind != JsonValueKind.True;
        }
        catch { return true; }
    }

    private void MarkWelcomePlayed()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_statePath)!);
            File.WriteAllText(_statePath,
                "{\"welcome_played\":true,\"at\":\"" + DateTimeOffset.UtcNow.ToString("o") + "\"}");
        }
        catch { }
    }

    // -- playback ------------------------------------------------------------

    private string CachePath(string key) =>
        Path.Combine(_cacheDir, key + ".wav");

    private byte[]? Cached(string key)
    {
        try
        {
            var p = CachePath(key);
            return File.Exists(p) ? File.ReadAllBytes(p) : null;
        }
        catch { return null; }
    }

    private static async Task<T> AwaitOrTimeout<T>(Task<T> task, TimeSpan timeout, T fallback)
    {
        var done = await Task.WhenAny(task, Task.Delay(timeout));
        return done == task ? await task : fallback;
    }

    /// <summary>
    /// Play a wav. WebView path waits for the splash's real playback
    /// lifecycle — posting play-voice is NOT completion; the voice-ended
    /// ack is. A lost ack is bounded by duration + watchdog slack, then we
    /// proceed (logged). Any channel failure falls back to SoundPlayer,
    /// whose PlaySync return IS the completion signal.
    /// </summary>
    internal async Task Play(byte[] wav, string key)
    {
        var pending = new PendingPlayback();
        _pending[key] = pending;
        var posted = false;
        try
        {
            if (_playBytes is not null)
            {
                await _playBytes(wav, key);
                posted = true;
            }
        }
        catch (Exception ex) { Log($"play '{key}' webview post threw: {ex.Message}"); posted = false; }

        if (posted)
        {
            try
            {
                var started = await AwaitOrTimeout(
                    pending.Started.Task, StartedAckTimeout, (false, 0.0));
                if (started.Item1)
                {
                    var watchdog = TimeSpan.FromSeconds(Math.Max(0.5, started.Item2))
                                   + LostEndWatchdogSlack;
                    if (!await AwaitOrTimeout(pending.Ended.Task, watchdog, false))
                    {
                        Log($"startup voice '{key}' playback-end signal timed out — proceeding");
                    }
                    _lastPlaybackEnd = _now();
                    return;
                }
                // started=false (or ack lost): the splash couldn't play it —
                // fall through to SoundPlayer so the line is still heard.
            }
            finally
            {
                _pending.TryRemove(key, out _);
            }
        }
        else
        {
            _pending.TryRemove(key, out _);
        }

        var tmp = CachePath(key);
        try
        {
            Directory.CreateDirectory(_cacheDir);
            File.WriteAllBytes(tmp, wav);
        }
        catch { }
        Log($"play '{key}' via SoundPlayer fallback");
        await SoundFilePlayer(tmp);
        _lastPlaybackEnd = _now();
    }

    private async Task<byte[]?> Synthesize(string backendUrl, string text, CancellationToken ct)
    {
        try
        {
            var body = JsonSerializer.Serialize(new { text, auto_filter = false });
            using var resp = await _http.PostAsync(
                backendUrl.TrimEnd('/') + "/api/voice/speak",
                new StringContent(body, Encoding.UTF8, "application/json"), ct);
            if (!resp.IsSuccessStatusCode) return null;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
            var rel = doc.RootElement.TryGetProperty("url", out var u) ? u.GetString() : null;
            if (string.IsNullOrEmpty(rel)) return null;
            var audio = await _http.GetByteArrayAsync(backendUrl.TrimEnd('/') + rel);
            return audio.Length > 100 ? audio : null;
        }
        catch { return null; }
    }

    /// <summary>
    /// Speak a line: cached wav → immediate playback; otherwise waits for
    /// the backend's voice API (bounded by deadline), synthesizes, caches,
    /// then plays. Never throws; never blocks the caller.
    /// </summary>
    public void Speak(string key, string text, Func<string?> backendUrl, TimeSpan? deadline = null)
    {
        if (!Enabled || _cancelled) { Log($"speak '{key}' suppressed: enabled={Enabled} cancelled={_cancelled}"); return; }
        var cached = Cached(key);
        if (cached is not null)
        {
            Log($"speak '{key}' delivering from cache ({cached.Length}B)");
            _ = DeliverAsync(key, cached);
            return;
        }
        Log($"speak '{key}' queued — polling backend for synthesis");
        _ = Task.Run(async () =>
        {
            _inflight++;
            try
            {
                var until = DateTime.UtcNow + (deadline ?? TimeSpan.FromSeconds(90));
                while (!_cancelled && DateTime.UtcNow < until)
                {
                    try
                    {
                        // Resolved per attempt — the caller's URL factory
                        // returns null until the backend exists and then the
                        // real (random) port. A URL captured at StartupBegan
                        // time pointed at nothing and the line never played.
                        var baseUrl = backendUrl()?.TrimEnd('/');
                        if (string.IsNullOrEmpty(baseUrl))
                        {
                            await Task.Delay(1500);
                            continue;
                        }
                        using var ping = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                        var r = await _http.GetAsync(baseUrl + "/api/status", ping.Token);
                        if (r.IsSuccessStatusCode)
                        {
                            // One-shot synthesis was the old behavior — if
                            // speak 503'd while the voice engine warmed, the
                            // line was lost forever. Retry until the deadline.
                            var wav = await Synthesize(baseUrl, text, CancellationToken.None);
                            if (wav is not null)
                            {
                                Directory.CreateDirectory(_cacheDir);
                                try { File.WriteAllBytes(CachePath(key), wav); } catch { }
                                await DeliverAsync(key, wav);
                                return;
                            }
                        }
                    }
                    catch { }
                    await Task.Delay(1500);
                }
            }
            catch { /* narration must never take down startup */ }
            finally { _inflight--; }
        });
    }

    internal async Task DeliverAsync(string key, byte[] wav)
    {
        // A synthesized friendly line still deserves delivery after the
        // ordinary completion cancel — the SoundPlayer fallback covers a
        // splash that's already gone. A declared fault silences friendly
        // lines — but the fault line itself must still be heard.
        if (_faulted && key != "fault") return;
        _delivering = true;
        NarrationStarted?.Invoke(key);
        try { await Play(wav, key); } catch (Exception ex) { Log($"play '{key}' failed: {ex.GetType().Name}"); }
        finally { _delivering = false; }
        NarrationEnded?.Invoke(key);
        if (key == "welcome") MarkWelcomePlayed();
    }

    /// <summary>
    /// The startup voice gate: hold the splash until every narration that
    /// actually started playing has finished, PLUS the quiet buffer after
    /// the final end. Lines still polling a backend that never answered
    /// cannot hold the gate — they die with the splash's own cancel.
    /// Nothing here can stall startup past `bound`, and a declared fault
    /// bypasses the gate entirely.
    /// </summary>
    public async Task VoiceGateAsync(TimeSpan bound)
    {
        var deadline = _now() + bound;
        while (_now() < deadline)
        {
            if (_faulted)
            {
                return; // recovery never waits on friendly narration
            }
            // _inflight covers a scheduled line still being fetched/
            // synthesized — cancelling it before it ever plays would lose
            // "Core systems online." even though it was coming.
            if (_delivering || _inflight > 0)
            {
                await Task.Delay(50);
                continue;
            }
            var end = _lastPlaybackEnd;
            if (end is null || _faulted)
            {
                return; // nothing actually played — no quiet-buffer penalty
            }
            var remaining = end.Value + QuietBuffer - _now();
            if (remaining <= TimeSpan.Zero)
            {
                return;
            }
            // A narration that sneaks in during the buffer re-arms the loop.
            await Task.Delay(remaining);
        }
    }

    /// <summary>Startup began — welcome on first launch, else the short line.</summary>
    public void StartupBegan(Func<string?> backendUrl)
    {
        if (!Enabled || _cancelled) return;
        if (FirstLaunchPending())
        {
            _welcomePending = true;
            Speak("welcome", WelcomeLine, backendUrl, TimeSpan.FromMinutes(3));
        }
        else
        {
            Speak("initializing", InitializingLine, backendUrl, TimeSpan.FromSeconds(60));
        }
    }

    /// <summary>Backend nearly/fully ready — the charge-complete line.</summary>
    public void NearlyReady(Func<string?> backendUrl)
    {
        if (!Enabled || _cancelled || _welcomePending || _saidOnline)
        {
            Log($"nearly-ready skipped: enabled={Enabled} cancelled={_cancelled} welcomePending={_welcomePending} saidOnline={_saidOnline}");
            return;
        }
        _saidOnline = true;
        Speak("online", OnlineLine, backendUrl, TimeSpan.FromSeconds(45));
    }

    /// <summary>Fatal startup — supersedes friendly narration immediately.</summary>
    public void Fault(Func<string?> backendUrl)
    {
        _faulted = true;
        _welcomePending = false;
        NarrationEnded?.Invoke("interrupted");
        Speak("fault", FaultLine, backendUrl, TimeSpan.FromMinutes(4));
    }

    public void Cancel() => _cancelled = true;

    /// <summary>Spoken first on close — the user's requested line.</summary>
    public const string ShutdownLine = "Shutting down the core.";

    /// <summary>
    /// Shutdown narration: speak ShutdownLine, then the persona farewell
    /// from /api/profiles/{pid}/farewell. Playback runs on the host
    /// SoundPlayer so completion is a real PlaySync return — the caller
    /// holds the window open until this task finishes. Bounded: a dead
    /// backend or failed synth simply skips that line; only a line
    /// already playing keeps the close waiting, which is the point.
    /// </summary>
    public async Task FarewellAsync(Func<string?> backendUrl, TimeSpan bound)
    {
        if (!Enabled) return;
        var deadline = DateTime.UtcNow + bound;
        var url = backendUrl()?.TrimEnd('/');
        if (string.IsNullOrEmpty(url)) return;
        try
        {
            var wav = await Synthesize(url, ShutdownLine, CancellationToken.None);
            if (wav is not null)
            {
                var tmp = CachePath("shutdown");
                try { Directory.CreateDirectory(_cacheDir); File.WriteAllBytes(tmp, wav); } catch { }
                Log("farewell 'shutdown' via SoundPlayer");
                await SoundFilePlayer(tmp);
            }
            if (DateTime.UtcNow >= deadline) return;

            // Persona goodbye — the backend renders style-aware text and
            // synthesizes it; the host just needs the wav to play.
            var pid = await ActiveProfileId(url);
            if (pid is null) return;
            using var resp = await _http.GetAsync($"{url}/api/profiles/{pid}/farewell");
            if (!resp.IsSuccessStatusCode) return;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            var rel = doc.RootElement.TryGetProperty("voice_url", out var u) ? u.GetString() : null;
            if (string.IsNullOrEmpty(rel)) return;
            var audio = await _http.GetByteArrayAsync(url + rel);
            if (audio.Length <= 100) return;
            var tmp2 = CachePath("farewell");
            try { File.WriteAllBytes(tmp2, audio); } catch { }
            Log("farewell 'goodbye' via SoundPlayer");
            await SoundFilePlayer(tmp2);
        }
        catch { /* farewell is best-effort — never traps the exit */ }
    }

    private async Task<string?> ActiveProfileId(string baseUrl)
    {
        try
        {
            using var r = await _http.GetAsync($"{baseUrl}/api/profiles");
            if (!r.IsSuccessStatusCode) return null;
            using var doc = JsonDocument.Parse(await r.Content.ReadAsStringAsync());
            return doc.RootElement.TryGetProperty("active", out var a)
                ? a.GetString() : null;
        }
        catch { return null; }
    }
}
