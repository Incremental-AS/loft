// Small per-user shell bits for an unpackaged app: the loft:// protocol, start at sign-in,
// the taskbar theme (white or black tray icons), and opening Explorer.
using System.Diagnostics;
using System.Runtime.Versioning;
using Microsoft.Win32;

namespace Loft.Sync.Shell;

[SupportedOSPlatform("windows")]
public static class ShellIntegration
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";

    /// <summary>
    /// Registers <c>loft:</c> for this user (HKCU\Software\Classes), so <c>loft://pair#…</c> links open Loft.
    /// <paramref name="scheme"/> is a parameter only so tests can use their own.
    /// </summary>
    public static void RegisterProtocol(string exePath, string scheme = "loft")
    {
        using var root = Registry.CurrentUser.CreateSubKey($@"Software\Classes\{scheme}");
        root.SetValue("", "URL:Loft");
        root.SetValue("URL Protocol", "");
        using (var icon = root.CreateSubKey("DefaultIcon")) icon.SetValue("", $"\"{exePath}\",0");
        using var cmd = root.CreateSubKey(@"shell\open\command");
        cmd.SetValue("", $"\"{exePath}\" \"%1\"");
    }

    public static string? ProtocolCommand(string scheme = "loft")
    {
        using var cmd = Registry.CurrentUser.OpenSubKey($@"Software\Classes\{scheme}\shell\open\command");
        return cmd?.GetValue("") as string;
    }

    public static void UnregisterProtocol(string scheme = "loft") =>
        Registry.CurrentUser.DeleteSubKeyTree($@"Software\Classes\{scheme}", throwOnMissingSubKey: false);

    public static bool StartsAtSignIn(string name = "Loft")
    {
        using var k = Registry.CurrentUser.OpenSubKey(RunKey);
        return k?.GetValue(name) is string;
    }

    public static void SetStartAtSignIn(bool on, string exePath, string name = "Loft")
    {
        using var k = Registry.CurrentUser.CreateSubKey(RunKey);
        if (on) k.SetValue(name, $"\"{exePath}\" --background");
        else k.DeleteValue(name, throwOnMissingValue: false);
    }

    /// <summary>True when the taskbar is light (use the black tray icons).</summary>
    public static bool TaskbarIsLight()
    {
        using var k = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
        return k?.GetValue("SystemUsesLightTheme") is int v && v == 1;
    }

    public static void OpenInExplorer(string path) =>
        Process.Start(new ProcessStartInfo("explorer.exe", $"\"{path}\"") { UseShellExecute = false });

    public static void OpenUrl(string url) =>
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
}
