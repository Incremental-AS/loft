package no.incremental.loft.ui

import android.app.Application
import android.content.Context
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.OpenableColumns
import android.util.LruCache
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import no.incremental.loft.Loft
import no.incremental.loft.R
import no.incremental.loft.Work
import no.incremental.loft.core.GatewayException
import no.incremental.loft.core.Item
import no.incremental.loft.core.LoopState
import no.incremental.loft.core.Pairing
import no.incremental.loft.core.PairingException
import no.incremental.loft.core.StoredToken
import no.incremental.loft.core.SyncError
import java.io.File
import java.util.UUID

enum class Screen { PAIR, SCAN, SIGN_IN, CHOOSE_ROOT, BROWSE, SETTINGS, CAMERA_FOLDER }

data class Folder(val id: String, val name: String)

class MainViewModel(app: Application) : AndroidViewModel(app) {
    val loft = Loft.get(app)
    private val ctx: Context get() = getApplication()

    var screen by mutableStateOf(homeScreen())
    var pairError by mutableStateOf<String?>(null)
    var signInError by mutableStateOf<String?>(null)
    var busy by mutableStateOf(false)
    var snack by mutableStateOf<String?>(null)

    // Browse state
    var stack by mutableStateOf(listOf<Folder>())
    var items by mutableStateOf<List<Item>>(emptyList())
    var loading by mutableStateOf(false)
    var browseError by mutableStateOf<String?>(null)
    private val thumbs = LruCache<String, ImageBitmap>(200)

    init {
        viewModelScope.launch {
            loft.changes.collect { page ->
                val cur = stack.lastOrNull()?.id ?: return@collect
                if (page == null || page.updated.any { it.parentId == cur } ||
                    page.deleted.any { id -> items.any { it.id == id } }) refresh()
            }
        }
    }

    fun homeScreen(): Screen = when {
        !loft.isPaired -> Screen.PAIR
        !loft.isSignedIn -> Screen.SIGN_IN
        loft.prefs.root == null -> Screen.CHOOSE_ROOT
        else -> Screen.BROWSE
    }

    fun goHome() {
        screen = homeScreen()
        if (screen == Screen.BROWSE) {
            if (stack.isEmpty() || stack.first().id != loft.prefs.root?.rootItemId) openRoot()
        }
    }

    // ── pairing ──────────────────────────────────────────────────────────

    /** A pairing link opened while signed in to another gateway: asks first. */
    var pendingPairing by mutableStateOf<Pairing?>(null)

    /** The App Link: re-pairing the same gateway just updates it; another gateway asks first. */
    fun openPairingLink(text: String) {
        val p = try {
            Pairing.parse(text)
        } catch (e: PairingException) {
            if (!loft.isSignedIn) acceptPairing(text) else snack = pairingMessage(e)
            return
        }
        val cur = loft.prefs.pairing
        when {
            cur != null && loft.isSignedIn && cur.lanUrl == p.lanUrl -> loft.prefs.pairing = p
            loft.isSignedIn -> pendingPairing = p
            else -> acceptPairing(text)
        }
    }

    fun confirmPendingPairing() {
        pendingPairing?.let { loft.pair(it) }
        pendingPairing = null
        stack = emptyList()
        items = emptyList()
        screen = Screen.SIGN_IN
    }

    /** A scanned code, an opened link or a pasted text. true = paired. */
    fun acceptPairing(text: String): Boolean = try {
        loft.pair(Pairing.parse(text))
        pairError = null
        signInError = null
        screen = Screen.SIGN_IN
        true
    } catch (e: PairingException) {
        pairError = pairingMessage(e)
        false
    }

    fun manualPairing(address: String, pin: String, name: String): Boolean = try {
        loft.pair(Pairing.manual(address, pin, name))
        pairError = null
        screen = Screen.SIGN_IN
        true
    } catch (e: PairingException) {
        pairError = pairingMessage(e)
        false
    }

