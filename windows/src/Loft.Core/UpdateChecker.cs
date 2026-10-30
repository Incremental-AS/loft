// Update check (docs/design.md §E.4): at most daily, against https://loft.incremental.no/releases.json,
// the only request Loft makes to us. Switched off in Settings, it makes none.
//
// releases.json is not defined yet by the website stream. This reader accepts, for the platform
// key "windows":
//   {"windows": {"version": "0.2.1", "url": "https://…", "sha256": "…", "notes": "…"}}
//   {"platforms": {"windows": {…same…}}}
// and ignores everything else.
using System.Text.Json;

namespace Loft.Core;

public sealed record ReleaseInfo(string Version, Uri? Url, string? Sha256, string? Notes);

public sealed class UpdateChecker
{
    public static readonly Uri ReleasesUrl = new("https://loft.incremental.no/releases.json");
    public static readonly TimeSpan MinInterval = TimeSpan.FromDays(1);

    private readonly HttpClient _http;
    private readonly SettingsStore _settings;
    private readonly Func<DateTimeOffset> _now;
    private readonly Uri _url;

    public UpdateChecker(HttpClient http, SettingsStore settings, Func<DateTimeOffset>? clock = null, Uri? url = null)
    {
        _http = http; _settings = settings; _now = clock ?? (() => DateTimeOffset.UtcNow); _url = url ?? ReleasesUrl;
    }

    public bool IsDue()
    {
        var (enabled, last) = _settings.Get(s => (s.UpdateCheckEnabled, s.LastUpdateCheck));
        return enabled && (last is null || _now() - last.Value >= MinInterval || last.Value > _now());
    }

    /// <summary>
    /// Fetches releases.json if the check is on and the last one is a day old. Returns the newer release,
    /// or null (off, not due, failed, or up to date). A failed fetch also counts as today's check.
    /// </summary>
    public async Task<ReleaseInfo?> CheckIfDueAsync(string currentVersion, string platform = "windows", CancellationToken ct = default)
    {
        if (!IsDue()) return null;
        _settings.Update(s => s.LastUpdateCheck = _now());
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(TimeSpan.FromSeconds(20));
            using var req = new HttpRequestMessage(HttpMethod.Get, _url);
            using var resp = await _http.SendAsync(req, cts.Token).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) return null;
            var body = await resp.Content.ReadAsByteArrayAsync(cts.Token).ConfigureAwait(false);
            var rel = Parse(body, platform);
            return rel is not null && CompareVersions(rel.Version, currentVersion) > 0 ? rel : null;
        }
        catch (Exception e) when (e is HttpRequestException or OperationCanceledException or IOException)
        {
            if (ct.IsCancellationRequested) throw;
            return null;
        }
    }

    public static ReleaseInfo? Parse(byte[] json, string platform)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return null;
            JsonElement p;
            if (root.TryGetProperty(platform, out var direct) && direct.ValueKind == JsonValueKind.Object) p = direct;
            else if (root.TryGetProperty("platforms", out var ps) && ps.ValueKind == JsonValueKind.Object
                     && ps.TryGetProperty(platform, out var nested) && nested.ValueKind == JsonValueKind.Object) p = nested;
            else return null;
            var version = Str(p, "version");
            if (version is null || ParseVersion(version) is null) return null;
            Uri? url = Str(p, "url") is { } u && Uri.TryCreate(u, UriKind.Absolute, out var uu) && uu.Scheme == Uri.UriSchemeHttps ? uu : null;
            return new ReleaseInfo(version, url, Str(p, "sha256"), Str(p, "notes"));
        }
        catch (JsonException) { return null; }
    }

    private static string? Str(JsonElement e, string k) =>
        e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    /// <summary>Numeric dotted versions ("0.2.10" > "0.2.9"); a pre-release suffix sorts before the release.</summary>
    public static int CompareVersions(string a, string b)
    {
        var (na, pa) = ParseVersion(a) ?? (Array.Empty<int>(), "");
        var (nb, pb) = ParseVersion(b) ?? (Array.Empty<int>(), "");
        for (var i = 0; i < Math.Max(na.Length, nb.Length); i++)
        {
            var x = i < na.Length ? na[i] : 0;
            var y = i < nb.Length ? nb[i] : 0;
            if (x != y) return x.CompareTo(y);
        }
        if (pa == pb) return 0;
        if (pa.Length == 0) return 1;
        if (pb.Length == 0) return -1;
        return string.CompareOrdinal(pa, pb);
    }

    private static (int[] nums, string pre)? ParseVersion(string v)
    {
        v = v.Trim().TrimStart('v', 'V');
        var plus = v.IndexOf('+');
        if (plus >= 0) v = v[..plus];
        var dash = v.IndexOf('-');
        var pre = dash >= 0 ? v[(dash + 1)..] : "";
        var core = dash >= 0 ? v[..dash] : v;
        var parts = core.Split('.');
        var nums = new int[parts.Length];
        for (var i = 0; i < parts.Length; i++)
            if (!int.TryParse(parts[i], System.Globalization.NumberStyles.None, null, out nums[i])) return null;
        return (nums, pre);
    }
}
