using ChatNexus.Desktop;

// Behavioral tests for the three-layer splash progress model. Run with:
//   dotnet run -c Release --project desktop/StartupProgress.Tests
// Exits non-zero on the first list of failed checks.

var failures = new List<string>();

void Check(bool condition, string name)
{
    if (!condition)
    {
        failures.Add(name);
        Console.WriteLine($"  FAIL  {name}");
    }
    else
    {
        Console.WriteLine($"  ok    {name}");
    }
}

// ---------------------------------------------------------------- jump
Console.WriteLine("no jump on milestone change");
{
    var r = new Rig();
    r.Progress.Report(0.06, "a", "b");
    r.Progress.Report(0.15, "a", "b");
    r.Progress.Report(0.30, "a", "b");
    r.Advance(6.0, 0.033);
    var before = r.Progress.DisplayedProgress;
    r.Progress.Report(0.55, "a", "b"); // large milestone increase
    r.Advance(0.033, 0.033);
    var after = r.Progress.DisplayedProgress;
    Check(after - before < 0.05, $"0.30→0.55 does not snap ({before:F3}→{after:F3})");
    Check(after >= before, "monotonic across milestone change");

    // Every tick of the transition stays forward-moving and bounded.
    var prev = after;
    var maxStep = 0.0;
    for (var i = 0; i < 90; i++)
    {
        r.Now += 0.033;
        r.Progress.Tick();
        var d = r.Progress.DisplayedProgress;
        Check(d >= prev - 1e-12, "never moves backward");
        maxStep = Math.Max(maxStep, d - prev);
        prev = d;
    }
    Check(maxStep < 0.02, $"per-tick delta stays small ({maxStep:F4}/33ms)");
}

// ---------------------------------------------------------------- ceiling
Console.WriteLine("phase ceiling");
{
    var r = new Rig();
    r.Progress.Report(0.06, "a", "b");
    r.Progress.Report(0.15, "a", "b");
    r.Progress.Report(0.30, "a", "b"); // backend_health phase
    r.Advance(120.0, 0.1); // pathologically slow backend
    var d = r.Progress.DisplayedProgress;
    Check(d < 0.55, $"never crosses next milestone early ({d:F3} < 0.55)");
    Check(d > 0.35, $"keeps creeping while waiting ({d:F3} > 0.35)");
    Check(d < 1.0, "never 100% before readiness");
}

// ---------------------------------------------------------------- monotonic+finish
Console.WriteLine("readiness completion");
{
    var r = new Rig();
    foreach (var m in new[] { 0.06, 0.15, 0.30, 0.55, 0.72, 0.85, 0.93 })
    {
        r.Progress.Report(m, "a", "b");
        r.Advance(0.5, 0.033);
    }
    r.Now = 8.0; // past minimum display time
    var prev = r.Progress.DisplayedProgress;
    Check(prev < 1.0, "still below 100% before ready");
    r.Progress.MarkAppReady();

    var ticksToReady = 0;
    while (!r.Progress.ReadyToDismiss && ticksToReady < 200)
    {
        r.Now += 0.033;
        r.Progress.Tick();
        var d = r.Progress.DisplayedProgress;
        Check(d >= prev - 1e-12, "monotonic during finish");
        prev = d;
        ticksToReady++;
    }
    Check(r.Progress.ReadyToDismiss, "ReadyToDismiss once bar completes");
    Check(r.Progress.DisplayedProgress == 1.0, "finishes at exactly 100%");
    Check(ticksToReady * 0.033 < 2.0, $"finishes promptly ({ticksToReady * 0.033:F2}s)");
}

