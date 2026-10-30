// The device token: the only secret on this PC (docs/design.md §E.3: never the password).
// The Windows store (Credential Manager) lives in Loft.Sync; this is the contract.
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Loft.Core;

public sealed record StoredToken
{
    [JsonPropertyName("token")] public string Token { get; init; } = "";
    [JsonPropertyName("device_id")] public string DeviceId { get; init; } = "";
    [JsonPropertyName("issued_at")] public DateTimeOffset IssuedAt { get; init; }
    [JsonPropertyName("expires_at")] public DateTimeOffset ExpiresAt { get; init; }

    /// <summary>Rotate weekly; the gateway TTL is 90 days, sliding.</summary>
    public static readonly TimeSpan RotateAfter = TimeSpan.FromDays(7);

    public static StoredToken From(LoginResult r, DateTimeOffset? issuedAt = null) => new()
    {
        Token = r.Token,
        DeviceId = r.DeviceId,
        IssuedAt = issuedAt ?? DateTimeOffset.UtcNow,
        ExpiresAt = DateTimeOffset.FromUnixTimeMilliseconds((long)(r.ExpiresAt * 1000)),
    };

    public bool DueForRotation(DateTimeOffset now) => now - IssuedAt > RotateAfter;

    public byte[] Serialize() => JsonSerializer.SerializeToUtf8Bytes(this);
    public static StoredToken? Deserialize(ReadOnlySpan<byte> data)
    {
        try { return JsonSerializer.Deserialize<StoredToken>(data); }
        catch (JsonException) { return null; }
    }

    // Never let a token reach a log line through ToString().
    public override string ToString() => $"StoredToken(device {DeviceId}, issued {IssuedAt:u})";
}

public interface ITokenStore
{
    StoredToken? Load();
    void Save(StoredToken token);
    void Clear();
}

public sealed class MemoryTokenStore(StoredToken? initial = null) : ITokenStore
{
    private readonly object _lock = new();
    private StoredToken? _t = initial;
    public StoredToken? Load() { lock (_lock) return _t; }
    public void Save(StoredToken token) { lock (_lock) _t = token; }
    public void Clear() { lock (_lock) _t = null; }
}
