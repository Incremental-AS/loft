// One staged file -> the gateway: chunked, resumable (docs/design.md §D.5). Retries while home
// can't be reached; a CAS failure on the gateway keeps both copies, so nothing is lost.
package no.incremental.loft.work

import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import no.incremental.loft.Loft
import no.incremental.loft.R
import no.incremental.loft.core.CommitRequest
import no.incremental.loft.core.CommitResponse
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.GatewayException
import java.io.File

class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val loft = Loft.get(applicationContext)
        val path = inputData.getString(KEY_PATH) ?: return Result.failure()
        val file = File(path)
        if (!file.isFile) return Result.failure()
        val itemId = inputData.getString(KEY_ITEM)
        val parentId = inputData.getString(KEY_PARENT)
        val name = inputData.getString(KEY_NAME)
        val label = name ?: loft.items[itemId]?.name ?: file.name
        val client = loft.client()
        if (client == null || !loft.isSignedIn || (itemId == null && (parentId == null || name == null))) {
            file.delete()
            return Result.failure()
        }
        if (file.length() > FOREGROUND_ABOVE) {
            try {
                setForeground(foreground(label))
            } catch (e: Exception) {
                Log.i(Loft.TAG, "upload in the background without a foreground service: $e")
            }
        }
        return try {
            val r = upload(loft, client, file, itemId, inputData.getString(KEY_IF_MATCH), parentId, name)
            loft.items[r.item.id] = r.item
            loft.prefs.addRecent(r.item)
            loft.notifyChildren(r.item.parentId)
            file.delete()
            Result.success(workDataOf(KEY_RESULT_ID to r.item.id, KEY_RESULT to r.result))
        } catch (e: GatewayException.Unreachable) {
            if (runAttemptCount < MAX_ATTEMPTS) Result.retry() else fail(file, label)
        } catch (e: GatewayException.Api) {
            // NAS down, or a sign-in that has to be renewed: keep the file and try again later.
            if ((e.error.isNasUnreachable || e.error.needsSignIn) && runAttemptCount < MAX_ATTEMPTS) Result.retry()
            else {
                Log.w(Loft.TAG, "upload refused: ${e.error.status} ${e.error.error}")
                fail(file, label)
            }
        } catch (e: GatewayException) {
            Log.w(Loft.TAG, "upload: $e")
            fail(file, label)
        }
    }

    private suspend fun upload(
        loft: Loft, client: GatewayClient, file: File, itemId: String?, ifMatch: String?,
        parentId: String?, name: String?,
    ): CommitResponse {
        val req = if (itemId != null) CommitRequest(itemId = itemId, ifMatch = ifMatch)
        else CommitRequest(parentId = parentId, name = name)
        return client.uploadAndCommit(file, req, loft.prefs.resumeStore) { done, total ->
            if (total > 0) setProgressAsync(workDataOf(KEY_PROGRESS to (done * 100 / total).toInt()))
        }
    }

    private fun fail(file: File, label: String): Result {
        Notifications.uploadFailed(applicationContext, label)
        file.delete()
        return Result.failure()
    }

    private fun foreground(label: String): ForegroundInfo {
        val n = Notifications.transfer(applicationContext, applicationContext.getString(R.string.uploading_file, label))
        return if (Build.VERSION.SDK_INT >= 29) {
            ForegroundInfo(Notifications.ID_TRANSFER, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            ForegroundInfo(Notifications.ID_TRANSFER, n)
        }
    }

    override suspend fun getForegroundInfo(): ForegroundInfo =
        foreground(inputData.getString(KEY_NAME) ?: "")

    companion object {
        const val TAG = "loft-upload"
        const val KEY_PATH = "path"
        const val KEY_PARENT = "parent_id"
        const val KEY_NAME = "name"
        const val KEY_ITEM = "item_id"
        const val KEY_IF_MATCH = "if_match"
        const val KEY_RESULT_ID = "result_item_id"
        const val KEY_RESULT = "result"
        const val KEY_PROGRESS = "progress"
        private const val MAX_ATTEMPTS = 30
        private const val FOREGROUND_ABOVE = 16L shl 20
    }
}
