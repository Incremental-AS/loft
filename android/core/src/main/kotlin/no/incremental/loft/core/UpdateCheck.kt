// The update check for the APK outside Google Play (docs/design.md §E.4): at most daily, one GET of
// releases.json, the only request Loft makes to Incremental. Switchable off in Settings.
// Format (docs/releases-json.md, android/docs/releases.example.json):
//   {"android": {"version": "0.2.1", "version_code": 2, "url": "https://loft.incremental.no/download/"}}
// The same object under a top-level "platforms" key is read too, as the Windows and Linux apps do.
package no.incremental.loft.core

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.longOrNull
import java.net.URI

data class UpdateInfo(val version: String, val versionCode: Long, val pageUrl: String)

object UpdateCheck {
    const val RELEASES_URL = "https://loft.incremental.no/releases.json"
    const val DOWNLOAD_PAGE = "https://loft.incremental.no/download/"

    /** null = nothing newer than `currentVersionCode` (or a file we can't read). */
    fun parse(json: String, currentVersionCode: Long, platform: String = "android"): UpdateInfo? {
        val root = try {
            LoftJson.parseToJsonElement(json) as? JsonObject
        } catch (e: Exception) {
            null
        } ?: return null
        val p = root[platform] as? JsonObject ?: (root["platforms"] as? JsonObject)?.get(platform) as? JsonObject
            ?: return null
        val code = (p["version_code"] as? JsonPrimitive)?.longOrNull ?: return null
        val version = (p["version"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return null
        if (code <= currentVersionCode) return null
        val url = (p["url"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        return UpdateInfo(version, code, safePage(url))
    }

    /** The notification only ever opens a page on loft.incremental.no. */
    fun safePage(url: String?): String {
        if (url == null) return DOWNLOAD_PAGE
        return try {
            val u = URI(url)
            if (u.scheme == "https" && u.host == "loft.incremental.no" && u.rawUserInfo == null && u.port == -1) url
            else DOWNLOAD_PAGE
        } catch (e: Exception) {
            DOWNLOAD_PAGE
        }
    }
}
