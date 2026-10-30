// Thin, checked wrappers over the Cloud Files placeholder calls (cfapi.h) that the engine uses.
// Every placeholder carries a small FileIdentity blob: the gateway item id and its two versions.
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text.Json;
using System.Text.Json.Serialization;
using Loft.Core;
using Microsoft.Win32.SafeHandles;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.Storage.CloudFilters;
using Windows.Win32.Storage.FileSystem;

namespace Loft.Sync;

/// <summary>What a placeholder knows about its gateway item. Serialised as compact JSON (well under the 4 KB limit).</summary>
public sealed record FileIdentity(
    [property: JsonPropertyName("i")] string Id,
    [property: JsonPropertyName("c")] string ContentEtag,
    [property: JsonPropertyName("m")] string MetaEtag)
{
    public static FileIdentity Of(Item it) => new(it.Id, it.ContentEtag, it.MetaEtag);

    public byte[] Encode() => JsonSerializer.SerializeToUtf8Bytes(this);

    public static FileIdentity? Decode(ReadOnlySpan<byte> blob)
    {
        if (blob.IsEmpty) return null;
        try { return JsonSerializer.Deserialize<FileIdentity>(blob); }
        catch (JsonException) { return null; }
    }
}

/// <summary>A local file or folder as Cloud Files sees it.</summary>
public sealed record PlaceholderState(
    bool Exists, bool IsDirectory, bool IsPlaceholder, bool InSync, bool Partial, CfPin Pin, FileIdentity? Identity,
    uint Attributes)
{
    public static readonly PlaceholderState Missing = new(false, false, false, false, false, CfPin.Unspecified, null, 0);
}

public enum CfPin { Unspecified = 0, Pinned = 1, Unpinned = 2, Excluded = 3, Inherit = 4 }

[SupportedOSPlatform("windows10.0.17763")]
public static unsafe class Placeholders
{
    public const uint FILE_ATTRIBUTE_DIRECTORY = 0x10, FILE_ATTRIBUTE_NORMAL = 0x80,
        FILE_ATTRIBUTE_OFFLINE = 0x1000, FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000,
        FILE_ATTRIBUTE_PINNED = 0x80000, FILE_ATTRIBUTE_UNPINNED = 0x100000,
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000;

    private const uint FILE_READ_ATTRIBUTES = 0x80, WRITE_DAC = 0x40000, DELETE = 0x10000;
    private const int MaxIdentity = 4096;

    // Windows hides a placeholder's reparse tag from a process until it has connected a sync root, so before
    // CfConnectSyncRoot (rebuilding the index at start) every placeholder looked like a plain file; seen on
    // Windows 11 as "index rebuilt from placeholders: 1 entries". The reparse-point attribute stays visible, and
    // CfGetPlaceholderInfo works, so Inspect asks cfapi when the tag says "not a placeholder". (Exposing placeholders with
    // RtlSetProcessPlaceholderCompatibilityMode instead makes listing a folder before connecting fail with "The
    // cloud file provider exited unexpectedly".)

    /// <summary>
    /// Opens without triggering hydration: attributes only, plus WRITE_DAC when we need to change the placeholder
    /// (cfapi accepts WRITE_DAC in place of WRITE_DATA, and it never conflicts with other openers' share modes).
    /// </summary>
    public static SafeFileHandle Open(string path, bool write)
    {
        var access = FILE_READ_ATTRIBUTES | (write ? WRITE_DAC : 0);
        var h = PInvoke.CreateFile(path, access,
            FILE_SHARE_MODE.FILE_SHARE_READ | FILE_SHARE_MODE.FILE_SHARE_WRITE | FILE_SHARE_MODE.FILE_SHARE_DELETE,
            null, FILE_CREATION_DISPOSITION.OPEN_EXISTING,
            FILE_FLAGS_AND_ATTRIBUTES.FILE_FLAG_BACKUP_SEMANTICS, null);
        if (h.IsInvalid) throw new IOException($"open {path}: win32 error {Marshal.GetLastWin32Error()}");
        return h;
    }

