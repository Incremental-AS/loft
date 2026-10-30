// App state: pairing → sign-in → folder → syncing. Owns the gateway client, the sync engine, the
// tray icon and the daily update check. Everything here runs on the UI thread unless it says so.
using System.Reflection;
using Loft.Core;
using Loft.Sync;
using Loft.Sync.Shell;
using Microsoft.UI.Dispatching;

namespace Loft.App;

public sealed class AppController
{
    public DispatcherQueue Dispatcher { get; }
    public SettingsStore Settings { get; } = new(SettingsStore.DefaultPath);
    public ITokenStore Tokens { get; } = new CredentialTokenStore();
    public GatewayClient? Client { get; private set; }
    public SyncEngine? Engine { get; private set; }
    public ReleaseInfo? Update { get; private set; }
    public static string AppVersion =>
        typeof(AppController).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion?.Split('+')[0] ?? "0.2.0";

    private TrayIcon? _tray;
    private OnboardingWindow? _onboarding;
    private SettingsWindow? _settingsWindow;
    private EngineStatus? _status;
    private string? _trayState;
    private string? _trayTooltip;
    private readonly HttpClient _updateHttp = new() { Timeout = TimeSpan.FromSeconds(30) };
    private DispatcherQueueTimer? _timer;

    public static string ExePath => Environment.ProcessPath ?? Path.Combine(AppContext.BaseDirectory, "Loft.exe");
    public static string Asset(string rel) => Path.Combine(AppContext.BaseDirectory, "Assets", rel);

    static AppController() => SettingsStore.Unreadable = Log.Write;

    public AppController(DispatcherQueue dispatcher) => Dispatcher = dispatcher;

    public void Start(string[] args)
    {
        Log.Write($"Loft {AppVersion} starting ({string.Join(' ', args.Where(a => !a.StartsWith("loft:", StringComparison.OrdinalIgnoreCase)))})");
        try { ShellIntegration.RegisterProtocol(ExePath); }
        catch (Exception e) { Log.Write($"protocol registration: {e.Message}"); }
        _tray = new TrayIcon(TrayIconPath("idle"), S.Product) { Menu = BuildMenu };
        _tray.Error += m => Log.Write("tray: " + m);
        _tray.SessionEnding += () =>
        {
            Log.Write("Windows session ending: saving state");
            Engine?.SaveState();
        };
        _tray.ThemeChanged += () =>
        {
            Log.Write($"taskbar theme changed: {(ShellIntegration.TaskbarIsLight() ? "light" : "dark")}");
            var state = _trayState ?? "idle";
            _trayState = null;
            SetTray(state, _trayTooltip ?? S.Product);
        };

        // --no-update-check: CI smoke runs never call loft.incremental.no.
        if (!args.Contains("--no-update-check"))
        {
            _timer = Dispatcher.CreateTimer();
            _timer.Interval = TimeSpan.FromHours(1);
            _timer.Tick += (_, _) => _ = CheckForUpdateAsync();
            _timer.Start();
            _ = CheckForUpdateAsync();
        }

        BuildClient();
        HandleArgs(args, fromSecondStart: false);
    }

    /// <summary>Command-line arguments from this or a second start: a pairing link, or just "show yourself".</summary>
    public void HandleArgs(string[] args, bool fromSecondStart)
    {
        var link = args.FirstOrDefault(a => a.StartsWith("loft:", StringComparison.OrdinalIgnoreCase)
                                            || a.StartsWith(Pairing.LinkBase, StringComparison.OrdinalIgnoreCase));
        if (link is not null)
        {
            ShowOnboarding(OnboardingWindow.Step.Pair, link);
            return;
        }
        var background = args.Contains("--background");
        switch (NextStep())
        {
            case OnboardingWindow.Step.Done:
                _ = StartEngineAsync();
                if (fromSecondStart) ShellIntegration.OpenInExplorer(Engine?.Folder ?? SyncFolder);
                break;
            case var step:
                if (!background || fromSecondStart) ShowOnboarding(step);
                break;
        }
    }

    /// <summary>Where setup has to continue.</summary>
    public OnboardingWindow.Step NextStep()
    {
        if (Settings.Get(s => s.Pairing) is null) return OnboardingWindow.Step.Pair;
        if (Tokens.Load() is null) return OnboardingWindow.Step.SignIn;
        if (Settings.Get(s => s.Root) is null) return OnboardingWindow.Step.Folder;
        return OnboardingWindow.Step.Done;
    }

    public string SyncFolder => Settings.Get(s => s.SyncFolder) ?? SyncRoot.DefaultFolder;

    /// <summary>(Re)builds the gateway client from the stored pairing and refreshed pins.</summary>
    public void BuildClient()
    {
        var (pairing, pins) = Settings.Get(s => (s.Pairing, s.RefreshedPins.ToList()));
        if (pairing is null) { Client = null; return; }
        Client = GatewayClient.Create(RouteConfig.From(pairing, pins), Tokens);
    }

