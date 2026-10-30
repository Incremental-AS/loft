// Package vfs is the ~/Loft FUSE file system (hanwen/go-fuse v2, pure Go).
//
// Listings come from the gateway and are kept current by its change feed. File contents
// are read on demand with Range requests into the local cache. Writes land in the cache
// and are uploaded on close/fsync through upload sessions; the gateway's compare-and-swap
// turns a stale save into a conflict copy, as in the reference client. Offline, cached
// files stay readable and writes wait in the queue.
package vfs

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/cache"
	"github.com/Incremental-AS/loft/linux/internal/gateway"
	"github.com/Incremental-AS/loft/linux/internal/names"
	"github.com/hanwen/go-fuse/v2/fs"
)

// Options to build the file system.
type Options struct {
	Client     *gateway.Client
	Cache      *cache.Cache
	CacheDir   string // meta.json (listings for offline use)
	StateDir   string // pending.json (local files, upload queue, change anchor)
	RootID     string // the root item (GET /v1/me root.root_item_id)
	EvictAfter time.Duration
	Log        *log.Logger
	// UploadDelay: how long a closed file waits before upload, so an editor's
	// write-temp-then-rename lands as one new version (default 1 s).
	UploadDelay time.Duration
	// ListTTL: how long a folder listing is trusted before it is read again (the change
	// feed keeps it current meanwhile; a re-read makes the gateway look at that NAS folder,
	// which shows changes made over SMB sooner than its 5-minute rescan).
	ListTTL time.Duration
}

// Local is a file made on this machine that the gateway doesn't have yet (or never
// gets: excluded names such as Office lock files stay local).
type Local struct {
	Key      string    `json:"key"`
	Parent   string    `json:"parent"`
	Name     string    `json:"name"`
	Excluded bool      `json:"excluded"`
	Created  time.Time `json:"created"`
}

// FS holds the metadata tree, the local overlay and the upload queue.
type FS struct {
	opt   Options
	c     *gateway.Client
	cache *cache.Cache
	log   *log.Logger

	mu       sync.Mutex
	rootID   string
	items    map[string]*gateway.Item
	kids     map[string]map[string]string // dir id -> NFC name -> item id
	listed   map[string]time.Time
	locals   map[string]*Local
	inodes   map[string]*fs.Inode
	inos     map[string]uint64
	nextIno  uint64
	anchor   string
	resume   map[string]string // "<sha>:<size>" -> upload id
	queue    map[string]time.Time
	firstQ   map[string]time.Time
	busy     map[string]bool
	deleted  map[string]bool // local files unlinked during their upload
	failed   map[string]string
	backoff  map[string]time.Duration
	metaDirt bool
	pendDirt bool

	state     gateway.SyncState
	feedLive  time.Time
	lastSync  time.Time
	conflicts int
	stopUp    bool // uploads paused until sign-in

	wake     chan struct{}
	fetchMu  sync.Mutex
	fetching map[string]*sync.Mutex
	notify   chan func()
}

type metaFile struct {
	Root   string          `json:"root"`
	Items  []*gateway.Item `json:"items"`
	Listed []string        `json:"listed"`
}

type pendingFile struct {
	Root   string            `json:"root"`
	Anchor string            `json:"anchor"`
	Locals []*Local          `json:"locals"`
	Resume map[string]string `json:"resume"`
}

