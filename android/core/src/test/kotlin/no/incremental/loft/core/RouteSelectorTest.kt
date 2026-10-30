package no.incremental.loft.core

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64

class RouteSelectorTest {
    private val pin = Base64.getEncoder().encodeToString(ByteArray(32))
    private val lanOnly = Pairing("H", "https://gw.example:8443", listOf(pin))
    private val tunnel = lanOnly.copy(publicUrl = "https://files.example", publicMode = PublicMode.TUNNEL)
    private val direct = lanOnly.copy(publicUrl = "https://home.example:8443", publicMode = PublicMode.DIRECT)

    @Test fun endpointsAndPinning() {
        val t = RouteSelector(tunnel)
        assertTrue(t.home.pinned)
        assertFalse(t.outside!!.pinned)
        assertEquals("public", t.outside!!.expectedHealthRoute)
        assertTrue(RouteSelector(direct).outside!!.pinned)
        assertNull(RouteSelector(lanOnly).outside)
    }

    @Test fun homeFirstThenOutside() = runBlocking {
        val s = RouteSelector(tunnel)
        val probed = mutableListOf<Route>()
        assertEquals(Route.HOME, s.reprobe { probed += it.route; true })
        assertEquals(Route.OUTSIDE, s.reprobe { probed += it.route; false })
        assertEquals(listOf(Route.HOME, Route.HOME), probed)    // only home is ever probed
        assertEquals("https://files.example/", s.endpoint.url.toString())
    }

    @Test fun withoutAnOutsideAddressHomeStays() = runBlocking {
        val s = RouteSelector(lanOnly)
        assertEquals(Route.HOME, s.reprobe { false })
        assertFalse(s.homeFailed())
        assertEquals(Route.HOME, s.current)
    }

    @Test fun ensureFreshProbesOnlyWhenStale() = runBlocking {
        var now = 1_000_000L
        val s = RouteSelector(tunnel, reprobeEveryMs = 300_000, clock = { now })
        var n = 0
        s.ensureFresh { n++; true }
        s.ensureFresh { n++; true }
        assertEquals(1, n)
        now += 300_001
        s.ensureFresh { n++; true }
        assertEquals(2, n)
        s.invalidate()
        s.ensureFresh { n++; true }
        assertEquals(3, n)
    }

    @Test fun homeFailedMovesOutUntilTheNextProbe() = runBlocking {
        var now = 0L
        val s = RouteSelector(tunnel, reprobeEveryMs = 1000, clock = { now })
        s.reprobe { true }
        assertTrue(s.homeFailed())
        assertEquals(Route.OUTSIDE, s.current)
        assertFalse(s.isStale)                 // a failed request is not a probe
        now += 1001
        assertEquals(Route.HOME, s.ensureFresh { true })
    }

    @Test fun signInOnlyOverPinnedRoutes() = runBlocking {
        assertEquals(Route.HOME, RouteSelector(tunnel).signInEndpoint { it.route == Route.HOME }?.route)
        // Not at home, outside is a tunnel: no password anywhere (and the tunnel isn't even asked).
        val asked = mutableListOf<Route>()
        assertNull(RouteSelector(tunnel).signInEndpoint { asked += it.route; it.route == Route.OUTSIDE })
        assertEquals(listOf(Route.HOME), asked)
        // Not at home, outside is a pinned port forward: sign-in goes there.
        val o = RouteSelector(direct).signInEndpoint { it.route == Route.OUTSIDE }
        assertEquals(Route.OUTSIDE, o?.route)
        assertTrue(o!!.pinned)
        assertNull(RouteSelector(lanOnly).signInEndpoint { false })
    }
}
