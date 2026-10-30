package no.incremental.loft

import android.app.Activity
import android.app.Instrumentation
import android.content.ClipData
import android.content.Intent
import android.net.Uri
import androidx.compose.ui.test.ExperimentalTestApi
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.longClick
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.Item
import no.incremental.loft.ui.MainActivity
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * The browse screen's own actions, through the UI: the upload button (the system picker's answer
 * injected with an ActivityMonitor), new folder, rename and delete; each checked on the gateway.
 */
@OptIn(ExperimentalTestApi::class)
@RunWith(AndroidJUnit4::class)
class BrowseActionsTest {
    @get:Rule val compose = createEmptyComposeRule()
    private val instr = InstrumentationRegistry.getInstrumentation()
    private val ctx = TestKit.ctx
    private val loft = Loft.get(ctx)
    private lateinit var c: GatewayClient
    private val media = mutableListOf<Uri>()
    private val folder = "browse"

    @Before fun setUp(): Unit = runBlocking { c = TestGateway.signedIn(loft) }

    @After fun tearDown() {
        TestKit.deleteMedia(media)
        loft.forgetGateway()
    }

    private fun waitOnGateway(what: String, folderId: String, check: (List<Item>) -> Boolean): List<Item> = runBlocking {
        TestKit.waitFor(what, 60_000) { c.allChildren(folderId).takeIf(check) }
    }

    private fun waitInUi(name: String) = compose.waitUntilAtLeastOneExists(hasTestTag("item:$name"), 30_000)

    private fun goneFromUi(name: String) =
        compose.waitUntil(30_000) { compose.onAllNodes(hasTestTag("item:$name")).fetchSemanticsNodes().isEmpty() }

    @Test fun uploadNewFolderRenameDelete() {
        val dir = runBlocking { TestKit.freshFolder(c, folder) }
        val stamp = System.currentTimeMillis()
        val picked = linkedMapOf(
            "picked-$stamp.jpg" to TestKit.jpeg(21),
            "picked-$stamp.pdf" to TestKit.randomBytes(700_000, 22),
        )
        val uris = picked.map { (n, b) -> TestKit.download(n, if (n.endsWith(".jpg")) "image/jpeg" else "application/pdf", b) }
            .also { media += it }

        // The system picker answers with our two documents (OpenMultipleDocuments reads the ClipData).
        val answer = Intent().apply {
            clipData = ClipData.newRawUri(null, uris[0]).apply { addItem(ClipData.Item(uris[1])) }
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        // Only the picker: an IntentFilter monitor would also catch intents without an action (the
        // test's own launch of MainActivity) and swallow them.
        val asked = mutableListOf<Intent>()
        val picker = object : Instrumentation.ActivityMonitor() {
            override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? =
                if (intent.action != Intent.ACTION_OPEN_DOCUMENT) null
                else Instrumentation.ActivityResult(Activity.RESULT_OK, answer).also { asked += intent }
        }
        instr.addMonitor(picker)

        try {
            ActivityScenario.launch<MainActivity>(Intent(ctx, MainActivity::class.java)).use { shotOnFailure("browse") {
                waitInUi(TestKit.PARENT)
                compose.onNodeWithTag("item:${TestKit.PARENT}").performClick()
                waitInUi(folder)
                compose.onNodeWithTag("item:$folder").performClick()
                compose.waitUntilAtLeastOneExists(hasText(ctx.getString(R.string.browse_empty)), 30_000)

                // Upload
                compose.onNodeWithTag("upload").performClick()
                val up = waitOnGateway("uploads", dir.id) { l -> l.map { it.name }.containsAll(picked.keys) }
                assertEquals(1, asked.size)
                assertTrue("several files at once", asked[0].getBooleanExtra(Intent.EXTRA_ALLOW_MULTIPLE, false))
                assertTrue(asked[0].hasCategory(Intent.CATEGORY_OPENABLE))
                assertEquals(picked.keys.sorted(), up.map { it.name }.sorted())
                runBlocking {
                    for (item in up) {
                        assertArrayEquals(item.name, picked.getValue(item.name), TestKit.content(c, item))
                    }
                }
                picked.keys.forEach { waitInUi(it) }

                // New folder
                compose.onNodeWithTag("newFolder").performClick()
                compose.onNodeWithTag("nameField").performTextInput("Made in the app")
                compose.onNodeWithTag("nameOk").performClick()
                waitInUi("Made in the app")
                val made = waitOnGateway("new folder", dir.id) { l -> l.any { it.name == "Made in the app" } }
                    .single { it.name == "Made in the app" }
                assertTrue(made.isDirectory)

                // Rename it
                compose.onNodeWithTag("item:Made in the app").performTouchInput { longClick() }
                compose.onNodeWithTag("menuRename").performClick()
                compose.onNodeWithTag("nameField").performTextClearance()
                compose.onNodeWithTag("nameField").performTextInput("Renamed in the app")
                compose.onNodeWithTag("nameOk").performClick()
                waitInUi("Renamed in the app")
                goneFromUi("Made in the app")
                val renamed = waitOnGateway("rename", dir.id) { l -> l.any { it.name == "Renamed in the app" } }
                    .single { it.name == "Renamed in the app" }
                assertEquals("same folder, new name", made.id, renamed.id)

                // Delete the folder and one of the uploaded files
                for (name in listOf("Renamed in the app", "picked-$stamp.pdf")) {
                    compose.onNodeWithTag("item:$name").performTouchInput { longClick() }
                    compose.onNodeWithTag("menuDelete").performClick()
                    compose.onNodeWithTag("deleteConfirm").performClick()
                    goneFromUi(name)
                }
                val left = waitOnGateway("deletes", dir.id) { l -> l.size == 1 }
                assertEquals(listOf("picked-$stamp.jpg"), left.map { it.name })
                TestKit.expectOnDisk("$folder/picked-$stamp.jpg", picked.getValue("picked-$stamp.jpg"))
                TestKit.expectGone("$folder/picked-$stamp.pdf")
                TestKit.expectGone("$folder/Made in the app")
                TestKit.expectGone("$folder/Renamed in the app")
            } }
        } finally {
            instr.removeMonitor(picker)
        }
    }
}
