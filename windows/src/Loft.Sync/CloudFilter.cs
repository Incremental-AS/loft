// The connection to the Cloud Files filter: callback trampolines and the CfExecute operations.
// Callbacks copy what they need out of the native structs (valid only during the call), hand the
// work to the thread pool, and the engine completes the operation later with the transfer key.
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.Storage.CloudFilters;

namespace Loft.Sync;

/// <summary>One callback, copied out of the native call.</summary>
public sealed record CfRequest(
    long ConnectionKey, long TransferKey, long RequestKey, string Path, byte[] Identity, long FileSize)
{
    public FileIdentity? Id => FileIdentity.Decode(Identity);
}

internal interface ICloudCallbacks
{
    Task FetchDataAsync(CfRequest r, long requiredOffset, long requiredLength, long optionalOffset, long optionalLength);
    void CancelFetchData(CfRequest r);
    Task FetchPlaceholdersAsync(CfRequest r);
    Task NotifyDeleteAsync(CfRequest r, bool isDirectory);
    Task NotifyRenameAsync(CfRequest r, string targetPath, bool isDirectory, bool targetInScope);
    Task NotifyDehydrateAsync(CfRequest r);
}

[SupportedOSPlatform("windows10.0.17763")]
internal sealed unsafe class CloudFilter : IDisposable
{
    public const int Alignment = 4096;

    private readonly GCHandle _self;
    private readonly CF_CALLBACK_REGISTRATION* _table;
    private readonly ICloudCallbacks _cb;
    private readonly Action<string> _log;
    public long ConnectionKey { get; }

    // NTSTATUS values the engine reports back.
    public static readonly NTSTATUS Success = new(0);
    public static NTSTATUS Unsuccessful => NTSTATUS.STATUS_CLOUD_FILE_UNSUCCESSFUL;
    public static NTSTATUS NetworkUnavailable => NTSTATUS.STATUS_CLOUD_FILE_NETWORK_UNAVAILABLE;
    public static NTSTATUS NotInSync => NTSTATUS.STATUS_CLOUD_FILE_NOT_IN_SYNC;
    public static NTSTATUS InUse => NTSTATUS.STATUS_CLOUD_FILE_IN_USE;
    public static NTSTATUS AccessDenied => NTSTATUS.STATUS_CLOUD_FILE_ACCESS_DENIED;
    public static NTSTATUS AuthFailed => NTSTATUS.STATUS_CLOUD_FILE_AUTHENTICATION_FAILED;

    private CloudFilter(string syncRoot, ICloudCallbacks cb, Action<string> log)
    {
        _cb = cb;
        _log = log;
        _self = GCHandle.Alloc(this);
        // Kept for the connection's lifetime (the filter may refer to it).
        var table = _table = (CF_CALLBACK_REGISTRATION*)NativeMemory.AllocZeroed(8, (nuint)sizeof(CF_CALLBACK_REGISTRATION));
        table[0] = new() { Type = CF_CALLBACK_TYPE.CF_CALLBACK_TYPE_FETCH_DATA, Callback = &OnFetchData };
        table[1] = new() { Type = CF_CALLBACK_TYPE.CF_CALLBACK_TYPE_CANCEL_FETCH_DATA, Callback = &OnCancelFetchData };
        table[2] = new() { Type = CF_CALLBACK_TYPE.CF_CALLBACK_TYPE_FETCH_PLACEHOLDERS, Callback = &OnFetchPlaceholders };
        table[3] = new() { Type = CF_CALLBACK_TYPE.CF_CALLBACK_TYPE_NOTIFY_DELETE, Callback = &OnNotifyDelete };
        table[4] = new() { Type = CF_CALLBACK_TYPE.CF_CALLBACK_TYPE_NOTIFY_RENAME, Callback = &OnNotifyRename };
        table[5] = new() { Type = CF_CALLBACK_TYPE.CF_CALLBACK_TYPE_NOTIFY_DEHYDRATE, Callback = &OnNotifyDehydrate };
        table[6] = new() { Type = CF_CALLBACK_TYPE.CF_CALLBACK_TYPE_NONE, Callback = null };
        CF_CONNECTION_KEY key;
        fixed (char* p = syncRoot)
        {
            var hr = PInvoke.CfConnectSyncRoot(new PCWSTR(p), table, (void*)GCHandle.ToIntPtr(_self),
                CF_CONNECT_FLAGS.CF_CONNECT_FLAG_REQUIRE_PROCESS_INFO | CF_CONNECT_FLAGS.CF_CONNECT_FLAG_REQUIRE_FULL_FILE_PATH,
                &key);
            if (hr.Failed)
            {
                _self.Free();
                NativeMemory.Free(_table);
                hr.ThrowOnFailure();
            }
        }
        ConnectionKey = key.Value;
    }

