package no.incremental.loft.core

import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File
import java.net.ServerSocket
import java.util.Base64

/** The client's protocol logic against MockWebServer (plain HTTP; TLS is PinningTest's job). */
class GatewayClientTest {
    private lateinit var home: MockWebServer
    private lateinit var outside: MockWebServer
    private val http = OkHttpClient()
    private val pin = Base64.getEncoder().encodeToString(ByteArray(32))
    private val token = StoredToken("lft_dev.secret-a", "dev", System.currentTimeMillis(), Long.MAX_VALUE)

    @Before fun setUp() {
        home = MockWebServer().apply { start() }
        outside = MockWebServer().apply { start() }
    }

    @After fun tearDown() {
        home.shutdown()
        outside.shutdown()
    }

    private fun base(s: MockWebServer) = s.url("/").toString().trimEnd('/')

    private fun client(tokens: TokenStore = MemoryTokenStore(token), withOutside: Boolean = false, mode: PublicMode = PublicMode.TUNNEL): GatewayClient {
        val p = Pairing("H", base(home), listOf(pin),
            if (withOutside) base(outside) else null, if (withOutside) mode else null)
        return GatewayClient(RouteSelector(p), tokens, http, http).apply {
            autoProbe = false
            retryDelaysMs = listOf(1, 1, 1)
        }
    }

