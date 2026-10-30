using System.Text;
using Loft.Core;
using Xunit;

namespace Loft.Core.Tests;

public class PairingTests
{
    // sha256("a"), sha256("b"): well-formed pins that belong to no key.
    internal const string PinA = "ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=";
    internal const string PinB = "PiPoFgA5WUoziU9lZOGxNIu9egCI1CxKy3PurtWcAJ0=";

    // Made by the gateway's own encoder (gateway/loft_gateway/admin.py encode_payload).
    private const string GatewayTunnel =
        "eyJ2IjoxLCJuYW1lIjoiSGplbW1lIHDDpSBsb2Z0ZXQiLCJsYW4iOiJodHRwczovL2dhdGV3YXkuZXhhbXBsZTo4NDQzIiwicGlucyI6WyJ5cGVCRXNvYnZjcjZ3akd6bWlQY1RhZUc3L2dVZkU1eXVZQjNoYS91U0xzPSIsIlBpUG9GZ0E1V1VvemlVOWxaT0d4Tkl1OWVnQ0kxQ3hLeTNQdXJ0V2NBSjA9Il0sInB1YiI6Imh0dHBzOi8vZmlsZXMuZXhhbXBsZS5vcmciLCJtb2RlIjoidHVubmVsIn0";
    private const string GatewayLanOnly =
        "eyJ2IjoxLCJuYW1lIjoiSG9tZSIsImxhbiI6Imh0dHBzOi8vZ2F0ZXdheS5leGFtcGxlOjg0NDMiLCJwaW5zIjpbInlwZUJFc29idmNyNndqR3ptaVBjVGFlRzcvZ1VmRTV5dVlCM2hhL3VTTHM9Il19";  // gitleaks:allow (test key)

    private static string Payload(string json) => Base64Url.Encode(Encoding.UTF8.GetBytes(json));

    [Fact]
    public void ParsesTheGatewaysOwnEncoding()
    {
        var p = Pairing.Parse("https://loft.incremental.no/pair#" + GatewayTunnel);
        Assert.Equal("Hjemme på loftet", p.Name);
        Assert.Equal("https://gateway.example:8443/", p.Lan.ToString());
        Assert.Equal(new[] { PinA, PinB }, p.Pins);
        Assert.Equal("https://files.example.org/", p.Public!.ToString());
        Assert.Equal(PublicMode.Tunnel, p.Mode);
        Assert.False(p.PublicAllowsSignIn);

        var lan = Pairing.Parse(GatewayLanOnly);
        Assert.Null(lan.Public);
        Assert.Equal(PublicMode.None, lan.Mode);
    }

    [Fact]
    public void EncodesLikeTheGateway()
    {
        Assert.Equal(GatewayTunnel, Pairing.Parse(GatewayTunnel).Encode());
        Assert.Equal(GatewayLanOnly, Pairing.Parse(GatewayLanOnly).Encode());
        Assert.Equal("https://loft.incremental.no/pair#" + GatewayLanOnly, Pairing.Parse(GatewayLanOnly).ToLink());
    }

    [Theory]
    [InlineData("https://loft.incremental.no/pair#{0}")]
    [InlineData("  https://loft.incremental.no/pair/#{0}  ")]
    [InlineData("loft://pair#{0}")]
    [InlineData("loft://pair/#{0}")]
    [InlineData("loft://pair?{0}")]
    [InlineData("loft://pair?p={0}")]
    [InlineData("LOFT://PAIR#{0}")]
    [InlineData("{0}")]
    public void AcceptsEveryLinkForm(string form)
    {
        var p = Pairing.Parse(string.Format(form, GatewayLanOnly));
        Assert.Equal("Home", p.Name);
    }

    [Fact]
    public void DirectModeAllowsSignInOutside()
    {
        var p = Pairing.Parse(Payload($$"""{"v":1,"name":"N","lan":"https://gw.example:8443","pins":["{{PinA}}"],"pub":"https://files.example.org:8443","mode":"direct"}"""));
        Assert.Equal(PublicMode.Direct, p.Mode);
        Assert.True(p.PublicAllowsSignIn);
    }

