using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using Loft.Core;
using Xunit;

namespace Loft.Core.Tests;

public class RouteSelectorTests
{
    private static RouteConfig Cfg(PublicMode mode) =>
        new(TestClients.Lan, mode == PublicMode.None ? null : TestClients.Pub, mode, new PinSet(new[] { PairingTests.PinA }));

    [Fact]
    public async Task StartsOutsideAndMovesHomeWhenTheProbeSucceeds()
    {
        var s = new RouteSelector(Cfg(PublicMode.Tunnel));
        Assert.Equal(Route.Public, s.Current);
        Assert.False(s.CanPasswordSignIn);
        Assert.Equal(Route.Lan, await s.ReprobeAsync(_ => Task.FromResult(true)));
        Assert.True(s.AtHome);
        Assert.Equal(Route.Lan, s.SignInRoute);
        s.LanFailed();
        Assert.Equal(Route.Public, s.Current);
        Assert.Null(s.SignInRoute);
    }

    [Fact]
    public async Task LanOnlyStaysOnTheLan()
    {
        var s = new RouteSelector(Cfg(PublicMode.None));
        Assert.Equal(Route.Lan, await s.ReprobeAsync(_ => Task.FromResult(false)));
        Assert.False(s.AtHome);
        Assert.Null(s.SignInRoute);
    }

    [Fact]
    public async Task DirectModeSignsInOutside()
    {
        var s = new RouteSelector(Cfg(PublicMode.Direct));
        await s.ReprobeAsync(_ => Task.FromResult(false));
        Assert.Equal(Route.Public, s.SignInRoute);
        Assert.True(s.Config.IsPinned(Route.Public));
        Assert.False(Cfg(PublicMode.Tunnel).IsPinned(Route.Public));
    }

    [Fact]
    public async Task ProbesAgainOnlyWhenStale()
    {
        var now = DateTimeOffset.UnixEpoch;
        var s = new RouteSelector(Cfg(PublicMode.Tunnel), () => now);
        var probes = 0;
        Task<bool> P(CancellationToken _) { probes++; return Task.FromResult(true); }
        await s.EnsureFreshAsync(P);
        await s.EnsureFreshAsync(P);
        Assert.Equal(1, probes);
        now += TimeSpan.FromMinutes(6);
        await s.EnsureFreshAsync(P);
        Assert.Equal(2, probes);
        s.Invalidate();
        await s.EnsureFreshAsync(P);
        Assert.Equal(3, probes);
    }

    [Fact]
    public void FromPairingPrefersRefreshedPins()
    {
        var p = new Pairing("N", TestClients.Lan, new[] { PairingTests.PinA });
        Assert.True(RouteConfig.From(p).Pins.Contains(PairingTests.PinA));
        var cfg = RouteConfig.From(p, new[] { PairingTests.PinB });
        Assert.True(cfg.Pins.Contains(PairingTests.PinB));
        Assert.False(cfg.Pins.Contains(PairingTests.PinA));
        Assert.True(RouteConfig.From(p, new[] { "junk" }).Pins.Contains(PairingTests.PinA));
    }
}

