package no.incremental.loft

import android.os.Bundle
import androidx.test.runner.AndroidJUnitRunner

/** Runs before Application.onCreate: no update check or other periodic work during tests. */
class LoftTestRunner : AndroidJUnitRunner() {
    override fun onCreate(arguments: Bundle?) {
        Work.disabledForTests = true
        super.onCreate(arguments)
    }
}
