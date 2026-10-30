// The pairing payload (docs/pairing.md): which gateway this device talks to,
// and the SPKI pins that make the very first connection authenticated.
package no.incremental.loft.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import java.net.URI
import java.util.Base64

/** How the outside address reaches the gateway (docs/pairing.md, `mode`). */
enum class PublicMode(val wire: String) {
    /** A TLS-ending proxy: normal web PKI, device tokens only, never a password. */
    TUNNEL("tunnel"),
    /** A port forward to the direct listener: checked against the pins, sign-in allowed. */
    DIRECT("direct");

    companion object {
        fun of(s: String): PublicMode? = entries.firstOrNull { it.wire == s }
    }
}

class PairingException(val reason: Reason, detail: String? = null) :
    Exception(detail?.let { "$reason: $it" } ?: reason.name) {
    enum class Reason {
        NOT_A_LOFT_CODE,     // not a pairing link or payload at all
        BAD_ENCODING,        // not base64url JSON
        UNKNOWN_VERSION,     // v is missing or not 1
        MISSING_FIELD,       // name / lan / pins missing or of the wrong type
        NOT_HTTPS,           // lan or pub is not an https:// URL with a host
        NO_PINS,             // pins empty
        BAD_PIN,             // a pin is not base64 of 32 bytes
        BAD_MODE,            // pub without a known mode, or mode without pub
    }
}

