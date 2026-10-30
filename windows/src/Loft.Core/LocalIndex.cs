// What this PC has placed in the sync folder: gateway item id ↔ name, parent, versions, and
// whether a folder's children have been placed ("populated"). Paths are derived from the parent
// chain, so a folder rename moves every descendant's path with it. The Cloud Files placeholders
// carry the same id in their FileIdentity blob, so the index can be rebuilt from disk.
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Loft.Core;

public sealed class LocalEntry
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("parent")] public string? ParentId { get; set; }
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("dir")] public bool IsDir { get; set; }
    [JsonPropertyName("size")] public long Size { get; set; }
    [JsonPropertyName("mtime_ns")] public long MtimeNs { get; set; }
    [JsonPropertyName("c")] public string ContentEtag { get; set; } = "";
    [JsonPropertyName("m")] public string MetaEtag { get; set; } = "";
    /// <summary>Folders: the children are placed, so changes inside must be applied here.</summary>
    [JsonPropertyName("populated")] public bool Populated { get; set; }
    /// <summary>SHA-256 of this content version when the gateway knows it (lets an unchanged file skip a re-upload).</summary>
    [JsonPropertyName("h")] public string? Sha256 { get; set; }
    /// <summary>Last time this PC downloaded or saved the file: the cache's least-recently-used order.</summary>
    [JsonPropertyName("used")] public DateTimeOffset? LastUsed { get; set; }

    public static LocalEntry From(Item it, bool populated = false) => new()
    {
        Id = it.Id, ParentId = it.ParentId, Name = it.Name, IsDir = it.IsDirectory, Size = it.Size,
        MtimeNs = it.MtimeNs, ContentEtag = it.ContentEtag, MetaEtag = it.MetaEtag, Populated = populated,
        Sha256 = it.Sha256,
    };
}

public sealed class LocalIndex
{
    private readonly object _lock = new();
    private readonly Dictionary<string, LocalEntry> _byId = new(StringComparer.Ordinal);
    private readonly Dictionary<string, HashSet<string>> _children = new(StringComparer.Ordinal);

    public string RootId { get; }

    public LocalIndex(string rootId)
    {
        RootId = rootId;
        Put(new LocalEntry { Id = rootId, Name = "", IsDir = true });
    }

    /// <summary>NFC + case-insensitive, like NTFS and the gateway's name_key.</summary>
    public static string NameKey(string name) => name.Normalize(NormalizationForm.FormC).ToUpperInvariant();

    public LocalEntry? Get(string id) { lock (_lock) return _byId.GetValueOrDefault(id); }

    public int Count { get { lock (_lock) return _byId.Count; } }

    public IReadOnlyList<LocalEntry> Children(string id)
    {
        lock (_lock)
            return _children.TryGetValue(id, out var set) ? set.Select(c => _byId[c]).ToList() : new List<LocalEntry>();
    }

    public IReadOnlyList<LocalEntry> All() { lock (_lock) return _byId.Values.ToList(); }

    /// <summary>Path relative to the sync folder ('\'-separated), "" for the root, null if the chain is broken.</summary>
    public string? RelativePath(string id)
    {
        lock (_lock)
        {
            var parts = new List<string>();
            var cur = id;
            for (var guard = 0; guard < 4096; guard++)
            {
                if (cur == RootId) { parts.Reverse(); return string.Join('\\', parts); }
                if (!_byId.TryGetValue(cur, out var e) || e.ParentId is null) return null;
                parts.Add(e.Name);
                cur = e.ParentId;
            }
            return null;
        }
    }

