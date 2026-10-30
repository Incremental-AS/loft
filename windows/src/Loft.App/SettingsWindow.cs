// Settings: the paired gateway, the account, the Loft folder, and general switches.
using Loft.Core;
using Loft.Sync.Shell;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace Loft.App;

public sealed class SettingsWindow : Window
{
    private readonly AppController _app;
    private readonly StackPanel _root = new() { Spacing = 12, MaxWidth = 560, HorizontalAlignment = HorizontalAlignment.Left };

    public SettingsWindow(AppController app)
    {
        _app = app;
        Title = "Loft · " + S.Settings.TrimEnd('…', ' ');
        SystemBackdrop = new MicaBackdrop();
        AppWindow.SetIcon(AppController.Asset("loft.ico"));
        Sizing.Place(AppWindow, 660, 940);
        Content = new ScrollViewer { Content = _root, Padding = new Thickness(40, 28, 40, 28) };
    }

    private static Style? Res(string key) =>
        Application.Current.Resources.TryGetValue(key, out var v) ? v as Style : null;

    private void Heading(string text) =>
        _root.Children.Add(new TextBlock { Text = text, Style = Res("SubtitleTextBlockStyle"), Margin = new Thickness(0, 12, 0, 0) });

    private void Line(string text, double opacity = 1) =>
        _root.Children.Add(new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, Opacity = opacity });

    public void Refresh()
    {
        _root.Children.Clear();
        var s = _app.Settings;

        Heading(S.GatewayHeading);
        if (s.Get(x => x.Pairing) is { } p)
        {
            Line(p.Name);
            Line(S.LanAddress(p.Lan.GetLeftPart(UriPartial.Authority)), 0.7);
            Line(p.Public is null ? S.HomeOnly : S.OutsideAddress(p.Public.GetLeftPart(UriPartial.Authority), p.Mode), 0.7);
            var forget = new Button { Content = S.ForgetGateway };
            forget.Click += async (_, _) => { await _app.SignOutAsync(forgetPairing: true); Refresh(); _app.ShowOnboarding(OnboardingWindow.Step.Pair); };
            _root.Children.Add(forget);
        }
        else
        {
            var setUp = new Button { Content = S.SetUp };
            setUp.Click += (_, _) => _app.ShowOnboarding(OnboardingWindow.Step.Pair);
            _root.Children.Add(setUp);
        }

        if (s.Get(x => x.NasUser) is { } user && _app.Tokens.Load() is not null)
        {
            Heading(S.AccountHeading);
            Line(S.SignedInAs(user));
            Line(S.ThisPc(s.Get(x => x.DeviceName) ?? Environment.MachineName), 0.7);
            var signOut = new Button { Content = S.SignOut };
            signOut.Click += async (_, _) => { await _app.SignOutAsync(forgetPairing: false); Refresh(); };
            _root.Children.Add(signOut);

            Heading(S.FolderHeading);
            var root = s.Get(x => x.Root);
            Line(S.LoftFolder(root is null ? "–" : root.IsAllShares ? S.AllShares : root.DisplayPath));
            Line(_app.SyncFolder, 0.7);
            var change = new Button { Content = S.Change };
            change.Click += (_, _) => _app.ShowOnboarding(OnboardingWindow.Step.Folder);
            _root.Children.Add(change);
            Line(S.StorageHelp, 0.7);
            var limit = s.Get(x => x.CacheLimitBytes);
            var choices = CachePolicy.Choices.Contains(limit) ? CachePolicy.Choices : CachePolicy.Choices.Append(limit).Order().ToArray();
            var cache = new ComboBox { Header = S.CacheLimit, ItemsSource = choices.Select(S.Gb).ToList(), MinWidth = 140 };
            cache.SelectedIndex = Array.IndexOf(choices, limit);
            cache.SelectionChanged += (_, _) =>
            {
                if (cache.SelectedIndex >= 0) s.Update(x => x.CacheLimitBytes = choices[cache.SelectedIndex]);
            };
            _root.Children.Add(cache);
            Line(S.CacheHelp, 0.7);
        }

        Heading(S.GeneralHeading);
        var updates = new ToggleSwitch { Header = S.CheckForUpdates, IsOn = s.Get(x => x.UpdateCheckEnabled) };
        updates.Toggled += (_, _) => s.Update(x => x.UpdateCheckEnabled = updates.IsOn);
        _root.Children.Add(updates);
        Line(S.UpdatesHelp, 0.7);
        var startup = new ToggleSwitch { Header = S.StartAtSignIn, IsOn = ShellIntegration.StartsAtSignIn() };
        startup.Toggled += (_, _) => ShellIntegration.SetStartAtSignIn(startup.IsOn, AppController.ExePath);
        _root.Children.Add(startup);
        Line(S.OfficeNote, 0.7);
        Line(S.Version(AppController.AppVersion), 0.5);
    }
}
