// Non-secret app state in private SharedPreferences: the pairing (it holds no secret, see
// docs/pairing.md), the account's display data, the chosen root and the settings.
// The device token is NOT here: KeystoreTokenStore holds it.
package no.incremental.loft.data

import android.content.Context
import android.content.SharedPreferences
import kotlinx.serialization.builtins.ListSerializer
import no.incremental.loft.core.Item
import no.incremental.loft.core.LoftJson
import no.incremental.loft.core.Pairing
import no.incremental.loft.core.ResumeStore
import no.incremental.loft.core.RootInfo
import no.incremental.loft.work.CameraLedger

class AppPrefs(ctx: Context) {
    val sp: SharedPreferences = ctx.getSharedPreferences("loft_state", Context.MODE_PRIVATE)

    var pairing: Pairing?
        get() = sp.getString(K_PAIRING, null)?.let { runCatching { Pairing.fromJson(it) }.getOrNull() }
        set(v) = sp.edit().putString(K_PAIRING, v?.toJson()).apply()

    var nasUser: String?
        get() = sp.getString(K_USER, null)
        set(v) = sp.edit().putString(K_USER, v).apply()

    var deviceName: String?
        get() = sp.getString(K_DEVICE_NAME, null)
        set(v) = sp.edit().putString(K_DEVICE_NAME, v).apply()

    var root: RootInfo?
        get() = sp.getString(K_ROOT, null)?.let { runCatching { LoftJson.decodeFromString(RootInfo.serializer(), it) }.getOrNull() }
        set(v) = sp.edit().putString(K_ROOT, v?.let { LoftJson.encodeToString(RootInfo.serializer(), it) }).apply()

    var anchor: String?
        get() = sp.getString(K_ANCHOR, null)
        set(v) = sp.edit().putString(K_ANCHOR, v).apply()

    // Camera upload (opt-in)
    var cameraUpload: Boolean
        get() = sp.getBoolean(K_CAM_ON, false)
        set(v) = sp.edit().putBoolean(K_CAM_ON, v).apply()
    var cameraFolderId: String?
        get() = sp.getString(K_CAM_FOLDER, null)
        set(v) = sp.edit().putString(K_CAM_FOLDER, v).apply()
    var cameraFolderName: String?
        get() = sp.getString(K_CAM_FOLDER_NAME, null)
        set(v) = sp.edit().putString(K_CAM_FOLDER_NAME, v).apply()
    var cameraWifiOnly: Boolean
        get() = sp.getBoolean(K_CAM_WIFI, true)
        set(v) = sp.edit().putBoolean(K_CAM_WIFI, v).apply()
    /** Camera upload's ledger (work/CameraLedger.kt): what is already uploaded, since it was switched on. */
    var cameraLedger: CameraLedger
        get() = CameraLedger.decode(sp.getLong(K_CAM_SINCE, 0), sp.getString(K_CAM_DONE, null))
        set(v) = sp.edit().putLong(K_CAM_SINCE, v.floor).putString(K_CAM_DONE, v.encode()).apply()

    /** Camera upload switched on: only photos and videos added from now on. */
    fun startCameraLedger() {
        cameraLedger = CameraLedger(System.currentTimeMillis() / 1000)
    }

    // Update check (on by default, switchable off)
    var updateCheck: Boolean
        get() = sp.getBoolean(K_UPDATES, true)
        set(v) = sp.edit().putBoolean(K_UPDATES, v).apply()
    var notifiedVersionCode: Long
        get() = sp.getLong(K_NOTIFIED, 0)
        set(v) = sp.edit().putLong(K_NOTIFIED, v).apply()

    /** Recently opened or saved files, newest first (DocumentsProvider recents). */
    var recents: List<Item>
        get() = sp.getString(K_RECENTS, null)?.let {
            runCatching { LoftJson.decodeFromString(ListSerializer(Item.serializer()), it) }.getOrNull()
        } ?: emptyList()
        set(v) = sp.edit().putString(K_RECENTS, LoftJson.encodeToString(ListSerializer(Item.serializer()), v.take(MAX_RECENTS))).apply()

    @Synchronized
    fun addRecent(item: Item) {
        if (item.isDirectory) return
        recents = listOf(item) + recents.filter { it.id != item.id }
    }

    @Synchronized
    fun removeRecent(id: String) {
        recents = recents.filter { it.id != id }
    }

    /** Everything bound to the account goes on sign-out; the pairing and settings stay. */
    fun clearAccount() {
        sp.edit().remove(K_USER).remove(K_DEVICE_NAME).remove(K_ROOT).remove(K_ANCHOR).remove(K_RECENTS)
            .remove(K_CAM_ON).remove(K_CAM_FOLDER).remove(K_CAM_FOLDER_NAME).remove(K_CAM_SINCE).remove(K_CAM_DONE)
            .remove(K_RESUME).apply()
    }

    val resumeStore: ResumeStore = object : ResumeStore {
        private fun map(): MutableMap<String, String> =
            sp.getStringSet(K_RESUME, emptySet())!!.mapNotNull { e ->
                e.split('=', limit = 2).takeIf { it.size == 2 }?.let { it[0] to it[1] }
            }.toMap().toMutableMap()

        private fun store(m: Map<String, String>) =
            sp.edit().putStringSet(K_RESUME, m.map { "${it.key}=${it.value}" }.toSet()).apply()

        @Synchronized override fun get(key: String) = map()[key]
        @Synchronized override fun put(key: String, uploadId: String) = store(map().apply { put(key, uploadId) })
        @Synchronized override fun forgetUpload(uploadId: String) = store(map().filterValues { it != uploadId })
    }

    companion object {
        private const val K_PAIRING = "pairing"
        private const val K_USER = "nas_user"
        private const val K_DEVICE_NAME = "device_name"
        private const val K_ROOT = "root"
        private const val K_ANCHOR = "anchor"
        private const val K_CAM_ON = "camera_upload"
        private const val K_CAM_FOLDER = "camera_folder_id"
        private const val K_CAM_FOLDER_NAME = "camera_folder_name"
        private const val K_CAM_WIFI = "camera_wifi_only"
        private const val K_CAM_SINCE = "camera_since"
        private const val K_CAM_DONE = "camera_done"
        private const val K_UPDATES = "update_check"
        private const val K_NOTIFIED = "notified_version_code"
        private const val K_RECENTS = "recents"
        private const val K_RESUME = "upload_resume"
        private const val MAX_RECENTS = 40
    }
}
