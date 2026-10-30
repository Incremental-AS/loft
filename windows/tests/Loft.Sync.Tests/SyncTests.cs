using System.Text;
using Loft.Core;
using Loft.Sync.Shell;
using Xunit;
using Xunit.Abstractions;

namespace Loft.Sync.Tests;

public class PlatformTests(ITestOutputHelper log)
{
    private bool NotWindows()
    {
        if (OperatingSystem.IsWindowsVersionAtLeast(10, 0, 17763)) return false;
        log.WriteLine("SKIPPED: needs Windows 10 1809 or later");
        return true;
    }

    [Fact]
    public void FileIdentityRoundTripsAndStaysSmall()
    {
        var id = new FileIdentity("0123456789abcdef0123456789abcdef", "c12", "m3");
        var blob = id.Encode();
        Assert.True(blob.Length < 200);
        Assert.Equal(id, FileIdentity.Decode(blob));
        Assert.Null(FileIdentity.Decode(Encoding.UTF8.GetBytes("not json")));
        Assert.Null(FileIdentity.Decode(ReadOnlySpan<byte>.Empty));
    }

    [Fact]
    public void FileTimeFromGatewayNanoseconds()
    {
        var t = new DateTimeOffset(2026, 10, 30, 12, 0, 0, TimeSpan.Zero);
        var ns = t.ToUnixTimeMilliseconds() * 1_000_000;
        Assert.Equal(t.UtcDateTime.ToFileTimeUtc(), Placeholders.FileTime(ns));
    }

    [Fact]
    public void CredentialManagerKeepsTheToken()
    {
        if (NotWindows()) return;
        var store = new CredentialTokenStore("no.incremental.loft/test-" + Guid.NewGuid().ToString("N"));
        try
        {
            Assert.Null(store.Load());
            var t = new StoredToken { Token = "nst_test.secret", DeviceId = "dev", IssuedAt = DateTimeOffset.UtcNow, ExpiresAt = DateTimeOffset.UtcNow.AddDays(90) };
            store.Save(t);
            Assert.Equal(t.Token, store.Load()!.Token);
            store.Save(t with { Token = "nst_test.rotated" });
            Assert.Equal("nst_test.rotated", store.Load()!.Token);
        }
        finally { store.Clear(); }
        Assert.Null(store.Load());
    }

    [Fact]
    public void ProtocolRegistrationPointsAtTheExe()
    {
        if (NotWindows()) return;
        var scheme = "loft-tmp-" + Guid.NewGuid().ToString("N")[..8];
        try
        {
            ShellIntegration.RegisterProtocol(@"C:\Program Files\Loft\Loft.exe", scheme);
            Assert.Equal("\"C:\\Program Files\\Loft\\Loft.exe\" \"%1\"", ShellIntegration.ProtocolCommand(scheme));
        }
        finally { ShellIntegration.UnregisterProtocol(scheme); }
        Assert.Null(ShellIntegration.ProtocolCommand(scheme));
    }
}

/// <summary>
/// A real Cloud Files sync root in %TEMP%, driven by <see cref="InMemoryGateway"/>: listing, hydration,
/// uploads of new and edited files, rename, delete, a change from the NAS, and a conflict.
/// </summary>
public class CloudFilesTests(ITestOutputHelper log) : IAsyncLifetime
{
    private InMemoryGateway _gw = null!;
    private SyncEngine? _engine;
    private string _folder = "", _work = "";
    private GatewayClient _client = null!;
    private readonly List<string> _log = new();

