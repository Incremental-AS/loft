// Package cache is the local content cache (~/.cache/loft): sparse files filled block by
// block from Range reads, a size cap with LRU eviction of unpinned files, and the local
// edits that wait for upload. It follows docs/design.md §E.1: the disk is a cache, not a
// mirror; pinned files stay; content that the gateway has not acknowledged is never
// evicted.
package cache

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/bits"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// BlockSize is the unit of partial caching.
const BlockSize = 1 << 20

// ErrStale: the entry now holds another version than the bytes being filled in.
var ErrStale = errors.New("cache entry changed version")

// Entry describes one cached file.
type Entry struct {
	Key        string    `json:"key"`
	Etag       string    `json:"etag"`      // content etag the clean bytes are ("" = local only)
	Size       int64     `json:"size"`      // logical size
	Complete   bool      `json:"complete"`  // every byte present
	Blocks     []byte    `json:"blocks"`    // bitmap of present blocks while !Complete
	Dirty      bool      `json:"dirty"`     // local changes the gateway hasn't acknowledged
	BaseEtag   string    `json:"base_etag"` // version the local changes started from (if_match)
	Gen        uint64    `json:"gen"`       // bumps on every local change
	Pinned     bool      `json:"pinned"`
	LastAccess time.Time `json:"last_access"`
	UploadedAt time.Time `json:"uploaded_at,omitempty"`
	open       int
	writers    int
}

// Cache is safe for concurrent use. Disk I/O happens under the lock; network I/O never.
type Cache struct {
	dir     string
	limit   int64
	mu      sync.Mutex
	saveMu  sync.Mutex
	entries map[string]*Entry
	files   map[string]*os.File
	changed bool
}

type index struct {
	Entries []*Entry `json:"entries"`
}

// Open loads the index and removes content files that no entry owns.
func Open(dir string, limit int64) (*Cache, error) {
	c := &Cache{dir: dir, limit: limit, entries: map[string]*Entry{}, files: map[string]*os.File{}}
	if err := os.MkdirAll(filepath.Join(dir, "content"), 0o700); err != nil {
		return nil, err
	}
	b, err := os.ReadFile(filepath.Join(dir, "index.json"))
	if err == nil {
		var ix index
		if json.Unmarshal(b, &ix) == nil {
			for _, e := range ix.Entries {
				if _, err := os.Stat(c.path(e.Key)); err == nil {
					c.entries[e.Key] = e
				}
			}
		}
	}
	owned := map[string]bool{}
	for k := range c.entries {
		owned[filepath.Base(c.path(k))] = true
	}
	if des, err := os.ReadDir(filepath.Join(dir, "content")); err == nil {
		for _, d := range des {
			if !owned[d.Name()] {
				os.Remove(filepath.Join(dir, "content", d.Name()))
			}
		}
	}
	return c, nil
}

func (c *Cache) SetLimit(n int64) {
	c.mu.Lock()
	c.limit = n
	c.mu.Unlock()
}

func (c *Cache) path(key string) string {
	h := sha256.Sum256([]byte(key))
	return filepath.Join(c.dir, "content", hex.EncodeToString(h[:20]))
}

// Path of the content file for a key (for uploads).
func (c *Cache) Path(key string) string { return c.path(key) }

