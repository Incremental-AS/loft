// First run: pair with a gateway, sign in at home, choose the Loft folder.
package no.incremental.loft.ui

import android.content.ClipboardManager
import android.content.Context
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import no.incremental.loft.R
import no.incremental.loft.core.RootInfo

@Composable
private fun Hero() {
    Image(
        painter = painterResource(if (isSystemInDarkTheme()) R.drawable.hero_dark else R.drawable.hero_light),
        contentDescription = null,
        contentScale = ContentScale.Crop,
        alignment = Alignment.BottomCenter,
        modifier = Modifier.fillMaxWidth().aspectRatio(16f / 10f),
    )
}

@Composable
fun PairScreen(vm: MainViewModel) {
    var dialog by rememberSaveable { mutableStateOf<String?>(null) }   // "paste" | "manual"
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).navigationBarsPadding()) {
        Hero()
        Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text(stringResource(R.string.welcome_title), style = MaterialTheme.typography.headlineMedium)
            Text(stringResource(R.string.pair_body), style = MaterialTheme.typography.bodyLarge)
            vm.pairError?.let { Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.testTag("pairError")) }
            Button(onClick = { vm.pairError = null; vm.screen = Screen.SCAN }, Modifier.fillMaxWidth()) {
                Text(stringResource(R.string.pair_scan))
            }
            OutlinedButton(onClick = { vm.pairError = null; dialog = "paste" }, Modifier.fillMaxWidth()) {
                Text(stringResource(R.string.pair_paste))
            }
            TextButton(onClick = { vm.pairError = null; dialog = "manual" }, Modifier.fillMaxWidth()) {
                Text(stringResource(R.string.pair_manual))
            }
        }
    }
    when (dialog) {
        "paste" -> PasteDialog(vm) { dialog = null }
        "manual" -> ManualDialog(vm) { dialog = null }
    }
}

