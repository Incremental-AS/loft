// Local-space rules that don't depend on Windows: which downloaded files to free when the cache is over
// its limit (least recently used first), and what to do with a delete made while offline.
using System.Text.Json.Serialization;

namespace Loft.Core;

/// <summary>A downloaded file Loft may free: not pinned, in sync, fully on disk.</summary>
public sealed record CachedFile(string Id, long Size, DateTimeOffset LastUsed);

public static class CachePolicy
{
    public const long DefaultLimit = 5L << 30;                   // 5 GB
    /// <summary>A file used this recently stays even over the limit (it's probably open).</summary>
    public static readonly TimeSpan MinIdle = TimeSpan.FromMinutes(5);

    /// <summary>The choices offered in Settings, in bytes.</summary>
    public static readonly long[] Choices = { 1L << 30, 2L << 30, 5L << 30, 10L << 30, 20L << 30, 50L << 30, 100L << 30 };

    /// <summary>
    /// Least recently used first, until the rest fits in <paramref name="limit"/>. Files used within
    /// <paramref name="minIdle"/> are never picked, so the total may stay over the limit for a while.
    /// </summary>
    public static IReadOnlyList<CachedFile> PickEvictions(IEnumerable<CachedFile> files, long limit, DateTimeOffset now,
                                                          TimeSpan? minIdle = null)
    {
        var all = files.ToList();
        var total = all.Sum(f => f.Size);
        var picked = new List<CachedFile>();
        if (total <= limit) return picked;
        var idle = minIdle ?? MinIdle;
        foreach (var f in all.OrderBy(f => f.LastUsed).ThenByDescending(f => f.Size))
        {
            if (total <= limit) break;
            if (now - f.LastUsed < idle) continue;
            picked.Add(f);
            total -= f.Size;
        }
        return picked;
    }
}

/// <summary>A delete Explorer made while the gateway was out of reach. <c>IfMatch</c> is the content version for files.</summary>
public sealed record PendingDelete(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("if_match")] string? IfMatch,
    [property: JsonPropertyName("path")] string Path,
    [property: JsonPropertyName("at")] DateTimeOffset At);

public enum PendingDeleteOutcome
{
    /// <summary>Gone on the NAS (now, or already): drop it from the queue.</summary>
    Done,
    /// <summary>Changed on the NAS since this PC saw it: don't delete; the NAS version comes back here.</summary>
    ChangedOnNas,
    /// <summary>Still can't reach the gateway or the NAS: keep it queued.</summary>
    KeepQueued,
    /// <summary>Refused for good (no access, a fixed share root): drop it; the item comes back here.</summary>
    Refused,
}

public static class OfflineDeletes
{
    /// <summary>A delete that failed this way is queued instead of refused.</summary>
    public static bool ShouldQueue(GatewayException e) =>
        e.IsUnreachable || e.Code is "nas_unavailable" || e.Status is 502 or 503 or 504;

    /// <summary>The queued folder deletes above <paramref name="d"/> (by path), which must not go ahead once it is kept.</summary>
    public static IReadOnlyList<PendingDelete> Ancestors(IEnumerable<PendingDelete> queue, PendingDelete d) =>
        queue.Where(x => x.Id != d.Id && x.IfMatch is null && x.Path.Length > 0
                         && d.Path.StartsWith(x.Path + "\\", StringComparison.OrdinalIgnoreCase)).ToList();

    /// <summary>What to do after replaying a queued delete (null = it went through).</summary>
    public static PendingDeleteOutcome Classify(GatewayException? e) => e switch
    {
        null => PendingDeleteOutcome.Done,
        { Code: "not_found" } => PendingDeleteOutcome.Done,
        { Status: 412 } => PendingDeleteOutcome.ChangedOnNas,
        _ when ShouldQueue(e) => PendingDeleteOutcome.KeepQueued,
        { Api.NeedsSignIn: true } or { Kind: GatewayErrorKind.NotSignedIn } => PendingDeleteOutcome.KeepQueued,
        _ => PendingDeleteOutcome.Refused,
    };
}
