package vfs

import (
	"context"
	"errors"
	"os"
	"sort"
	"sync"
	"syscall"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/cache"
	"github.com/Incremental-AS/loft/linux/internal/gateway"
	"github.com/Incremental-AS/loft/linux/internal/names"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// Node is a file or folder. key is the gateway item id, or "local-…" for a file the
// gateway doesn't have yet; it changes when that file is uploaded (guarded by f.mu).
type Node struct {
	fs.Inode
	f   *FS
	key string
	dir bool
}

var (
	_ fs.NodeLookuper      = (*Node)(nil)
	_ fs.NodeReaddirer     = (*Node)(nil)
	_ fs.NodeGetattrer     = (*Node)(nil)
	_ fs.NodeSetattrer     = (*Node)(nil)
	_ fs.NodeOpener        = (*Node)(nil)
	_ fs.NodeCreater       = (*Node)(nil)
	_ fs.NodeMkdirer       = (*Node)(nil)
	_ fs.NodeUnlinker      = (*Node)(nil)
	_ fs.NodeRmdirer       = (*Node)(nil)
	_ fs.NodeRenamer       = (*Node)(nil)
	_ fs.NodeStatfser      = (*Node)(nil)
	_ fs.NodeGetxattrer    = (*Node)(nil)
	_ fs.NodeSetxattrer    = (*Node)(nil)
	_ fs.NodeRemovexattrer = (*Node)(nil)
	_ fs.NodeListxattrer   = (*Node)(nil)
)

func (n *Node) k() string {
	n.f.mu.Lock()
	defer n.f.mu.Unlock()
	return n.key
}

const opTimeout = 2 * time.Minute

func opCtx(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, opTimeout)
}

// inode returns the kernel inode for a key, reusing the live one.
func (n *Node) inode(ctx context.Context, key string, dir bool) *fs.Inode {
	f := n.f
	f.mu.Lock()
	ino := f.ino(key)
	f.mu.Unlock()
	mode := uint32(syscall.S_IFREG)
	if dir {
		mode = syscall.S_IFDIR
	}
	in := n.NewInode(ctx, &Node{f: f, key: key, dir: dir}, fs.StableAttr{Mode: mode, Ino: ino})
	// go-fuse returns the existing inode for a known ino; make sure it carries the key.
	if live, ok := in.Operations().(*Node); ok {
		f.mu.Lock()
		live.key = key
		f.inodes[key] = in
		f.mu.Unlock()
	}
	return in
}

// attr fills attributes from the item, or from the local copy when it has changes.
func (f *FS) attr(key string, dir bool, out *fuse.Attr) syscall.Errno {
	f.mu.Lock()
	it := f.items[key]
	loc := f.locals[key]
	ino := f.ino(key)
	f.mu.Unlock()
	out.Ino = ino
	out.Owner = fuse.Owner{Uid: uint32(os.Getuid()), Gid: uint32(os.Getgid())}
	out.Blksize = 4096
	if dir || (it != nil && it.IsDir()) {
		out.Mode = syscall.S_IFDIR | 0o755
		out.Nlink = 2
		if it != nil {
			t := it.Modified()
			out.SetTimes(&t, &t, &t)
		}
		return 0
	}
	out.Mode = syscall.S_IFREG | 0o644
	out.Nlink = 1
	var size int64
	var mt time.Time
	if it != nil {
		size, mt = it.Size, it.Modified()
	}
	if e, ok := f.cache.Get(key); ok && (e.Dirty || loc != nil) {
		size = e.Size
		if st, err := os.Stat(f.cache.Path(key)); err == nil {
			mt = st.ModTime()
		}
	} else if it == nil && loc == nil {
		return syscall.ENOENT
	}
	out.Size = uint64(size)
	out.Blocks = (uint64(size) + 511) / 512
	out.SetTimes(&mt, &mt, &mt)
	return 0
}

func (n *Node) Getattr(ctx context.Context, fh fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	return n.f.attr(n.k(), n.dir, &out.Attr)
}

