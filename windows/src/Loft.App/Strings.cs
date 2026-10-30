// Every user-facing string, English and Norwegian (bokmål). Wording follows the Mac app's
// (apple/App/Copy.swift) with "this PC" and Explorer for "this Mac" and Finder. Rules: sentence
// case, name the place ("home", "the NAS", "this PC"), never call Loft "cloud", and in Norwegian a
// capital letter after every colon (not in times or URLs).
using System.Globalization;
using Loft.Core;

namespace Loft.App;

public static class S
{
    public static bool No { get; } =
        CultureInfo.CurrentUICulture.TwoLetterISOLanguageName is "nb" or "nn" or "no";

    private static string T(string en, string no) => No ? no : en;

    public const string Product = "Loft";

    // Tray
    public static string UpToDate(Route r, bool atHome) => T($"Up to date · {RouteName(r, atHome)}", $"Alt er synkronisert · {RouteName(r, atHome)}");
    public static string Syncing(int n, Route r, bool atHome) => No
        ? $"Synkroniserer {n} {(n == 1 ? "fil" : "filer")} · {RouteName(r, atHome)}"
        : $"Syncing {n} {(n == 1 ? "file" : "files")} · {RouteName(r, atHome)}";
    public static string Waiting(int n) => No
        ? $"{n} {(n == 1 ? "endring venter" : "endringer venter")} på opplasting"
        : $"{n} {(n == 1 ? "change" : "changes")} waiting to upload";
    public static string Paused(int n) => T($"Paused · {Waiting(n)}", $"På pause · {Waiting(n)}");
    public static string CantReachHome => T("Can't reach home. Retrying…", "Får ikke kontakt med hjemmenettet. Prøver igjen …");
    public static string CantReachNas => T("Can't reach the NAS. Retrying…", "Får ikke kontakt med NAS-en. Prøver igjen …");
    public static string RouteName(Route r, bool atHome) =>
        r == Route.Lan && atHome ? T("Home", "Hjemme") : T("Away", "Borte");

    public static string Error(SyncError e) => e.Kind switch
    {
        SyncErrorKind.NasPasswordChanged => T("Sign in again · NAS password changed", "Logg inn igjen · Passordet på NAS-en er endret"),
        SyncErrorKind.SignInExpired => T("Sign in again · Sign-in expired", "Logg inn igjen · Innloggingen har gått ut"),
        SyncErrorKind.DeviceRemoved => T("Signed out · This PC was removed from Loft", "Logget ut · Denne PC-en er fjernet fra Loft"),
        SyncErrorKind.NoAccess => T("No access · Loft folder unavailable", "Ingen tilgang · Loft-mappen er ikke tilgjengelig"),
        _ => T("Sync stopped · Something went wrong", "Synkroniseringen stoppet · Noe gikk galt"),
    };

    public static string SignInAgain => T("Sign in…", "Logg inn …");
    public static string ChooseFolder => T("Choose folder…", "Velg mappe …");
    public static string TryAgain => T("Try again", "Prøv igjen");
    public static string OpenInExplorer => T("Open Loft in Explorer", "Åpne Loft i Utforsker");
    public static string PauseSyncing => T("Pause syncing", "Sett synkroniseringen på pause");
    public static string ResumeSyncing => T("Resume syncing", "Fortsett synkroniseringen");
    public static string Settings => T("Settings…", "Innstillinger …");
    public static string Quit => T("Quit Loft", "Avslutt Loft");
    public static string SetUp => T("Set up Loft…", "Sett opp Loft …");
    public static string UpdateAvailable(string v) => T($"Loft {v} is available…", $"Loft {v} er tilgjengelig …");

    // Pairing
    public static string PairTitle => T("Pair with your gateway", "Par med gatewayen din");
    public static string PairBody => T(
        "On the gateway, run loft-gateway pair. Open the link it shows on this PC, or paste it here.",
        "Kjør loft-gateway pair på gatewayen. Åpne lenken den viser på denne PC-en, eller lim den inn her.");
    public static string PairField => T("Pairing link", "Paringslenke");
    public static string Continue => T("Continue", "Fortsett");
    public static string SetUpManually => T("Set up manually", "Sett opp manuelt");
    public static string ManualBody => T(
        "Type the gateway's address and one of its key fingerprints. loft-gateway pair shows both. The fingerprint is how Loft checks it's really your gateway, so there's no setup without it.",
        "Skriv inn adressen til gatewayen og ett av fingeravtrykkene dens. Du finner begge i det loft-gateway pair viser. Med fingeravtrykket sjekker Loft at det virkelig er din gateway, så uten det kommer du ikke videre.");
    public static string AddressField => T("Address, like nas-gateway.local:8443", "Adresse, for eksempel nas-gateway.local:8443");
    public static string PinField => T("Key fingerprint", "Fingeravtrykk");
    public static string ReplacePairing(string oldName, string newName) => T(
        $"This PC is paired with {oldName}. Pairing with {newName} signs you out of {oldName} on this PC.",
        $"Denne PC-en er paret med {oldName}. Parer du med {newName}, blir du logget ut av {oldName} på denne PC-en.");

