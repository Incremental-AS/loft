package no.incremental.loft.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.Base64

class PairingTest {
    // Test pins: base64 of 32 bytes, not any real key.
    private val pinA = Base64.getEncoder().encodeToString(ByteArray(32) { 1 })
    private val pinB = Base64.getEncoder().encodeToString(ByteArray(32) { 2 })

    private fun payload(json: String) =
        Base64.getUrlEncoder().withoutPadding().encodeToString(json.toByteArray())

    private fun reason(input: String): PairingException.Reason {
        try {
            Pairing.parse(input)
        } catch (e: PairingException) {
            return e.reason
        }
        fail("accepted: $input")
        throw AssertionError()
    }

    @Test fun parsesLinkAsTheGatewayPrintsIt() {
        val json = """{"v":1,"name":"Home","lan":"https://10.0.0.5:8443","pins":["$pinA","$pinB"]}"""
        val p = Pairing.parse("https://loft.incremental.no/pair#" + payload(json))
        assertEquals("Home", p.name)
        assertEquals("https://10.0.0.5:8443", p.lanUrl)
        assertEquals(listOf(pinA, pinB), p.pins)
        assertNull(p.publicUrl)
        assertNull(p.publicMode)
    }

    @Test fun acceptsLoftSchemeBarePayloadAndWhitespace() {
        val json = """{"v":1,"name":"Home","lan":"https://gw.example:8443","pins":["$pinA"]}"""
        assertEquals("Home", Pairing.parse("loft://pair#" + payload(json)).name)
        assertEquals("Home", Pairing.parse("  " + payload(json) + "\n").name)
        assertEquals("Home", Pairing.parse("HTTPS://LOFT.INCREMENTAL.NO/pair/#" + payload(json)).name)
    }

    @Test fun roundTripsThroughTheLink() {
        val p = Pairing("Attic", "https://gw.example:8443", listOf(pinA, pinB), "https://files.example", PublicMode.TUNNEL)
        assertEquals(p, Pairing.parse(p.toLink()))
        assertEquals(p, Pairing.fromJson(p.toJson()))
        assertTrue(p.toLink().startsWith("https://loft.incremental.no/pair#"))
        assertTrue(!p.toPayload().contains("="))
    }

    @Test fun publicUrlAndMode() {
        val t = Pairing.parse(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":["$pinA"],"pub":"https://x.example","mode":"tunnel"}"""))
        assertEquals(PublicMode.TUNNEL, t.publicMode)
        assertEquals("https://x.example", t.publicUrl)
        val d = Pairing.parse(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":["$pinA"],"pub":"https://x.example:8443/","mode":"direct"}"""))
        assertEquals(PublicMode.DIRECT, d.publicMode)
        assertEquals("https://x.example:8443", d.publicUrl)
    }

    @Test fun refusesUnknownVersion() {
        assertEquals(PairingException.Reason.UNKNOWN_VERSION,
            reason(payload("""{"v":2,"name":"H","lan":"https://a:1","pins":["$pinA"]}""")))
        assertEquals(PairingException.Reason.UNKNOWN_VERSION,
            reason(payload("""{"name":"H","lan":"https://a:1","pins":["$pinA"]}""")))
        assertEquals(PairingException.Reason.UNKNOWN_VERSION,
            reason(payload("""{"v":"1","name":"H","lan":"https://a:1","pins":["$pinA"]}""")))
    }

    @Test fun refusesNonHttpsUrls() {
        assertEquals(PairingException.Reason.NOT_HTTPS,
            reason(payload("""{"v":1,"name":"H","lan":"http://a:1","pins":["$pinA"]}""")))
        assertEquals(PairingException.Reason.NOT_HTTPS,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":["$pinA"],"pub":"http://x","mode":"direct"}""")))
        assertEquals(PairingException.Reason.NOT_HTTPS,
            reason(payload("""{"v":1,"name":"H","lan":"https://u@a:1","pins":["$pinA"]}""")))
        assertEquals(PairingException.Reason.NOT_HTTPS,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1/x","pins":["$pinA"]}""")))
        assertEquals(PairingException.Reason.NOT_HTTPS,
            reason(payload("""{"v":1,"name":"H","lan":"https:///","pins":["$pinA"]}""")))
    }

    @Test fun refusesMissingOrBadPins() {
        assertEquals(PairingException.Reason.NO_PINS,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":[]}""")))
        assertEquals(PairingException.Reason.MISSING_FIELD,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1"}""")))
        assertEquals(PairingException.Reason.BAD_PIN,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":["abc"]}""")))
        assertEquals(PairingException.Reason.BAD_PIN,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":[1]}""")))
    }

    @Test fun modeOnlyWithPub() {
        assertEquals(PairingException.Reason.BAD_MODE,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":["$pinA"],"pub":"https://x"}""")))
        assertEquals(PairingException.Reason.BAD_MODE,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":["$pinA"],"mode":"direct"}""")))
        assertEquals(PairingException.Reason.BAD_MODE,
            reason(payload("""{"v":1,"name":"H","lan":"https://a:1","pins":["$pinA"],"pub":"https://x","mode":"vpn"}""")))
    }

    @Test fun refusesOtherLinksAndGarbage() {
        val good = payload("""{"v":1,"name":"H","lan":"https://a:1","pins":["$pinA"]}""")
        assertEquals(PairingException.Reason.NOT_A_LOFT_CODE, reason("https://evil.example/pair#$good"))
        assertEquals(PairingException.Reason.NOT_A_LOFT_CODE, reason("hello world"))
        assertEquals(PairingException.Reason.BAD_ENCODING, reason(payload("not json")))
        assertEquals(PairingException.Reason.BAD_ENCODING, reason(payload("[1,2]")))
        assertEquals(PairingException.Reason.MISSING_FIELD,
            reason(payload("""{"v":1,"name":" ","lan":"https://a:1","pins":["$pinA"]}""")))
    }

    @Test fun manualSetupNeedsAddressAndPin() {
        val p = Pairing.manual("gw.example", pinA)
        assertEquals("https://gw.example:8443", p.lanUrl)
        assertEquals("gw.example", p.name)
        assertEquals(listOf(pinA), p.pins)
        assertEquals("https://10.1.2.3:9443", Pairing.manual("10.1.2.3:9443", " $pinA ").lanUrl)
        assertEquals("https://gw.example", Pairing.manual("https://gw.example", pinA).lanUrl)
        assertEquals("https://[fd00::1]:8443", Pairing.manual("[fd00::1]:8443", pinA).lanUrl)
        assertEquals("Attic", Pairing.manual("gw.example", "sha256/$pinA", "Attic").name)
        for ((a, pin, why) in listOf(
            Triple("gw.example", "", PairingException.Reason.NO_PINS),
            Triple("gw.example", "nope", PairingException.Reason.BAD_PIN),
            Triple("http://gw.example:8443", pinA, PairingException.Reason.NOT_HTTPS),
            Triple("", pinA, PairingException.Reason.MISSING_FIELD),
        )) {
            try {
                Pairing.manual(a, pin)
                fail("accepted $a / $pin")
            } catch (e: PairingException) {
                assertEquals(why, e.reason)
            }
        }
    }

    @Test fun withPinsKeepsOldPinsWhenTheNewListIsUseless() {
        val p = Pairing("H", "https://a:1", listOf(pinA))
        assertEquals(listOf(pinB, pinA), p.withPins(listOf(pinB, pinA, "junk")).pins)
        assertEquals(listOf(pinA), p.withPins(listOf("junk")).pins)
        assertEquals(listOf(pinA), p.withPins(emptyList()).pins)
    }
}
