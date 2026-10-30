// HTTP client for the gateway API (api/openapi.yaml). Behaviour follows GatewayClient.swift: route
// fallback, token re-read on a rotation race, edge-5xx = unreachable, chunked resumable
// uploads, sliced Range downloads, and the same error codes.
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Loft.Core;

public sealed class GatewayClient
{
    public RouteSelector Selector { get; }
    public ITokenStore Tokens { get; }
    public IGatewayTransport Transport { get; }
    /// <summary>The pinned /healthz probe, run before requests when the last one is stale.</summary>
    public Func<CancellationToken, Task<bool>>? Probe { get; set; }
    /// <summary>Upload chunk / download slice sizes; tests make them small.</summary>
    public long ChunkLimit { get; set; } = 32L << 20;
    public long DownloadSlice { get; set; } = 32L << 20;
    /// <summary>Waits between retries after transport errors only.</summary>
    public TimeSpan[] RetryDelays { get; set; } =
        { TimeSpan.FromSeconds(1), TimeSpan.FromSeconds(2), TimeSpan.FromSeconds(4), TimeSpan.FromSeconds(8), TimeSpan.FromSeconds(16) };

    public GatewayClient(RouteSelector selector, ITokenStore tokens, IGatewayTransport transport)
    {
        Selector = selector; Tokens = tokens; Transport = transport;
    }

    /// <summary>The production wiring: pinned LAN, pinned or web-PKI outside, probe over the pinned LAN client.</summary>
    public static GatewayClient Create(RouteConfig cfg, ITokenStore tokens)
    {
        var transport = new HttpGatewayTransport(cfg);
        var sel = new RouteSelector(cfg);
        return new GatewayClient(sel, tokens, transport)
        {
            Probe = ct => RouteSelector.ProbeLanAsync(transport, cfg, ct),
        };
    }

    // ── core request ────────────────────────────────────────────────────

    internal sealed record Req(HttpMethod Method, string Path)
    {
        public IReadOnlyList<KeyValuePair<string, string>> Query { get; init; } = Array.Empty<KeyValuePair<string, string>>();
        public byte[]? Body { get; init; }
        public string ContentType { get; init; } = "application/json";
        public IReadOnlyDictionary<string, string>? Headers { get; init; }
        public bool Auth { get; init; } = true;
        /// <summary>Pin the request to one route (sign-in); no fallback then.</summary>
        public Route? Route { get; init; }
        public TimeSpan Timeout { get; init; } = TimeSpan.FromSeconds(60);
        public HttpCompletionOption Completion { get; init; } = HttpCompletionOption.ResponseContentRead;
    }