func (c *Cache) file(key string) (*os.File, error) {
	if f := c.files[key]; f != nil {
		return f, nil
	}
	f, err := os.OpenFile(c.path(key), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if e := c.entries[key]; e != nil && e.open > 0 {
		c.files[key] = f
	}
	return f, nil
}

func (c *Cache) release(key string, f *os.File) {
	if c.files[key] != f {
		f.Close()
	}
}

func (c *Cache) closeFile(key string) {
	if f := c.files[key]; f != nil {
		f.Close()
		delete(c.files, key)
	}
}

// Get returns a copy of the entry.
func (c *Cache) Get(key string) (Entry, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil {
		return Entry{}, false
	}
	return *e, true
}

// Prepare makes sure an entry for this version exists. A clean entry holding another
// version is emptied; a dirty one is left alone (the upload will settle it).
func (c *Cache) Prepare(key, etag string, size int64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e != nil && (e.Dirty || (e.Etag == etag && e.Size == size)) {
		return nil
	}
	if e == nil {
		e = &Entry{Key: key}
		c.entries[key] = e
	}
	f, err := c.file(key)
	if err != nil {
		return err
	}
	defer c.release(key, f)
	if err := f.Truncate(0); err != nil {
		return err
	}
	e.Etag, e.Size, e.Complete, e.Blocks, e.UploadedAt = etag, size, size == 0, nil, time.Time{}
	if size > 0 {
		e.Blocks = make([]byte, (nblocks(size)+7)/8)
	}
	e.LastAccess = time.Now()
	c.changed = true
	return nil
}

func nblocks(size int64) int64 { return (size + BlockSize - 1) / BlockSize }

func (e *Entry) has(b int64) bool { return e.Blocks[b/8]&(1<<(b%8)) != 0 }

// Span is a byte range.
type Span struct{ Off, Len int64 }

// Missing returns the block-aligned spans not yet cached within [off, off+n).
func (c *Cache) Missing(key string, off, n int64) []Span {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil {
		return []Span{{off, n}}
	}
	if e.Complete || e.Dirty {
		return nil
	}
	end := min(off+n, e.Size)
	var out []Span
	for b := off / BlockSize; b*BlockSize < end; b++ {
		if e.has(b) {
			continue
		}
		s := b * BlockSize
		l := min(int64(BlockSize), e.Size-s)
		if k := len(out); k > 0 && out[k-1].Off+out[k-1].Len == s {
			out[k-1].Len += l
		} else {
			out = append(out, Span{s, l})
		}
	}
	return out
}

// Fill stores bytes fetched for version etag at off. Blocks fully covered (or reaching
// the end of the file) become present.
func (c *Cache) Fill(key, etag string, off int64, data []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil || e.Etag != etag || e.Dirty {
		return ErrStale
	}
	if e.Complete {
		return nil
	}
	f, err := c.file(key)
	if err != nil {
		return err
	}
	defer c.release(key, f)
	if _, err := f.WriteAt(data, off); err != nil {
		return err
	}
	end := off + int64(len(data))
	for b := (off + BlockSize - 1) / BlockSize; b*BlockSize < e.Size; b++ {
		bs, be := b*BlockSize, min((b+1)*BlockSize, e.Size)
		if bs < off || be > end {
			break
		}
		e.Blocks[b/8] |= 1 << (b % 8)
	}
	all := true
	for b := int64(0); b < nblocks(e.Size); b++ {
		if !e.has(b) {
			all = false
			break
		}
	}
	if all {
		e.Complete, e.Blocks = true, nil
		f.Truncate(e.Size)
	}
	c.changed = true
	return nil
}

// SetWhole records a file fetched whole into Path(key) (Download) as complete.
func (c *Cache) SetWhole(key, etag string, size int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil {
		e = &Entry{Key: key}
		c.entries[key] = e
	}
	e.Etag, e.Size, e.Complete, e.Blocks, e.Dirty, e.BaseEtag = etag, size, true, nil, false, ""
	e.LastAccess = time.Now()
	c.changed = true
}

// ReadAt reads cached bytes. The caller made sure they are present.
func (c *Cache) ReadAt(key string, buf []byte, off int64) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil {
		return 0, os.ErrNotExist
	}
	e.LastAccess = time.Now()
	if off >= e.Size {
		return 0, nil
	}
	if int64(len(buf)) > e.Size-off {
		buf = buf[:e.Size-off]
	}
	f, err := c.file(key)
	if err != nil {
		return 0, err
	}
	defer c.release(key, f)
	n, err := f.ReadAt(buf, off)
	if n == len(buf) {
		err = nil
	}
	return n, err
}

func (c *Cache) dirty(e *Entry) {
	if !e.Dirty {
		e.Dirty, e.BaseEtag = true, e.Etag
	}
	e.Gen++
	e.UploadedAt = time.Time{}
	e.LastAccess = time.Now()
	c.changed = true
}