@Composable
private fun PasteDialog(vm: MainViewModel, close: () -> Unit) {
    val ctx = LocalContext.current
    var text by rememberSaveable {
        val clip = (ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).primaryClip
        val s = clip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(ctx)?.toString().orEmpty()
        mutableStateOf(if (s.contains("/pair#")) s else "")
    }
    AlertDialog(
        onDismissRequest = close,
        title = { Text(stringResource(R.string.pair_paste_title)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(text, { text = it }, Modifier.fillMaxWidth().testTag("pasteField"),
                    placeholder = { Text(stringResource(R.string.pair_paste_hint)) }, minLines = 2)
                vm.pairError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = { TextButton(onClick = { if (vm.acceptPairing(text)) close() }) { Text(stringResource(R.string.pair_continue)) } },
        dismissButton = { TextButton(onClick = close) { Text(stringResource(R.string.cancel)) } },
    )
}

@Composable
private fun ManualDialog(vm: MainViewModel, close: () -> Unit) {
    var address by rememberSaveable { mutableStateOf("") }
    var pin by rememberSaveable { mutableStateOf("") }
    var name by rememberSaveable { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = close,
        title = { Text(stringResource(R.string.pair_manual_title)) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(stringResource(R.string.pair_manual_body), style = MaterialTheme.typography.bodyMedium)
                OutlinedTextField(address, { address = it }, Modifier.fillMaxWidth(), singleLine = true,
                    label = { Text(stringResource(R.string.pair_manual_address)) },
                    placeholder = { Text(stringResource(R.string.pair_manual_address_hint)) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
                OutlinedTextField(pin, { pin = it }, Modifier.fillMaxWidth(), singleLine = true,
                    label = { Text(stringResource(R.string.pair_manual_pin)) })
                OutlinedTextField(name, { name = it }, Modifier.fillMaxWidth(), singleLine = true,
                    label = { Text(stringResource(R.string.pair_manual_name)) })
                vm.pairError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = { TextButton(onClick = { if (vm.manualPairing(address, pin, name)) close() }) { Text(stringResource(R.string.pair_continue)) } },
        dismissButton = { TextButton(onClick = close) { Text(stringResource(R.string.cancel)) } },
    )
}

@Composable
fun SignInScreen(vm: MainViewModel) {
    var user by rememberSaveable { mutableStateOf(vm.loft.prefs.nasUser ?: "") }
    var password by remember { mutableStateOf("") }
    Column(
        Modifier.fillMaxSize().systemBarsPadding().imePadding().verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(stringResource(R.string.signin_title), style = MaterialTheme.typography.headlineMedium)
        vm.loft.prefs.pairing?.let {
            Text(stringResource(R.string.signin_gateway, it.name), style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.primary)
        }
        Text(stringResource(R.string.signin_body), style = MaterialTheme.typography.bodyLarge)
        OutlinedTextField(user, { user = it }, Modifier.fillMaxWidth().testTag("user"), singleLine = true,
            label = { Text(stringResource(R.string.signin_user)) },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii, imeAction = ImeAction.Next, autoCorrectEnabled = false))
        OutlinedTextField(password, { password = it }, Modifier.fillMaxWidth().testTag("password"), singleLine = true,
            label = { Text(stringResource(R.string.signin_password)) },
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done))
        Text(stringResource(R.string.signin_help), style = MaterialTheme.typography.bodySmall)
        vm.signInError?.let { Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.testTag("signInError")) }
        Button(
            onClick = { vm.signIn(user, password); password = "" },
            enabled = !vm.busy && user.isNotBlank() && password.isNotEmpty(),
            modifier = Modifier.fillMaxWidth().testTag("signIn"),
        ) {
            if (vm.busy) CircularProgressIndicator(Modifier.height(20.dp), strokeWidth = 2.dp)
            else Text(stringResource(R.string.signin_button))
        }
        TextButton(onClick = { vm.forgetGateway() }, Modifier.fillMaxWidth()) { Text(stringResource(R.string.forget_gateway)) }
    }
}

/** Shares, then folders inside one: the NAS folder that becomes Loft on this phone. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChooseRootScreen(vm: MainViewModel) {
    var share by rememberSaveable { mutableStateOf<String?>(null) }
    var path by rememberSaveable { mutableStateOf("") }
    var entries by remember { mutableStateOf<List<String>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var saving by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(share, path) {
        entries = null
        error = null
        try {
            val c = vm.loft.requireClient()
            entries = if (share == null) c.shares() else c.folders(share!!, path).folders
        } catch (e: Exception) {
            error = vm.describe(e)
            entries = emptyList()
        }
    }

    fun choose(s: String, p: String) {
        saving = true
        scope.launch {
            try {
                val me = vm.loft.requireClient().setRoot(s, p)
                vm.loft.prefs.root = me.root
                vm.rootChosen()
            } catch (e: Exception) {
                error = vm.describe(e)
            } finally {
                saving = false
            }
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text(stringResource(R.string.pick_title)) },
            navigationIcon = {
                if (share != null) IconButton(onClick = {
                    if (path.isEmpty()) share = null else path = path.substringBeforeLast('/', "")
                }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back)) }
            },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            Text(stringResource(R.string.pick_body), Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                style = MaterialTheme.typography.bodyMedium)
            share?.let {
                Text(if (path.isEmpty()) it else "$it/$path", Modifier.padding(horizontal = 16.dp),
                    style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.primary)
            }
            error?.let { Text(it, Modifier.padding(16.dp), color = MaterialTheme.colorScheme.error) }
            Box(Modifier.weight(1f)) {
                val list = entries
                if (list == null) {
                    CircularProgressIndicator(Modifier.align(Alignment.Center))
                } else if (list.isEmpty() && error == null) {
                    Text(stringResource(R.string.pick_no_folders), Modifier.align(Alignment.Center))
                } else {
                    LazyColumn {
                        items(list) { name ->
                            ListItem(
                                headlineContent = { Text(name) },
                                leadingContent = { Icon(Icons.Filled.Folder, null) },
                                modifier = Modifier.clickable {
                                    if (share == null) share = name else path = if (path.isEmpty()) name else "$path/$name"
                                }.testTag("folder:$name"),
                            )
                            HorizontalDivider()
                        }
                    }
                }
            }
            Column(Modifier.padding(16.dp).navigationBarsPadding(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (share != null) {
                    Button(onClick = { choose(share!!, path) }, enabled = !saving, modifier = Modifier.fillMaxWidth().testTag("useThis")) {
                        Text(stringResource(R.string.pick_use_this))
                    }
                } else {
                    OutlinedButton(onClick = { choose(RootInfo.ALL_SHARES, "") }, enabled = !saving, modifier = Modifier.fillMaxWidth()) {
                        Text(stringResource(R.string.pick_all_shares))
                    }
                }
            }
        }
    }
}
