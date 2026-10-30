// Pairing payload (docs/pairing.md). The pins arrive with the address, so the
// very first connection is authenticated: no trust on first use, no bare "type an address".
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Loft.Core;

/// <summary>How the gateway's outside address is reached (pairing key <c>mode</c>).</summary>
public enum PublicMode
{
    /// <summary>No outside address: home network only.</summary>
    None,
    /// <summary>A TLS-ending proxy. Normal web PKI, device tokens only, never a password.</summary>
    Tunnel,
    /// <summary>A port forward to the direct listener: checked against the pins, sign-in allowed.</summary>
    Direct,
}

public sealed class PairingException(string message) : Exception(message);

/// <summary>One gateway this device is paired with (0.2 holds one).</summary>
public sealed record Pairing
{
    public const string LinkBase = "https://loft.incremental.no/pair#";
    public const int Version = 1;

    public string Name { get; }
    public Uri Lan { get; }
    /// <summary>base64 SHA-256 of the direct key's SubjectPublicKeyInfo: live first, then standby.</summary>
    public IReadOnlyList<string> Pins { get; }
    public Uri? Public { get; }
    public PublicMode Mode { get; }

    public Pairing(string name, Uri lan, IReadOnlyList<string> pins, Uri? pub = null, PublicMode mode = PublicMode.None)
    {
        if (string.IsNullOrWhiteSpace(name)) throw new PairingException("The pairing code has no gateway name.");
        if (!IsHttpsUrl(lan)) throw new PairingException("The gateway address must start with https://.");
        if (pins.Count == 0) throw new PairingException("The pairing code has no key fingerprints.");
        foreach (var p in pins)
            if (!SpkiPin.IsWellFormed(p)) throw new PairingException("The pairing code has a malformed key fingerprint.");
        if (pub is null && mode != PublicMode.None) throw new PairingException("The pairing code has a mode but no outside address.");
        if (pub is not null)
        {
            if (!IsHttpsUrl(pub)) throw new PairingException("The outside address must start with https://.");
            if (mode == PublicMode.None) throw new PairingException("The pairing code has an outside address but no mode.");
        }
        Name = name.Trim();
        Lan = Normalise(lan);
        Pins = pins.Distinct(StringComparer.Ordinal).ToArray();
        Public = pub is null ? null : Normalise(pub);
        Mode = mode;
    }

    /// <summary>The outside address may be used for a password sign-in only in direct mode.</summary>
    public bool PublicAllowsSignIn => Public is not null && Mode == PublicMode.Direct;

    // ── parsing ─────────────────────────────────────────────────────────

    /// <summary>
    /// Accepts the link (<c>https://loft.incremental.no/pair#…</c>), the desktop protocol form
    /// (<c>loft://pair#…</c>, also <c>loft://pair?…</c>), or the bare payload.
    /// </summary>
    public static Pairing Parse(string text)
    {
        var payload = ExtractPayload(text);
        byte[] raw;
        try { raw = Base64Url.Decode(payload); }
        catch (FormatException) { throw new PairingException("This isn't a Loft pairing code."); }
        JsonObject obj;
        try
        {
            obj = JsonNode.Parse(Encoding.UTF8.GetString(raw)) as JsonObject
                  ?? throw new PairingException("This isn't a Loft pairing code.");
        }
        catch (JsonException) { throw new PairingException("This isn't a Loft pairing code."); }

        if (obj["v"] is not JsonValue vv || !vv.TryGetValue(out int v))
            throw new PairingException("This isn't a Loft pairing code.");
        if (v != Version)
            throw new PairingException("This pairing code is from a newer gateway. Update Loft, then try again.");

        var name = Str(obj, "name") ?? throw new PairingException("The pairing code has no gateway name.");
        var lan = Url(Str(obj, "lan") ?? throw new PairingException("The pairing code has no address."));
        if (obj["pins"] is not JsonArray arr) throw new PairingException("The pairing code has no key fingerprints.");
        var pins = new List<string>();
        foreach (var n in arr)
        {
            if (n is not JsonValue pv || !pv.TryGetValue(out string? s) || s is null)
                throw new PairingException("The pairing code has a malformed key fingerprint.");
            pins.Add(s);
        }
        Uri? pub = Str(obj, "pub") is { } ps ? Url(ps) : null;
        var mode = Str(obj, "mode") switch
        {
            null => PublicMode.None,
            "tunnel" => PublicMode.Tunnel,
            "direct" => PublicMode.Direct,
            _ => throw new PairingException("The pairing code has an unknown mode."),
        };
        return new Pairing(name, lan, pins, pub, mode);
    }

    public static bool TryParse(string text, out Pairing? pairing, out string? error)
    {
        try { pairing = Parse(text); error = null; return true; }
        catch (PairingException e) { pairing = null; error = e.Message; return false; }
    }