// ---------------------------------------------------------------- min display
Console.WriteLine("minimum display time");
{
    var r = new Rig();
    r.Progress.Report(0.06, "a", "b");
    r.Progress.Report(0.15, "a", "b");
    r.Progress.Report(0.30, "a", "b");
    r.Progress.Report(0.55, "a", "b");
    r.Now = 3.0; // app ready in 3s — well under the 7s minimum
    r.Progress.MarkAppReady();
    r.Advance(0.5, 0.033);
    Check(!r.Progress.ReadyToDismiss, "not dismissable before 7s minimum");
    Check(r.Progress.DisplayedProgress < 1.0, "bar holds under 100% while waiting");

    while (!r.Progress.ReadyToDismiss && r.Now < 12.0)
    {
        r.Now += 0.033;
        r.Progress.Tick();
    }
    Check(r.Now >= StartupProgress.MinimumDisplayTime.TotalSeconds,
        $"dismissed only after minimum ({r.Now:F2}s)");
    Check(r.Progress.ReadyToDismiss, "dismisses once ready + minimum elapsed");
}

// ---------------------------------------------------------------- long startup
Console.WriteLine("long startup");
{
    var r = new Rig();
    r.Progress.Report(0.06, "a", "b");
    r.Progress.Report(0.15, "a", "b");
    r.Progress.Report(0.30, "a", "b");
    r.Advance(40.0, 0.1); // very slow backend health
    r.Progress.Report(0.55, "a", "b");
    r.Progress.Report(0.72, "a", "b");
    r.Progress.Report(0.85, "a", "b");
    r.Progress.Report(0.93, "a", "b");
    r.Now = 12.0;
    r.Progress.MarkAppReady();
    var readyAt = r.Now;
    while (!r.Progress.ReadyToDismiss && r.Now < readyAt + 5.0)
    {
        r.Now += 0.033;
        r.Progress.Tick();
    }
    Check(r.Progress.ReadyToDismiss, "long startup still completes");
    Check(r.Now - readyAt < 2.5, $"dismisses shortly after readiness ({r.Now - readyAt:F2}s)");
}

// ---------------------------------------------------------------- deceleration
Console.WriteLine("slow phase decelerates");
{
    var r = new Rig();
    r.Progress.Report(0.06, "a", "b");
    r.Progress.Report(0.15, "a", "b");
    r.Progress.Report(0.30, "a", "b");
    r.Advance(1.0, 0.033); // early in phase: brisk
    var early = r.Progress.Velocity;
    r.Advance(15.0, 0.1); // deep past expected duration
    var late = r.Progress.Velocity;
    Check(late < early * 0.5, $"velocity decays near ceiling ({early:F4} → {late:F4})");
    Check(late > 0.0, "never fully frozen while waiting");
}

// ---------------------------------------------------------------- acceleration
Console.WriteLine("fast milestone accelerates");
{
    var r = new Rig();
    r.Progress.Report(0.06, "a", "b");
    r.Progress.Report(0.15, "a", "b");
    r.Progress.Report(0.30, "a", "b");
    r.Advance(20.0, 0.1); // settled at crawl near ceiling
    var idle = r.Progress.Velocity;
    r.Progress.Report(0.55, "a", "b");
    var v0 = r.Progress.Velocity;
    r.Advance(0.2, 0.033);
    var v1 = r.Progress.Velocity;
    Check(v1 > v0, $"velocity ramps up after milestone ({v0:F4} → {v1:F4})");
    Check(v1 > idle * 3, $"accelerates well above crawl ({idle:F4} → {v1:F4})");
}

// ---------------------------------------------------------------- dt independence
Console.WriteLine("timer-interval independence");
{
    double RunFor(double step)
    {
        var r = new Rig();
        r.Progress.Report(0.06, "a", "b");
        r.Progress.Report(0.15, "a", "b");
        r.Progress.Report(0.30, "a", "b");
        var next = 2.0;
        foreach (var m in new[] { 0.55, 0.72, 0.85, 0.93 })
        {
            while (r.Now < next)
            {
                r.Now += step;
                r.Progress.Tick();
            }
            r.Progress.Report(m, "a", "b");
            next += 1.0;
        }
        while (r.Now < 8.0)
        {
            r.Now += step;
            r.Progress.Tick();
        }
        return r.Progress.DisplayedProgress;
    }
    var d16 = RunFor(0.016);
    var d33 = RunFor(0.033);
    var d50 = RunFor(0.050);
    var d100 = RunFor(0.100);
    var spread = Math.Max(Math.Max(d16, d33), Math.Max(d50, d100))
               - Math.Min(Math.Min(d16, d33), Math.Min(d50, d100));
    Check(spread < 0.05, $"trajectory consistent across dt ({d16:F3}/{d33:F3}/{d50:F3}/{d100:F3})");
}

