// The Windows sync engine: Cloud Files callbacks (folder listing, ranged hydration, deletes and
// renames from Explorer), the change feed applied to placeholders, and local changes (new files,
// edits, new folders, pin / "Free up space") uploaded through the gateway. The decisions come from
// Loft.Core (ChangePlanner, LocalRules); this class executes them against the file system.
using System.Collections.Concurrent;
using System.Runtime.Versioning;
using Loft.Core;

namespace Loft.Sync;

public sealed record EngineStatus(LoopState Loop, Route Route, bool AtHome, int Uploading, int Downloading, int Waiting,
                                  DateTimeOffset? LastSynced, bool Paused);

[SupportedOSPlatform("windows10.0.17763")]
public sealed class SyncEngine : ICloudCallbacks, IAsyncDisposable
{
    private readonly GatewayClient _client;
    private readonly SettingsStore _settings;
    private readonly Action<string> _log;
    private readonly string _indexPath;
    private readonly SemaphoreSlim _work = new(1, 1);
    private readonly ConcurrentDictionary<string, DateTime> _queue = new(StringComparer.OrdinalIgnoreCase);
    private readonly ConcurrentDictionary<string, DateTime> _suppressed = new(StringComparer.OrdinalIgnoreCase);
    private readonly ConcurrentDictionary<long, CancellationTokenSource> _fetches = new();
    private readonly ConcurrentDictionary<string, byte> _retryIds = new(StringComparer.Ordinal);
    private readonly List<ParkedItem> _parked = new();
    private readonly CancellationTokenSource _stop = new();
    private LocalIndex _index;
    private CloudFilter? _filter;
    private FileSystemWatcher? _watcher;
    private readonly List<Task> _loops = new();
    private int _uploading, _downloading;
    private LoopState _loop = LoopState.Ok;
    private int _indexDirty;
    /// <summary>
    /// Files renamed or moved in Explorer while in sync: id → their size and write time at that moment.
    /// Windows clears a placeholder's in-sync state on every rename; this tells "renamed only" from "edited".
    /// </summary>
    private readonly ConcurrentDictionary<string, (DateTime, long)> _renamed = new(StringComparer.Ordinal);
    private int _cacheCheckDue = 1;
    private DateTimeOffset _lastCacheCheck = DateTimeOffset.MinValue;
    private readonly SemaphoreSlim _replaying = new(1, 1);
    /// <summary>
    /// NAS items whose name was already taken on disk when their folder was listed or matched (left from an earlier
    /// setup, or made here meanwhile): a local file with the same bytes becomes that item instead of a conflict copy.
    /// </summary>
    private readonly ConcurrentDictionary<string, Item> _twins = new(StringComparer.Ordinal);
    private static string TwinKey(string parentId, string name) => parentId + "/" + LocalIndex.NameKey(name);

    public string Folder { get; }
    public RootInfo Root { get; }
    public string SyncRootId { get; }
    public SyncRoot.How Registration { get; private set; }
    public event Action<EngineStatus>? StatusChanged;
    public TimeSpan Debounce { get; set; } = TimeSpan.FromSeconds(2);

    public SyncEngine(GatewayClient client, SettingsStore settings, string folder, RootInfo root, string account,
                      Action<string> log, string? indexPath = null)
    {
        _client = client;
        _settings = settings;
        _log = log;
        Folder = Path.GetFullPath(folder).TrimEnd('\\');
        Root = root;
        SyncRootId = SyncRoot.IdFor(account);
        _indexPath = indexPath ?? Path.Combine(Path.GetDirectoryName(SettingsStore.DefaultPath)!, "index.json");
        _index = LocalIndex.Load(_indexPath, root.RootItemId) ?? new LocalIndex(root.RootItemId);
    }

    public LocalIndex Index => _index;

    private DateTimeOffset? _lastSynced;

    public EngineStatus Status => new(_loop, _client.Selector.Current, _client.Selector.AtHome, _uploading, _downloading,
        _queue.Count + _settings.Get(s => s.PendingDeletes.Count), _lastSynced, _settings.Get(s => s.Paused));

    private void Changed() => StatusChanged?.Invoke(Status);

    // ── lifecycle ───────────────────────────────────────────────────────

