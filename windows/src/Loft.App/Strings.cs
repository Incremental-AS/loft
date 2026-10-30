// Every user-facing string. The words live in Strings/<language>.json (en is the source); this class
// only names them, so callers never branch on language. Wording rules and how to add a language:
// Strings/README.md.
using System.Globalization;
using Loft.Core;

namespace Loft.App;

public static class S
{
    private static readonly Localizer Default = Localizer.For(typeof(S).Assembly, CultureInfo.CurrentUICulture);
    private static readonly AsyncLocal<Localizer?> Override = new();

    private static Localizer L => Override.Value ?? Default;

    /// <summary>The catalog in use, from Windows' display language ("en", "nb", "pt-BR" …).</summary>
    public static string Language => L.Language;

    /// <summary>Tests: use <paramref name="localizer"/> on this async flow until disposed.</summary>
    internal static IDisposable Use(Localizer localizer)
    {
        var before = Override.Value;
        Override.Value = localizer;
        return new Restore(() => Override.Value = before);
    }

    private sealed class Restore(Action undo) : IDisposable
    {
        public void Dispose() => undo();
    }

    private static string T(string key, params (string, object?)[] args) => L.Text(key, args);

    public const string Product = "Loft";

    // Tray
    public static string UpToDate(Route r, bool atHome) => T("tray.up_to_date", ("route", RouteName(r, atHome)));
    public static string Syncing(int n, Route r, bool atHome) => L.Plural("tray.syncing", n, ("route", RouteName(r, atHome)));
    public static string Waiting(int n) => L.Plural("tray.waiting", n);
    public static string Paused(int n) => T("tray.paused", ("waiting", Waiting(n)));
    public static string CantReachHome => T("tray.cant_reach_home");
    public static string CantReachNas => T("tray.cant_reach_nas");
    public static string RouteName(Route r, bool atHome) =>
        r == Route.Lan && atHome ? T("route.home") : T("route.away");

    public static string Error(SyncError e) => e.Kind switch
    {
        SyncErrorKind.NasPasswordChanged => T("error.nas_password_changed"),
        SyncErrorKind.SignInExpired => T("error.sign_in_expired"),
        SyncErrorKind.DeviceRemoved => T("error.device_removed"),
        SyncErrorKind.NoAccess => T("error.no_access"),
        _ => T("error.stopped"),
    };

    public static string SignInAgain => T("menu.sign_in_again");
    public static string ChooseFolder => T("menu.choose_folder");
    public static string TryAgain => T("menu.try_again");
    public static string OpenInExplorer => T("menu.open_in_explorer");
    public static string PauseSyncing => T("menu.pause_syncing");
    public static string ResumeSyncing => T("menu.resume_syncing");
    public static string Settings => T("menu.settings");
    public static string Quit => T("menu.quit");
    public static string SetUp => T("menu.set_up");
    public static string UpdateAvailable(string v) => T("menu.update_available", ("version", v));

    // Pairing
    public static string PairTitle => T("pair.title");
    public static string PairBody => T("pair.body");
    public static string PairField => T("pair.link_field");
    public static string Continue => T("common.continue");
    public static string SetUpManually => T("pair.set_up_manually");
    public static string ManualBody => T("pair.manual_body");
    public static string AddressField => T("pair.address_field");
    public static string PinField => T("pair.pin_field");
    public static string ReplacePairing(string oldName, string newName) => T("pair.replace", ("old", oldName), ("new", newName));

    // Sign in
    public static string SignInTitle => T("sign_in.title");
    public static string SignInBody => T("sign_in.body");
    public static string Gateway(string name) => T("sign_in.gateway", ("name", name));
    public static string Username => T("sign_in.username");
    public static string Password => T("sign_in.password");
    public static string SignIn => T("sign_in.button");
    public static string AccountHelp => T("sign_in.account_help");
    public static string NotAtHome => T("sign_in.not_at_home");
    public static string WrongPassword => T("sign_in.wrong_password");
    public static string AccountNotAllowed => T("sign_in.account_not_allowed");
    public static string TooManyAttempts => T("sign_in.too_many_attempts");
    public static string SomethingWrong => T("common.something_wrong");

    // Folder
    public static string PickFolderTitle => T("folder.title");
    public static string PickFolderBody => T("folder.body");
    public static string UseThisFolder => T("folder.use_this");
    public static string UseAllShares => T("folder.use_all_shares");
    public static string AllShares => T("folder.all_shares");
    public static string Back => T("common.back");

    // Done
    public static string DoneTitle => T("done.title");
    public static string DoneBody => T("done.body");
    public static string Done => T("done.button");

    // Settings
    public static string GatewayHeading => T("settings.gateway_heading");
    public static string LanAddress(string a) => T("settings.lan_address", ("address", a));
    public static string OutsideAddress(string a, PublicMode m) => m == PublicMode.Direct
        ? T("settings.outside_direct", ("address", a))
        : T("settings.outside_tunnel", ("address", a));
    public static string HomeOnly => T("settings.home_only");
    public static string ForgetGateway => T("settings.forget_gateway");
    public static string AccountHeading => T("settings.account_heading");
    public static string SignedInAs(string u) => T("settings.signed_in_as", ("user", u));
    public static string ThisPc(string d) => T("settings.this_pc", ("device", d));
    public static string SignOut => T("settings.sign_out");
    public static string FolderHeading => T("settings.folder_heading");
    public static string LoftFolder(string p) => T("settings.loft_folder", ("path", p));
    public static string Change => T("settings.change");
    public static string GeneralHeading => T("settings.general_heading");
    public static string CheckForUpdates => T("settings.check_for_updates");
    public static string UpdatesHelp => T("settings.updates_help");
    public static string StartAtSignIn => T("settings.start_at_sign_in");
    public static string StorageHelp => T("settings.storage_help");
    public static string CacheLimit => T("settings.cache_limit");
    public static string CacheHelp => T("settings.cache_help");
    public static string Gb(long bytes) => bytes >= 1L << 30 && bytes % (1L << 30) == 0
        ? T("settings.size_gb", ("n", bytes >> 30))
        : T("settings.size_mb", ("n", bytes >> 20));
    public static string OfficeNote => T("settings.office_note");
    public static string Version(string v) => T("settings.version", ("version", v));
}