    public async Task InitializeAsync()
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 17763)) return;
        _gw = new InMemoryGateway();
        _gw.AddFile(_gw.RootId, "hello.txt", Encoding.UTF8.GetBytes("hello from the NAS"));
        var big = new byte[5 * 1024 * 1024 + 77];
        new Random(7).NextBytes(big);
        _gw.AddFile(_gw.RootId, "big.bin", big);
        var docs = _gw.AddDir(_gw.RootId, "Docs");
        _gw.AddFile(docs, "inside.txt", Encoding.UTF8.GetBytes("inside"));

        var tag = Guid.NewGuid().ToString("N")[..8];
        _work = Path.Combine(Path.GetTempPath(), "loft-cf-work-" + tag);
        _folder = Path.Combine(Path.GetTempPath(), "LoftTest-" + tag);
        Directory.CreateDirectory(_work);
        var settings = new SettingsStore(Path.Combine(_work, "settings.json"));
        settings.Update(s => s.EvictAfterMinutes = 600);
        var cfg = new RouteConfig(new Uri("https://gw.example:8443"), null, PublicMode.None,
                                  new PinSet(new[] { "ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }));
        var tokens = new MemoryTokenStore(new StoredToken { Token = "t", DeviceId = "d", IssuedAt = DateTimeOffset.UtcNow, ExpiresAt = DateTimeOffset.UtcNow.AddDays(1) });
        _client = new GatewayClient(new RouteSelector(cfg), tokens, _gw) { Probe = _ => Task.FromResult(true) };
        var root = new RootInfo("r1", "home", "", _gw.RootId, "e.1");
        _engine = new SyncEngine(_client, settings, _folder, root, "test-" + tag, l => { lock (_log) _log.Add(l); },
                                 Path.Combine(_work, "index.json"))
        { Debounce = TimeSpan.FromMilliseconds(300) };
        await _engine.StartAsync(Environment.ProcessPath!, "Loft test");
        var rootState = Placeholders.Inspect(_folder);
        log.WriteLine($"sync root registered: {_engine.Registration}; root placeholder={rootState.IsPlaceholder} " +
                      $"attrs=0x{rootState.Attributes:X} insync={rootState.InSync}");
    }

    public async Task DisposeAsync()
    {
        if (_engine is null) return;
        await _engine.DisposeAsync();
        SyncEngine.RemoveSyncRoot(_folder, _engine.SyncRootId, l => log.WriteLine("cleanup: " + l));
        lock (_log) foreach (var l in _log) log.WriteLine("engine: " + l);
        try { Directory.Delete(_work, true); } catch { }
    }

    private static List<string> Cmd(string args)
    {
        var psi = new System.Diagnostics.ProcessStartInfo("cmd.exe", "/c " + args)
        { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        using var p = System.Diagnostics.Process.Start(psi)!;
        var lines = p.StandardOutput.ReadToEnd().Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).ToList();
        p.WaitForExit(30_000);
        return lines;
    }

    private static async Task Eventually(Func<bool> cond, string what, int seconds = 30)
    {
        var until = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < until)
        {
            try { if (cond()) return; } catch (IOException) { }
            await Task.Delay(200);
        }
        Assert.Fail("timed out waiting for: " + what);
    }

    [Fact]
    public async Task EndToEndThroughExplorersApis()
    {
        if (_engine is null) { log.WriteLine("SKIPPED: needs Windows 10 1809 or later"); return; }

        // Listing the folder from another process (as Explorer would) asks Loft for placeholders (FETCH_PLACEHOLDERS).
        var listed = Cmd($"dir /b /a \"{_folder}\"");
        log.WriteLine("dir /b: " + string.Join(", ", listed));
        var names = Directory.EnumerateFileSystemEntries(_folder).Select(Path.GetFileName).OrderBy(n => n).ToList();
        Assert.Equal(new[] { "big.bin", "Docs", "hello.txt" }, names);
        var st = Placeholders.Inspect(Path.Combine(_folder, "hello.txt"));
        Assert.True(st.IsPlaceholder && st.InSync && st.Partial);
        Assert.Equal(_gw.Find(_gw.RootId, "hello.txt")!.Value.Id, st.Identity!.Id);

        // Opening hydrates (FETCH_DATA with ranged downloads).
        Assert.Equal("hello from the NAS", File.ReadAllText(Path.Combine(_folder, "hello.txt")));
        Assert.Equal(_gw.Find(_gw.RootId, "big.bin")!.Value.Data, File.ReadAllBytes(Path.Combine(_folder, "big.bin")));
        // Progressive hydration: the filter may finish marking the file full just after the read returns.
        await Eventually(() => !Placeholders.Inspect(Path.Combine(_folder, "big.bin")).Partial, "big.bin fully on disk", 10);
        // A folder's children are placed when something lists it (Explorer does; a bare path open doesn't).
        Assert.Equal(new[] { "inside.txt" }, Cmd($"dir /b /a \"{Path.Combine(_folder, "Docs")}\""));
        Assert.Equal("inside", File.ReadAllText(Path.Combine(_folder, "Docs", "inside.txt")));

        // "Free up space" (the file becomes unpinned): Loft drops the bytes and keeps the placeholder.
        var bigPath = Path.Combine(_folder, "big.bin");
        Placeholders.SetPin(bigPath, CfPin.Unpinned);
        await Eventually(() => Placeholders.Inspect(bigPath).Partial, "big.bin freed after unpin");
        // "Always keep on this device" (pinned): Loft fetches the whole file.
        Placeholders.SetPin(bigPath, CfPin.Pinned);
        await Eventually(() => !Placeholders.Inspect(bigPath).Partial, "big.bin hydrated after pin");
        Placeholders.SetPin(bigPath, CfPin.Unspecified);
        Placeholders.Dehydrate(bigPath);
        Assert.True(Placeholders.Inspect(bigPath).Partial);

        // A new local file goes to the NAS and becomes a placeholder.
        File.WriteAllText(Path.Combine(_folder, "new.txt"), "made on Windows");
        await Eventually(() => _gw.Find(_gw.RootId, "new.txt") is { } f && Encoding.UTF8.GetString(f.Data) == "made on Windows", "upload of new.txt");
        await Eventually(() => Placeholders.Inspect(Path.Combine(_folder, "new.txt")) is { IsPlaceholder: true, InSync: true }, "new.txt in sync");

        // A new local folder with a file in it.
        Directory.CreateDirectory(Path.Combine(_folder, "Made here"));
        File.WriteAllText(Path.Combine(_folder, "Made here", "a.txt"), "a");
        await Eventually(() => _gw.Find(_gw.RootId, "Made here") is { } d && _gw.Find(d.Id, "a.txt") is not null, "folder + file upload");

        // An edit goes up as a new version of the same item.
        var helloId = st.Identity.Id;
        File.WriteAllText(Path.Combine(_folder, "hello.txt"), "edited on Windows");
        await Eventually(() => Encoding.UTF8.GetString(_gw.Find(_gw.RootId, "hello.txt")!.Value.Data) == "edited on Windows", "edit upload");
        Assert.Equal(helloId, _gw.Find(_gw.RootId, "hello.txt")!.Value.Id);

        // Rename in Explorer = PATCH on the gateway. Done from another process, as Explorer is one: a rename made
        // inside the provider's own process did not reach NOTIFY_RENAME on the CI runner.
        Cmd($"move \"{Path.Combine(_folder, "new.txt")}\" \"{Path.Combine(_folder, "renamed.txt")}\"");
        var newId = _gw.Find(_gw.RootId, "renamed.txt")?.Id;
        Assert.NotNull(newId);

        // Delete in Explorer = the NAS trash.
        Cmd($"del \"{Path.Combine(_folder, "renamed.txt")}\"");
        Assert.True(_gw.IsDeleted(newId!));

        // A file added on the NAS shows up here through the change feed.
        _gw.AddFile(_gw.RootId, "from-nas.txt", Encoding.UTF8.GetBytes("pushed"));
        await Eventually(() => File.Exists(Path.Combine(_folder, "from-nas.txt")), "placeholder from the feed");
        Assert.Equal("pushed", File.ReadAllText(Path.Combine(_folder, "from-nas.txt")));

        // New bytes on the NAS replace the local copy.
        _gw.SetContent(helloId, Encoding.UTF8.GetBytes("changed on the NAS"));
        await Eventually(() => File.ReadAllText(Path.Combine(_folder, "hello.txt")) == "changed on the NAS", "content update from the feed");

        log.WriteLine(string.Join("\n", _gw.Log.TakeLast(20)));
    }

    [Fact]
    public async Task ConflictKeepsBothVersions()
    {
        if (_engine is null) { log.WriteLine("SKIPPED: needs Windows 10 1809 or later"); return; }
        Cmd($"dir /b /a \"{_folder}\"");
        var p = Path.Combine(_folder, "hello.txt");
        Assert.Equal("hello from the NAS", File.ReadAllText(p));          // hydrated, in sync
        var id = Placeholders.Inspect(p).Identity!.Id;
        // Edit here, then on the NAS before the upload starts: the local edit is based on the old version, and
        // the feed leaves a file with local changes alone, so the upload's CAS decides.
        File.WriteAllText(p, "local edit");
        _gw.SetContent(id, Encoding.UTF8.GetBytes("NAS edit"));
        await Eventually(() => _gw.Names(_gw.RootId).Any(n => n.StartsWith("hello (conflict, ")), "conflict copy on the NAS");
        var copy = _gw.Names(_gw.RootId).First(n => n.StartsWith("hello (conflict, "));
        Assert.Equal("local edit", Encoding.UTF8.GetString(_gw.Find(_gw.RootId, copy)!.Value.Data));
        await Eventually(() => File.ReadAllText(p) == "NAS edit", "original back to the NAS version");
        await Eventually(() => File.Exists(Path.Combine(_folder, copy)), "conflict copy placeholder");
    }
}
