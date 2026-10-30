using Loft.Core;
using Xunit;
using static Loft.Core.Tests.FakeTransport;

namespace Loft.Core.Tests;

public class CachePolicyTests
{
    private static readonly DateTimeOffset Now = new(2026, 10, 30, 12, 0, 0, TimeSpan.Zero);
    private const long MB = 1 << 20;

    private static CachedFile F(string id, long mb, double hoursAgo) => new(id, mb * MB, Now.AddHours(-hoursAgo));

    [Fact]
    public void UnderTheLimitNothingIsFreed()
    {
        var files = new[] { F("a", 100, 5), F("b", 200, 1) };
        Assert.Empty(CachePolicy.PickEvictions(files, 300 * MB, Now));
    }

    [Fact]
    public void LeastRecentlyUsedGoFirstUntilTheRestFits()
    {
        var files = new[] { F("new", 400, 1), F("oldest", 300, 48), F("old", 300, 24), F("mid", 300, 6) };
        var picked = CachePolicy.PickEvictions(files, 800 * MB, Now).Select(f => f.Id).ToList();
        Assert.Equal(new[] { "oldest", "old" }, picked);      // 1300 → 1000 → 700 MB
    }

    [Fact]
    public void FilesUsedJustNowStayEvenOverTheLimit()
    {
        var files = new[] { F("open", 900, 0.01), F("old", 200, 10) };
        var picked = CachePolicy.PickEvictions(files, 500 * MB, Now).Select(f => f.Id).ToList();
        Assert.Equal(new[] { "old" }, picked);                // 1100 → 900 MB: still over, but "open" is in use
    }

    [Fact]
    public void DefaultLimitIsFiveGigabytes()
    {
        Assert.Equal(5L * 1024 * 1024 * 1024, CachePolicy.DefaultLimit);
        Assert.Equal(CachePolicy.DefaultLimit, new LoftSettings().CacheLimitBytes);
        Assert.Contains(CachePolicy.DefaultLimit, CachePolicy.Choices);
    }

    [Fact]
    public void SettingsKeepCacheLimitAndPendingDeletes()
    {
        var dir = Path.Combine(Path.GetTempPath(), "loft-cache-" + Guid.NewGuid().ToString("N"));
        var path = Path.Combine(dir, "settings.json");
        try
        {
            var s = new SettingsStore(path);
            s.Update(x =>
            {
                x.CacheLimitBytes = 10L << 30;
                x.PendingDeletes.Add(new PendingDelete("f1", "c3", @"Docs\a.txt", Now));
            });
            var again = new SettingsStore(path);
            Assert.Equal(10L << 30, again.Get(x => x.CacheLimitBytes));
            var d = Assert.Single(again.Get(x => x.PendingDeletes));
            Assert.Equal(("f1", "c3", @"Docs\a.txt"), (d.Id, d.IfMatch, d.Path));
            again.Update(x => x.ClearAccount());
            Assert.Empty(again.Get(x => x.PendingDeletes));
        }
        finally { Directory.Delete(dir, true); }
    }
}

