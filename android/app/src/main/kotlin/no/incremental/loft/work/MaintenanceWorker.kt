// Daily: rotate the device token when it is a week old (the gateway's 90-day TTL slides on
// every rotation) and refresh the key pins from /v1/identity. Talks to the user's own gateway only.
package no.incremental.loft.work

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import no.incremental.loft.Loft

class MaintenanceWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val loft = Loft.get(applicationContext)
        if (!loft.isSignedIn) return Result.success()
        loft.rotateIfDue()
        loft.refreshPins()
        return Result.success()
    }
}
