package no.incremental.loft.ui

import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import no.incremental.loft.core.Pairing
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.Base64
import kotlin.random.Random

/**
 * The scan screen's decoder, fed camera-like frames: a generated QR code of a real pairing link
 * on a grey, noisy background in a Y plane whose rows are padded (rowStride > width) and whose
 * last row stops at the image width, as YUV_420_888 planes from CameraX do.
 */
class QrDecoderTest {
    private val pin = Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-256").digest("test key".toByteArray()))
    private val link = Pairing("Test gateway", "https://192.0.2.10:8443", listOf(pin)).toLink()

    /** A `w` x `h` frame with `stride`-byte rows; the QR code (if any) drawn at `qrSize` px, centred. */
    private fun frame(w: Int, h: Int, stride: Int, text: String?, qrSize: Int, seed: Int = 1): ByteBuffer {
        val rnd = Random(seed)
        val bytes = ByteArray(stride * (h - 1) + w)
        for (y in 0 until h) for (x in 0 until w) bytes[y * stride + x] = (100 + rnd.nextInt(40)).toByte()
        if (text != null) {
            val m = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, qrSize, qrSize, mapOf(EncodeHintType.MARGIN to 4))
            val ox = (w - m.width) / 2
            val oy = (h - m.height) / 2
            for (y in 0 until m.height) for (x in 0 until m.width) {
                val v = if (m.get(x, y)) 20 + rnd.nextInt(30) else 215 + rnd.nextInt(30)
                bytes[(oy + y) * stride + ox + x] = v.toByte()
            }
        }
        // Junk in the row padding, which must not be read as image.
        for (y in 0 until h - 1) for (x in w until stride) bytes[y * stride + x] = 0
        return ByteBuffer.wrap(bytes)
    }

    @Test fun decodesAPairingLinkFromAPaddedFrame() {
        val buf = frame(640, 480, 704, link, 360)
        val text = QrDecoder().decode(buf, 704, 640, 480)
        assertEquals(link, text)
        val p = Pairing.parse(text!!)
        assertEquals("https://192.0.2.10:8443", p.lanUrl)
        assertEquals(listOf(pin), p.pins)
    }

    @Test fun decodesAFrameWithoutPadding() {
        assertEquals(link, QrDecoder().decode(frame(480, 640, 480, link, 400), 480, 480, 640))
    }

    @Test fun aFrameWithoutACodeGivesNull() {
        assertNull(QrDecoder().decode(frame(640, 480, 704, null, 0), 704, 640, 480))
    }

    @Test fun theSameDecoderReadsFrameAfterFrame() {
        val d = QrDecoder()
        assertNull(d.decode(frame(640, 480, 640, null, 0), 640, 640, 480))
        assertEquals(link, d.decode(frame(640, 480, 640, link, 300, seed = 2), 640, 640, 480))
        assertEquals(link, d.decode(frame(640, 480, 640, link, 300, seed = 3), 640, 640, 480))
    }
}
