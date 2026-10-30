// The instrumented tests run against gateway/scripts/dev_server.py on the host. The emulator
// reaches the host's loopback as 10.0.2.2. The pin comes from the dev server's work dir:
//   -e loftPin "$(cat <work-dir>/direct.pin)"   [-e loftLan https://10.0.2.2:18443]
package no.incremental.loft

import androidx.test.platform.app.InstrumentationRegistry
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.Pairing
import no.incremental.loft.core.StoredToken

object TestGateway {
    private val args get() = InstrumentationRegistry.getArguments()
    val lan: String get() = args.getString("loftLan") ?: "https://10.0.2.2:18443"
    val pin: String get() = args.getString("loftPin") ?: error("pass -e loftPin <dev server direct.pin>")
    // dev_server.py's built-in test account and share.
    val user: String get() = args.getString("loftUser") ?: "loft-dev"
    val password: String get() = args.getString("loftPassword") ?: "dev-password"
    val share: String get() = args.getString("loftShare") ?: "home"

    /** What `loft-admin pair` would print for the dev server, as a link. */
    fun link(): String = Pairing("Test gateway", lan, listOf(pin)).toLink()

    /** Paired, signed in and rooted at the dev share, the way the app does it. */
    suspend fun signedIn(loft: Loft): GatewayClient {
        loft.forgetGateway()
        loft.pair(Pairing.parse(link()))
        val c = loft.requireClient()
        val ep = c.signInEndpoint() ?: error("dev server not reachable at $lan with that pin")
        val r = c.login(ep, user, password, "Android test")
        loft.tokens.save(StoredToken.of(r))
        loft.prefs.nasUser = user
        val me = c.setRoot(share, "")
        loft.prefs.root = me.root
        return c
    }
}
