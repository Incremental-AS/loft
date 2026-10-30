package no.incremental.loft.ui

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.provider.DocumentsContract
import android.text.format.DateUtils
import android.text.format.Formatter
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.InsertDriveFile
import androidx.compose.material.icons.filled.CreateNewFolder
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Upload
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import no.incremental.loft.Loft
import no.incremental.loft.R
import no.incremental.loft.core.Item
import no.incremental.loft.core.LoopState
import no.incremental.loft.core.Route

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun BrowseScreen(vm: MainViewModel) {
    val ctx = LocalContext.current
    val snackbar = remember { SnackbarHostState() }
    var menuFor by remember { mutableStateOf<Item?>(null) }
    var renaming by remember { mutableStateOf<Item?>(null) }
    var deleting by remember { mutableStateOf<Item?>(null) }
    var newFolder by remember { mutableStateOf(false) }
    val loop by vm.loft.loopState.collectAsState()
    val route by vm.loft.route.collectAsState()
    val pick = rememberLauncherForActivityResult(OpenableDocuments()) { uris ->
        vm.upload(uris)
    }

    LaunchedEffect(Unit) { if (vm.stack.isEmpty()) vm.openRoot() }
    LaunchedEffect(vm.snack) {
        vm.snack?.let { snackbar.showSnackbar(it) }
        vm.snack = null
    }

    val cur = vm.stack.lastOrNull()
    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(cur?.name ?: "", maxLines = 1, overflow = TextOverflow.Ellipsis)
                        val text = when (val l = loop) {
                            LoopState.CantReachHome -> stringResource(R.string.cant_reach_home)
                            LoopState.CantReachNas -> stringResource(R.string.cant_reach_nas)
                            is LoopState.Failed -> failedText(ctx, l)
                            LoopState.Ok -> when (route) {
                                Route.HOME -> stringResource(R.string.route_home)
                                Route.OUTSIDE -> stringResource(R.string.route_outside)
                                null -> ""
                            }
                        }
                        if (text.isNotEmpty()) Text(text, style = MaterialTheme.typography.labelMedium,
                            color = if (loop == LoopState.Ok) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.error)
                    }
                },
                navigationIcon = {
                    if (vm.stack.size > 1) IconButton(onClick = { vm.up() }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back))
                    }
                },
                actions = {
                    IconButton(onClick = { newFolder = true }, Modifier.testTag("newFolder")) {
                        Icon(Icons.Filled.CreateNewFolder, stringResource(R.string.new_folder))
                    }
                    IconButton(onClick = { vm.screen = Screen.SETTINGS }) {
                        Icon(Icons.Filled.Settings, stringResource(R.string.settings))
                    }
                },
            )
        },
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = { pick.launch(arrayOf("*/*")) },
                icon = { Icon(Icons.Filled.Upload, null) },
                text = { Text(stringResource(R.string.upload)) },
                modifier = Modifier.testTag("upload"),
            )
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { pad ->
        PullToRefreshBox(isRefreshing = vm.loading, onRefresh = { vm.refresh() }, modifier = Modifier.padding(pad).fillMaxSize()) {
            Column(Modifier.fillMaxSize()) {
                vm.browseError?.let {
                    Text(it, Modifier.padding(16.dp).testTag("browseError"), color = MaterialTheme.colorScheme.error)
                    if (vm.needsSignIn()) TextButton(onClick = { vm.signOut() }, Modifier.padding(horizontal = 8.dp)) {
                        Text(stringResource(R.string.sign_in_again))
                    }
                }
                if (vm.items.isEmpty() && !vm.loading && vm.browseError == null) {
                    Box(Modifier.fillMaxSize()) {
                        Text(stringResource(R.string.browse_empty), Modifier.align(Alignment.Center))
                    }
                }
                LazyColumn(Modifier.fillMaxSize().testTag("items")) {
                    items(vm.items, key = { it.id }) { item ->
                        Box {
                            ListItem(
                                headlineContent = { Text(item.name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                                supportingContent = { Text(subtitle(ctx, item)) },
                                leadingContent = { ItemIcon(vm, item) },
                                trailingContent = {
                                    if (!item.isFixed) IconButton(onClick = { menuFor = item }) {
                                        Icon(Icons.Filled.MoreVert, stringResource(R.string.more))
                                    }
                                },
                                modifier = Modifier.combinedClickable(
                                    onClick = { if (item.isDirectory) vm.enter(item) else open(ctx, vm, item, chooser = false) },
                                    onLongClick = { if (!item.isFixed) menuFor = item },
                                ).testTag("item:${item.name}"),
                            )
                            DropdownMenu(expanded = menuFor?.id == item.id, onDismissRequest = { menuFor = null }) {
                                if (!item.isDirectory) {
                                    DropdownMenuItem(text = { Text(stringResource(R.string.open_with)) },
                                        onClick = { menuFor = null; open(ctx, vm, item, chooser = true) })
                                    DropdownMenuItem(text = { Text(stringResource(R.string.share)) },
                                        onClick = { menuFor = null; share(ctx, vm, item) })
                                }
                                DropdownMenuItem(text = { Text(stringResource(R.string.rename)) },
                                    onClick = { menuFor = null; renaming = item }, modifier = Modifier.testTag("menuRename"))
                                DropdownMenuItem(text = { Text(stringResource(R.string.delete)) },
                                    onClick = { menuFor = null; deleting = item }, modifier = Modifier.testTag("menuDelete"))
                            }
                        }
                        HorizontalDivider()
                    }
                }
            }
        }
    }

    if (newFolder) NameDialog(stringResource(R.string.new_folder), "", stringResource(R.string.create),
        onDone = { vm.mkdir(it); newFolder = false }, onCancel = { newFolder = false })
    renaming?.let { item ->
        NameDialog(stringResource(R.string.rename), item.name, stringResource(R.string.rename),
            onDone = { vm.rename(item, it); renaming = null }, onCancel = { renaming = null })
    }
    deleting?.let { item ->
        AlertDialog(
            onDismissRequest = { deleting = null },
            text = { Text(stringResource(R.string.delete_confirm, item.name)) },
            confirmButton = { TextButton(onClick = { vm.delete(item); deleting = null }, Modifier.testTag("deleteConfirm")) { Text(stringResource(R.string.delete)) } },
            dismissButton = { TextButton(onClick = { deleting = null }) { Text(stringResource(R.string.cancel)) } },
        )
    }
}

private fun failedText(ctx: Context, s: LoopState.Failed): String = ctx.getString(when (s.error) {
    no.incremental.loft.core.SyncError.NasPasswordChanged -> R.string.nas_password_changed
    no.incremental.loft.core.SyncError.SignInExpired -> R.string.sign_in_expired
    no.incremental.loft.core.SyncError.DeviceRemoved -> R.string.device_removed
    no.incremental.loft.core.SyncError.NoAccess -> R.string.no_access
    is no.incremental.loft.core.SyncError.Stopped -> R.string.action_failed
})

@Composable
fun NameDialog(title: String, initial: String, confirm: String, onDone: (String) -> Unit, onCancel: () -> Unit) {
    var text by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onCancel,
        title = { Text(title) },
        text = {
            OutlinedTextField(text, { text = it }, Modifier.fillMaxWidth().testTag("nameField"), singleLine = true,
                label = { Text(stringResource(R.string.folder_name)) })
        },
        confirmButton = { TextButton(onClick = { onDone(text) }, enabled = text.isNotBlank(), modifier = Modifier.testTag("nameOk")) { Text(confirm) } },
        dismissButton = { TextButton(onClick = onCancel) { Text(stringResource(R.string.cancel)) } },
    )
}