func (n *Node) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	f := n.f
	name = names.NFC(name)
	dir := n.k()
	if err := f.ensureListed(ctx, dir); err != nil {
		return nil, errno(err)
	}
	f.mu.Lock()
	key, isDir, ok := f.childLocked(dir, name)
	f.mu.Unlock()
	if !ok {
		return nil, syscall.ENOENT
	}
	if e := f.attr(key, isDir, &out.Attr); e != 0 {
		return nil, e
	}
	return n.inode(ctx, key, isDir), 0
}

func (n *Node) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	f := n.f
	dir := n.k()
	if err := f.ensureListed(ctx, dir); err != nil {
		return nil, errno(err)
	}
	f.mu.Lock()
	seen := map[string]bool{}
	var out []fuse.DirEntry
	for _, l := range f.locals {
		if l.Parent == dir && !seen[l.Name] {
			seen[l.Name] = true
			out = append(out, fuse.DirEntry{Name: l.Name, Mode: syscall.S_IFREG, Ino: f.ino(l.Key)})
		}
	}
	for name, id := range f.kids[dir] {
		it := f.items[id]
		if it == nil || seen[name] {
			continue
		}
		mode := uint32(syscall.S_IFREG)
		if it.IsDir() {
			mode = syscall.S_IFDIR
		}
		out = append(out, fuse.DirEntry{Name: name, Mode: mode, Ino: f.ino(id)})
	}
	f.mu.Unlock()
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return fs.NewListDirStream(out), 0
}

// ── open / read / write ─────────────────────────────────────────────────────

type handle struct {
	n     *Node
	write bool
	once  sync.Once
}

var (
	_ fs.FileReader   = (*handle)(nil)
	_ fs.FileWriter   = (*handle)(nil)
	_ fs.FileFlusher  = (*handle)(nil)
	_ fs.FileReleaser = (*handle)(nil)
	_ fs.FileFsyncer  = (*handle)(nil)
)

func (n *Node) Open(ctx context.Context, flags uint32) (fs.FileHandle, uint32, syscall.Errno) {
	if n.dir {
		return nil, 0, syscall.EISDIR
	}
	f := n.f
	key := n.k()
	write := flags&syscall.O_ACCMODE != syscall.O_RDONLY
	if write {
		if err := f.prepareWrite(ctx, key, flags&syscall.O_TRUNC != 0); err != nil {
			return nil, 0, errno(err)
		}
		if flags&syscall.O_TRUNC != 0 {
			if err := f.cache.Truncate(key, 0); err != nil {
				return nil, 0, syscall.EIO
			}
		}
	} else if err := f.prepareRead(key); err != nil {
		return nil, 0, errno(err)
	}
	f.cache.Opened(key, write)
	return &handle{n: n, write: write}, 0, 0
}

// prepareRead makes sure a cache entry for the current version exists (no network).
func (f *FS) prepareRead(key string) error {
	it, loc := f.item(key)
	if loc != nil {
		return nil
	}
	if it == nil {
		return syscall.ENOENT
	}
	return f.cache.Prepare(key, it.ContentEtag, it.Size)
}

// prepareWrite makes the whole file local first: writes go to a complete copy, so the
// upload carries the full file. With O_TRUNC nothing needs fetching.
func (f *FS) prepareWrite(ctx context.Context, key string, trunc bool) error {
	it, loc := f.item(key)
	if loc != nil {
		return nil
	}
	if it == nil {
		return syscall.ENOENT
	}
	if e, ok := f.cache.Get(key); ok && e.Dirty {
		return nil
	}
	if err := f.cache.Prepare(key, it.ContentEtag, it.Size); err != nil {
		return err
	}
	if trunc {
		return nil
	}
	return f.fetchRange(ctx, key, 0, it.Size, false)
}

func (h *handle) Read(ctx context.Context, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	f := h.n.f
	key := h.n.k()
	e, ok := f.cache.Get(key)
	if !ok {
		if err := f.prepareRead(key); err != nil {
			return nil, errno(err)
		}
		e, _ = f.cache.Get(key)
	}
	if !e.Complete && !e.Dirty {
		if err := f.fetchRange(ctx, key, off, int64(len(dest)), true); err != nil {
			return nil, errno(err)
		}
	}
	n, err := f.cache.ReadAt(f.keyNow(h.n), dest, off)
	if err != nil && n == 0 {
		return nil, syscall.EIO
	}
	return fuse.ReadResultData(dest[:n]), 0
}