    /// <summary>"Set up manually": the address AND a pin, never an address alone.</summary>
    public static Pairing Manual(string address, string pin, string? name = null)
    {
        address = address.Trim();
        if (address.Length == 0) throw new PairingException("Type the gateway's address.");
        if (!address.Contains("://", StringComparison.Ordinal)) address = "https://" + address;
        var lan = Url(address);
        if (!HasPort(address)) lan = new UriBuilder(lan) { Port = DefaultDirectPort }.Uri;   // as Mac and Android
        pin = pin.Trim();
        if (pin.Length == 0) throw new PairingException("Type the key fingerprint. Without it Loft can't check it's your gateway.");
        if (!SpkiPin.IsWellFormed(pin)) throw new PairingException("That key fingerprint doesn't look right. It's 44 characters and ends with =.");
        return new Pairing(string.IsNullOrWhiteSpace(name) ? lan.Host : name!, lan, new[] { pin });
    }

    internal static string ExtractPayload(string text)
    {
        var t = (text ?? "").Trim();
        if (t.Length == 0) throw new PairingException("Paste the pairing link first.");
        var hash = t.IndexOf('#');
        if (t.Contains("://", StringComparison.Ordinal))
        {
            var head = hash >= 0 ? t[..hash] : t;
            var q = head.IndexOf('?');
            var path = (q >= 0 ? head[..q] : head).TrimEnd('/');
            var ok = path.Equals("https://loft.incremental.no/pair", StringComparison.OrdinalIgnoreCase)
                     || path.Equals("loft://pair", StringComparison.OrdinalIgnoreCase)
                     || path.Equals("loft:pair", StringComparison.OrdinalIgnoreCase);
            if (!ok) throw new PairingException("This isn't a Loft pairing link.");
            if (hash >= 0) return t[(hash + 1)..].Trim();
            if (q >= 0)
            {
                var query = head[(q + 1)..];
                foreach (var part in query.Split('&'))
                {
                    var eq = part.IndexOf('=');
                    if (eq < 0) return Uri.UnescapeDataString(part);
                    var key = part[..eq];
                    if (key is "p" or "payload" or "code") return Uri.UnescapeDataString(part[(eq + 1)..]);
                }
            }
            throw new PairingException("The pairing link is incomplete. Copy the whole link.");
        }
        return hash >= 0 ? t[(hash + 1)..].Trim() : t;
    }

    // ── encoding ────────────────────────────────────────────────────────

    public string Encode()
    {
        var o = new JsonObject
        {
            ["v"] = Version,
            ["name"] = Name,
            ["lan"] = Lan.GetLeftPart(UriPartial.Authority),
            ["pins"] = new JsonArray(Pins.Select(p => (JsonNode)JsonValue.Create(p)!).ToArray()),
        };
        if (Public is not null)
        {
            o["pub"] = Public.ToString().TrimEnd('/');
            o["mode"] = Mode == PublicMode.Direct ? "direct" : "tunnel";
        }
        var json = o.ToJsonString(new JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
        return Base64Url.Encode(Encoding.UTF8.GetBytes(json));
    }

    public string ToLink() => LinkBase + Encode();

    // ── helpers ─────────────────────────────────────────────────────────

    private static string? Str(JsonObject o, string key) =>
        o[key] is JsonValue v && v.TryGetValue(out string? s) && !string.IsNullOrWhiteSpace(s) ? s : null;

    private static Uri Url(string s)
    {
        if (!Uri.TryCreate(s.Trim(), UriKind.Absolute, out var u) || !IsHttpsUrl(u))
            throw new PairingException("The gateway address must start with https://.");
        return u;
    }

    /// <summary>The direct listener's port when the address has none (loft-gateway's default).</summary>
    public const int DefaultDirectPort = 8443;

    private static bool HasPort(string url)
    {
        var a = url[(url.IndexOf("://", StringComparison.Ordinal) + 3)..].Split('/', '?', '#')[0];
        if (a.StartsWith('[')) return a.Contains("]:", StringComparison.Ordinal);   // [v6]:port
        return a.Count(c => c == ':') == 1;                                          // host:port
    }

    private static bool IsHttpsUrl(Uri u) =>
        u.IsAbsoluteUri && u.Scheme == Uri.UriSchemeHttps && !string.IsNullOrEmpty(u.Host)
        && string.IsNullOrEmpty(u.UserInfo);

    private static Uri Normalise(Uri u)
    {
        var s = u.GetLeftPart(UriPartial.Path).TrimEnd('/') + "/";
        return new Uri(s);
    }
}

public static class Base64Url
{
    public static string Encode(byte[] data) =>
        Convert.ToBase64String(data).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    public static byte[] Decode(string s)
    {
        s = s.Trim();
        foreach (var c in s)
            if (!(char.IsAsciiLetterOrDigit(c) || c is '-' or '_' or '='))
                throw new FormatException("not base64url");
        s = s.TrimEnd('=').Replace('-', '+').Replace('_', '/');
        s += (s.Length % 4) switch { 2 => "==", 3 => "=", 0 => "", _ => throw new FormatException("bad length") };
        return Convert.FromBase64String(s);
    }
}
