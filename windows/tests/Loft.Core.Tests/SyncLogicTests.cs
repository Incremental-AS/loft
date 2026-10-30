using System.Text;
using Loft.Core;
using Xunit;
using static Loft.Core.Tests.FakeTransport;

namespace Loft.Core.Tests;

public class LocalIndexTests
{
    private static LocalIndex Tree()
    {
        var x = new LocalIndex("root");
        x.Upsert(Dir("d1", "Docs"));
        x.Upsert(Dir("d2", "Sub", "d1"));
        x.Upsert(File("f1", "a.txt", "d2"));
        x.Upsert(File("f2", "b.txt"));
        return x;
    }

    [Fact]
    public void PathsFollowTheParentChain()
    {
        var x = Tree();
        Assert.Equal("", x.RelativePath("root"));
        Assert.Equal(@"Docs\Sub\a.txt", x.RelativePath("f1"));
        x.Upsert(Dir("d1", "Papers"));                      // folder renamed on the NAS
        Assert.Equal(@"Papers\Sub\a.txt", x.RelativePath("f1"));
        Assert.Null(x.RelativePath("nope"));
    }

    [Fact]
    public void FindsByPathIgnoringCaseAndNormalisation()
    {
        var x = Tree();
        Assert.Equal("f1", x.FindByPath(@"docs\SUB\A.TXT")!.Id);
        Assert.Equal("f1", x.FindByPath("Docs/Sub/a.txt")!.Id);
        Assert.Equal("root", x.FindByPath("")!.Id);
        x.Upsert(File("f3", "Blåbær.txt"));
        Assert.Equal("f3", x.FindByPath("Blåbær.txt")!.Id);   // NFD from another app
        Assert.Null(x.FindByPath(@"Docs\nope"));
    }

    [Fact]
    public void RemoveTakesTheSubtree()
    {
        var x = Tree();
        Assert.Equal(new[] { "d1", "d2", "f1" }, x.Remove("d1").OrderBy(s => s));
        Assert.Null(x.Get("f1"));
        Assert.Equal(new[] { "f2" }, x.Children("root").Select(c => c.Id));
        Assert.Empty(x.Remove("root"));
    }

    [Fact]
    public void MovesUpdateChildren()
    {
        var x = Tree();
        x.Upsert(File("f2", "b.txt", "d2"));
        Assert.Equal(new[] { "d1" }, x.Children("root").Select(c => c.Id));
        Assert.Equal(new[] { "f1", "f2" }, x.Children("d2").Select(c => c.Id).OrderBy(s => s));
        Assert.True(x.IsAncestor("d1", "f2"));
        Assert.False(x.IsAncestor("f2", "d1"));
    }

    [Fact]
    public void PopulatedSurvivesUpserts()
    {
        var x = Tree();
        x.SetPopulated("d1");
        x.Upsert(Dir("d1", "Docs", m: "m2"));
        Assert.True(x.Get("d1")!.Populated);
    }

    [Fact]
    public void SavesAndLoads()
    {
        var x = Tree();
        x.SetPopulated("root");
        var path = Path.Combine(Path.GetTempPath(), "loft-index-" + Guid.NewGuid().ToString("N") + ".json");
        x.Save(path);
        var y = LocalIndex.Load(path, "root")!;
        Assert.Equal(x.Count, y.Count);
        Assert.Equal(@"Docs\Sub\a.txt", y.RelativePath("f1"));
        Assert.True(y.Get("root")!.Populated);
        Assert.Null(LocalIndex.Load(path, "other-root"));
        System.IO.File.Delete(path);
    }
}

public class ChangePlannerTests
{
    private static LocalIndex Index()
    {
        var x = new LocalIndex("root");
        x.SetPopulated("root");
        x.Upsert(Dir("d1", "Docs"));
        x.SetPopulated("d1");
        x.Upsert(Dir("d2", "Closed"));                         // never opened on this PC
        x.Upsert(File("f1", "a.txt", "d1", size: 3));
        x.Upsert(File("f2", "b.txt"));
        return x;
    }

    private static ChangesPage Page(IEnumerable<Item>? up = null, IEnumerable<string>? del = null) =>
        new((up ?? Array.Empty<Item>()).ToList(), (del ?? Array.Empty<string>()).ToList(), "1.2", false);

    [Fact]
    public void NewItemsInOpenedFoldersBecomePlaceholders()
    {
        var ops = ChangePlanner.Plan(Index(), Page(new[] { File("n1", "new.txt", "d1"), File("n2", "hidden.txt", "d2"), File("n3", "x", "unknown") }));
        Assert.Equal(new[] { (RemoteOpKind.CreatePlaceholder, "n1") }, ops.Select(o => (o.Kind, o.Id)));
    }

