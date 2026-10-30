// Wire models for the gateway API. Source of truth: gateway/loft_gateway/api.py + service.py
// (and api/openapi.yaml). Mirrors apple/Packages/LoftCore/Sources/LoftCore/Models.swift.
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Loft.Core;

public sealed record Item
{
    [JsonPropertyName("id")] public string Id { get; init; } = "";
    [JsonPropertyName("parent_id")] public string? ParentId { get; init; }
    [JsonPropertyName("name")] public string Name { get; init; } = "";
    /// <summary>"file" | "dir"</summary>
    [JsonPropertyName("type")] public string Type { get; init; } = "file";
    [JsonPropertyName("size")] public long Size { get; init; }
    [JsonPropertyName("mtime_ns")] public long MtimeNs { get; init; }
    [JsonPropertyName("content_etag")] public string ContentEtag { get; init; } = "";
    [JsonPropertyName("meta_etag")] public string MetaEtag { get; init; } = "";
    [JsonPropertyName("sha256")] public string? Sha256 { get; init; }
    /// <summary>All-shares root: true on the root and on each share (can't be renamed, moved or deleted).</summary>
    [JsonPropertyName("fixed")] public bool? Fixed { get; init; }

    [JsonIgnore] public bool IsDirectory => Type == "dir";
    [JsonIgnore] public bool IsFixed => Fixed ?? false;
    [JsonIgnore] public DateTimeOffset Modified => DateTimeOffset.FromUnixTimeMilliseconds(MtimeNs / 1_000_000);
}

public sealed record ChildrenPage(
    [property: JsonPropertyName("items")] IReadOnlyList<Item> Items,
    [property: JsonPropertyName("next_cursor")] string? NextCursor);

public sealed record ChangesPage(
    [property: JsonPropertyName("updated")] IReadOnlyList<Item> Updated,
    [property: JsonPropertyName("deleted")] IReadOnlyList<string> Deleted,
    [property: JsonPropertyName("anchor")] string Anchor,
    [property: JsonPropertyName("more")] bool More)
{
    [JsonIgnore] public bool IsEmpty => Updated.Count == 0 && Deleted.Count == 0;
}

public sealed record AnchorResponse([property: JsonPropertyName("anchor")] string Anchor);

public sealed record LoginResult(
    [property: JsonPropertyName("device_id")] string DeviceId,
    [property: JsonPropertyName("token")] string Token,
    [property: JsonPropertyName("nas_user")] string? NasUser,
    [property: JsonPropertyName("expires_at")] double ExpiresAt);

public sealed record RootInfo(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("share")] string Share,
    [property: JsonPropertyName("path")] string Path,
    [property: JsonPropertyName("root_item_id")] string RootItemId,
    [property: JsonPropertyName("anchor")] string Anchor)
{
    public const string AllShares = "*";
    [JsonIgnore] public bool IsAllShares => Share == AllShares;
    [JsonIgnore] public string DisplayPath => Path.Length == 0 ? Share : $"{Share}/{Path}";
}

public sealed record Me(
    [property: JsonPropertyName("nas_user")] string NasUser,
    [property: JsonPropertyName("device_id")] string DeviceId,
    [property: JsonPropertyName("device_name")] string DeviceName,
    [property: JsonPropertyName("route")] string Route,
    [property: JsonPropertyName("root")] RootInfo? Root);

public sealed record Device(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("device_name")] string DeviceName,
    [property: JsonPropertyName("created_at")] double CreatedAt,
    [property: JsonPropertyName("last_used_at")] double? LastUsedAt,
    [property: JsonPropertyName("revoked_at")] double? RevokedAt);

public sealed record DevicesResponse(
    [property: JsonPropertyName("devices")] IReadOnlyList<Device> Devices,
    [property: JsonPropertyName("current")] string Current);

public sealed record SharesResponse([property: JsonPropertyName("shares")] IReadOnlyList<string> Shares);