@Composable
private fun ItemIcon(vm: MainViewModel, item: Item) {
    val mime = Loft.mimeType(item)
    val thumb: ImageBitmap? = if (!item.isDirectory && (mime.startsWith("image/") || mime.startsWith("video/"))) {
        produceState<ImageBitmap?>(null, item.id, item.contentEtag) { value = vm.thumbnail(item) }.value
    } else null
    if (thumb != null) {
        Image(thumb, null, Modifier.size(40.dp).clip(RoundedCornerShape(6.dp)), contentScale = ContentScale.Crop)
    } else {
        Icon(if (item.isDirectory) Icons.Filled.Folder else Icons.AutoMirrored.Filled.InsertDriveFile, null,
            Modifier.size(40.dp).padding(6.dp), tint = MaterialTheme.colorScheme.primary)
    }
}

private fun subtitle(ctx: Context, item: Item): String {
    val date = if (item.mtimeNs > 0) DateUtils.formatDateTime(ctx, item.modifiedMillis,
        DateUtils.FORMAT_SHOW_DATE or DateUtils.FORMAT_SHOW_TIME or DateUtils.FORMAT_ABBREV_MONTH) else ""
    return if (item.isDirectory) date else listOf(Formatter.formatShortFileSize(ctx, item.size), date)
        .filter { it.isNotEmpty() }.joinToString(" · ")
}

/** The system picker, showing only files that can be read as a stream (no virtual documents). */
private class OpenableDocuments : ActivityResultContracts.OpenMultipleDocuments() {
    override fun createIntent(context: Context, input: Array<String>): Intent =
        super.createIntent(context, input).addCategory(Intent.CATEGORY_OPENABLE)
}

/** Other apps open the file through Loft's DocumentsProvider: they stream it, and can save back. */
private fun open(ctx: Context, vm: MainViewModel, item: Item, chooser: Boolean) {
    vm.loft.prefs.addRecent(item)
    val uri = DocumentsContract.buildDocumentUri(Loft.AUTHORITY, item.id)
    val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, Loft.mimeType(item))
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
    try {
        ctx.startActivity(if (chooser) Intent.createChooser(view, item.name) else view)
    } catch (e: ActivityNotFoundException) {
        vm.snack = ctx.getString(R.string.no_app_to_open)
    }
}

private fun share(ctx: Context, vm: MainViewModel, item: Item) {
    val uri = DocumentsContract.buildDocumentUri(Loft.AUTHORITY, item.id)
    val send = Intent(Intent.ACTION_SEND).setType(Loft.mimeType(item))
        .putExtra(Intent.EXTRA_STREAM, uri)
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    try {
        ctx.startActivity(Intent.createChooser(send, item.name))
    } catch (e: ActivityNotFoundException) {
        vm.snack = ctx.getString(R.string.no_app_to_open)
    }
}