    // Sign in
    public static string SignInTitle => T("Sign in to your NAS", "Logg inn på NAS-en");
    public static string SignInBody => T(
        "Use the username and password you already have on the NAS. Sign in at home the first time, so your password never passes through anyone else's servers. After that, Loft works from anywhere.",
        "Bruk brukernavnet og passordet du allerede har på NAS-en. Logg inn hjemme første gang, så passordet ditt aldri går via andres servere. Etter det virker Loft hvor som helst.");
    public static string Gateway(string name) => T($"Gateway: {name}", $"Gateway: {name}");
    public static string Username => T("NAS username", "Brukernavn på NAS-en");
    public static string Password => T("Password", "Passord");
    public static string SignIn => T("Sign in", "Logg inn");
    public static string AccountHelp => T(
        "Loft shows every shared folder this account can open. No account yet? Ask whoever runs the NAS.",
        "Loft viser alle delte mapper denne kontoen kan åpne. Har du ikke konto ennå? Spør den som drifter NAS-en.");
    public static string NotAtHome => T(
        "You're not at home right now. Connect to the home network for the first sign-in. After that, Loft works from anywhere.",
        "Du er ikke hjemme nå. Koble til hjemmenettet for den første innloggingen. Etter det virker Loft hvor som helst.");
    public static string WrongPassword => T(
        "That username and password didn't work on the NAS. Check them and try again.",
        "Brukernavnet og passordet virket ikke på NAS-en. Sjekk dem og prøv igjen.");
    public static string AccountNotAllowed => T(
        "Loft can't use the NAS's built-in accounts, like admin. Sign in with your own NAS account.",
        "Loft kan ikke bruke de innebygde kontoene på NAS-en, som admin. Logg inn med din egen konto på NAS-en.");
    public static string TooManyAttempts => T("Too many tries. Wait 15 minutes, then try again.", "For mange forsøk. Vent 15 minutter før du prøver igjen.");
    public static string SomethingWrong => T("Something went wrong. Try again.", "Noe gikk galt. Prøv igjen.");

    // Folder
    public static string PickFolderTitle => T("Choose your Loft folder", "Velg Loft-mappen din");
    public static string PickFolderBody => T(
        "This folder on the NAS becomes Loft in Explorer. You can change it later in Settings.",
        "Denne mappen på NAS-en blir Loft i Utforsker. Du kan endre den senere i Innstillinger.");
    public static string UseThisFolder => T("Use this folder", "Bruk denne mappen");
    public static string UseAllShares => T("Use all shared folders", "Bruk alle delte mapper");
    public static string AllShares => T("All your shared folders", "Alle de delte mappene dine");
    public static string Back => T("Back", "Tilbake");

    // Done
    public static string DoneTitle => T("You're all set.", "Alt er klart.");
    public static string DoneBody => T(
        "Loft is in Explorer's navigation pane. Your files show up right away and download when you open them. To keep something on this PC for offline use, right-click it and choose Always keep on this device.",
        "Loft ligger i navigasjonsruten i Utforsker. Filene vises med en gang og lastes ned når du åpner dem. Vil du ha noe på denne PC-en også uten nett, høyreklikker du og velger Behold alltid på denne enheten.");
    public static string Done => T("Done", "Ferdig");

    // Settings
    public static string GatewayHeading => T("Gateway", "Gateway");
    public static string LanAddress(string a) => T($"At home: {a}", $"Hjemme: {a}");
    public static string OutsideAddress(string a, PublicMode m) => m == PublicMode.Direct
        ? T($"Away: {a} (straight to the gateway)", $"Borte: {a} (rett til gatewayen)")
        : T($"Away: {a} (through a tunnel)", $"Borte: {a} (gjennom en tunnel)");
    public static string HomeOnly => T("Away: not set up (home network only)", "Borte: Ikke satt opp (bare hjemmenettet)");
    public static string ForgetGateway => T("Forget this gateway", "Glem denne gatewayen");
    public static string AccountHeading => T("Account", "Konto");
    public static string SignedInAs(string u) => T($"Signed in as {u}", $"Logget inn som {u}");
    public static string ThisPc(string d) => T($"This PC: {d}", $"Denne PC-en: {d}");
    public static string SignOut => T("Sign out", "Logg ut");
    public static string FolderHeading => T("Folder", "Mappe");
    public static string LoftFolder(string p) => T($"Loft folder: {p}", $"Loft-mappe: {p}");
    public static string Change => T("Change…", "Endre …");
    public static string GeneralHeading => T("General", "Generelt");
    public static string CheckForUpdates => T("Check for updates once a day", "Se etter oppdateringer én gang i døgnet");
    public static string UpdatesHelp => T(
        "Loft asks loft.incremental.no if there's a newer version. It sends nothing about you, this PC or your files.",
        "Loft spør loft.incremental.no om det finnes en nyere versjon. Den sender ingenting om deg, denne PC-en eller filene dine.");
    public static string StartAtSignIn => T("Open Loft when I sign in to Windows", "Åpne Loft når jeg logger på Windows");
    public static string StorageHelp => T(
        "Files you keep on this device are never removed. Anything else can be cleared from this PC once it's safely on the NAS.",
        "Filer du beholder på denne enheten, blir aldri fjernet. Alt annet kan fjernes fra PC-en når det er trygt lagret på NAS-en.");
    public static string CacheLimit => T("Space for downloaded files", "Plass til nedlastede filer");
    public static string CacheHelp => T(
        "When downloaded files take up more than this, Loft clears the ones you haven't opened for the longest. They stay on the NAS. Files you keep on this device don't count.",
        "Når nedlastede filer tar mer plass enn dette, fjerner Loft dem du har latt ligge lengst uten å åpne. De ligger fortsatt på NAS-en. Filer du beholder på denne enheten, teller ikke med.");
    public static string Gb(long bytes) => bytes >= 1L << 30 && bytes % (1L << 30) == 0 ? $"{bytes >> 30} GB" : $"{bytes >> 20} MB";
    public static string OfficeNote => T(
        "Office AutoSave only works with OneDrive, so save with Ctrl+S. If something crashes, AutoRecover in Office still helps.",
        "Autolagring i Office virker bare med OneDrive, så lagre med Ctrl+S. Krasjer noe, har du fortsatt Autogjenoppretting i Office.");
    public static string Version(string v) => T($"Loft {v} for Windows", $"Loft {v} for Windows");
}