public sealed record FoldersResponse(
    [property: JsonPropertyName("share")] string Share,
    [property: JsonPropertyName("path")] string Path,
    [property: JsonPropertyName("folders")] IReadOnlyList<string> Folders);

public sealed record UploadState(
    [property: JsonPropertyName("upload_id")] string UploadId,
    [property: JsonPropertyName("received")] long Received,
    [property: JsonPropertyName("size")] long Size,
    [property: JsonPropertyName("chunk_max")] long? ChunkMax);

[JsonConverter(typeof(CommitResultConverter))]
public enum CommitResult { Created, Modified, Replaced, Conflict }

internal sealed class CommitResultConverter : JsonConverter<CommitResult>
{
    public override CommitResult Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options) =>
        reader.GetString() switch
        {
            "created" => CommitResult.Created,
            "modified" => CommitResult.Modified,
            "replaced" => CommitResult.Replaced,
            "conflict" => CommitResult.Conflict,
            var s => throw new JsonException($"unknown commit result {s}"),
        };

    public override void Write(Utf8JsonWriter writer, CommitResult value, JsonSerializerOptions options) =>
        writer.WriteStringValue(value.ToString().ToLowerInvariant());
}

public sealed record CommitRequest
{
    [JsonPropertyName("parent_id")] public string? ParentId { get; init; }
    [JsonPropertyName("name")] public string? Name { get; init; }
    /// <summary>Modify existing content.</summary>
    [JsonPropertyName("item_id")] public string? ItemId { get; init; }
    /// <summary>Content etag the edit is based on.</summary>
    [JsonPropertyName("if_match")] public string? IfMatch { get; init; }
    /// <summary>Safe-save: the item this create supersedes.</summary>
    [JsonPropertyName("replaces")] public string? Replaces { get; init; }
}

public sealed record CommitResponse(
    [property: JsonPropertyName("result")] CommitResult Result,
    [property: JsonPropertyName("item")] Item Item,
    [property: JsonPropertyName("replaced_id")] string? ReplacedId);

/// <summary>DELETE /v1/items/{id}: "trashed" | "superseded" | "already_deleted". All three are success.</summary>
public sealed record DeleteResponse(
    [property: JsonPropertyName("result")] string Result,
    [property: JsonPropertyName("superseded_by")] string? SupersededBy);

public sealed record Health(
    [property: JsonPropertyName("service")] string Service,
    [property: JsonPropertyName("route")] string Route);

public sealed record Identity(
    [property: JsonPropertyName("direct_spki_sha256")] IReadOnlyList<string> DirectSpkiSha256);

/// <summary>Gateway error body: {"error": "&lt;code&gt;", ...extra}. <see cref="Status"/> is the HTTP status.</summary>
public sealed record ApiError
{
    [JsonPropertyName("error")] public string Error { get; init; } = "";
    [JsonIgnore] public int Status { get; init; }
    /// <summary>409 wrong_offset / upload_incomplete.</summary>
    [JsonPropertyName("received")] public long? Received { get; init; }
    /// <summary>409 name_exists.</summary>
    [JsonPropertyName("existing_id")] public string? ExistingId { get; init; }
    /// <summary>412 *_etag_mismatch.</summary>
    [JsonPropertyName("current")] public Item? Current { get; init; }

    [JsonIgnore] public bool IsAnchorExpired => Error == "anchor_expired";
    [JsonIgnore] public bool NeedsSignIn => SyncError.FromLoopFailure(this)?.NeedsSignIn ?? false;
    /// <summary>The gateway answered but the NAS behind it did not: "Can't reach the NAS".</summary>
    [JsonIgnore] public bool IsNasUnreachable => Error is "nas_unavailable" or "nas_error";

    public static ApiError? TryParse(byte[] body, int status)
    {
        if (body.Length == 0) return null;
        try
        {
            var e = JsonSerializer.Deserialize<ApiError>(body, Json.Options);
            return e is null || string.IsNullOrEmpty(e.Error) ? null : e with { Status = status };
        }
        catch (JsonException) { return null; }
    }
}

internal static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
}
