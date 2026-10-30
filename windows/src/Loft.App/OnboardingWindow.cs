// First run: pair → sign in → choose folder → done. Built in code (no XAML pages) so the whole
// flow reads top to bottom.
using Loft.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;

namespace Loft.App;

public sealed class OnboardingWindow : Window
{
    public enum Step { Pair, SignIn, Folder, Done }

    private readonly AppController _app;
    private readonly ContentControl _host = new() { HorizontalContentAlignment = HorizontalAlignment.Stretch };
    private Step _step;

    public OnboardingWindow(AppController app)
    {
        _app = app;
        Title = "Loft";
        SystemBackdrop = new MicaBackdrop();
        AppWindow.SetIcon(AppController.Asset("loft.ico"));
        Sizing.Place(AppWindow, 640, 780);
        Content = new ScrollViewer { Content = _host, Padding = new Thickness(40, 32, 40, 32) };
    }

    public void Show(Step step, string? pairingLink = null)
    {
        _step = step;
        _host.Content = step switch
        {
            Step.Pair => PairPage(pairingLink),
            Step.SignIn => SignInPage(),
            Step.Folder => FolderPage(),
            _ => DonePage(),
        };
        Activate();
    }

    // ── building blocks ─────────────────────────────────────────────────

    private static Style? Res(string key) =>
        Application.Current.Resources.TryGetValue(key, out var v) ? v as Style : null;

    private static StackPanel Page(string title, string body)
    {
        var p = new StackPanel { Spacing = 16, MaxWidth = 520, HorizontalAlignment = HorizontalAlignment.Left };
        p.Children.Add(new Image
        {
            Source = new BitmapImage(new Uri(AppController.Asset("AppIcon256.png"))),
            Width = 64, Height = 64, HorizontalAlignment = HorizontalAlignment.Left,
        });
        p.Children.Add(new TextBlock { Text = title, Style = Res("TitleTextBlockStyle"), TextWrapping = TextWrapping.Wrap });
        p.Children.Add(new TextBlock { Text = body, Style = Res("BodyTextBlockStyle"), TextWrapping = TextWrapping.Wrap });
        return p;
    }

    private static InfoBar ErrorBar() => new() { Severity = InfoBarSeverity.Error, IsClosable = false, IsOpen = false };

    private static void ShowError(InfoBar bar, string? message)
    {
        bar.Message = message ?? "";
        bar.IsOpen = !string.IsNullOrEmpty(message);
    }

    private static Button Accent(string text) => new() { Content = text, Style = Res("AccentButtonStyle") };

    // ── pair ────────────────────────────────────────────────────────────

    private UIElement PairPage(string? link)
    {
        var page = Page(S.PairTitle, S.PairBody);
        var field = new TextBox { Header = S.PairField, PlaceholderText = "https://loft.incremental.no/pair#…", Text = link ?? "" };
        var error = ErrorBar();
        var next = Accent(S.Continue);
        var manualPanel = new StackPanel { Spacing = 12, Visibility = Visibility.Collapsed };
        var address = new TextBox { Header = S.AddressField, PlaceholderText = "https://192.0.2.10:8443" };
        var pin = new TextBox { Header = S.PinField, PlaceholderText = "base64 SHA-256 (44 characters)" };
        var manualNext = Accent(S.Continue);
        manualPanel.Children.Add(new TextBlock { Text = S.ManualBody, TextWrapping = TextWrapping.Wrap });
        manualPanel.Children.Add(address);
        manualPanel.Children.Add(pin);
        manualPanel.Children.Add(manualNext);
        var manualToggle = new HyperlinkButton { Content = S.SetUpManually };
        manualToggle.Click += (_, _) => manualPanel.Visibility = manualPanel.Visibility == Visibility.Visible ? Visibility.Collapsed : Visibility.Visible;

        async Task Use(Func<Pairing> make)
        {
            Pairing p;
            try { p = make(); }
            catch (PairingException e) { ShowError(error, e.Message); return; }
            var old = _app.Settings.Get(s => s.Pairing);
            if (old is not null && old.Encode() != p.Encode() && _app.NextStep() != Step.Pair)
            {
                var dlg = new ContentDialog
                {
                    XamlRoot = Content.XamlRoot,
                    Title = S.PairTitle,
                    Content = S.ReplacePairing(old.Name, p.Name),
                    PrimaryButtonText = S.Continue,
                    CloseButtonText = S.Back,
                    DefaultButton = ContentDialogButton.Close,
                };
                if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;
            }
            ShowError(error, null);
            await _app.PairAsync(p);
            Show(Step.SignIn);
        }

        next.Click += async (_, _) => await Use(() => Pairing.Parse(field.Text));
        manualNext.Click += async (_, _) => await Use(() => Pairing.Manual(address.Text, pin.Text));
        page.Children.Add(field);
        page.Children.Add(error);
        page.Children.Add(next);
        page.Children.Add(manualToggle);
        page.Children.Add(manualPanel);
        return page;
    }

    // ── sign in ─────────────────────────────────────────────────────────