// WriteAt applies a local write. The entry must be complete (or new).
func (c *Cache) WriteAt(key string, data []byte, off int64) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil || !(e.Complete || e.Dirty) {
		return 0, errors.New("write to a file that is not fully cached")
	}
	f, err := c.file(key)
	if err != nil {
		return 0, err
	}
	defer c.release(key, f)
	n, err := f.WriteAt(data, off)
	if n > 0 {
		c.dirty(e)
		e.Size = max(e.Size, off+int64(n))
		e.Complete = true
	}
	return n, err
}

// Truncate sets the local size.
func (c *Cache) Truncate(key string, size int64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil || !(e.Complete || e.Dirty || size == 0) {
		return errors.New("truncate of a file that is not fully cached")
	}
	f, err := c.file(key)
	if err != nil {
		return err
	}
	defer c.release(key, f)
	if err := f.Truncate(size); err != nil {
		return err
	}
	c.dirty(e)
	e.Size, e.Complete, e.Blocks = size, true, nil
	return nil
}

// NewLocal creates an empty, dirty entry for a file made on this machine.
func (c *Cache) NewLocal(key string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := &Entry{Key: key, Complete: true}
	c.entries[key] = e
	f, err := c.file(key)
	if err != nil {
		return err
	}
	defer c.release(key, f)
	if err := f.Truncate(0); err != nil {
		return err
	}
	c.dirty(e)
	return nil
}

// Settle records an acknowledged upload of generation gen as version etag. If the file
// changed again meanwhile it stays dirty, now based on etag. Returns true when clean.
func (c *Cache) Settle(key string, gen uint64, etag string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil {
		return true
	}
	c.changed = true
	if e.Gen == gen {
		e.Dirty, e.Etag, e.BaseEtag, e.UploadedAt = false, etag, "", time.Now()
		return true
	}
	e.BaseEtag = etag
	return false
}

// Rebase keeps local changes but bases them on another version (after a conflict copy
// took our bytes, or the item vanished and the upload becomes a create).
func (c *Cache) Rebase(key, base string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if e := c.entries[key]; e != nil {
		e.BaseEtag = base
		c.changed = true
	}
}

// Gen returns the change generation of a key (0 if unknown).
func (c *Cache) Gen(key string) uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	if e := c.entries[key]; e != nil {
		return e.Gen
	}
	return 0
}

// Rekey moves an entry to a new key (a local file got its gateway id).
func (c *Cache) Rekey(old, new string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[old]
	if e == nil || old == new {
		return nil
	}
	if f := c.files[old]; f != nil {
		c.files[new] = f
		delete(c.files, old)
	}
	if prev := c.entries[new]; prev != nil {
		c.closeFile(new)
	}
	if err := os.Rename(c.path(old), c.path(new)); err != nil {
		return err
	}
	delete(c.entries, old)
	e.Key = new
	c.entries[new] = e
	c.changed = true
	return nil
}

// Drop removes an entry. Dirty entries are kept unless force.
func (c *Cache) Drop(key string, force bool) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil {
		return true
	}
	if e.Dirty && !force {
		return false
	}
	c.closeFile(key)
	os.Remove(c.path(key))
	delete(c.entries, key)
	c.changed = true
	return true
}

// Opened/Closed track open file handles (never evicted while open).
func (c *Cache) Opened(key string, write bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if e := c.entries[key]; e != nil {
		e.open++
		if write {
			e.writers++
		}
		e.LastAccess = time.Now()
	}
}

func (c *Cache) Closed(key string, write bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if e := c.entries[key]; e != nil {
		e.open = max(e.open-1, 0)
		if write {
			e.writers = max(e.writers-1, 0)
		}
		if e.open == 0 {
			c.closeFile(key)
		}
	}
}

// Writers reports open write handles.
func (c *Cache) Writers(key string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	if e := c.entries[key]; e != nil {
		return e.writers
	}
	return 0
}

