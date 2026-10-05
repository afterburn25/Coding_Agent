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
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(30) };

    private volatile bool _cancelled;
    private bool _welcomePending;
    private bool _saidOnline;

    public event Action<string>? NarrationStarted;
    public event Action<string>? NarrationEnded;

    public StartupNarrator(string appDir, Func<byte[], string, Task>? playBytes = null)
    {
        _appDir = appDir;
        _playBytes = playBytes;
        var dataDir = Path.Combine(appDir, "data");
        _statePath = Path.Combine(dataDir, "startup-narration.json");
        _cacheDir = Path.Combine(dataDir, "voice", "startup");
    }

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

    private async Task Play(byte[] wav, string key)
    {
        if (_playBytes is not null)
        {
            try { await _playBytes(wav, key); return; }
            catch { /* fall through to SoundPlayer */ }
        }
        await Task.Run(() =>
        {
            var tmp = CachePath(key);
            try
            {
                Directory.CreateDirectory(_cacheDir);
                File.WriteAllBytes(tmp, wav);
                using var player = new System.Media.SoundPlayer(tmp);
                player.PlaySync();
            }
            catch { }
        });
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
    public void Speak(string key, string text, string backendUrl, TimeSpan? deadline = null)
    {
        if (!Enabled || _cancelled) return;
        var cached = Cached(key);
        if (cached is not null)
        {
            _ = DeliverAsync(key, cached);
            return;
        }
        _ = Task.Run(async () =>
        {
            var until = DateTime.UtcNow + (deadline ?? TimeSpan.FromSeconds(90));
            while (!_cancelled && DateTime.UtcNow < until)
            {
                try
                {
                    using var ping = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                    var r = await _http.GetAsync(backendUrl.TrimEnd('/') + "/api/status", ping.Token);
                    if (r.IsSuccessStatusCode)
                    {
                        var wav = await Synthesize(backendUrl, text, CancellationToken.None);
                        if (wav is not null)
                        {
                            Directory.CreateDirectory(_cacheDir);
                            try { File.WriteAllBytes(CachePath(key), wav); } catch { }
                            await DeliverAsync(key, wav);
                        }
                        return;
                    }
                }
                catch { }
                await Task.Delay(1500);
            }
        });
    }

    private async Task DeliverAsync(string key, byte[] wav)
    {
        if (_cancelled) return;
        NarrationStarted?.Invoke(key);
        try { await Play(wav, key); } catch { }
        NarrationEnded?.Invoke(key);
        if (key == "welcome") MarkWelcomePlayed();
    }

    /// <summary>Startup began — welcome on first launch, else the short line.</summary>
    public void StartupBegan(string backendUrl)
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
    public void NearlyReady(string backendUrl)
    {
        if (!Enabled || _cancelled || _welcomePending || _saidOnline) return;
        _saidOnline = true;
        Speak("online", OnlineLine, backendUrl, TimeSpan.FromSeconds(45));
    }

    /// <summary>Fatal startup — supersedes friendly narration immediately.</summary>
    public void Fault(string backendUrl)
    {
        _welcomePending = false;
        NarrationEnded?.Invoke("interrupted");
        Speak("fault", FaultLine, backendUrl, TimeSpan.FromMinutes(4));
    }

    public void Cancel() => _cancelled = true;
}
