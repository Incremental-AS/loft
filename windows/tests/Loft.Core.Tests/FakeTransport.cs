using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Loft.Core;

namespace Loft.Core.Tests;

/// <summary>Records every request and answers from a handler. No network.</summary>
internal sealed class FakeTransport : IGatewayTransport
{
    public sealed record Sent(Route Route, string Method, Uri Url, Dictionary<string, string> Headers, byte[]? Body)
    {
        public string Path => Url.AbsolutePath;
        public string? Query(string k) =>
            System.Web.HttpUtility.ParseQueryString(Url.Query)[k];
        public string BodyText => Body is null ? "" : Encoding.UTF8.GetString(Body);
    }

    public readonly List<Sent> Requests = new();
    public Func<Sent, HttpResponseMessage> Handler { get; set; } = _ => Json(404, new { error = "not_found" });

    public async Task<HttpResponseMessage> SendAsync(Route route, HttpRequestMessage request, HttpCompletionOption completion,
                                                     CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        var headers = request.Headers.ToDictionary(h => h.Key, h => string.Join(",", h.Value), StringComparer.OrdinalIgnoreCase);
        byte[]? body = request.Content is null ? null : await request.Content.ReadAsByteArrayAsync(ct);
        var s = new Sent(route, request.Method.Method, request.RequestUri!, headers, body);
        lock (Requests) Requests.Add(s);
        return Handler(s);
    }

    public static HttpResponseMessage Json(int status, object body) => new((HttpStatusCode)status)
    {
        Content = new ByteArrayContent(JsonSerializer.SerializeToUtf8Bytes(body))
        { Headers = { ContentType = new MediaTypeHeaderValue("application/json") } },
    };

    public static HttpResponseMessage Raw(int status, string text) =>
        new((HttpStatusCode)status) { Content = new StringContent(text) };

    public static HttpResponseMessage Bytes(int status, byte[] data, string etag, long? from = null, long? total = null)
    {
        var r = new HttpResponseMessage((HttpStatusCode)status) { Content = new ByteArrayContent(data) };
        r.Headers.ETag = new EntityTagHeaderValue($"\"{etag}\"");
        if (from is not null)
            r.Content.Headers.ContentRange = new ContentRangeHeaderValue(from.Value, from.Value + data.Length - 1, total!.Value);
        return r;
    }

    public static Item File(string id, string name, string parent = "root", long size = 0, string c = "c1", string m = "m1") =>
        new() { Id = id, ParentId = parent, Name = name, Type = "file", Size = size, ContentEtag = c, MetaEtag = m };

    public static Item Dir(string id, string name, string parent = "root", string m = "m1") =>
        new() { Id = id, ParentId = parent, Name = name, Type = "dir", ContentEtag = "c0", MetaEtag = m };
}

internal static class TestClients
{
    public static readonly Uri Lan = new("https://gw.example:8443/");
    public static readonly Uri Pub = new("https://files.example.org/");

    public static StoredToken Token(string t = "tok-1") =>
        new() { Token = t, DeviceId = "dev1", IssuedAt = DateTimeOffset.UtcNow, ExpiresAt = DateTimeOffset.UtcNow.AddDays(90) };

    public static (GatewayClient client, FakeTransport fake, MemoryTokenStore tokens) Make(
        PublicMode mode = PublicMode.Tunnel, bool lanUp = true, bool signedIn = true)
    {
        var cfg = new RouteConfig(Lan, mode == PublicMode.None ? null : Pub, mode, new PinSet(new[] { PairingTests.PinA }));
        var fake = new FakeTransport();
        var tokens = new MemoryTokenStore(signedIn ? Token() : null);
        var c = new GatewayClient(new RouteSelector(cfg), tokens, fake)
        {
            Probe = _ => Task.FromResult(lanUp),
            RetryDelays = new[] { TimeSpan.Zero, TimeSpan.Zero },
        };
        return (c, fake, tokens);
    }
}
