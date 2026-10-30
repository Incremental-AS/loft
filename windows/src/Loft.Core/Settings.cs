// Non-secret state: pairing, account, root, change anchor, preferences, upload resume map and
// the uploaded journal (for freeing space after evict_after). One JSON file per Windows user
// (%LOCALAPPDATA%\Loft\settings.json). Tokens never go here.
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Loft.Core;

public sealed class LoftSettings
{
    [JsonPropertyName("pairing")] public string? PairingPayload { get; set; }
    /// <summary>Pins last fetched from /v1/identity; empty = the pairing's.</summary>
    [JsonPropertyName("pins")] public List<string> RefreshedPins { get; set; } = new();
    [JsonPropertyName("nas_user")] public string? NasUser { get; set; }
    [JsonPropertyName("device_id")] public string? DeviceId { get; set; }
    [JsonPropertyName("device_name")] public string? DeviceName { get; set; }
    [JsonPropertyName("root")] public RootInfo? Root { get; set; }
    [JsonPropertyName("anchor")] public string? Anchor { get; set; }
    [JsonPropertyName("sync_folder")] public string? SyncFolder { get; set; }
    [JsonPropertyName("paused")] public bool Paused { get; set; }
    [JsonPropertyName("update_check")] public bool UpdateCheckEnabled { get; set; } = true;
    [JsonPropertyName("last_update_check")] public DateTimeOffset? LastUpdateCheck { get; set; }
    [JsonPropertyName("evict_after_minutes")] public int EvictAfterMinutes { get; set; } = 10;
    /// <summary>Space for downloaded files Loft keeps on its own (not "Always keep on this device" ones).</summary>
    [JsonPropertyName("cache_limit_bytes")] public long CacheLimitBytes { get; set; } = CachePolicy.DefaultLimit;
    /// <summary>Deletes made while the gateway couldn't be reached; sent when it can.</summary>
    [JsonPropertyName("pending_deletes")] public List<PendingDelete> PendingDeletes { get; set; } = new();
    /// <summary>"&lt;sha256&gt;:&lt;size&gt;" → upload id, so a retried save continues where it stopped.</summary>
    [JsonPropertyName("upload_resume")] public Dictionary<string, string> UploadResume { get; set; } = new();
    /// <summary>item id → when the gateway acknowledged its upload.</summary>
    [JsonPropertyName("uploaded")] public Dictionary<string, DateTimeOffset> Uploaded { get; set; } = new();

    [JsonIgnore]
    public Pairing? Pairing
    {
        get => PairingPayload is null ? null : Core.Pairing.TryParse(PairingPayload, out var p, out _) ? p : null;
        set { PairingPayload = value?.Encode(); RefreshedPins.Clear(); }
    }

    /// <summary>Everything account-bound goes on sign-out; the pairing and preferences stay.</summary>
    public void ClearAccount()
    {
        NasUser = null; DeviceId = null; Root = null; Anchor = null; Paused = false;
        UploadResume.Clear(); Uploaded.Clear(); PendingDeletes.Clear();
    }
}

public interface IUploadResume
{
    string? Find(string sha256, long size);
    void Remember(string uploadId, string sha256, long size);
    void Forget(string uploadId);
}

/// <summary>Loads and saves <see cref="LoftSettings"/> atomically; all access under one lock.</summary>
public sealed class SettingsStore : IUploadResume
{
    private readonly string _path;
    private readonly object _lock = new();
    private LoftSettings _s;

    public SettingsStore(string path)
    {
        _path = path;
        _s = Read(path) ?? new LoftSettings();
    }

    public static string DefaultPath =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Loft", "settings.json");

    /// <summary>Read under the lock; returns what <paramref name="f"/> returns.</summary>
    public T Get<T>(Func<LoftSettings, T> f) { lock (_lock) return f(_s); }

    /// <summary>Change and save in one step.</summary>
    public void Update(Action<LoftSettings> f)
    {
        lock (_lock)
        {
            f(_s);
            Write();
        }
    }

    /// <summary>
    /// Reads the file as text, so a byte-order mark (PowerShell 5 and some editors write one) doesn't make it
    /// unreadable. A file that still can't be read is kept as settings.json.unreadable rather than silently
    /// overwritten by the next save (which would forget the pairing and the account).
    /// </summary>
    internal static LoftSettings? Read(string path)
    {
        if (!File.Exists(path)) return null;
        try { return JsonSerializer.Deserialize<LoftSettings>(File.ReadAllText(path)); }
        catch (Exception e) when (e is JsonException or IOException or NotSupportedException)
        {
            Unreadable?.Invoke($"settings unreadable ({e.GetType().Name}: {e.Message}); kept as {Path.GetFileName(path)}.unreadable");
            try { File.Copy(path, path + ".unreadable", overwrite: true); }
            catch (IOException) { }
            return null;
        }
    }

    /// <summary>For the app's log: the settings file was there but couldn't be read.</summary>
    public static Action<string>? Unreadable { get; set; }

    private void Write()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        var tmp = _path + ".tmp";
        File.WriteAllBytes(tmp, JsonSerializer.SerializeToUtf8Bytes(_s, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(tmp, _path, overwrite: true);
    }

    // IUploadResume
    public string? Find(string sha256, long size) => Get(s => s.UploadResume.GetValueOrDefault($"{sha256}:{size}"));
    public void Remember(string uploadId, string sha256, long size) => Update(s => s.UploadResume[$"{sha256}:{size}"] = uploadId);
    public void Forget(string uploadId) => Update(s =>
    {
        foreach (var k in s.UploadResume.Where(kv => kv.Value == uploadId).Select(kv => kv.Key).ToList())
            s.UploadResume.Remove(k);
    });
}
