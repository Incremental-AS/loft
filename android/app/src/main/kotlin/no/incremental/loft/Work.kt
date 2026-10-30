// WorkManager scheduling in one place: uploads, camera upload, the update check and the
// weekly token rotation.
package no.incremental.loft

import android.content.Context
import android.provider.MediaStore
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.workDataOf
import no.incremental.loft.work.CameraUploadWorker
import no.incremental.loft.work.MaintenanceWorker
import no.incremental.loft.work.UpdateCheckWorker
import no.incremental.loft.work.UploadWorker
import java.io.File
import java.util.UUID
import java.util.concurrent.TimeUnit

object Work {
    /** Set by the instrumented test runner: no update check (and no other periodic work) in tests. */
    @Volatile var disabledForTests = false

    private const val UPDATE = "update-check"
    private const val MAINTENANCE = "maintenance"
    private const val CAMERA_PERIODIC = "camera-upload"
    private const val CAMERA_TRIGGER = "camera-upload-trigger"

    private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    /** Called from Application.onCreate and when a setting changes. */
    fun schedulePeriodic(ctx: Context) {
        if (disabledForTests) return
        val wm = WorkManager.getInstance(ctx)
        val loft = Loft.get(ctx)
        if (loft.prefs.updateCheck) {
            wm.enqueueUniquePeriodicWork(UPDATE, ExistingPeriodicWorkPolicy.KEEP,
                PeriodicWorkRequestBuilder<UpdateCheckWorker>(1, TimeUnit.DAYS).setConstraints(online)
                    .setInitialDelay(1, TimeUnit.HOURS).build())
        } else {
            wm.cancelUniqueWork(UPDATE)
        }
        if (loft.isSignedIn) {
            wm.enqueueUniquePeriodicWork(MAINTENANCE, ExistingPeriodicWorkPolicy.KEEP,
                PeriodicWorkRequestBuilder<MaintenanceWorker>(1, TimeUnit.DAYS).setConstraints(online).build())
        }
        scheduleCamera(ctx)
    }

    fun scheduleCamera(ctx: Context) {
        if (disabledForTests) return
        val wm = WorkManager.getInstance(ctx)
        val p = Loft.get(ctx).prefs
        if (!p.cameraUpload || p.cameraFolderId == null) {
            wm.cancelUniqueWork(CAMERA_PERIODIC)
            wm.cancelUniqueWork(CAMERA_TRIGGER)
            return
        }
        val c = cameraConstraints(ctx)
        wm.enqueueUniquePeriodicWork(CAMERA_PERIODIC, ExistingPeriodicWorkPolicy.UPDATE,
            PeriodicWorkRequestBuilder<CameraUploadWorker>(1, TimeUnit.HOURS).setConstraints(c).build())
        enqueueCameraTrigger(ctx)
    }

    /** Runs as soon as MediaStore changes (a new photo), then re-arms itself. */
    fun enqueueCameraTrigger(ctx: Context) {
        if (disabledForTests) return
        val base = cameraConstraints(ctx)
        val c = Constraints.Builder()
            .setRequiredNetworkType(base.requiredNetworkType)
            .addContentUriTrigger(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, true)
            .addContentUriTrigger(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, true)
            .setTriggerContentUpdateDelay(5, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(ctx).enqueueUniqueWork(CAMERA_TRIGGER, ExistingWorkPolicy.REPLACE,
            OneTimeWorkRequestBuilder<CameraUploadWorker>().setConstraints(c)
                .setInputData(workDataOf(CameraUploadWorker.KEY_REARM to true)).build())
    }

    private fun cameraConstraints(ctx: Context): Constraints = Constraints.Builder()
        .setRequiredNetworkType(if (Loft.get(ctx).prefs.cameraWifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
        .build()

    fun cancelAccountWork(ctx: Context) {
        val wm = WorkManager.getInstance(ctx)
        wm.cancelUniqueWork(CAMERA_PERIODIC)
        wm.cancelUniqueWork(CAMERA_TRIGGER)
        wm.cancelUniqueWork(MAINTENANCE)
        wm.cancelAllWorkByTag(UploadWorker.TAG)
    }

    /**
     * Upload a file from the app's cache: a new file in `parentId` named `name`, or new
     * content for `itemId` based on `ifMatch`. Uploads to one item run in order.
     * Returns the work id.
     */
    fun enqueueUpload(
        ctx: Context, file: File, parentId: String? = null, name: String? = null,
        itemId: String? = null, ifMatch: String? = null,
    ): UUID {
        val req = OneTimeWorkRequestBuilder<UploadWorker>()
            .setConstraints(online)
            .addTag(UploadWorker.TAG)
            .setInputData(workDataOf(
                UploadWorker.KEY_PATH to file.absolutePath,
                UploadWorker.KEY_PARENT to parentId,
                UploadWorker.KEY_NAME to name,
                UploadWorker.KEY_ITEM to itemId,
                UploadWorker.KEY_IF_MATCH to ifMatch,
            ))
            .build()
        val unique = "upload-" + (itemId ?: "$parentId/$name")
        WorkManager.getInstance(ctx).enqueueUniqueWork(unique, ExistingWorkPolicy.APPEND_OR_REPLACE, req)
        return req.id
    }

    /**
     * Where files wait for upload: app-private storage, never shared storage. Not the cache
     * dir: the system may clear that under storage pressure, and these are unsaved changes.
     */
    fun stagingDir(ctx: Context): File = File(ctx.filesDir, "outbox").apply { mkdirs() }
}