    [Fact]
    public void RenameMoveContentAndMeta()
    {
        var ops = ChangePlanner.Plan(Index(), Page(new[]
        {
            File("f1", "renamed.txt", "d1", size: 3),          // rename
            File("f2", "b.txt", c: "c2", size: 9),              // new bytes
            Dir("d1", "Docs", m: "m2"),                         // meta only
        }));
        Assert.Equal(new[]
        {
            (RemoteOpKind.Move, "f1"),
            (RemoteOpKind.UpdateContent, "f2"),
            (RemoteOpKind.UpdateMetadata, "d1"),
        }, ops.Select(o => (o.Kind, o.Id)));
    }

    [Fact]
    public void MoveIntoAClosedFolderRemovesItHere()
    {
        var ops = ChangePlanner.Plan(Index(), Page(new[] { File("f1", "a.txt", "d2", size: 3) }));
        Assert.Equal(new[] { (RemoteOpKind.DeleteLocal, "f1") }, ops.Select(o => (o.Kind, o.Id)));
    }

    [Fact]
    public void DeletesComeBeforeCreatesSoNamesAreFree()
    {
        // Another device's safe-save: X superseded by Y with the same name.
        var ops = ChangePlanner.Plan(Index(), Page(new[] { File("y", "b.txt", c: "c1") }, new[] { "f2", "never-seen" }));
        Assert.Equal(new[] { (RemoteOpKind.DeleteLocal, "f2"), (RemoteOpKind.CreatePlaceholder, "y") }, ops.Select(o => (o.Kind, o.Id)));
    }

    [Fact]
    public void UnchangedItemsDoNothing()
    {
        Assert.Empty(ChangePlanner.Plan(Index(), Page(new[] { File("f1", "a.txt", "d1", size: 3) })));
    }

    [Fact]
    public void FolderListingFindsWhatIsGone()
    {
        var ops = ChangePlanner.PlanFolder(Index(), "d1", new[] { File("n1", "n.txt", "d1") });
        Assert.Equal(new[] { (RemoteOpKind.DeleteLocal, "f1"), (RemoteOpKind.CreatePlaceholder, "n1") }, ops.Select(o => (o.Kind, o.Id)));
    }
}

public class LocalRulesTests
{
    [Theory]
    [InlineData("report.docx", true, RenameAction.Patch)]
    [InlineData("~WRL0001.tmp", true, RenameAction.Park)]
    [InlineData("report.docx", false, RenameAction.DeleteRemote)]
    public void Renames(string name, bool inside, RenameAction want) => Assert.Equal(want, LocalRules.OnRename(name, inside));

    [Fact]
    public void ANewFileTakingAParkedNameIsItsSuccessor()
    {
        var x = new LocalIndex("root");
        var parked = new[] { new ParkedItem("x1", "root", "Report.docx", "c4", DateTimeOffset.UtcNow) };
        var req = LocalRules.CommitForNewFile("root", "report.docx", parked, x);
        Assert.Equal(("x1", "c4"), (req.Replaces, req.IfMatch));
        Assert.Null(LocalRules.CommitForNewFile("root", "other.docx", parked, x).Replaces);
        Assert.Null(LocalRules.CommitForNewFile("sub", "report.docx", parked, x).Replaces);
    }

    [Fact]
    public void APlainFileOverALiveItemIsAnEditWithCas()
    {
        // An app that overwrites a placeholder in place (CREATE_ALWAYS) leaves a plain file behind.
        var x = new LocalIndex("root");
        x.Upsert(File("f1", "a.txt", c: "c3"));
        var req = LocalRules.CommitForNewFile("root", "A.TXT", Array.Empty<ParkedItem>(), x);
        Assert.Equal(("f1", "c3", (string?)null, (string?)null), (req.ItemId, req.IfMatch, req.ParentId, req.Replaces));
    }

    [Fact]
    public void ConflictOutcomes()
    {
        var item = File("f1", "a (conflict, PC, 2026-10-30 1200).txt");
        Assert.Equal(CommitOutcome.RefetchOriginal,
            LocalRules.AfterCommit(LocalRules.CommitForEdit("f1", "c1"), new CommitResponse(CommitResult.Conflict, item, null)));
        Assert.Equal(CommitOutcome.RenameToConflictCopy,
            LocalRules.AfterCommit(new CommitRequest { ParentId = "root", Name = "a.txt" }, new CommitResponse(CommitResult.Conflict, item, null)));
        Assert.Equal(CommitOutcome.InSync,
            LocalRules.AfterCommit(LocalRules.CommitForEdit("f1", "c1"), new CommitResponse(CommitResult.Modified, item, null)));
    }

