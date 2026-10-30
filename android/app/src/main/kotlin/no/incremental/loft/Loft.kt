// One object graph per process: the pairing, the token store, the gateway client and
// the change feed. The UI, the DocumentsProvider and the workers all go through here.
package no.incremental.loft

import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import android.util.Log
import android.webkit.MimeTypeMap
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import no.incremental.loft.core.ChangeWatcher
import no.incremental.loft.core.ChangesPage
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.GatewayException
import no.incremental.loft.core.HttpClients
import no.incremental.loft.core.Item
import no.incremental.loft.core.LoopState
import no.incremental.loft.core.Pairing
import no.incremental.loft.core.PinSource
import no.incremental.loft.core.Route
import no.incremental.loft.core.RouteSelector
import no.incremental.loft.data.AppPrefs
import no.incremental.loft.data.KeystoreTokenStore
import java.util.concurrent.ConcurrentHashMap

class Loft private constructor(val ctx: Context) {
    val prefs = AppPrefs(ctx)
    val tokens = KeystoreTokenStore(ctx)
    private val baseHttp = HttpClients.base()
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** The latest state of the change loop and the route, for the UI's status line. */
    val loopState = MutableStateFlow<LoopState>(LoopState.Ok)
    val route = MutableStateFlow<Route?>(null)
    /** Every change page (null = "re-read everything"), for open screens. */
    val changes = MutableSharedFlow<ChangesPage?>(extraBufferCapacity = 16)

    /** Items seen in listings, so a deletion can be told to the right folder. */
    val items = ConcurrentHashMap<String, Item>()

    private var cached: Pair<Pairing, GatewayClient>? = null

    /** The client for the current pairing; rebuilt (fresh TLS state) when the pins change. */
    @Synchronized
    fun client(): GatewayClient? {
        val p = prefs.pairing ?: return null
        cached?.let { if (it.first == p) return it.second }
        val pinned = HttpClients.pinned(baseHttp, PinSource { prefs.pairing?.pins?.toSet() ?: emptySet() })
        val c = GatewayClient(RouteSelector(p), tokens, pinned, baseHttp)
        cached = p to c
        return c
    }

    fun requireClient(): GatewayClient = client() ?: throw GatewayException.NotSignedIn()

    val isPaired: Boolean get() = prefs.pairing != null
    val isSignedIn: Boolean get() = isPaired && tokens.load() != null
    val isReady: Boolean get() = isSignedIn && prefs.root != null

    fun deviceName(): String = listOfNotNull(Build.MANUFACTURER?.replaceFirstChar { it.uppercase() }, Build.MODEL)
        .distinct().joinToString(" ").take(80).ifBlank { "Android" }

    /** A new pairing replaces the old one and everything signed in with it. */
    fun pair(p: Pairing) {
        signOutLocal()
        prefs.pairing = p
    }

    fun forgetGateway() {
        signOutLocal()
        prefs.pairing = null
    }

    /** Sign out: tell the gateway to forget this device (best effort), then forget the token here. */
    suspend fun signOut() {
        val c = client()
        val t = tokens.load()
        if (c != null && t != null) {
            try { c.revoke(t.deviceId) } catch (e: Exception) { Log.i(TAG, "revoke on sign-out: $e") }
        }
        signOutLocal()
    }

    fun signOutLocal() {
        stopWatcher()
        tokens.clear()
        prefs.clearAccount()
        items.clear()
        Work.cancelAccountWork(ctx)
        notifyRoots()
    }

    /** New pins from GET /v1/identity over a trusted route (pairing.md: pins are refreshed as before). */
    suspend fun refreshPins() {
        val c = client() ?: return
        val id = try { c.identity() } catch (e: Exception) { return }
        val p = prefs.pairing ?: return
        val np = p.withPins(id.directSpkiSha256)
        if (np != p) prefs.pairing = np
    }

    /** Rotate the device token if it is due (TTL is 90 days, sliding on every rotation). */
    suspend fun rotateIfDue() {
        val t = tokens.load() ?: return
        if (t.dueForRotation()) {
            try { requireClient().rotateToken() } catch (e: Exception) { Log.i(TAG, "rotate: $e") }
        }
    }

    // ── change feed ──────────────────────────────────────────────────────

    private var watcher: Job? = null
    @Volatile private var lastTouch = 0L

    /** Keep the change feed running while the app or the Files app uses Loft (idle stop: 10 min). */
    @Synchronized
    fun touch() {
        lastTouch = System.currentTimeMillis()
        if (watcher?.isActive == true || !isReady) return
        val c = client() ?: return
        watcher = scope.launch {
            val loop = launch {
                ChangeWatcher(c).run(prefs.anchor,
                    onChange = { page ->
                        notifyChanged(page)
                        changes.emit(page)
                    },
                    onState = {
                        loopState.value = it
                        route.value = c.selector.current
                    },
                    onAnchor = { prefs.anchor = it })
            }
            while (loop.isActive) {
                delay(30_000)
                if (System.currentTimeMillis() - lastTouch > IDLE_MS) loop.cancel()
            }
        }
    }

    @Synchronized
    fun stopWatcher() {
        watcher?.cancel()
        watcher = null
    }

    // ── DocumentsProvider notifications ─────────────────────────────────

    fun notifyRoots() {
        ctx.contentResolver.notifyChange(DocumentsContract.buildRootsUri(AUTHORITY), null)
    }

    fun notifyChildren(parentId: String?) {
        if (parentId == null) return
        ctx.contentResolver.notifyChange(DocumentsContract.buildChildDocumentsUri(AUTHORITY, parentId), null)
    }

    fun notifyChanged(page: ChangesPage?) {
        if (page == null) {
            // Everything under the authority: roots, documents, children.
            ctx.contentResolver.notifyChange(Uri.parse("content://$AUTHORITY/"), null)
            return
        }
        val parents = HashSet<String>()
        for (it in page.updated) {
            items[it.id]?.parentId?.let(parents::add)    // a move: the old folder too
            items[it.id] = it
            it.parentId?.let(parents::add)
            ctx.contentResolver.notifyChange(DocumentsContract.buildDocumentUri(AUTHORITY, it.id), null)
        }
        for (id in page.deleted) {
            val old = items.remove(id)
            if (old?.parentId != null) parents += old.parentId!! else {
                ctx.contentResolver.notifyChange(Uri.parse("content://$AUTHORITY/"), null)
            }
            prefs.removeRecent(id)
        }
        parents.forEach(::notifyChildren)
    }

    fun remember(list: Collection<Item>) = list.forEach { items[it.id] = it }

    companion object {
        const val TAG = "Loft"
        const val AUTHORITY = BuildConfig.APPLICATION_ID + ".documents"
        private const val IDLE_MS = 10 * 60_000L

        @Volatile private var inst: Loft? = null

        fun get(ctx: Context): Loft = inst ?: synchronized(this) {
            inst ?: Loft(ctx.applicationContext).also { inst = it }
        }

        fun mimeType(item: Item): String =
            if (item.isDirectory) DocumentsContract.Document.MIME_TYPE_DIR else mimeType(item.name)

        fun mimeType(name: String): String {
            val ext = name.substringAfterLast('.', "").lowercase()
            return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "application/octet-stream"
        }
    }
}