    public void ShowOnboarding(OnboardingWindow.Step step, string? pairingLink = null)
    {
        if (_onboarding is null)
        {
            _onboarding = new OnboardingWindow(this);
            _onboarding.Closed += (_, _) => _onboarding = null;
        }
        _onboarding.Show(step, pairingLink);
    }

    /// <summary>An open Settings window follows sign-in, sign-out and folder changes made elsewhere.</summary>
    private void RefreshSettingsWindow() => Dispatcher.TryEnqueue(() => _settingsWindow?.Refresh());

    public void ShowSettings()
    {
        if (_settingsWindow is null)
        {
            _settingsWindow = new SettingsWindow(this);
            _settingsWindow.Closed += (_, _) => _settingsWindow = null;
        }
        _settingsWindow.Refresh();
        _settingsWindow.Activate();
    }

    // ── setup steps (called by the onboarding window) ───────────────────

    /// <summary>Stores a new pairing. A different gateway than before signs this PC out of the old one first.</summary>
    public async Task PairAsync(Pairing p)
    {
        var old = Settings.Get(s => s.Pairing);
        if (old is not null && old.Encode() != p.Encode()) await SignOutAsync(forgetPairing: true);
        Settings.Update(s => s.Pairing = p);
        BuildClient();
        Log.Write($"paired with {p.Name} ({(p.Public is null ? "home only" : p.Mode.ToString().ToLowerInvariant())})");
        RefreshSettingsWindow();
    }

    public async Task SignInAsync(string user, string password)
    {
        var c = Client ?? throw new InvalidOperationException("not paired");
        var r = await c.LoginAsync(user, password, Environment.MachineName);
        Tokens.Save(StoredToken.From(r));
        Settings.Update(s => { s.NasUser = r.NasUser ?? user; s.DeviceId = r.DeviceId; s.DeviceName = Environment.MachineName; });
        try
        {
            if (await c.RefreshPinsAsync() is { } pins) Settings.Update(s => s.RefreshedPins = pins.ToList());
        }
        catch (GatewayException e) { Log.Write($"pins refresh after sign-in: {e.Message}"); }
        Log.Write($"signed in as {Settings.Get(s => s.NasUser)} on {c.Selector.Current}");
        RefreshSettingsWindow();
    }

    public async Task SetRootAsync(string share, string path)
    {
        var c = Client ?? throw new InvalidOperationException("not paired");
        var me = await c.SetRootAsync(share, path);
        await StopEngineAsync(removeRoot: true);
        Settings.Update(s => { s.Root = me.Root; s.Anchor = me.Root?.Anchor; s.Uploaded.Clear(); s.PendingDeletes.Clear(); });
        TryDeleteIndex();
        await StartEngineAsync();
        RefreshSettingsWindow();
    }

    // ── engine ──────────────────────────────────────────────────────────

    public async Task StartEngineAsync()
    {
        if (Engine is not null || Client is null) return;
        var root = Settings.Get(s => s.Root);
        if (root is null || Tokens.Load() is null) return;
        var account = Settings.Get(s => s.NasUser) ?? "default";
        var engine = new SyncEngine(Client, Settings, SyncFolder, root, account, Log.Write);
        engine.StatusChanged += st => Dispatcher.TryEnqueue(() => OnStatus(st));
        try
        {
            await engine.StartAsync(Asset("loft.ico"));
            Engine = engine;
            Settings.Update(s => s.SyncFolder = engine.Folder);
            OnStatus(engine.Status);
        }
        catch (Exception e)
        {
            Log.Write($"engine start failed: {e}");
            await engine.DisposeAsync();
            SetTray("error", S.Error(new SyncError(SyncErrorKind.Stopped, "engine_start")));
        }
    }

    public async Task StopEngineAsync(bool removeRoot)
    {
        var e = Engine;
        Engine = null;
        if (e is not null)
        {
            await e.DisposeAsync();
            if (removeRoot) await Task.Run(() => SyncEngine.RemoveSyncRoot(e.Folder, e.SyncRootId, Log.Write));
        }
        else if (removeRoot && Settings.Get(s => s.NasUser) is { } user)
        {
            await Task.Run(() => SyncEngine.RemoveSyncRoot(SyncFolder, SyncRoot.IdFor(user), Log.Write));
        }
    }

    private void TryDeleteIndex()
    {
        try { File.Delete(Path.Combine(Path.GetDirectoryName(SettingsStore.DefaultPath)!, "index.json")); }
        catch (IOException) { }
    }