func (f *FS) keyNow(n *Node) string { return n.k() }

func (h *handle) Write(ctx context.Context, data []byte, off int64) (uint32, syscall.Errno) {
	if !h.write {
		return 0, syscall.EBADF
	}
	f := h.n.f
	key := h.n.k()
	n, err := f.cache.WriteAt(key, data, off)
	if err != nil {
		f.log.Printf("write %s: %v", key, err)
		return uint32(n), syscall.EIO
	}
	f.mu.Lock()
	delete(f.failed, key)
	f.mu.Unlock()
	return uint32(n), 0
}

func (h *handle) Flush(ctx context.Context) syscall.Errno {
	if h.write {
		h.n.f.enqueue(h.n.k())
	}
	return 0
}

func (h *handle) Release(ctx context.Context) syscall.Errno {
	h.once.Do(func() {
		key := h.n.k()
		h.n.f.cache.Closed(key, h.write)
		if h.write {
			h.n.f.enqueue(key)
		}
	})
	return 0
}

// Fsync uploads now. Offline the data is safe in the cache and stays queued, so fsync
// succeeds; a refusal by the gateway is reported.
func (h *handle) Fsync(ctx context.Context, flags uint32) syscall.Errno {
	f := h.n.f
	key := h.n.k()
	if e, ok := f.cache.Get(key); !ok || !e.Dirty {
		return 0
	}
	err := f.uploadNow(ctx, key)
	if err == nil || gateway.IsUnreachable(err) || gateway.StateOf(err) == gateway.StateCantReachNAS {
		return 0
	}
	if errors.Is(err, errBusy) {
		return 0
	}
	return errno(err)
}

func (n *Node) Setattr(ctx context.Context, fh fs.FileHandle, in *fuse.SetAttrIn, out *fuse.AttrOut) syscall.Errno {
	f := n.f
	key := n.k()
	if sz, ok := in.GetSize(); ok && !n.dir {
		if err := f.prepareWrite(ctx, key, sz == 0); err != nil {
			return errno(err)
		}
		if err := f.cache.Truncate(key, int64(sz)); err != nil {
			return syscall.EIO
		}
		if f.cache.Writers(key) == 0 {
			f.enqueue(key)
		}
	}
	// Modes, owners and times are not stored by the gateway; accept them so cp -p,
	// rsync and editors don't fail.
	return f.attr(key, n.dir, &out.Attr)
}

// fetchRange makes [off, off+n) of the current version local. readAhead widens small
// reads so sequential reading doesn't make one request per 128 KiB.
func (f *FS) fetchRange(ctx context.Context, key string, off, n int64, readAhead bool) error {
	f.fetchMu.Lock()
	m := f.fetching[key]
	if m == nil {
		m = &sync.Mutex{}
		f.fetching[key] = m
	}
	f.fetchMu.Unlock()
	m.Lock()
	defer m.Unlock()
	for attempt := 0; attempt < 3; attempt++ {
		e, ok := f.cache.Get(key)
		if !ok {
			return syscall.ENOENT
		}
		if e.Complete || e.Dirty {
			return nil
		}
		spans := f.cache.Missing(key, off, n)
		if len(spans) == 0 {
			return nil
		}
		stale := false
		for i, s := range spans {
			l := s.Len
			if readAhead && i == len(spans)-1 {
				l = max(l, min(8*cache.BlockSize, e.Size-s.Off))
			}
			r, err := f.c.ReadRange(ctx, key, s.Off, l)
			if err != nil {
				return err
			}
			if r.Etag != e.Etag {
				stale = true
				break
			}
			if err := f.cache.Fill(key, e.Etag, r.Start, r.Data); err != nil {
				if errors.Is(err, cache.ErrStale) {
					stale = true
					break
				}
				return err
			}
			if r.Total != e.Size {
				stale = true
				break
			}
		}
		if !stale {
			return nil
		}
		// The NAS copy moved on: take the gateway's current metadata and start over.
		it, err := f.refreshItem(ctx, key)
		if err != nil {
			return err
		}
		if err := f.cache.Prepare(key, it.ContentEtag, it.Size); err != nil {
			return err
		}
		n = min(n, max(it.Size-off, 0))
	}
	return gateway.ErrChangedDuringTransfer
}

