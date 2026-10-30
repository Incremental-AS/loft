package no.incremental.loft

import android.content.Intent
import android.net.Uri
import androidx.compose.ui.test.ExperimentalTestApi
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTextInput
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import no.incremental.loft.core.CommitRequest
import no.incremental.loft.core.Pairing
import no.incremental.loft.ui.MainActivity
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** The app the way a person uses it: open the pairing link, sign in, choose the folder, browse. */
@OptIn(ExperimentalTestApi::class)
@RunWith(AndroidJUnit4::class)
class AppFlowTest {
    @get:Rule val compose = createEmptyComposeRule()
    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext
    private val loft = Loft.get(ctx)

    @After fun tearDown() = loft.forgetGateway()

    @Test fun pairingLinkSignInChooseFolderBrowse() {
        // A file to look for afterwards, put there by a separate signed-in session.
        val marker = "android-ui-${System.currentTimeMillis()}.txt"
        runBlocking {
            val c = TestGateway.signedIn(loft)
            val f = File(ctx.cacheDir, "marker").apply { writeText("ui test") }
            c.uploadAndCommit(f, CommitRequest(parentId = loft.prefs.root!!.rootItemId, name = marker))
        }
        loft.forgetGateway()
        assertNull(loft.prefs.pairing)

        // The App Link: https://loft.incremental.no/pair#<payload>
        val open = Intent(Intent.ACTION_VIEW, Uri.parse(TestGateway.link())).setClass(ctx, MainActivity::class.java)
        ActivityScenario.launch<MainActivity>(open).use {
            assertEquals(Pairing.parse(TestGateway.link()), loft.prefs.pairing)

            compose.onNodeWithTag("user").performTextInput(TestGateway.user)
            compose.onNodeWithTag("password").performTextInput(TestGateway.password)
            compose.onNodeWithTag("signIn").performClick()
            compose.waitUntil(30_000) {
                compose.onAllNodes(hasTestTag("signInError")).fetchSemanticsNodes().isNotEmpty() ||
                    compose.onAllNodes(hasTestTag("folder:${TestGateway.share}")).fetchSemanticsNodes().isNotEmpty()
            }
            compose.onAllNodes(hasTestTag("signInError")).fetchSemanticsNodes().firstOrNull()?.let { n ->
                throw AssertionError("sign-in failed: ${n.config}")
            }

            compose.waitUntilAtLeastOneExists(hasTestTag("folder:${TestGateway.share}"), 30_000)
            compose.onNodeWithTag("folder:${TestGateway.share}").performClick()
            compose.waitUntilAtLeastOneExists(hasTestTag("useThis"), 10_000)
            compose.onNodeWithTag("useThis").performClick()

            // The list is lazy: the marker may be below the fold when the root holds many folders.
            compose.waitUntil(30_000) {
                runCatching { compose.onNodeWithTag("items").performScrollToNode(hasText(marker)) }.isSuccess
            }
            compose.onNodeWithText(marker).assertExists()
        }
        // Clean up the marker.
        runBlocking {
            val c = loft.requireClient()
            c.allChildren(loft.prefs.root!!.rootItemId).filter { it.name == marker }.forEach { c.delete(it.id) }
        }
    }

    @Test fun aBadPairingLinkIsRefused() {
        loft.forgetGateway()
        val bad = Intent(Intent.ACTION_VIEW, Uri.parse("https://loft.incremental.no/pair#eyJ2IjoyfQ"))
            .setClass(ctx, MainActivity::class.java)
        ActivityScenario.launch<MainActivity>(bad).use {
            assertNull(loft.prefs.pairing)
            compose.waitUntilAtLeastOneExists(hasTestTag("pairError"), 10_000)
        }
    }
}
