package no.incremental.loft.work

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import no.incremental.loft.R
import no.incremental.loft.ui.MainActivity

object Notifications {
    const val CH_UPDATES = "updates"
    const val CH_TRANSFERS = "transfers"
    const val ID_UPDATE = 1
    const val ID_TRANSFER = 2
    const val ID_FAILED_BASE = 1000

    fun ensureChannels(ctx: Context) {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_UPDATES, ctx.getString(R.string.channel_updates),
            NotificationManager.IMPORTANCE_DEFAULT))
        nm.createNotificationChannel(NotificationChannel(CH_TRANSFERS, ctx.getString(R.string.channel_transfers),
            NotificationManager.IMPORTANCE_LOW))
    }

    private fun allowed(ctx: Context) = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    @SuppressLint("MissingPermission")          // allowed() checks POST_NOTIFICATIONS
    fun updateAvailable(ctx: Context, version: String, pageUrl: String) {
        if (!allowed(ctx)) return
        val open = PendingIntent.getActivity(ctx, 0, Intent(Intent.ACTION_VIEW, Uri.parse(pageUrl)),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(ctx, CH_UPDATES)
            .setSmallIcon(R.drawable.ic_stat_loft)
            .setContentTitle(ctx.getString(R.string.update_available_title, version))
            .setContentText(ctx.getString(R.string.update_available_body))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        try { NotificationManagerCompat.from(ctx).notify(ID_UPDATE, n) } catch (e: SecurityException) { }
    }

    fun transfer(ctx: Context, text: String, progress: Int = -1): android.app.Notification =
        NotificationCompat.Builder(ctx, CH_TRANSFERS)
            .setSmallIcon(R.drawable.ic_stat_loft)
            .setContentTitle(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .apply { if (progress >= 0) setProgress(100, progress, false) else setProgress(0, 0, true) }
            .build()

    @SuppressLint("MissingPermission")          // allowed() checks POST_NOTIFICATIONS
    fun uploadFailed(ctx: Context, name: String) {
        if (!allowed(ctx)) return
        val open = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(ctx, CH_TRANSFERS)
            .setSmallIcon(R.drawable.ic_stat_loft)
            .setContentTitle(ctx.getString(R.string.upload_failed, name))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        try {
            NotificationManagerCompat.from(ctx).notify(ID_FAILED_BASE + (name.hashCode() and 0xffff), n)
        } catch (e: SecurityException) {
        }
    }
}