    private fun pairingMessage(e: PairingException): String = ctx.getString(when (e.reason) {
        PairingException.Reason.NOT_A_LOFT_CODE -> R.string.pair_err_not_loft
        PairingException.Reason.UNKNOWN_VERSION -> R.string.pair_err_version
        PairingException.Reason.NOT_HTTPS -> R.string.pair_err_https
        PairingException.Reason.BAD_PIN -> R.string.pair_err_pin
        PairingException.Reason.NO_PINS -> R.string.pair_err_no_pin
        PairingException.Reason.MISSING_FIELD -> if (e.message?.contains("address") == true) R.string.pair_err_address else R.string.pair_err_invalid
        else -> R.string.pair_err_invalid
    })

    fun forgetGateway() {
        loft.forgetGateway()
        stack = emptyList()
        items = emptyList()
        screen = Screen.PAIR
    }

    // ── sign in ─────────────────────────────────────────────────────────

    fun signIn(user: String, password: String) {
        if (busy) return
        busy = true
        signInError = null
        viewModelScope.launch {
            try {
                val c = loft.requireClient()
                // Pinned routes only: home, or an outside address in `direct` mode.
                val ep = c.signInEndpoint()
                if (ep == null) {
                    signInError = ctx.getString(R.string.signin_not_home)
                    return@launch
                }
                val r = c.login(ep, user.trim(), password, loft.deviceName())
                loft.tokens.save(StoredToken.of(r))
                loft.prefs.nasUser = r.nasUser ?: user.trim()
                val me = c.me()
                loft.prefs.deviceName = me.deviceName
                loft.prefs.root = me.root
                loft.refreshPins()
                Work.schedulePeriodic(ctx)
                loft.notifyRoots()
                goHome()
            } catch (e: GatewayException.Api) {
                signInError = ctx.getString(when (e.error.error) {
                    "invalid_credentials", "missing_credentials" -> R.string.signin_wrong_password
                    "account_not_allowed" -> R.string.signin_not_allowed
                    "too_many_attempts" -> R.string.signin_too_many
                    "nas_unavailable", "nas_error" -> R.string.signin_nas_down
                    "password_login_direct_only" -> R.string.signin_not_home
                    else -> R.string.signin_failed
                })
            } catch (e: GatewayException.Unreachable) {
                signInError = ctx.getString(R.string.signin_not_home)
            } catch (e: Exception) {
                signInError = ctx.getString(R.string.signin_failed)
            } finally {
                busy = false
            }
        }
    }

    fun signOut() {
        viewModelScope.launch {
            loft.signOut()
            stack = emptyList()
            items = emptyList()
            screen = Screen.SIGN_IN
        }
    }

    // ── root ─────────────────────────────────────────────────────────────

    fun rootChosen() {
        loft.prefs.anchor = null
        loft.items.clear()
        loft.stopWatcher()
        loft.notifyRoots()
        stack = emptyList()
        goHome()
    }

    // ── browse ───────────────────────────────────────────────────────────

    fun rootName(): String {
        val r = loft.prefs.root ?: return ""
        return if (r.isAllShares) ctx.getString(R.string.all_shares) else r.displayPath.substringAfterLast('/')
    }

    fun openRoot() {
        val r = loft.prefs.root ?: return
        stack = listOf(Folder(r.rootItemId, rootName()))
        items = emptyList()
        refresh()
    }

    fun enter(item: Item) {
        stack = stack + Folder(item.id, item.name)
        items = emptyList()
        refresh()
    }

    fun up(): Boolean {
        if (stack.size <= 1) return false
        stack = stack.dropLast(1)
        items = emptyList()
        refresh()
        return true
    }

    fun refresh() {
        val cur = stack.lastOrNull() ?: return
        loft.touch()
        loading = true
        viewModelScope.launch {
            try {
                val c = loft.requireClient()
                val list = c.allChildren(cur.id)
                loft.remember(list)
                loft.route.value = c.selector.current
                if (stack.lastOrNull()?.id == cur.id) {
                    items = list.sortedWith(compareBy<Item>({ !it.isDirectory }, { it.name.lowercase() }))
                    browseError = null
                }
            } catch (e: Exception) {
                browseError = describe(e)
            } finally {
                loading = false
            }
        }
    }