// ---------------------------------------------------------------- profile: missing
Console.WriteLine("profile fallbacks");
{
    var missing = StartupProfile.Load(Path.Combine(Path.GetTempPath(),
        $"nexus-profile-{Guid.NewGuid():N}.json"));
    Check(missing.ExpectedSeconds("backend_health", 4.5) == 4.5,
        "missing profile uses defaults");

    var bad = Path.Combine(Path.GetTempPath(), $"nexus-profile-{Guid.NewGuid():N}.json");
    File.WriteAllText(bad, "{ not json !!!");
    var corrupted = StartupProfile.Load(bad);
    Check(corrupted.ExpectedSeconds("backend_health", 4.5) == 4.5,
        "corrupted profile falls back to defaults");
    File.Delete(bad);
}

// ---------------------------------------------------------------- profile: outlier
Console.WriteLine("outlier resistance");
{
    var profile = new StartupProfile();
    for (var i = 0; i < 6; i++)
    {
        profile.Record("backend_health", 4.0);
    }
    var before = profile.ExpectedSeconds("backend_health", 4.5);
    profile.Record("backend_health", 60.0); // one pathological startup
    var after = profile.ExpectedSeconds("backend_health", 4.5);
    Check(after < before + 6.0, $"single slow run barely moves EMA ({before:F2} → {after:F2})");
    Check(after > before, "slow run still nudges the estimate");
}

// ---------------------------------------------------------------- profile: roundtrip
Console.WriteLine("profile persistence");
{
    var dir = Path.Combine(Path.GetTempPath(), $"nexus-prof-{Guid.NewGuid():N}");
    Directory.CreateDirectory(dir);
    var path = Path.Combine(dir, "startup_profile.json");
    var profile = new StartupProfile();
    foreach (var s in new[] { 4.0, 4.2, 3.8 })
    {
        profile.Record("backend_health", s);
    }
    profile.Save(path, StartupProfile.CurrentVersion());
    var loaded = StartupProfile.Load(path);
    var learned = loaded.ExpectedSeconds("backend_health", 4.5);
    Check(Math.Abs(learned - 4.0) < 0.5, $"learned timing survives roundtrip ({learned:F2}s)");
    try { Directory.Delete(dir, true); } catch { }
}

// ---------------------------------------------------------------- stall status
Console.WriteLine("stall-aware status");
{
    var r = new Rig();
    r.Progress.Report(0.30, "STARTING · CORE SERVICES", "Waiting for backend health");
    r.Advance(30.0, 0.1);
    Check(r.Progress.Stalled, "stall detected after abnormal wait");
    Check(r.Progress.Secondary.Contains("waiting", StringComparison.OrdinalIgnoreCase)
       || r.Progress.Secondary.Contains("longer", StringComparison.OrdinalIgnoreCase),
        $"status admits slow phase ({r.Progress.Secondary})");
    Check(r.Progress.Primary == "STARTING · CORE SERVICES", "primary stage stays factual");
}

// ---------------------------------------------------------------- failure
Console.WriteLine("failure freezes the bar");
{
    var r = new Rig();
    r.Progress.Report(0.30, "a", "b");
    r.Advance(1.0, 0.033);
    var d = r.Progress.DisplayedProgress;
    r.Progress.MarkFailed();
    r.Advance(5.0, 0.033);
    Check(r.Progress.DisplayedProgress == d, "failed progress does not crawl");
}

