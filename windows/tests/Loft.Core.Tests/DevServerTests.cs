// Against a real gateway: gateway/scripts/dev_server.py <nas-dir> <work-dir> [direct public].
// Runs only when LOFT_DEV_LAN (https://127.0.0.1:18443), LOFT_DEV_PUBLIC (http://127.0.0.1:18080)
// and LOFT_DEV_PIN (contents of <work-dir>/direct.pin) are set; otherwise every test returns early
// and says so. User loft-dev / dev-password, share "home".
using System.Text;
using Loft.Core;
using Xunit;
using Xunit.Abstractions;

namespace Loft.Core.Tests;

public class DevServerTests(ITestOutputHelper log)
{
    private static readonly string? LanUrl = Environment.GetEnvironmentVariable("LOFT_DEV_LAN");
    private static readonly string? PubUrl = Environment.GetEnvironmentVariable("LOFT_DEV_PUBLIC");
    private static readonly string? Pin = Environment.GetEnvironmentVariable("LOFT_DEV_PIN");
    private const string User = "loft-dev", Password = "dev-password";

    private bool Skip()
    {
        if (LanUrl is not null && PubUrl is not null && Pin is not null) return false;
        log.WriteLine("SKIPPED: LOFT_DEV_LAN / LOFT_DEV_PUBLIC / LOFT_DEV_PIN not set (no dev_server.py running)");
        return true;
    }

    /// <summary>The dev server's public listener is plain HTTP on loopback, standing in for a tunnel.</summary>
    private static GatewayClient Client(string pin, ITokenStore tokens, string? lan = null)
    {
        var cfg = new RouteConfig(new Uri(lan ?? LanUrl!), new Uri(PubUrl!), PublicMode.Tunnel, new PinSet(new[] { pin }));
        var c = GatewayClient.Create(cfg, tokens);
        c.RetryDelays = new[] { TimeSpan.FromMilliseconds(50) };
        return c;
    }

    private static async Task<(GatewayClient c, MemoryTokenStore tokens, Me me)> SignedIn()
    {
        var tokens = new MemoryTokenStore();
        var c = Client(Pin!, tokens);
        tokens.Save(StoredToken.From(await c.LoginAsync(User, Password, "Loft.Core tests")));
        var me = await c.SetRootAsync("home", "");
        return (c, tokens, me);
    }

    private static string Temp(byte[] data)
    {
        var p = Path.Combine(Path.GetTempPath(), "loft-it-" + Guid.NewGuid().ToString("N"));
        File.WriteAllBytes(p, data);
        return p;
    }

    [Fact]
    public async Task PinnedProbeAndSignIn()
    {
        if (Skip()) return;
        var tokens = new MemoryTokenStore();
        var c = Client(Pin!, tokens);
        Assert.Equal(Route.Lan, await c.Selector.ReprobeAsync(c.Probe!));
        var r = await c.LoginAsync(User, Password, "Loft.Core tests");
        Assert.StartsWith("nst_", r.Token);
        tokens.Save(StoredToken.From(r));
        var me = await c.MeAsync();
        Assert.Equal(User, me.NasUser);
        Assert.Equal("direct", me.Route);
        Assert.Contains("home", await c.SharesAsync());
        var ident = await c.IdentityAsync();
        Assert.Contains(Pin!, ident.DirectSpkiSha256);
    }

