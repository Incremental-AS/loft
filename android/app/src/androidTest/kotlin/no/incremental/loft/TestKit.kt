// Helpers for the instrumented tests: shell commands through UiAutomation (the shell user, so
// files it writes belong to someone else than Loft, as a camera app's do), runtime permissions,
// MediaStore files, and checks against the dev gateway.
package no.incremental.loft

import android.content.ContentValues
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.MediaStore
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.Item
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import kotlin.random.Random

object TestKit {
    private val instr get() = InstrumentationRegistry.getInstrumentation()
    val ctx get() = instr.targetContext
    private const val TAG = "LoftIT"

    /** A shell command as the shell user; returns its output. Arguments are split on spaces. */
    fun shell(cmd: String): String {
        val pfd = instr.uiAutomation.executeShellCommand(cmd)
        return ParcelFileDescriptor.AutoCloseInputStream(pfd).use { it.readBytes().decodeToString() }
    }

    /** Writes `bytes` to `path` as the shell user (`tee`, so no shell redirection is needed). */
    fun shellWrite(path: String, bytes: ByteArray) {
        val fds = instr.uiAutomation.executeShellCommandRw("tee $path")
        val out = fds[0]
        val drain = Thread { ParcelFileDescriptor.AutoCloseInputStream(out).use { it.readBytes() } }.apply { start() }
        ParcelFileDescriptor.AutoCloseOutputStream(fds[1]).use { it.write(bytes) }
        drain.join(30_000)
    }

    fun grant(vararg perms: String) = perms.forEach { instr.uiAutomation.grantRuntimePermission(ctx.packageName, it) }

    fun sha256(b: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

    fun randomBytes(n: Int, seed: Int): ByteArray = Random(seed).nextBytes(n)

    /** A real JPEG: a coloured pattern with some noise, so every seed gives other bytes. */
    fun jpeg(seed: Int, w: Int = 640, h: Int = 480): ByteArray {
        val rnd = Random(seed)
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        c.drawColor(Color.rgb(rnd.nextInt(256), rnd.nextInt(256), rnd.nextInt(256)))
        val p = Paint()
        repeat(40) {
            p.color = Color.rgb(rnd.nextInt(256), rnd.nextInt(256), rnd.nextInt(256))
            c.drawCircle(rnd.nextFloat() * w, rnd.nextFloat() * h, 10f + rnd.nextFloat() * 60f, p)
        }
        return ByteArrayOutputStream().also { bmp.compress(Bitmap.CompressFormat.JPEG, 85, it) }.toByteArray()
    }

    /** A real 2 s H.264 MP4 (ffmpeg testsrc), from the test APK's assets. */
    fun mp4(): ByteArray = instr.context.assets.open("clip.mp4").use { it.readBytes() }

    /** A file in Downloads through MediaStore: a real content:// URI owned by this app. */
    fun download(name: String, mime: String, bytes: ByteArray): Uri {
        val r = ctx.contentResolver
        val uri = r.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, name)
            put(MediaStore.MediaColumns.MIME_TYPE, mime)
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        })!!
        r.openOutputStream(uri)!!.use { it.write(bytes) }
        r.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
        return uri
    }

    fun deleteMedia(uris: List<Uri>) = uris.forEach { runCatching { ctx.contentResolver.delete(it, null, null) } }

    /** Every test folder lives in this one folder in the Loft root, so the root stays short. */
    const val PARENT = "android-it"

    /**
     * A folder in [PARENT], emptied first (a previous run leaves its files for the disk check).
     * The UI tests open [PARENT] first, then this folder.
     */
    suspend fun freshFolder(c: GatewayClient, name: String): Item {
        val root = Loft.get(ctx).prefs.root!!.rootItemId
        val parent = c.allChildren(root).firstOrNull { it.name == PARENT && it.isDirectory } ?: c.mkdir(root, PARENT)
        c.allChildren(parent.id).filter { it.name == name }.forEach { c.delete(it.id) }
        return c.mkdir(parent.id, name)
    }

    suspend fun files(c: GatewayClient, folderId: String): List<Item> = c.allChildren(folderId).filter { !it.isDirectory }

    suspend fun content(c: GatewayClient, item: Item): ByteArray {
        val f = File(ctx.cacheDir, "check-${item.id}")
        try {
            c.download(item.id, f)
            return f.readBytes()
        } finally {
            f.delete()
        }
    }

    /** Polls `check` until it returns non-null or the time is up. */
    suspend fun <T> waitFor(what: String, timeoutMs: Long = 60_000, everyMs: Long = 1_000, check: suspend () -> T?): T {
        val end = System.currentTimeMillis() + timeoutMs
        while (true) {
            check()?.let { return it }
            if (System.currentTimeMillis() > end) throw AssertionError("timed out waiting for $what")
            delay(everyMs)
        }
    }

    /**
     * One line per file for the host-side check that the bytes are on the dev server's disk:
     * `adb logcat -s LoftIT` -> `disk <sha256> android-it/<path>`, path under the share (android/scripts/check-disk.sh).
     */
    fun expectOnDisk(path: String, bytes: ByteArray) = Log.i(TAG, "disk ${sha256(bytes)} $PARENT/$path")

    /** `gone <path>`: must not be on the disk any more (deleted or renamed away). */
    fun expectGone(path: String) = Log.i(TAG, "gone $PARENT/$path")

    fun log(msg: String) = Log.i(TAG, msg)

    /** The screen as a PNG in the app's external files (`adb pull /sdcard/Android/data/<app>/files/shots`). */
    fun screenshot(name: String) {
        val bmp = instr.uiAutomation.takeScreenshot() ?: return
        val dir = File(ctx.getExternalFilesDir(null), "shots").apply { mkdirs() }
        File(dir, "$name.png").outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }
}

/** Runs `block`; when it fails, a screenshot of the moment goes to `shots/<name>.png` first. */
inline fun <T> shotOnFailure(name: String, block: () -> T): T = try {
    block()
} catch (e: Throwable) {
    runCatching { TestKit.screenshot(name) }
    throw e
}
