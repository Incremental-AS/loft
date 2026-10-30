// The gateway in the system Files app and the document picker (docs/design.md §E). Document ids
// are the gateway's item ids (stable across renames and moves). Reads stream; writes land
// in app-private storage and upload when the file is closed; the change feed tells the
// system when something changed on the NAS.
package no.incremental.loft.provider

import android.content.res.AssetFileDescriptor
import android.database.Cursor
import android.database.MatrixCursor
import android.graphics.Point
import android.os.Bundle
import android.os.CancellationSignal
import android.os.Handler
import android.os.HandlerThread
import android.os.ParcelFileDescriptor
import android.os.storage.StorageManager
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import android.provider.DocumentsContract.Root
import android.provider.DocumentsProvider
import android.util.Log
import android.webkit.MimeTypeMap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import no.incremental.loft.Loft
import no.incremental.loft.R
import no.incremental.loft.Work
import no.incremental.loft.core.CommitRequest
import no.incremental.loft.core.Exclusions
import no.incremental.loft.core.GatewayClient
import no.incremental.loft.core.GatewayException
import no.incremental.loft.core.Item
import java.io.File
import java.io.FileNotFoundException
import java.util.UUID

class LoftDocumentsProvider : DocumentsProvider() {
    private val loft: Loft by lazy { Loft.get(context!!) }

    override fun onCreate(): Boolean = true

    // ── helpers ──────────────────────────────────────────────────────────

    /** Run a gateway call from a binder thread; the system cancels through `signal`. */
    private fun <T> net(signal: CancellationSignal? = null, timeoutMs: Long = 60_000, block: suspend (GatewayClient) -> T): T {
        val client = loft.client() ?: throw FileNotFoundException("not paired")
        if (!loft.isSignedIn) throw FileNotFoundException("not signed in")
        return runBlocking(Dispatchers.IO) {
            val job = async { withTimeout(timeoutMs) { block(client) } }
            signal?.setOnCancelListener { job.cancel() }
            job.await()
        }
    }

    private fun fail(e: Exception, what: String): Nothing {
        Log.w(Loft.TAG, "provider $what: $e")
        if (e is GatewayException.Api && e.error.status == 404) throw FileNotFoundException(what)
        if (e is FileNotFoundException) throw e
        throw IllegalStateException(message(e), e)
    }

    private fun message(e: Exception): String {
        val c = context!!
        return when (e) {
            is GatewayException.Unreachable -> c.getString(R.string.cant_reach_home)
            is GatewayException.Api -> when {
                e.error.isNasUnreachable -> c.getString(R.string.cant_reach_nas)
                e.error.needsSignIn -> c.getString(R.string.sign_in_expired)
                e.error.status == 409 -> c.getString(R.string.name_exists)
                else -> c.getString(R.string.action_failed)
            }
            else -> c.getString(R.string.action_failed)
        }
    }

    private val rootItemId: String? get() = loft.prefs.root?.rootItemId

    private fun item(id: String, signal: CancellationSignal? = null): Item = try {
        net(signal, 20_000) { it.item(id) }.also { loft.items[it.id] = it }
    } catch (e: GatewayException.Api) {
        if (e.error.status == 404) throw FileNotFoundException(id)
        loft.items[id] ?: fail(e, "item")
    } catch (e: GatewayException) {
        loft.items[id] ?: fail(e, "item")
    }

