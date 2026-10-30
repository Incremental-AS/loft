// Streams a big file to whatever app opened it: a proxy file descriptor whose reads become
// HTTP Range requests, so a video starts playing (and seeks) without downloading it first.
package no.incremental.loft.provider

import android.os.HandlerThread
import android.os.ProxyFileDescriptorCallback
import android.system.ErrnoException
import android.system.OsConstants
import android.util.Log
import kotlinx.coroutines.runBlocking
import no.incremental.loft.Loft
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.Item

class RangeReader(
    private val client: GatewayClient,
    private val item: Item,
    private val thread: HandlerThread,
) : ProxyFileDescriptorCallback() {
    private val blocks = object : LinkedHashMap<Long, ByteArray>(16, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<Long, ByteArray>?) = size > MAX_BLOCKS
    }
    private var etag: String? = null

    override fun onGetSize(): Long = item.size

    override fun onRead(offset: Long, size: Int, data: ByteArray): Int {
        var done = 0
        while (done < size && offset + done < item.size) {
            val pos = offset + done
            val index = pos / BLOCK
            val block = block(index)
            val inBlock = (pos - index * BLOCK).toInt()
            val n = minOf(size - done, block.size - inBlock)
            if (n <= 0) break
            System.arraycopy(block, inBlock, data, done, n)
            done += n
        }
        return done
    }

    private fun block(index: Long): ByteArray {
        blocks[index]?.let { return it }
        val r = try {
            runBlocking { client.readRange(item.id, index * BLOCK, BLOCK.toLong()) }
        } catch (e: Exception) {
            Log.w(Loft.TAG, "range read ${item.id}@$index: $e")
            throw ErrnoException("onRead", OsConstants.EIO)
        }
        if (etag == null) etag = r.etag
        // The file changed on the NAS while it was open: refuse to mix two versions.
        if (r.etag != etag || r.start != index * BLOCK) throw ErrnoException("onRead", OsConstants.EIO)
        blocks[index] = r.data
        return r.data
    }

    override fun onRelease() {
        blocks.clear()
        thread.quitSafely()
    }

    companion object {
        const val BLOCK = 1 shl 20          // 1 MiB per request
        const val MAX_BLOCKS = 16
    }
}