    private fun json(body: String, code: Int = 200) =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)

    private val meJson = """{"nas_user":"loft-dev","device_id":"dev","device_name":"Pixel","route":"direct",
        "root":{"id":"r1","share":"home","path":"","root_item_id":"r1","anchor":"e.0"}}"""

    @Test fun sendsTheTokenAndParses() = runBlocking {
        home.enqueue(json(meJson))
        val me = client().me()
        assertEquals("loft-dev", me.nasUser)
        assertEquals("r1", me.root!!.rootItemId)
        val r = home.takeRequest()
        assertEquals("Bearer lft_dev.secret-a", r.getHeader("Authorization"))
        assertEquals("/v1/me", r.path)
    }

    @Test fun notSignedInWithoutAToken() = runBlocking {
        try {
            client(MemoryTokenStore()).me()
            fail()
        } catch (e: GatewayException.NotSignedIn) {
        }
        assertEquals(0, home.requestCount)
    }

    @Test fun errorMapping() = runBlocking {
        val c = client()
        home.enqueue(json("""{"error":"device_revoked"}""", 401))
        val revoked = runCatching { c.me() }.exceptionOrNull() as GatewayException.Api
        assertEquals(401, revoked.error.status)
        assertEquals(SyncError.DeviceRemoved, SyncError.of(revoked.error))
        assertTrue(revoked.error.needsSignIn)

        home.enqueue(MockResponse().setResponseCode(502).setBody("<html>Bad gateway</html>"))
        val edge = runCatching { c.me() }.exceptionOrNull()
        assertTrue(edge is GatewayException.Unreachable)
        assertEquals(LoopState.CantReachHome, LoopState.of(edge!!))

        home.enqueue(json("""{"error":"nas_unavailable","detail":"x"}""", 503))
        val nas = runCatching { c.me() }.exceptionOrNull()!!
        assertEquals(LoopState.CantReachNas, LoopState.of(nas))

        home.enqueue(json("""{"error":"name_exists","existing_id":"abc"}""", 409))
        val dup = runCatching { c.mkdir("p", "x") }.exceptionOrNull() as GatewayException.Api
        assertEquals("abc", dup.error.existingId)

        home.enqueue(json("""{"error":"access_denied"}""", 403))
        val denied = runCatching { c.me() }.exceptionOrNull()!!
        assertEquals(LoopState.Failed(SyncError.NoAccess), LoopState.of(denied))

        home.enqueue(json("not json"))
        assertTrue(runCatching { c.me() }.exceptionOrNull() is GatewayException.BadResponse)
    }

    @Test fun retriesOnceWithATokenRotatedUnderIt() = runBlocking {
        val newer = token.copy(token = "lft_dev.secret-b")
        val store = object : TokenStore {
            var n = 0
            override fun load() = if (n++ == 0) token else newer
            override fun save(t: StoredToken) {}
            override fun clear() {}
        }
        home.enqueue(json("""{"error":"invalid_token"}""", 401))
        home.enqueue(json(meJson))
        client(store).me()
        assertEquals("Bearer lft_dev.secret-a", home.takeRequest().getHeader("Authorization"))
        assertEquals("Bearer lft_dev.secret-b", home.takeRequest().getHeader("Authorization"))
    }

    @Test fun invalidTokenWithTheSameTokenIsFinal() = runBlocking {
        home.enqueue(json("""{"error":"invalid_token"}""", 401))
        val e = runCatching { client().me() }.exceptionOrNull() as GatewayException.Api
        assertEquals(SyncError.SignInExpired, SyncError.of(e.error))
        assertEquals(1, home.requestCount)
    }

    @Test fun homeFailureFallsBackOutside() = runBlocking {
        val c = client(withOutside = true)
        c.selector.reprobe { true }
        assertEquals(Route.HOME, c.selector.current)
        home.shutdown()                          // home stops answering mid-session
        outside.enqueue(json(meJson))
        c.me()
        assertEquals(Route.OUTSIDE, c.selector.current)
        assertEquals(1, outside.requestCount)
    }

    @Test fun autoProbeChoosesTheRoute() = runBlocking {
        val c = client(withOutside = true).apply { autoProbe = true }
        home.enqueue(json("""{"ok":true,"service":"loft","route":"direct"}"""))
        home.enqueue(json(meJson))
        c.me()
        assertEquals(Route.HOME, c.selector.current)
        assertEquals("/healthz", home.takeRequest().path)
        assertEquals("/v1/me", home.takeRequest().path)
    }

    @Test fun probeWantsTheDirectListener() = runBlocking {
        val c = client()
        home.enqueue(json("""{"ok":true,"service":"loft","route":"public"}"""))
        assertTrue(!c.probe(c.selector.home))
        home.enqueue(json("""{"ok":true,"service":"other","route":"direct"}"""))
        assertTrue(!c.probe(c.selector.home))
        home.enqueue(json("""{"ok":true,"service":"loft","route":"direct"}"""))
        assertTrue(c.probe(c.selector.home))
    }

    @Test fun passwordNeverGoesToATunnel() = runBlocking {
        val c = client(withOutside = true, mode = PublicMode.TUNNEL)
        try {
            c.login(c.selector.outside!!, "u", "p", "Pixel")
            fail("login over a tunnel")
        } catch (e: IllegalArgumentException) {
        }
        assertEquals(0, outside.requestCount)
    }

    @Test fun loginGoesToTheGivenEndpointOnly() = runBlocking {
        val c = client(MemoryTokenStore(), withOutside = true)
        home.enqueue(json("""{"error":"invalid_credentials"}""", 401))
        val e = runCatching { c.login(c.selector.home, "loft-dev", "wrong", "Pixel") }.exceptionOrNull() as GatewayException.Api
        assertEquals("invalid_credentials", e.error.error)
        val r = home.takeRequest()
        assertEquals("/v1/auth/login", r.path)
        assertNull(r.getHeader("Authorization"))
        val body = r.body.readUtf8()
        assertTrue(body.contains("\"device_name\":\"Pixel\""))
        assertEquals(0, outside.requestCount)

        home.enqueue(json("""{"device_id":"d1","token":"lft_d1.s","nas_user":"loft-dev","expires_at":1900000000.5}"""))
        val ok = c.login(c.selector.home, "loft-dev", "dev-password", "Pixel")
        assertEquals("d1", ok.deviceId)
        assertEquals(1_900_000_000_500L, StoredToken.of(ok).expiresAtMs)
    }

    @Test fun rotateStoresTheNewToken() = runBlocking {
        val store = MemoryTokenStore(token)
        home.enqueue(json("""{"device_id":"dev","token":"lft_dev.new","expires_at":1900000000}"""))
        client(store).rotateToken()
        assertEquals("lft_dev.new", store.load()!!.token)
        assertEquals("/v1/auth/refresh", home.takeRequest().path)
    }

    @Test fun chunkedUploadResumesAfterWrongOffset() = runBlocking {
        val f = File.createTempFile("loft", ".bin").apply { writeBytes(ByteArray(20) { it.toByte() }); deleteOnExit() }
        val c = client().apply { chunkLimit = 8 }
        home.enqueue(json("""{"upload_id":"u1","received":0,"size":20,"chunk_max":33554432}"""))
        home.enqueue(json("""{"upload_id":"u1","received":8,"size":20}"""))
        home.enqueue(json("""{"error":"wrong_offset","received":16}""", 409))   // the gateway had more than we thought
        home.enqueue(json("""{"upload_id":"u1","received":20,"size":20}"""))
        home.enqueue(json("""{"result":"created","item":{"id":"i1","parent_id":"p","name":"a.bin","type":"file","size":20,"mtime_ns":0,"content_etag":"c1","meta_etag":"m1","sha256":null}}"""))
        val resume = MemoryResumeStore()
        val r = c.uploadAndCommit(f, CommitRequest(parentId = "p", name = "a.bin"), resume)
        assertEquals("i1", r.item.id)

        val create = home.takeRequest()
        val sha = GatewayClient.sha256Hex(f)
        assertTrue(create.body.readUtf8().contains("\"sha256\":\"$sha\""))
        val c1 = home.takeRequest()
        assertEquals("/v1/uploads/u1?offset=0", c1.path)
        assertEquals(GatewayClient.sha256Hex(ByteArray(8) { it.toByte() }), c1.getHeader("X-Chunk-Sha256"))
        assertEquals("/v1/uploads/u1?offset=8", home.takeRequest().path)
        val c3 = home.takeRequest()
        assertEquals("/v1/uploads/u1?offset=16", c3.path)
        assertArrayEquals(ByteArray(4) { (16 + it).toByte() }, c3.body.readByteArray())
        val commit = home.takeRequest()
        assertEquals("/v1/uploads/u1/commit", commit.path)
        assertTrue(commit.body.readUtf8().contains("\"parent_id\":\"p\""))
        assertNull(resume.get("$sha:20"))          // forgotten once committed
    }

    @Test fun uploadContinuesARememberedSession() = runBlocking {
        val f = File.createTempFile("loft", ".bin").apply { writeBytes(ByteArray(10) { 7 }); deleteOnExit() }
        val resume = MemoryResumeStore().apply { put("${GatewayClient.sha256Hex(f)}:10", "u9") }
        home.enqueue(json("""{"upload_id":"u9","received":6,"size":10}"""))
        home.enqueue(json("""{"upload_id":"u9","received":10,"size":10}"""))
        assertEquals("u9", client().upload(f, null, resume))
        assertEquals("/v1/uploads/u9", home.takeRequest().path)
        assertEquals("/v1/uploads/u9?offset=6", home.takeRequest().path)
    }

    @Test fun uploadRecreatesAnExpiredSession() = runBlocking {
        val f = File.createTempFile("loft", ".bin").apply { writeBytes(ByteArray(4) { 1 }); deleteOnExit() }
        home.enqueue(json("""{"upload_id":"u1","received":0,"size":4}"""))
        home.enqueue(json("""{"error":"no_such_upload"}""", 404))
        home.enqueue(json("""{"upload_id":"u2","received":0,"size":4}"""))
        home.enqueue(json("""{"upload_id":"u2","received":4,"size":4}"""))
        assertEquals("u2", client().upload(f))
    }

    /** Serves `content` with Range support, ETag c<version()>. */
    private fun rangeServer(content: () -> ByteArray, version: () -> Int) = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest): MockResponse {
            val data = content()
            val range = request.getHeader("Range")
            val etag = "\"c${version()}\""
            if (range == null) return MockResponse().setHeader("ETag", etag).setBody(okio.Buffer().write(data))
            val m = Regex("bytes=(\\d+)-(\\d+)").matchEntire(range)!!
            val start = m.groupValues[1].toInt()
            if (start >= data.size) return json("""{"error":"range_not_satisfiable"}""", 416)
            val end = minOf(m.groupValues[2].toInt(), data.size - 1)
            return MockResponse().setResponseCode(206).setHeader("ETag", etag)
                .setHeader("Content-Range", "bytes $start-$end/${data.size}")
                .setBody(okio.Buffer().write(data.copyOfRange(start, end + 1)))
        }
    }

    @Test fun downloadInSlices() = runBlocking {
        val data = ByteArray(21) { (it * 3).toByte() }
        home.dispatcher = rangeServer({ data }, { 1 })
        val c = client().apply { downloadSlice = 8 }
        val dest = File.createTempFile("loft", ".dl").apply { deleteOnExit() }
        assertEquals("c1", c.download("i1", dest))
        assertArrayEquals(data, dest.readBytes())
        assertEquals(3, home.requestCount)
    }

    @Test fun downloadStartsOverWhenTheFileChanges() = runBlocking {
        var n = 0
        home.dispatcher = rangeServer({ ByteArray(16) { 5 } }, { if (n++ == 0) 1 else 2 })
        val c = client().apply { downloadSlice = 8 }
        val dest = File.createTempFile("loft", ".dl").apply { deleteOnExit() }
        assertEquals("c2", c.download("i1", dest))
        assertEquals(16, dest.length())
    }

    @Test fun downloadOfAnEmptyFile() = runBlocking {
        home.dispatcher = rangeServer({ ByteArray(0) }, { 3 })
        val dest = File.createTempFile("loft", ".dl").apply { writeText("old"); deleteOnExit() }
        assertEquals("c3", client().download("i1", dest))
        assertEquals(0, dest.length())
    }

    @Test fun readRangeForStreaming() = runBlocking {
        val data = ByteArray(100) { it.toByte() }
        home.dispatcher = rangeServer({ data }, { 4 })
        val r = client().readRange("i1", 40, 10)
        assertEquals(40, r.start)
        assertEquals(100, r.total)
        assertEquals("c4", r.etag)
        assertArrayEquals(data.copyOfRange(40, 50), r.data)
    }

    @Test fun thumbnailNoneIsNull() = runBlocking {
        home.enqueue(json("""{"error":"no_thumbnail"}""", 404))
        assertNull(client().thumbnail("i1", 256))
        home.enqueue(MockResponse().setBody("JPEGBYTES"))
        assertEquals("JPEGBYTES", String(client().thumbnail("i1", 256)!!))
        assertEquals("/v1/items/i1/thumbnail?px=256", home.takeRequest().path)
    }

    @Test fun changeWatcherRestartsOnAnExpiredAnchor() = runBlocking {
        val seen = mutableListOf<String>()
        home.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val p = request.path!!
                return when {
                    p.startsWith("/v1/changes/latest") -> json("""{"anchor":"e2.5"}""")
                    p.contains("anchor=e1.1") -> json("""{"error":"anchor_expired"}""", 410)
                    p.contains("anchor=e2.5") -> json("""{"updated":[{"id":"x","parent_id":"r","name":"n","type":"file"}],"deleted":["y"],"anchor":"e2.6","more":false}""")
                    else -> { Thread.sleep(50); json("""{"updated":[],"deleted":[],"anchor":"e2.6","more":false}""") }
                }
            }
        }
        val c = client()
        val anchors = mutableListOf<String>()
        val job = launch(kotlinx.coroutines.Dispatchers.IO) {
            ChangeWatcher(c, waitS = 0).run("e1.1",
                onChange = { page -> synchronized(seen) { seen += page?.updated?.joinToString { it.id } ?: "<all>" } },
                onAnchor = { synchronized(anchors) { anchors += it } })
        }
        withTimeout(10_000) {
            while (synchronized(seen) { seen.size } < 2) kotlinx.coroutines.delay(20)
        }
        job.cancelAndJoin()
        assertEquals(listOf("<all>", "x"), synchronized(seen) { seen.take(2) })
        assertEquals(listOf("e2.5", "e2.6"), synchronized(anchors) { anchors.take(2) })
    }

    @Test fun unreachableHomeWithoutOutsideIsUnreachable() = runBlocking {
        val port = ServerSocket(0).use { it.localPort }        // nothing listens here
        val p = Pairing("H", "http://127.0.0.1:$port", listOf(pin))
        val c = GatewayClient(RouteSelector(p), MemoryTokenStore(token), http, http).apply { autoProbe = false }
        val e = runCatching { c.me() }.exceptionOrNull()
        assertTrue(e is GatewayException.Unreachable)
        assertEquals(LoopState.CantReachHome, LoopState.of(e!!))
    }
}
