package no.incremental.loft

import android.content.ClipData
import android.content.Intent
import android.net.Uri
import androidx.compose.ui.test.ExperimentalTestApi
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.ui.ShareActivity
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * "Save to Loft" from another app's share sheet: ACTION_SEND and ACTION_SEND_MULTIPLE with
 * content:// URIs from MediaStore, a folder chosen in Loft's chooser, and the files on the gateway.
 */
@OptIn(ExperimentalTestApi::class)
@RunWith(AndroidJUnit4::class)
class ShareTargetTest {
    @get:Rule val compose = createEmptyComposeRule()
    private val ctx = TestKit.ctx
    private val loft = Loft.get(ctx)
    private lateinit var c: GatewayClient
    private val media = mutableListOf<Uri>()

    @Before fun setUp(): Unit = runBlocking { c = TestGateway.signedIn(loft) }

    @After fun tearDown() {
        TestKit.deleteMedia(media)
        loft.forgetGateway()
    }

    /** Opens the share sheet target with `intent`, picks `folder` in the chooser and saves there. */
    private fun shareInto(intent: Intent, folder: String) {
        ActivityScenario.launch<ShareActivity>(intent.setClass(ctx, ShareActivity::class.java)).use { s -> shotOnFailure("share-$folder") {
            for (step in listOf(TestKit.PARENT, folder)) {
                compose.waitUntilAtLeastOneExists(hasTestTag("chooseFolder:$step"), 30_000)
                compose.onNodeWithTag("chooseFolder:$step").performClick()
                // In the folder: its own list loads.
                compose.waitUntil(10_000) { compose.onAllNodes(hasTestTag("chooseFolder:$step")).fetchSemanticsNodes().isEmpty() }
            }
            // The folder is empty (no subfolders), the button saves here.
            compose.waitUntil(10_000) { compose.onAllNodes(hasTestTag("chooseFolder:${TestKit.PARENT}")).fetchSemanticsNodes().isEmpty() }
            compose.onNodeWithTag("chooseHere").performClick()
            // The activity stages the files, queues the uploads and closes itself.
            val end = System.currentTimeMillis() + 15_000
            while (s.state != Lifecycle.State.DESTROYED && System.currentTimeMillis() < end) Thread.sleep(200)
            assertEquals(Lifecycle.State.DESTROYED, s.state)
        } }
    }

    private fun checkArrived(folderId: String, folder: String, expected: Map<String, ByteArray>) = runBlocking {
        val got = TestKit.waitFor("${expected.keys} in $folder", 90_000) {
            TestKit.files(c, folderId).takeIf { l -> l.map { it.name }.containsAll(expected.keys) }
        }
        assertEquals("exactly the shared files, once", expected.keys.sorted(), got.map { it.name }.sorted())
        for (item in got) {
            val want = expected.getValue(item.name)
            assertEquals(item.name, want.size.toLong(), item.size)
            assertArrayEquals(item.name, want, TestKit.content(c, item))
            TestKit.expectOnDisk("$folder/${item.name}", want)
        }
    }

    @Test fun sendOneFile() {
        val folder = "share-one"
        val dir = runBlocking { TestKit.freshFolder(c, folder) }
        val stamp = System.currentTimeMillis()
        val name = "shared-note-$stamp.txt"
        val bytes = "Shared from another app at $stamp\n".repeat(50).toByteArray()
        val uri = TestKit.download(name, "text/plain", bytes).also { media += it }

        shareInto(Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, uri)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), folder)
        checkArrived(dir.id, folder, mapOf(name to bytes))
    }

    @Test fun sendMultipleFiles() {
        val folder = "share-many"
        val dir = runBlocking { TestKit.freshFolder(c, folder) }
        val stamp = System.currentTimeMillis()
        val files = linkedMapOf(
            "photo-$stamp.jpg" to ("image/jpeg" to TestKit.jpeg(11)),
            "data-$stamp.bin" to ("application/octet-stream" to TestKit.randomBytes(5 * 1024 * 1024 + 123, 12)),
            "clip-$stamp.mp4" to ("video/mp4" to TestKit.mp4()),
        )
        val uris = ArrayList(files.map { (n, v) -> TestKit.download(n, v.first, v.second) }).also { media += it }
        val send = Intent(Intent.ACTION_SEND_MULTIPLE).setType("*/*")
            .putParcelableArrayListExtra(Intent.EXTRA_STREAM, uris)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        send.clipData = ClipData.newRawUri(null, uris[0]).apply { uris.drop(1).forEach { addItem(ClipData.Item(it)) } }

        shareInto(send, folder)
        checkArrived(dir.id, folder, files.mapValues { it.value.second })
    }
}
