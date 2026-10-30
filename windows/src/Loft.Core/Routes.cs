// Route selection (docs/design.md §A, §C, §E.2). Mirrors RouteSelector.swift, with the
// outside modes: a tunnel URL is web PKI + device tokens only; a direct URL is pinned and allows sign-in.
using System.Text.Json;

namespace Loft.Core;

public enum Route
{
    /// <summary>The home network address from the pairing (pinned).</summary>
    Lan,
    /// <summary>The outside address: a tunnel (web PKI) or a port forward (pinned).</summary>
    Public,
}

public sealed class RouteConfig
{
    public Uri Lan { get; }
    public Uri? Public { get; }
    public PublicMode Mode { get; }
    public PinSet Pins { get; }
    public TimeSpan ProbeTimeout { get; init; } = TimeSpan.FromSeconds(1.5);
    public TimeSpan ReprobeEvery { get; init; } = TimeSpan.FromMinutes(5);

    public RouteConfig(Uri lan, Uri? pub, PublicMode mode, PinSet pins)
    {
        Lan = WithSlash(lan); Public = pub is null ? null : WithSlash(pub); Mode = pub is null ? PublicMode.None : mode; Pins = pins;
    }

    public static RouteConfig From(Pairing p, IEnumerable<string>? refreshedPins = null)
    {
        var pins = refreshedPins?.Where(SpkiPin.IsWellFormed).ToList();
        return new RouteConfig(p.Lan, p.Public, p.Mode, new PinSet(pins is { Count: > 0 } ? pins : p.Pins));
    }

    public bool HasPublic => Public is not null;
    /// <summary>Whether a route's TLS is checked against the pins (true) or web PKI (false).</summary>
    public bool IsPinned(Route r) => r == Route.Lan || Mode == PublicMode.Direct;
    public Uri BaseUrl(Route r) => r == Route.Lan ? Lan : Public ?? Lan;

    private static Uri WithSlash(Uri u) => u.AbsoluteUri.EndsWith('/') ? u : new Uri(u.AbsoluteUri + "/");
}

public sealed class RouteSelector
{
    private readonly object _lock = new();
    private Route _current;
    private bool _lanOk;
    private DateTimeOffset? _lastProbe;
    private readonly Func<DateTimeOffset> _now;

    public RouteConfig Config { get; }

    public RouteSelector(RouteConfig config, Func<DateTimeOffset>? clock = null)
    {
        Config = config;
        _now = clock ?? (() => DateTimeOffset.UtcNow);
        _current = config.HasPublic ? Route.Public : Route.Lan;
    }

    public Route Current { get { lock (_lock) return _current; } }
    public Uri BaseUrl => Config.BaseUrl(Current);
    /// <summary>True when the last probe found the gateway on the home network.</summary>
    public bool AtHome { get { lock (_lock) return _lanOk && _current == Route.Lan; } }

    /// <summary>
    /// Where a password may go right now, or null: the LAN when the probe found it, else a direct-mode
    /// outside address. Never a tunnel.
    /// </summary>
    public Route? SignInRoute
    {
        get
        {
            if (AtHome) return Route.Lan;
            if (Config.Mode == PublicMode.Direct) return Route.Public;
            return null;
        }
    }

    public bool CanPasswordSignIn => SignInRoute is not null;

    /// <summary>
    /// <paramref name="probe"/> = GET &lt;lan&gt;/healthz over the pinned client within ProbeTimeout; true only for
    /// 200 with {"service":"loft","route":"direct"}. A pin mismatch is a failed handshake, so false.
    /// </summary>
    public async Task<Route> ReprobeAsync(Func<CancellationToken, Task<bool>> probe, CancellationToken ct = default)
    {
        var ok = await probe(ct).ConfigureAwait(false);
        lock (_lock)
        {
            _lanOk = ok;
            _current = ok || !Config.HasPublic ? Route.Lan : Route.Public;
            _lastProbe = _now();
            return _current;
        }
    }

    public Task<Route> EnsureFreshAsync(Func<CancellationToken, Task<bool>> probe, CancellationToken ct = default)
    {
        bool stale;
        lock (_lock) stale = _lastProbe is null || _now() - _lastProbe.Value > Config.ReprobeEvery;
        return stale ? ReprobeAsync(probe, ct) : Task.FromResult(Current);
    }

    /// <summary>A request on the LAN got no answer: use the outside address until the next probe.</summary>
    public void LanFailed()
    {
        lock (_lock)
        {
            _lanOk = false;
            if (Config.HasPublic) _current = Route.Public;
        }
    }

    /// <summary>Network changed: probe again before the next request.</summary>
    public void Invalidate() { lock (_lock) _lastProbe = null; }

    public static async Task<bool> ProbeLanAsync(IGatewayTransport transport, RouteConfig cfg, CancellationToken ct = default)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(cfg.ProbeTimeout);
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, new Uri(cfg.Lan, "healthz"));
            using var resp = await transport.SendAsync(Route.Lan, req, HttpCompletionOption.ResponseContentRead, cts.Token)
                .ConfigureAwait(false);
            if ((int)resp.StatusCode != 200) return false;
            var body = await resp.Content.ReadAsByteArrayAsync(cts.Token).ConfigureAwait(false);
            var h = JsonSerializer.Deserialize<Health>(body);
            return h is { Service: "loft", Route: "direct" };
        }
        catch (Exception e) when (e is HttpRequestException or OperationCanceledException or JsonException
                                      or System.Security.Authentication.AuthenticationException or IOException)
        {
            if (ct.IsCancellationRequested) throw;
            return false;
        }
    }
}

/// <summary>Sends a request on a route. The real one keeps one pinned and one web-PKI client.</summary>
public interface IGatewayTransport
{
    Task<HttpResponseMessage> SendAsync(Route route, HttpRequestMessage request, HttpCompletionOption completion,
                                        CancellationToken ct);
}

public sealed class HttpGatewayTransport : IGatewayTransport, IDisposable
{
    private readonly HttpClient _lan;
    private readonly HttpClient _public;

    /// <summary>LAN: pinned. Outside: pinned in direct mode, web PKI in tunnel mode.</summary>
    public HttpGatewayTransport(RouteConfig cfg)
        : this(LoftHttp.PinnedHandler(cfg.Pins),
               cfg.Mode == PublicMode.Direct ? LoftHttp.PinnedHandler(cfg.Pins) : LoftHttp.WebPkiHandler())
    {
    }

    /// <summary>For tests: any handlers.</summary>
    public HttpGatewayTransport(HttpMessageHandler lan, HttpMessageHandler pub)
    {
        _lan = LoftHttp.Client(lan);
        _public = LoftHttp.Client(pub);
    }

    public Task<HttpResponseMessage> SendAsync(Route route, HttpRequestMessage request, HttpCompletionOption completion,
                                               CancellationToken ct) =>
        (route == Route.Lan ? _lan : _public).SendAsync(request, completion, ct);

    public void Dispose() { _lan.Dispose(); _public.Dispose(); }
}