    private fun addRow(c: MatrixCursor, it: Item) {
        val root = loft.prefs.root
        val isRoot = it.id == root?.rootItemId
        val mime = Loft.mimeType(it)
        var flags = 0
        if (it.isDirectory) {
            // An all-shares root holds only the shares themselves.
            if (!(isRoot && root?.isAllShares == true)) flags = flags or Document.FLAG_DIR_SUPPORTS_CREATE
        } else {
            flags = flags or Document.FLAG_SUPPORTS_WRITE
            if (mime.startsWith("image/") || mime.startsWith("video/")) flags = flags or Document.FLAG_SUPPORTS_THUMBNAIL
        }
        if (!isRoot && !it.isFixed) {
            flags = flags or Document.FLAG_SUPPORTS_RENAME or Document.FLAG_SUPPORTS_DELETE or Document.FLAG_SUPPORTS_MOVE
        }
        c.newRow().apply {
            add(Document.COLUMN_DOCUMENT_ID, it.id)
            add(Document.COLUMN_DISPLAY_NAME, it.name)
            add(Document.COLUMN_MIME_TYPE, mime)
            add(Document.COLUMN_SIZE, if (it.isDirectory) null else it.size)
            add(Document.COLUMN_LAST_MODIFIED, if (it.mtimeNs > 0) it.modifiedMillis else null)
            add(Document.COLUMN_FLAGS, flags)
        }
    }

    // ── roots and queries ───────────────────────────────────────────────

    override fun queryRoots(projection: Array<out String>?): Cursor {
        val c = MatrixCursor(projection ?: ROOT_COLUMNS)
        c.setNotificationUri(context!!.contentResolver, DocumentsContract.buildRootsUri(Loft.AUTHORITY))
        val root = loft.prefs.root
        val pairing = loft.prefs.pairing
        if (!loft.isSignedIn || root == null || pairing == null) return c
        c.newRow().apply {
            add(Root.COLUMN_ROOT_ID, ROOT_ID)
            add(Root.COLUMN_DOCUMENT_ID, root.rootItemId)
            add(Root.COLUMN_TITLE, context!!.getString(R.string.provider_root_title))
            add(Root.COLUMN_SUMMARY, pairing.name)
            add(Root.COLUMN_FLAGS, Root.FLAG_SUPPORTS_CREATE or Root.FLAG_SUPPORTS_IS_CHILD or Root.FLAG_SUPPORTS_RECENTS)
            add(Root.COLUMN_ICON, R.mipmap.ic_launcher)
        }
        return c
    }

    override fun queryDocument(documentId: String, projection: Array<out String>?): Cursor {
        val c = MatrixCursor(projection ?: DOCUMENT_COLUMNS)
        c.setNotificationUri(context!!.contentResolver, DocumentsContract.buildDocumentUri(Loft.AUTHORITY, documentId))
        addRow(c, loft.items[documentId]?.takeIf { documentId != rootItemId } ?: item(documentId))
        return c
    }

    override fun queryChildDocuments(parentDocumentId: String, projection: Array<out String>?, sortOrder: String?): Cursor {
        loft.touch()
        val c = MatrixCursor(projection ?: DOCUMENT_COLUMNS)
        c.setNotificationUri(context!!.contentResolver,
            DocumentsContract.buildChildDocumentsUri(Loft.AUTHORITY, parentDocumentId))
        try {
            val list = net(timeoutMs = 45_000) { it.allChildren(parentDocumentId) }
            loft.remember(list)
            list.forEach { addRow(c, it) }
        } catch (e: Exception) {
            if (e is GatewayException.Api && e.error.status == 404) throw FileNotFoundException(parentDocumentId)
            Log.w(Loft.TAG, "children $parentDocumentId: $e")
            // Offline: show what we saw last time, with the reason on top.
            loft.items.values.filter { it.parentId == parentDocumentId }.sortedBy { it.name.lowercase() }
                .forEach { addRow(c, it) }
            c.extras = Bundle().apply { putString(DocumentsContract.EXTRA_ERROR, message(e)) }
        }
        return c
    }

    override fun queryRecentDocuments(rootId: String, projection: Array<out String>?): Cursor {
        val c = MatrixCursor(projection ?: DOCUMENT_COLUMNS)
        if (loft.isSignedIn) loft.prefs.recents.forEach { addRow(c, it) }
        return c
    }