// fetchWhole downloads every missing byte (pinned files).
func (f *FS) fetchWhole(ctx context.Context, key string) error {
	e, ok := f.cache.Get(key)
	if !ok {
		return nil
	}
	return f.fetchRange(ctx, key, 0, e.Size, false)
}

// ── create / mkdir / unlink / rmdir / rename ────────────────────────────────

func (n *Node) Create(ctx context.Context, name string, flags uint32, mode uint32, out *fuse.EntryOut) (*fs.Inode, fs.FileHandle, uint32, syscall.Errno) {
	f := n.f
	name = names.NFC(name)
	if !names.Valid(name) {
		return nil, nil, 0, syscall.EINVAL
	}
	dir := n.k()
	_ = f.ensureListed(ctx, dir) // best effort: offline we still create
	f.mu.Lock()
	if _, _, ok := f.childLocked(dir, name); ok {
		f.mu.Unlock()
		return nil, nil, 0, syscall.EEXIST
	}
	// The NAS is case-insensitive: two names that differ only in case can't both live there.
	if !excluded(name) {
		k := names.Key(name)
		for other := range f.kids[dir] {
			if names.Key(other) == k {
				f.mu.Unlock()
				return nil, nil, 0, syscall.EEXIST
			}
		}
	}
	key := newLocalKey()
	if err := f.cache.NewLocal(key); err != nil {
		f.mu.Unlock()
		return nil, nil, 0, syscall.EIO
	}
	f.locals[key] = &Local{Key: key, Parent: dir, Name: name, Excluded: excluded(name), Created: time.Now()}
	f.pendDirt = true
	f.mu.Unlock()
	f.attr(key, false, &out.Attr)
	in := n.inode(ctx, key, false)
	f.cache.Opened(key, true)
	return in, &handle{n: in.Operations().(*Node), write: true}, 0, 0
}

func (n *Node) Mkdir(ctx context.Context, name string, mode uint32, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	f := n.f
	name = names.NFC(name)
	if !names.Valid(name) {
		return nil, syscall.EINVAL
	}
	ctx, cancel := opCtx(ctx)
	defer cancel()
	it, err := f.c.Mkdir(ctx, n.k(), name)
	if a, ok := gateway.AsAPI(err); ok && a.Code == "name_exists" && a.ExistingID != "" {
		// Same folder made elsewhere meanwhile: use it (the reference client merges too).
		it, err = f.c.Item(ctx, a.ExistingID)
		if err == nil && !it.IsDir() {
			return nil, syscall.EEXIST
		}
	}
	if err != nil {
		return nil, errno(err)
	}
	f.mu.Lock()
	notes := f.upsertLocked(it)
	f.listed[it.ID] = time.Now()
	f.mu.Unlock()
	f.runNotes(notes)
	f.attr(it.ID, true, &out.Attr)
	return n.inode(ctx, it.ID, true), 0
}

func (n *Node) Unlink(ctx context.Context, name string) syscall.Errno {
	f := n.f
	name = names.NFC(name)
	dir := n.k()
	f.mu.Lock()
	key, isDir, ok := f.childLocked(dir, name)
	f.mu.Unlock()
	if !ok {
		return syscall.ENOENT
	}
	if isDir {
		return syscall.EISDIR
	}
	return errno(f.removeFile(ctx, key))
}

