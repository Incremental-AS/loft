// The device token, encrypted with an AES-256-GCM key that lives in the Android Keystore
// (hardware-backed where the phone has it) and never leaves it. The ciphertext sits in
// private SharedPreferences; without the Keystore key it is useless, and backups are off.
package no.incremental.loft.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import no.incremental.loft.core.LoftJson
import no.incremental.loft.core.StoredToken
import no.incremental.loft.core.TokenStore
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class KeystoreTokenStore(ctx: Context, private val alias: String = "loft.device-token") : TokenStore {
    private val sp = ctx.getSharedPreferences("loft_token", Context.MODE_PRIVATE)
    @Volatile private var cache: StoredToken? = null
    @Volatile private var loaded = false

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (ks.getKey(alias, null) as? SecretKey)?.let { return it }
        val kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        kg.init(
            KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return kg.generateKey()
    }

    @Synchronized
    override fun load(): StoredToken? {
        if (loaded) return cache
        val blob = sp.getString(K_BLOB, null)
        cache = blob?.let {
            try {
                val raw = Base64.decode(it, Base64.NO_WRAP)
                val c = Cipher.getInstance(TRANSFORM)
                c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, raw, 0, IV_LEN))
                val plain = c.doFinal(raw, IV_LEN, raw.size - IV_LEN)
                LoftJson.decodeFromString(StoredToken.serializer(), plain.decodeToString())
            } catch (e: Exception) {
                null                       // key gone (e.g. restored to another phone): signed out
            }
        }
        loaded = true
        return cache
    }

    @Synchronized
    override fun save(t: StoredToken) {
        val c = Cipher.getInstance(TRANSFORM)
        c.init(Cipher.ENCRYPT_MODE, key())
        val iv = c.iv
        require(iv.size == IV_LEN)
        val ct = c.doFinal(LoftJson.encodeToString(StoredToken.serializer(), t).encodeToByteArray())
        sp.edit().putString(K_BLOB, Base64.encodeToString(iv + ct, Base64.NO_WRAP)).commit()
        cache = t
        loaded = true
    }

    @Synchronized
    override fun clear() {
        sp.edit().remove(K_BLOB).commit()
        cache = null
        loaded = true
    }

    companion object {
        private const val KEYSTORE = "AndroidKeyStore"
        private const val TRANSFORM = "AES/GCM/NoPadding"
        private const val IV_LEN = 12
        private const val K_BLOB = "token"
    }
}