// ---------------------------------------------------------------- status vocabulary
Console.WriteLine("canonical startup status vocabulary");
{
    var required = new[]
    {
        "init", "boot", "desktop_init", "restore", "backend_launch",
        "backend_health", "runtime_sync", "brain", "services", "models",
        "capabilities", "voice", "visual", "webview_init", "interface_nav",
        "interface_ready", "interface", "workspace", "finalizing", "online",
        "language_core", "dev_core", "model_memory", "workstation",
        "bg_setup", "anomaly", "containment", "analyzing", "repair",
        "lkg", "safemode", "fatal",
    };
    foreach (var key in required)
    {
        Check(StartupStatus.Map.ContainsKey(key), $"status key '{key}' exists");
        var (p, s) = StartupStatus.Get(key);
        Check(p.Length > 0 && s.Length > 0, $"status '{key}' has both lines");
    }
    Check(StartupStatus.Map["online"].Primary == "CORE SYSTEMS · ONLINE",
        "ready state is CORE SYSTEMS · ONLINE");
    Check(StartupStatus.Map["online"].Secondary == "Nexus Core ready",
        "ready secondary is plain-English");
    Check(StartupStatus.Map["finalizing"].Primary == "FINALIZING · NEXUS CORE",
        "finalizing label is FINALIZING · NEXUS CORE");
    Check(StartupStatus.Map["finalizing"].Secondary == "Verifying interface and system readiness",
        "finalizing secondary");
    Check(StartupStatus.Map["desktop_init"].Primary == "CORE CONTROL · ESTABLISHED",
        "desktop_init label");
    Check(StartupStatus.Map["backend_health"].Primary == "VERIFYING · CORE INTEGRITY",
        "backend_health label");
    Check(StartupStatus.Map["webview_init"].Primary == "OPENING · COMMAND INTERFACE",
        "webview_init label");
    Check(StartupStatus.Map["interface_nav"].Primary == "LOADING · NEXUS WORKSPACE",
        "interface_nav label");
    Check(StartupStatus.Map["interface_ready"].Primary == "SYNCHRONIZING · CORE INTERFACE",
        "interface_ready label");
    Check(StartupStatus.Map["fatal"].Primary == "NEXUS CORE · COULD NOT START",
        "fatal state approved label");
    // Truth audit: no status may claim a heavy service/model is starting
    // or downloading — startup only *checks* those.
    var banned = new[] { "INVOKEAI", "COMFYUI", "DOWNLOAD", "PREVIEW",
        "STARTING INVOKE", "LOADING MODEL WEIGHTS" };
    foreach (var kv in StartupStatus.Map)
    {
        var text = (kv.Value.Primary + " " + kv.Value.Secondary).ToUpperInvariant();
        foreach (var b in banned)
        {
            Check(!text.Contains(b), $"status '{kv.Key}' does not claim '{b}'");
        }
    }
    // Provisioning text exists but only via explicit workstation keys.
    Check(StartupStatus.Map["workstation"].Primary == "PREPARING · WORKSTATION",
        "workstation label present");
    Check(StartupStatus.Map["bg_setup"].Secondary.Contains("continue installing"),
        "background-setup secondary is truthful about post-launch work");
}

// ---------------------------------------------------------------- status key report
Console.WriteLine("status key resolution");
{
    var r = new Rig();
    r.Progress.Report(0.15, "services");
    Check(r.Progress.Primary == "STARTING · CORE SERVICES"
          || r.Progress.Primary == "INITIALIZING · NEXUS CORE",
        $"key-resolved primary ({r.Progress.Primary})");
    r.Advance(1.0, 0.1); // past the coalescing hold
    Check(r.Progress.Primary == "STARTING · CORE SERVICES",
        $"canonical primary after hold ({r.Progress.Primary})");
    Check(r.Progress.Secondary == "Launching Nexus agent and service runtime",
        $"canonical secondary ({r.Progress.Secondary})");
}

// ---------------------------------------------------------------- status coalescing
Console.WriteLine("status coalescing hold");
{
    var r = new Rig();
    r.Progress.Report(0.06, "desktop_init");
    r.Advance(StartupProgress.StatusHold.TotalSeconds + 0.1, 0.05);
    Check(r.Progress.Primary == "CORE CONTROL · ESTABLISHED", "first status shown");
    r.Progress.Report(0.15, "backend_launch");
    r.Advance(0.05, 0.05); // inside the hold — rapid milestone
    Check(r.Progress.Primary == "CORE CONTROL · ESTABLISHED",
        "rapid status change is held");
    r.Progress.Report(0.30, "capabilities");
    r.Advance(StartupProgress.StatusHold.TotalSeconds + 0.1, 0.05);
    Check(r.Progress.Primary == "VERIFYING · CAPABILITIES",
        $"newest status wins after hold ({r.Progress.Primary})");
}

