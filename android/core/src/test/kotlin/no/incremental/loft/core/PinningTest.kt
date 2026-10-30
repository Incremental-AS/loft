package no.incremental.loft.core

import kotlinx.coroutines.runBlocking
import okhttp3.Request
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.IOException
import java.util.Base64

/** Real TLS handshakes against a self-signed key, as the gateway's direct listener has. */
class PinningTest {
    private lateinit var server: MockWebServer
    // A name that doesn't match the address we connect to, like a gateway cert with a LAN IP.
    private val gatewayCert = HeldCertificate.Builder().commonName("loft direct").addSubjectAlternativeName("gateway.invalid").build()
    private val otherCert = HeldCertificate.Builder().commonName("look-alike").addSubjectAlternativeName("gateway.invalid").build()

    @Before fun setUp() {
        server = MockWebServer()
        server.useHttps(HandshakeCertificates.Builder().heldCertificate(gatewayCert).build().sslSocketFactory(), false)
        server.start()
    }

    @After fun tearDown() = server.shutdown()

    private fun get(pins: Set<String>): Int {
        val c = HttpClients.pinned(HttpClients.base(), PinSource { pins })
        return c.newCall(Request.Builder().url(server.url("/healthz")).build()).execute().use { it.code }
    }

    @Test fun pinOfMatchesTheGatewayFormula() {
        // base64(sha256(SubjectPublicKeyInfo)): what `loft-gateway init` prints (and openssl computes).
        val spki = gatewayCert.keyPair.public.encoded
        val expected = Base64.getEncoder().encodeToString(java.security.MessageDigest.getInstance("SHA-256").digest(spki))
        assertEquals(expected, Spki.pin(gatewayCert.certificate))
        assertTrue(Pairing.isValidPin(expected))
    }

    @Test fun acceptsTheSelfSignedKeyByPinWhateverTheName() {
        server.enqueue(MockResponse().setBody("{}"))
        assertEquals(200, get(setOf(Spki.pin(gatewayCert.certificate))))
    }

    @Test fun acceptsTheStandbyPinToo() {
        server.enqueue(MockResponse().setBody("{}"))
        assertEquals(200, get(setOf(Spki.pin(otherCert.certificate), Spki.pin(gatewayCert.certificate))))
    }

    @Test fun refusesAnotherKey() {
        server.enqueue(MockResponse().setBody("{}"))
        try {
            get(setOf(Spki.pin(otherCert.certificate)))
            fail("connected to a server with an unpinned key")
        } catch (e: IOException) {
            // handshake refused: the request never left
        }
        assertEquals(0, server.requestCount)
    }

    @Test fun refusesWithNoPins() {
        try {
            get(emptySet())
            fail("connected without pins")
        } catch (e: IOException) {
        }
        assertEquals(0, server.requestCount)
    }

    @Test fun systemPkiRefusesTheSelfSignedKey() {
        // The tunnel route uses normal web PKI: a self-signed gateway key must not pass there.
        server.enqueue(MockResponse().setBody("{}"))
        try {
            HttpClients.base().newCall(Request.Builder().url(server.url("/healthz")).build()).execute().close()
            fail("system trust accepted a self-signed key")
        } catch (e: IOException) {
        }
    }

    @Test fun probeIsFalseOnPinMismatch() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"ok":true,"service":"loft","route":"direct"}"""))
        val pairing = Pairing("H", server.url("/").toString().trimEnd('/'), listOf(Spki.pin(otherCert.certificate)))
        val base = HttpClients.base()
        val client = GatewayClient(RouteSelector(pairing), MemoryTokenStore(),
            HttpClients.pinned(base, PinSource { pairing.pins.toSet() }), base)
        assertFalse(client.probe(client.selector.home))
    }

    @Test fun probeIsTrueOnPinMatch() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"ok":true,"service":"loft","route":"direct"}"""))
        val pairing = Pairing("H", server.url("/").toString().trimEnd('/'), listOf(Spki.pin(gatewayCert.certificate)))
        val base = HttpClients.base()
        val client = GatewayClient(RouteSelector(pairing), MemoryTokenStore(),
            HttpClients.pinned(base, PinSource { pairing.pins.toSet() }), base)
        assertTrue(client.probe(client.selector.home))
    }
}