    [Theory]
    [InlineData("""{"v":2,"name":"N","lan":"https://gw.example","pins":["PINA"]}""", "newer gateway")]
    [InlineData("""{"name":"N","lan":"https://gw.example","pins":["PINA"]}""", "isn't a Loft")]
    [InlineData("""{"v":"1","name":"N","lan":"https://gw.example","pins":["PINA"]}""", "isn't a Loft")]
    [InlineData("""{"v":1,"name":"N","lan":"http://gw.example","pins":["PINA"]}""", "https")]
    [InlineData("""{"v":1,"name":"N","lan":"https://user:pw@gw.example","pins":["PINA"]}""", "https")]
    [InlineData("""{"v":1,"name":"N","lan":"https://gw.example","pins":[]}""", "no key fingerprints")]
    [InlineData("""{"v":1,"name":"N","lan":"https://gw.example"}""", "no key fingerprints")]
    [InlineData("""{"v":1,"name":"N","lan":"https://gw.example","pins":["abc"]}""", "malformed key fingerprint")]
    [InlineData("""{"v":1,"name":"N","lan":"https://gw.example","pins":[5]}""", "malformed key fingerprint")]
    [InlineData("""{"v":1,"lan":"https://gw.example","pins":["PINA"]}""", "no gateway name")]
    [InlineData("""{"v":1,"name":"N","pins":["PINA"]}""", "no address")]
    [InlineData("""{"v":1,"name":"N","lan":"https://gw.example","pins":["PINA"],"pub":"http://out.example","mode":"tunnel"}""", "https")]
    [InlineData("""{"v":1,"name":"N","lan":"https://gw.example","pins":["PINA"],"pub":"https://out.example"}""", "no mode")]
    [InlineData("""{"v":1,"name":"N","lan":"https://gw.example","pins":["PINA"],"mode":"direct"}""", "no outside address")]
    [InlineData("""{"v":1,"name":"N","lan":"https://gw.example","pins":["PINA"],"pub":"https://out.example","mode":"vpn"}""", "unknown mode")]
    [InlineData("""[1,2]""", "isn't a Loft")]
    public void RefusesBadPayloads(string json, string why)
    {
        var e = Assert.Throws<PairingException>(() => Pairing.Parse(Payload(json.Replace("PINA", PinA))));
        Assert.Contains(why, e.Message);
    }

    [Theory]
    [InlineData("")]
    [InlineData("not base64 at all!")]
    [InlineData("https://evil.example/pair#eyJ2IjoxfQ")]
    [InlineData("https://loft.incremental.no/other#eyJ2IjoxfQ")]
    [InlineData("loft://stream?items=abc")]
    [InlineData("https://loft.incremental.no/pair")]
    public void RefusesGarbageAndForeignLinks(string text)
    {
        Assert.False(Pairing.TryParse(text, out var p, out var err));
        Assert.Null(p);
        Assert.False(string.IsNullOrEmpty(err));
    }

    [Fact]
    public void ManualNeedsAddressAndPin()
    {
        var p = Pairing.Manual("gw.example:8443", PinA);
        Assert.Equal("https://gw.example:8443/", p.Lan.ToString());
        Assert.Equal("gw.example", p.Name);
        Assert.Equal(new[] { PinA }, p.Pins);
        Assert.Equal("https://gw.example:8443/", Pairing.Manual("https://gw.example:8443", PinA).Lan.ToString());
        Assert.Equal("https://gw.example:8443/", Pairing.Manual("gw.example", PinA).Lan.ToString());
        Assert.Equal("https://gw.example:9443/", Pairing.Manual("https://gw.example:9443/", PinA).Lan.ToString());
        Assert.Equal("https://[fd00::5]:8443/", Pairing.Manual("[fd00::5]", PinA).Lan.ToString());
        Assert.Throws<PairingException>(() => Pairing.Manual("gw.example:8443", ""));
        Assert.Throws<PairingException>(() => Pairing.Manual("gw.example:8443", "deadbeef"));
        Assert.Throws<PairingException>(() => Pairing.Manual("http://gw.example:8443", PinA));
        Assert.Throws<PairingException>(() => Pairing.Manual("", PinA));
    }

    [Fact]
    public void DuplicatePinsCollapse()
    {
        var p = new Pairing("N", new Uri("https://gw.example"), new[] { PinA, PinA, PinB });
        Assert.Equal(new[] { PinA, PinB }, p.Pins);
    }
}