// New loads what the last run left (listings, local files, the change anchor).
func New(opt Options) (*FS, error) {
	if opt.Log == nil {
		opt.Log = log.New(os.Stderr, "loft: ", log.LstdFlags)
	}
	if opt.UploadDelay == 0 {
		opt.UploadDelay = time.Second
	}
	if opt.ListTTL == 0 {
		opt.ListTTL = 30 * time.Second
	}
	f := &FS{
		opt: opt, c: opt.Client, cache: opt.Cache, log: opt.Log, rootID: opt.RootID,
		items: map[string]*gateway.Item{}, kids: map[string]map[string]string{},
		listed: map[string]time.Time{}, locals: map[string]*Local{}, inodes: map[string]*fs.Inode{},
		inos: map[string]uint64{opt.RootID: 1}, nextIno: 2, resume: map[string]string{},
		queue: map[string]time.Time{}, firstQ: map[string]time.Time{}, busy: map[string]bool{},
		deleted: map[string]bool{}, failed: map[string]string{}, backoff: map[string]time.Duration{},
		state: gateway.StateOK, wake: make(chan struct{}, 1), fetching: map[string]*sync.Mutex{},
		notify: make(chan func(), 256),
	}
	if b, err := os.ReadFile(filepath.Join(opt.CacheDir, "meta.json")); err == nil {
		var m metaFile
		if json.Unmarshal(b, &m) == nil && m.Root == opt.RootID {
			for _, it := range m.Items {
				f.putItem(it)
			}
			// Loaded listings are a fallback for offline use; refresh before trusting.
			for _, id := range m.Listed {
				f.listed[id] = time.Time{}
			}
		}
	}
	if b, err := os.ReadFile(filepath.Join(opt.StateDir, "pending.json")); err == nil {
		var p pendingFile
		if json.Unmarshal(b, &p) == nil && p.Root == opt.RootID {
			f.anchor = p.Anchor
			for _, l := range p.Locals {
				if _, ok := opt.Cache.Get(l.Key); ok {
					f.locals[l.Key] = l
				}
			}
			if p.Resume != nil {
				f.resume = p.Resume
			}
		}
	}
	// Everything still dirty in the cache goes back in the queue.
	for _, k := range opt.Cache.DirtyKeys() {
		if f.locals[k] != nil || f.items[k] != nil {
			f.queue[k] = time.Now()
			f.firstQ[k] = time.Now()
		} else {
			f.log.Printf("cached changes for %s have no known place on the gateway; kept in the cache", k)
		}
	}
	return f, nil
}

// RootNode is the FUSE root.
func (f *FS) RootNode() *Node { return &Node{f: f, key: f.rootID, dir: true} }

// putItem stores an item and its place in its parent (caller holds mu or is init).
func (f *FS) putItem(it *gateway.Item) {
	cp := *it
	f.items[it.ID] = &cp
	if p := it.Parent(); p != "" {
		m := f.kids[p]
		if m == nil {
			m = map[string]string{}
			f.kids[p] = m
		}
		m[it.Name] = it.ID
	}
}

func (f *FS) unplace(it *gateway.Item) {
	if m := f.kids[it.Parent()]; m != nil && m[it.Name] == it.ID {
		delete(m, it.Name)
	}
}

// upsert applies a fresh item (listing, feed, API answer). Returns notifications to run
// once the lock is released.
func (f *FS) upsertLocked(it *gateway.Item) []func() {
	var out []func()
	old := f.items[it.ID]
	if old != nil {
		if old.Parent() != it.Parent() || old.Name != it.Name {
			f.unplace(old)
			out = append(out, f.entryNote(old.Parent(), old.Name))
		}
		if old.ContentEtag != it.ContentEtag && !it.IsDir() {
			id := it.ID
			if e, ok := f.cache.Get(id); ok && !e.Dirty {
				if e.Pinned {
					f.cache.Prepare(id, it.ContentEtag, it.Size)
					out = append(out, func() { go f.fetchWhole(context.Background(), id) })
				} else {
					f.cache.Drop(id, false)
				}
			}
			if in := f.inodes[id]; in != nil {
				out = append(out, func() { in.NotifyContent(0, 0) })
			}
		}
	} else {
		out = append(out, f.entryNote(it.Parent(), it.Name))
	}
	f.putItem(it)
	f.metaDirt = true
	return out
}

// removeRemoteLocked forgets an item the gateway deleted. Local changes to it are not
// lost: the file becomes a local one and is uploaded again under its name.
func (f *FS) removeRemoteLocked(id string) []func() {
	it := f.items[id]
	if it == nil {
		return nil
	}
	out := []func(){f.entryNote(it.Parent(), it.Name)}
	f.unplace(it)
	delete(f.items, id)
	delete(f.listed, id)
	f.metaDirt = true
	if e, ok := f.cache.Get(id); ok && e.Dirty && !it.IsDir() {
		f.locals[id] = &Local{Key: id, Parent: it.Parent(), Name: it.Name, Created: time.Now()}
		f.cache.Rebase(id, "")
		f.pendDirt = true
		f.enqueueLocked(id, 0)
		f.log.Printf("%s was deleted on the NAS while it had unsaved changes here; uploading it again", it.Name)
	} else {
		f.cache.Drop(id, false)
	}
	return out
}