    /// <summary>Registers and connects the sync root, then starts the change feed, the watcher and housekeeping.</summary>
    public async Task StartAsync(string iconPath, string displayName = "Loft")
    {
        Registration = await SyncRoot.RegisterAsync(Folder, SyncRootId, displayName, iconPath, _log).ConfigureAwait(false);
        if (_index.Count <= 1 && Directory.EnumerateFileSystemEntries(Folder).Any()) RebuildIndex();
        _filter = CloudFilter.Connect(Folder, this, _log);

        _watcher = new FileSystemWatcher(Folder)
        {
            IncludeSubdirectories = true,
            NotifyFilter = NotifyFilters.FileName | NotifyFilters.DirectoryName | NotifyFilters.LastWrite
                           | NotifyFilters.Size | NotifyFilters.Attributes,
            InternalBufferSize = 64 * 1024,
        };
        _watcher.Created += (_, e) => Enqueue(e.FullPath);
        _watcher.Changed += (_, e) => Enqueue(e.FullPath);
        _watcher.Renamed += (_, e) => Enqueue(e.FullPath);
        _watcher.Error += (_, e) => { _log("watcher overflow: rescanning"); ScanLocal(); };
        _watcher.EnableRaisingEvents = true;

        ScanLocal();
        var ct = _stop.Token;
        _loops.Add(Task.Run(() => LocalLoopAsync(ct)));
        _loops.Add(Task.Run(() => FeedLoopAsync(ct)));
        _loops.Add(Task.Run(() => HousekeepingLoopAsync(ct)));
        TryDo(() => SyncRoot.NotifyShell(Folder), _log);
        _log($"engine started: {Folder} ({Registration}), root {Root.DisplayPath}");
    }

    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        if (_watcher is not null) { _watcher.EnableRaisingEvents = false; _watcher.Dispose(); }
        try { await Task.WhenAll(_loops).WaitAsync(TimeSpan.FromSeconds(10)).ConfigureAwait(false); }
        catch (Exception) { /* stopping */ }
        foreach (var f in _fetches.Values) f.Cancel();
        _filter?.Dispose();
        SaveIndex();
    }

    /// <summary>
    /// Sign-out: disconnect, remove what only lives on the NAS (dehydrated placeholders that are in sync), turn
    /// everything else back into plain files (nothing unsynced is ever deleted), and unregister the root.
    /// </summary>
    public static void RemoveSyncRoot(string folder, string syncRootId, Action<string> log)
    {
        if (Directory.Exists(folder))
        {
            Strip(folder, log);
        }
        SyncRoot.Unregister(folder, syncRootId, log);
        TryDo(() => SyncRoot.NotifyShell(folder), log);
        try { if (Directory.Exists(folder) && !Directory.EnumerateFileSystemEntries(folder).Any()) Directory.Delete(folder); }
        catch (IOException) { }
    }

    private static void Strip(string dir, Action<string> log)
    {
        foreach (var d in Directory.EnumerateDirectories(dir))
        {
            var st = SafeInspect(d);
            if (st.IsPlaceholder && (st.Attributes & Placeholders.FILE_ATTRIBUTE_RECALL_ON_OPEN) != 0)
            {
                TryDo(() => Directory.Delete(d, recursive: false), log);     // never opened: nothing local inside
                continue;
            }
            Strip(d, log);
            if (!Directory.EnumerateFileSystemEntries(d).Any()) TryDo(() => Directory.Delete(d), log);
            else if (st.IsPlaceholder) TryDo(() => Placeholders.Revert(d), log);
        }
        foreach (var f in Directory.EnumerateFiles(dir))
        {
            var st = SafeInspect(f);
            if (!st.IsPlaceholder) continue;
            if (st.InSync && st.Partial) TryDo(() => File.Delete(f), log);
            else TryDo(() => Placeholders.Revert(f), log);
        }
    }

    private static PlaceholderState SafeInspect(string p)
    {
        try { return Placeholders.Inspect(p); }
        catch (Exception) { return PlaceholderState.Missing; }
    }

    private static void TryDo(Action a, Action<string> log)
    {
        try { a(); }
        catch (Exception e) { log($"{e.GetType().Name}: {e.Message}"); }
    }

    // ── paths ───────────────────────────────────────────────────────────

    private bool TryRel(string full, out string rel)
    {
        full = Path.GetFullPath(full).TrimEnd('\\');
        if (full.Equals(Folder, StringComparison.OrdinalIgnoreCase)) { rel = ""; return true; }
        if (full.StartsWith(Folder + "\\", StringComparison.OrdinalIgnoreCase)) { rel = full[(Folder.Length + 1)..]; return true; }
        rel = "";
        return false;
    }

    private string? FullPath(string id) =>
        _index.RelativePath(id) is { } rel ? (rel.Length == 0 ? Folder : Path.Combine(Folder, rel)) : null;

    private IDisposable Suppress(params string[] paths)
    {
        foreach (var p in paths) _suppressed[Path.GetFullPath(p).TrimEnd('\\')] = DateTime.UtcNow.AddMinutes(5);
        return new Releaser(() =>
        {
            foreach (var p in paths) _suppressed[Path.GetFullPath(p).TrimEnd('\\')] = DateTime.UtcNow.AddSeconds(3);
        });
    }

    private bool IsSuppressed(string path) =>
        _suppressed.TryGetValue(Path.GetFullPath(path).TrimEnd('\\'), out var until) && until > DateTime.UtcNow;

    private sealed class Releaser(Action a) : IDisposable { public void Dispose() => a(); }

    private static NTSTATUS_ Map(Exception e) => e switch
    {
        GatewayException { IsUnreachable: true } => NTSTATUS_.Network,
        GatewayException { Status: 412 } => NTSTATUS_.NotInSync,
        GatewayException { Status: 403 } => NTSTATUS_.Denied,
        GatewayException g when g.Api?.NeedsSignIn == true || g.Kind == GatewayErrorKind.NotSignedIn => NTSTATUS_.Auth,
        GatewayException { Kind: GatewayErrorKind.ChangedDuringTransfer } => NTSTATUS_.NotInSync,
        _ => NTSTATUS_.Failed,
    };

    private enum NTSTATUS_ { Ok, Network, NotInSync, Denied, Auth, Failed, InUse }

    private static Windows.Win32.Foundation.NTSTATUS Nt(NTSTATUS_ s) => s switch
    {
        NTSTATUS_.Ok => CloudFilter.Success,
        NTSTATUS_.Network => CloudFilter.NetworkUnavailable,
        NTSTATUS_.NotInSync => CloudFilter.NotInSync,
        NTSTATUS_.Denied => CloudFilter.AccessDenied,
        NTSTATUS_.Auth => CloudFilter.AuthFailed,
        NTSTATUS_.InUse => CloudFilter.InUse,
        _ => CloudFilter.Unsuccessful,
    };

    // ── Cloud Files callbacks ───────────────────────────────────────────

    public async Task FetchPlaceholdersAsync(CfRequest r)
    {
        LocalEntry? dir = null;
        if (TryRel(r.Path, out var rel)) dir = rel.Length == 0 ? _index.Get(Root.RootItemId) : _index.FindByPath(rel);
        if (dir is null && r.Id is { } fid) dir = _index.Get(fid.Id);
        if (dir is null)
        {
            _log($"fetch placeholders: unknown folder {r.Path}");
            CloudFilter.TransferPlaceholders(r, Array.Empty<Item>(), CloudFilter.NotInSync);
            return;
        }
        try
        {
            var gone = _settings.Get(s => s.PendingDeletes.Select(d => d.Id).ToHashSet());
            var kids = (await _client.AllChildrenAsync(dir.Id, _stop.Token).ConfigureAwait(false))
                .Where(k => !Exclusions.IsExcluded(k.Name) && !gone.Contains(k.Id)).ToList();
            var dirPath = rel.Length == 0 ? Folder : Path.Combine(Folder, rel);
            // Names already on disk (a file made here before the folder was listed) stay as they are.
            var fresh = kids.Where(k => !Path.Exists(Path.Combine(dirPath, k.Name))).ToList();
            foreach (var k in kids.Except(fresh))
                if (SafeInspect(Path.Combine(dirPath, k.Name)) is { IsPlaceholder: false }) _twins[TwinKey(dir.Id, k.Name)] = k;
            var hr = CloudFilter.TransferPlaceholders(r, fresh, CloudFilter.Success);
            if (hr.Failed) _log($"transfer placeholders {rel}: 0x{hr.Value:X8}");
            foreach (var k in fresh) _index.Upsert(k);
            _index.SetPopulated(dir.Id);
            MarkIndexDirty();
            _log($"listed {(rel.Length == 0 ? "\\" : rel)}: {fresh.Count} items");
        }
        catch (Exception e)
        {
            _log($"fetch placeholders {rel}: {e.Message}");
            CloudFilter.TransferPlaceholders(r, Array.Empty<Item>(), Nt(Map(e)));
            Track(e);
        }
    }

    public async Task FetchDataAsync(CfRequest r, long requiredOffset, long requiredLength, long optionalOffset, long optionalLength)
    {
        var fid = r.Id;
        if (fid is null)
        {
            CloudFilter.TransferData(r, default, requiredOffset, CloudFilter.NotInSync, requiredLength);
            return;
        }
        const long A = CloudFilter.Alignment;
        var start = requiredOffset / A * A;
        var end = requiredOffset + requiredLength;
        if (optionalLength > 0 && optionalOffset <= end)
            end = Math.Max(end, Math.Min(optionalOffset + optionalLength, end + (32L << 20)));
        end = Math.Min((end + A - 1) / A * A, r.FileSize);
        var cts = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        _fetches[r.TransferKey] = cts;
        Interlocked.Increment(ref _downloading);
        Changed();
        var next = start;                       // first byte not yet handed to the filter
        try
        {
            for (var attempt = 0; ; attempt++)
            {
                try
                {
                    var buf = new byte[1 << 20];
                    var fill = 0;
                    var bufStart = next;
                    await _client.ReadRangeAsync(fid.Id, next, end - next, (mem, off) =>
                    {
                        var src = mem.Span;
                        while (!src.IsEmpty)
                        {
                            var n = Math.Min(src.Length, buf.Length - fill);
                            src[..n].CopyTo(buf.AsSpan(fill));
                            fill += n;
                            src = src[n..];
                            if (fill == buf.Length)
                            {
                                Transfer(r, buf, bufStart);
                                bufStart += fill;
                                next = bufStart;
                                fill = 0;
                            }
                        }
                        return Task.CompletedTask;
                    }, cts.Token, onStart: (etag, total, from) =>
                    {
                        if (etag != fid.ContentEtag || total != r.FileSize || from != next)
                            throw GatewayException.ChangedDuringTransfer();
                    }).ConfigureAwait(false);
                    if (fill > 0)
                    {
                        Transfer(r, buf.AsSpan(0, fill), bufStart);
                        next = bufStart + fill;
                    }
                    if (next < Math.Min(requiredOffset + requiredLength, r.FileSize))
                        throw GatewayException.ChangedDuringTransfer();      // the NAS sent less than the file size
                    _index.Touch(fid.Id, DateTimeOffset.UtcNow);
                    MarkIndexDirty();
                    Interlocked.Exchange(ref _cacheCheckDue, 1);
                    return;
                }
                catch (GatewayException e) when (e.IsUnreachable && attempt < 2 && !cts.IsCancellationRequested)
                {
                    _log($"fetch {fid.Id}: {e.Message}; resuming at {next}");
                }
            }
        }
        catch (Exception e)
        {
            if (e is GatewayException { Kind: GatewayErrorKind.ChangedDuringTransfer })
            {
                _log($"fetch {fid.Id}: the NAS copy changed; refreshing the placeholder");
                _retryIds[fid.Id] = 0;
            }
            else _log($"fetch {fid.Id} [{requiredOffset}+{requiredLength}]: {e.Message}");
            var failFrom = Math.Max(next, start);
            var failLen = Math.Max(requiredOffset + requiredLength - failFrom, A);
            CloudFilter.TransferData(r, default, failFrom, Nt(e is OperationCanceledException ? NTSTATUS_.Failed : Map(e)),
                                     Math.Min(failLen, Math.Max(r.FileSize - failFrom, 0)));
            Track(e);
        }
        finally
        {
            _fetches.TryRemove(r.TransferKey, out _);
            cts.Dispose();
            Interlocked.Decrement(ref _downloading);
            Changed();
        }
    }

    private void Transfer(CfRequest r, ReadOnlySpan<byte> data, long offset)
    {
        var hr = CloudFilter.TransferData(r, data, offset, CloudFilter.Success);
        if (hr.Failed) throw new IOException($"TransferData at {offset}: 0x{hr.Value:X8}");
    }

    public void CancelFetchData(CfRequest r)
    {
        if (_fetches.TryGetValue(r.TransferKey, out var cts)) cts.Cancel();
    }

    public async Task NotifyDeleteAsync(CfRequest r, bool isDirectory)
    {
        if (IsSuppressed(r.Path) || r.Id is not { } fid)
        {
            CloudFilter.AckDelete(r, CloudFilter.Success);
            return;
        }
        try
        {
            var res = await _client.DeleteAsync(fid.Id, isDirectory ? null : fid.ContentEtag, _stop.Token).ConfigureAwait(false);
            _log($"deleted {r.Path}: {res.Result}");
            Forget(fid.Id);
            CloudFilter.AckDelete(r, CloudFilter.Success);
        }
        catch (GatewayException e) when (e.Code == "not_found")
        {
            Forget(fid.Id);
            CloudFilter.AckDelete(r, CloudFilter.Success);
        }
        catch (GatewayException e) when (OfflineDeletes.ShouldQueue(e))
        {
            // Offline: let Explorer delete it now and send the delete when the gateway is back. A file changed on
            // the NAS meanwhile is not deleted there (the content version no longer matches) and comes back here.
            TryRel(r.Path, out var rel);
            _settings.Update(s => s.PendingDeletes.Add(
                new PendingDelete(fid.Id, isDirectory ? null : fid.ContentEtag, rel, DateTimeOffset.UtcNow)));
            Forget(fid.Id);
            CloudFilter.AckDelete(r, CloudFilter.Success);
            _log($"deleted {r.Path} while offline: queued");
            Track(e);
            Changed();
        }
        catch (Exception e)
        {
            // Changed on the NAS since this PC saw it (412), offline, or not allowed: keep the file.
            _log($"delete {r.Path} refused: {e.Message}");
            if (e is GatewayException { Status: 412 }) _retryIds[fid.Id] = 0;
            CloudFilter.AckDelete(r, Nt(Map(e)));
            Track(e);
        }
    }

    private void Forget(string id)
    {
        _index.Remove(id);
        lock (_parked) _parked.RemoveAll(p => p.Id == id);
        MarkIndexDirty();
    }

    public async Task NotifyRenameAsync(CfRequest r, string target, bool isDirectory, bool targetInScope)
    {
        if (IsSuppressed(r.Path) || IsSuppressed(target) || r.Id is not { } fid)
        {
            CloudFilter.AckRename(r, CloudFilter.Success);
            return;
        }
        var newName = Path.GetFileName(target);
        var action = LocalRules.OnRename(newName, targetInScope && TryRel(target, out _));
        try
        {
            switch (action)
            {
                case RenameAction.DeleteRemote:
                    await _client.DeleteAsync(fid.Id, isDirectory ? null : fid.ContentEtag, _stop.Token).ConfigureAwait(false);
                    Forget(fid.Id);
                    _log($"moved out of Loft: {r.Path} (moved to the NAS trash)");
                    break;
                case RenameAction.Park when isDirectory:
                    CloudFilter.AckRename(r, CloudFilter.AccessDenied);      // a folder can't take a temp name
                    return;
                case RenameAction.Park:
                    if (_index.Get(fid.Id) is { } pk)
                    {
                        lock (_parked) _parked.Add(new ParkedItem(fid.Id, pk.ParentId ?? Root.RootItemId, pk.Name, fid.ContentEtag, DateTimeOffset.UtcNow));
                        _index.Remove(fid.Id);
                        MarkIndexDirty();
                    }
                    break;
                case RenameAction.Patch:
                    if (!isDirectory && SafeInspect(r.Path) is { InSync: true, Partial: false })
                        _renamed[fid.Id] = Stamp(r.Path);
                    TryRel(Path.GetDirectoryName(target)!, out var parentRel);
                    var parent = _index.FindByPath(parentRel) ?? await EnsureRemoteFolderAsync(parentRel).ConfigureAwait(false);
                    ParkedItem? wasParked;
                    lock (_parked)
                    {
                        wasParked = _parked.FirstOrDefault(p => p.Id == fid.Id);
                        if (wasParked is not null) _parked.Remove(wasParked);
                    }
                    var cur = _index.Get(fid.Id);
                    var oldParent = cur?.ParentId ?? wasParked?.ParentId;
                    var oldName = cur?.Name ?? wasParked?.OriginalName;
                    var name = newName != oldName ? newName : null;
                    var par = parent.Id != oldParent ? parent.Id : null;
                    if (name is null && par is null) { Enqueue(target); break; }
                    Item moved;
                    try { moved = await _client.PatchAsync(fid.Id, name, par, fid.MetaEtag, _stop.Token).ConfigureAwait(false); }
                    catch (GatewayException pe) when (pe.Status == 412)
                    {
                        // Renamed elsewhere since: names are last-writer-wins (content is CAS).
                        moved = await _client.PatchAsync(fid.Id, name, par, null, _stop.Token).ConfigureAwait(false);
                    }
                    _index.Upsert(moved);
                    MarkIndexDirty();
                    Enqueue(target);                                     // refresh the placeholder's identity
                    _log($"renamed {r.Path} -> {target}");
                    break;
            }
            CloudFilter.AckRename(r, CloudFilter.Success);
        }
        catch (Exception e)
        {
            _log($"rename {r.Path} -> {target} refused: {e.Message}");
            CloudFilter.AckRename(r, Nt(Map(e)));
            Track(e);
        }
    }

    public Task NotifyDehydrateAsync(CfRequest r)
    {
        CloudFilter.AckDehydrate(r, CloudFilter.Success);
        return Task.CompletedTask;
    }

    /// <summary>Makes sure every folder of <paramref name="rel"/> exists on the gateway (a folder made here a moment ago).</summary>
    private async Task<LocalEntry> EnsureRemoteFolderAsync(string rel)
    {
        var cur = _index.Get(Root.RootItemId)!;
        var path = Folder;
        foreach (var seg in rel.Split('\\', StringSplitOptions.RemoveEmptyEntries))
        {
            path = Path.Combine(path, seg);
            var next = _index.ChildByName(cur.Id, seg);
            if (next is null)
            {
                Item made;
                try { made = await _client.MkdirAsync(cur.Id, seg, _stop.Token).ConfigureAwait(false); }
                catch (GatewayException e) when (e.Code == "name_exists" && e.Api?.ExistingId is { } ex)
                {
                    made = await _client.ItemAsync(ex, _stop.Token).ConfigureAwait(false);
                }
                next = _index.Upsert(made);
                _index.SetPopulated(made.Id);
                MarkIndexDirty();
                Enqueue(path);                                           // convert it to a placeholder
            }
            cur = next;
        }
        return cur;
    }

    // ── local changes ───────────────────────────────────────────────────

    private void Enqueue(string path)
    {
        if (!TryRel(path, out var rel) || rel.Length == 0) return;
        _queue[Path.GetFullPath(path)] = DateTime.UtcNow + Debounce;
    }

    private void Requeue(string path, TimeSpan after) => _queue[path] = DateTime.UtcNow + after;

    /// <summary>Everything that may need work after a restart: new files, edits made while Loft wasn't running.</summary>
    private void ScanLocal()
    {
        try { Scan(Folder); }
        catch (Exception e) { _log($"scan: {e.Message}"); }

        void Scan(string dir)
        {
            foreach (var p in Directory.EnumerateFileSystemEntries(dir))
            {
                var st = SafeInspect(p);
                if (!st.Exists || Exclusions.IsExcluded(Path.GetFileName(p))) continue;
                if (!st.IsPlaceholder || !st.InSync || (st.Pin == CfPin.Pinned && st.Partial)) Enqueue(p);
                // Only folders whose children are on disk; listing an unopened one would populate it.
                if (st.IsDirectory && (!st.IsPlaceholder || (st.Attributes & Placeholders.FILE_ATTRIBUTE_RECALL_ON_OPEN) == 0))
                    Scan(p);
            }
        }
    }

    private async Task LocalLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(500, ct).ConfigureAwait(false); }
            catch (OperationCanceledException) { return; }
            if (_settings.Get(s => s.Paused)) continue;
            var now = DateTime.UtcNow;
            // Folders before files, shallow before deep: a file needs its folder on the NAS first.
            var due = _queue.Where(kv => kv.Value <= now).Select(kv => kv.Key)
                            .OrderBy(p => p.Count(c => c == '\\')).ToList();
            foreach (var p in due)
            {
                if (ct.IsCancellationRequested) return;
                if (!_queue.TryRemove(p, out var when)) continue;
                if (when > DateTime.UtcNow) { _queue[p] = when; continue; }      // touched again meanwhile
                await _work.WaitAsync(ct).ConfigureAwait(false);
                try { await ProcessLocalAsync(p, ct).ConfigureAwait(false); }
                catch (Exception e) when (!ct.IsCancellationRequested)
                {
                    _log($"local {p}: {e.GetType().Name}: {e.Message}");
                    Track(e);
                    Requeue(p, e is GatewayException { IsUnreachable: true } ? TimeSpan.FromSeconds(30) : TimeSpan.FromSeconds(10));
                }
                finally { _work.Release(); }
            }
            Changed();
        }
    }

    private async Task ProcessLocalAsync(string full, CancellationToken ct)
    {
        if (!TryRel(full, out var rel) || rel.Length == 0 || IsSuppressed(full)) return;
        var name = Path.GetFileName(full);
        if (Exclusions.IsExcluded(name)) return;
        var st = Placeholders.Inspect(full);
        if (!st.Exists) return;
        var parentRel = Path.GetDirectoryName(rel) ?? "";

        if (!st.IsPlaceholder)
        {
            var parent = _index.FindByPath(parentRel);
            if (parent is null)
            {
                Enqueue(Path.GetDirectoryName(full)!);                    // its folder first
                Requeue(full, TimeSpan.FromSeconds(3));
                return;
            }
            if (st.IsDirectory)
            {
                Item dir;
                var existed = false;
                try { dir = await _client.MkdirAsync(parent.Id, name, ct).ConfigureAwait(false); }
                catch (GatewayException e) when (e.Code == "name_exists" && e.Api?.ExistingId is { } ex)
                {
                    dir = await _client.ItemAsync(ex, ct).ConfigureAwait(false);
                    if (!dir.IsDirectory) { _log($"{rel}: a file with this name is on the NAS; folder kept here only"); return; }
                    existed = true;
                }
                if (existed)
                {
                    // The NAS already has this folder (made elsewhere, or set up here before): what's in it on the
                    // NAS is listed into it when it's opened, and files here that are the same as there are matched.
                    foreach (var k in await _client.AllChildrenAsync(dir.Id, ct).ConfigureAwait(false))
                        if (Path.Exists(Path.Combine(full, k.Name))) _twins[TwinKey(dir.Id, k.Name)] = k;
                    Placeholders.Convert(full, dir, populated: false);
                    _index.Upsert(dir);
                }
                else
                {
                    Placeholders.Convert(full, dir, populated: true);
                    _index.Upsert(dir);
                    _index.SetPopulated(dir.Id);
                }
                MarkIndexDirty();
                foreach (var child in Directory.EnumerateFileSystemEntries(full)) Enqueue(child);
                _log(existed ? $"folder {rel}: matched the NAS folder" : $"folder {rel} -> NAS");
                return;
            }
            await UploadNewAsync(full, rel, parent, ct).ConfigureAwait(false);
            return;
        }

        var fid = st.Identity;
        // Windows marks a placeholder "not in sync" on every rename or move. A folder, a file whose bytes aren't
        // here, and a file whose size and write time didn't change since the rename have nothing to upload:
        // just mark them in sync again, with the identity (versions) the rename gave them.
        if (!st.InSync && fid is not null && _index.Get(fid.Id) is { } known)
        {
            var renamedOnly = !st.IsDirectory && !st.Partial && _renamed.TryRemove(fid.Id, out var was)
                              && was == Stamp(full) && known.ContentEtag == fid.ContentEtag;
            if (st.IsDirectory || st.Partial || renamedOnly)
            {
                var newer = !st.IsDirectory && known.ContentEtag != fid.ContentEtag;     // only possible when Partial
                Placeholders.Update(full, ItemOf(known), markInSync: true, dehydrate: newer, withMetadata: newer);
                if (st.IsDirectory) return;
                st = Placeholders.Inspect(full);
                fid = st.Identity;
            }
        }
        if (st.IsDirectory)
        {
            if (!st.InSync) Placeholders.SetInSync(full);
            return;
        }
        if (!st.InSync && !st.Partial && fid is not null)
        {
            await UploadEditAsync(full, rel, fid, ct).ConfigureAwait(false);
            return;
        }
        if (st.InSync)
        {
            if (st.Pin == CfPin.Pinned && st.Partial)
            {
                _ = Task.Run(() => TryDo(() => Placeholders.Hydrate(full), _log));   // "Always keep on this device"
            }
            else if (st.Pin == CfPin.Unpinned && !st.Partial)
            {
                Placeholders.Dehydrate(full);                                // "Free up space"
                Placeholders.SetPin(full, CfPin.Unspecified);                // so opening it later keeps the bytes
                _log($"freed {rel}");
            }
            if (fid is not null && _index.Get(fid.Id) is { } e && (e.MetaEtag != fid.MetaEtag || e.ContentEtag != fid.ContentEtag)
                && e.ContentEtag == fid.ContentEtag)
            {
                Placeholders.Update(full, ItemOf(e), markInSync: true, dehydrate: false, withMetadata: false);
            }
        }
    }

    private static Item ItemOf(LocalEntry e) => new()
    {
        Id = e.Id, ParentId = e.ParentId, Name = e.Name, Type = e.IsDir ? "dir" : "file", Size = e.Size,
        MtimeNs = e.MtimeNs, ContentEtag = e.ContentEtag, MetaEtag = e.MetaEtag,
    };

    private static bool StillWriting(string path)
    {
        try
        {
            using var _ = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
            return false;
        }
        catch (IOException) { return true; }
        catch (UnauthorizedAccessException) { return true; }
    }

    private static (DateTime, long) Stamp(string p)
    {
        var fi = new FileInfo(p);
        return (fi.LastWriteTimeUtc, fi.Length);
    }

    private async Task UploadNewAsync(string full, string rel, LocalEntry parent, CancellationToken ct)
    {
        if (StillWriting(full)) { Requeue(full, TimeSpan.FromSeconds(5)); return; }
        // The NAS has a file with this name that this PC never placed here (kept from an earlier setup): the same
        // bytes make it that file again, without an upload. Different bytes upload as a conflict copy, as before.
        if (_twins.TryRemove(TwinKey(parent.Id, Path.GetFileName(full)), out var twin) && !twin.IsDirectory
            && twin.Sha256 is { } twinSha && new FileInfo(full).Length == twin.Size)
        {
            var stamp = Stamp(full);
            if (await Hashing.HexOfFileAsync(full, ct).ConfigureAwait(false) == twinSha && Stamp(full) == stamp)
            {
                Placeholders.Convert(full, twin, populated: false);
                _index.Upsert(twin);
                _index.Touch(twin.Id, DateTimeOffset.UtcNow);
                MarkIndexDirty();
                _log($"{rel}: same as the NAS copy; matched");
                return;
            }
        }
        CommitRequest req;
        lock (_parked) req = LocalRules.CommitForNewFile(parent.Id, Path.GetFileName(full), _parked.ToList(), _index);
        // Rewritten in place (a plain file now) with the same bytes as the NAS version: nothing to send.
        if (req is { ItemId: { } eid, IfMatch: { } etag } && await SameAsNasAsync(full, eid, etag, ct).ConfigureAwait(false) is { } same)
        {
            Placeholders.Convert(full, ItemOf(same), populated: false);
            _log($"{rel}: unchanged");
            return;
        }
        var before = Stamp(full);
        CommitResponse r;
        Interlocked.Increment(ref _uploading);
        Changed();
        try { r = await _client.UploadAndCommitAsync(full, req, _settings, ct: ct).ConfigureAwait(false); }
        finally { Interlocked.Decrement(ref _uploading); }
        if (r.ReplacedId is { } old) Forget(old);
        var path = full;
        switch (LocalRules.AfterCommit(req, r))
        {
            case CommitOutcome.RenameToConflictCopy:
                path = Path.Combine(Path.GetDirectoryName(full)!, r.Item.Name);
                using (Suppress(full, path)) File.Move(full, path);
                _retryFolder(parent.Id);
                _log($"{rel}: the name was taken on the NAS; kept as {r.Item.Name}");
                break;
            case CommitOutcome.RefetchOriginal:
                // Overwritten here and changed on the NAS: ours is now the conflict copy (it arrives through the
                // feed); this file becomes the NAS version again.
                var nas = await _client.ItemAsync(req.ItemId!, ct).ConfigureAwait(false);
                Placeholders.Convert(full, nas, populated: false);
                Refetch(full, rel, nas);
                _log($"{rel}: changed on the NAS too; your version is saved as {r.Item.Name}");
                return;
        }
        _index.Upsert(r.Item);
        MarkIndexDirty();
        if (Stamp(path) != before) { Requeue(path, Debounce); return; }     // written again meanwhile: next round
        Placeholders.Convert(path, r.Item, populated: false);
        _settings.Update(s => s.Uploaded[r.Item.Id] = DateTimeOffset.UtcNow);
        _index.Touch(r.Item.Id, DateTimeOffset.UtcNow);
        _log($"{rel} -> NAS ({r.Result.ToString().ToLowerInvariant()})");
    }

    /// <summary>Point the placeholder at the NAS version and drop the local bytes (retried later if the file is open).</summary>
    private void Refetch(string full, string rel, Item nas)
    {
        try { Placeholders.Update(full, nas, markInSync: true, dehydrate: true, withMetadata: true); }
        catch (Exception e) when (e is IOException or System.Runtime.InteropServices.COMException)
        {
            _log($"{rel}: open in another program; will refresh it later");
            _retryIds[nas.Id] = 0;
        }
        _index.Upsert(nas);
        MarkIndexDirty();
    }

    /// <summary>
    /// The index entry if <paramref name="full"/> holds exactly the bytes of NAS version <paramref name="etag"/>
    /// (same size and SHA-256, and not written to while hashing); null otherwise or when the hash isn't known.
    /// </summary>
    private async Task<LocalEntry?> SameAsNasAsync(string full, string id, string etag, CancellationToken ct)
    {
        if (_index.Get(id) is not { Sha256: { } sha } e || e.ContentEtag != etag) return null;
        var stamp = Stamp(full);
        if (stamp.Item2 != e.Size) return null;
        var hash = await Hashing.HexOfFileAsync(full, ct).ConfigureAwait(false);
        return hash == sha && Stamp(full) == stamp ? e : null;
    }

    private async Task UploadEditAsync(string full, string rel, FileIdentity fid, CancellationToken ct)
    {
        if (StillWriting(full)) { Requeue(full, TimeSpan.FromSeconds(5)); return; }
        // Saved again without a change (or touched): same bytes as the NAS version, nothing to send.
        if (await SameAsNasAsync(full, fid.Id, fid.ContentEtag, ct).ConfigureAwait(false) is { } same)
        {
            Placeholders.Update(full, ItemOf(same), markInSync: true, dehydrate: false, withMetadata: false);
            _log($"{rel}: unchanged");
            return;
        }
        var req = LocalRules.CommitForEdit(fid.Id, fid.ContentEtag);
        var before = Stamp(full);
        CommitResponse r;
        Interlocked.Increment(ref _uploading);
        Changed();
        try { r = await _client.UploadAndCommitAsync(full, req, _settings, ct: ct).ConfigureAwait(false); }
        catch (GatewayException e) when (e.Code is "not_found")
        {
            // Deleted on the NAS while edited here: keep the edit as a new file.
            Placeholders.Revert(full);
            Forget(fid.Id);
            Requeue(full, TimeSpan.Zero);
            return;
        }
        finally { Interlocked.Decrement(ref _uploading); }
        switch (LocalRules.AfterCommit(req, r))
        {
            case CommitOutcome.RefetchOriginal:
                // The NAS copy moved on: our bytes are now the gateway's conflict copy (it arrives through the
                // feed); this file goes back to the NAS version.
                var nas = await _client.ItemAsync(fid.Id, ct).ConfigureAwait(false);
                Refetch(full, rel, nas);
                _log($"{rel}: changed on the NAS too; your version is saved as {r.Item.Name}");
                break;
            default:
                _index.Upsert(r.Item);
                MarkIndexDirty();
                if (Stamp(full) != before) { Requeue(full, Debounce); return; }
                Placeholders.Update(full, r.Item, markInSync: true, dehydrate: false, withMetadata: false);
                _settings.Update(s => s.Uploaded[r.Item.Id] = DateTimeOffset.UtcNow);
                _index.Touch(r.Item.Id, DateTimeOffset.UtcNow);
                _log($"{rel} -> NAS (modified)");
                break;
        }
    }

    // ── the change feed ─────────────────────────────────────────────────

    private async Task FeedLoopAsync(CancellationToken ct)
    {
        var watcher = new ChangeWatcher(_client);
        while (!ct.IsCancellationRequested)
        {
            if (_settings.Get(s => s.Paused))
            {
                try { await Task.Delay(1000, ct).ConfigureAwait(false); } catch (OperationCanceledException) { return; }
                continue;
            }
            using var pauseCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            var pauseWatch = Task.Run(async () =>
            {
                while (!pauseCts.IsCancellationRequested && !_settings.Get(s => s.Paused))
                    await Task.Delay(1000, pauseCts.Token).ConfigureAwait(false);
                pauseCts.Cancel();
            });
            await watcher.RunAsync(_settings.Get(s => s.Anchor) ?? Root.Anchor,
                ApplyPageAsync, ResyncAsync,
                a => _settings.Update(s => s.Anchor = a),
                st =>
                {
                    var back = st.Kind == LoopStateKind.Ok && _loop.Kind != LoopStateKind.Ok;
                    _loop = st;
                    if (st.Kind == LoopStateKind.Ok) _lastSynced = DateTimeOffset.UtcNow;
                    Changed();
                    if (back) _ = Task.Run(() => ReplayDeletesAsync(_stop.Token));         // reachable again
                },
                pauseCts.Token).ConfigureAwait(false);
            pauseCts.Cancel();
            try { await pauseWatch.ConfigureAwait(false); } catch (OperationCanceledException) { }
        }
    }

    private async Task ApplyPageAsync(ChangesPage page)
    {
        await _work.WaitAsync(_stop.Token).ConfigureAwait(false);
        try { Apply(ChangePlanner.Plan(_index, page)); }
        finally { _work.Release(); }
        SaveIndexIfDirty();
    }

    /// <summary>Anchor unknown or expired: list every folder this PC has opened and reconcile it.</summary>
    private async Task ResyncAsync()
    {
        var folders = new Queue<string>();
        folders.Enqueue(Root.RootItemId);
        while (folders.Count > 0)
        {
            var id = folders.Dequeue();
            var e = _index.Get(id);
            if (e is null || !e.Populated) continue;
            List<Item> kids;
            try { kids = await _client.AllChildrenAsync(id, _stop.Token).ConfigureAwait(false); }
            catch (GatewayException ex) when (ex.Code == "not_found") { continue; }
            kids = kids.Where(k => !Exclusions.IsExcluded(k.Name)).ToList();
            await _work.WaitAsync(_stop.Token).ConfigureAwait(false);
            try { Apply(ChangePlanner.PlanFolder(_index, id, kids)); }
            finally { _work.Release(); }
            foreach (var k in kids.Where(k => k.IsDirectory)) folders.Enqueue(k.Id);
        }
        SaveIndexIfDirty();
        _log("resync done");
    }

    private void _retryFolder(string folderId) => _retryIds["folder:" + folderId] = 0;

    private void Apply(IReadOnlyList<RemoteOp> ops)
    {
        foreach (var op in ops)
        {
            try { ApplyOne(op); }
            catch (Exception e)
            {
                _log($"apply {op.Kind} {op.Id}: {e.GetType().Name}: {e.Message}");
                _retryIds[op.Id] = 0;
            }
        }
        if (ops.Count > 0) { MarkIndexDirty(); _settings.Update(_ => { }); }
    }

    private void ApplyOne(RemoteOp op)
    {
        var it = op.Item;
        switch (op.Kind)
        {
            case RemoteOpKind.CreatePlaceholder:
            {
                if (Exclusions.IsExcluded(it!.Name)) return;
                // Deleted here while offline and not changed on the NAS since: don't bring it back.
                if (_settings.Get(s => s.PendingDeletes.Any(d => d.Id == it.Id && (d.IfMatch is null || d.IfMatch == it.ContentEtag))))
                    return;
                var dir = FullPath(it.ParentId!);
                if (dir is null || !Directory.Exists(dir)) return;
                var target = Path.Combine(dir, it.Name);
                Placeholders.CreateResult res;
                using (Suppress(target)) res = Placeholders.Create(dir, it);
                if (res == Placeholders.CreateResult.Created
                    || (res == Placeholders.CreateResult.Exists && SafeInspect(target).Identity?.Id == it.Id))
                    _index.Upsert(it);
                else if (res == Placeholders.CreateResult.Exists)
                    _log($"{it.Name}: a local file has this name; it uploads as a conflict copy");
                else _retryIds[it.Id] = 0;
                return;
            }
            case RemoteOpKind.Move:
            {
                var from = FullPath(op.Id);
                var toDir = FullPath(it!.ParentId!);
                if (from is null || toDir is null) return;
                var to = Path.Combine(toDir, it.Name);
                var st = SafeInspect(from);
                if (!st.Exists)
                {
                    _index.Upsert(it);
                    if (!Path.Exists(to)) using (Suppress(to)) Placeholders.Create(toDir, it);
                    return;
                }
                if (Path.Exists(to) && !string.Equals(from, to, StringComparison.OrdinalIgnoreCase))
                {
                    _log($"move {from} -> {to}: target exists; will retry");
                    _retryIds[op.Id] = 0;
                    return;
                }
                using (Suppress(from, to))
                {
                    if (st.IsDirectory) Directory.Move(from, to);
                    else File.Move(from, to);
                }
                _index.Upsert(it);
                TryDo(() => Placeholders.Update(to, it, markInSync: st.InSync, dehydrate: false, withMetadata: false), _log);
                return;
            }
            case RemoteOpKind.DeleteLocal:
            {
                var path = FullPath(op.Id);
                if (path is not null) DeleteLocal(path);
                Forget(op.Id);
                return;
            }
            case RemoteOpKind.UpdateContent:
            {
                var path = FullPath(op.Id);
                if (path is null) return;
                var st = SafeInspect(path);
                if (!st.Exists) return;
                if (!st.InSync) return;          // edited here too: the upload's CAS turns it into a conflict copy
                Placeholders.Update(path, it!, markInSync: true, dehydrate: true, withMetadata: true);
                _index.Upsert(it!);
                if (st.Pin == CfPin.Pinned) _ = Task.Run(() => TryDo(() => Placeholders.Hydrate(path), _log));
                return;
            }
            case RemoteOpKind.UpdateMetadata:
            {
                if (op.Id == Root.RootItemId) { _index.Upsert(it!); return; }
                var path = FullPath(op.Id);
                var st = path is null ? PlaceholderState.Missing : SafeInspect(path);
                if (st.Exists && st.IsPlaceholder)
                    Placeholders.Update(path!, it!, markInSync: st.InSync, dehydrate: false, withMetadata: st.InSync && !it!.IsDirectory);
                _index.Upsert(it!);
                return;
            }
        }
    }

    /// <summary>Gone on the NAS. In-sync placeholders go; anything with local changes stays and uploads as new.</summary>
    private void DeleteLocal(string path)
    {
        var st = SafeInspect(path);
        if (!st.Exists) return;
        if (!st.IsDirectory)
        {
            if (st.IsPlaceholder && st.InSync) { using (Suppress(path)) File.Delete(path); }
            else
            {
                if (st.IsPlaceholder) Placeholders.Revert(path);
                Enqueue(path);
            }
            return;
        }
        foreach (var child in Directory.EnumerateFileSystemEntries(path).ToList()) DeleteLocal(child);
        if (!Directory.EnumerateFileSystemEntries(path).Any()) { using (Suppress(path)) Directory.Delete(path); }
        else
        {
            if (st.IsPlaceholder) Placeholders.Revert(path);
            Enqueue(path);
        }
    }

    // ── housekeeping ────────────────────────────────────────────────────

    private async Task HousekeepingLoopAsync(CancellationToken ct)
    {
        var lastPins = DateTimeOffset.MinValue;
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromSeconds(60), ct).ConfigureAwait(false); }
            catch (OperationCanceledException) { return; }
            try
            {
                SaveIndexIfDirty();
                await ReplayDeletesAsync(ct).ConfigureAwait(false);
                await RetryAsync(ct).ConfigureAwait(false);
                Evict();
                EnforceCacheLimit();
                if (_client.Tokens.Load() is { } t && t.DueForRotation(DateTimeOffset.UtcNow))
                    await _client.RotateTokenAsync(ct).ConfigureAwait(false);
                if (DateTimeOffset.UtcNow - lastPins > TimeSpan.FromHours(6))
                {
                    if (await _client.RefreshPinsAsync(ct).ConfigureAwait(false) is { } pins)
                        _settings.Update(s => s.RefreshedPins = pins.ToList());
                    lastPins = DateTimeOffset.UtcNow;
                }
            }
            catch (Exception e) when (!ct.IsCancellationRequested)
            {
                _log($"housekeeping: {e.Message}");
                Track(e);
            }
        }
    }

    private async Task RetryAsync(CancellationToken ct)
    {
        foreach (var key in _retryIds.Keys.ToList())
        {
            _retryIds.TryRemove(key, out _);
            List<RemoteOp> ops;
            if (key.StartsWith("folder:", StringComparison.Ordinal))
            {
                var id = key[7..];
                var kids = (await _client.AllChildrenAsync(id, ct).ConfigureAwait(false)).Where(k => !Exclusions.IsExcluded(k.Name)).ToList();
                ops = ChangePlanner.PlanFolder(_index, id, kids).ToList();
            }
            else
            {
                Item it;
                try { it = await _client.ItemAsync(key, ct).ConfigureAwait(false); }
                catch (GatewayException e) when (e.Code == "not_found")
                {
                    ops = ChangePlanner.Plan(_index, new ChangesPage(Array.Empty<Item>(), new[] { key }, "", false)).ToList();
                    goto apply;
                }
                // Treat the current state as if the feed had just sent it; a stale local entry gets refreshed.
                var known = _index.Get(key);
                if (known is not null && known.ContentEtag == it.ContentEtag && known.Name == it.Name && known.ParentId == it.ParentId)
                    known.ContentEtag = "";      // force a content refresh of the placeholder
                ops = ChangePlanner.Plan(_index, new ChangesPage(new[] { it }, Array.Empty<string>(), "", false)).ToList();
            }
            apply:
            await _work.WaitAsync(ct).ConfigureAwait(false);
            try { Apply(ops); }
            finally { _work.Release(); }
        }
    }

    /// <summary>
    /// docs/design.md §E.1: a save lands here, goes to the NAS, and the local bytes are dropped after evict_after,
    /// unless the file is pinned ("Always keep on this device"), open, or has changes not yet uploaded.
    /// </summary>
    private void Evict()
    {
        var after = TimeSpan.FromMinutes(_settings.Get(s => s.EvictAfterMinutes));
        var due = _settings.Get(s => s.Uploaded.Where(kv => DateTimeOffset.UtcNow - kv.Value > after).Select(kv => kv.Key).ToList());
        foreach (var id in due)
        {
            var path = FullPath(id);
            var st = path is null ? PlaceholderState.Missing : SafeInspect(path);
            var done = !st.Exists || st.IsDirectory || st.Partial || st.Pin == CfPin.Pinned;
            if (!done && st.InSync)
            {
                try { Placeholders.Dehydrate(path!); done = true; }
                catch (Exception) { /* open in some program: next round */ }
            }
            if (done) _settings.Update(s => s.Uploaded.Remove(id));
        }
    }

    /// <summary>Sends the deletes made while offline, oldest first. One that fails for lack of a connection stays queued.</summary>
    public async Task ReplayDeletesAsync(CancellationToken ct)
    {
        if (_settings.Get(s => s.PendingDeletes.Count) == 0 || _settings.Get(s => s.Paused)) return;
        if (!await _replaying.WaitAsync(0, ct).ConfigureAwait(false)) return;
        try
        {
            foreach (var d in _settings.Get(s => s.PendingDeletes.OrderBy(x => x.At).ToList()))
            {
                if (ct.IsCancellationRequested) return;
                if (!_settings.Get(s => s.PendingDeletes.Any(x => x.Id == d.Id))) continue;     // kept meanwhile
                GatewayException? err = null;
                try { await _client.DeleteAsync(d.Id, d.IfMatch, ct).ConfigureAwait(false); }
                catch (GatewayException e) { err = e; }
                var outcome = OfflineDeletes.Classify(err);
                if (outcome == PendingDeleteOutcome.KeepQueued)
                {
                    if (err is not null) Track(err);
                    return;                                      // still offline: try the rest later too
                }
                _settings.Update(s => s.PendingDeletes.RemoveAll(x => x.Id == d.Id));
                switch (outcome)
                {
                    case PendingDeleteOutcome.Done:
                        _log($"deleted {d.Path} (queued while offline)");
                        break;
                    case PendingDeleteOutcome.ChangedOnNas:
                        _log($"{d.Path}: changed on the NAS since; not deleted there, it comes back here");
                        _retryIds[d.Id] = 0;
                        // Its folders were deleted here after it (Explorer deletes the contents first): keep them
                        // on the NAS too, or deleting the folder would take the changed file with it.
                        var kept = OfflineDeletes.Ancestors(_settings.Get(s => s.PendingDeletes.ToList()), d);
                        foreach (var a in kept)
                        {
                            _log($"{a.Path}: kept, it holds a file changed on the NAS");
                            _retryIds[a.Id] = 0;
                        }
                        var keptIds = kept.Select(a => a.Id).ToHashSet();
                        _settings.Update(s => s.PendingDeletes.RemoveAll(x => keptIds.Contains(x.Id)));
                        break;
                    default:
                        _log($"{d.Path}: the NAS refused the delete ({err?.Code ?? err?.Message}); it comes back here");
                        _retryIds[d.Id] = 0;
                        break;
                }
            }
        }
        finally
        {
            _replaying.Release();
            Changed();
        }
    }

    /// <summary>
    /// Keeps the files Loft downloaded on its own under the cache limit (default 5 GB): least recently used first,
    /// never a pinned file ("Always keep on this device"), one with changes not yet uploaded, or one in use.
    /// </summary>
    private void EnforceCacheLimit()
    {
        var limit = _settings.Get(s => s.CacheLimitBytes);
        if (limit <= 0) return;
        var now = DateTimeOffset.UtcNow;
        if (Interlocked.Exchange(ref _cacheCheckDue, 0) == 0 && now - _lastCacheCheck < TimeSpan.FromMinutes(10)) return;
        _lastCacheCheck = now;
        var files = new List<CachedFile>();
        var paths = new Dictionary<string, string>(StringComparer.Ordinal);
        var dirs = new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
        foreach (var e in _index.All())
        {
            if (e.IsDir || FullPath(e.Id) is not { } path) continue;
            var st = SafeInspect(path);
            if (!st.Exists || !st.IsPlaceholder || st.IsDirectory || st.Partial || !st.InSync || IsPinned(path, st, dirs)) continue;
            var fi = new FileInfo(path);
            var used = e.LastUsed ?? new DateTimeOffset(fi.LastAccessTimeUtc > fi.LastWriteTimeUtc ? fi.LastAccessTimeUtc : fi.LastWriteTimeUtc);
            files.Add(new CachedFile(e.Id, fi.Length, used));
            paths[e.Id] = path;
        }
        var freed = 0L;
        foreach (var f in CachePolicy.PickEvictions(files, limit, now))
        {
            try
            {
                Placeholders.Dehydrate(paths[f.Id]);
                freed += f.Size;
                _settings.Update(s => s.Uploaded.Remove(f.Id));
            }
            catch (Exception) { /* open in some program: next round */ }
        }
        var total = files.Sum(f => f.Size);
        if (freed > 0)
            _log($"cache over {limit >> 20} MB: freed {freed >> 20} MB ({total >> 20} MB were downloaded)");
        // Still over (files in use, or used too recently): look again at the next round, not in 10 minutes.
        if (total - freed > limit) Interlocked.Exchange(ref _cacheCheckDue, 1);
    }

    /// <summary>"Always keep on this device" on the file or on a folder above it.</summary>
    private bool IsPinned(string path, PlaceholderState st, Dictionary<string, bool> dirs)
    {
        if (st.Pin == CfPin.Pinned) return true;
        if (st.Pin != CfPin.Inherit && st.Pin != CfPin.Unspecified) return false;
        var dir = Path.GetDirectoryName(path);
        if (dir is null || dir.Length <= Folder.Length) return false;
        if (dirs.TryGetValue(dir, out var known)) return known;
        var d = SafeInspect(dir);
        return dirs[dir] = d.Pin == CfPin.Pinned || (d.Pin is CfPin.Inherit or CfPin.Unspecified && IsPinned(dir, d, dirs));
    }

    // ── index ───────────────────────────────────────────────────────────

    private void MarkIndexDirty() => Interlocked.Exchange(ref _indexDirty, 1);

    private void SaveIndexIfDirty()
    {
        if (Interlocked.Exchange(ref _indexDirty, 0) == 1) SaveIndex();
    }

    private void SaveIndex()
    {
        try { _index.Save(_indexPath); }
        catch (Exception e) { _log($"index save: {e.Message}"); }
    }

    /// <summary>The index file is gone but placeholders exist: read the ids back from the placeholders.</summary>
    private void RebuildIndex()
    {
        var idx = new LocalIndex(Root.RootItemId);
        idx.SetPopulated(Root.RootItemId);
        Walk(Folder, Root.RootItemId);
        _index = idx;
        MarkIndexDirty();
        _log($"index rebuilt from placeholders: {idx.Count} entries");

        void Walk(string dir, string parentId)
        {
            foreach (var p in Directory.EnumerateFileSystemEntries(dir))
            {
                PlaceholderState st;
                try { st = Placeholders.Inspect(p); }
                catch (Exception e) { _log($"rebuild: {p}: {e.GetType().Name}: {e.Message}"); continue; }
                if (!st.IsPlaceholder || st.Identity is not { } fid) continue;
                var fi = new FileInfo(p);
                idx.Upsert(new Item
                {
                    Id = fid.Id, ParentId = parentId, Name = Path.GetFileName(p), Type = st.IsDirectory ? "dir" : "file",
                    Size = st.IsDirectory ? 0 : fi.Length, ContentEtag = fid.ContentEtag, MetaEtag = fid.MetaEtag,
                    MtimeNs = (fi.LastWriteTimeUtc - DateTime.UnixEpoch).Ticks * 100,
                });
                if (st.IsDirectory && (st.Attributes & Placeholders.FILE_ATTRIBUTE_RECALL_ON_OPEN) == 0)
                {
                    idx.SetPopulated(fid.Id);
                    Walk(p, fid.Id);
                }
            }
        }
    }

    /// <summary>
    /// Per-item failures never stop sync; only "no answer", "NAS down" and "sign in again" reach the tray
    /// (the change loop reports the rest).
    /// </summary>
    private void Track(Exception e)
    {
        if (e is not GatewayException g) return;
        var s = LoopState.From(g);
        var relevant = s.Kind is LoopStateKind.CantReachHome or LoopStateKind.CantReachNas
                       || (s.Kind == LoopStateKind.Failed && s.Error!.NeedsSignIn);
        if (relevant) { _loop = s; Changed(); }
    }

    // ── for the app ─────────────────────────────────────────────────────

    /// <summary>Writes the index now (sign-out of Windows, shutdown).</summary>
    public void SaveState() => SaveIndex();

    /// <summary>"Try again": reconcile every opened folder now.</summary>
    public Task ResyncNowAsync() => ResyncAsync();
}
