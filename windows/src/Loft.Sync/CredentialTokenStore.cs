// The device token in Windows Credential Manager (a generic credential for this Windows user,
// kept on this PC only and protected by the system with DPAPI). Never the password.
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using Loft.Core;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.Security.Credentials;

namespace Loft.Sync;

[SupportedOSPlatform("windows5.1.2600")]
public sealed unsafe class CredentialTokenStore : ITokenStore
{
    public const string DefaultTarget = "no.incremental.loft/device-token";
    private readonly string _target;
    private readonly object _lock = new();

    public CredentialTokenStore(string target = DefaultTarget) => _target = target;

    public StoredToken? Load()
    {
        lock (_lock)
        {
            CREDENTIALW* cred;
            fixed (char* t = _target)
            {
                if (!PInvoke.CredRead(new PCWSTR(t), CRED_TYPE.CRED_TYPE_GENERIC, 0, &cred)) return null;
            }
            try
            {
                return StoredToken.Deserialize(new ReadOnlySpan<byte>(cred->CredentialBlob, (int)cred->CredentialBlobSize));
            }
            finally { PInvoke.CredFree(cred); }
        }
    }

    public void Save(StoredToken token)
    {
        lock (_lock)
        {
            var blob = token.Serialize();
            try
            {
                fixed (char* t = _target)
                fixed (char* user = "Loft")
                fixed (byte* b = blob)
                {
                    var c = new CREDENTIALW
                    {
                        Type = CRED_TYPE.CRED_TYPE_GENERIC,
                        TargetName = new PWSTR(t),
                        UserName = new PWSTR(user),
                        CredentialBlob = b,
                        CredentialBlobSize = (uint)blob.Length,
                        Persist = CRED_PERSIST.CRED_PERSIST_LOCAL_MACHINE,
                    };
                    if (!PInvoke.CredWrite(&c, 0))
                        throw new IOException($"Credential Manager refused the token (win32 error {Marshal.GetLastWin32Error()})");
                }
            }
            finally { Array.Clear(blob); }
        }
    }

    public void Clear()
    {
        lock (_lock)
        {
            fixed (char* t = _target) PInvoke.CredDelete(new PCWSTR(t), CRED_TYPE.CRED_TYPE_GENERIC, 0);
        }
    }
}
