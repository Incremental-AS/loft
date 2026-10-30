package no.incremental.loft

import android.Manifest
import android.content.ContentValues
import android.media.MediaScannerConnection
import android.net.ConnectivityManager
import android.net.Uri
import android.provider.MediaStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.ListenableWorker
import androidx.work.NetworkType
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.testing.TestListenableWorkerBuilder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.Item
import no.incremental.loft.work.CameraUploadWorker
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Camera upload: photos and videos that another app (here the shell user) put in DCIM, read with
 * the media permissions, uploaded once to the chosen folder. The worker runs for real (work-testing
 * builds it; the Wi-Fi test lets WorkManager itself schedule it).
 */
@RunWith(AndroidJUnit4::class)
class CameraUploadTest {
    private val ctx = TestKit.ctx
    private val loft = Loft.get(ctx)
    private val p = loft.prefs
    private lateinit var c: GatewayClient
    private val shellFiles = mutableListOf<String>()
    private val ownMedia = mutableListOf<Uri>()
    private val dcim = "/sdcard/DCIM/LoftTest"

    @Before fun setUp(): Unit = runBlocking {
        c = TestGateway.signedIn(loft)
        TestKit.grant(Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO,
            Manifest.permission.ACCESS_MEDIA_LOCATION)
        TestKit.shell("mkdir -p $dcim")
        TestKit.shell("mkdir -p /sdcard/Pictures/LoftTest")
    }

    @After fun tearDown() {
        TestKit.shell("svc wifi enable")
        Work.disabledForTests = false
        p.cameraUpload = false
        Work.scheduleCamera(ctx)              // cancels the camera work
        Work.disabledForTests = true
        shellFiles.forEach { TestKit.shell("rm -f $it") }
        scan(shellFiles)
        TestKit.deleteMedia(ownMedia)
        loft.forgetGateway()
    }

    /** Camera upload switched on the way Settings does it: from now on, into `folder`. */
    private fun enable(folder: Item, wifiOnly: Boolean) {
        p.cameraFolderId = folder.id
        p.cameraFolderName = folder.name
        p.cameraWifiOnly = wifiOnly
        p.startCameraLedger()
        p.cameraUpload = true
    }

    private fun scan(paths: List<String>) {
        if (paths.isEmpty()) return
        val done = CountDownLatch(paths.size)
        MediaScannerConnection.scanFile(ctx, paths.toTypedArray(), null) { _, _ -> done.countDown() }
        done.await(30, TimeUnit.SECONDS)
    }

    /** A file written by the shell user (not Loft) and scanned into MediaStore, like a camera app's. */
    private fun push(dir: String, name: String, bytes: ByteArray): String {
        val path = "$dir/$name"
        TestKit.shellWrite(path, bytes)
        shellFiles += path
        scan(listOf(path))
        return path
    }

    private suspend fun runWorker(): ListenableWorker.Result =
        TestListenableWorkerBuilder<CameraUploadWorker>(ctx).build().doWork()

    private suspend fun checkFolder(folder: Item, expected: Map<String, ByteArray>) {
        val got = TestKit.files(c, folder.id)
        assertEquals("each file once, nothing else", expected.keys.sorted(), got.map { it.name }.sorted())
        for (item in got) {
            assertArrayEquals(item.name, expected.getValue(item.name), TestKit.content(c, item))
            TestKit.expectOnDisk("${folder.name}/${item.name}", expected.getValue(item.name))
        }
    }

    @Test fun newPhotosAndVideosUploadOnce(): Unit = runBlocking {
        val folder = TestKit.freshFolder(c, "camera")
        enable(folder, wifiOnly = false)
        delay(1100)                                          // DATE_ADDED is in seconds
        val s = System.currentTimeMillis()
        val photo = "IMG_$s.jpg" to TestKit.jpeg(31)
        val video = "VID_$s.mp4" to TestKit.mp4()
        push(dcim, photo.first, photo.second)
        push(dcim, video.first, video.second)
        push("/sdcard/Pictures/LoftTest", "Screenshot_$s.jpg", TestKit.jpeg(32))   // not DCIM: stays home

        assertEquals(ListenableWorker.Result.success(), runWorker())
        checkFolder(folder, mapOf(photo, video))

        // Nothing new: a second run uploads nothing again.
        assertEquals(ListenableWorker.Result.success(), runWorker())
        checkFolder(folder, mapOf(photo, video))

        // One more photo: only that one goes.
        delay(1100)
        val photo2 = "IMG_${s}_2.jpg" to TestKit.jpeg(33)
        push(dcim, photo2.first, photo2.second)
        assertEquals(ListenableWorker.Result.success(), runWorker())
        assertEquals(ListenableWorker.Result.success(), runWorker())
        checkFolder(folder, mapOf(photo, video, photo2))
    }

    @Test fun photosTakenBeforeSwitchingOnStayHome(): Unit = runBlocking {
        val folder = TestKit.freshFolder(c, "camera-before")
        val s = System.currentTimeMillis()
        push(dcim, "IMG_${s}_old.jpg", TestKit.jpeg(41))
        delay(1100)
        enable(folder, wifiOnly = false)
        assertEquals(ListenableWorker.Result.success(), runWorker())
        checkFolder(folder, emptyMap())
    }

