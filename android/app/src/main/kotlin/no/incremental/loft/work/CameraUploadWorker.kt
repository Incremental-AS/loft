// Camera upload (opt-in): new photos and videos in DCIM go to the chosen Loft folder, oldest
// first, one at a time; CameraLedger records each one as it lands, so none goes twice.
package no.incremental.loft.work

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import androidx.core.content.ContextCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import no.incremental.loft.Loft
import no.incremental.loft.R
import no.incremental.loft.Work
import no.incremental.loft.data.AppPrefs
import no.incremental.loft.core.CommitRequest
import no.incremental.loft.core.Exclusions
import no.incremental.loft.core.GatewayException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.File

class CameraUploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    private data class Media(val uri: Uri, val id: Long, val name: String, val added: Long, val image: Boolean)

    // The hourly job and the MediaStore trigger are two WorkManager jobs that may run at once; both
    // would read the same ledger and upload the same photo twice (the second lands as a conflict copy).
    override suspend fun doWork(): Result = oneAtATime.withLock { upload() }

    private suspend fun upload(): Result {
        val loft = Loft.get(applicationContext)
        val p = loft.prefs
        val folder = p.cameraFolderId
        try {
            if (!p.cameraUpload || folder == null || !loft.isReady || !canRead()) return Result.success()
            val client = loft.client() ?: return Result.success()
            val ledger = p.cameraLedger
            val todo = newMedia(ledger)
            if (todo.isEmpty()) return Result.success()
            try {
                setForeground(foregroundInfo())
            } catch (e: Exception) {
                Log.i(Loft.TAG, "camera upload without a foreground service: $e")
            }
            for (m in todo) {
                val staged = File(Work.stagingDir(applicationContext), "camera-${m.id}")
                try {
                    val src = if (m.image && Build.VERSION.SDK_INT >= 29 && granted(Manifest.permission.ACCESS_MEDIA_LOCATION)) {
                        MediaStore.setRequireOriginal(m.uri)     // keep the photo's location data
                    } else m.uri
                    val input = applicationContext.contentResolver.openInputStream(src)
                    if (input == null) {
                        done(p, ledger, m)
                        continue
                    }
                    input.use { i -> staged.outputStream().use { o -> i.copyTo(o) } }
                    val r = client.uploadAndCommit(staged, CommitRequest(parentId = folder, name = m.name), p.resumeStore)
                    loft.items[r.item.id] = r.item
                    loft.notifyChildren(folder)
                    done(p, ledger, m)
                } catch (e: SecurityException) {
                    done(p, ledger, m)                             // no longer readable: skip it
                } catch (e: java.io.FileNotFoundException) {
                    done(p, ledger, m)                             // deleted since the query
                } finally {
                    staged.delete()
                }
            }
            return Result.success()
        } catch (e: GatewayException.Unreachable) {
            return Result.retry()
        } catch (e: GatewayException.Api) {
            Log.w(Loft.TAG, "camera upload: ${e.error.status} ${e.error.error}")
            return if (e.error.isNasUnreachable) Result.retry() else Result.success()
        } catch (e: GatewayException) {
            Log.w(Loft.TAG, "camera upload: $e")
            return Result.success()
        } finally {
            if (inputData.getBoolean(KEY_REARM, false)) Work.enqueueCameraTrigger(applicationContext)
        }
    }

    /** Recorded at once, item by item, so a stopped run never uploads the same item twice. */
    private fun done(p: AppPrefs, ledger: CameraLedger, m: Media) {
        ledger.mark(m.id, m.added)
        p.cameraLedger = ledger
    }

    private fun granted(perm: String) =
        ContextCompat.checkSelfPermission(applicationContext, perm) == PackageManager.PERMISSION_GRANTED

    private fun canRead(): Boolean = if (Build.VERSION.SDK_INT >= 33) {
        granted(Manifest.permission.READ_MEDIA_IMAGES) || granted(Manifest.permission.READ_MEDIA_VIDEO) ||
            (Build.VERSION.SDK_INT >= 34 && granted(Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED))
    } else granted(Manifest.permission.READ_EXTERNAL_STORAGE)

    private fun newMedia(ledger: CameraLedger): List<Media> {
        val since = ledger.from
        val out = mutableListOf<Media>()
        for ((collection, image) in listOf(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI to true,
            MediaStore.Video.Media.EXTERNAL_CONTENT_URI to false,
        )) {
            val sel = StringBuilder("${MediaStore.MediaColumns.DATE_ADDED} >= ?")
            val args = mutableListOf(since.toString())
            if (Build.VERSION.SDK_INT >= 29) {
                sel.append(" AND ${MediaStore.MediaColumns.RELATIVE_PATH} LIKE ? AND ${MediaStore.MediaColumns.IS_PENDING} = 0")
                args += "DCIM/%"
            } else {
                @Suppress("DEPRECATION")
                sel.append(" AND ${MediaStore.MediaColumns.DATA} LIKE ?")
                args += "%/DCIM/%"
            }
            applicationContext.contentResolver.query(collection,
                arrayOf(MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.DATE_ADDED),
                sel.toString(), args.toTypedArray(), null)?.use { c ->
                while (c.moveToNext()) {
                    val id = c.getLong(0)
                    val name = c.getString(1) ?: continue
                    val added = c.getLong(2)
                    if (ledger.isDone(id)) continue
                    if (Exclusions.isExcluded(name)) continue
                    out += Media(ContentUris.withAppendedId(collection, id), id, name, added, image)
                }
            }
        }
        return out.sortedWith(compareBy({ it.added }, { it.id }))
    }

    private fun foregroundInfo(): ForegroundInfo {
        val n = Notifications.transfer(applicationContext, applicationContext.getString(R.string.camera_uploading))
        return if (Build.VERSION.SDK_INT >= 29) {
            ForegroundInfo(Notifications.ID_TRANSFER + 1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else ForegroundInfo(Notifications.ID_TRANSFER + 1, n)
    }

    override suspend fun getForegroundInfo(): ForegroundInfo = foregroundInfo()

    companion object {
        const val KEY_REARM = "rearm"
        private val oneAtATime = Mutex()
    }
}