    public LocalEntry? FindByPath(string relativePath)
    {
        lock (_lock)
        {
            var cur = _byId[RootId];
            foreach (var part in relativePath.Split(new[] { '\\', '/' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var hit = ChildByNameLocked(cur.Id, part);
                if (hit is null) return null;
                cur = hit;
            }
            return cur;
        }
    }

    public LocalEntry? ChildByName(string parentId, string name) { lock (_lock) return ChildByNameLocked(parentId, name); }

    private LocalEntry? ChildByNameLocked(string parentId, string name)
    {
        if (!_children.TryGetValue(parentId, out var set)) return null;
        var key = NameKey(name);
        foreach (var c in set)
            if (NameKey(_byId[c].Name) == key) return _byId[c];
        return null;
    }

    /// <summary>
    /// Insert or update from a gateway item; keeps <see cref="LocalEntry.Populated"/> and
    /// <see cref="LocalEntry.LastUsed"/>, and the known SHA-256 while the content version is the same.
    /// </summary>
    public LocalEntry Upsert(Item it)
    {
        lock (_lock)
        {
            _byId.TryGetValue(it.Id, out var old);
            var e = LocalEntry.From(it, old?.Populated ?? false);
            e.LastUsed = old?.LastUsed;
            if (e.Sha256 is null && old is not null && old.ContentEtag == it.ContentEtag) e.Sha256 = old.Sha256;
            if (it.Id == RootId) e.ParentId = null;
            Put(e);
            return e;
        }
    }

    /// <summary>Marks a file as just used (downloaded or saved here).</summary>
    public void Touch(string id, DateTimeOffset when)
    {
        lock (_lock) if (_byId.TryGetValue(id, out var e)) e.LastUsed = when;
    }

    public void SetPopulated(string id, bool populated = true)
    {
        lock (_lock) if (_byId.TryGetValue(id, out var e)) e.Populated = populated;
    }

    /// <summary>Removes the entry and everything below it. Returns the ids removed.</summary>
    public IReadOnlyList<string> Remove(string id)
    {
        lock (_lock)
        {
            var gone = new List<string>();
            if (id == RootId) return gone;
            var stack = new Stack<string>();
            stack.Push(id);
            while (stack.Count > 0)
            {
                var cur = stack.Pop();
                if (_children.TryGetValue(cur, out var kids)) foreach (var k in kids.ToList()) stack.Push(k);
                if (_byId.Remove(cur, out var e))
                {
                    gone.Add(cur);
                    if (e.ParentId is not null && _children.TryGetValue(e.ParentId, out var sib)) sib.Remove(cur);
                }
                _children.Remove(cur);
            }
            return gone;
        }
    }

    public bool IsAncestor(string ancestorId, string id)
    {
        lock (_lock)
        {
            var cur = id;
            for (var guard = 0; guard < 4096 && cur is not null; guard++)
            {
                if (cur == ancestorId) return true;
                cur = _byId.TryGetValue(cur, out var e) ? e.ParentId! : null!;
            }
            return false;
        }
    }

    private void Put(LocalEntry e)
    {
        if (_byId.TryGetValue(e.Id, out var old) && old.ParentId is not null && _children.TryGetValue(old.ParentId, out var oldSib))
            oldSib.Remove(e.Id);
        _byId[e.Id] = e;
        if (e.ParentId is not null)
        {
            if (!_children.TryGetValue(e.ParentId, out var sib)) _children[e.ParentId] = sib = new HashSet<string>(StringComparer.Ordinal);
            sib.Add(e.Id);
        }
    }

    // ── persistence ─────────────────────────────────────────────────────

    private sealed record Snapshot(
        [property: JsonPropertyName("root")] string Root,
        [property: JsonPropertyName("entries")] List<LocalEntry> Entries);

    public void Save(string path)
    {
        Snapshot snap;
        lock (_lock) snap = new Snapshot(RootId, _byId.Values.ToList());
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        var tmp = path + ".tmp";
        File.WriteAllBytes(tmp, JsonSerializer.SerializeToUtf8Bytes(snap));
        File.Move(tmp, path, overwrite: true);
    }

    /// <summary>null if missing, unreadable, or made for another root.</summary>
    public static LocalIndex? Load(string path, string rootId)
    {
        try
        {
            if (!File.Exists(path)) return null;
            var snap = JsonSerializer.Deserialize<Snapshot>(File.ReadAllBytes(path));
            if (snap is null || snap.Root != rootId) return null;
            var idx = new LocalIndex(rootId);
            lock (idx._lock)
                foreach (var e in snap.Entries)
                    idx.Put(e);
            return idx;
        }
        catch (Exception e) when (e is JsonException or IOException) { return null; }
    }
}
