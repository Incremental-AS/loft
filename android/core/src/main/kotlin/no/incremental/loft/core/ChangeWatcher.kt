// The change feed loop (docs/design.md §D.2, LoftCore.ChangeWatcher): long-poll /v1/changes
// and tell the platform side when something moved.
package no.incremental.loft.core

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlin.coroutines.coroutineContext

class ChangeWatcher(
    private val client: GatewayClient,
    var waitS: Int = 25,                      // the gateway caps at 25 s (Cloudflare: 100)
    var maxBackoffMs: Long = 60_000,
) {
    /**
     * Runs until cancelled. `onChange(page)` fires with every non-empty page, and with null
     * when the anchor was (re)started, which means "re-read everything". `onState` after every
     * round. `onAnchor` lets the caller persist the anchor.
     */
    suspend fun run(
        start: String?,
        onChange: suspend (ChangesPage?) -> Unit,
        onState: suspend (LoopState) -> Unit = {},
        onAnchor: (String) -> Unit = {},
    ) {
        var anchor = start
        var backoff = 2_000L
        while (true) {
            coroutineContext.ensureActive()
            try {
                if (anchor == null) {
                    anchor = client.latestAnchor()
                    onAnchor(anchor)
                    onChange(null)                    // we missed whatever happened before
                }
                val page = client.changes(anchor, waitS)
                anchor = page.anchor
                onAnchor(page.anchor)
                if (!page.isEmpty) onChange(page)
                onState(LoopState.Ok)
                backoff = 2_000L
            } catch (c: CancellationException) {
                throw c
            } catch (e: GatewayException.Api) {
                if (e.error.isAnchorExpired) {
                    anchor = null                     // epoch changed / pruned: start over
                    continue
                }
                onState(LoopState.of(e))
                delay(backoff)
                backoff = minOf(backoff * 2, maxBackoffMs)
            } catch (e: Exception) {
                onState(LoopState.of(e))
                delay(backoff)
                backoff = minOf(backoff * 2, maxBackoffMs)
            }
        }
    }
}
