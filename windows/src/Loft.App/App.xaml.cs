using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.Windows.AppLifecycle;

namespace Loft.App;

public partial class App : Application
{
    public static App? Instance { get; private set; }
    private readonly string[] _args;
    private AppController? _controller;

    public App(string[] args)
    {
        _args = args;
        Instance = this;
        InitializeComponent();
        // A tray app: closing the last window must not end the process.
        DispatcherShutdownMode = DispatcherShutdownMode.OnExplicitShutdown;
        UnhandledException += (_, e) =>
        {
            Log.Write($"unhandled: {e.Exception}");
            e.Handled = true;
        };
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _controller = new AppController(DispatcherQueue.GetForCurrentThread());
        _controller.Start(_args);
    }

    /// <summary>A second start handed its activation to us (protocol link, Start menu).</summary>
    public void OnRedirected(AppActivationArguments a)
    {
        string[] args = a.Data switch
        {
            Windows.ApplicationModel.Activation.ILaunchActivatedEventArgs l => SplitArgs(l.Arguments),
            Windows.ApplicationModel.Activation.IProtocolActivatedEventArgs p => new[] { p.Uri.AbsoluteUri },
            _ => Array.Empty<string>(),
        };
        _controller?.Dispatcher.TryEnqueue(() => _controller.HandleArgs(args, fromSecondStart: true));
    }

    internal static string[] SplitArgs(string? commandLine)
    {
        if (string.IsNullOrWhiteSpace(commandLine)) return Array.Empty<string>();
        var list = new List<string>();
        var cur = new System.Text.StringBuilder();
        var quoted = false;
        foreach (var c in commandLine)
        {
            if (c == '"') { quoted = !quoted; continue; }
            if (c == ' ' && !quoted) { if (cur.Length > 0) { list.Add(cur.ToString()); cur.Clear(); } continue; }
            cur.Append(c);
        }
        if (cur.Length > 0) list.Add(cur.ToString());
        return list.ToArray();
    }
}