    public static PlaceholderState Inspect(string path)
    {
        if (!Path.Exists(path)) return PlaceholderState.Missing;
        var attrs = (uint)File.GetAttributes(path);
        var isDir = (attrs & FILE_ATTRIBUTE_DIRECTORY) != 0;
        using var h = Open(path, write: false);

        FILE_ATTRIBUTE_TAG_INFO tag;
        if (!PInvoke.GetFileInformationByHandleEx(new HANDLE(h.DangerousGetHandle()), FILE_INFO_BY_HANDLE_CLASS.FileAttributeTagInfo,
                                                  &tag, (uint)sizeof(FILE_ATTRIBUTE_TAG_INFO)))
            throw new IOException($"attributes {path}: win32 error {Marshal.GetLastWin32Error()}");
        var state = PInvoke.CfGetPlaceholderStateFromAttributeTag(tag.FileAttributes, tag.ReparseTag);
        var isPlaceholder = (state & CF_PLACEHOLDER_STATE.CF_PLACEHOLDER_STATE_PLACEHOLDER) != 0;
        // The tag may be hidden from us (see above): then cfapi itself says whether it's a placeholder.

        var buf = stackalloc byte[sizeof(CF_PLACEHOLDER_BASIC_INFO) + MaxIdentity];
        uint returned;
        var hr = PInvoke.CfGetPlaceholderInfo(new HANDLE(h.DangerousGetHandle()), CF_PLACEHOLDER_INFO_CLASS.CF_PLACEHOLDER_INFO_BASIC,
                                              buf, (uint)(sizeof(CF_PLACEHOLDER_BASIC_INFO) + MaxIdentity), &returned);
        if (!isPlaceholder && hr.Failed)                         // some other kind of reparse point
            return new PlaceholderState(true, isDir, false, false, false, CfPin.Unspecified, null, attrs);
        hr.ThrowOnFailure();
        if (!isPlaceholder)
            state = CF_PLACEHOLDER_STATE.CF_PLACEHOLDER_STATE_PLACEHOLDER
                    | ((attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_OFFLINE)) != 0
                        ? CF_PLACEHOLDER_STATE.CF_PLACEHOLDER_STATE_PARTIAL : 0);
        var info = (CF_PLACEHOLDER_BASIC_INFO*)buf;
        var idLen = (int)Math.Min(info->FileIdentityLength, MaxIdentity);
        var idBytes = new ReadOnlySpan<byte>((byte*)&info->FileIdentity, idLen);
        var partial = (state & (CF_PLACEHOLDER_STATE.CF_PLACEHOLDER_STATE_PARTIAL | CF_PLACEHOLDER_STATE.CF_PLACEHOLDER_STATE_PARTIALLY_ON_DISK)) != 0;
        return new PlaceholderState(true, isDir, true, info->InSyncState == CF_IN_SYNC_STATE.CF_IN_SYNC_STATE_IN_SYNC,
                                    partial, (CfPin)(int)info->PinState, FileIdentity.Decode(idBytes), attrs);
    }

    internal static CF_FS_METADATA Metadata(Item it)
    {
        var ft = FileTime(it.MtimeNs);
        return new CF_FS_METADATA
        {
            FileSize = it.IsDirectory ? 0 : it.Size,
            BasicInfo = new FILE_BASIC_INFO
            {
                CreationTime = ft, LastAccessTime = ft, LastWriteTime = ft, ChangeTime = ft,
                FileAttributes = it.IsDirectory ? FILE_ATTRIBUTE_DIRECTORY : FILE_ATTRIBUTE_NORMAL,
            },
        };
    }

    /// <summary>100 ns ticks since 1601 (FILETIME) from the gateway's nanoseconds since 1970.</summary>
    public static long FileTime(long unixNs) => unixNs <= 0 ? 0 : unixNs / 100 + 116_444_736_000_000_000L;

    public enum CreateResult { Created, Exists, Failed }

    /// <summary>Creates one placeholder in <paramref name="dir"/>, in sync. Folders are populated on demand.</summary>
    public static CreateResult Create(string dir, Item it)
    {
        var id = FileIdentity.Of(it).Encode();
        var name = Marshal.StringToHGlobalUni(it.Name);
        try
        {
            fixed (byte* idp = id)
            {
                var info = new CF_PLACEHOLDER_CREATE_INFO
                {
                    RelativeFileName = new PCWSTR((char*)name),
                    FsMetadata = Metadata(it),
                    FileIdentity = idp,
                    FileIdentityLength = (uint)id.Length,
                    Flags = CF_PLACEHOLDER_CREATE_FLAGS.CF_PLACEHOLDER_CREATE_FLAG_MARK_IN_SYNC,
                };
                uint done;
                fixed (char* d = dir)
                {
                    var hr = PInvoke.CfCreatePlaceholders(new PCWSTR(d), &info, 1, CF_CREATE_FLAGS.CF_CREATE_FLAG_NONE, &done);
                    if (info.Result.Succeeded && hr.Succeeded) return CreateResult.Created;
                    if (IsExists(info.Result) || IsExists(hr)) return CreateResult.Exists;
                    return CreateResult.Failed;
                }
            }
        }
        finally { Marshal.FreeHGlobal(name); }
    }

    internal static bool IsExists(HRESULT hr) =>
        hr == HRESULT_FROM_WIN32(WIN32_ERROR.ERROR_ALREADY_EXISTS) || hr == HRESULT_FROM_WIN32(WIN32_ERROR.ERROR_FILE_EXISTS);

    internal static HRESULT HRESULT_FROM_WIN32(WIN32_ERROR e) => new(unchecked((int)(0x80070000u | (uint)e)));

    /// <summary>A local file or folder Loft just uploaded becomes a placeholder for that item.</summary>
    public static void Convert(string path, Item it, bool populated)
    {
        using var h = Open(path, write: true);
        var id = FileIdentity.Of(it).Encode();
        var flags = CF_CONVERT_FLAGS.CF_CONVERT_FLAG_MARK_IN_SYNC;
        if (it.IsDirectory && !populated) flags |= CF_CONVERT_FLAGS.CF_CONVERT_FLAG_ENABLE_ON_DEMAND_POPULATION;
        fixed (byte* idp = id)
        {
            PInvoke.CfConvertToPlaceholder(new HANDLE(h.DangerousGetHandle()), idp, (uint)id.Length, flags, null, null)
                .ThrowOnFailure();
        }
    }

    /// <summary>
    /// New identity (and metadata) for a placeholder. <paramref name="dehydrate"/> drops the local bytes, so the next
    /// open fetches the new version; it fails while another program has the file open.
    /// </summary>
    public static void Update(string path, Item it, bool markInSync, bool dehydrate, bool withMetadata)
    {
        using var h = Open(path, write: true);
        var id = FileIdentity.Of(it).Encode();
        var flags = CF_UPDATE_FLAGS.CF_UPDATE_FLAG_NONE;
        if (markInSync) flags |= CF_UPDATE_FLAGS.CF_UPDATE_FLAG_MARK_IN_SYNC;
        if (dehydrate && !it.IsDirectory) flags |= CF_UPDATE_FLAGS.CF_UPDATE_FLAG_DEHYDRATE;
        var md = Metadata(it);
        fixed (byte* idp = id)
        {
            PInvoke.CfUpdatePlaceholder(new HANDLE(h.DangerousGetHandle()), withMetadata ? &md : null, idp, (uint)id.Length,
                                        null, 0, flags, null, null).ThrowOnFailure();
        }
    }

    public static void SetInSync(string path)
    {
        using var h = Open(path, write: true);
        PInvoke.CfSetInSyncState(new HANDLE(h.DangerousGetHandle()), CF_IN_SYNC_STATE.CF_IN_SYNC_STATE_IN_SYNC,
                                 CF_SET_IN_SYNC_FLAGS.CF_SET_IN_SYNC_FLAG_NONE, null).ThrowOnFailure();
    }

    /// <summary>"Free up space": drop the local bytes, keep the placeholder.</summary>
    public static void Dehydrate(string path)
    {
        using var h = Open(path, write: true);
        PInvoke.CfDehydratePlaceholder(new HANDLE(h.DangerousGetHandle()), 0, -1, CF_DEHYDRATE_FLAGS.CF_DEHYDRATE_FLAG_NONE, null)
            .ThrowOnFailure();
    }

    /// <summary>"Always keep on this device": fetch the whole file now. Never call this from a callback thread.</summary>
    public static void Hydrate(string path)
    {
        using var h = Open(path, write: false);
        PInvoke.CfHydratePlaceholder(new HANDLE(h.DangerousGetHandle()), 0, -1, CF_HYDRATE_FLAGS.CF_HYDRATE_FLAG_NONE, null)
            .ThrowOnFailure();
    }

    /// <summary>Back to a plain file (keeps the bytes if they are on disk).</summary>
    public static void Revert(string path)
    {
        using var h = Open(path, write: true);
        PInvoke.CfRevertPlaceholder(new HANDLE(h.DangerousGetHandle()), CF_REVERT_FLAGS.CF_REVERT_FLAG_NONE, null).ThrowOnFailure();
    }

    public static void SetPin(string path, CfPin pin)
    {
        using var h = Open(path, write: true);
        PInvoke.CfSetPinState(new HANDLE(h.DangerousGetHandle()), (CF_PIN_STATE)(int)pin, CF_SET_PIN_FLAGS.CF_SET_PIN_FLAG_NONE, null)
            .ThrowOnFailure();
    }
}
