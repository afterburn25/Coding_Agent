using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal static class PlaybackProbe
{
    [STAThread]
    static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new ProbeForm(Path.GetFullPath(args[0])));
    }
}

internal sealed class ProbeForm : Form
{
    private readonly string root;
    private readonly string output;
    private WebView2? web;
    public ProbeForm(string repo)
    {
        root = repo;
        output = Path.Combine(root, "docs", "review", "startup-milestones", "windows-playback-verification.json");
        ClientSize = new Size(1280, 720); ShowInTaskbar = false; Opacity = 0;
        Text = "Nexus Core offline media verification";
        Shown += async (_, _) => await Verify();
    }
    private async Task Verify()
    {
        try
        {
            var windows = new List<object>();
            foreach (var relative in new[] {
                "desktop/ChatNexus.Desktop/splash/assets/NexusCore-Startup-Glow-Only.mp4",
                "docs/reference/NexusCore-Startup-Captioned-Reference.mp4" })
            {
                // Standard Windows Media Player playback engine, independent of
                // Chromium/WebView2. Nothing launches Nexus or touches its data.
                var type = Type.GetTypeFromProgID("WMPlayer.OCX") ?? throw new Exception("Windows Media Player engine unavailable");
                dynamic player = Activator.CreateInstance(type)!;
                try
                {
                    player.settings.autoStart = true; player.settings.mute = true;
                    player.URL = Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar));
                    player.controls.play();
                    for (int i = 0; i < 200 && (player.currentMedia == null || (double)player.currentMedia.duration < 1); i++) await Task.Delay(50);
                    double duration = player.currentMedia.duration;
                    if (Math.Abs(duration - 30) > .05) throw new Exception($"Windows player duration mismatch: {duration}, state={player.playState}, openState={player.openState}, errors={player.error.errorCount}");
                    player.controls.play();
                    for (int i = 0; i < 200 && (double)player.controls.currentPosition < .3; i++) await Task.Delay(50);
                    double position = player.controls.currentPosition;
                    int state = player.playState, errors = player.error.errorCount;
                    if (position < .3 || errors > 0) throw new Exception("Windows Media Player did not play " + relative);
                    windows.Add(new { file = relative, duration, advancedTo = position, playState = state, errors });
                }
                finally { player.close(); System.Runtime.InteropServices.Marshal.FinalReleaseComObject(player); }
            }
            web = new WebView2 { Dock = DockStyle.Fill }; Controls.Add(web);
            var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(root, "tools", "startup_cinematic", "work", "webview-profile"),
                new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required --disable-background-timer-throttling --disable-renderer-backgrounding"));
            await web.EnsureCoreWebView2Async(env);
            web.CoreWebView2.SetVirtualHostNameToFolderMapping("nexus-video.example", root, CoreWebView2HostResourceAccessKind.DenyCors);
            var done = new TaskCompletionSource<JsonElement>();
            web.CoreWebView2.WebMessageReceived += (_, e) => done.TrySetResult(JsonDocument.Parse(e.WebMessageAsJson).RootElement.Clone());
            web.CoreWebView2.Navigate("https://nexus-video.example/tools/startup_cinematic/playback.html");
            var result = await done.Task.WaitAsync(TimeSpan.FromSeconds(60));
            if (!result.GetProperty("ok").GetBoolean()) throw new Exception(result.ToString());
            File.WriteAllText(output, JsonSerializer.Serialize(new {
                success = true, windowsMediaPlayer = windows,
                webView2Version = env.BrowserVersionString, webView2 = result.GetProperty("report")
            }, new JsonSerializerOptions { WriteIndented = true }));
            Environment.ExitCode = 0;
        }
        catch (Exception e)
        {
            File.WriteAllText(output, JsonSerializer.Serialize(new { success = false, error = e.ToString() }, new JsonSerializerOptions { WriteIndented = true }));
            Environment.ExitCode = 1;
        }
        finally { web?.Dispose(); Close(); }
    }
}