// removeFile deletes a file: a local one just goes; a gateway one goes to the
// gateway's trash, but only if it still is the version we know (if_match).
func (f *FS) removeFile(ctx context.Context, key string) error {
	f.mu.Lock()
	if l := f.locals[key]; l != nil && f.items[key] == nil {
		delete(f.locals, key)
		f.dequeueLocked(key)
		if f.busy[key] {
			f.deleted[key] = true // the upload in flight deletes its result
		}
		delete(f.failed, key)
		f.pendDirt = true
		f.mu.Unlock()
		f.cache.Drop(key, true)
		return nil
	}
	it := f.items[key]
	f.mu.Unlock()
	if it == nil {
		return syscall.ENOENT
	}
	ctx, cancel := opCtx(ctx)
	defer cancel()
	_, err := f.c.Delete(ctx, key, it.ContentEtag)
	if a, ok := gateway.AsAPI(err); ok && a.Status == 412 {
		// Changed on the NAS after we last saw it: keep the newer content.
		f.refreshItem(ctx, key)
		return syscall.ESTALE
	}
	if err != nil && !gateway.IsCode(err, "not_found") {
		return err
	}
	f.mu.Lock()
	f.dequeueLocked(key)
	delete(f.failed, key)
	notes := f.removeRemoteLocked(key)
	delete(f.locals, key)
	f.mu.Unlock()
	f.cache.Drop(key, true)
	f.runNotes(notes)
	return nil
}

func (n *Node) Rmdir(ctx context.Context, name string) syscall.Errno {
	f := n.f
	name = names.NFC(name)
	dir := n.k()
	f.mu.Lock()
	key, isDir, ok := f.childLocked(dir, name)
	f.mu.Unlock()
	if !ok {
		return syscall.ENOENT
	}
	if !isDir {
		return syscall.ENOTDIR
	}
	return errno(f.removeDir(ctx, key))
}

func (f *FS) removeDir(ctx context.Context, key string) error {
	ctx, cancel := opCtx(ctx)
	defer cancel()
	if err := f.ensureListed(ctx, key); err != nil {
		return err
	}
	f.mu.Lock()
	busy := len(f.kids[key]) > 0
	for _, l := range f.locals {
		busy = busy || l.Parent == key
	}
	f.mu.Unlock()
	if busy {
		return syscall.ENOTEMPTY
	}
	// The NAS may hold children this machine never saw: never take them along.
	if p, err := f.c.Children(ctx, key, "", 1); err != nil {
		return err
	} else if len(p.Items) > 0 {
		f.mu.Lock()
		f.listed[key] = time.Time{}
		f.mu.Unlock()
		return syscall.ENOTEMPTY
	}
	if _, err := f.c.Delete(ctx, key, ""); err != nil && !gateway.IsCode(err, "not_found") {
		return err
	}
	f.mu.Lock()
	notes := f.removeRemoteLocked(key)
	f.mu.Unlock()
	f.runNotes(notes)
	return nil
}

const (
	renameNoReplace = 1 // RENAME_NOREPLACE
	renameExchange  = 2 // RENAME_EXCHANGE
)