func (c *Cache) SetPinned(key string, on bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if e := c.entries[key]; e != nil {
		e.Pinned = on
		c.changed = true
	}
}

// Used is the space the cached content takes.
func (c *Cache) Used() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.usedLocked()
}

func (c *Cache) usedLocked() int64 {
	var n int64
	for _, e := range c.entries {
		n += e.used()
	}
	return n
}

func (e *Entry) used() int64 {
	if e.Complete || e.Dirty {
		return e.Size
	}
	var cnt int
	for _, b := range e.Blocks {
		cnt += bits.OnesCount8(b)
	}
	return min(int64(cnt)*BlockSize, e.Size)
}

// Evict frees space: files acknowledged by the gateway and unused for evictAfter go
// first (docs/design.md §E.1), then least recently used ones until under the cap. Never dirty,
// pinned or open files. Returns the evicted keys.
func (c *Cache) Evict(evictAfter time.Duration) []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	var cands []*Entry
	for _, e := range c.entries {
		if !e.Dirty && !e.Pinned && e.open == 0 {
			cands = append(cands, e)
		}
	}
	sort.Slice(cands, func(i, j int) bool { return cands[i].LastAccess.Before(cands[j].LastAccess) })
	var out []string
	used := c.usedLocked()
	now := time.Now()
	for _, e := range cands {
		uploadedIdle := evictAfter > 0 && !e.UploadedAt.IsZero() && now.Sub(e.LastAccess) > evictAfter
		if !uploadedIdle && used <= c.limit {
			continue
		}
		used -= e.used()
		out = append(out, e.Key)
		c.closeFile(e.Key)
		os.Remove(c.path(e.Key))
		delete(c.entries, e.Key)
		c.changed = true
	}
	return out
}

// DirtyKeys lists entries waiting for upload.
func (c *Cache) DirtyKeys() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []string
	for k, e := range c.entries {
		if e.Dirty {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// Keys lists all entries.
func (c *Cache) Keys() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]string, 0, len(c.entries))
	for k := range c.entries {
		out = append(out, k)
	}
	return out
}

// Save writes the index if anything changed.
func (c *Cache) Save() error {
	c.saveMu.Lock()
	defer c.saveMu.Unlock()
	c.mu.Lock()
	if !c.changed {
		c.mu.Unlock()
		return nil
	}
	ix := index{}
	for _, e := range c.entries {
		cp := *e
		ix.Entries = append(ix.Entries, &cp)
	}
	c.changed = false
	c.mu.Unlock()
	b, err := json.Marshal(ix)
	if err != nil {
		return err
	}
	tmp := filepath.Join(c.dir, "index.json.tmp")
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(c.dir, "index.json"))
}

// Close saves and closes every open file.
func (c *Cache) Close() error {
	err := c.Save()
	c.mu.Lock()
	for k := range c.files {
		c.closeFile(k)
	}
	c.mu.Unlock()
	return err
}

// Clear removes everything, dirty content included (sign-out).
func (c *Cache) Clear() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	for k := range c.files {
		c.closeFile(k)
	}
	c.entries = map[string]*Entry{}
	c.changed = true
	if err := os.RemoveAll(filepath.Join(c.dir, "content")); err != nil {
		return err
	}
	return os.MkdirAll(filepath.Join(c.dir, "content"), 0o700)
}

// DirtyInIndex lists the keys with unacknowledged changes from the saved index, without
// opening the cache (safe while a mount runs).
func DirtyInIndex(dir string) []string {
	b, err := os.ReadFile(filepath.Join(dir, "index.json"))
	if err != nil {
		return nil
	}
	var ix index
	if json.Unmarshal(b, &ix) != nil {
		return nil
	}
	var out []string
	for _, e := range ix.Entries {
		if e.Dirty {
			out = append(out, e.Key)
		}
	}
	return out
}

func (e Entry) String() string {
	return fmt.Sprintf("%s etag=%s size=%d complete=%v dirty=%v", e.Key, e.Etag, e.Size, e.Complete, e.Dirty)
}
