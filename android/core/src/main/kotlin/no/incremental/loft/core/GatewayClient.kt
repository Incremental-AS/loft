// HTTP client for the gateway API (api/openapi.yaml), following LoftCore.GatewayClient: route
// choice with fallback, token re-read on a rotation race, chunked resumable uploads,
// sliced Range downloads, and the same error mapping.
package no.incremental.loft.core

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Headers
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlin.coroutines.coroutineContext
import kotlin.coroutines.resumeWithException

private val JSON_TYPE = "application/json".toMediaType()
private val OCTET_TYPE = "application/octet-stream".toMediaType()

/** A finished response with its body read. */
class HttpResult(val code: Int, val headers: Headers, val body: ByteArray) {
    val etag: String get() = (headers["ETag"] ?: "").trim('"')
}

/** One Range read: `start` is where the bytes begin (0 if the gateway sent the whole file). */
class RangeResult(val data: ByteArray, val etag: String, val total: Long, val start: Long)

class GatewayClient(
    val selector: RouteSelector,
    private val tokens: TokenStore,
    private val pinnedHttp: OkHttpClient,
    private val pkiHttp: OkHttpClient,
) {
    /** Probe the home route before a request when the last probe is stale. */
    var autoProbe = true
    var probeTimeoutMs = 1_500L
    /** Upload chunk / download slice sizes: smaller than the Mac's 32 MB, phones have less RAM. */
    var chunkLimit: Long = 8L shl 20
    var downloadSlice: Long = 8L shl 20
    /** Waits between retries after transport errors in transfers. */
    var retryDelaysMs: List<Long> = listOf(1_000, 2_000, 4_000, 8_000, 16_000)

    private fun http(e: Endpoint) = if (e.pinned) pinnedHttp else pkiHttp

    // ── route probe ─────────────────────────────────────────────────────

    /**
     * GET <endpoint>/healthz: true only for 200 {"service":"loft","route":<expected>}.
     * Route probes use the short [probeTimeoutMs]; sign-in passes [SIGN_IN_PROBE_MS], since the
     * first TLS handshake of a fresh process can take longer than 1.5 s on a slow phone.
     */
    suspend fun probe(e: Endpoint, timeoutMs: Long = probeTimeoutMs): Boolean = withContext(Dispatchers.IO) {
        val req = Request.Builder().url(e.url.newBuilder().addPathSegment("healthz").build()).build()
        try {
            val call = http(e).newCall(req)
            call.timeout().timeout(timeoutMs, TimeUnit.MILLISECONDS)
            call.await().use { r ->
                if (r.code != 200) return@withContext false
                val h = LoftJson.decodeFromString<Health>(r.body?.string() ?: "")
                h.service == "loft" && h.route == e.expectedHealthRoute
            }
        } catch (c: CancellationException) {
            throw c
        } catch (t: Exception) {
            false
        }
    }

    suspend fun reprobe(): Route = selector.reprobe { probe(it) }

    /** Where a password may go now (pinned routes only); null = not at home. */
    suspend fun signInEndpoint(): Endpoint? = selector.signInEndpoint { probe(it, SIGN_IN_PROBE_MS) }

    // ── core request ────────────────────────────────────────────────────

    /**
     * One request. On the home route a transport failure moves to the outside route once
     * (if there is one); a 401 invalid_token is retried once with a freshly read token in
     * case another component rotated it (the old one stays valid 120 s, so this is rare).
     * `fixed`: this endpoint only, no fallback (sign-in, probes).
     */
    private suspend fun execute(
        method: String, path: String, query: List<Pair<String, String>> = emptyList(),
        body: RequestBody? = null, headers: Map<String, String> = emptyMap(), auth: Boolean = true,
        fixed: Endpoint? = null, timeoutMs: Long = 60_000,
    ): Response {
        if (fixed == null && autoProbe) selector.ensureFresh { probe(it) }
        var triedOutside = fixed != null
        var triedNewToken = false
        while (true) {
            val ep = fixed ?: selector.endpoint
            val url = ep.url.newBuilder().addPathSegments(path).apply {
                query.forEach { (k, v) -> addQueryParameter(k, v) }
            }.build()
            val rb = Request.Builder().url(url)
            val b = body ?: if (method == "POST" || method == "PUT" || method == "PATCH") ByteArray(0).toRequestBody(null) else null
            rb.method(method, b)
            headers.forEach { (k, v) -> rb.header(k, v) }
            var sent: String? = null
            if (auth) {
                val t = tokens.load() ?: throw GatewayException.NotSignedIn()
                sent = t.token
                rb.header("Authorization", "Bearer ${t.token}")
            }
            val call = http(ep).newCall(rb.build())
            call.timeout().timeout(timeoutMs, TimeUnit.MILLISECONDS)
            val resp = try {
                call.await()
            } catch (e: IOException) {
                coroutineContext.ensureActive()
                if (!triedOutside && ep.route == Route.HOME && selector.homeFailed()) {
                    triedOutside = true
                    continue
                }
                throw GatewayException.Unreachable(e.toString(), e)
            }
            if (resp.isSuccessful) return resp
            val bytes = resp.use { r -> try { r.body?.bytes() } catch (e: IOException) { null } ?: ByteArray(0) }
            val parsed = try {
                LoftJson.decodeFromString<ApiError.Body>(bytes.decodeToString())
            } catch (e: Exception) {
                null
            }
            // A proxy in front of a dead tunnel answers 502/521/523/530 with HTML: that is
            // "no answer from home", not a gateway verdict.
            if (parsed == null && resp.code >= 500) throw GatewayException.Unreachable("edge ${resp.code}")
            val err = ApiError(resp.code, parsed?.error ?: "http_${resp.code}", parsed?.received,
                parsed?.existingId, parsed?.current)
            if (resp.code == 401 && err.error == "invalid_token" && !triedNewToken) {
                val fresh = tokens.load()
                if (fresh != null && fresh.token != sent) {
                    triedNewToken = true
                    continue
                }
            }
            throw GatewayException.Api(err)
        }
    }

    private suspend fun raw(
        method: String, path: String, query: List<Pair<String, String>> = emptyList(),
        body: RequestBody? = null, headers: Map<String, String> = emptyMap(), auth: Boolean = true,
        fixed: Endpoint? = null, timeoutMs: Long = 60_000,
    ): HttpResult = withContext(Dispatchers.IO) {
        // Bodies are read here, never on the caller's thread (Android: not on the main thread).
        val r = execute(method, path, query, body, headers, auth, fixed, timeoutMs)
        r.use {
            val bytes = try {
                it.body?.bytes() ?: ByteArray(0)
            } catch (e: IOException) {
                throw GatewayException.Unreachable("body: $e", e)
            }
            HttpResult(it.code, it.headers, bytes)
        }
    }

    private suspend inline fun <reified T> call(
        method: String, path: String, query: List<Pair<String, String>> = emptyList(),
        json: String? = null, auth: Boolean = true, fixed: Endpoint? = null, timeoutMs: Long = 60_000,
    ): T {
        val r = raw(method, path, query, json?.toRequestBody(JSON_TYPE), emptyMap(), auth, fixed, timeoutMs)
        return try {
            LoftJson.decodeFromString<T>(r.body.decodeToString())
        } catch (e: Exception) {
            throw GatewayException.BadResponse(r.code, "${T::class.simpleName}: ${e.message}")
        }
    }

    // ── unauthenticated ─────────────────────────────────────────────────

    /** Current pins of the direct key, over whichever route is trusted right now. */
    suspend fun identity(): Identity = call("GET", "v1/identity", auth = false, timeoutMs = 10_000)

    // ── auth ────────────────────────────────────────────────────────────

    @Serializable private data class LoginBody(val user: String, val password: String, val device_name: String)

    /**
     * Password sign-in. Only to a pinned endpoint (the home address, or an outside address in
     * `direct` mode): a tunnel never sees a password. The password is sent once and dropped.
     */
    suspend fun login(endpoint: Endpoint, user: String, password: String, deviceName: String): LoginResult {
        require(endpoint.pinned) { "a password only goes over a pinned connection" }
        return call("POST", "v1/auth/login", json = LoftJson.encodeToString(LoginBody(user, password, deviceName)),
            auth = false, fixed = endpoint, timeoutMs = 30_000)
    }

    /** Rotate the device token; stores the new one before returning it. */
    suspend fun rotateToken(): StoredToken {
        val r: LoginResult = call("POST", "v1/auth/refresh")
        val t = StoredToken.of(r)
        tokens.save(t)
        return t
    }

    suspend fun devices(): DevicesResponse = call("GET", "v1/auth/devices")

    @Serializable private data class Ok(val ok: Boolean = false)

    suspend fun revoke(deviceId: String) {
        call<Ok>("DELETE", "v1/auth/device/$deviceId")
    }

    // ── roots ───────────────────────────────────────────────────────────

    suspend fun me(): Me = call("GET", "v1/me")

    suspend fun shares(): List<String> = call<SharesResponse>("GET", "v1/roots").shares

    suspend fun folders(share: String, path: String): FoldersResponse =
        call("GET", "v1/roots", query = listOf("share" to share, "path" to path))

    @Serializable private data class RootBody(val share: String, val path: String)

    suspend fun setRoot(share: String, path: String): Me =
        call("PUT", "v1/me/root", json = LoftJson.encodeToString(RootBody(share, path)))

    // ── items ───────────────────────────────────────────────────────────

    suspend fun item(id: String): Item = call("GET", "v1/items/$id")

    suspend fun children(id: String, cursor: String? = null, limit: Int = 500): ChildrenPage {
        val q = mutableListOf("limit" to limit.toString())
        if (cursor != null) q += "cursor" to cursor
        return call("GET", "v1/items/$id/children", query = q, timeoutMs = 120_000)
    }

    /** Every child of a folder, following cursors. */
    suspend fun allChildren(id: String): List<Item> {
        val out = mutableListOf<Item>()
        var cursor: String? = null
        do {
            val page = children(id, cursor)
            out += page.items
            cursor = page.nextCursor
        } while (cursor != null)
        return out
    }

    suspend fun changes(anchor: String, waitS: Int = 0, limit: Int = 500): ChangesPage =
        call("GET", "v1/changes",
            query = listOf("anchor" to anchor, "wait" to waitS.toString(), "limit" to limit.toString()),
            timeoutMs = (waitS + 30) * 1_000L)

    suspend fun latestAnchor(): String = call<AnchorResponse>("GET", "v1/changes/latest").anchor

    /** A JPEG or PNG of at most `px` on its longest side, rendered on the gateway; null if none. */
    suspend fun thumbnail(id: String, px: Int): ByteArray? = try {
        raw("GET", "v1/items/$id/thumbnail", query = listOf("px" to px.toString()), timeoutMs = 45_000).body
    } catch (e: GatewayException.Api) {
        if (e.error.error == "no_thumbnail") null else throw e
    }

    @Serializable private data class FolderBody(val parent_id: String, val name: String)

    suspend fun mkdir(parentId: String, name: String): Item =
        call("POST", "v1/folders", json = LoftJson.encodeToString(FolderBody(parentId, name)))

    @Serializable private data class PatchBody(val name: String? = null, val parent_id: String? = null, val if_match: String? = null)

    suspend fun patch(id: String, name: String? = null, parentId: String? = null, ifMatch: String? = null): Item =
        call("PATCH", "v1/items/$id", json = LoftJson.encodeToString(PatchBody(name, parentId, ifMatch)))

    suspend fun delete(id: String, ifMatch: String? = null): DeleteResponse =
        call("DELETE", "v1/items/$id", query = ifMatch?.let { listOf("if_match" to it) } ?: emptyList())

    // ── upload (docs/design.md §D.5) ───────────────────────────────────────

    @Serializable private data class UploadBody(val size: Long, val sha256: String, val near_id: String? = null)

    /** `near`: the parent (create) or the item (modify); required in an all-shares root. */
    suspend fun uploadCreate(size: Long, sha256: String, near: String? = null): UploadState =
        call("POST", "v1/uploads", json = LoftJson.encodeToString(UploadBody(size, sha256, near)))

    suspend fun uploadStatus(id: String): UploadState = call("GET", "v1/uploads/$id")

    suspend fun uploadChunk(id: String, offset: Long, data: ByteArray): UploadState {
        val r = raw("PUT", "v1/uploads/$id", query = listOf("offset" to offset.toString()),
            body = data.toRequestBody(OCTET_TYPE), headers = mapOf("X-Chunk-Sha256" to sha256Hex(data)),
            timeoutMs = 300_000)
        return try {
            LoftJson.decodeFromString(r.body.decodeToString())
        } catch (e: Exception) {
            throw GatewayException.BadResponse(r.code, "UploadState: ${e.message}")
        }
    }

    /** The gateway re-reads the whole file from the NAS before it answers. */
    suspend fun commit(uploadId: String, req: CommitRequest): CommitResponse =
        call("POST", "v1/uploads/$uploadId/commit", json = LoftJson.encodeToString(req), timeoutMs = 600_000)

    /**
     * Upload a local file into a finished, hash-checked upload session and return its id.
     * Resumes from a session the store remembers for this content, from `received` after a
     * 409 wrong_offset, and after transport errors.
     */
    suspend fun upload(
        file: File, near: String? = null, resume: ResumeStore? = null,
        progress: ((Long, Long) -> Unit)? = null,
    ): String = withContext(Dispatchers.IO) {
        val size = file.length()
        val sha = sha256Hex(file)
        val key = "$sha:$size"
        var st: UploadState? = resume?.get(key)?.let { id ->
            try { uploadStatus(id) } catch (e: GatewayException) { null }
        }
        if (st == null) st = uploadCreate(size, sha, near)
        resume?.put(key, st.uploadId)
        val chunk = minOf(st.chunkMax ?: chunkLimit, chunkLimit).coerceAtLeast(1)
        var offset = st.received
        var failures = 0
        var recreated = false
        RandomAccessFile(file, "r").use { raf ->
            while (offset < size) {
                coroutineContext.ensureActive()
                val n = minOf(chunk, size - offset).toInt()
                val buf = ByteArray(n)
                raf.seek(offset)
                val read = raf.read(buf)
                if (read <= 0) throw GatewayException.ChangedDuringTransfer()     // shrank under us
                val data = if (read == n) buf else buf.copyOf(read)
                try {
                    st = uploadChunk(st!!.uploadId, offset, data)
                    offset = st!!.received
                    failures = 0
                    progress?.invoke(offset, size)
                } catch (e: GatewayException.Api) {
                    when {
                        e.error.error == "wrong_offset" && e.error.received != null -> offset = e.error.received
                        e.error.error == "no_such_upload" && !recreated -> {
                            recreated = true                                    // expired (24 h) or gateway rebuilt
                            st = uploadCreate(size, sha, near)
                            resume?.put(key, st!!.uploadId)
                            offset = 0
                        }
                        else -> throw e
                    }
                } catch (e: GatewayException.Unreachable) {
                    if (failures >= retryDelaysMs.size) throw e
                    delay(retryDelaysMs[failures])
                    failures++
                    try { offset = uploadStatus(st!!.uploadId).received } catch (_: GatewayException) {}
                }
            }
        }
        st!!.uploadId
    }

    /** Upload + commit; forgets the resumable session once committed. */
    suspend fun uploadAndCommit(
        file: File, req: CommitRequest, resume: ResumeStore? = null,
        progress: ((Long, Long) -> Unit)? = null,
    ): CommitResponse {
        val id = upload(file, req.itemId ?: req.parentId, resume, progress)
        try {
            val r = commit(id, req)
            resume?.forgetUpload(id)
            return r
        } catch (e: GatewayException.Api) {
            if (e.error.error == "file_hash_mismatch" || e.error.error == "no_such_upload") resume?.forgetUpload(id)
            throw e
        }
    }

    // ── download (Range, sliced) ────────────────────────────────────────

    /**
     * Download an item's content into `dest` (created/truncated) in Range slices, so a dropped
     * connection resumes from the last byte written. Returns the content etag ("c<N>"). If the
     * NAS copy changes mid-way it starts over (max 3 times).
     */
    suspend fun download(id: String, dest: File, progress: ((Long, Long) -> Unit)? = null): String {
        repeat(3) {
            downloadOnce(id, dest, progress)?.let { return it }
        }
        throw GatewayException.ChangedDuringTransfer()
    }

    private suspend fun downloadOnce(id: String, dest: File, progress: ((Long, Long) -> Unit)?): String? =
        withContext(Dispatchers.IO) { downloadInto(id, dest, progress) }

    private suspend fun downloadInto(id: String, dest: File, progress: ((Long, Long) -> Unit)?): String? {
        RandomAccessFile(dest, "rw").use { out ->
            out.setLength(0)
            var offset = 0L
            var total: Long? = null
            var etag: String? = null
            var failures = 0
            var noRange = false
            while (total == null || offset < total) {
                coroutineContext.ensureActive()
                val headers = if (noRange) emptyMap() else mapOf("Range" to "bytes=$offset-${offset + downloadSlice - 1}")
                val r = try {
                    raw("GET", "v1/items/$id/content", headers = headers, timeoutMs = 300_000)
                } catch (e: GatewayException.Unreachable) {
                    if (failures >= retryDelaysMs.size) throw e
                    delay(retryDelaysMs[failures])
                    failures++
                    continue
                } catch (e: GatewayException.Api) {
                    if (e.error.status != 416) throw e
                    if (offset > 0 || noRange) return null      // shrank on the NAS since the first slice
                    noRange = true                              // empty file: ask again without a Range
                    continue
                }
                failures = 0
                val tag = r.etag
                if (etag != null && tag != etag) return null
                etag = tag
                val t = totalFromContentRange(r)
                if (r.code == 206 && t != null) {
                    total = t
                } else {
                    out.setLength(0)                            // 200: the whole file
                    offset = 0
                    total = r.body.size.toLong()
                }
                out.seek(offset)
                out.write(r.body)
                offset += r.body.size
                progress?.invoke(offset, total)
                if (r.body.isEmpty()) break
            }
            return etag ?: ""
        }
    }

    /** One Range read, for streaming (a player seeking in a video, a provider pipe). */
    suspend fun readRange(id: String, offset: Long, length: Long): RangeResult {
        val r = raw("GET", "v1/items/$id/content",
            headers = mapOf("Range" to "bytes=$offset-${offset + maxOf(length, 1) - 1}"), timeoutMs = 120_000)
        val t = totalFromContentRange(r)
        return if (r.code == 206 && t != null) RangeResult(r.body, r.etag, t, offset)
        else RangeResult(r.body, r.etag, r.body.size.toLong(), 0)
    }

    private fun totalFromContentRange(r: HttpResult): Long? =
        r.headers["Content-Range"]?.substringAfterLast('/', "")?.toLongOrNull()

    companion object {
        const val SIGN_IN_PROBE_MS = 8_000L

        fun sha256Hex(data: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(data).toHex()

        fun sha256Hex(file: File): String {
            val md = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { input ->
                val buf = ByteArray(1 shl 16)
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    md.update(buf, 0, n)
                }
            }
            return md.digest().toHex()
        }

        private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }
    }
}

/** OkHttp's async call as a suspend function; cancelling the coroutine cancels the call. */
suspend fun Call.await(): Response = suspendCancellableCoroutine { cont ->
    cont.invokeOnCancellation { runCatching { cancel() } }
    enqueue(object : Callback {
        override fun onFailure(call: Call, e: IOException) {
            if (cont.isActive) cont.resumeWithException(e)
        }

        override fun onResponse(call: Call, response: Response) {
            if (cont.isActive) cont.resume(response) { _, r, _ -> r.close() } else response.close()
        }
    })
}