    /// <summary>
    /// One request. Falls back once to the outside address if the LAN stops answering mid-session,
    /// and retries once with a freshly read token if another part of the app rotated it (the old
    /// one stays valid for 120 s, so this is rare). Returns only 2xx; the caller disposes it.
    /// </summary>
    internal async Task<HttpResponseMessage> SendAsync(Req r, CancellationToken ct)
    {
        if (r.Route is null && Probe is not null) await Selector.EnsureFreshAsync(Probe, ct).ConfigureAwait(false);
        var triedPublic = r.Route is not null;
        var triedNewToken = false;
        while (true)
        {
            var route = r.Route ?? Selector.Current;
            var url = BuildUrl(Selector.Config.BaseUrl(route), r.Path, r.Query);
            using var msg = new HttpRequestMessage(r.Method, url);
            if (r.Body is not null)
            {
                msg.Content = new ByteArrayContent(r.Body);
                msg.Content.Headers.ContentType = new MediaTypeHeaderValue(r.ContentType);
            }
            if (r.Headers is not null)
                foreach (var (k, v) in r.Headers) msg.Headers.TryAddWithoutValidation(k, v);
            msg.Headers.CacheControl = new CacheControlHeaderValue { NoCache = true, NoStore = true };
            string? sentToken = null;
            if (r.Auth)
            {
                var t = Tokens.Load() ?? throw GatewayException.NotSignedIn();
                sentToken = t.Token;
                msg.Headers.Authorization = new AuthenticationHeaderValue("Bearer", t.Token);
            }

            HttpResponseMessage resp;
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(r.Timeout);
            try
            {
                resp = await Transport.SendAsync(route, msg, r.Completion, cts.Token).ConfigureAwait(false);
            }
            catch (Exception e) when (IsTransportFailure(e, ct))
            {
                if (!triedPublic && route == Route.Lan && Selector.Config.HasPublic)
                {
                    Selector.LanFailed();
                    triedPublic = true;
                    continue;
                }
                throw GatewayException.Unreachable(Describe(e), e);
            }

            var status = (int)resp.StatusCode;
            if (status is >= 200 and < 300) return resp;

            byte[] body;
            try { body = await resp.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false); }
            catch (Exception e) when (IsTransportFailure(e, ct)) { resp.Dispose(); throw GatewayException.Unreachable(Describe(e), e); }
            resp.Dispose();
            var decoded = ApiError.TryParse(body, status);
            // A proxy in front of a dead tunnel answers 502/521/523/530 with HTML:
            // that is "no answer from home", not a gateway verdict.
            if (decoded is null && status >= 500) throw GatewayException.Unreachable($"edge {status}");
            var err = decoded ?? new ApiError { Error = $"http_{status}", Status = status };
            if (status == 401 && err.Error == "invalid_token" && !triedNewToken && r.Auth
                && Tokens.Load() is { } fresh && fresh.Token != sentToken)
            {
                triedNewToken = true;
                continue;
            }
            throw GatewayException.FromApi(err);
        }
    }

    private static bool IsTransportFailure(Exception e, CancellationToken callerCt) =>
        !callerCt.IsCancellationRequested
        && e is HttpRequestException or TaskCanceledException or OperationCanceledException or IOException
            or System.Security.Authentication.AuthenticationException;

    private static string Describe(Exception e) => e is OperationCanceledException ? "timeout" : e.GetBaseException().Message;

    internal static Uri BuildUrl(Uri baseUrl, string path, IReadOnlyList<KeyValuePair<string, string>> query)
    {
        var sb = new StringBuilder(baseUrl.AbsoluteUri.TrimEnd('/')).Append('/').Append(path.TrimStart('/'));
        for (var i = 0; i < query.Count; i++)
            sb.Append(i == 0 ? '?' : '&').Append(Uri.EscapeDataString(query[i].Key)).Append('=')
              .Append(Uri.EscapeDataString(query[i].Value));
        return new Uri(sb.ToString());
    }

    internal async Task<T> CallAsync<T>(Req r, CancellationToken ct)
    {
        using var resp = await SendAsync(r, ct).ConfigureAwait(false);
        var data = await resp.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false);
        try
        {
            return JsonSerializer.Deserialize<T>(data, Json.Options)
                   ?? throw GatewayException.BadResponse((int)resp.StatusCode, typeof(T).Name + ": null");
        }
        catch (JsonException e)
        {
            throw GatewayException.BadResponse((int)resp.StatusCode, $"{typeof(T).Name}: {e.Message}");
        }
    }

    private static byte[] JsonBody(object o) => JsonSerializer.SerializeToUtf8Bytes(o, o.GetType(), Json.Options);
    private static string Seg(string id) => Uri.EscapeDataString(id);
    private static KeyValuePair<string, string> Q(string k, string v) => new(k, v);

    // ── unauthenticated ─────────────────────────────────────────────────

    /// <summary>Current pins, over whichever trusted route is up (pinned, or the tunnel's web PKI).</summary>
    public Task<Identity> IdentityAsync(CancellationToken ct = default) =>
        CallAsync<Identity>(new Req(HttpMethod.Get, "v1/identity") { Auth = false, Timeout = TimeSpan.FromSeconds(10) }, ct);

    /// <summary>Fetch the pins and use them from now on. Returns them, or null if the gateway sent none.</summary>
    public async Task<IReadOnlyList<string>?> RefreshPinsAsync(CancellationToken ct = default)
    {
        var id = await IdentityAsync(ct).ConfigureAwait(false);
        return Selector.Config.Pins.Replace(id.DirectSpkiSha256) ? id.DirectSpkiSha256 : null;
    }

    // ── auth ────────────────────────────────────────────────────────────

    /// <summary>
    /// Password sign-in. Only ever over a pinned route: the LAN when the probe found it, else a
    /// direct-mode outside address. A tunnel never sees a password. The password is sent once and
    /// dropped; only the returned token is kept (by the caller, via <see cref="ITokenStore"/>).
    /// </summary>
    public async Task<LoginResult> LoginAsync(string user, string password, string deviceName, CancellationToken ct = default)
    {
        if (Probe is not null) await Selector.ReprobeAsync(Probe, ct).ConfigureAwait(false);
        var route = Selector.SignInRoute ?? throw GatewayException.SignInNeedsHome();
        if (!Selector.Config.IsPinned(route)) throw GatewayException.SignInNeedsHome();   // belt and braces
        return await CallAsync<LoginResult>(new Req(HttpMethod.Post, "v1/auth/login")
        {
            Body = JsonBody(new Dictionary<string, string> { ["user"] = user, ["password"] = password, ["device_name"] = deviceName }),
            Auth = false, Route = route, Timeout = TimeSpan.FromSeconds(30),
        }, ct).ConfigureAwait(false);
    }

    /// <summary>Sign in with a code made on the LAN by an account the gateway allows codes for. Either route.</summary>
    public Task<LoginResult> CodeLoginAsync(string code, string deviceName, CancellationToken ct = default) =>
        CallAsync<LoginResult>(new Req(HttpMethod.Post, "v1/auth/code")
        {
            Body = JsonBody(new Dictionary<string, string> { ["code"] = code, ["device_name"] = deviceName }),
            Auth = false, Timeout = TimeSpan.FromSeconds(30),
        }, ct);

    /// <summary>Rotate the device token; stores the new one before returning it.</summary>
    public async Task<StoredToken> RotateTokenAsync(CancellationToken ct = default)
    {
        var r = await CallAsync<LoginResult>(new Req(HttpMethod.Post, "v1/auth/refresh"), ct).ConfigureAwait(false);
        var t = StoredToken.From(r);
        Tokens.Save(t);
        return t;
    }

    public Task<DevicesResponse> DevicesAsync(CancellationToken ct = default) =>
        CallAsync<DevicesResponse>(new Req(HttpMethod.Get, "v1/auth/devices"), ct);

    private sealed record Ok([property: System.Text.Json.Serialization.JsonPropertyName("ok")] bool Value);

    public async Task RevokeAsync(string deviceId, CancellationToken ct = default) =>
        await CallAsync<Ok>(new Req(HttpMethod.Delete, $"v1/auth/device/{Seg(deviceId)}"), ct).ConfigureAwait(false);

    // ── roots ───────────────────────────────────────────────────────────

    public Task<Me> MeAsync(CancellationToken ct = default) => CallAsync<Me>(new Req(HttpMethod.Get, "v1/me"), ct);

    public async Task<IReadOnlyList<string>> SharesAsync(CancellationToken ct = default) =>
        (await CallAsync<SharesResponse>(new Req(HttpMethod.Get, "v1/roots"), ct).ConfigureAwait(false)).Shares;

    public Task<FoldersResponse> FoldersAsync(string share, string path, CancellationToken ct = default) =>
        CallAsync<FoldersResponse>(new Req(HttpMethod.Get, "v1/roots") { Query = new[] { Q("share", share), Q("path", path) } }, ct);

    public Task<Me> SetRootAsync(string share, string path, CancellationToken ct = default) =>
        CallAsync<Me>(new Req(HttpMethod.Put, "v1/me/root")
        {
            Body = JsonBody(new Dictionary<string, string> { ["share"] = share, ["path"] = path }),
        }, ct);

    // ── items ───────────────────────────────────────────────────────────

    public Task<Item> ItemAsync(string id, CancellationToken ct = default) =>
        CallAsync<Item>(new Req(HttpMethod.Get, $"v1/items/{Seg(id)}"), ct);

    public Task<ChildrenPage> ChildrenAsync(string id, string? cursor, int limit = 500, CancellationToken ct = default)
    {
        var q = new List<KeyValuePair<string, string>> { Q("limit", limit.ToString()) };
        if (cursor is not null) q.Add(Q("cursor", cursor));
        return CallAsync<ChildrenPage>(new Req(HttpMethod.Get, $"v1/items/{Seg(id)}/children")
        { Query = q, Timeout = TimeSpan.FromSeconds(120) }, ct);
    }

    /// <summary>Every child of a folder, all pages.</summary>
    public async Task<List<Item>> AllChildrenAsync(string id, CancellationToken ct = default)
    {
        var all = new List<Item>();
        string? cursor = null;
        do
        {
            var p = await ChildrenAsync(id, cursor, 2000, ct).ConfigureAwait(false);
            all.AddRange(p.Items);
            cursor = p.NextCursor;
        } while (cursor is not null);
        return all;
    }

    public Task<ChangesPage> ChangesAsync(string anchor, int wait = 0, int limit = 500, CancellationToken ct = default) =>
        CallAsync<ChangesPage>(new Req(HttpMethod.Get, "v1/changes")
        {
            Query = new[] { Q("anchor", anchor), Q("wait", wait.ToString()), Q("limit", limit.ToString()) },
            Timeout = TimeSpan.FromSeconds(wait + 30),
        }, ct);

    public async Task<string> LatestAnchorAsync(CancellationToken ct = default) =>
        (await CallAsync<AnchorResponse>(new Req(HttpMethod.Get, "v1/changes/latest"), ct).ConfigureAwait(false)).Anchor;

    public Task<Item> MkdirAsync(string parentId, string name, CancellationToken ct = default) =>
        CallAsync<Item>(new Req(HttpMethod.Post, "v1/folders")
        {
            Body = JsonBody(new Dictionary<string, string> { ["parent_id"] = parentId, ["name"] = name }),
        }, ct);

    public Task<Item> PatchAsync(string id, string? name = null, string? parentId = null, string? ifMatch = null,
                                 CancellationToken ct = default)
    {
        var b = new Dictionary<string, string>();
        if (name is not null) b["name"] = name;
        if (parentId is not null) b["parent_id"] = parentId;
        if (ifMatch is not null) b["if_match"] = ifMatch;
        return CallAsync<Item>(new Req(HttpMethod.Patch, $"v1/items/{Seg(id)}") { Body = JsonBody(b) }, ct);
    }

    public Task<DeleteResponse> DeleteAsync(string id, string? ifMatch = null, CancellationToken ct = default) =>
        CallAsync<DeleteResponse>(new Req(HttpMethod.Delete, $"v1/items/{Seg(id)}")
        {
            Query = ifMatch is null ? Array.Empty<KeyValuePair<string, string>>() : new[] { Q("if_match", ifMatch) },
        }, ct);

    // ── upload (docs/design.md §D.5) ───────────────────────────────────────

    /// <summary><paramref name="near"/>: the parent (create) or the item (modify); picks the share for the temp file.</summary>
    public Task<UploadState> UploadCreateAsync(long size, string sha256, string? near = null, CancellationToken ct = default)
    {
        var b = new Dictionary<string, object> { ["size"] = size, ["sha256"] = sha256 };
        if (near is not null) b["near_id"] = near;
        return CallAsync<UploadState>(new Req(HttpMethod.Post, "v1/uploads") { Body = JsonBody(b) }, ct);
    }

    public Task<UploadState> UploadStatusAsync(string id, CancellationToken ct = default) =>
        CallAsync<UploadState>(new Req(HttpMethod.Get, $"v1/uploads/{Seg(id)}"), ct);

    public Task<UploadState> UploadChunkAsync(string id, long offset, byte[] data, CancellationToken ct = default) =>
        CallAsync<UploadState>(new Req(HttpMethod.Put, $"v1/uploads/{Seg(id)}")
        {
            Query = new[] { Q("offset", offset.ToString()) },
            Body = data, ContentType = "application/octet-stream",
            Headers = new Dictionary<string, string> { ["X-Chunk-Sha256"] = Hashing.Hex(data) },
            Timeout = TimeSpan.FromSeconds(300),
        }, ct);

    /// <summary>The gateway re-reads the whole file from the NAS before it answers (5 GB ≈ 40 s).</summary>
    public Task<CommitResponse> CommitAsync(string uploadId, CommitRequest req, CancellationToken ct = default) =>
        CallAsync<CommitResponse>(new Req(HttpMethod.Post, $"v1/uploads/{Seg(uploadId)}/commit")
        { Body = JsonBody(req), Timeout = TimeSpan.FromSeconds(600) }, ct);

    /// <summary>
    /// Upload a local file into a finished, hash-checked session and return its id. Resumes from a
    /// session <paramref name="resume"/> remembers for this content, from the gateway's
    /// <c>received</c> after a 409 wrong_offset, and after transport errors.
    /// </summary>
    public async Task<string> UploadAsync(string path, string? near = null, IUploadResume? resume = null,
                                          IProgress<(long done, long total)>? progress = null, CancellationToken ct = default)
    {
        long size;
        string sha;
        await using (var hs = OpenShared(path))
        {
            size = hs.Length;
            sha = await Hashing.HexAsync(hs, ct).ConfigureAwait(false);
        }
        UploadState? st = null;
        if (resume?.Find(sha, size) is { } known)
        {
            try { st = await UploadStatusAsync(known, ct).ConfigureAwait(false); }
            catch (GatewayException) { st = null; }
        }
        st ??= await UploadCreateAsync(size, sha, near, ct).ConfigureAwait(false);
        resume?.Remember(st.UploadId, sha, size);
        var chunk = Math.Min(st.ChunkMax ?? ChunkLimit, ChunkLimit);
        await using var fs = OpenShared(path);
        var offset = st.Received;
        var failures = 0;
        var recreated = false;
        while (offset < size)
        {
            ct.ThrowIfCancellationRequested();
            fs.Seek(offset, SeekOrigin.Begin);
            var want = (int)Math.Min(chunk, size - offset);
            var data = new byte[want];
            var got = await ReadFullyAsync(fs, data, ct).ConfigureAwait(false);
            if (got == 0) throw GatewayException.ChangedDuringTransfer();      // file shrank under us
            if (got < want) Array.Resize(ref data, got);
            try
            {
                st = await UploadChunkAsync(st.UploadId, offset, data, ct).ConfigureAwait(false);
                offset = st.Received;
                failures = 0;
                progress?.Report((offset, size));
            }
            catch (GatewayException e) when (e.Code == "wrong_offset" && e.Api!.Received is { } rec)
            {
                offset = rec;
            }
            catch (GatewayException e) when (e.Code == "no_such_upload" && !recreated)
            {
                recreated = true;                                   // expired (24 h) or gateway rebuilt
                st = await UploadCreateAsync(size, sha, near, ct).ConfigureAwait(false);
                resume?.Remember(st.UploadId, sha, size);
                offset = 0;
            }
            catch (GatewayException e) when (e.IsUnreachable)
            {
                if (failures >= RetryDelays.Length) throw;
                await Task.Delay(RetryDelays[failures], ct).ConfigureAwait(false);
                failures++;
                try { offset = (await UploadStatusAsync(st.UploadId, ct).ConfigureAwait(false)).Received; }
                catch (GatewayException) { /* keep our offset; the next PUT tells us */ }
            }
        }
        return st.UploadId;
    }

    /// <summary>Upload + commit in one go; forgets the resumable session once it's committed or dead.</summary>
    public async Task<CommitResponse> UploadAndCommitAsync(string path, CommitRequest req, IUploadResume? resume = null,
                                                           IProgress<(long, long)>? progress = null, CancellationToken ct = default)
    {
        var id = await UploadAsync(path, req.ItemId ?? req.ParentId, resume, progress, ct).ConfigureAwait(false);
        try
        {
            var r = await CommitAsync(id, req, ct).ConfigureAwait(false);
            resume?.Forget(id);
            return r;
        }
        catch (GatewayException e) when (e.Code is "file_hash_mismatch" or "no_such_upload")
        {
            resume?.Forget(id);
            throw;
        }
    }

    private static FileStream OpenShared(string path) =>
        new(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 1 << 16, useAsync: true);

    private static async Task<int> ReadFullyAsync(Stream s, byte[] buf, CancellationToken ct)
    {
        var n = 0;
        while (n < buf.Length)
        {
            var r = await s.ReadAsync(buf.AsMemory(n), ct).ConfigureAwait(false);
            if (r == 0) break;
            n += r;
        }
        return n;
    }

    // ── download (Range) ────────────────────────────────────────────────

    public sealed record RangeResult(string Etag, long Total, long Start, long Count);

    private static string EtagOf(HttpResponseMessage resp)
    {
        var raw = resp.Headers.ETag?.Tag
                  ?? (resp.Headers.TryGetValues("ETag", out var v) ? v.FirstOrDefault() : null) ?? "";
        return raw.Trim('"');
    }

    /// <summary>
    /// Stream the bytes from <paramref name="offset"/>, at most <paramref name="length"/> of them, into
    /// <paramref name="sink"/> (called with each buffer and the file offset it starts at). Returns the
    /// content etag the bytes belong to, the file size, and where they started (0 if the gateway sent the
    /// whole file). Retries transport errors only until the first byte arrives; after that the caller
    /// resumes from what it has.
    /// </summary>
    public async Task<RangeResult> ReadRangeAsync(string id, long offset, long length,
                                                  Func<ReadOnlyMemory<byte>, long, Task> sink, CancellationToken ct = default,
                                                  Action<string, long, long>? onStart = null)
    {
        var headers = new Dictionary<string, string> { ["Range"] = $"bytes={offset}-{offset + Math.Max(length, 1) - 1}" };
        var failures = 0;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            HttpResponseMessage resp;
            try
            {
                resp = await SendAsync(new Req(HttpMethod.Get, $"v1/items/{Seg(id)}/content")
                {
                    Headers = headers, Timeout = TimeSpan.FromSeconds(300),
                    Completion = HttpCompletionOption.ResponseHeadersRead,
                }, ct).ConfigureAwait(false);
            }
            catch (GatewayException e) when (e.IsUnreachable)
            {
                if (failures >= RetryDelays.Length) throw;
                await Task.Delay(RetryDelays[failures], ct).ConfigureAwait(false);
                failures++;
                continue;
            }
            catch (GatewayException e) when (e.Status == 416 && offset == 0 && headers.Count > 0)
            {
                headers.Clear();                                    // empty file: ask again without a Range
                continue;
            }
            catch (GatewayException e) when (e.Status == 416)
            {
                throw GatewayException.ChangedDuringTransfer();     // shorter on the NAS than we were told
            }
            using (resp)
            {
                var tag = EtagOf(resp);
                long start = offset, total;
                var cr = resp.Content.Headers.ContentRange;
                if ((int)resp.StatusCode == 206 && cr?.Length is { } len)
                {
                    total = len;
                    start = cr.From ?? offset;
                }
                else
                {
                    start = 0;                                      // 200: the whole file
                    total = resp.Content.Headers.ContentLength ?? -1;
                }
                // Before any byte: the caller checks the version and size (throw to refuse them).
                onStart?.Invoke(tag, total, start);
                await using var body = await resp.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
                var buf = new byte[1 << 20];
                long count = 0;
                while (true)
                {
                    int n;
                    try { n = await body.ReadAsync(buf, ct).ConfigureAwait(false); }
                    catch (Exception e) when (IsTransportFailure(e, ct)) { throw GatewayException.Unreachable(Describe(e), e); }
                    if (n == 0) break;
                    await sink(buf.AsMemory(0, n), start + count).ConfigureAwait(false);
                    count += n;
                }
                if (total < 0) total = count;
                return new RangeResult(tag, total, start, count);
            }
        }
    }

    /// <summary>
    /// Download an item's content into <paramref name="dest"/> (created/truncated) in Range slices, so a
    /// dropped connection resumes from the last byte written. Returns the content etag the bytes belong
    /// to. If the NAS copy changes mid-way it starts over (at most 3 times).
    /// </summary>
    public async Task<string> DownloadAsync(string id, string dest, IProgress<(long done, long total)>? progress = null,
                                            CancellationToken ct = default)
    {
        for (var restarts = 0; ; restarts++)
        {
            var tag = await DownloadOnceAsync(id, dest, progress, ct).ConfigureAwait(false);
            if (tag is not null) return tag;
            if (restarts >= 2) throw GatewayException.ChangedDuringTransfer();
        }
    }

    /// <summary>null = the content changed on the NAS between slices; the caller starts over.</summary>
    private async Task<string?> DownloadOnceAsync(string id, string dest, IProgress<(long, long)>? progress, CancellationToken ct)
    {
        await using var fs = new FileStream(dest, FileMode.Create, FileAccess.Write, FileShare.Read, 1 << 16, useAsync: true);
        long offset = 0;
        long? total = null;
        string? etag = null;
        var failures = 0;
        while (total is null || offset < total || (total == 0 && etag is null))
        {
            ct.ThrowIfCancellationRequested();
            var headers = total == 0
                ? new Dictionary<string, string>()
                : new Dictionary<string, string> { ["Range"] = $"bytes={offset}-{offset + DownloadSlice - 1}" };
            HttpResponseMessage resp;
            try
            {
                resp = await SendAsync(new Req(HttpMethod.Get, $"v1/items/{Seg(id)}/content")
                {
                    Headers = headers, Timeout = TimeSpan.FromSeconds(300),
                }, ct).ConfigureAwait(false);
            }
            catch (GatewayException e) when (e.IsUnreachable)
            {
                if (failures >= RetryDelays.Length) throw;
                await Task.Delay(RetryDelays[failures], ct).ConfigureAwait(false);
                failures++;
                continue;
            }
            catch (GatewayException e) when (e.Status == 416)
            {
                if (offset > 0) return null;                        // shrank on the NAS since the first slice
                total = 0;                                          // empty file: ask again without a Range
                continue;
            }
            using (resp)
            {
                failures = 0;
                var data = await resp.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false);
                var tag = EtagOf(resp);
                if (etag is not null && tag != etag) return null;
                etag = tag;
                var cr = resp.Content.Headers.ContentRange;
                if ((int)resp.StatusCode == 206 && cr?.Length is { } len)
                {
                    total = len;
                }
                else
                {
                    // 200: the gateway sent the whole file; start the write over from zero.
                    fs.SetLength(0);
                    fs.Seek(0, SeekOrigin.Begin);
                    offset = 0;
                    total = data.Length;
                }
                await fs.WriteAsync(data, ct).ConfigureAwait(false);
                offset += data.Length;
                progress?.Report((offset, total.Value));
                if (data.Length == 0) break;
            }
        }
        return etag ?? "";
    }
}

public static class Hashing
{
    public static string Hex(ReadOnlySpan<byte> data) => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();

    public static async Task<string> HexAsync(Stream s, CancellationToken ct = default) =>
        Convert.ToHexString(await SHA256.HashDataAsync(s, ct).ConfigureAwait(false)).ToLowerInvariant();

    public static async Task<string> HexOfFileAsync(string path, CancellationToken ct = default)
    {
        await using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete,
                                            1 << 16, useAsync: true);
        return await HexAsync(fs, ct).ConfigureAwait(false);
    }
}
