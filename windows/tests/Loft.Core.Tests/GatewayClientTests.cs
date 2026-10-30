using System.Net.Http;
using System.Text;
using System.Text.Json;
using Loft.Core;
using Xunit;
using static Loft.Core.Tests.FakeTransport;

namespace Loft.Core.Tests;

public class GatewayClientTests
{
    private static readonly object MeBody = new { nas_user = "u", device_id = "dev1", device_name = "PC", route = "direct", root = (object?)null };

    [Fact]
    public async Task UsesLanWhenTheProbeFindsIt()
    {
        var (c, fake, _) = TestClients.Make(lanUp: true);
        fake.Handler = _ => Json(200, MeBody);
        await c.MeAsync();
        Assert.Equal(Route.Lan, fake.Requests.Single().Route);
        Assert.Equal("https://gw.example:8443/v1/me", fake.Requests[0].Url.ToString());
        Assert.Equal("Bearer tok-1", fake.Requests[0].Headers["Authorization"]);
    }

    [Fact]
    public async Task UsesTheOutsideAddressWhenNotAtHome()
    {
        var (c, fake, _) = TestClients.Make(lanUp: false);
        fake.Handler = _ => Json(200, MeBody);
        await c.MeAsync();
        Assert.Equal(Route.Public, fake.Requests.Single().Route);
        Assert.Equal("https://files.example.org/v1/me", fake.Requests[0].Url.ToString());
    }

    [Fact]
    public async Task FallsBackOnceWhenTheLanStopsAnswering()
    {
        var (c, fake, _) = TestClients.Make(lanUp: true);
        fake.Handler = s => s.Route == Route.Lan ? throw new HttpRequestException("connection refused") : Json(200, MeBody);
        await c.MeAsync();
        Assert.Equal(new[] { Route.Lan, Route.Public }, fake.Requests.Select(r => r.Route));
        Assert.Equal(Route.Public, c.Selector.Current);
    }