    public static CloudFilter Connect(string syncRoot, ICloudCallbacks cb, Action<string> log) => new(syncRoot, cb, log);

    public void Dispose()
    {
        PInvoke.CfDisconnectSyncRoot(new CF_CONNECTION_KEY(ConnectionKey));
        if (_self.IsAllocated) _self.Free();
        NativeMemory.Free(_table);
    }

    // ── callback trampolines ───────────────────────────────────────────

    private static CloudFilter? Self(CF_CALLBACK_INFO* info) =>
        info->CallbackContext == null ? null : GCHandle.FromIntPtr((IntPtr)info->CallbackContext).Target as CloudFilter;

    private static CfRequest Copy(CF_CALLBACK_INFO* info)
    {
        var path = new string(info->VolumeDosName.Value) + new string(info->NormalizedPath.Value);
        var id = info->FileIdentity == null || info->FileIdentityLength == 0
            ? Array.Empty<byte>()
            : new ReadOnlySpan<byte>(info->FileIdentity, (int)info->FileIdentityLength).ToArray();
        return new CfRequest(info->ConnectionKey.Value, info->TransferKey, info->RequestKey, path, id, info->FileSize);
    }

    private void Run(string what, Func<Task> work) => Background.Run(_log, what, work);

    [UnmanagedCallersOnly(CallConvs = new[] { typeof(CallConvStdcall) })]
    private static void OnFetchData(CF_CALLBACK_INFO* info, CF_CALLBACK_PARAMETERS* p)
    {
        try
        {
            var self = Self(info);
            if (self is null) return;
            var r = Copy(info);
            ref var f = ref p->FetchData;
            long ro = f.RequiredFileOffset, rl = f.RequiredLength, oo = f.OptionalFileOffset, ol = f.OptionalLength;
            self.Run("fetch data", () => self._cb.FetchDataAsync(r, ro, rl, oo, ol));
        }
        catch { /* never let an exception cross into native code */ }
    }

    [UnmanagedCallersOnly(CallConvs = new[] { typeof(CallConvStdcall) })]
    private static void OnCancelFetchData(CF_CALLBACK_INFO* info, CF_CALLBACK_PARAMETERS* p)
    {
        try
        {
            var self = Self(info);
            self?._cb.CancelFetchData(Copy(info));
        }
        catch { }
    }

    [UnmanagedCallersOnly(CallConvs = new[] { typeof(CallConvStdcall) })]
    private static void OnFetchPlaceholders(CF_CALLBACK_INFO* info, CF_CALLBACK_PARAMETERS* p)
    {
        try
        {
            var self = Self(info);
            if (self is null) return;
            var r = Copy(info);
            self.Run("fetch placeholders", () => self._cb.FetchPlaceholdersAsync(r));
        }
        catch { }
    }

    [UnmanagedCallersOnly(CallConvs = new[] { typeof(CallConvStdcall) })]
    private static void OnNotifyDelete(CF_CALLBACK_INFO* info, CF_CALLBACK_PARAMETERS* p)
    {
        try
        {
            var self = Self(info);
            if (self is null) return;
            var r = Copy(info);
            var isDir = (p->Delete.Flags & CF_CALLBACK_DELETE_FLAGS.CF_CALLBACK_DELETE_FLAG_IS_DIRECTORY) != 0;
            self.Run("notify delete", () => self._cb.NotifyDeleteAsync(r, isDir));
        }
        catch { }
    }