func (f *FS) entryNote(parent, name string) func() {
	in := f.inodes[parent]
	if in == nil {
		return func() {}
	}
	return func() { in.NotifyEntry(name) }
}

// runNotes hands kernel cache invalidations to a goroutine: they must never run while
// a FUSE operation waits on our lock.
func (f *FS) runNotes(n []func()) {
	for _, fn := range n {
		select {
		case f.notify <- fn:
		default: // a full queue only means the 1 s entry timeout does the work
		}
	}
}

func (f *FS) notifier(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case fn := <-f.notify:
			fn()
		}
	}
}

func (f *FS) ino(key string) uint64 {
	if n, ok := f.inos[key]; ok {
		return n
	}
	n := f.nextIno
	f.nextIno++
	f.inos[key] = n
	return n
}

func newLocalKey() string {
	b := make([]byte, 12)
	rand.Read(b)
	return "local-" + hex.EncodeToString(b)
}

// child finds a name in a folder: local files first, then the gateway's listing.
func (f *FS) childLocked(dir, name string) (key string, isDir bool, ok bool) {
	for _, l := range f.locals {
		if l.Parent == dir && l.Name == name {
			return l.Key, false, true
		}
	}
	if id, ok := f.kids[dir][name]; ok {
		if it := f.items[id]; it != nil {
			return id, it.IsDir(), true
		}
	}
	return "", false, false
}

// ensureListed reads a folder from the gateway unless the listing is current. Offline
// (or NAS down) a listing from before is used.
func (f *FS) ensureListed(ctx context.Context, dir string) error {
	f.mu.Lock()
	t, have := f.listed[dir]
	fresh := have && !t.IsZero() && time.Since(t) < f.opt.ListTTL
	// Known to be offline: don't make every `ls` wait for a connect timeout.
	fresh = fresh || (have && f.state == gateway.StateCantReachHome)
	_, isLocal := f.locals[dir]
	f.mu.Unlock()
	if fresh || isLocal {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	items, err := f.c.AllChildren(ctx, dir)
	if err != nil {
		if have && (gateway.IsUnreachable(err) || gateway.StateOf(err) == gateway.StateCantReachNAS ||
			errors.Is(err, gateway.ErrNotSignedIn)) {
			return nil
		}
		return err
	}
	f.mu.Lock()
	seen := map[string]bool{}
	var notes []func()
	for i := range items {
		seen[items[i].ID] = true
		notes = append(notes, f.upsertLocked(&items[i])...)
	}
	for _, id := range f.kids[dir] {
		if !seen[id] {
			if it := f.items[id]; it != nil && it.Parent() == dir {
				notes = append(notes, f.removeRemoteLocked(id)...)
			}
		}
	}
	f.listed[dir] = time.Now()
	f.metaDirt = true
	f.mu.Unlock()
	f.runNotes(notes)
	return nil
}

// Item returns a copy of an item's metadata.
func (f *FS) item(key string) (*gateway.Item, *Local) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var it *gateway.Item
	if x := f.items[key]; x != nil {
		cp := *x
		it = &cp
	}
	var l *Local
	if x := f.locals[key]; x != nil {
		cp := *x
		l = &cp
	}
	return it, l
}

// refreshItem re-reads one item from the gateway.
func (f *FS) refreshItem(ctx context.Context, key string) (*gateway.Item, error) {
	it, err := f.c.Item(ctx, key)
	if err != nil {
		if gateway.IsCode(err, "not_found") {
			f.mu.Lock()
			n := f.removeRemoteLocked(key)
			f.mu.Unlock()
			f.runNotes(n)
		}
		return nil, err
	}
	f.mu.Lock()
	n := f.upsertLocked(it)
	f.mu.Unlock()
	f.runNotes(n)
	return it, nil
}

// Start runs the change feed, the uploader and housekeeping until ctx ends.
func (f *FS) Start(ctx context.Context) {
	go f.notifier(ctx)
	go f.watch(ctx)
	go f.uploader(ctx)
	go f.housekeeping(ctx)
}

