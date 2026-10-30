package no.incremental.loft

import android.Manifest
import android.app.NotificationManager
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.ListenableWorker
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.testing.TestListenableWorkerBuilder
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import no.incremental.loft.core.HttpClients
import no.incremental.loft.core.UpdateCheck
import no.incremental.loft.work.Notifications
import no.incremental.loft.work.UpdateCheckWorker
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * The update check against a local HTTPS server serving releases.json (docs/releases-json.md):
 * a notification for a newer version_code, none for the same one, and nothing at all, not even a
 * request, when the user has switched the check off.
 */
@RunWith(AndroidJUnit4::class)
class UpdateCheckWorkerTest {
    private val ctx = TestKit.ctx
    private val prefs = Loft.get(ctx).prefs
    private val nm = ctx.getSystemService(NotificationManager::class.java)
    private lateinit var server: MockWebServer

    @Before fun setUp() {
        val cert = HeldCertificate.Builder().addSubjectAlternativeName("localhost").build()
        val serverTls = HandshakeCertificates.Builder().heldCertificate(cert).build()
        val clientTls = HandshakeCertificates.Builder().addTrustedCertificate(cert.certificate).build()
        server = MockWebServer().apply {
            useHttps(serverTls.sslSocketFactory(), false)
            start()
        }
        val http = HttpClients.base().newBuilder().sslSocketFactory(clientTls.sslSocketFactory(), clientTls.trustManager).build()
        UpdateCheckWorker.endpointForTests = server.url("/releases.json").toString() to http
        TestKit.grant(Manifest.permission.POST_NOTIFICATIONS)
        nm.cancelAll()
        prefs.updateCheck = true
        prefs.notifiedVersionCode = 0
    }

    @After fun tearDown() {
        UpdateCheckWorker.endpointForTests = null
        server.shutdown()
        nm.cancelAll()
        prefs.updateCheck = true
        prefs.notifiedVersionCode = 0
    }

    private fun serve(versionCode: Long, version: String) = server.enqueue(MockResponse()
        .setHeader("Content-Type", "application/json")
        .setBody("""{"android":{"version":"$version","version_code":$versionCode,
            "url":"https://loft.incremental.no/download/#android","sha256":"00","notes":"https://loft.incremental.no/download/#android"},
            "windows":{"version":"$version","url":"https://loft.incremental.no/download/#windows"}}"""))

    private suspend fun run(): ListenableWorker.Result = TestListenableWorkerBuilder<UpdateCheckWorker>(ctx).build().doWork()

    private suspend fun updateNotification(waitMs: Long): android.app.Notification? {
        val end = System.currentTimeMillis() + waitMs
        while (true) {
            nm.activeNotifications.firstOrNull { it.id == Notifications.ID_UPDATE }?.let { return it.notification }
            if (System.currentTimeMillis() > end) return null
            delay(200)
        }
    }

    @Test fun newerVersionNotifiesOnce(): Unit = runBlocking {
        serve(BuildConfig.VERSION_CODE + 1L, "0.9.0")
        assertEquals(ListenableWorker.Result.success(), run())
        val req = server.takeRequest()
        assertEquals("/releases.json", req.path)
        assertEquals("GET", req.method)
        assertEquals("Loft-Android/${BuildConfig.VERSION_NAME}", req.getHeader("User-Agent"))
        assertNull("no account or device data", req.getHeader("Authorization"))
        assertEquals(0L, req.bodySize)

        val n = updateNotification(5_000) ?: throw AssertionError("no update notification")
        assertEquals(Notifications.CH_UPDATES, n.channelId)
        assertTrue(n.extras.getCharSequence("android.title").toString().contains("0.9.0"))

        // The same release again the next day: no second notification.
        nm.cancelAll()
        serve(BuildConfig.VERSION_CODE + 1L, "0.9.0")
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(2, server.requestCount)
        assertNull(updateNotification(2_000))
    }

    @Test fun sameVersionDoesNotNotify(): Unit = runBlocking {
        serve(BuildConfig.VERSION_CODE.toLong(), BuildConfig.VERSION_NAME)
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(1, server.requestCount)
        assertNull(updateNotification(3_000))
    }

    @Test fun switchedOffMeansNoRequestAndNoNotification(): Unit = runBlocking {
        prefs.updateCheck = false
        serve(BuildConfig.VERSION_CODE + 5L, "1.0.0")
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals("not even a request", 0, server.requestCount)
        assertNull(updateNotification(2_000))
    }

    @Test fun theSwitchSchedulesAndCancelsTheDailyCheck() {
        Loft.get(ctx).forgetGateway()
        fun daily(): List<WorkInfo> = WorkManager.getInstance(ctx).getWorkInfosForUniqueWork("update-check").get()
            .filter { !it.state.isFinished }
        Work.disabledForTests = false
        try {
            prefs.updateCheck = true
            Work.schedulePeriodic(ctx)
            val w = daily().single()
            assertEquals(WorkInfo.State.ENQUEUED, w.state)
            assertTrue("at most once a day", w.periodicityInfo!!.repeatIntervalMillis >= 24 * 3600_000L)

            prefs.updateCheck = false
            Work.schedulePeriodic(ctx)
            assertTrue(daily().isEmpty())
        } finally {
            WorkManager.getInstance(ctx).cancelUniqueWork("update-check").result.get()
            Work.disabledForTests = true
        }
    }

    @Test fun productionUrlIsUnchanged() {
        assertEquals("https://loft.incremental.no/releases.json", UpdateCheck.RELEASES_URL)
    }
}
