package no.incremental.loft.ui

import android.Manifest
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import no.incremental.loft.BuildConfig
import no.incremental.loft.Loft
import no.incremental.loft.R
import no.incremental.loft.Work
import no.incremental.loft.core.Item

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(vm: MainViewModel) {
    val ctx = LocalContext.current
    val p = vm.loft.prefs
    var camera by remember { mutableStateOf(p.cameraUpload) }
    var wifiOnly by remember { mutableStateOf(p.cameraWifiOnly) }
    var updates by remember { mutableStateOf(p.updateCheck) }
    var cameraNote by remember { mutableStateOf<String?>(null) }

    val mediaPerms = if (Build.VERSION.SDK_INT >= 33) arrayOf(
        Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO,
        Manifest.permission.ACCESS_MEDIA_LOCATION, Manifest.permission.POST_NOTIFICATIONS,
    ) else if (Build.VERSION.SDK_INT >= 29) arrayOf(
        Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.ACCESS_MEDIA_LOCATION,
    ) else arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE)

    fun enableCamera() {
        p.cameraUpload = true
        p.startCameraLedger()                                  // only photos taken from now on
        camera = true
        if (p.cameraFolderId == null) vm.screen = Screen.CAMERA_FOLDER else Work.scheduleCamera(ctx)
    }

    val askMedia = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { r ->
        val ok = if (Build.VERSION.SDK_INT >= 33) {
            r[Manifest.permission.READ_MEDIA_IMAGES] == true || r[Manifest.permission.READ_MEDIA_VIDEO] == true
        } else r[Manifest.permission.READ_EXTERNAL_STORAGE] == true
        if (ok) enableCamera() else cameraNote = ctx.getString(R.string.camera_permission_needed)
    }
    val askNotify = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text(stringResource(R.string.settings)) },
            navigationIcon = {
                IconButton(onClick = { vm.goHome() }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back)) }
            },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).navigationBarsPadding(),
            verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Section(stringResource(R.string.settings_account))
            p.nasUser?.let { Text(stringResource(R.string.signed_in_as, it)) }
            p.pairing?.let { Text(stringResource(R.string.gateway_line, it.name)) }
            p.root?.let {
                Text(stringResource(R.string.loft_folder, if (it.isAllShares) stringResource(R.string.all_shares) else it.displayPath))
            }
            OutlinedButton(onClick = { vm.screen = Screen.CHOOSE_ROOT }) { Text(stringResource(R.string.change)) }

            HorizontalDivider()
            Section(stringResource(R.string.settings_camera))
            Toggle(stringResource(R.string.camera_upload), camera) { on ->
                if (on) askMedia.launch(mediaPerms) else {
                    p.cameraUpload = false
                    camera = false
                    Work.scheduleCamera(ctx)
                }
            }
            Text(stringResource(R.string.camera_upload_body), style = MaterialTheme.typography.bodySmall)
            cameraNote?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (camera) {
                Text(stringResource(R.string.camera_folder, p.cameraFolderName ?: "—"))
                OutlinedButton(onClick = { vm.screen = Screen.CAMERA_FOLDER }) { Text(stringResource(R.string.camera_choose_folder)) }
                Toggle(stringResource(R.string.camera_wifi_only), wifiOnly) {
                    p.cameraWifiOnly = it
                    wifiOnly = it
                    Work.scheduleCamera(ctx)
                }
            }

            if (Work.updateCheckAvailable) {
                HorizontalDivider()
                Section(stringResource(R.string.settings_updates))
                Toggle(stringResource(R.string.update_check), updates) {
                    p.updateCheck = it
                    updates = it
                    Work.schedulePeriodic(ctx)
                    if (it && Build.VERSION.SDK_INT >= 33) askNotify.launch(Manifest.permission.POST_NOTIFICATIONS)
                }
                Text(stringResource(R.string.update_check_body), style = MaterialTheme.typography.bodySmall)
            }

            HorizontalDivider()
            Button(onClick = { vm.signOut() }, Modifier.fillMaxWidth()) { Text(stringResource(R.string.sign_out)) }
            TextButton(onClick = { vm.forgetGateway() }, Modifier.fillMaxWidth()) { Text(stringResource(R.string.forget_gateway)) }
            Text(stringResource(R.string.version_line, BuildConfig.VERSION_NAME), style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun Section(title: String) =
    Text(title, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)

@Composable
private fun Toggle(label: String, value: Boolean, onChange: (Boolean) -> Unit) {
    ListItem(
        headlineContent = { Text(label) },
        trailingContent = { Switch(checked = value, onCheckedChange = onChange) },
        modifier = Modifier.clickable { onChange(!value) },
    )
}

@Composable
fun CameraFolderScreen(vm: MainViewModel) {
    val ctx = LocalContext.current
    FolderChooser(vm.loft, stringResource(R.string.camera_choose_folder), stringResource(R.string.camera_choose_folder),
        onChosen = { f ->
            vm.loft.prefs.cameraFolderId = f.id
            vm.loft.prefs.cameraFolderName = f.name
            Work.scheduleCamera(ctx)
            vm.screen = Screen.SETTINGS
        },
        onCancel = { vm.screen = Screen.SETTINGS })
}

/** Folders inside the Loft root, for "Save to Loft" and the camera upload folder. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FolderChooser(loft: Loft, title: String, confirm: String, onChosen: (Folder) -> Unit, onCancel: () -> Unit) {
    val root = loft.prefs.root ?: return
    var stack by remember { mutableStateOf(listOf(Folder(root.rootItemId, if (root.isAllShares) "Loft" else root.displayPath.substringAfterLast('/')))) }
    var folders by remember { mutableStateOf<List<Item>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val cur = stack.last()
    LaunchedEffect(cur.id) {
        folders = null
        error = null
        try {
            folders = loft.requireClient().allChildren(cur.id).filter { it.isDirectory }.sortedBy { it.name.lowercase() }
        } catch (e: Exception) {
            error = e.message
            folders = emptyList()
        }
    }
    // The all-shares root holds only shares: no files directly in it.
    val canSave = !(root.isAllShares && stack.size == 1)
    Scaffold(topBar = {
        TopAppBar(
            title = { Text(if (stack.size == 1) title else cur.name) },
            navigationIcon = {
                IconButton(onClick = { if (stack.size > 1) stack = stack.dropLast(1) else onCancel() }) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back))
                }
            },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            error?.let { Text(it, Modifier.padding(16.dp), color = MaterialTheme.colorScheme.error) }
            Box(Modifier.weight(1f)) {
                val list = folders
                if (list == null) CircularProgressIndicator(Modifier.align(Alignment.Center))
                else LazyColumn {
                    items(list, key = { it.id }) { f ->
                        ListItem(
                            headlineContent = { Text(f.name) },
                            leadingContent = { Icon(Icons.Filled.Folder, null) },
                            modifier = Modifier.clickable { stack = stack + Folder(f.id, f.name) }.testTag("chooseFolder:${f.name}"),
                        )
                        HorizontalDivider()
                    }
                }
            }
            Button(onClick = { onChosen(cur) }, enabled = canSave,
                modifier = Modifier.fillMaxWidth().padding(16.dp).navigationBarsPadding().testTag("chooseHere")) { Text(confirm) }
        }
    }
}
