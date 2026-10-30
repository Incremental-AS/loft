// Registering the Loft folder as a Cloud Files sync root. StorageProviderSyncRootManager gives the
// Explorer navigation-pane entry ("Loft", with the app icon) and works for an unpackaged app; if it
// is unavailable (some Server SKUs), CfRegisterSyncRoot registers the root without the pane entry.
using System.Runtime.Versioning;
using System.Security.Principal;
using Windows.Security.Cryptography;
using Windows.Storage;
using Windows.Storage.Provider;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.Storage.CloudFilters;

namespace Loft.Sync;

[SupportedOSPlatform("windows10.0.17763")]
public static class SyncRoot
{
    public const string ProviderName = "Loft";
    /// <summary>Fixed provider id for Loft (random, made once; not a secret).</summary>
    public static readonly Guid ProviderId = new("5f0c3a8e-6a3b-4f7e-9a51-2d64c9b7e0a1");
    public const string Version = "0.2.0";

    /// <summary>"Loft!&lt;user SID&gt;!&lt;account&gt;", the form Explorer expects for a navigation-pane root.</summary>
    public static string IdFor(string account)
    {
        var sid = WindowsIdentity.GetCurrent().User?.Value ?? "S-0";
        var safe = new string(account.Select(c => char.IsLetterOrDigit(c) || c is '-' or '_' or '.' ? c : '_').ToArray());
        return $"{ProviderName}!{sid}!{safe}";
    }

    public static string DefaultFolder =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Loft");

    public enum How { ShellRegistered, CfRegisteredOnly }

    /// <summary>Registers (or re-registers) <paramref name="folder"/>. <paramref name="iconPath"/> is an .ico or exe.</summary>
    public static async Task<How> RegisterAsync(string folder, string id, string displayName, string iconPath, Action<string> log)
    {
        Directory.CreateDirectory(folder);
        try
        {
            var info = new StorageProviderSyncRootInfo
            {
                Id = id,
                Path = await StorageFolder.GetFolderFromPathAsync(folder),
                DisplayNameResource = displayName,
                IconResource = iconPath + ",0",
                HydrationPolicy = StorageProviderHydrationPolicy.Progressive,
                HydrationPolicyModifier = StorageProviderHydrationPolicyModifier.AutoDehydrationAllowed,
                PopulationPolicy = StorageProviderPopulationPolicy.Full,
                InSyncPolicy = StorageProviderInSyncPolicy.FileLastWriteTime | StorageProviderInSyncPolicy.DirectoryLastWriteTime,
                HardlinkPolicy = StorageProviderHardlinkPolicy.None,
                ShowSiblingsAsGroup = false,
                Version = Version,
                AllowPinning = true,
                ProviderId = ProviderId,
                Context = CryptographicBuffer.ConvertStringToBinary(id, BinaryStringEncoding.Utf8),
            };
            StorageProviderSyncRootManager.Register(info);
            return How.ShellRegistered;
        }
        catch (Exception e)
        {
            log($"StorageProviderSyncRootManager.Register failed ({e.GetType().Name}: {e.Message}); using CfRegisterSyncRoot");
            RegisterWithCfApi(folder, id);
            return How.CfRegisteredOnly;
        }
    }

    private static unsafe void RegisterWithCfApi(string folder, string id)
    {
        fixed (char* name = ProviderName)
        fixed (char* ver = Version)
        fixed (char* path = folder)
        {
            var idBytes = System.Text.Encoding.UTF8.GetBytes(id);
            fixed (byte* idp = idBytes)
            {
                var reg = new CF_SYNC_REGISTRATION
                {
                    StructSize = (uint)sizeof(CF_SYNC_REGISTRATION),
                    ProviderName = new PCWSTR(name),
                    ProviderVersion = new PCWSTR(ver),
                    SyncRootIdentity = idp,
                    SyncRootIdentityLength = (uint)idBytes.Length,
                    ProviderId = ProviderId,
                };
                var pol = new CF_SYNC_POLICIES
                {
                    StructSize = (uint)sizeof(CF_SYNC_POLICIES),
                    Hydration = new CF_HYDRATION_POLICY
                    {
                        Primary = CF_HYDRATION_POLICY_PRIMARY.CF_HYDRATION_POLICY_PROGRESSIVE,
                        Modifier = CF_HYDRATION_POLICY_MODIFIER.CF_HYDRATION_POLICY_MODIFIER_AUTO_DEHYDRATION_ALLOWED,
                    },
                    Population = new CF_POPULATION_POLICY
                    {
                        Primary = CF_POPULATION_POLICY_PRIMARY.CF_POPULATION_POLICY_FULL,
                        Modifier = CF_POPULATION_POLICY_MODIFIER.CF_POPULATION_POLICY_MODIFIER_NONE,
                    },
                    InSync = CF_INSYNC_POLICY.CF_INSYNC_POLICY_TRACK_FILE_LAST_WRITE_TIME
                             | CF_INSYNC_POLICY.CF_INSYNC_POLICY_TRACK_DIRECTORY_LAST_WRITE_TIME,
                    HardLink = CF_HARDLINK_POLICY.CF_HARDLINK_POLICY_NONE,
                    PlaceholderManagement = CF_PLACEHOLDER_MANAGEMENT_POLICY.CF_PLACEHOLDER_MANAGEMENT_POLICY_DEFAULT,
                };
                PInvoke.CfRegisterSyncRoot(new PCWSTR(path), &reg, &pol, CF_REGISTER_FLAGS.CF_REGISTER_FLAG_UPDATE)
                    .ThrowOnFailure();
            }
        }
    }

    /// <summary>Removes the registration (both ways; whichever exists).</summary>
    public static unsafe void Unregister(string folder, string id, Action<string> log)
    {
        try { StorageProviderSyncRootManager.Unregister(id); }
        catch (Exception e) { log($"Unregister({id}): {e.Message}"); }
        fixed (char* p = folder)
        {
            var hr = PInvoke.CfUnregisterSyncRoot(new PCWSTR(p));
            if (hr.Failed) log($"CfUnregisterSyncRoot: 0x{hr.Value:X8}");
        }
    }

    /// <summary>
    /// Tells Explorer the folder changed. After the sync root is registered again (sign-out and back in, another
    /// folder chosen), an Explorer window left open on it otherwise shows "This folder is empty" until reopened.
    /// </summary>
    public static unsafe void NotifyShell(string folder)
    {
        fixed (char* p = folder)
            PInvoke.SHChangeNotify(Windows.Win32.UI.Shell.SHCNE_ID.SHCNE_UPDATEDIR,
                                   Windows.Win32.UI.Shell.SHCNF_FLAGS.SHCNF_PATHW | Windows.Win32.UI.Shell.SHCNF_FLAGS.SHCNF_FLUSHNOWAIT,
                                   p, null);
    }

    public static bool IsRegistered(string id)
    {
        try { return StorageProviderSyncRootManager.GetCurrentSyncRoots().Any(r => r.Id == id); }
        catch { return false; }
    }
}
