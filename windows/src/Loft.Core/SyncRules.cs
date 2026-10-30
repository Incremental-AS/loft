// The sync decisions, kept platform-neutral so they are unit-tested on Linux. Loft.Sync executes
// them against Cloud Files placeholders.
namespace Loft.Core;

public enum RemoteOpKind
{
    /// <summary>The item moved or was renamed on the NAS: move the local placeholder.</summary>
    Move,
    /// <summary>Gone on the NAS (or moved somewhere this PC doesn't show).</summary>
    DeleteLocal,
    /// <summary>New on the NAS, in a folder whose children are placed: create a placeholder.</summary>
    CreatePlaceholder,
    /// <summary>New bytes on the NAS: update size/time and drop the stale local bytes (if in sync).</summary>
    UpdateContent,
    /// <summary>Only the metadata version moved: refresh the placeholder's identity.</summary>
    UpdateMetadata,
}

public sealed record RemoteOp(RemoteOpKind Kind, string Id, Item? Item);

public static class ChangePlanner
{
    /// <summary>
    /// Turns one change-feed page into local operations, ordered moves → deletes → creates → updates, so
    /// a name freed by a move or delete is free before a create needs it. Items in folders this PC has
    /// never opened are skipped: they are listed when the folder is opened.
    /// </summary>
    public static IReadOnlyList<RemoteOp> Plan(LocalIndex index, ChangesPage page)
    {
        var moves = new List<RemoteOp>();
        var deletes = new List<RemoteOp>();
        var creates = new List<RemoteOp>();
        var updates = new List<RemoteOp>();
        var deleted = page.Deleted.ToHashSet(StringComparer.Ordinal);

        foreach (var it in page.Updated)
        {
            if (deleted.Contains(it.Id)) continue;
            if (it.Id == index.RootId)
            {
                updates.Add(new RemoteOp(RemoteOpKind.UpdateMetadata, it.Id, it));
                continue;
            }
            var known = index.Get(it.Id);
            if (known is null)
            {
                var parent = it.ParentId is null ? null : index.Get(it.ParentId);
                if (parent is { IsDir: true, Populated: true })
                    creates.Add(new RemoteOp(RemoteOpKind.CreatePlaceholder, it.Id, it));
                continue;
            }
            var moved = known.ParentId != it.ParentId || known.Name != it.Name;
            if (moved)
            {
                var newParent = it.ParentId is null ? null : index.Get(it.ParentId);
                if (newParent is { IsDir: true, Populated: true } && !index.IsAncestor(it.Id, newParent.Id))
                    moves.Add(new RemoteOp(RemoteOpKind.Move, it.Id, it));
                else
                {
                    deletes.Add(new RemoteOp(RemoteOpKind.DeleteLocal, it.Id, it));
                    continue;
                }
            }
            if (!it.IsDirectory && known.ContentEtag != it.ContentEtag)
                updates.Add(new RemoteOp(RemoteOpKind.UpdateContent, it.Id, it));
            else if (!moved && (known.MetaEtag != it.MetaEtag || known.Size != it.Size || known.MtimeNs != it.MtimeNs))
                updates.Add(new RemoteOp(RemoteOpKind.UpdateMetadata, it.Id, it));
        }
        foreach (var id in page.Deleted)
            if (index.Get(id) is not null && id != index.RootId)
                deletes.Add(new RemoteOp(RemoteOpKind.DeleteLocal, id, null));

        // Parents before children among the creates (a folder made in a known folder, then its
        // file: the file is skipped, as the new folder is placed unpopulated).
        return moves.Concat(deletes).Concat(creates).Concat(updates).ToList();
    }

    /// <summary>
    /// A full listing of one folder against what is placed: the same operations as a feed page would give.
    /// Used after an expired anchor and for "Try again".
    /// </summary>
    public static IReadOnlyList<RemoteOp> PlanFolder(LocalIndex index, string folderId, IReadOnlyList<Item> listing)
    {
        var seen = listing.Select(i => i.Id).ToHashSet(StringComparer.Ordinal);
        var gone = index.Children(folderId).Where(c => !seen.Contains(c.Id)).Select(c => c.Id).ToList();
        var page = new ChangesPage(listing, gone, "", false);
        return Plan(index, page);
    }
}

public enum RenameAction
{
    /// <summary>PATCH name/parent on the gateway.</summary>
    Patch,
    /// <summary>Renamed to a temp name (Office safe-save): keep it on the NAS as it is, remember it.</summary>
    Park,
    /// <summary>Moved out of the sync folder: delete on the gateway (to its trash).</summary>
    DeleteRemote,
}

/// <summary>A placeholder that Office renamed to a temp name during a safe-save.</summary>
public sealed record ParkedItem(string Id, string ParentId, string OriginalName, string ContentEtag, DateTimeOffset At);

public static class LocalRules
{
    public static RenameAction OnRename(string newName, bool newInsideRoot)
    {
        if (!newInsideRoot) return RenameAction.DeleteRemote;
        if (Exclusions.IsExcluded(newName)) return RenameAction.Park;
        return RenameAction.Patch;
    }

    /// <summary>
    /// The commit for a local file that is not (or no longer) a placeholder. If Office parked the original under
    /// a temp name and this file takes its place, it is the original's successor (create-before-delete safe-save,
    /// docs/design.md §D.3), so the gateway keeps one item with its history instead of delete + create. If the item
    /// with this name is still live here (an app overwrote the placeholder in place, which turns it into a plain
    /// file), it is an edit of that item, with CAS on the version this PC had.
    /// </summary>
    public static CommitRequest CommitForNewFile(string parentId, string name, IEnumerable<ParkedItem> parked,
                                                 LocalIndex index)
    {
        var key = LocalIndex.NameKey(name);
        var p = parked.Where(x => x.ParentId == parentId && LocalIndex.NameKey(x.OriginalName) == key)
                      .OrderByDescending(x => x.At).FirstOrDefault();
        if (p is not null)
            return new CommitRequest { ParentId = parentId, Name = name, Replaces = p.Id, IfMatch = p.ContentEtag };
        if (index.ChildByName(parentId, name) is { IsDir: false } live)
            return CommitForEdit(live.Id, live.ContentEtag);
        return new CommitRequest { ParentId = parentId, Name = name };
    }

    /// <summary>The commit for an edited placeholder: CAS on the content version this PC had.</summary>
    public static CommitRequest CommitForEdit(string itemId, string baseContentEtag) =>
        new() { ItemId = itemId, IfMatch = baseContentEtag };

    /// <summary>
    /// What the local file becomes after a commit. A conflict on an edit means the NAS copy moved on: our
    /// bytes are now the gateway's "Name (conflict, &lt;device&gt;, &lt;time&gt;)" copy, and the original must be
    /// fetched again. A conflict on a create means the name was taken: the local file takes the conflict
    /// copy's name.
    /// </summary>
    public static CommitOutcome AfterCommit(CommitRequest req, CommitResponse r) => r.Result switch
    {
        CommitResult.Conflict when req.ItemId is not null => CommitOutcome.RefetchOriginal,
        CommitResult.Conflict => CommitOutcome.RenameToConflictCopy,
        _ => CommitOutcome.InSync,
    };
}

public enum CommitOutcome
{
    /// <summary>The local bytes are what the gateway has: mark in sync with the new identity.</summary>
    InSync,
    /// <summary>Edit lost the race: the conflict copy arrives through the feed; drop the local bytes.</summary>
    RefetchOriginal,
    /// <summary>New file whose name was taken: rename locally to the conflict copy's name.</summary>
    RenameToConflictCopy,
}
