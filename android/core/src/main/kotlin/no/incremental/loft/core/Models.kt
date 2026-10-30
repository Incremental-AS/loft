// Wire models for the gateway API. gateway/loft_gateway/api.py + service.py are the source
// of truth; these mirror apple/Packages/LoftCore/Sources/LoftCore/Models.swift.
package no.incremental.loft.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

val LoftJson = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    encodeDefaults = true
}

@Serializable
data class Item(
    val id: String,
    @SerialName("parent_id") val parentId: String? = null,
    val name: String,
    val type: String,                         // "file" | "dir"
    val size: Long = 0,
    @SerialName("mtime_ns") val mtimeNs: Long = 0,
    @SerialName("content_etag") val contentEtag: String = "",
    @SerialName("meta_etag") val metaEtag: String = "",
    val sha256: String? = null,
    /** All-shares root: true on the root and on each share (no rename, move or delete). */
    val fixed: Boolean? = null,
) {
    val isDirectory: Boolean get() = type == "dir"
    val isFixed: Boolean get() = fixed ?: false
    val modifiedMillis: Long get() = mtimeNs / 1_000_000
}

@Serializable
data class ChildrenPage(val items: List<Item>, @SerialName("next_cursor") val nextCursor: String? = null)

@Serializable
data class ChangesPage(
    val updated: List<Item> = emptyList(),
    val deleted: List<String> = emptyList(),
    val anchor: String,
    val more: Boolean = false,
) {
    val isEmpty: Boolean get() = updated.isEmpty() && deleted.isEmpty()
}

@Serializable
data class AnchorResponse(val anchor: String)

@Serializable
data class LoginResult(
    @SerialName("device_id") val deviceId: String,
    val token: String,
    @SerialName("nas_user") val nasUser: String? = null,
    @SerialName("expires_at") val expiresAt: Double,
)

@Serializable
data class RootInfo(
    val id: String,
    val share: String,
    val path: String,
    @SerialName("root_item_id") val rootItemId: String,
    val anchor: String,
) {
    val isAllShares: Boolean get() = share == ALL_SHARES
    val displayPath: String get() = if (path.isEmpty()) share else "$share/$path"

    companion object {
        const val ALL_SHARES = "*"
    }
}

@Serializable
data class Me(
    @SerialName("nas_user") val nasUser: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("device_name") val deviceName: String,
    val route: String,
    val root: RootInfo? = null,
)

@Serializable
data class Device(
    val id: String,
    @SerialName("device_name") val deviceName: String,
    @SerialName("created_at") val createdAt: Double,
    @SerialName("last_used_at") val lastUsedAt: Double? = null,
    @SerialName("revoked_at") val revokedAt: Double? = null,
)

@Serializable
data class DevicesResponse(val devices: List<Device>, val current: String)

@Serializable
data class SharesResponse(val shares: List<String>)

@Serializable
data class FoldersResponse(val share: String, val path: String, val folders: List<String>)

@Serializable
data class UploadState(
    @SerialName("upload_id") val uploadId: String,
    val received: Long,
    val size: Long,
    @SerialName("chunk_max") val chunkMax: Long? = null,
)

@Serializable
data class CommitRequest(
    @SerialName("parent_id") val parentId: String? = null,
    val name: String? = null,
    @SerialName("item_id") val itemId: String? = null,     // modify existing content
    @SerialName("if_match") val ifMatch: String? = null,   // content etag the edit is based on
    val replaces: String? = null,                          // safe-save: the item this create supersedes
)

@Serializable
data class CommitResponse(
    val result: String,                                    // created | modified | replaced | conflict
    val item: Item,
    @SerialName("replaced_id") val replacedId: String? = null,
)

/** "trashed" | "superseded" | "already_deleted": all three are success. */
@Serializable
data class DeleteResponse(val result: String, @SerialName("superseded_by") val supersededBy: String? = null)

@Serializable
data class Health(val service: String = "", val route: String = "")

@Serializable
data class Identity(@SerialName("direct_spki_sha256") val directSpkiSha256: List<String> = emptyList())

