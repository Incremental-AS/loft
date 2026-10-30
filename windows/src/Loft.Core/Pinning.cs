// SPKI pinning for the direct route (docs/design.md §C). The pin is the gateway's identity: a
// connection is accepted when the server key's SHA-256 is one of the pins, and refused
// otherwise. Never "accept any certificate".
using System.Net.Security;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace Loft.Core;

public static class SpkiPin
{
    /// <summary>base64(SHA-256(SubjectPublicKeyInfo DER)), the same value <c>loft-admin pair</c> prints.</summary>
    public static string Of(X509Certificate2 cert) =>
        Convert.ToBase64String(SHA256.HashData(cert.PublicKey.ExportSubjectPublicKeyInfo()));

    public static string Of(X509Certificate cert)
    {
        using var c2 = new X509Certificate2(cert);
        return Of(c2);
    }

    public static bool IsWellFormed(string pin)
    {
        if (string.IsNullOrWhiteSpace(pin) || pin.Length != 44) return false;
        try { return Convert.FromBase64String(pin).Length == 32; }
        catch (FormatException) { return false; }
    }
}

/// <summary>The live pin set. Replaced from <c>/v1/identity</c> after a trusted connection.</summary>
public sealed class PinSet
{
    private readonly object _lock = new();
    private HashSet<string> _pins;

    public PinSet(IEnumerable<string> pins) => _pins = new HashSet<string>(pins, StringComparer.Ordinal);

    public bool Contains(string pin) { lock (_lock) return _pins.Contains(pin); }

    public IReadOnlyCollection<string> Snapshot() { lock (_lock) return _pins.ToArray(); }

    /// <summary>Ignores an empty or malformed list: losing every pin would lock us out.</summary>
    public bool Replace(IEnumerable<string> pins)
    {
        var next = pins.Where(SpkiPin.IsWellFormed).ToHashSet(StringComparer.Ordinal);
        if (next.Count == 0) return false;
        lock (_lock) _pins = next;
        return true;
    }

    /// <summary>The check itself. Chain and name errors are expected (self-signed, IP SAN); the pin decides.</summary>
    public bool Accepts(X509Certificate? cert)
    {
        if (cert is null) return false;
        try { return Contains(SpkiPin.Of(cert)); }
        catch (CryptographicException) { return false; }
    }
}

public static class LoftHttp
{
    /// <summary>
    /// Handler for a pinned route: <see cref="HttpClientHandler.ServerCertificateCustomValidationCallback"/>
    /// accepts the server only if its key is pinned. No redirects, no cookies, no proxy credentials.
    /// </summary>
    public static HttpClientHandler PinnedHandler(PinSet pins)
    {
        var h = BaseHandler();
        h.ServerCertificateCustomValidationCallback = (_, cert, _, _) => pins.Accepts(cert);
        return h;
    }

    /// <summary>Handler for a tunnel route: the system's normal web PKI checks, nothing relaxed.</summary>
    public static HttpClientHandler WebPkiHandler()
    {
        var h = BaseHandler();
        h.ServerCertificateCustomValidationCallback = (_, _, _, errors) => errors == SslPolicyErrors.None;
        return h;
    }

    private static HttpClientHandler BaseHandler() => new()
    {
        AllowAutoRedirect = false,
        UseCookies = false,
        AutomaticDecompression = System.Net.DecompressionMethods.None,
        MaxConnectionsPerServer = 4,
    };

    public static HttpClient Client(HttpMessageHandler handler) =>
        new(handler, disposeHandler: true) { Timeout = Timeout.InfiniteTimeSpan };
}