    /**
     * A video still being recorded (IS_PENDING) when a later photo is uploaded must still go up once
     * it is finished: the watermark must not jump past it.
     */
    @Test fun aVideoFinishedAfterALaterPhotoStillUploads(): Unit = runBlocking {
        val folder = TestKit.freshFolder(c, "camera-pending")
        enable(folder, wifiOnly = false)
        delay(1100)
        val s = System.currentTimeMillis()
        val r = ctx.contentResolver
        val video = "VID_${s}_long.mp4" to TestKit.mp4()
        val vUri = r.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, video.first)
            put(MediaStore.MediaColumns.MIME_TYPE, "video/mp4")
            put(MediaStore.MediaColumns.RELATIVE_PATH, "DCIM/LoftTest")
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        })!!.also { ownMedia += it }
        r.openOutputStream(vUri)!!.use { it.write(video.second) }
        delay(2100)
        val photo = "IMG_${s}_during.jpg" to TestKit.jpeg(61)
        push(dcim, photo.first, photo.second)

        assertEquals(ListenableWorker.Result.success(), runWorker())
        checkFolder(folder, mapOf(photo))

        delay(1100)
        r.update(vUri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
        // The evidence: the video keeps the DATE_ADDED of its start, older than the photo's.
        val added = { uri: Uri -> r.query(uri, arrayOf(MediaStore.MediaColumns.DATE_ADDED), null, null, null)!!
            .use { it.moveToFirst(); it.getLong(0) } }
        val photoAdded = r.query(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, arrayOf(MediaStore.MediaColumns.DATE_ADDED),
            "${MediaStore.MediaColumns.DISPLAY_NAME} = ?", arrayOf(photo.first), null)!!.use { it.moveToFirst(); it.getLong(0) }
        TestKit.log("pending video DATE_ADDED ${added(vUri)}, later photo DATE_ADDED $photoAdded")
        assertTrue(added(vUri) < photoAdded)

        assertEquals(ListenableWorker.Result.success(), runWorker())
        assertEquals(ListenableWorker.Result.success(), runWorker())
        checkFolder(folder, mapOf(photo, video))
    }

    /** The periodic job and the MediaStore trigger can run at the same time: still one copy each. */
    @Test fun twoRunsAtOnceUploadEachFileOnce(): Unit = runBlocking {
        val folder = TestKit.freshFolder(c, "camera-twice")
        enable(folder, wifiOnly = false)
        delay(1100)
        val s = System.currentTimeMillis()
        val files = (1..3).associate { "IMG_${s}_$it.jpg" to TestKit.jpeg(80 + it, 2400, 1800) }
        files.forEach { (n, b) -> push(dcim, n, b) }
        val results = listOf(async(Dispatchers.IO) { runWorker() }, async(Dispatchers.IO) { runWorker() }).awaitAll()
        results.forEach { assertEquals(ListenableWorker.Result.success(), it) }
        checkFolder(folder, files)
    }

    private fun metered(): Boolean = ctx.getSystemService(ConnectivityManager::class.java).isActiveNetworkMetered

    private suspend fun wifi(on: Boolean) {
        TestKit.shell("svc wifi ${if (on) "enable" else "disable"}")
        TestKit.waitFor("Wi-Fi ${if (on) "on" else "off"}", 30_000) { (metered() != on).takeIf { it } }
        delay(2000)
    }

    private fun cameraWork(): List<WorkInfo> = listOf("camera-upload", "camera-upload-trigger").flatMap {
        WorkManager.getInstance(ctx).getWorkInfosForUniqueWork(it).get()
    }.filter { !it.state.isFinished }

    /**
     * "Only on Wi-Fi": on a metered network (the emulator's mobile data, Wi-Fi switched off) nothing
     * goes; back on Wi-Fi it does. Switched off, the next photo goes over mobile data. Real WorkManager
     * scheduling, real network changes.
     */
    @Test fun wifiOnlyWaitsForAnUnmeteredNetwork(): Unit = runBlocking {
        val folder = TestKit.freshFolder(c, "camera-wifi")
        enable(folder, wifiOnly = true)
        Work.disabledForTests = false
        try {
            wifi(false)
            assertTrue("mobile data is metered", metered())
            Work.scheduleCamera(ctx)
            val work = cameraWork()
            assertEquals(2, work.size)
            work.forEach { assertEquals(NetworkType.UNMETERED, it.constraints.requiredNetworkType) }

            delay(1100)
            val s = System.currentTimeMillis()
            val photo = "IMG_${s}_wifi.jpg" to TestKit.jpeg(71)
            push(dcim, photo.first, photo.second)
            delay(25_000)
            checkFolder(folder, emptyMap())                    // still waiting for Wi-Fi

            wifi(true)
            TestKit.waitFor("the photo over Wi-Fi", 120_000) {
                TestKit.files(c, folder.id).takeIf { it.isNotEmpty() }
            }
            delay(10_000)                                      // both jobs (periodic + trigger) have had their go
            checkFolder(folder, mapOf(photo))

            // Wi-Fi only off: the next photo goes over mobile data.
            p.cameraWifiOnly = false
            Work.scheduleCamera(ctx)
            cameraWork().forEach { assertEquals(NetworkType.CONNECTED, it.constraints.requiredNetworkType) }
            wifi(false)
            delay(1100)
            val photo2 = "IMG_${s}_mobile.jpg" to TestKit.jpeg(72)
            push(dcim, photo2.first, photo2.second)
            TestKit.waitFor("the photo over mobile data", 120_000) {
                TestKit.files(c, folder.id).takeIf { it.size >= 2 }
            }
            delay(5_000)
            checkFolder(folder, mapOf(photo, photo2))
        } finally {
            Work.disabledForTests = true
        }
    }
}