    /// <summary>Revokes this PC on the gateway (best effort), forgets the token, removes the sync root.</summary>
    public async Task SignOutAsync(bool forgetPairing)
    {
        if (Client is not null && Tokens.Load() is { } t)
        {
            try { await Client.RevokeAsync(t.DeviceId); }
            catch (Exception e) { Log.Write($"revoke on sign-out: {e.Message}"); }
        }
        await StopEngineAsync(removeRoot: true);
        Tokens.Clear();
        Settings.Update(s =>
        {
            s.ClearAccount();
            if (forgetPairing) s.Pairing = null;
        });
        TryDeleteIndex();
        BuildClient();
        SetTray("idle", S.Product);
        Log.Write(forgetPairing ? "signed out, gateway forgotten" : "signed out");
        RefreshSettingsWindow();
    }

    public void TogglePause()
    {
        Settings.Update(s => s.Paused = !s.Paused);
        if (Engine is not null) OnStatus(Engine.Status);
    }

    // ── tray ────────────────────────────────────────────────────────────

    private static string TrayIconPath(string state) =>
        Asset(Path.Combine("tray", $"loft-tray-{state}-{(ShellIntegration.TaskbarIsLight() ? "black" : "white")}.ico"));

    private void SetTray(string state, string tooltip)
    {
        var icon = state != _trayState ? TrayIconPath(state) : null;
        _trayState = state;
        _trayTooltip = tooltip;
        _tray?.Update(icon, tooltip == S.Product ? S.Product : "Loft · " + tooltip);
    }

    private void OnStatus(EngineStatus st)
    {
        _status = st;
        var (state, line) = Describe(st);
        SetTray(state, line);
    }

    /// <summary>Glyph and words for the tray: error > offline > syncing > idle.</summary>
    private static (string state, string line) Describe(EngineStatus st)
    {
        if (st.Loop.Kind == LoopStateKind.Failed) return ("error", S.Error(st.Loop.Error!));
        if (st.Paused) return ("offline", S.Paused(st.Waiting));
        if (st.Loop.Kind == LoopStateKind.CantReachHome) return ("offline", S.CantReachHome);
        if (st.Loop.Kind == LoopStateKind.CantReachNas) return ("offline", S.CantReachNas);
        var busy = st.Uploading + st.Downloading;
        if (busy > 0) return ("syncing", S.Syncing(busy, st.Route, st.AtHome));
        return ("idle", S.UpToDate(st.Route, st.AtHome));
    }

    private IReadOnlyList<TrayMenuItem> BuildMenu()
    {
        var items = new List<TrayMenuItem>();
        var step = NextStep();
        if (step != OnboardingWindow.Step.Done)
        {
            items.Add(new TrayMenuItem(S.SetUp, () => ShowOnboarding(step)));
        }
        else if (_status is { } st)
        {
            var (_, line) = Describe(st);
            items.Add(new TrayMenuItem(line, Enabled: false));
            if (st.Waiting > 0 && !st.Paused) items.Add(new TrayMenuItem(S.Waiting(st.Waiting), Enabled: false));
            if (st.Loop.Kind == LoopStateKind.Failed)
            {
                var e = st.Loop.Error!;
                if (e.NeedsSignIn) items.Add(new TrayMenuItem(S.SignInAgain, () => _ = ReSignInAsync()));
                else if (e.Kind == SyncErrorKind.NoAccess) items.Add(new TrayMenuItem(S.ChooseFolder, () => ShowOnboarding(OnboardingWindow.Step.Folder)));
                else items.Add(new TrayMenuItem(S.TryAgain, () => _ = Engine?.ResyncNowAsync()));
            }
            else
            {
                items.Add(new TrayMenuItem(st.Paused ? S.ResumeSyncing : S.PauseSyncing, TogglePause));
            }
        }
        if (Engine is not null) items.Add(new TrayMenuItem(S.OpenInExplorer, () => ShellIntegration.OpenInExplorer(Engine.Folder)));
        if (Update is { } u)
            items.Add(new TrayMenuItem(S.UpdateAvailable(u.Version), () => ShellIntegration.OpenUrl((u.Url ?? new Uri("https://loft.incremental.no/download/")).AbsoluteUri)));
        items.Add(TrayMenuItem.Separator);
        items.Add(new TrayMenuItem(S.Settings, ShowSettings));
        items.Add(new TrayMenuItem(S.Quit, () => _ = QuitAsync()));
        return items;
    }

    private async Task ReSignInAsync()
    {
        await StopEngineAsync(removeRoot: false);
        Tokens.Clear();
        ShowOnboarding(OnboardingWindow.Step.SignIn);
    }

    private async Task QuitAsync()
    {
        _timer?.Stop();
        await StopEngineAsync(removeRoot: false);
        _tray?.Dispose();
        Microsoft.UI.Xaml.Application.Current.Exit();
    }

    // ── updates ─────────────────────────────────────────────────────────

    private async Task CheckForUpdateAsync()
    {
        try
        {
            var checker = new UpdateChecker(_updateHttp, Settings);
            if (await checker.CheckIfDueAsync(AppVersion) is { } rel)
            {
                Update = rel;
                Log.Write($"update available: {rel.Version}");
            }
        }
        catch (Exception e) { Log.Write($"update check: {e.Message}"); }
    }
}