data class Pairing(
    val name: String,
    /** `https://host:port` of the direct listener, no trailing slash. */
    val lanUrl: String,
    /** base64 SHA-256 of the direct key's SubjectPublicKeyInfo, live first. */
    val pins: List<String>,
    val publicUrl: String? = null,
    val publicMode: PublicMode? = null,
) {
    /** The compact JSON the payload carries; also how the app stores the pairing. */
    fun toJson(): String = buildJsonObject {
        put("v", VERSION)
        put("name", name)
        put("lan", lanUrl)
        putJsonArray("pins") { pins.forEach { add(JsonPrimitive(it)) } }
        if (publicUrl != null && publicMode != null) {
            put("pub", publicUrl)
            put("mode", publicMode.wire)
        }
    }.toString()

    fun toPayload(): String =
        Base64.getUrlEncoder().withoutPadding().encodeToString(toJson().toByteArray(Charsets.UTF_8))

    fun toLink(): String = LINK_PREFIX + toPayload()

    /** Same gateway, newer pins (from GET /v1/identity over a trusted route). */
    fun withPins(newPins: List<String>): Pairing {
        val clean = newPins.filter { isValidPin(it) }.distinct()
        return if (clean.isEmpty()) this else copy(pins = clean)
    }

    companion object {
        const val VERSION = 1
        const val LINK_HOST = "loft.incremental.no"
        const val LINK_PREFIX = "https://$LINK_HOST/pair#"

        /** Default port of the direct listener, used when "Set up manually" gets a bare host. */
        const val DEFAULT_DIRECT_PORT = 8443

        private val PAYLOAD_RE = Regex("^[A-Za-z0-9_-]+={0,2}$")

        /**
         * A scanned QR code, an opened app link or a pasted text: the link
         * `https://loft.incremental.no/pair#<payload>`, `loft://pair#<payload>`, or the bare payload.
         */
        fun parse(input: String): Pairing = fromPayload(extractPayload(input))

        fun extractPayload(input: String): String {
            val text = input.trim()
            val hash = text.indexOf('#')
            if (hash < 0) {
                if (PAYLOAD_RE.matches(text)) return text
                throw PairingException(PairingException.Reason.NOT_A_LOFT_CODE)
            }
            val head = text.substring(0, hash).trimEnd('/').lowercase()
            if (head != "https://$LINK_HOST/pair" && head != "loft://pair") {
                throw PairingException(PairingException.Reason.NOT_A_LOFT_CODE)
            }
            val payload = text.substring(hash + 1)
            if (!PAYLOAD_RE.matches(payload)) throw PairingException(PairingException.Reason.BAD_ENCODING)
            return payload
        }

        fun fromPayload(payload: String): Pairing {
            val raw = try {
                Base64.getUrlDecoder().decode(payload.trimEnd('='))
            } catch (e: IllegalArgumentException) {
                throw PairingException(PairingException.Reason.BAD_ENCODING)
            }
            return fromJson(raw.toString(Charsets.UTF_8))
        }

        /** Validates exactly as docs/pairing.md says: v 1, https URLs, pins, mode only with pub. */
        fun fromJson(json: String): Pairing {
            val obj = try {
                Json.parseToJsonElement(json) as? JsonObject
            } catch (e: Exception) {
                null
            } ?: throw PairingException(PairingException.Reason.BAD_ENCODING)

            val v = (obj["v"] as? JsonPrimitive)?.takeIf { !it.isString }?.intOrNull
            if (v != VERSION) throw PairingException(PairingException.Reason.UNKNOWN_VERSION, "v=${obj["v"]}")

            val name = obj.string("name")?.trim()
            if (name.isNullOrEmpty()) throw PairingException(PairingException.Reason.MISSING_FIELD, "name")
            val lan = obj.string("lan") ?: throw PairingException(PairingException.Reason.MISSING_FIELD, "lan")
            val lanUrl = httpsOrigin(lan)

            val pinsEl = obj["pins"] as? JsonArray
                ?: throw PairingException(PairingException.Reason.MISSING_FIELD, "pins")
            val pins = pinsEl.map {
                val s = (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content
                    ?: throw PairingException(PairingException.Reason.BAD_PIN)
                if (!isValidPin(s)) throw PairingException(PairingException.Reason.BAD_PIN)
                s
            }.distinct()
            if (pins.isEmpty()) throw PairingException(PairingException.Reason.NO_PINS)

            val pub = obj.string("pub")
            val modeRaw = obj.string("mode")
            if (obj.containsKey("pub") && pub == null) throw PairingException(PairingException.Reason.NOT_HTTPS, "pub")
            if (obj.containsKey("mode") && modeRaw == null) throw PairingException(PairingException.Reason.BAD_MODE)
            var publicUrl: String? = null
            var mode: PublicMode? = null
            if (pub != null || modeRaw != null) {
                if (pub == null || modeRaw == null) throw PairingException(PairingException.Reason.BAD_MODE)
                publicUrl = httpsOrigin(pub)
                mode = PublicMode.of(modeRaw) ?: throw PairingException(PairingException.Reason.BAD_MODE, modeRaw)
            }
            return Pairing(name, lanUrl, pins, publicUrl, mode)
        }

        /**
         * "Set up manually": the address of the direct listener **and** a pin. There is no way
         * to pair with an address alone (docs/pairing.md: no plain "type the address" field).
         * `address`: `host`, `host:port` or `https://host[:port]`.
         */
        fun manual(address: String, pin: String, name: String? = null): Pairing {
            val a = address.trim()
            if (a.isEmpty()) throw PairingException(PairingException.Reason.MISSING_FIELD, "address")
            val url = when {
                a.contains("://") -> a
                hasPort(a) -> "https://$a"
                else -> "https://$a:$DEFAULT_DIRECT_PORT"
            }
            val lanUrl = httpsOrigin(url)
            val p = pin.trim().removePrefix("sha256/")
            if (p.isEmpty()) throw PairingException(PairingException.Reason.NO_PINS)
            if (!isValidPin(p)) throw PairingException(PairingException.Reason.BAD_PIN)
            val host = URI(lanUrl).host.trim('[', ']')
            return Pairing(name?.trim()?.takeIf { it.isNotEmpty() } ?: host, lanUrl, listOf(p))
        }

        private fun hasPort(a: String): Boolean {
            if (a.startsWith("[")) return a.contains("]:")       // [v6]:port
            return a.count { it == ':' } == 1                      // host:port (a bare v6 has many)
        }

        fun isValidPin(s: String): Boolean = try {
            s.length == 44 && Base64.getDecoder().decode(s).size == 32
        } catch (e: IllegalArgumentException) {
            false
        }

        /** `https://host[:port]` with nothing else (no user, path, query or fragment). */
        fun httpsOrigin(s: String): String {
            val u = try {
                URI(s.trim())
            } catch (e: Exception) {
                throw PairingException(PairingException.Reason.NOT_HTTPS, s)
            }
            if (!u.scheme.equals("https", ignoreCase = true) || u.host.isNullOrEmpty() ||
                u.rawUserInfo != null || u.rawQuery != null || u.rawFragment != null ||
                !(u.rawPath.isNullOrEmpty() || u.rawPath == "/")
            ) {
                throw PairingException(PairingException.Reason.NOT_HTTPS, s)
            }
            if (u.port == 0 || u.port > 65535) throw PairingException(PairingException.Reason.NOT_HTTPS, s)
            val port = if (u.port == -1) "" else ":${u.port}"
            return "https://${u.host.lowercase()}$port"
        }

        private fun JsonObject.string(key: String): String? =
            (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull
    }
}