/// <summary>Real TLS on loopback: the pinned handler accepts exactly the pinned key.</summary>
public class PinningTests
{
    private static X509Certificate2 SelfSigned(out string pin)
    {
        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        pin = Convert.ToBase64String(SHA256.HashData(key.ExportSubjectPublicKeyInfo()));
        var req = new CertificateRequest("CN=loft", key, HashAlgorithmName.SHA256);
        var san = new SubjectAlternativeNameBuilder();
        san.AddIpAddress(IPAddress.Loopback);
        req.CertificateExtensions.Add(san.Build());
        using var cert = req.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddDays(1));
        // Re-import so the private key is usable by every platform's TLS stack.
        return new X509Certificate2(cert.Export(X509ContentType.Pfx), (string?)null, X509KeyStorageFlags.Exportable);
    }

    /// <summary>A one-request-per-connection HTTPS server that answers /healthz like the gateway's direct listener.</summary>
    private static (int port, CancellationTokenSource stop) Serve(X509Certificate2 cert)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        var stop = new CancellationTokenSource();
        _ = Task.Run(async () =>
        {
            stop.Token.Register(listener.Stop);
            while (!stop.IsCancellationRequested)
            {
                TcpClient c;
                try { c = await listener.AcceptTcpClientAsync(stop.Token); }
                catch { return; }
                _ = Task.Run(async () =>
                {
                    using (c)
                    {
                        try
                        {
                            using var ssl = new SslStream(c.GetStream());
                            await ssl.AuthenticateAsServerAsync(cert);
                            var buf = new byte[4096];
                            var got = new StringBuilder();
                            while (!got.ToString().Contains("\r\n\r\n"))
                            {
                                var n = await ssl.ReadAsync(buf);
                                if (n == 0) return;
                                got.Append(Encoding.ASCII.GetString(buf, 0, n));
                            }
                            var body = """{"ok":true,"service":"loft","route":"direct","version":"test"}""";
                            var resp = $"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n{body}";
                            await ssl.WriteAsync(Encoding.ASCII.GetBytes(resp));
                        }
                        catch { /* handshake refused by the client: expected in the mismatch test */ }
                    }
                });
            }
        });
        return (port, stop);
    }

    [Fact]
    public void PinIsTheSpkiHash()
    {
        using var cert = SelfSigned(out var pin);
        Assert.Equal(pin, SpkiPin.Of(cert));
        Assert.True(SpkiPin.IsWellFormed(pin));
    }

    [Fact]
    public async Task PinnedHandlerAcceptsThePinnedKey()
    {
        using var cert = SelfSigned(out var pin);
        var (port, stop) = Serve(cert);
        using var stopper = stop;
        var cfg = new RouteConfig(new Uri($"https://127.0.0.1:{port}"), null, PublicMode.None, new PinSet(new[] { pin }));
        using var t = new HttpGatewayTransport(cfg);
        Assert.True(await RouteSelector.ProbeLanAsync(t, cfg));
    }

    [Fact]
    public async Task PinnedHandlerRefusesAnyOtherKey()
    {
        using var cert = SelfSigned(out _);
        var (port, stop) = Serve(cert);
        using var stopper = stop;
        var cfg = new RouteConfig(new Uri($"https://127.0.0.1:{port}"), null, PublicMode.None,
                                  new PinSet(new[] { PairingTests.PinA }));
        using var t = new HttpGatewayTransport(cfg);
        Assert.False(await RouteSelector.ProbeLanAsync(t, cfg));
        using var client = LoftHttp.Client(LoftHttp.PinnedHandler(cfg.Pins));
        await Assert.ThrowsAsync<HttpRequestException>(() => client.GetAsync($"https://127.0.0.1:{port}/healthz"));
    }

    [Fact]
    public async Task WebPkiHandlerRefusesASelfSignedGateway()
    {
        using var cert = SelfSigned(out _);
        var (port, stop) = Serve(cert);
        using var stopper = stop;
        using var client = LoftHttp.Client(LoftHttp.WebPkiHandler());
        await Assert.ThrowsAsync<HttpRequestException>(() => client.GetAsync($"https://127.0.0.1:{port}/healthz"));
    }

    [Fact]
    public async Task PinsCanRotateWithoutANewClient()
    {
        using var cert = SelfSigned(out var pin);
        var (port, stop) = Serve(cert);
        using var stopper = stop;
        var pins = new PinSet(new[] { PairingTests.PinA });
        var cfg = new RouteConfig(new Uri($"https://127.0.0.1:{port}"), null, PublicMode.None, pins);
        using var t = new HttpGatewayTransport(cfg);
        Assert.False(await RouteSelector.ProbeLanAsync(t, cfg));
        pins.Replace(new[] { PairingTests.PinA, pin });
        Assert.True(await RouteSelector.ProbeLanAsync(t, cfg));
    }
}