public class SettingsFileTests
{
    [Fact]
    public void ASettingsFileWithAByteOrderMarkStillReads()
    {
        // PowerShell 5's Set-Content -Encoding UTF8 writes a BOM; found on the Windows 11 test PC, where the
        // BOM made Loft forget its pairing and account.
        var dir = Path.Combine(Path.GetTempPath(), "loft-bom-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        var path = Path.Combine(dir, "settings.json");
        try
        {
            System.IO.File.WriteAllText(path, "{\"nas_user\":\"anna\",\"cache_limit_bytes\":20971520}", new System.Text.UTF8Encoding(true));
            Assert.Equal(0xEF, System.IO.File.ReadAllBytes(path)[0]);
            var s = new SettingsStore(path);
            Assert.Equal(("anna", 20971520L), s.Get(x => (x.NasUser, x.CacheLimitBytes)));
        }
        finally { Directory.Delete(dir, true); }
    }

    [Fact]
    public void AnUnreadableSettingsFileIsKeptAside()
    {
        var dir = Path.Combine(Path.GetTempPath(), "loft-bad-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        var path = Path.Combine(dir, "settings.json");
        try
        {
            System.IO.File.WriteAllText(path, "{\"nas_user\": \"anna\", oops");
            var s = new SettingsStore(path);
            Assert.Null(s.Get(x => x.NasUser));
            Assert.True(System.IO.File.Exists(path + ".unreadable"));
            Assert.Contains("oops", System.IO.File.ReadAllText(path + ".unreadable"));
        }
        finally { Directory.Delete(dir, true); }
    }
}

public class OfflineDeleteTests
{
    private static GatewayException Api(int status, string code) =>
        GatewayException.FromApi(new ApiError { Status = status, Error = code });

    [Fact]
    public void QueuedWhenTheGatewayOrNasIsOutOfReach()
    {
        Assert.True(OfflineDeletes.ShouldQueue(GatewayException.Unreachable("timeout")));
        Assert.True(OfflineDeletes.ShouldQueue(Api(503, "nas_unavailable")));
        Assert.False(OfflineDeletes.ShouldQueue(Api(412, "content_etag_mismatch")));
        Assert.False(OfflineDeletes.ShouldQueue(Api(403, "access_denied")));
    }

    [Fact]
    public void AKeptFileKeepsTheFoldersAboveIt()
    {
        // Found on Windows 11: Explorer deletes a folder's contents first, then the folder. Offline, all of that is
        // queued; when one file turns out to be changed on the NAS, its folders must not be deleted either.
        var t = DateTimeOffset.UnixEpoch;
        var file = new PendingDelete("f", "c1", @"Docs\Fresh\Sub\direct.txt", t);
        var queue = new[]
        {
            file,
            new PendingDelete("other", "c1", @"Docs\Fresh\Sub\other.txt", t),
            new PendingDelete("sub", null, @"Docs\Fresh\Sub", t),
            new PendingDelete("fresh", null, @"Docs\Fresh", t),
            new PendingDelete("fresh2", null, @"Docs\Fresh2", t),
            new PendingDelete("docs-file", "c1", @"Docs", t),
        };
        Assert.Equal(new[] { "sub", "fresh" }, OfflineDeletes.Ancestors(queue, file).Select(d => d.Id));
    }

    [Fact]
    public void ReplayOutcomes()
    {
        Assert.Equal(PendingDeleteOutcome.Done, OfflineDeletes.Classify(null));
        Assert.Equal(PendingDeleteOutcome.Done, OfflineDeletes.Classify(Api(404, "not_found")));
        Assert.Equal(PendingDeleteOutcome.ChangedOnNas, OfflineDeletes.Classify(Api(412, "content_etag_mismatch")));
        Assert.Equal(PendingDeleteOutcome.KeepQueued, OfflineDeletes.Classify(GatewayException.Unreachable("down")));
        Assert.Equal(PendingDeleteOutcome.KeepQueued, OfflineDeletes.Classify(Api(503, "nas_unavailable")));
        Assert.Equal(PendingDeleteOutcome.KeepQueued, OfflineDeletes.Classify(Api(401, "token_expired")));
        Assert.Equal(PendingDeleteOutcome.Refused, OfflineDeletes.Classify(Api(403, "access_denied")));
    }
}

public class LocalIndexCacheFieldTests
{
    [Fact]
    public void UpsertKeepsLastUsedAndTheHashOfTheSameVersion()
    {
        var x = new LocalIndex("root");
        x.Upsert(File("f", "a.txt") with { Sha256 = "aa" });
        var t = new DateTimeOffset(2026, 10, 30, 10, 0, 0, TimeSpan.Zero);
        x.Touch("f", t);
        x.Upsert(File("f", "renamed.txt", m: "m2"));                  // rename from the feed: no sha256 in it
        Assert.Equal(("renamed.txt", "aa", t), (x.Get("f")!.Name, x.Get("f")!.Sha256, x.Get("f")!.LastUsed));
        x.Upsert(File("f", "renamed.txt", c: "c2", m: "m3"));         // new content: the old hash no longer applies
        Assert.Null(x.Get("f")!.Sha256);
        Assert.Equal(t, x.Get("f")!.LastUsed);
    }

    [Fact]
    public void CacheFieldsSurviveSaveAndLoad()
    {
        var path = Path.Combine(Path.GetTempPath(), "loft-index-" + Guid.NewGuid().ToString("N") + ".json");
        try
        {
            var x = new LocalIndex("root");
            x.Upsert(File("f", "a.txt") with { Sha256 = "bb" });
            var t = new DateTimeOffset(2026, 10, 30, 11, 0, 0, TimeSpan.Zero);
            x.Touch("f", t);
            x.Save(path);
            var y = LocalIndex.Load(path, "root")!;
            Assert.Equal(("bb", t), (y.Get("f")!.Sha256, y.Get("f")!.LastUsed));
        }
        finally { System.IO.File.Delete(path); }
    }
}
