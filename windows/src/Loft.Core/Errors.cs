// Error mapping, as GatewayClient.swift / Models.swift (SyncError, LoopState).
namespace Loft.Core;

public enum GatewayErrorKind
{
    /// <summary>The gateway answered with an error body (<see cref="GatewayException.Api"/>).</summary>
    Api,
    /// <summary>No answer: network, TLS, pin mismatch, timeout, or an edge proxy without its gateway.</summary>
    Unreachable,
    /// <summary>Answered, but not something we understand.</summary>
    BadResponse,
    /// <summary>No token in the credential store.</summary>
    NotSignedIn,
    /// <summary>The file changed on the NAS under a download (or shrank under an upload).</summary>
    ChangedDuringTransfer,
    /// <summary>A password sign-in needs a pinned route, and there is none right now.</summary>
    SignInNeedsHome,
}

public sealed class GatewayException : Exception
{
    public GatewayErrorKind Kind { get; }
    public ApiError? Api { get; }
    public int Status { get; }

    private GatewayException(GatewayErrorKind kind, string message, ApiError? api = null, int status = 0,
                             Exception? inner = null)
        : base(message, inner)
    {
        Kind = kind; Api = api; Status = api?.Status ?? status;
    }

    public static GatewayException FromApi(ApiError e) => new(GatewayErrorKind.Api, $"gateway {e.Status} {e.Error}", e);
    public static GatewayException Unreachable(string why, Exception? inner = null) =>
        new(GatewayErrorKind.Unreachable, "unreachable: " + why, inner: inner);
    public static GatewayException BadResponse(int status, string why) =>
        new(GatewayErrorKind.BadResponse, $"bad response {status}: {why}", status: status);
    public static GatewayException NotSignedIn() => new(GatewayErrorKind.NotSignedIn, "not signed in");
    public static GatewayException ChangedDuringTransfer() =>
        new(GatewayErrorKind.ChangedDuringTransfer, "changed during transfer");
    public static GatewayException SignInNeedsHome() =>
        new(GatewayErrorKind.SignInNeedsHome, "password sign-in needs the home network or a direct outside address");

    public bool IsUnreachable => Kind == GatewayErrorKind.Unreachable;
    public string? Code => Api?.Error;
}

/// <summary>Why sync has stopped, as the tray menu shows it. Feed it sync-loop failures only.</summary>
public enum SyncErrorKind
{
    /// <summary>401 nas_credentials_invalid</summary>
    NasPasswordChanged,
    /// <summary>401 token_expired / invalid_token / token_superseded / missing_token</summary>
    SignInExpired,
    /// <summary>401 device_revoked</summary>
    DeviceRemoved,
    /// <summary>403 access_denied / 404 root_missing: the Loft folder is gone or this account lost it.</summary>
    NoAccess,
    /// <summary>Anything else; the code is for the log, never the menu.</summary>
    Stopped,
}

public sealed record SyncError(SyncErrorKind Kind, string? Code = null)
{
    public bool NeedsSignIn => Kind is SyncErrorKind.NasPasswordChanged or SyncErrorKind.SignInExpired
        or SyncErrorKind.DeviceRemoved;

    /// <summary>null = not an error state (NAS down behind the gateway, or anchor_expired): keep retrying.</summary>
    public static SyncError? FromLoopFailure(ApiError e) => e.Error switch
    {
        "nas_credentials_invalid" => new(SyncErrorKind.NasPasswordChanged),
        "token_expired" or "invalid_token" or "token_superseded" or "missing_token" => new(SyncErrorKind.SignInExpired),
        "device_revoked" => new(SyncErrorKind.DeviceRemoved),
        "access_denied" or "root_missing" => new(SyncErrorKind.NoAccess),
        "nas_unavailable" or "nas_error" or "anchor_expired" => null,
        _ => new(SyncErrorKind.Stopped, e.Error),
    };
}

public enum LoopStateKind { Ok, CantReachHome, CantReachNas, Failed }

/// <summary>What the tray should say after one round of the change loop.</summary>
public sealed record LoopState(LoopStateKind Kind, SyncError? Error = null)
{
    public static readonly LoopState Ok = new(LoopStateKind.Ok);
    public static readonly LoopState CantReachHome = new(LoopStateKind.CantReachHome);
    public static readonly LoopState CantReachNas = new(LoopStateKind.CantReachNas);

    public static LoopState From(Exception error)
    {
        if (error is not GatewayException g) return CantReachHome;   // HttpRequestException and friends
        switch (g.Kind)
        {
            case GatewayErrorKind.Unreachable:
            case GatewayErrorKind.SignInNeedsHome:
                return CantReachHome;
            case GatewayErrorKind.NotSignedIn:
                return new(LoopStateKind.Failed, new SyncError(SyncErrorKind.SignInExpired));
            case GatewayErrorKind.Api:
                if (g.Api!.IsNasUnreachable) return CantReachNas;
                return SyncError.FromLoopFailure(g.Api) is { } s ? new(LoopStateKind.Failed, s) : Ok;
            case GatewayErrorKind.BadResponse:
                return new(LoopStateKind.Failed, new SyncError(SyncErrorKind.Stopped, $"bad_response_{g.Status}"));
            default:
                return Ok;
        }
    }
}
