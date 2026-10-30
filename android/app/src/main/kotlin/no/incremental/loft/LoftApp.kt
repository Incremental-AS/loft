package no.incremental.loft

import android.app.Application
import no.incremental.loft.work.Notifications

class LoftApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Notifications.ensureChannels(this)
        Work.schedulePeriodic(this)      // a no-op under the instrumented test runner
    }
}
