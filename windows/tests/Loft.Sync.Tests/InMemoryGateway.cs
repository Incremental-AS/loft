// A small in-process gateway with the same rules as gateway/loft_gateway (ids, etags, CAS with
// conflict copies, Range, chunked uploads, change feed), so the Cloud Files engine can be driven on
// a Windows runner without a real gateway.
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Loft.Core;

namespace Loft.Sync.Tests;

public sealed class InMemoryGateway : IGatewayTransport
{
    private sealed class Node
    {
        public string Id = Guid.NewGuid().ToString("N");
        public string? Parent;
        public string Name = "";
        public bool Dir;
        public byte[] Data = Array.Empty<byte>();
        public int Cv = 1, Mv = 1;
        public long Mtime = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() * 1_000_000;
        public bool Deleted;
        public long Seq;
    }

    private readonly object _lock = new();
    private readonly Dictionary<string, Node> _nodes = new();
    private readonly Dictionary<string, (long size, string sha, MemoryStream data)> _uploads = new();
    private long _seq = 1;
    private readonly SemaphoreSlim _changed = new(0);
    public string RootId { get; }
    public string DeviceName { get; set; } = "Test PC";
    public List<string> Log { get; } = new();

    public InMemoryGateway()
    {
        var root = new Node { Name = "", Dir = true };
        RootId = root.Id;
        _nodes[root.Id] = root;
    }

    // ── test-side helpers ───────────────────────────────────────────────

    public string AddFile(string parent, string name, byte[] data)
    {
        lock (_lock)
        {
            var n = new Node { Parent = parent, Name = name, Data = data, Seq = ++_seq };
            _nodes[n.Id] = n;
            Signal();
            return n.Id;
        }
    }

    public string AddDir(string parent, string name)
    {
        lock (_lock)
        {
            var n = new Node { Parent = parent, Name = name, Dir = true, Seq = ++_seq };
            _nodes[n.Id] = n;
            Signal();
            return n.Id;
        }
    }

    public void SetContent(string id, byte[] data)
    {
        lock (_lock)
        {
            var n = _nodes[id];
            n.Data = data; n.Cv++; n.Seq = ++_seq;
            Signal();
        }
    }

    public (string Id, string Name, byte[] Data, bool Deleted)? Find(string parent, string name)
    {
        lock (_lock)
        {
            var n = _nodes.Values.FirstOrDefault(x => x.Parent == parent && x.Name.Equals(name, StringComparison.OrdinalIgnoreCase) && !x.Deleted);
            return n is null ? null : (n.Id, n.Name, n.Data, n.Deleted);
        }
    }

    public bool IsDeleted(string id) { lock (_lock) return _nodes[id].Deleted; }
    public string NameOf(string id) { lock (_lock) return _nodes[id].Name; }
    public IReadOnlyList<string> Names(string parent)
    {
        lock (_lock) return _nodes.Values.Where(x => x.Parent == parent && !x.Deleted).Select(x => x.Name).OrderBy(x => x).ToList();
    }

    private void Signal() { if (_changed.CurrentCount == 0) _changed.Release(); }

    // ── wire ────────────────────────────────────────────────────────────

    private static object Wire(Node n) => new Dictionary<string, object?>
    {
        ["id"] = n.Id, ["parent_id"] = n.Parent, ["name"] = n.Name, ["type"] = n.Dir ? "dir" : "file",
        ["size"] = n.Dir ? 0 : n.Data.Length, ["mtime_ns"] = n.Mtime, ["content_etag"] = $"c{n.Cv}",
        ["meta_etag"] = $"m{n.Mv}", ["sha256"] = n.Dir ? null : Hashing.Hex(n.Data),
    };

    private static HttpResponseMessage Json(int status, object body) => new((HttpStatusCode)status)
    {
        Content = new ByteArrayContent(JsonSerializer.SerializeToUtf8Bytes(body))
        { Headers = { ContentType = new MediaTypeHeaderValue("application/json") } },
    };

    private static HttpResponseMessage Err(int status, string code, object? extra = null)
    {
        var d = new Dictionary<string, object?> { ["error"] = code };
        if (extra is not null)
            foreach (var p in extra.GetType().GetProperties()) d[p.Name] = p.GetValue(extra);
        return Json(status, d);
    }

