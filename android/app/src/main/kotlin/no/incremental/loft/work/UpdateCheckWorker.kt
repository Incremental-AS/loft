// Daily check of https://loft.incremental.no/releases.json (docs/design.md §E.4, docs/releases-json.md).
// The only request Loft makes to Incremental; off when the user switches it off. The instrumented
// tests never schedule it (Work.disabledForTests); UpdateCheckWorkerTest runs it against a local server.
package no.incremental.loft.work

import android.content.Context
import android.util.Log
import androidx.annotation.VisibleForTesting
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import no.incremental.loft.BuildConfig
import no.incremental.loft.Loft
import no.incremental.loft.core.HttpClients
import no.incremental.loft.core.UpdateCheck
import okhttp3.OkHttpClient
import okhttp3.Request

class UpdateCheckWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val prefs = Loft.get(applicationContext).prefs
        if (!prefs.updateCheck) return Result.success()
        val (url, http) = endpointForTests ?: (UpdateCheck.RELEASES_URL to HttpClients.base())
        val body = try {
            withContext(Dispatchers.IO) {
                val req = Request.Builder().url(url)
                    .header("User-Agent", "Loft-Android/${BuildConfig.VERSION_NAME}")
                    .build()
                http.newCall(req).execute().use { r ->
                    if (!r.isSuccessful) null else r.body?.string()
                }
            }
        } catch (e: Exception) {
            Log.i(Loft.TAG, "update check: $e")
            return Result.success()          // try again tomorrow, no retry storm
        } ?: return Result.success()
        val update = UpdateCheck.parse(body, BuildConfig.VERSION_CODE.toLong()) ?: return Result.success()
        if (update.versionCode > prefs.notifiedVersionCode) {
            Notifications.updateAvailable(applicationContext, update.version, update.pageUrl)
            prefs.notifiedVersionCode = update.versionCode
        }
        return Result.success()
    }

    companion object {
        /** Tests only: where releases.json comes from instead of loft.incremental.no, and the client for it. */
        @VisibleForTesting
        @Volatile
        internal var endpointForTests: Pair<String, OkHttpClient>? = null
    }
}