func (f *FS) housekeeping(ctx context.Context) {
	save := time.NewTicker(3 * time.Second)
	evict := time.NewTicker(time.Minute)
	defer save.Stop()
	defer evict.Stop()
	for {
		select {
		case <-ctx.Done():
			f.Persist()
			return
		case <-save.C:
			f.Persist()
		case <-evict.C:
			f.Evict()
		}
	}
}

// Evict trims the cache (docs/design.md §E.1).
func (f *FS) Evict() {
	if keys := f.cache.Evict(f.opt.EvictAfter); len(keys) > 0 {
		f.log.Printf("cache: freed %d files", len(keys))
	}
}

// Persist writes listings, local files and the anchor if they changed.
func (f *FS) Persist() {
	f.mu.Lock()
	var meta, pend []byte
	if f.metaDirt {
		m := metaFile{Root: f.rootID}
		for _, it := range f.items {
			m.Items = append(m.Items, it)
		}
		for id, t := range f.listed {
			if !t.IsZero() || f.items[id] != nil || id == f.rootID {
				m.Listed = append(m.Listed, id)
			}
		}
		sort.Strings(m.Listed)
		meta, _ = json.Marshal(m)
		f.metaDirt = false
	}
	if f.pendDirt {
		p := pendingFile{Root: f.rootID, Anchor: f.anchor, Resume: f.resume}
		for _, l := range f.locals {
			p.Locals = append(p.Locals, l)
		}
		pend, _ = json.MarshalIndent(p, "", " ")
		f.pendDirt = false
	}
	f.mu.Unlock()
	if meta != nil {
		writeAtomic(filepath.Join(f.opt.CacheDir, "meta.json"), meta)
	}
	if pend != nil {
		writeAtomic(filepath.Join(f.opt.StateDir, "pending.json"), pend)
	}
	f.cache.Save()
}

func writeAtomic(path string, b []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Snapshot is what `loft status` and the tray show.
type Snapshot struct {
	State     gateway.SyncState
	Route     gateway.Route
	Pending   int
	Uploading int
	Failed    []string
	LastSync  time.Time
	Conflicts int
}

func (f *FS) Snapshot() Snapshot {
	f.mu.Lock()
	defer f.mu.Unlock()
	s := Snapshot{State: f.state, LastSync: f.lastSync, Conflicts: f.conflicts}
	for _, k := range f.cache.DirtyKeys() {
		if l := f.locals[k]; l != nil && l.Excluded {
			continue
		}
		if _, failed := f.failed[k]; failed {
			continue
		}
		s.Pending++
	}
	for range f.busy {
		s.Uploading++
	}
	for k, why := range f.failed {
		name := k
		if l := f.locals[k]; l != nil {
			name = l.Name
		} else if it := f.items[k]; it != nil {
			name = it.Name
		}
		s.Failed = append(s.Failed, name+": "+why)
	}
	sort.Strings(s.Failed)
	s.Route = f.c.Route()
	return s
}

// ResumableUpload / SetResumableUpload implement gateway.ResumeStore.
func (f *FS) ResumableUpload(sha string, size int64) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.resume[resumeKey(sha, size)]
}

func (f *FS) SetResumableUpload(id, sha string, size int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if id == "" {
		delete(f.resume, resumeKey(sha, size))
	} else {
		f.resume[resumeKey(sha, size)] = id
	}
	f.pendDirt = true
}

func resumeKey(sha string, size int64) string { return sha + ":" + itoa(size) }

func itoa(n int64) string {
	b, _ := json.Marshal(n)
	return string(b)
}

// LocalOnly lists the files that never leave this computer (excluded names), from the
// saved state, so `loft logout` doesn't count them as waiting for upload.
func LocalOnly(stateDir string) map[string]bool {
	out := map[string]bool{}
	b, err := os.ReadFile(filepath.Join(stateDir, "pending.json"))
	if err != nil {
		return out
	}
	var p pendingFile
	if json.Unmarshal(b, &p) == nil {
		for _, l := range p.Locals {
			if l.Excluded {
				out[l.Key] = true
			}
		}
	}
	return out
}

// excluded reports whether a name stays on this machine.
func excluded(name string) bool { return names.Excluded(name) }