func (n *Node) Rename(ctx context.Context, name string, newParent fs.InodeEmbedder, newName string, flags uint32) syscall.Errno {
	f := n.f
	if flags&renameExchange != 0 {
		return syscall.EINVAL
	}
	np, ok := newParent.(*Node)
	if !ok {
		return syscall.EXDEV
	}
	name, newName = names.NFC(name), names.NFC(newName)
	if !names.Valid(newName) {
		return syscall.EINVAL
	}
	srcDir, dstDir := n.k(), np.k()
	ctx, cancel := opCtx(ctx)
	defer cancel()
	if err := f.ensureListed(ctx, dstDir); err != nil && !gateway.IsUnreachable(err) {
		return errno(err)
	}
	f.mu.Lock()
	src, srcIsDir, ok := f.childLocked(srcDir, name)
	dst, dstIsDir, exists := f.childLocked(dstDir, newName)
	srcLocal := f.locals[src] != nil && f.items[src] == nil
	dstLocal := exists && f.locals[dst] != nil && f.items[dst] == nil
	f.mu.Unlock()
	if !ok {
		return syscall.ENOENT
	}
	if exists && dst == src {
		return 0
	}
	if exists && flags&renameNoReplace != 0 {
		return syscall.EEXIST
	}
	if exists && srcIsDir != dstIsDir {
		if srcIsDir {
			return syscall.ENOTDIR
		}
		return syscall.EISDIR
	}

	if srcLocal {
		if exists && !dstLocal {
			// An editor's safe-save: the new file replaces the old one. It becomes a
			// modify of the old item (compare-and-swap on the version we knew), so the
			// NAS gets one new version, not a delete and a create.
			return errno(f.adoptLocal(src, dst))
		}
		if exists && dstLocal {
			f.removeFile(ctx, dst)
		}
		f.mu.Lock()
		if l := f.locals[src]; l != nil {
			wasExcluded := l.Excluded
			l.Parent, l.Name, l.Excluded = dstDir, newName, excluded(newName)
			f.pendDirt = true
			if wasExcluded && !l.Excluded {
				f.enqueueLocked(src, f.opt.UploadDelay)
			}
			if l.Excluded {
				f.dequeueLocked(src)
			}
		}
		f.mu.Unlock()
		return 0
	}

	// A file or folder the gateway has.
	if excluded(newName) {
		f.log.Printf("rename %q -> %q refused: names like that stay on this computer", name, newName)
		return syscall.EINVAL
	}
	if exists {
		var err error
		if dstLocal {
			err = f.removeFile(ctx, dst)
		} else if dstIsDir {
			err = f.removeDir(ctx, dst)
		} else {
			err = f.removeFile(ctx, dst)
		}
		if err != nil {
			return errno(err)
		}
	}
	it, _ := f.item(src)
	if it == nil {
		return syscall.ENOENT
	}
	var nm, parent string
	if newName != it.Name {
		nm = newName
	}
	if dstDir != srcDir {
		parent = dstDir
	}
	got, err := f.c.Patch(ctx, src, nm, parent, it.MetaEtag)
	if a, ok := gateway.AsAPI(err); ok && a.Status == 412 {
		// Renamed elsewhere since: names are last-writer-wins (content is compare-and-swap).
		got, err = f.c.Patch(ctx, src, nm, parent, "")
	}
	if err != nil {
		return errno(err)
	}
	f.mu.Lock()
	notes := f.upsertLocked(got)
	f.mu.Unlock()
	f.runNotes(notes)
	return 0
}

// adoptLocal turns local file src into the new content of gateway file dst.
func (f *FS) adoptLocal(src, dst string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	it := f.items[dst]
	if it == nil {
		return syscall.ENOENT
	}
	if f.busy[src] {
		// Its first upload is in flight; let it finish as its own file, then rename.
		// Rare (the upload waits UploadDelay after close); report busy so the editor retries.
		return syscall.EBUSY
	}
	f.dequeueLocked(src)
	f.dequeueLocked(dst)
	delete(f.locals, src)
	f.cache.Drop(dst, true)
	if err := f.cache.Rekey(src, dst); err != nil {
		return err
	}
	f.cache.Rebase(dst, it.ContentEtag)
	if ino, ok := f.inos[src]; ok {
		f.inos[dst] = ino
	}
	if in := f.inodes[src]; in != nil {
		if live, ok := in.Operations().(*Node); ok {
			live.key = dst
		}
		f.inodes[dst] = in
		delete(f.inodes, src)
	}
	delete(f.failed, dst)
	f.pendDirt = true
	f.enqueueLocked(dst, f.opt.UploadDelay)
	return nil
}

// ── statfs / xattrs ─────────────────────────────────────────────────────────

func (n *Node) Statfs(ctx context.Context, out *fuse.StatfsOut) syscall.Errno {
	var st syscall.Statfs_t
	if err := syscall.Statfs(n.f.opt.CacheDir, &st); err != nil {
		return 0
	}
	out.FromStatfsT(&st)
	out.NameLen = 255
	return 0
}

const (
	xaPinned = "user.loft.pinned"
	xaState  = "user.loft.state"
)

func (n *Node) xattr(name string) (string, bool) {
	f := n.f
	key := n.k()
	switch name {
	case xaPinned:
		if e, ok := f.cache.Get(key); ok && e.Pinned {
			return "1", true
		}
		return "0", true
	case xaState:
		if n.dir {
			return "folder", true
		}
		return f.fileState(key), true
	}
	return "", false
}