    private UIElement SignInPage()
    {
        var page = Page(S.SignInTitle, S.SignInBody);
        if (_app.Settings.Get(s => s.Pairing) is { } p)
            page.Children.Add(new TextBlock { Text = S.Gateway(p.Name), Opacity = 0.7 });
        var user = new TextBox { Header = S.Username, PlaceholderText = "yourname", Text = _app.Settings.Get(s => s.NasUser) ?? "" };
        var pass = new PasswordBox { Header = S.Password };
        var error = ErrorBar();
        var go = Accent(S.SignIn);
        var ring = new ProgressRing { IsActive = false, Width = 20, Height = 20 };
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        row.Children.Add(go);
        row.Children.Add(ring);

        async Task SignIn()
        {
            if (string.IsNullOrWhiteSpace(user.Text) || pass.Password.Length == 0) return;
            go.IsEnabled = false;
            ring.IsActive = true;
            ShowError(error, null);
            try
            {
                await _app.SignInAsync(user.Text.Trim(), pass.Password);
                pass.Password = "";
                Show(Step.Folder);
            }
            catch (GatewayException e)
            {
                ShowError(error, e.Kind switch
                {
                    GatewayErrorKind.SignInNeedsHome or GatewayErrorKind.Unreachable => S.NotAtHome,
                    GatewayErrorKind.Api when e.Status == 401 => S.WrongPassword,
                    GatewayErrorKind.Api when e.Code == "account_not_allowed" => S.AccountNotAllowed,
                    GatewayErrorKind.Api when e.Status == 429 => S.TooManyAttempts,
                    _ => S.SomethingWrong,
                });
                Log.Write($"sign-in failed: {e.Kind} {e.Code}");
            }
            catch (Exception e)
            {
                ShowError(error, S.SomethingWrong);
                Log.Write($"sign-in failed: {e.GetType().Name}");
            }
            finally
            {
                go.IsEnabled = true;
                ring.IsActive = false;
            }
        }

        go.Click += async (_, _) => await SignIn();
        pass.KeyDown += async (_, e) => { if (e.Key == Windows.System.VirtualKey.Enter) await SignIn(); };
        page.Children.Add(user);
        page.Children.Add(pass);
        page.Children.Add(new TextBlock { Text = S.AccountHelp, TextWrapping = TextWrapping.Wrap, Opacity = 0.7 });
        page.Children.Add(error);
        page.Children.Add(row);
        return page;
    }

    // ── folder ──────────────────────────────────────────────────────────

    private UIElement FolderPage()
    {
        var page = Page(S.PickFolderTitle, S.PickFolderBody);
        var where = new TextBlock { Opacity = 0.7 };
        var list = new ListView { SelectionMode = ListViewSelectionMode.None, IsItemClickEnabled = true, MaxHeight = 260 };
        var back = new Button { Content = S.Back, IsEnabled = false };
        var use = Accent(S.UseThisFolder);
        use.IsEnabled = false;
        var all = new Button { Content = S.UseAllShares };
        var error = ErrorBar();
        string? share = null;
        var path = "";

        async Task Load()
        {
            ShowError(error, null);
            try
            {
                var c = _app.Client!;
                if (share is null)
                {
                    list.ItemsSource = (await c.SharesAsync()).ToList();
                    where.Text = "";
                }
                else
                {
                    var f = await c.FoldersAsync(share, path);
                    list.ItemsSource = f.Folders.ToList();
                    where.Text = path.Length == 0 ? share : $"{share}/{path}";
                }
                back.IsEnabled = share is not null;
                use.IsEnabled = share is not null;
            }
            catch (Exception e)
            {
                ShowError(error, S.SomethingWrong);
                Log.Write($"folders: {e.Message}");
            }
        }

        list.ItemClick += async (_, e) =>
        {
            var name = (string)e.ClickedItem;
            if (share is null) share = name;
            else path = path.Length == 0 ? name : $"{path}/{name}";
            await Load();
        };
        back.Click += async (_, _) =>
        {
            if (path.Length == 0) share = null;
            else path = path.Contains('/') ? path[..path.LastIndexOf('/')] : "";
            await Load();
        };

        async Task Choose(string s, string p)
        {
            use.IsEnabled = all.IsEnabled = false;
            try
            {
                await _app.SetRootAsync(s, p);
                Show(Step.Done);
            }
            catch (Exception e)
            {
                ShowError(error, S.SomethingWrong);
                Log.Write($"set root: {e.Message}");
            }
            finally { use.IsEnabled = share is not null; all.IsEnabled = true; }
        }

        use.Click += async (_, _) => { if (share is not null) await Choose(share, path); };
        all.Click += async (_, _) => await Choose(RootInfo.AllShares, "");

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        buttons.Children.Add(back);
        buttons.Children.Add(use);
        buttons.Children.Add(all);
        page.Children.Add(where);
        page.Children.Add(list);
        page.Children.Add(error);
        page.Children.Add(buttons);
        _ = Load();
        return page;
    }

    // ── done ────────────────────────────────────────────────────────────

    private UIElement DonePage()
    {
        var page = Page(S.DoneTitle, S.DoneBody);
        var open = Accent(S.OpenInExplorer);
        open.Click += (_, _) =>
        {
            Loft.Sync.Shell.ShellIntegration.OpenInExplorer(_app.Engine?.Folder ?? _app.SyncFolder);
            Close();
        };
        var done = new Button { Content = S.Done };
        done.Click += (_, _) => Close();
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        row.Children.Add(open);
        row.Children.Add(done);
        page.Children.Add(row);
        return page;
    }
}