/** Gateway error body `{"error": "<code>", ...}` plus the HTTP status. */
data class ApiError(
    val status: Int,
    val error: String,
    val received: Long? = null,         // 409 wrong_offset / upload_incomplete
    val existingId: String? = null,     // 409 name_exists
    val current: Item? = null,          // 412 *_etag_mismatch
) {
    val isAnchorExpired: Boolean get() = error == "anchor_expired"
    val isNasUnreachable: Boolean get() = error == "nas_unavailable" || error == "nas_error"
    val needsSignIn: Boolean get() = SyncError.of(this)?.needsSignIn ?: false

    @Serializable
    internal data class Body(
        val error: String,
        val received: Long? = null,
        @SerialName("existing_id") val existingId: String? = null,
        val current: Item? = null,
    )
}

/** Why sync has stopped (LoftCore.SyncError). Feed it failures of the sync loop only. */
sealed class SyncError {
    object NasPasswordChanged : SyncError()   // 401 nas_credentials_invalid
    object SignInExpired : SyncError()        // 401 token_expired / invalid_token / token_superseded / missing_token
    object DeviceRemoved : SyncError()        // 401 device_revoked
    object NoAccess : SyncError()             // 403 access_denied / 404 root_missing
    data class Stopped(val code: String) : SyncError()

    val needsSignIn: Boolean
        get() = this == NasPasswordChanged || this == SignInExpired || this == DeviceRemoved

    companion object {
        /** null = not an error state (NAS down behind the gateway, or anchor_expired). */
        fun of(e: ApiError): SyncError? = when (e.error) {
            "nas_credentials_invalid" -> NasPasswordChanged
            "token_expired", "invalid_token", "token_superseded", "missing_token" -> SignInExpired
            "device_revoked" -> DeviceRemoved
            "access_denied", "root_missing" -> NoAccess
            "nas_unavailable", "nas_error", "anchor_expired" -> null
            else -> Stopped(e.error)
        }
    }
}

/** What the app shows after one round of the change loop (LoftCore.LoopState). */
sealed class LoopState {
    object Ok : LoopState()
    object CantReachHome : LoopState()        // no answer from the gateway
    object CantReachNas : LoopState()         // gateway up, NAS behind it down
    data class Failed(val error: SyncError) : LoopState()

    companion object {
        fun of(t: Throwable): LoopState = when (t) {
            is GatewayException.Unreachable -> CantReachHome
            is GatewayException.NotSignedIn -> Failed(SyncError.SignInExpired)
            is GatewayException.Api -> when {
                t.error.isNasUnreachable -> CantReachNas
                else -> SyncError.of(t.error)?.let { Failed(it) } ?: Ok
            }
            is GatewayException.BadResponse -> Failed(SyncError.Stopped("bad_response_${t.status}"))
            is GatewayException.ChangedDuringTransfer -> Ok
            else -> CantReachHome
        }
    }
}

sealed class GatewayException(message: String, cause: Throwable? = null) : Exception(message, cause) {
    /** The gateway answered with an error body. */
    class Api(val error: ApiError) : GatewayException("gateway ${error.status} ${error.error}")
    /** No answer: network, TLS, pin mismatch, timeout, or an edge proxy without the gateway. */
    class Unreachable(why: String, cause: Throwable? = null) : GatewayException("unreachable: $why", cause)
    /** Answered, but not something we understand. */
    class BadResponse(val status: Int, why: String) : GatewayException("bad response $status: $why")
    /** No device token stored. */
    class NotSignedIn : GatewayException("not signed in")
    /** The file changed on the NAS under a download, or shrank under an upload. */
    class ChangedDuringTransfer : GatewayException("changed during transfer")
}

/** Names that never leave the device (LoftCore.Exclusions; the gateway refuses them too). */
object Exclusions {
    private val prefixes = listOf("~$", "._", ".~lock.", "Word Work File ", "~WRL", "~WRD", ".loft")
    private val exact = setOf(".DS_Store", "Icon\r", "Thumbs.db", "desktop.ini")

    fun isExcluded(name: String): Boolean =
        name in exact || prefixes.any { name.startsWith(it) } || name.endsWith(".tmp") || name.contains(".sb-")
}
