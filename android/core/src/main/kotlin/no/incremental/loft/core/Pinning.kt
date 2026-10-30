// SPKI pinning for the gateway's own key (docs/design.md §C). The direct listener has a
// self-signed certificate; it is trusted only because its key hashes to a pin from the
// pairing payload (or from /v1/identity later). Never "accept any cert".
package no.incremental.loft.core

import okhttp3.OkHttpClient
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.Base64
import java.util.concurrent.TimeUnit
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

object Spki {
    /** base64(SHA-256(SubjectPublicKeyInfo DER)): the form `loft-admin pair` prints. */
    fun pin(cert: X509Certificate): String =
        Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded))
}

/** Where the trust manager reads the current pins (they change after /v1/identity). */
fun interface PinSource {
    fun pins(): Set<String>
}

/**
 * Accepts a server only if its leaf certificate's key matches a pin. The chain, the issuer,
 * the dates and the host name don't matter: the pin is the identity (the same rule as the
 * Apple client's PinnedSessionDelegate). A mismatch is a failed handshake, which the
 * client reads as "no answer" on that route.
 */
class PinnedTrustManager(private val source: PinSource) : X509TrustManager {
    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        val leaf = chain?.firstOrNull() ?: throw CertificateException("no certificate")
        val pins = source.pins()
        if (pins.isEmpty()) throw CertificateException("no pins")
        if (Spki.pin(leaf) !in pins) throw CertificateException("certificate key does not match the pinned key")
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw CertificateException("client certificates are not used")
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}

/** The pin decides; the gateway's certificate names a LAN address that rarely matches. */
private val PIN_IS_THE_NAME = HostnameVerifier { _, _ -> true }

object HttpClients {
    /**
     * Base client shared by both routes. No cache, no cookies, no redirects: a redirect
     * would carry the device token to wherever it points.
     */
    fun base(): OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .followRedirects(false)
        .followSslRedirects(false)
        .retryOnConnectionFailure(true)
        .build()

    /** For the direct listener (and a `direct`-mode outside address): pinned, self-signed. */
    fun pinned(base: OkHttpClient, pins: PinSource): OkHttpClient {
        val tm = PinnedTrustManager(pins)
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(null, arrayOf(tm), SecureRandom())
        return base.newBuilder()
            .sslSocketFactory(ctx.socketFactory, tm)
            .hostnameVerifier(PIN_IS_THE_NAME)
            .build()
    }
}
