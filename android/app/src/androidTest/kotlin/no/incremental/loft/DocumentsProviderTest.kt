package no.incremental.loft

import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Point
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import no.incremental.loft.core.CommitRequest
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.Item
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import kotlin.random.Random

/** The provider as the Files app and the system picker see it: through DocumentsContract. */
@RunWith(AndroidJUnit4::class)
class DocumentsProviderTest {
    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext
    private val resolver = ctx.contentResolver
    private val loft = Loft.get(ctx)
    private val auth = Loft.AUTHORITY
    private lateinit var client: GatewayClient
    private lateinit var dirId: String

    @Before fun setUp() = runBlocking<Unit> {
        client = TestGateway.signedIn(loft)
        dirId = client.mkdir(loft.prefs.root!!.rootItemId, "android-provider-${System.currentTimeMillis()}").id
    }

    @After fun tearDown() = runBlocking<Unit> {
        runCatching { client.delete(dirId) }
        loft.forgetGateway()
    }

    private fun children(parent: String): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        resolver.query(DocumentsContract.buildChildDocumentsUri(auth, parent), null, null, null, null)!!.use { c ->
            val id = c.getColumnIndexOrThrow(Document.COLUMN_DOCUMENT_ID)
            val name = c.getColumnIndexOrThrow(Document.COLUMN_DISPLAY_NAME)
            while (c.moveToNext()) out += c.getString(id) to c.getString(name)
        }
        return out
    }

    private fun waitFor(timeoutMs: Long = 60_000, what: String, cond: suspend () -> Boolean) = runBlocking {
        val end = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < end) {
            if (runCatching { cond() }.getOrDefault(false)) return@runBlocking
            Thread.sleep(500)
        }
        throw AssertionError("timed out waiting for $what")
    }

    @Test fun rootsShowTheLoftFolder() {
        resolver.query(DocumentsContract.buildRootsUri(auth), null, null, null, null)!!.use { c ->
            assertTrue(c.moveToFirst())
            assertEquals(loft.prefs.root!!.rootItemId, c.getString(c.getColumnIndexOrThrow(DocumentsContract.Root.COLUMN_DOCUMENT_ID)))
            assertEquals("Test gateway", c.getString(c.getColumnIndexOrThrow(DocumentsContract.Root.COLUMN_SUMMARY)))
        }
    }

    @Test fun createWriteReadRenameMoveDelete() {
        val parent = DocumentsContract.buildDocumentUri(auth, dirId)
        val sub = DocumentsContract.createDocument(resolver, parent, Document.MIME_TYPE_DIR, "Sub")!!
        val file = DocumentsContract.createDocument(resolver, parent, "text/plain", "notes")!!
        val fileId = DocumentsContract.getDocumentId(file)
        assertTrue(children(dirId).contains(fileId to "notes.txt"))

        // Write: the provider hands out a private copy and uploads it when it is closed.
        val text = "Hello from the Android provider test ${System.nanoTime()}\n"
        resolver.openOutputStream(file, "wt")!!.use { it.write(text.toByteArray()) }
        waitFor(what = "upload on close") { client.item(fileId).size == text.length.toLong() }

        resolver.openInputStream(file)!!.use { assertEquals(text, it.readBytes().decodeToString()) }

        // Append mode ("wa") downloads the current content first.
        resolver.openOutputStream(file, "wa")!!.use { it.write("more\n".toByteArray()) }
        waitFor(what = "append upload") { client.item(fileId).size == (text.length + 5).toLong() }
        resolver.openInputStream(file)!!.use { assertEquals(text + "more\n", it.readBytes().decodeToString()) }

        val renamed = DocumentsContract.renameDocument(resolver, file, "renamed.txt")
        assertTrue(renamed == null || DocumentsContract.getDocumentId(renamed) == fileId)
        assertEquals("renamed.txt", runBlocking { client.item(fileId) }.name)

        val moved = DocumentsContract.moveDocument(resolver, file, parent, sub)!!
        assertEquals(fileId, DocumentsContract.getDocumentId(moved))
        assertTrue(children(DocumentsContract.getDocumentId(sub)).any { it.first == fileId })
        // Tree access (ACTION_OPEN_DOCUMENT_TREE) checks isChildDocument: inside the tree works…
        val subTree = DocumentsContract.buildTreeDocumentUri(auth, DocumentsContract.getDocumentId(sub))
        resolver.query(DocumentsContract.buildDocumentUriUsingTree(subTree, fileId), null, null, null, null)!!.use {
            assertTrue(it.moveToFirst())
        }
        // …and a document outside it is refused.
        try {
            resolver.query(DocumentsContract.buildDocumentUriUsingTree(subTree, dirId), null, null, null, null)?.close()
            throw AssertionError("tree access outside the tree")
        } catch (e: SecurityException) {
        }

        assertTrue(DocumentsContract.deleteDocument(resolver, moved))
        assertFalse(children(DocumentsContract.getDocumentId(sub)).any { it.first == fileId })
    }

    @Test fun bigFilesStreamThroughAProxyDescriptor() = runBlocking<Unit> {
        // Larger than SMALL_FILE, so the read goes through Range requests, not a download.
        val data = Random(7).nextBytes((9 shl 20) + 4321)
        val src = File(ctx.cacheDir, "big").apply { writeBytes(data) }
        client.chunkLimit = 4L shl 20
        val it: Item = client.uploadAndCommit(src, CommitRequest(parentId = dirId, name = "big.bin")).item
        val uri = DocumentsContract.buildDocumentUri(auth, it.id)
        val got = resolver.openInputStream(uri)!!.use { s -> s.readBytes() }
        assertEquals(data.size, got.size)
        assertArrayEquals(MessageDigest.getInstance("SHA-256").digest(data), MessageDigest.getInstance("SHA-256").digest(got))
        // Random access, as a video player seeks.
        resolver.openFileDescriptor(uri, "r")!!.use { pfd ->
            val ch = java.io.FileInputStream(pfd.fileDescriptor).channel
            val buf = java.nio.ByteBuffer.allocate(4096)
            ch.read(buf, 5_000_000L)
            assertArrayEquals(data.copyOfRange(5_000_000, 5_004_096), buf.array())
        }
    }

    @Test fun thumbnailsComeFromTheGateway() = runBlocking<Unit> {
        val bmp = Bitmap.createBitmap(64, 48, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.rgb(181, 84, 26)) }
        val png = ByteArrayOutputStream().also { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }.toByteArray()
        val src = File(ctx.cacheDir, "thumb.png").apply { writeBytes(png) }
        val it = client.uploadAndCommit(src, CommitRequest(parentId = dirId, name = "rust.png")).item
        children(dirId)                                    // as the Files app lists before it asks
        val thumb = DocumentsContract.getDocumentThumbnail(resolver,
            DocumentsContract.buildDocumentUri(auth, it.id), Point(96, 96), null)
        assertNotNull(thumb)
        assertTrue(thumb!!.width in 1..96 && thumb.height in 1..96)
    }

    @Test fun recentsListOpenedFiles() = runBlocking<Unit> {
        val src = File(ctx.cacheDir, "recent.txt").apply { writeText("recent") }
        val it = client.uploadAndCommit(src, CommitRequest(parentId = dirId, name = "recent.txt")).item
        resolver.openInputStream(DocumentsContract.buildDocumentUri(auth, it.id))!!.use { s -> s.readBytes() }
        val recents: Uri = DocumentsContract.buildRecentDocumentsUri(auth, "loft")
        resolver.query(recents, null, null, null, null)!!.use { c ->
            assertTrue(c.moveToFirst())
            assertEquals(it.id, c.getString(c.getColumnIndexOrThrow(Document.COLUMN_DOCUMENT_ID)))
        }
    }
}