    [Fact]
    public async Task NoOutsideAddressMeansUnreachable()
    {
        var (c, fake, _) = TestClients.Make(mode: PublicMode.None, lanUp: true);
        fake.Handler = _ => throw new HttpRequestException("connection refused");
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.MeAsync());
        Assert.Equal(GatewayErrorKind.Unreachable, e.Kind);
        Assert.Single(fake.Requests);
    }

    [Fact]
    public async Task EdgeHtml5xxIsUnreachableNotAVerdict()
    {
        var (c, fake, _) = TestClients.Make(lanUp: false);
        fake.Handler = _ => Raw(530, "<html>Argo Tunnel error</html>");
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.MeAsync());
        Assert.Equal(GatewayErrorKind.Unreachable, e.Kind);
        Assert.Equal(LoopStateKind.CantReachHome, LoopState.From(e).Kind);
    }

    [Fact]
    public async Task GatewayJson5xxIsAVerdict()
    {
        var (c, fake, _) = TestClients.Make(lanUp: true);
        fake.Handler = _ => Json(502, new { error = "nas_error", detail = "x" });
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.MeAsync());
        Assert.Equal("nas_error", e.Code);
        Assert.Equal(502, e.Status);
        Assert.Equal(LoopStateKind.CantReachNas, LoopState.From(e).Kind);
    }

    [Fact]
    public async Task RetriesOnceWithATokenRotatedUnderneath()
    {
        var (c, fake, tokens) = TestClients.Make();
        fake.Handler = s =>
        {
            if (s.Headers["Authorization"] == "Bearer tok-1")
            {
                tokens.Save(TestClients.Token("tok-2"));          // the other part of the app rotated it
                return Json(401, new { error = "invalid_token" });
            }
            return Json(200, MeBody);
        };
        await c.MeAsync();
        Assert.Equal(new[] { "Bearer tok-1", "Bearer tok-2" }, fake.Requests.Select(r => r.Headers["Authorization"]));
    }

    [Fact]
    public async Task DoesNotLoopOnAnInvalidToken()
    {
        var (c, fake, _) = TestClients.Make();
        fake.Handler = _ => Json(401, new { error = "invalid_token" });
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.MeAsync());
        Assert.Single(fake.Requests);
        Assert.Equal(new SyncError(SyncErrorKind.SignInExpired), LoopState.From(e).Error);
    }

    [Fact]
    public async Task NotSignedInSendsNothing()
    {
        var (c, fake, _) = TestClients.Make(signedIn: false);
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.MeAsync());
        Assert.Equal(GatewayErrorKind.NotSignedIn, e.Kind);
        Assert.Empty(fake.Requests);
    }

    [Theory]
    [InlineData("nas_credentials_invalid", SyncErrorKind.NasPasswordChanged, true)]
    [InlineData("token_expired", SyncErrorKind.SignInExpired, true)]
    [InlineData("token_superseded", SyncErrorKind.SignInExpired, true)]
    [InlineData("missing_token", SyncErrorKind.SignInExpired, true)]
    [InlineData("device_revoked", SyncErrorKind.DeviceRemoved, true)]
    [InlineData("access_denied", SyncErrorKind.NoAccess, false)]
    [InlineData("root_missing", SyncErrorKind.NoAccess, false)]
    [InlineData("something_new", SyncErrorKind.Stopped, false)]
    public void MapsLoopFailuresLikeTheReferenceClient(string code, SyncErrorKind kind, bool signIn)
    {
        var s = SyncError.FromLoopFailure(new ApiError { Error = code, Status = 401 });
        Assert.NotNull(s);
        Assert.Equal(kind, s!.Kind);
        Assert.Equal(signIn, s.NeedsSignIn);
    }

    [Theory]
    [InlineData("nas_unavailable")]
    [InlineData("nas_error")]
    [InlineData("anchor_expired")]
    public void SomeFailuresAreNotErrorStates(string code) =>
        Assert.Null(SyncError.FromLoopFailure(new ApiError { Error = code }));

    // ── sign-in: a password only ever goes over a pinned route ─────────

    [Fact]
    public async Task LoginGoesToTheLan()
    {
        var (c, fake, _) = TestClients.Make(lanUp: true, signedIn: false);
        fake.Handler = _ => Json(200, new { device_id = "d", token = "t", nas_user = "u", expires_at = 2e9 });
        var r = await c.LoginAsync("u", "pw", "PC");
        Assert.Equal("t", r.Token);
        var s = fake.Requests.Single();
        Assert.Equal(Route.Lan, s.Route);
        Assert.False(s.Headers.ContainsKey("Authorization"));
        Assert.Contains("\"password\":\"pw\"", s.BodyText);
    }

    [Fact]
    public async Task LoginNeverGoesThroughATunnel()
    {
        var (c, fake, _) = TestClients.Make(mode: PublicMode.Tunnel, lanUp: false, signedIn: false);
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.LoginAsync("u", "pw", "PC"));
        Assert.Equal(GatewayErrorKind.SignInNeedsHome, e.Kind);
        Assert.Empty(fake.Requests);
    }

    [Fact]
    public async Task LoginNeverFallsBackFromLanToATunnel()
    {
        var (c, fake, _) = TestClients.Make(mode: PublicMode.Tunnel, lanUp: true, signedIn: false);
        fake.Handler = _ => throw new HttpRequestException("reset");
        await Assert.ThrowsAsync<GatewayException>(() => c.LoginAsync("u", "pw", "PC"));
        Assert.All(fake.Requests, r => Assert.Equal(Route.Lan, r.Route));
    }

    [Fact]
    public async Task LoginUsesADirectOutsideAddress()
    {
        var (c, fake, _) = TestClients.Make(mode: PublicMode.Direct, lanUp: false, signedIn: false);
        fake.Handler = _ => Json(200, new { device_id = "d", token = "t", nas_user = "u", expires_at = 2e9 });
        await c.LoginAsync("u", "pw", "PC");
        Assert.Equal(Route.Public, fake.Requests.Single().Route);
    }

    [Fact]
    public async Task LanOnlyPairingOutsideHomeCantSignIn()
    {
        var (c, fake, _) = TestClients.Make(mode: PublicMode.None, lanUp: false, signedIn: false);
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.LoginAsync("u", "pw", "PC"));
        Assert.Equal(GatewayErrorKind.SignInNeedsHome, e.Kind);
        Assert.Empty(fake.Requests);
    }

    [Fact]
    public async Task RotationStoresTheNewToken()
    {
        var (c, fake, tokens) = TestClients.Make();
        fake.Handler = _ => Json(200, new { device_id = "dev1", token = "tok-new", expires_at = 2e9 });
        var t = await c.RotateTokenAsync();
        Assert.Equal("tok-new", tokens.Load()!.Token);
        Assert.Equal("/v1/auth/refresh", fake.Requests.Single().Path);
        Assert.Equal(t, tokens.Load());
    }

    [Fact]
    public async Task RefreshPinsReplacesThemButNeverEmpties()
    {
        var (c, fake, _) = TestClients.Make();
        fake.Handler = _ => Json(200, new { direct_spki_sha256 = new[] { PairingTests.PinB } });
        await c.RefreshPinsAsync();
        Assert.True(c.Selector.Config.Pins.Contains(PairingTests.PinB));
        Assert.False(c.Selector.Config.Pins.Contains(PairingTests.PinA));
        fake.Handler = _ => Json(200, new { direct_spki_sha256 = Array.Empty<string>() });
        Assert.Null(await c.RefreshPinsAsync());
        Assert.True(c.Selector.Config.Pins.Contains(PairingTests.PinB));
        Assert.DoesNotContain(fake.Requests, r => r.Headers.ContainsKey("Authorization"));
    }

    [Fact]
    public async Task ErrorExtrasArrive()
    {
        var (c, fake, _) = TestClients.Make();
        fake.Handler = _ => Json(409, new { error = "name_exists", existing_id = "abc" });
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.MkdirAsync("root", "x"));
        Assert.Equal("abc", e.Api!.ExistingId);
        Assert.Equal(409, e.Status);
    }

    [Fact]
    public async Task QueryAndPathAreEscaped()
    {
        var (c, fake, _) = TestClients.Make();
        fake.Handler = _ => Json(200, new { share = "a b", path = "x/y&z", folders = Array.Empty<string>() });
        await c.FoldersAsync("a b", "x/y&z");
        Assert.Equal("a b", fake.Requests[0].Query("share"));
        Assert.Equal("x/y&z", fake.Requests[0].Query("path"));
    }

    // ── uploads ─────────────────────────────────────────────────────────

    private static string TempFile(byte[] data)
    {
        var p = Path.Combine(Path.GetTempPath(), "loft-tmp-" + Guid.NewGuid().ToString("N"));
        System.IO.File.WriteAllBytes(p, data);
        return p;
    }

    private sealed class Resume : IUploadResume
    {
        public readonly Dictionary<string, string> Map = new();
        public string? Find(string sha256, long size) => Map.GetValueOrDefault($"{sha256}:{size}");
        public void Remember(string uploadId, string sha256, long size) => Map[$"{sha256}:{size}"] = uploadId;
        public void Forget(string uploadId) { foreach (var k in Map.Where(kv => kv.Value == uploadId).Select(kv => kv.Key).ToList()) Map.Remove(k); }
    }

    [Fact]
    public async Task UploadsInChunksWithHashesThenCommits()
    {
        var data = Enumerable.Range(0, 2500).Select(i => (byte)i).ToArray();
        var path = TempFile(data);
        var (c, fake, _) = TestClients.Make();
        c.ChunkLimit = 1000;
        var received = new MemoryStream();
        fake.Handler = s => (s.Method, s.Path) switch
        {
            ("POST", "/v1/uploads") => Json(200, new { upload_id = "u1", received = 0, size = 2500, chunk_max = 33554432 }),
            ("PUT", "/v1/uploads/u1") => Chunk(s),
            ("POST", "/v1/uploads/u1/commit") => Json(200, new { result = "created", item = File("f1", "a.bin", size: 2500), replaced_id = (string?)null }),
            _ => Json(404, new { error = "not_found" }),
        };
        HttpResponseMessage Chunk(Sent s)
        {
            Assert.Equal(Hashing.Hex(s.Body!), s.Headers["X-Chunk-Sha256"]);
            Assert.Equal(received.Length.ToString(), s.Query("offset"));
            received.Write(s.Body!);
            return Json(200, new { upload_id = "u1", received = received.Length, size = 2500 });
        }
        var resume = new Resume();
        var r = await c.UploadAndCommitAsync(path, new CommitRequest { ParentId = "root", Name = "a.bin" }, resume);
        Assert.Equal(CommitResult.Created, r.Result);
        Assert.Equal(data, received.ToArray());
        Assert.Equal(3, fake.Requests.Count(x => x.Method == "PUT"));
        var create = JsonDocument.Parse(fake.Requests[0].Body!).RootElement;
        Assert.Equal(Hashing.Hex(data), create.GetProperty("sha256").GetString());
        Assert.Equal("root", create.GetProperty("near_id").GetString());
        var commit = JsonDocument.Parse(fake.Requests.Last().Body!).RootElement;
        Assert.Equal("a.bin", commit.GetProperty("name").GetString());
        Assert.False(commit.TryGetProperty("item_id", out _));            // nulls are left out
        Assert.Empty(resume.Map);                                          // committed: forgotten
        System.IO.File.Delete(path);
    }

    [Fact]
    public async Task UploadResumesFromTheGatewaysOffset()
    {
        var data = new byte[3000];
        new Random(1).NextBytes(data);
        var path = TempFile(data);
        var (c, fake, _) = TestClients.Make();
        c.ChunkLimit = 1000;
        long have = 0;
        var wrongOnce = true;
        fake.Handler = s => (s.Method, s.Path) switch
        {
            ("POST", "/v1/uploads") => Json(200, new { upload_id = "u1", received = 0, size = 3000 }),
            ("PUT", "/v1/uploads/u1") when wrongOnce && s.Query("offset") == "1000" =>
                Lose(),
            ("PUT", "/v1/uploads/u1") when s.Query("offset") != have.ToString() =>
                Json(409, new { error = "wrong_offset", received = have }),
            ("PUT", "/v1/uploads/u1") => Take(s),
            _ => Json(404, new { error = "not_found" }),
        };
        HttpResponseMessage Lose()
        {
            wrongOnce = false;
            have = 2000;                     // the gateway got that chunk (and the next) before we lost the answer
            return Json(409, new { error = "wrong_offset", received = 2000 });
        }
        HttpResponseMessage Take(Sent s)
        {
            have += s.Body!.Length;
            return Json(200, new { upload_id = "u1", received = have, size = 3000 });
        }
        var id = await c.UploadAsync(path);
        Assert.Equal("u1", id);
        Assert.Equal(new[] { "0", "1000", "2000" }, fake.Requests.Where(r => r.Method == "PUT").Select(r => r.Query("offset")));
        System.IO.File.Delete(path);
    }

    [Fact]
    public async Task UploadRecreatesAnExpiredSessionOnce()
    {
        var path = TempFile(new byte[10]);
        var (c, fake, _) = TestClients.Make();
        var creates = 0;
        fake.Handler = s => (s.Method, s.Path) switch
        {
            ("POST", "/v1/uploads") => Json(200, new { upload_id = $"u{++creates}", received = 0, size = 10 }),
            ("PUT", "/v1/uploads/u1") => Json(404, new { error = "no_such_upload" }),
            ("PUT", "/v1/uploads/u2") => Json(200, new { upload_id = "u2", received = 10, size = 10 }),
            _ => Json(404, new { error = "not_found" }),
        };
        var resume = new Resume();
        Assert.Equal("u2", await c.UploadAsync(path, resume: resume));
        Assert.Equal(2, creates);
        Assert.Equal("u2", resume.Map.Values.Single());
        System.IO.File.Delete(path);
    }

    [Fact]
    public async Task UploadContinuesARememberedSession()
    {
        var data = new byte[2000];
        var path = TempFile(data);
        var (c, fake, _) = TestClients.Make();
        c.ChunkLimit = 1000;
        var resume = new Resume();
        resume.Remember("old", Hashing.Hex(data), 2000);
        fake.Handler = s => (s.Method, s.Path) switch
        {
            ("GET", "/v1/uploads/old") => Json(200, new { upload_id = "old", received = 1000, size = 2000 }),
            ("PUT", "/v1/uploads/old") => Json(200, new { upload_id = "old", received = 2000, size = 2000 }),
            _ => Json(500, new { error = "unexpected" }),
        };
        Assert.Equal("old", await c.UploadAsync(path, resume: resume));
        Assert.Equal("1000", fake.Requests.Single(r => r.Method == "PUT").Query("offset"));
        Assert.DoesNotContain(fake.Requests, r => r.Method == "POST");
        System.IO.File.Delete(path);
    }

    [Fact]
    public async Task UploadRetriesTransportErrorsThenGivesUp()
    {
        var path = TempFile(new byte[10]);
        var (c, fake, _) = TestClients.Make(mode: PublicMode.None);
        fake.Handler = s => (s.Method, s.Path) switch
        {
            ("POST", "/v1/uploads") => Json(200, new { upload_id = "u1", received = 0, size = 10 }),
            _ => throw new HttpRequestException("reset"),
        };
        var e = await Assert.ThrowsAsync<GatewayException>(() => c.UploadAsync(path));
        Assert.True(e.IsUnreachable);
        Assert.Equal(3, fake.Requests.Count(r => r.Method == "PUT"));     // first try + 2 retries
        System.IO.File.Delete(path);
    }

    // ── downloads ───────────────────────────────────────────────────────

    private static Func<Sent, HttpResponseMessage> Serve(Func<byte[]> content, Func<string> etag)
    {
        return s =>
        {
            var data = content();
            if (!s.Headers.TryGetValue("Range", out var range)) return Bytes(200, data, etag());
            var m = System.Text.RegularExpressions.Regex.Match(range, @"bytes=(\d+)-(\d+)");
            long a = long.Parse(m.Groups[1].Value), b = Math.Min(long.Parse(m.Groups[2].Value), data.Length - 1);
            if (a >= data.Length) return Json(416, new { error = "range_not_satisfiable" });
            return Bytes(206, data[(int)a..(int)(b + 1)], etag(), a, data.Length);
        };
    }

    [Fact]
    public async Task DownloadsInSlices()
    {
        var data = new byte[2500];
        new Random(2).NextBytes(data);
        var (c, fake, _) = TestClients.Make();
        c.DownloadSlice = 1000;
        fake.Handler = Serve(() => data, () => "c7");
        var dest = Path.Combine(Path.GetTempPath(), "loft-dl-" + Guid.NewGuid().ToString("N"));
        Assert.Equal("c7", await c.DownloadAsync("f1", dest));
        Assert.Equal(data, System.IO.File.ReadAllBytes(dest));
        Assert.Equal(new[] { "bytes=0-999", "bytes=1000-1999", "bytes=2000-2999" }, fake.Requests.Select(r => r.Headers["Range"]));
        System.IO.File.Delete(dest);
    }

    [Fact]
    public async Task DownloadOfAnEmptyFile()
    {
        var (c, fake, _) = TestClients.Make();
        fake.Handler = Serve(Array.Empty<byte>, () => "c1");
        var dest = Path.Combine(Path.GetTempPath(), "loft-dl-" + Guid.NewGuid().ToString("N"));
        Assert.Equal("c1", await c.DownloadAsync("f1", dest));
        Assert.Equal(0, new FileInfo(dest).Length);
        System.IO.File.Delete(dest);
    }

    [Fact]
    public async Task DownloadStartsOverWhenTheNasCopyChanges()
    {
        var (c, fake, _) = TestClients.Make();
        c.DownloadSlice = 4;
        var calls = 0;
        var v1 = Encoding.ASCII.GetBytes("aaaaaaaa");
        var v2 = Encoding.ASCII.GetBytes("bbbbbbbbbb");
        var serve = Serve(() => calls <= 1 ? v1 : v2, () => calls <= 1 ? "c1" : "c2");
        fake.Handler = s => { calls++; return serve(s); };
        var dest = Path.Combine(Path.GetTempPath(), "loft-dl-" + Guid.NewGuid().ToString("N"));
        Assert.Equal("c2", await c.DownloadAsync("f1", dest));
        Assert.Equal(v2, System.IO.File.ReadAllBytes(dest));
        System.IO.File.Delete(dest);
    }

    [Fact]
    public async Task ReadRangeStreamsIntoTheSink()
    {
        var data = new byte[5000];
        new Random(3).NextBytes(data);
        var (c, fake, _) = TestClients.Make();
        fake.Handler = Serve(() => data, () => "c3");
        var got = new byte[5000];
        long n = 0;
        var r = await c.ReadRangeAsync("f1", 1000, 2000, (buf, off) => { buf.Span.CopyTo(got.AsSpan((int)off)); n += buf.Length; return Task.CompletedTask; });
        Assert.Equal(("c3", 5000L, 1000L, 2000L), (r.Etag, r.Total, r.Start, r.Count));
        Assert.Equal(data[1000..3000], got[1000..3000]);
        Assert.Equal("bytes=1000-2999", fake.Requests.Single().Headers["Range"]);
    }

    [Fact]
    public async Task ReadRangePastTheEndMeansTheFileShrank()
    {
        var (c, fake, _) = TestClients.Make();
        fake.Handler = Serve(() => new byte[10], () => "c3");
        var e = await Assert.ThrowsAsync<GatewayException>(() =>
            c.ReadRangeAsync("f1", 100, 10, (_, _) => Task.CompletedTask));
        Assert.Equal(GatewayErrorKind.ChangedDuringTransfer, e.Kind);
    }
}