    override fun isChildDocument(parentDocumentId: String, documentId: String): Boolean {
        var cur: String? = documentId
        repeat(64) {
            if (cur == null) return false
            if (cur == parentDocumentId) return true
            val it = loft.items[cur] ?: try { item(cur!!) } catch (e: Exception) { return false }
            cur = it.parentId
        }
        return false
    }

    // ── content ─────────────────────────────────────────────────────────

    override fun openDocument(documentId: String, mode: String, signal: CancellationSignal?): ParcelFileDescriptor {
        loft.touch()
        val it = item(documentId, signal)
        if (it.isDirectory) throw FileNotFoundException("is a folder")
        val ctx = context!!
        val pfdMode = ParcelFileDescriptor.parseMode(mode)
        val writing = mode.contains('w') || mode.contains('a')
        if (!writing) {
            loft.prefs.addRecent(it)
            if (it.size <= SMALL_FILE) {
                return ParcelFileDescriptor.open(cachedCopy(it, signal), ParcelFileDescriptor.MODE_READ_ONLY)
            }
            val t = HandlerThread("loft-read-${it.id.take(8)}").apply { start() }
            val sm = ctx.getSystemService(StorageManager::class.java)
            return sm.openProxyFileDescriptor(ParcelFileDescriptor.MODE_READ_ONLY,
                RangeReader(loft.requireClient(), it, t), Handler(t.looper))
        }
        // Writing: a private working copy; uploaded when the app closes the file.
        val truncate = mode == "w" || mode.contains('t')
        val work = File(Work.stagingDir(ctx), "edit-${it.id}-${UUID.randomUUID()}")
        if (truncate) {
            work.createNewFile()
        } else {
            try {
                net(signal, 30 * 60_000L) { c -> c.download(it.id, work) }
            } catch (e: Exception) {
                work.delete()
                fail(e, "open for writing")
            }
        }
        val before = if (truncate) null else work.length() to GatewayClient.sha256Hex(work)
        val t = HandlerThread("loft-write-${it.id.take(8)}").apply { start() }
        return ParcelFileDescriptor.open(work, pfdMode, Handler(t.looper)) { e ->
            try {
                val unchanged = before != null && work.length() == before.first && GatewayClient.sha256Hex(work) == before.second
                if (e != null || unchanged) {
                    work.delete()
                } else {
                    Work.enqueueUpload(ctx, work, itemId = it.id, ifMatch = it.contentEtag)
                }
            } finally {
                t.quitSafely()
            }
        }
    }

    /** Small files: download once per content version into the cache, then a plain fd. */
    private fun cachedCopy(it: Item, signal: CancellationSignal?): File {
        val dir = File(context!!.cacheDir, "docs").apply { mkdirs() }
        val f = File(dir, "${it.id}-${it.contentEtag}")
        if (f.isFile && f.length() == it.size) return f
        dir.listFiles { x -> x.name.startsWith(it.id + "-") }?.forEach { old -> old.delete() }
        val tmp = File(dir, "${f.name}.part")
        try {
            val tag = net(signal, 10 * 60_000L) { c -> c.download(it.id, tmp) }
            val final = File(dir, "${it.id}-$tag")
            tmp.renameTo(final)
            return final
        } catch (e: Exception) {
            tmp.delete()
            fail(e, "download")
        }
    }

