// Route selection (docs/design.md §A, §E.2, LoftCore.RouteSelector): the home address
// first, the outside address when home doesn't answer.
package no.incremental.loft.core

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

enum class Route { HOME, OUTSIDE }

/**
 * One way to the gateway. `pinned`: TLS is checked against the pins (the home address, and
 * an outside address in `direct` mode), so a password may go there. Otherwise normal web
 * PKI and device tokens only.
 */
data class Endpoint(val route: Route, val url: HttpUrl, val pinned: Boolean) {
    /** /healthz answers with the listener it came in on: the direct listener says "direct". */
    val expectedHealthRoute: String get() = if (pinned) "direct" else "public"
}

class RouteSelector(
    pairing: Pairing,
    private val reprobeEveryMs: Long = 5 * 60_000L,
    private val clock: () -> Long = System::currentTimeMillis,
) {
    val home = Endpoint(Route.HOME, pairing.lanUrl.toHttpUrl(), pinned = true)
    val outside: Endpoint? = pairing.publicUrl?.let {
        Endpoint(Route.OUTSIDE, it.toHttpUrl(), pinned = pairing.publicMode == PublicMode.DIRECT)
    }

    private val lock = Any()
    private var _current: Route = if (outside != null) Route.OUTSIDE else Route.HOME
    private var lastProbe: Long? = null

    val current: Route get() = synchronized(lock) { _current }

    val endpoint: Endpoint get() = endpointFor(current)

    fun endpointFor(r: Route): Endpoint = if (r == Route.OUTSIDE && outside != null) outside else home

    /** `probe`: GET <home>/healthz over the pinned client with a short timeout. */
    suspend fun reprobe(probe: suspend (Endpoint) -> Boolean): Route {
        val ok = probe(home)
        set(if (ok || outside == null) Route.HOME else Route.OUTSIDE, probed = true)
        return current
    }

    suspend fun ensureFresh(probe: suspend (Endpoint) -> Boolean): Route =
        if (isStale) reprobe(probe) else current

    /** A request on the home route got no answer: use the outside route until the next probe. */
    fun homeFailed(): Boolean {
        if (outside == null) return false
        set(Route.OUTSIDE, probed = false)
        return true
    }

    /** Forget the last probe, e.g. after a network change. */
    fun invalidate() = synchronized(lock) { lastProbe = null }

    val isStale: Boolean
        get() = synchronized(lock) { lastProbe?.let { clock() - it > reprobeEveryMs } ?: true }

    private fun set(r: Route, probed: Boolean) = synchronized(lock) {
        _current = r
        if (probed) lastProbe = clock()
    }

    /**
     * Where a password may go right now: the home address if it answers with a pinned key,
     * else an outside address in `direct` mode if that does. null = "not at home" (and the
     * outside route, if any, is a tunnel, which never takes a password).
     */
    suspend fun signInEndpoint(probe: suspend (Endpoint) -> Boolean): Endpoint? {
        if (probe(home)) {
            set(Route.HOME, probed = true)
            return home
        }
        val o = outside
        if (o != null && o.pinned && probe(o)) return o
        return null
    }
}
