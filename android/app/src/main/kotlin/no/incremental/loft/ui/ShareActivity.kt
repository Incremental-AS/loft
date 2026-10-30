// "Save to Loft" in the share sheet: pick a folder in Loft, then the files upload in the
// background (WorkManager), so the sharing app isn't held up.
package no.incremental.loft.ui

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import no.incremental.loft.Loft
import no.incremental.loft.R

class ShareActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val loft = Loft.get(this)
        val uris = sharedUris(intent)
        setContent {
            LoftTheme {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    if (!loft.isReady) {
                        Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                            Text(stringResource(R.string.share_not_signed_in), style = MaterialTheme.typography.bodyLarge)
                            Button(onClick = {
                                startActivity(Intent(this@ShareActivity, MainActivity::class.java))
                                finish()
                            }) { Text(stringResource(R.string.open_loft)) }
                        }
                    } else {
                        FolderChooser(loft, stringResource(R.string.share_target_label), stringResource(R.string.save_here),
                            onChosen = { folder -> save(uris, folder.id) },
                            onCancel = { finish() })
                    }
                }
            }
        }
    }

    private fun save(uris: List<Uri>, parentId: String) {
        lifecycleScope.launch {
            val n = MainViewModel.stageAndUpload(applicationContext, uris, parentId)
            Toast.makeText(applicationContext, resources.getQuantityString(R.plurals.share_saving, n, n), Toast.LENGTH_SHORT).show()
            finish()
        }
    }

    @Suppress("DEPRECATION")
    private fun sharedUris(i: Intent): List<Uri> = when (i.action) {
        Intent.ACTION_SEND -> listOfNotNull(
            if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
            else i.getParcelableExtra(Intent.EXTRA_STREAM) as? Uri
        )
        Intent.ACTION_SEND_MULTIPLE ->
            (if (Build.VERSION.SDK_INT >= 33) i.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
            else i.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)) ?: emptyList()
        else -> emptyList()
    }
}
