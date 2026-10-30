// The QR decoder behind the scan screen: ZXing on the luminance (Y) plane of a camera frame.
// No Google Play services, since Loft ships outside Google Play. Kept apart from CameraX so the
// exact code the camera uses can be tested with a generated QR code.
package no.incremental.loft.ui

import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.ReaderException
import com.google.zxing.common.HybridBinarizer
import java.nio.ByteBuffer

class QrDecoder {
    private val reader = MultiFormatReader().apply {
        setHints(mapOf(DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE)))
    }

    /**
     * The text of a QR code in a `width` x `height` luminance plane whose rows start every
     * `rowStride` bytes (a YUV_420_888 frame's plane 0; the stride may be wider than the image,
     * and the last row may stop at the image width). null = no readable code in this frame.
     */
    fun decode(plane: ByteBuffer, rowStride: Int, width: Int, height: Int): String? {
        val data = ByteArray(width * height)
        val buf = plane.duplicate()
        buf.rewind()
        for (row in 0 until height) {
            val start = row * rowStride
            if (start >= buf.limit()) break
            buf.position(start)
            buf.get(data, row * width, minOf(width, buf.remaining()))
        }
        val source = PlanarYUVLuminanceSource(data, width, height, 0, 0, width, height, false)
        return try {
            reader.decodeWithState(BinaryBitmap(HybridBinarizer(source))).text
        } catch (e: ReaderException) {
            null
        } catch (e: RuntimeException) {
            null
        } finally {
            reader.reset()
        }
    }
}