    [Fact]
    public async Task WrongPinMeansNoPasswordIsSent()
    {
        if (Skip()) return;
        var c = Client(PairingTests.PinA, new MemoryTokenStore());
        Assert.Equal(Route.Public, await c.Selector.ReprobeAsync(c.Probe!));
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.LoginAsync(User, Password, "x"));
        Assert.Equal(GatewayErrorKind.SignInNeedsHome, e.Kind);
    }

    [Fact]
    public async Task ThePublicListenerRefusesPasswordsButTakesTokens()
    {
        if (Skip()) return;
        var (signed, tokens, _) = await SignedIn();
        // A client that is "away": LAN pinned to a key it will never see, so it uses the public listener.
        var away = Client(PairingTests.PinA, tokens);
        var me = await away.MeAsync();
        Assert.Equal("public", me.Route);
        Assert.Equal(Route.Public, away.Selector.Current);
        // The gateway itself refuses a login there (second wall behind our own refusal).
        var req = new GatewayClient.Req(HttpMethod.Post, "v1/auth/login")
        {
            Body = Encoding.UTF8.GetBytes($$"""{"user":"{{User}}","password":"wrong-on-purpose","device_name":"x"}"""),
            Auth = false, Route = Route.Public,
        };
        var e = await Assert.ThrowsAsync<GatewayException>(() => away.CallAsync<LoginResult>(req, default));
        Assert.Equal(403, e.Status);
        _ = signed;
    }

    [Fact]
    public async Task UploadDownloadRenameDeleteAndTheFeed()
    {
        if (Skip()) return;
        var (c, _, me) = await SignedIn();
        var root = me.Root!.RootItemId;
        var anchor = await c.LatestAnchorAsync();

        var folder = await c.MkdirAsync(root, "win-it-" + Guid.NewGuid().ToString("N")[..8]);
        var data = new byte[3 * 1024 * 1024 + 123];
        new Random(5).NextBytes(data);
        var src = Temp(data);
        c.ChunkLimit = 1024 * 1024;                               // four chunks
        var settings = new SettingsStore(Path.Combine(Path.GetTempPath(), "loft-it-settings-" + Guid.NewGuid().ToString("N")));
        var created = await c.UploadAndCommitAsync(src, new CommitRequest { ParentId = folder.Id, Name = "data.bin" }, settings);
        Assert.Equal(CommitResult.Created, created.Result);
        Assert.Equal(data.Length, created.Item.Size);
        Assert.Equal(Hashing.Hex(data), created.Item.Sha256);
        Assert.Empty(settings.Get(s => s.UploadResume));

        c.DownloadSlice = 1024 * 1024;
        var dest = Temp(Array.Empty<byte>());
        var etag = await c.DownloadAsync(created.Item.Id, dest);
        Assert.Equal(created.Item.ContentEtag, etag);
        Assert.Equal(data, File.ReadAllBytes(dest));

        var part = new byte[data.Length];
        var rr = await c.ReadRangeAsync(created.Item.Id, 4096, 100_000, (buf, off) => { buf.Span.CopyTo(part.AsSpan((int)off)); return Task.CompletedTask; });
        Assert.Equal((4096L, 100_000L, (long)data.Length), (rr.Start, rr.Count, rr.Total));
        Assert.Equal(data[4096..104096], part[4096..104096]);

        var kids = await c.AllChildrenAsync(folder.Id);
        Assert.Equal("data.bin", kids.Single().Name);

        var renamed = await c.PatchAsync(created.Item.Id, name: "renamed.bin", ifMatch: created.Item.MetaEtag);
        Assert.Equal("renamed.bin", renamed.Name);

        // The feed sees the folder and the file.
        var page = await c.ChangesAsync(anchor);
        Assert.Contains(page.Updated, i => i.Id == folder.Id);
        Assert.Contains(page.Updated, i => i.Id == created.Item.Id && i.Name == "renamed.bin");

        // An edit on a stale base becomes the gateway's conflict copy; the original keeps the NAS bytes.
        var v2 = Temp(Encoding.UTF8.GetBytes("second version"));
        var mod = await c.UploadAndCommitAsync(v2, LocalRules.CommitForEdit(created.Item.Id, created.Item.ContentEtag));
        Assert.Equal(CommitResult.Modified, mod.Result);
        var v3 = Temp(Encoding.UTF8.GetBytes("edited on a stale copy"));
        var req = LocalRules.CommitForEdit(created.Item.Id, created.Item.ContentEtag);
        var conflict = await c.UploadAndCommitAsync(v3, req);
        Assert.Equal(CommitResult.Conflict, conflict.Result);
        Assert.Equal(CommitOutcome.RefetchOriginal, LocalRules.AfterCommit(req, conflict));
        Assert.Contains("(conflict, Loft.Core tests, ", conflict.Item.Name);
        log.WriteLine("conflict copy: " + conflict.Item.Name);

        // A create on a taken name: also a conflict copy, which the local file is renamed to.
        // (Another name than above: two conflict copies of one name from one device in the same minute
        // get the same name, and the gateway answers 409 name_exists_on_nas for the second.)
        var dup = Temp(Encoding.UTF8.GetBytes("same name, other bytes"));
        await c.UploadAndCommitAsync(dup, new CommitRequest { ParentId = folder.Id, Name = "taken.txt" });
        var createReq = new CommitRequest { ParentId = folder.Id, Name = "taken.txt" };
        var dupR = await c.UploadAndCommitAsync(dup, createReq);
        Assert.Equal(CommitOutcome.RenameToConflictCopy, LocalRules.AfterCommit(createReq, dupR));

        // Delete with a stale etag is refused; with the current one it goes to the trash.
        var stale = await Assert.ThrowsAsync<GatewayException>(() => c.DeleteAsync(created.Item.Id, created.Item.ContentEtag));
        Assert.Equal(412, stale.Status);
        var cur = await c.ItemAsync(created.Item.Id);
        Assert.Equal("trashed", (await c.DeleteAsync(cur.Id, cur.ContentEtag)).Result);
        var del = await c.DeleteAsync(folder.Id);
        Assert.Equal("trashed", del.Result);
        var after = await c.ChangesAsync(page.Anchor);
        Assert.Contains(folder.Id, after.Deleted);
        foreach (var f in new[] { src, dest, v2, v3, dup }) File.Delete(f);
    }

    [Fact]
    public async Task TokenRotationKeepsTheOldOneBriefly()
    {
        if (Skip()) return;
        var (c, tokens, _) = await SignedIn();
        var old = tokens.Load()!;
        var fresh = await c.RotateTokenAsync();
        Assert.NotEqual(old.Token, fresh.Token);
        Assert.Equal(fresh, tokens.Load());
        await c.MeAsync();
        var oldClient = Client(Pin!, new MemoryTokenStore(old));
        await oldClient.MeAsync();                                // 120 s grace
        var e = await Assert.ThrowsAsync<GatewayException>(() => oldClient.RotateTokenAsync());
        Assert.True(SyncError.FromLoopFailure(e.Api!)!.NeedsSignIn);
    }

    [Fact]
    public async Task FallsBackToThePublicListenerWhenTheLanDies()
    {
        if (Skip()) return;
        var (_, tokens, _) = await SignedIn();
        // A LAN address where nothing listens, with the right pin: the probe fails, requests go outside.
        var c = Client(Pin!, tokens, lan: "https://127.0.0.1:9");
        var me = await c.MeAsync();
        Assert.Equal("public", me.Route);
    }

    [Fact]
    public async Task ChangeWatcherSeesAnUpload()
    {
        if (Skip()) return;
        var (c, _, me) = await SignedIn();
        var start = await c.LatestAnchorAsync();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        var seen = new TaskCompletionSource<ChangesPage>();
        var w = new ChangeWatcher(c) { Wait = 5 };
        var run = w.RunAsync(start, p => { seen.TrySetResult(p); return Task.CompletedTask; }, () => Task.CompletedTask,
                             _ => { }, _ => { }, cts.Token);
        var f = Temp(Encoding.UTF8.GetBytes("hello"));
        var name = "watch-" + Guid.NewGuid().ToString("N")[..8] + ".txt";
        var r = await c.UploadAndCommitAsync(f, new CommitRequest { ParentId = me.Root!.RootItemId, Name = name });
        var page = await seen.Task.WaitAsync(TimeSpan.FromSeconds(20));
        Assert.Contains(page.Updated, i => i.Id == r.Item.Id);
        cts.Cancel();
        await run;
        await c.DeleteAsync(r.Item.Id, r.Item.ContentEtag);
        File.Delete(f);
    }
}
