package no.incremental.loft

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import no.incremental.loft.core.CommitRequest
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.GatewayException
import no.incremental.loft.core.HttpClients
import no.incremental.loft.core.MemoryTokenStore
import no.incremental.loft.core.Pairing
import no.incremental.loft.core.PinSource
import no.incremental.loft.core.Route
import no.incremental.loft.core.RouteSelector
import no.incremental.loft.data.KeystoreTokenStore
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.Base64
import kotlin.random.Random

/** Pair, sign in, browse, upload and download against the real dev gateway, with real TLS pinning. */
@RunWith(AndroidJUnit4::class)
class GatewayE2ETest {
    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext
    private val loft = Loft.get(ctx)

    @After fun tearDown() = loft.forgetGateway()

    @Test fun pinnedProbeAndWrongPin() = runBlocking<Unit> {
        val good = Pairing.parse(TestGateway.link())
        val base = HttpClients.base()
        val c = GatewayClient(RouteSelector(good), MemoryTokenStore(), HttpClients.pinned(base, PinSource { good.pins.toSet() }), base)
        assertTrue("dev server answers with the pinned key", c.probe(c.selector.home))
        assertEquals(Route.HOME, c.reprobe())

        // A look-alike: right address, wrong pin. The handshake fails; no password is sent.
        val fake = Pairing("Fake", good.lanUrl, listOf(Base64.getEncoder().encodeToString(ByteArray(32) { 9 })))
        val f = GatewayClient(RouteSelector(fake), MemoryTokenStore(), HttpClients.pinned(base, PinSource { fake.pins.toSet() }), base)
        assertFalse(f.probe(f.selector.home))
        assertNull(f.signInEndpoint())
        try {
            f.login(f.selector.home, TestGateway.user, TestGateway.password, "x")
            fail("login over an unpinned connection")
        } catch (e: GatewayException.Unreachable) {
        }
    }

    @Test fun signInStoresTheTokenInTheKeystore() = runBlocking<Unit> {
        loft.forgetGateway()
        loft.pair(Pairing.parse(TestGateway.link()))
        val c = loft.requireClient()
        val ep = c.signInEndpoint()
        assertNotNull(ep)
        try {
            c.login(ep!!, TestGateway.user, "wrong-" + TestGateway.password, "Android test")
            fail("wrong password accepted")
        } catch (e: GatewayException.Api) {
            assertEquals("invalid_credentials", e.error.error)
        }
        val r = c.login(ep!!, TestGateway.user, TestGateway.password, "Android test")
        loft.tokens.save(no.incremental.loft.core.StoredToken.of(r))
        // A fresh store instance decrypts what the first one wrote, with the Keystore key.
        assertEquals(r.token, KeystoreTokenStore(ctx).load()!!.token)
        // The plaintext token is not in the preferences file.
        val prefsDir = File(ctx.applicationInfo.dataDir, "shared_prefs")
        prefsDir.listFiles()?.forEach { assertFalse(it.name, it.readText().contains(r.token)) }
        assertEquals(TestGateway.user, c.me().nasUser)
        val rotated = c.rotateToken()
        assertEquals(rotated.token, KeystoreTokenStore(ctx).load()!!.token)
        assertEquals(TestGateway.user, c.me().nasUser)
    }

    @Test fun browseUploadDownload() = runBlocking<Unit> {
        val c = TestGateway.signedIn(loft)
        assertTrue(c.shares().contains(TestGateway.share))
        val root = loft.prefs.root!!
        val dir = c.mkdir(root.rootItemId, "android-e2e-${System.currentTimeMillis()}")
        assertTrue(c.allChildren(root.rootItemId).any { it.id == dir.id })

        // 3 MB in 1 MB chunks: the resumable path, not one request.
        c.chunkLimit = 1L shl 20
        c.downloadSlice = 1L shl 20
        val data = Random(42).nextBytes(3 * (1 shl 20) + 123)
        val src = File(ctx.cacheDir, "e2e-src").apply { writeBytes(data) }
        val up = c.uploadAndCommit(src, CommitRequest(parentId = dir.id, name = "random.bin"), loft.prefs.resumeStore)
        assertEquals("created", up.result)
        assertEquals(data.size.toLong(), up.item.size)

        val dst = File(ctx.cacheDir, "e2e-dst")
        val tag = c.download(up.item.id, dst)
        assertEquals(up.item.contentEtag, tag)
        assertArrayEquals(data, dst.readBytes())
        val r = c.readRange(up.item.id, 1_000_000, 1000)
        assertArrayEquals(data.copyOfRange(1_000_000, 1_001_000), r.data)

        // Modify with the right etag, then a stale etag keeps both (conflict copy).
        val v2 = File(ctx.cacheDir, "e2e-v2").apply { writeBytes("second".toByteArray()) }
        val mod = c.uploadAndCommit(v2, CommitRequest(itemId = up.item.id, ifMatch = up.item.contentEtag))
        assertEquals("modified", mod.result)
        val stale = c.uploadAndCommit(v2, CommitRequest(itemId = up.item.id, ifMatch = up.item.contentEtag))
        assertEquals("conflict", stale.result)

        val renamed = c.patch(up.item.id, name = "renamed.bin")
        assertEquals("renamed.bin", renamed.name)
        assertEquals("trashed", c.delete(renamed.id).result)
        assertFalse(c.allChildren(dir.id).any { it.id == renamed.id })
        c.delete(dir.id)
    }

    @Test fun changeFeedSeesAnUpload() = runBlocking<Unit> {
        val c = TestGateway.signedIn(loft)
        val anchor = c.latestAnchor()
        val root = loft.prefs.root!!
        val f = File(ctx.cacheDir, "feed").apply { writeText("feed ${System.nanoTime()}") }
        val made = c.uploadAndCommit(f, CommitRequest(parentId = root.rootItemId, name = "feed-${System.currentTimeMillis()}.txt"))
        val page = c.changes(anchor, waitS = 5)
        assertTrue(page.updated.any { it.id == made.item.id })
        c.delete(made.item.id)
    }
}
