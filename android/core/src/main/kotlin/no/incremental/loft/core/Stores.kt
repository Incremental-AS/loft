// The device token (the only secret on the device; never the password) and the upload
// resume journal. The Android app backs TokenStore with an Android Keystore key.
package no.incremental.loft.core

import kotlinx.serialization.Serializable

@Serializable
data class StoredToken(
    val token: String,
    val deviceId: String,
    val issuedAtMs: Long,
    val expiresAtMs: Long,
) {
    fun dueForRotation(nowMs: Long = System.currentTimeMillis()): Boolean = nowMs - issuedAtMs > ROTATE_AFTER_MS

    companion object {
        /** The gateway's TTL is 90 days, sliding on every refresh (LoftConfig.rotateTokenAfter). */
        const val ROTATE_AFTER_MS = 7L * 86_400_000L

        fun of(r: LoginResult, nowMs: Long = System.currentTimeMillis()) =
            StoredToken(r.token, r.deviceId, nowMs, (r.expiresAt * 1000).toLong())
    }
}

interface TokenStore {
    fun load(): StoredToken?
    fun save(t: StoredToken)
    fun clear()
}

class MemoryTokenStore(private var t: StoredToken? = null) : TokenStore {
    @Synchronized override fun load() = t
    @Synchronized override fun save(t: StoredToken) { this.t = t }
    @Synchronized override fun clear() { t = null }
}

/**
 * Unfinished upload sessions by content (`<sha256>:<size>`), so a retried upload continues
 * from the gateway's `received`. Content-keyed is safe: the gateway checks the whole-file
 * hash at commit.
 */
interface ResumeStore {
    fun get(key: String): String?
    fun put(key: String, uploadId: String)
    fun forgetUpload(uploadId: String)
}

class MemoryResumeStore : ResumeStore {
    private val m = HashMap<String, String>()
    @Synchronized override fun get(key: String) = m[key]
    @Synchronized override fun put(key: String, uploadId: String) { m[key] = uploadId }
    @Synchronized override fun forgetUpload(uploadId: String) { m.values.removeAll { it == uploadId } }
}