    [UnmanagedCallersOnly(CallConvs = new[] { typeof(CallConvStdcall) })]
    private static void OnNotifyRename(CF_CALLBACK_INFO* info, CF_CALLBACK_PARAMETERS* p)
    {
        try
        {
            var self = Self(info);
            if (self is null) return;
            var r = Copy(info);
            ref var rn = ref p->Rename;
            var target = new string(info->VolumeDosName.Value) + new string(rn.TargetPath.Value);
            var isDir = (rn.Flags & CF_CALLBACK_RENAME_FLAGS.CF_CALLBACK_RENAME_FLAG_IS_DIRECTORY) != 0;
            var inScope = (rn.Flags & CF_CALLBACK_RENAME_FLAGS.CF_CALLBACK_RENAME_FLAG_TARGET_IN_SCOPE) != 0;
            self.Run("notify rename", () => self._cb.NotifyRenameAsync(r, target, isDir, inScope));
        }
        catch { }
    }

    [UnmanagedCallersOnly(CallConvs = new[] { typeof(CallConvStdcall) })]
    private static void OnNotifyDehydrate(CF_CALLBACK_INFO* info, CF_CALLBACK_PARAMETERS* p)
    {
        try
        {
            var self = Self(info);
            if (self is null) return;
            var r = Copy(info);
            self.Run("notify dehydrate", () => self._cb.NotifyDehydrateAsync(r));
        }
        catch { }
    }

    // ── operations ──────────────────────────────────────────────────────

    private static CF_OPERATION_INFO OpInfo(CfRequest r, CF_OPERATION_TYPE type) => new()
    {
        StructSize = (uint)sizeof(CF_OPERATION_INFO),
        Type = type,
        ConnectionKey = new CF_CONNECTION_KEY(r.ConnectionKey),
        TransferKey = r.TransferKey,
        RequestKey = r.RequestKey,
    };

    private static uint ParamSize<T>(CF_OPERATION_PARAMETERS* p, T* member) where T : unmanaged =>
        (uint)((byte*)member - (byte*)p + sizeof(T));

    /// <summary>
    /// Hands bytes to the filter for a FETCH_DATA. Offset must be 4 KiB aligned and length a multiple of 4 KiB,
    /// except for the last block of the file. A failure status (with no buffer) fails that range.
    /// </summary>
    public static HRESULT TransferData(CfRequest r, ReadOnlySpan<byte> data, long offset, NTSTATUS status, long failLength = 0)
    {
        var info = OpInfo(r, CF_OPERATION_TYPE.CF_OPERATION_TYPE_TRANSFER_DATA);
        var p = new CF_OPERATION_PARAMETERS();
        fixed (byte* d = data)
        {
            p.TransferData.Flags = CF_OPERATION_TRANSFER_DATA_FLAGS.CF_OPERATION_TRANSFER_DATA_FLAG_NONE;
            p.TransferData.CompletionStatus = status;
            p.TransferData.Buffer = status == Success ? d : null;
            p.TransferData.Offset = offset;
            p.TransferData.Length = status == Success ? data.Length : failLength;
            p.ParamSize = ParamSize(&p, (CF_OPERATION_PARAMETERS._Anonymous_e__Union._TransferData_e__Struct*)&p.Anonymous);
            return PInvoke.CfExecute(&info, &p);
        }
    }

