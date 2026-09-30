using System.Diagnostics;
using System.Net;
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

        try
        {
            if (selfTest)
            {
                using var backend = BackendProcess.Start(appDir);
                backend.WaitUntilHealthyAsync(TimeSpan.FromSeconds(30)).GetAwaiter().GetResult();
                backend.ProbeUiAsync().GetAwaiter().GetResult();
                return 0;
            }

            ApplicationConfiguration.Initialize();
            using var form = new MainForm(appDir);
            Application.Run(form);
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
                    "Chat Nexus startup error",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
            return 1;
        }
    }
}

internal sealed class BackendProcess : IDisposable
{
    private readonly Process _process;
    public int Port { get; }
    public string BaseUrl => $"http://127.0.0.1:{Port}/";

    private BackendProcess(Process process, int port)
    {
        _process = process;
        Port = port;
    }

    public static BackendProcess Start(string appDir)
    {
        var backendExe = Path.Combine(appDir, "backend", "ChatNexus.Backend.exe");
        if (!File.Exists(backendExe))
        {
            throw new FileNotFoundException("Chat Nexus backend executable is missing.", backendExe);
        }

        var workspace = Path.Combine(appDir, "Source");
        if (!File.Exists(Path.Combine(workspace, "localcodeagent", "server.py")))
        {
            workspace = appDir;
        }

        var config = Path.Combine(appDir, "config.json");
        var port = FindFreePort();

        var start = new ProcessStartInfo
        {
            FileName = backendExe,
            WorkingDirectory = appDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        start.ArgumentList.Add("--server");
        start.ArgumentList.Add("--host");
        start.ArgumentList.Add("127.0.0.1");
        start.ArgumentList.Add("--port");
        start.ArgumentList.Add(port.ToString());
        start.ArgumentList.Add("--workspace");
        start.ArgumentList.Add(workspace);
        start.ArgumentList.Add("--config");
        start.ArgumentList.Add(config);

        var process = Process.Start(start)
            ?? throw new InvalidOperationException("Could not start Chat Nexus backend.");
        return new BackendProcess(process, port);
    }

    public async Task WaitUntilHealthyAsync(TimeSpan timeout)
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
        var deadline = DateTime.UtcNow + timeout;
        Exception? last = null;

        while (DateTime.UtcNow < deadline)
        {
            if (_process.HasExited)
            {
                throw new InvalidOperationException(
                    $"Chat Nexus backend exited during startup with code {_process.ExitCode}."
                );
            }

            try
            {
                using var response = await client.GetAsync($"{BaseUrl}api/status");
                if (response.IsSuccessStatusCode)
                {
                    return;
                }
            }
            catch (Exception ex)
            {
                last = ex;
            }

            await Task.Delay(150);
        }

        throw new TimeoutException(
            $"Chat Nexus backend did not become ready within {timeout.TotalSeconds:0} seconds. {last?.Message}"
        );
    }

    public async Task ProbeUiAsync()
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        foreach (var route in new[] { "", "image.html", "research.html" })
        {
            var body = await client.GetStringAsync(BaseUrl + route);
            if (!body.Contains("Chat Nexus", StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException($"UI smoke check failed for /{route}");
            }
        }
    }

    public void Dispose()
    {
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
            _process.Dispose();
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

    public MainForm(string appDir)
    {
        _appDir = appDir;

        Text = "Chat Nexus";
        StartPosition = FormStartPosition.CenterScreen;
        Width = 1440;
        Height = 900;
        MinimumSize = new Size(980, 640);
        BackColor = Color.FromArgb(7, 16, 31);

        var iconPath = Path.Combine(_appDir, "chat-nexus.ico");
        if (File.Exists(iconPath))
        {
            Icon = new Icon(iconPath);
        }

        _webView.Dock = DockStyle.Fill;
        Controls.Add(_webView);

        Shown += async (_, _) => await StartAsync();
        FormClosing += (_, _) => _backend?.Dispose();
    }

    private async Task StartAsync()
    {
        try
        {
            _backend = BackendProcess.Start(_appDir);
            await _backend.WaitUntilHealthyAsync(TimeSpan.FromSeconds(30));

            var userDataFolder = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "ChatNexus",
                "WebView2"
            );
            Directory.CreateDirectory(userDataFolder);

            var environment = await CoreWebView2Environment.CreateAsync(
                browserExecutableFolder: null,
                userDataFolder: userDataFolder
            );

            await _webView.EnsureCoreWebView2Async(environment);
            ConfigureWebView();
            _webView.Source = new Uri(_backend.BaseUrl);
        }
        catch (Exception ex)
        {
            _backend?.Dispose();
            _backend = null;
            MessageBox.Show(
                ex.ToString(),
                "Chat Nexus startup error",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            Close();
        }
    }

    private void ConfigureWebView()
    {
        var core = _webView.CoreWebView2;
        core.Settings.AreDevToolsEnabled = false;
        core.Settings.AreDefaultScriptDialogsEnabled = true;
        core.Settings.IsStatusBarEnabled = false;

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
            // External navigation failure should not crash Chat Nexus.
        }
    }
}
