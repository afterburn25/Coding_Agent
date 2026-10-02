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