    [Theory]
    [InlineData("~$report.docx", true)]
    [InlineData("~WRL0003.tmp", true)]
    [InlineData("ppt8A1.tmp", true)]
    [InlineData("A1B2C3D4", true)]                       // Excel temp
    [InlineData("a1b2c3d4", false)]
    [InlineData("A1B2C3D4.xlsx", false)]
    [InlineData("desktop.ini", true)]
    [InlineData("Thumbs.db", true)]
    [InlineData(".DS_Store", true)]
    [InlineData(".loft", true)]
    [InlineData("report.docx", false)]
    [InlineData("notes.txt", false)]
    public void Exclusions_(string name, bool excluded) => Assert.Equal(excluded, Exclusions.IsExcluded(name));
}

public class ChangeWatcherTests
{
    [Fact]
    public async Task ResyncsThenAppliesPagesThenRecoversFromAnExpiredAnchor()
    {
        var (c, fake, _) = TestClients.Make();
        var changesCalls = 0;
        using var cts = new CancellationTokenSource();
        fake.Handler = s => s.Path switch
        {
            "/v1/changes/latest" => Json(200, new { anchor = "e.1" }),
            "/v1/changes" => ++changesCalls switch
            {
                1 => Json(200, new { updated = new[] { File("f1", "a") }, deleted = Array.Empty<string>(), anchor = "e.2", more = false }),
                2 => Json(200, new { updated = Array.Empty<Item>(), deleted = Array.Empty<string>(), anchor = "e.2", more = false }),
                3 => Json(410, new { error = "anchor_expired" }),
                _ => Stop(),
            },
            _ => Json(404, new { error = "not_found" }),
        };
        HttpResponseMessage Stop() { cts.Cancel(); throw new OperationCanceledException(); }
        var pages = 0; var resyncs = 0; var anchors = new List<string>(); var states = new List<LoopState>();
        var w = new ChangeWatcher(c) { Wait = 0 };
        await w.RunAsync(null, _ => { pages++; return Task.CompletedTask; }, () => { resyncs++; return Task.CompletedTask; },
                         anchors.Add, states.Add, cts.Token);
        Assert.Equal(1, pages);
        Assert.Equal(2, resyncs);                           // at start, and after anchor_expired
        Assert.Equal(new[] { "e.1", "e.2", "e.2", "e.1" }, anchors);
        Assert.All(states, s => Assert.Equal(LoopStateKind.Ok, s.Kind));
        Assert.Equal("e.1", fake.Requests.First(r => r.Path == "/v1/changes").Query("anchor"));
    }

    [Fact]
    public async Task ReportsWhySyncStopped()
    {
        var (c, fake, _) = TestClients.Make();
        using var cts = new CancellationTokenSource();
        fake.Handler = _ => Json(401, new { error = "device_revoked" });
        var states = new List<LoopState>();
        var w = new ChangeWatcher(c) { FirstBackoff = TimeSpan.FromMilliseconds(1) };
        await w.RunAsync("e.1", _ => Task.CompletedTask, () => Task.CompletedTask, _ => { },
                         s => { states.Add(s); if (states.Count == 2) cts.Cancel(); }, cts.Token);
        Assert.Equal(new SyncError(SyncErrorKind.DeviceRemoved), states[0].Error);
    }
}