    public async Task<HttpResponseMessage> SendAsync(Route route, HttpRequestMessage req, HttpCompletionOption completion, CancellationToken ct)
    {
        var path = req.RequestUri!.AbsolutePath;
        var q = System.Web.HttpUtility.ParseQueryString(req.RequestUri.Query);
        var body = req.Content is null ? Array.Empty<byte>() : await req.Content.ReadAsByteArrayAsync(ct);
        var m = req.Method.Method;
        lock (Log) Log.Add($"{m} {path}{req.RequestUri.Query}");

        if (path == "/healthz") return Json(200, new { ok = true, service = "loft", route = "direct" });
        if (path == "/v1/identity") return Json(200, new { direct_spki_sha256 = Array.Empty<string>() });
        if (path == "/v1/changes/latest") lock (_lock) return Json(200, new { anchor = $"e.{_seq}" });
        if (path == "/v1/changes")
        {
            var since = long.Parse(q["anchor"]!.Split('.')[1]);
            var wait = int.Parse(q["wait"] ?? "0");
            var deadline = DateTime.UtcNow.AddSeconds(Math.Min(wait, 2));
            while (true)
            {
                lock (_lock)
                {
                    var hits = _nodes.Values.Where(n => n.Seq > since && n.Id != RootId).ToList();
                    if (hits.Count > 0 || DateTime.UtcNow >= deadline)
                        return Json(200, new
                        {
                            updated = hits.Where(n => !n.Deleted).Select(Wire).ToList(),
                            deleted = hits.Where(n => n.Deleted).Select(n => n.Id).ToList(),
                            anchor = $"e.{_seq}",
                            more = false,
                        });
                }
                await _changed.WaitAsync(TimeSpan.FromMilliseconds(200), ct);
            }
        }

        Match mm;
        lock (_lock)
        {
            if ((mm = Regex.Match(path, "^/v1/items/([^/]+)$")).Success)
            {
                if (!_nodes.TryGetValue(mm.Groups[1].Value, out var n) || n.Deleted) return Err(404, "not_found");
                if (m == "GET") return Json(200, Wire(n));
                if (m == "PATCH")
                {
                    var b = JsonDocument.Parse(body).RootElement;
                    if (b.TryGetProperty("if_match", out var im) && im.GetString() != $"m{n.Mv}") return Err(412, "meta_etag_mismatch");
                    if (b.TryGetProperty("name", out var nm)) n.Name = nm.GetString()!;
                    if (b.TryGetProperty("parent_id", out var pp)) n.Parent = pp.GetString();
                    n.Mv++; n.Seq = ++_seq; Signal();
                    return Json(200, Wire(n));
                }
                if (m == "DELETE")
                {
                    if (!n.Dir && q["if_match"] is { } im && im != $"c{n.Cv}") return Err(412, "content_etag_mismatch");
                    n.Deleted = true; n.Seq = ++_seq; Signal();
                    return Json(200, new { result = "trashed" });
                }
            }
            if ((mm = Regex.Match(path, "^/v1/items/([^/]+)/children$")).Success)
            {
                var id = mm.Groups[1].Value;
                return Json(200, new
                {
                    items = _nodes.Values.Where(x => x.Parent == id && !x.Deleted).OrderBy(x => x.Name).Select(Wire).ToList(),
                    next_cursor = (string?)null,
                });
            }
            if ((mm = Regex.Match(path, "^/v1/items/([^/]+)/content$")).Success)
            {
                if (!_nodes.TryGetValue(mm.Groups[1].Value, out var n) || n.Deleted) return Err(404, "not_found");
                var size = n.Data.Length;
                HttpResponseMessage r;
                if (req.Headers.Range is { } range && range.Ranges.Count == 1)
                {
                    var rr = range.Ranges.First();
                    long a = rr.From ?? Math.Max(size - rr.To!.Value, 0), e = rr.From is null ? size - 1 : Math.Min(rr.To ?? size - 1, size - 1);
                    if (a >= size) return Err(416, "range_not_satisfiable");
                    r = new HttpResponseMessage(HttpStatusCode.PartialContent) { Content = new ByteArrayContent(n.Data[(int)a..(int)(e + 1)]) };
                    r.Content.Headers.ContentRange = new ContentRangeHeaderValue(a, e, size);
                }
                else r = new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(n.Data) };
                r.Headers.ETag = new EntityTagHeaderValue($"\"c{n.Cv}\"");
                return r;
            }
            if (path == "/v1/folders" && m == "POST")
            {
                var b = JsonDocument.Parse(body).RootElement;
                var parent = b.GetProperty("parent_id").GetString()!;
                var name = b.GetProperty("name").GetString()!;
                var ex = _nodes.Values.FirstOrDefault(x => x.Parent == parent && !x.Deleted && x.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
                if (ex is not null) return Err(409, "name_exists", new { existing_id = ex.Id });
                var d = new Node { Parent = parent, Name = name, Dir = true, Seq = ++_seq };
                _nodes[d.Id] = d; Signal();
                return Json(200, Wire(d));
            }
            if (path == "/v1/uploads" && m == "POST")
            {
                var b = JsonDocument.Parse(body).RootElement;
                var id = Guid.NewGuid().ToString("N");
                _uploads[id] = (b.GetProperty("size").GetInt64(), b.GetProperty("sha256").GetString()!, new MemoryStream());
                return Json(200, new { upload_id = id, received = 0, size = _uploads[id].size, chunk_max = 32 << 20 });
            }
            if ((mm = Regex.Match(path, "^/v1/uploads/([^/]+)$")).Success)
            {
                if (!_uploads.TryGetValue(mm.Groups[1].Value, out var u)) return Err(404, "no_such_upload");
                if (m == "PUT")
                {
                    var off = long.Parse(q["offset"]!);
                    if (off != u.data.Length) return Err(409, "wrong_offset", new { received = u.data.Length });
                    u.data.Write(body);
                }
                return Json(200, new { upload_id = mm.Groups[1].Value, received = u.data.Length, size = u.size });
            }
            if ((mm = Regex.Match(path, "^/v1/uploads/([^/]+)/commit$")).Success)
            {
                if (!_uploads.Remove(mm.Groups[1].Value, out var u)) return Err(404, "no_such_upload");
                var data = u.data.ToArray();
                if (Hashing.Hex(data) != u.sha) return Err(422, "file_hash_mismatch");
                var b = JsonDocument.Parse(body).RootElement;
                string? S(string k) => b.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
                var itemId = S("item_id"); var ifMatch = S("if_match"); var replaces = S("replaces");
                string result;
                Node target;
                if (itemId is not null)
                {
                    var t = _nodes[itemId];
                    if (ifMatch is not null && ifMatch != $"c{t.Cv}")
                    {
                        target = new Node { Parent = t.Parent, Name = Conflict(t.Name), Data = data, Seq = ++_seq };
                        _nodes[target.Id] = target;
                        result = "conflict";
                    }
                    else { t.Data = data; t.Cv++; t.Seq = ++_seq; target = t; result = "modified"; }
                    Signal();
                    return Json(200, new { result, item = Wire(target), replaced_id = (string?)null });
                }
                var parent = S("parent_id")!; var name = S("name")!;
                var ex = _nodes.Values.FirstOrDefault(x => x.Parent == parent && !x.Deleted && x.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
                string? replacedId = null;
                if (ex is not null)
                {
                    if (replaces == ex.Id && (ifMatch is null || ifMatch == $"c{ex.Cv}"))
                    {
                        ex.Deleted = true; ex.Seq = ++_seq; replacedId = ex.Id; result = "replaced";
                    }
                    else { name = Conflict(name); result = "conflict"; }
                }
                else result = "created";
                target = new Node { Parent = parent, Name = name, Data = data, Seq = ++_seq };
                _nodes[target.Id] = target;
                Signal();
                return Json(200, new { result, item = Wire(target), replaced_id = replacedId });
            }
        }
        return Err(404, "not_found");
    }

    private string Conflict(string name)
    {
        var dot = name.LastIndexOf('.');
        var (stem, ext) = dot > 0 ? (name[..dot], name[dot..]) : (name, "");
        return $"{stem} (conflict, {DeviceName}, {DateTime.Now:yyyy-MM-dd HHmmss}){ext}";
    }
}