// ---------------------------------------------------------------- ready label
Console.WriteLine("ready label");
{
    var r = new Rig();
    r.Progress.Report(0.93, "a", "b");
    r.Advance(StartupProgress.MinimumDisplayTime.TotalSeconds + 1, 0.1);
    r.Progress.MarkAppReady();
    Check(r.Progress.Primary == "CORE SYSTEMS · ONLINE" ||
          r.Progress.Primary == "FINALIZING · NEXUS CORE",
        $"ready primary ({r.Progress.Primary})");
    // ONLINE only resolves once the host's real flow runs: the bar must
    // finish (ReadyToDismiss) and the completion effect must start —
    // a bare "ready" flag parks on FINALIZING so the label never claims
    // online ahead of the visuals.
    var spins = 0;
    while (!r.Progress.ReadyToDismiss && spins++ < 400) r.Advance(0.05, 0.05);
    Check(r.Progress.ReadyToDismiss, "bar completes to ready-to-dismiss");
    r.Progress.BeginCompletion();
    r.Advance(StartupProgress.StatusHold.TotalSeconds + 0.1, 0.05);
    // Completion started but the cinematic hasn't crossed its online
    // boundary yet — the label must still be FINALIZING, not ONLINE.
    Check(r.Progress.Primary == "FINALIZING · NEXUS CORE",
        $"pre-confirm completion stays FINALIZING ({r.Progress.Primary})");
    Check(r.Progress.Secondary == "Verifying interface and system readiness",
        $"finalizing secondary ({r.Progress.Secondary})");
    r.Progress.ConfirmSequence();
    Check(r.Progress.Primary == "CORE SYSTEMS · ONLINE",
        $"confirmed sequence resolves to CORE SYSTEMS · ONLINE ({r.Progress.Primary})");
    Check(r.Progress.Secondary == "Nexus Core ready",
        $"ready secondary ({r.Progress.Secondary})");
}

// ---------------------------------------------------------------- milestone captions
Console.WriteLine("milestone ladder maps to approved captions");
{
    var expected = new (double At, string Primary)[]
    {
        (0.06, "CORE CONTROL · ESTABLISHED"),
        (0.15, "STARTING · CORE SERVICES"),
        (0.30, "VERIFYING · CORE INTEGRITY"),
        (0.55, "SYNCHRONIZING · NEXUS BRAIN"),
        (0.72, "OPENING · COMMAND INTERFACE"),
        (0.85, "LOADING · NEXUS WORKSPACE"),
        (0.93, "SYNCHRONIZING · CORE INTERFACE"),
    };
    var keys = new[] { "desktop_init", "backend_launch", "backend_health",
        "runtime_sync", "webview_init", "interface_nav", "interface_ready" };
    for (var i = 0; i < expected.Length; i++)
    {
        var r = new Rig();
        r.Progress.Report(expected[i].At, keys[i]);
        r.Advance(StartupProgress.StatusHold.TotalSeconds + 0.1, 0.05);
        Check(r.Progress.Primary == expected[i].Primary,
            $"{expected[i].At:F2} → {expected[i].Primary} (got {r.Progress.Primary})");
    }
    // 0% is INITIALIZING before any milestone reports.
    var fresh = new Rig();
    Check(fresh.Progress.Primary == "INITIALIZING · NEXUS CORE",
        $"0% → INITIALIZING (got {fresh.Progress.Primary})");
}

// ---------------------------------------------------------------- narrator: voice-off gate
Console.WriteLine("narrator gate — no narration means no wait");
{
    var dir = Path.Combine(Path.GetTempPath(), $"nexus-narr-{Guid.NewGuid():N}");
    var n = new StartupNarrator(dir);
    n.QuietBuffer = TimeSpan.FromMilliseconds(100);
    var sw = System.Diagnostics.Stopwatch.StartNew();
    await n.VoiceGateAsync(TimeSpan.FromSeconds(3));
    Check(sw.ElapsedMilliseconds < 1500,
        $"gate passes instantly with no narration ({sw.ElapsedMilliseconds}ms)");
}

