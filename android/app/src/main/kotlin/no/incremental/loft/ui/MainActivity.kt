package no.incremental.loft.ui

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.ui.res.stringResource
import no.incremental.loft.R
import androidx.compose.material3.MaterialTheme
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private val vm: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (savedInstanceState == null) handle(intent)
        setContent {
            LoftTheme {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    BackHandler(enabled = vm.screen != vm.homeScreen() || (vm.screen == Screen.BROWSE && vm.stack.size > 1)) {
                        when {
                            vm.screen == Screen.BROWSE -> vm.up()
                            vm.screen == Screen.CAMERA_FOLDER -> vm.screen = Screen.SETTINGS
                            vm.screen == Screen.CHOOSE_ROOT && vm.loft.prefs.root != null -> vm.screen = Screen.SETTINGS
                            else -> vm.goHome()
                        }
                    }
                    when (vm.screen) {
                        Screen.PAIR -> PairScreen(vm)
                        Screen.SCAN -> ScanScreen(vm)
                        Screen.SIGN_IN -> SignInScreen(vm)
                        Screen.CHOOSE_ROOT -> ChooseRootScreen(vm)
                        Screen.BROWSE -> BrowseScreen(vm)
                        Screen.SETTINGS -> SettingsScreen(vm)
                        Screen.CAMERA_FOLDER -> CameraFolderScreen(vm)
                    }
                    vm.pendingPairing?.let { p ->
                        AlertDialog(
                            onDismissRequest = { vm.pendingPairing = null },
                            title = { Text(stringResource(R.string.pair_replace_title, p.name)) },
                            text = { Text(stringResource(R.string.pair_replace_body, vm.loft.prefs.pairing?.name ?: "")) },
                            confirmButton = { TextButton(onClick = { vm.confirmPendingPairing() }) { Text(stringResource(R.string.pair_replace_confirm)) } },
                            dismissButton = { TextButton(onClick = { vm.pendingPairing = null }) { Text(stringResource(R.string.cancel)) } },
                        )
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    override fun onResume() {
        super.onResume()
        vm.loft.touch()
        if (vm.screen == Screen.BROWSE && vm.stack.isEmpty()) vm.openRoot()
        lifecycleScope.launch { vm.loft.rotateIfDue() }
    }

    /** https://loft.incremental.no/pair#<payload>: the App Link (docs/pairing.md). */
    private fun handle(intent: Intent?) {
        val data = intent?.data ?: return
        if (intent.action == Intent.ACTION_VIEW) vm.openPairingLink(data.toString())
    }
}