    override fun openDocumentThumbnail(documentId: String, sizeHint: Point?, signal: CancellationSignal?): AssetFileDescriptor {
        val px = maxOf(sizeHint?.x ?: 256, sizeHint?.y ?: 256).coerceIn(64, 1024)
        val etag = loft.items[documentId]?.contentEtag ?: "x"
        val dir = File(context!!.cacheDir, "thumbs").apply { mkdirs() }
        val f = File(dir, "$documentId-$etag-$px")
        if (!f.isFile) {
            val bytes = try {
                net(signal, 45_000) { it.thumbnail(documentId, px) }
            } catch (e: Exception) {
                fail(e, "thumbnail")
            } ?: throw FileNotFoundException("no thumbnail")
            f.writeBytes(bytes)
        }
        val pfd = ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY)
        return AssetFileDescriptor(pfd, 0, f.length())
    }

    // ── changes ─────────────────────────────────────────────────────────

    override fun createDocument(parentDocumentId: String, mimeType: String, displayName: String): String {
        val name = withExtension(displayName.trim(), mimeType)
        if (name.isEmpty() || Exclusions.isExcluded(name)) throw IllegalArgumentException("name not allowed: $name")
        val created = try {
            if (mimeType == Document.MIME_TYPE_DIR) {
                net { c -> mkdirUnique(c, parentDocumentId, name) }
            } else {
                // An empty file, so the new document has an id at once; content follows on write.
                val empty = File(Work.stagingDir(context!!), "new-${UUID.randomUUID()}").apply { createNewFile() }
                try {
                    net { c -> c.uploadAndCommit(empty, CommitRequest(parentId = parentDocumentId, name = name)).item }
                } finally {
                    empty.delete()
                }
            }
        } catch (e: Exception) {
            fail(e, "create")
        }
        loft.items[created.id] = created
        loft.notifyChildren(parentDocumentId)
        return created.id
    }

    private suspend fun mkdirUnique(c: GatewayClient, parent: String, name: String): Item {
        var n = 1
        while (true) {
            val candidate = if (n == 1) name else "$name ($n)"
            try {
                return c.mkdir(parent, candidate)
            } catch (e: GatewayException.Api) {
                if (e.error.error != "name_exists" || n >= 50) throw e
                n++
            }
        }
    }

    override fun renameDocument(documentId: String, displayName: String): String? {
        val it = try {
            net { c -> c.patch(documentId, name = displayName.trim()) }
        } catch (e: Exception) {
            fail(e, "rename")
        }
        loft.items[it.id] = it
        loft.notifyChildren(it.parentId)
        return null                                       // same id: ids survive renames
    }

    override fun deleteDocument(documentId: String) {
        val parent = loft.items[documentId]?.parentId
        try {
            net { c -> c.delete(documentId) }
        } catch (e: Exception) {
            fail(e, "delete")
        }
        loft.items.remove(documentId)
        loft.prefs.removeRecent(documentId)
        if (parent != null) loft.notifyChildren(parent) else loft.notifyChanged(null)
    }

    override fun removeDocument(documentId: String, parentDocumentId: String) = deleteDocument(documentId)

    override fun moveDocument(sourceDocumentId: String, sourceParentDocumentId: String, targetParentDocumentId: String): String {
        val it = try {
            net { c -> c.patch(sourceDocumentId, parentId = targetParentDocumentId) }
        } catch (e: Exception) {
            fail(e, "move")
        }
        loft.items[it.id] = it
        loft.notifyChildren(sourceParentDocumentId)
        loft.notifyChildren(targetParentDocumentId)
        return it.id
    }

    companion object {
        const val ROOT_ID = "loft"
        /** Up to this size a read downloads the file once; above it, reads stream by Range. */
        const val SMALL_FILE = 8L shl 20

        val ROOT_COLUMNS = arrayOf(
            Root.COLUMN_ROOT_ID, Root.COLUMN_DOCUMENT_ID, Root.COLUMN_TITLE, Root.COLUMN_SUMMARY,
            Root.COLUMN_FLAGS, Root.COLUMN_ICON,
        )
        val DOCUMENT_COLUMNS = arrayOf(
            Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE,
            Document.COLUMN_SIZE, Document.COLUMN_LAST_MODIFIED, Document.COLUMN_FLAGS,
        )

        /** "notes" + text/plain -> "notes.txt", as the system expects from a provider. */
        fun withExtension(name: String, mimeType: String): String {
            if (mimeType == Document.MIME_TYPE_DIR || name.contains('.')) return name
            val ext = MimeTypeMap.getSingleton().getExtensionFromMimeType(mimeType) ?: return name
            return "$name.$ext"
        }
    }
}