public class UpdateCheckerTests
{
    private sealed class Handler(Func<HttpRequestMessage, HttpResponseMessage> f) : HttpMessageHandler
    {
        public int Calls;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Calls++;
            return Task.FromResult(f(request));
        }
    }

    private static SettingsStore Settings() =>
        new(Path.Combine(Path.GetTempPath(), "loft-settings-" + Guid.NewGuid().ToString("N") + ".json"));

    private static HttpResponseMessage Rel(string json) => new(System.Net.HttpStatusCode.OK) { Content = new StringContent(json) };

    [Fact]
    public async Task ChecksAtMostDailyAndOnlyWhenOn()
    {
        var now = new DateTimeOffset(2026, 10, 30, 12, 0, 0, TimeSpan.Zero);
        var h = new Handler(r =>
        {
            Assert.Equal(UpdateChecker.ReleasesUrl, r.RequestUri);
            return Rel("""{"windows":{"version":"0.2.1","url":"https://loft.incremental.no/download/","sha256":"ab"}}""");
        });
        var s = Settings();
        var u = new UpdateChecker(new HttpClient(h), s, () => now);
        var rel = await u.CheckIfDueAsync("0.2.0");
        Assert.Equal("0.2.1", rel!.Version);
        Assert.Null(await u.CheckIfDueAsync("0.2.0"));              // same day: no request
        now += TimeSpan.FromHours(23);
        Assert.Null(await u.CheckIfDueAsync("0.2.0"));
        Assert.Equal(1, h.Calls);
        now += TimeSpan.FromHours(2);
        Assert.NotNull(await u.CheckIfDueAsync("0.2.0"));
        Assert.Equal(2, h.Calls);
        s.Update(x => x.UpdateCheckEnabled = false);
        now += TimeSpan.FromDays(3);
        Assert.Null(await u.CheckIfDueAsync("0.2.0"));
        Assert.Equal(2, h.Calls);                                   // off means no request at all
    }

    [Fact]
    public async Task UpToDateOrBrokenMeansNothingToShow()
    {
        var now = DateTimeOffset.UnixEpoch;
        var s = Settings();
        var u = new UpdateChecker(new HttpClient(new Handler(_ => Rel("""{"windows":{"version":"0.2.0"}}"""))), s, () => now);
        Assert.Null(await u.CheckIfDueAsync("0.2.0"));
        now += TimeSpan.FromDays(2);
        var broken = new UpdateChecker(new HttpClient(new Handler(_ => throw new HttpRequestException("offline"))), s, () => now);
        Assert.Null(await broken.CheckIfDueAsync("0.2.0"));
        Assert.Equal(now, s.Get(x => x.LastUpdateCheck));           // a failure still counts as today's check
    }

    [Theory]
    [InlineData("""{"platforms":{"windows":{"version":"1.0.0"}}}""", "1.0.0")]
    [InlineData("""{"windows":{"version":"v0.3.0","url":"http://insecure.example/x"}}""", "v0.3.0")]
    [InlineData("""{"android":{"version":"9.9.9"}}""", null)]
    [InlineData("""{"windows":{"version":"latest"}}""", null)]
    [InlineData("""not json""", null)]
    public void ParsesReleasesJson(string json, string? version)
    {
        var r = UpdateChecker.Parse(Encoding.UTF8.GetBytes(json), "windows");
        Assert.Equal(version, r?.Version);
        if (r is not null) Assert.True(r.Url is null || r.Url.Scheme == "https");
    }

    [Theory]
    [InlineData("0.2.10", "0.2.9", 1)]
    [InlineData("0.2.0", "0.2", 0)]
    [InlineData("0.3.0-beta1", "0.3.0", -1)]
    [InlineData("1.0.0", "0.9.9", 1)]
    [InlineData("v1.2.3", "1.2.3", 0)]
    public void ComparesVersions(string a, string b, int sign) =>
        Assert.Equal(sign, Math.Sign(UpdateChecker.CompareVersions(a, b)));
}

public class SettingsTests
{
    [Fact]
    public void RoundTripsAndKeepsThePairingOnSignOut()
    {
        var path = Path.Combine(Path.GetTempPath(), "loft-settings-" + Guid.NewGuid().ToString("N") + ".json");
        var s = new SettingsStore(path);
        var pairing = new Pairing("Home", TestClients.Lan, new[] { PairingTests.PinA });
        s.Update(x => { x.Pairing = pairing; x.NasUser = "u"; x.Root = new RootInfo("r", "home", "", "ri", "e.1"); });
        s.Remember("up1", "abc", 5);
        var t = new SettingsStore(path);
        Assert.Equal(pairing.Encode(), t.Get(x => x.Pairing)!.Encode());
        Assert.Equal("up1", t.Find("abc", 5));
        t.Forget("up1");
        Assert.Null(t.Find("abc", 5));
        t.Update(x => x.ClearAccount());
        Assert.Null(new SettingsStore(path).Get(x => x.NasUser));
        Assert.NotNull(new SettingsStore(path).Get(x => x.Pairing));
        Assert.DoesNotContain("token", System.IO.File.ReadAllText(path), StringComparison.OrdinalIgnoreCase);
        System.IO.File.Delete(path);
    }

    [Fact]
    public void TokenNeverPrintsItself()
    {
        var t = TestClients.Token("secret-value");
        Assert.DoesNotContain("secret-value", t.ToString());
        Assert.Equal("secret-value", StoredToken.Deserialize(t.Serialize())!.Token);
        Assert.True(t with { IssuedAt = DateTimeOffset.UtcNow.AddDays(-8) } is var old && old.DueForRotation(DateTimeOffset.UtcNow));
    }
}