// ---------------------------------------------------------------- narrator: end + buffer
Console.WriteLine("narrator gate — holds quiet buffer after real playback end");
{
    var dir = Path.Combine(Path.GetTempPath(), $"nexus-narr-{Guid.NewGuid():N}");
    var n = new StartupNarrator(dir, playBytes: (b, k) => Task.CompletedTask);
    n.QuietBuffer = TimeSpan.FromMilliseconds(200);
    n.SoundFilePlayer = _ => Task.CompletedTask;
    // Simulate a posted line that starts and ends ~now.
    var play = n.Play(new byte[] { 1, 2, 3 }, "online");
    await Task.Delay(30);
    n.NotifyVoiceResult("online", true, 0.05);
    n.NotifyVoiceEnded("online");
    await play;
    var sw = System.Diagnostics.Stopwatch.StartNew();
    await n.VoiceGateAsync(TimeSpan.FromSeconds(5));
    Check(sw.ElapsedMilliseconds >= 150,
        $"gate holds the quiet buffer after playback end ({sw.ElapsedMilliseconds}ms)");
    Check(sw.ElapsedMilliseconds < 2000,
        $"gate does not overstay ({sw.ElapsedMilliseconds}ms)");
}

// ---------------------------------------------------------------- narrator: in-flight delivery
Console.WriteLine("narrator gate — waits for a line still playing");
{
    var dir = Path.Combine(Path.GetTempPath(), $"nexus-narr-{Guid.NewGuid():N}");
    var n = new StartupNarrator(dir, playBytes: (b, k) => Task.CompletedTask);
    n.QuietBuffer = TimeSpan.FromMilliseconds(80);
    n.SoundFilePlayer = _ => Task.CompletedTask;
    var deliver = n.DeliverAsync("welcome", new byte[] { 9, 9 });
    await Task.Delay(30);
    n.NotifyVoiceResult("welcome", true, 0.2); // started; ended deliberately delayed
    var gate = n.VoiceGateAsync(TimeSpan.FromSeconds(5));
    await Task.Delay(150);
    Check(!gate.IsCompleted, "gate waits while playback is unfinished");
    n.NotifyVoiceEnded("welcome");
    await deliver;
    await gate;
    Check(gate.IsCompletedSuccessfully, "gate releases after playback end + buffer");
}

// ---------------------------------------------------------------- narrator: lost ended ack
Console.WriteLine("narrator gate — lost playback-ended ack is bounded");
{
    var dir = Path.Combine(Path.GetTempPath(), $"nexus-narr-{Guid.NewGuid():N}");
    var logs = new List<string>();
    var n = new StartupNarrator(dir, playBytes: (b, k) => Task.CompletedTask, log: logs.Add);
    n.LostEndWatchdogSlack = TimeSpan.FromMilliseconds(60);
    n.SoundFilePlayer = _ => Task.CompletedTask;
    var play = n.Play(new byte[] { 1 }, "initializing");
    await Task.Delay(30);
    n.NotifyVoiceResult("initializing", true, 0.02); // started; no 'ended' ever
    var sw = System.Diagnostics.Stopwatch.StartNew();
    await play; // must return via watchdog, not hang
    Check(sw.ElapsedMilliseconds < 2000,
        $"lost end ack does not hang playback ({sw.ElapsedMilliseconds}ms)");
    Check(logs.Any(l => l.Contains("timed out")), "lost end ack is logged");
}