    /** The status line under the title: why nothing loads, in words. */
    fun describe(e: Throwable): String = when (val s = LoopState.of(e)) {
        LoopState.CantReachHome -> ctx.getString(R.string.cant_reach_home)
        LoopState.CantReachNas -> ctx.getString(R.string.cant_reach_nas)
        is LoopState.Failed -> ctx.getString(when (s.error) {
            SyncError.NasPasswordChanged -> R.string.nas_password_changed
            SyncError.SignInExpired -> R.string.sign_in_expired
            SyncError.DeviceRemoved -> R.string.device_removed
            SyncError.NoAccess -> R.string.no_access
            is SyncError.Stopped -> R.string.action_failed
        })
        LoopState.Ok -> ctx.getString(R.string.action_failed)
    }

    fun needsSignIn(): Boolean = loft.loopState.value.let { it is LoopState.Failed && it.error.needsSignIn }

    private fun act(block: suspend () -> Unit) {
        viewModelScope.launch {
            try {
                block()
                refresh()
            } catch (e: GatewayException.Api) {
                snack = if (e.error.status == 409) ctx.getString(R.string.name_exists) else describe(e)
            } catch (e: Exception) {
                snack = describe(e)
            }
        }
    }

    fun mkdir(name: String) {
        val cur = stack.lastOrNull() ?: return
        act { loft.requireClient().mkdir(cur.id, name.trim()) }
    }

    fun rename(item: Item, name: String) = act { loft.requireClient().patch(item.id, name = name.trim()) }

    fun delete(item: Item) = act {
        loft.requireClient().delete(item.id)
        loft.prefs.removeRecent(item.id)
    }

    /** Files picked with the system picker or shared to Loft: stage, then upload in the background. */
    fun upload(uris: List<Uri>, parentId: String? = stack.lastOrNull()?.id) {
        if (parentId == null || uris.isEmpty()) return
        viewModelScope.launch {
            val n = stageAndUpload(ctx, uris, parentId)
            if (n > 0) snack = ctx.resources.getQuantityString(R.plurals.uploading_n, n, n)
        }
    }

    suspend fun thumbnail(item: Item): ImageBitmap? {
        val key = "${item.id}-${item.contentEtag}"
        thumbs.get(key)?.let { return it }
        val bytes = try {
            loft.requireClient().thumbnail(item.id, 128)
        } catch (e: Exception) {
            null
        } ?: return null
        val bmp = withContext(Dispatchers.Default) {
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
        } ?: return null
        thumbs.put(key, bmp)
        return bmp
    }

    companion object {
        /** Copy content URIs into app storage and queue one upload each. Returns how many were queued. */
        suspend fun stageAndUpload(ctx: Context, uris: List<Uri>, parentId: String): Int = withContext(Dispatchers.IO) {
            var n = 0
            for (uri in uris) {
                try {
                    val name = displayName(ctx, uri) ?: "file-${System.currentTimeMillis()}"
                    val f = File(Work.stagingDir(ctx), "share-${UUID.randomUUID()}")
                    val input = ctx.contentResolver.openInputStream(uri) ?: continue
                    input.use { i -> f.outputStream().use { o -> i.copyTo(o) } }
                    Work.enqueueUpload(ctx, f, parentId = parentId, name = name)
                    n++
                } catch (e: Exception) {
                    android.util.Log.w(Loft.TAG, "stage $uri: $e")
                }
            }
            n
        }

        fun displayName(ctx: Context, uri: Uri): String? {
            ctx.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0)?.let { return it.substringAfterLast('/') }
            }
            return uri.lastPathSegment?.substringAfterLast('/')
        }
    }
}