    /// <summary>Answers a FETCH_PLACEHOLDERS with the folder's children (or a failure). Marks the folder populated.</summary>
    public static HRESULT TransferPlaceholders(CfRequest r, IReadOnlyList<Loft.Core.Item> items, NTSTATUS status)
    {
        var info = OpInfo(r, CF_OPERATION_TYPE.CF_OPERATION_TYPE_TRANSFER_PLACEHOLDERS);
        var count = status == Success ? items.Count : 0;
        var arr = (CF_PLACEHOLDER_CREATE_INFO*)NativeMemory.AllocZeroed((nuint)Math.Max(count, 1), (nuint)sizeof(CF_PLACEHOLDER_CREATE_INFO));
        var allocs = new List<IntPtr>();
        try
        {
            for (var i = 0; i < count; i++)
            {
                var it = items[i];
                var name = Marshal.StringToHGlobalUni(it.Name);
                allocs.Add(name);
                var id = FileIdentity.Of(it).Encode();
                var idp = Marshal.AllocHGlobal(id.Length);
                allocs.Add(idp);
                Marshal.Copy(id, 0, idp, id.Length);
                arr[i] = new CF_PLACEHOLDER_CREATE_INFO
                {
                    RelativeFileName = new PCWSTR((char*)name),
                    FsMetadata = Placeholders.Metadata(it),
                    FileIdentity = (void*)idp,
                    FileIdentityLength = (uint)id.Length,
                    Flags = CF_PLACEHOLDER_CREATE_FLAGS.CF_PLACEHOLDER_CREATE_FLAG_MARK_IN_SYNC,
                };
            }
            var p = new CF_OPERATION_PARAMETERS();
            p.TransferPlaceholders.Flags = CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAGS.CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_DISABLE_ON_DEMAND_POPULATION;
            p.TransferPlaceholders.CompletionStatus = status;
            p.TransferPlaceholders.PlaceholderTotalCount = count;
            p.TransferPlaceholders.PlaceholderArray = count > 0 ? arr : null;
            p.TransferPlaceholders.PlaceholderCount = (uint)count;
            p.ParamSize = ParamSize(&p, (CF_OPERATION_PARAMETERS._Anonymous_e__Union._TransferPlaceholders_e__Struct*)&p.Anonymous);
            return PInvoke.CfExecute(&info, &p);
        }
        finally
        {
            foreach (var a in allocs) Marshal.FreeHGlobal(a);
            NativeMemory.Free(arr);
        }
    }

    public static HRESULT AckDelete(CfRequest r, NTSTATUS status)
    {
        var info = OpInfo(r, CF_OPERATION_TYPE.CF_OPERATION_TYPE_ACK_DELETE);
        var p = new CF_OPERATION_PARAMETERS();
        p.AckDelete.Flags = CF_OPERATION_ACK_DELETE_FLAGS.CF_OPERATION_ACK_DELETE_FLAG_NONE;
        p.AckDelete.CompletionStatus = status;
        p.ParamSize = ParamSize(&p, (CF_OPERATION_PARAMETERS._Anonymous_e__Union._AckDelete_e__Struct*)&p.Anonymous);
        return PInvoke.CfExecute(&info, &p);
    }

    public static HRESULT AckRename(CfRequest r, NTSTATUS status)
    {
        var info = OpInfo(r, CF_OPERATION_TYPE.CF_OPERATION_TYPE_ACK_RENAME);
        var p = new CF_OPERATION_PARAMETERS();
        p.AckRename.Flags = CF_OPERATION_ACK_RENAME_FLAGS.CF_OPERATION_ACK_RENAME_FLAG_NONE;
        p.AckRename.CompletionStatus = status;
        p.ParamSize = ParamSize(&p, (CF_OPERATION_PARAMETERS._Anonymous_e__Union._AckRename_e__Struct*)&p.Anonymous);
        return PInvoke.CfExecute(&info, &p);
    }

    public static HRESULT AckDehydrate(CfRequest r, NTSTATUS status)
    {
        var info = OpInfo(r, CF_OPERATION_TYPE.CF_OPERATION_TYPE_ACK_DEHYDRATE);
        var p = new CF_OPERATION_PARAMETERS();
        p.AckDehydrate.Flags = CF_OPERATION_ACK_DEHYDRATE_FLAGS.CF_OPERATION_ACK_DEHYDRATE_FLAG_NONE;
        p.AckDehydrate.CompletionStatus = status;
        fixed (byte* id = r.Identity)
        {
            p.AckDehydrate.FileIdentity = id;
            p.AckDehydrate.FileIdentityLength = (uint)r.Identity.Length;
            p.ParamSize = ParamSize(&p, (CF_OPERATION_PARAMETERS._Anonymous_e__Union._AckDehydrate_e__Struct*)&p.Anonymous);
            return PInvoke.CfExecute(&info, &p);
        }
    }
}

internal static class Background
{
    /// <summary>Runs callback work off the filter's thread; logs instead of crashing.</summary>
    public static void Run(Action<string> log, string what, Func<Task> work)
    {
        _ = Task.Run(async () =>
        {
            try { await work().ConfigureAwait(false); }
            catch (Exception e) { log($"{what}: unhandled {e.GetType().Name}: {e.Message}"); }
        });
    }
}