// ---------------------------------------------------------------- narrator: fault bypass
Console.WriteLine("narrator gate — fault bypasses the gate");
{
    var dir = Path.Combine(Path.GetTempPath(), $"nexus-narr-{Guid.NewGuid():N}");
    var blocker = new TaskCompletionSource<bool>();
    var n = new StartupNarrator(dir,
        playBytes: (b, k) => Task.CompletedTask,
        log: _ => { });
    n.SoundFilePlayer = _ => blocker.Task; // delivery never finishes
    var deliver = n.DeliverAsync("welcome", new byte[] { 5 });
    await Task.Delay(50); // mid-delivery
    n.Fault(() => null);  // startup failure — gate must not wait on friendly audio
    var sw = System.Diagnostics.Stopwatch.StartNew();
    await n.VoiceGateAsync(TimeSpan.FromSeconds(5));
    Check(sw.ElapsedMilliseconds < 1500,
        $"faulted gate returns immediately even mid-delivery ({sw.ElapsedMilliseconds}ms)");
    blocker.TrySetResult(true);
}

// ---------------------------------------------------------------- narrator: voice queue
Console.WriteLine("voice queue — clips serialize, 2s gap between real ends");
{
    var dir = Path.Combine(Path.GetTempPath(), $"nexus-narr-{Guid.NewGuid():N}");
    var sw = System.Diagnostics.Stopwatch.StartNew();
    var starts = new List<(string Key, long Ms)>();
    var n = new StartupNarrator(dir,
        playBytes: (b, k) =>
        {
            lock (starts) starts.Add((k, sw.ElapsedMilliseconds));
            return Task.CompletedTask;
        });
    n.QuietBuffer = TimeSpan.FromMilliseconds(300);
    n.SoundFilePlayer = _ => Task.CompletedTask;
    var d1 = n.DeliverAsync("a", new byte[] { 1 });
    await Task.Delay(30);
    n.NotifyVoiceResult("a", true, 0.01);
    n.NotifyVoiceEnded("a");           // a's playback ends ~t=30-50ms
    await d1;
    var d2 = n.DeliverAsync("b", new byte[] { 2 });
    await Task.Delay(350);             // past a-end + gap: b is dequeued by now
    n.NotifyVoiceResult("b", true, 0.01);
    n.NotifyVoiceEnded("b");
    await d2;
    Check(starts.Select(s => s.Key).SequenceEqual(new[] { "a", "b" }),
        $"queue delivered clips in order: {string.Join(",", starts.Select(s => s.Key))}");
    var bStart = starts.First(s => s.Key == "b").Ms;
    Check(bStart >= 280,
        $"second clip could not start before the quiet gap elapsed ({bStart}ms)");
}

// ---------------------------------------------------------------- narrator: queue FIFO
Console.WriteLine("voice queue — FIFO order under concurrent enqueue");
{
    var dir = Path.Combine(Path.GetTempPath(), $"nexus-narr-{Guid.NewGuid():N}");
    var n = new StartupNarrator(dir);
    var order = new List<string>();
    var t1 = n.EnqueueVoiceAsync(async () => { order.Add("a-start"); await Task.Delay(20); order.Add("a-end"); });
    var t2 = n.EnqueueVoiceAsync(async () => { order.Add("b-start"); await Task.Delay(20); order.Add("b-end"); });
    var t3 = n.EnqueueVoiceAsync(() => { order.Add("c"); return Task.CompletedTask; });
    await Task.WhenAll(t1, t2, t3);
    Check(order.SequenceEqual(new[] { "a-start", "a-end", "b-start", "b-end", "c" }),
        $"queued plays never overlap: {string.Join(",", order)}");
}

// ---------------------------------------------------------------- report
Console.WriteLine();
if (failures.Count > 0)
{
    Console.WriteLine($"{failures.Count} FAILED");
    return 1;
}
Console.WriteLine("all progress-model checks passed");
return 0;

/// <summary>A progress model driven by a manually-advanced fake clock.</summary>
sealed class Rig
{
    public double Now;
    public readonly StartupProgress Progress;
    public Rig()
    {
        Progress = new StartupProgress(new StartupProfile(), null, () => TimeSpan.FromSeconds(Now));
    }
    public double Advance(double seconds, double step)
    {
        var steps = (int)Math.Round(seconds / step);
        for (var i = 0; i < steps; i++)
        {
            Now += step;
            Progress.Tick();
        }
        return Progress.DisplayedProgress;
    }
}