func (f *FS) fileState(key string) string {
	f.mu.Lock()
	l := f.locals[key]
	busy := f.busy[key]
	_, failed := f.failed[key]
	f.mu.Unlock()
	e, ok := f.cache.Get(key)
	switch {
	case l != nil && l.Excluded:
		return "local-only"
	case failed:
		return "upload-failed"
	case busy:
		return "uploading"
	case ok && e.Dirty:
		return "pending-upload"
	case ok && e.Complete:
		return "cached"
	case ok && e.Size > 0 && len(f.cache.Missing(key, 0, e.Size)) < int((e.Size+cache.BlockSize-1)/cache.BlockSize):
		return "partly-cached"
	}
	return "online-only"
}

func (n *Node) Getxattr(ctx context.Context, attr string, dest []byte) (uint32, syscall.Errno) {
	v, ok := n.xattr(attr)
	if !ok {
		return 0, syscall.ENODATA
	}
	if len(dest) == 0 {
		return uint32(len(v)), 0
	}
	if len(dest) < len(v) {
		return uint32(len(v)), syscall.ERANGE
	}
	return uint32(copy(dest, v)), 0
}

func (n *Node) Listxattr(ctx context.Context, dest []byte) (uint32, syscall.Errno) {
	v := xaPinned + "\x00" + xaState + "\x00"
	if len(dest) == 0 {
		return uint32(len(v)), 0
	}
	if len(dest) < len(v) {
		return uint32(len(v)), syscall.ERANGE
	}
	return uint32(copy(dest, v)), 0
}

// Setxattr user.loft.pinned=1 keeps a file on this computer (downloaded now, kept
// current, never evicted); 0 lets the cache evict it again.
func (n *Node) Setxattr(ctx context.Context, attr string, data []byte, flags uint32) syscall.Errno {
	if attr != xaPinned {
		return syscall.ENOTSUP
	}
	on := len(data) > 0 && data[0] == '1'
	return errno(n.f.Pin(ctx, n.k(), n.dir, on))
}

func (n *Node) Removexattr(ctx context.Context, attr string) syscall.Errno {
	if attr != xaPinned {
		return syscall.ENODATA
	}
	return errno(n.f.Pin(ctx, n.k(), n.dir, false))
}

// Pin keeps a file (or every file already listed in a folder) local.
func (f *FS) Pin(ctx context.Context, key string, dir bool, on bool) error {
	if dir {
		return syscall.EISDIR
	}
	if err := f.prepareRead(key); err != nil {
		return err
	}
	f.cache.SetPinned(key, on)
	if on {
		return f.fetchWhole(ctx, key)
	}
	return nil
}

// ── errors ──────────────────────────────────────────────────────────────────

// errno maps gateway answers to what a Linux program understands.
func errno(err error) syscall.Errno {
	if err == nil {
		return 0
	}
	var en syscall.Errno
	if errors.As(err, &en) {
		return en
	}
	if errors.Is(err, context.Canceled) {
		return syscall.EINTR
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return syscall.ETIMEDOUT
	}
	if errors.Is(err, gateway.ErrNotSignedIn) {
		return syscall.EACCES
	}
	if errors.Is(err, gateway.ErrChangedDuringTransfer) {
		return syscall.EAGAIN
	}
	if gateway.IsUnreachable(err) {
		return syscall.EHOSTUNREACH
	}
	if a, ok := gateway.AsAPI(err); ok {
		switch a.Code {
		case "not_found", "root_missing", "no_such_share":
			return syscall.ENOENT
		case "name_exists", "name_exists_on_nas":
			return syscall.EEXIST
		case "invalid_or_excluded_name", "bad_path", "move_into_self", "parent_and_name_required":
			return syscall.EINVAL
		case "access_denied", "share_blocked":
			return syscall.EACCES
		case "share_is_fixed", "cannot_move_root", "cannot_delete_root":
			return syscall.EPERM
		case "is_a_folder":
			return syscall.EISDIR
		case "not_a_folder":
			return syscall.ENOTDIR
		case "cross_share_move":
			return syscall.EXDEV // mv falls back to copy + delete
		case "content_etag_mismatch", "meta_etag_mismatch":
			return syscall.ESTALE
		case "too_many_attempts", "busy":
			return syscall.EAGAIN
		}
		if a.Status == 401 {
			return syscall.EACCES
		}
	}
	return syscall.EIO
}
