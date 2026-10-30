package vfs

import (
	"context"
	"errors"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/gateway"
	"github.com/hanwen/go-fuse/v2/fs"
)

var errBusy = errors.New("upload already running")

// maxWaitForWriters: a file kept open for writing is still uploaded after this long
// (each flush/fsync enqueues it again anyway).
const maxWaitForWriters = 30 * time.Second

func (f *FS) enqueue(key string) {
	f.mu.Lock()
	f.enqueueLocked(key, f.opt.UploadDelay)
	f.mu.Unlock()
}

func (f *FS) enqueueLocked(key string, delay time.Duration) {
	if l := f.locals[key]; l != nil && l.Excluded {
		return
	}
	if e, ok := f.cache.Get(key); !ok || !e.Dirty {
		return
	}
	f.queue[key] = time.Now().Add(delay)
	if _, ok := f.firstQ[key]; !ok {
		f.firstQ[key] = time.Now()
	}
	select {
	case f.wake <- struct{}{}:
	default:
	}
}

func (f *FS) dequeueLocked(key string) {
	delete(f.queue, key)
	delete(f.firstQ, key)
	delete(f.backoff, key)
}

// uploader works through the queue, one file at a time, oldest first.
func (f *FS) uploader(ctx context.Context) {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		case <-f.wake:
		}
		for {
			key := f.nextDue()
			if key == "" {
				break
			}
			err := f.uploadOne(ctx, key)
			if ctx.Err() != nil {
				return
			}
			if err != nil && (gateway.IsUnreachable(err) || gateway.StateOf(err) == gateway.StateCantReachNAS) {
				break // the next tick tries again; no point hammering a dead route
			}
		}
	}
}

func (f *FS) nextDue() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.stopUp {
		return ""
	}
	now := time.Now()
	best := ""
	var bestT time.Time
	for k, t := range f.queue {
		if t.After(now) || f.busy[k] {
			continue
		}
		if f.cache.Writers(k) > 0 && now.Sub(f.firstQ[k]) < maxWaitForWriters {
			continue
		}
		if best == "" || f.firstQ[k].Before(bestT) {
			best, bestT = k, f.firstQ[k]
		}
	}
	return best
}

// uploadNow uploads one file right away (fsync).
func (f *FS) uploadNow(ctx context.Context, key string) error {
	for i := 0; i < 600; i++ { // wait for a running upload of the same file
		f.mu.Lock()
		busy := f.busy[key]
		f.mu.Unlock()
		if !busy {
			break
		}
		if err := sleepCtx(ctx, 100*time.Millisecond); err != nil {
			return err
		}
	}
	return f.uploadOne(ctx, key)
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// uploadOne sends one file's local changes and settles the answer: a new item for a
// local file, a new version for a gateway file, or a conflict copy when the NAS copy
// changed since this machine's base version (the gateway names it
// "Name (conflict, <device>, <date time>).ext"; the original keeps the NAS content).
func (f *FS) uploadOne(ctx context.Context, key string) error {
	f.mu.Lock()
	if f.busy[key] {
		f.mu.Unlock()
		return errBusy
	}
	loc := f.locals[key]
	it := f.items[key]
	var req gateway.CommitRequest
	switch {
	case loc != nil && it == nil:
		if loc.Excluded {
			f.dequeueLocked(key)
			f.mu.Unlock()
			return nil
		}
		if pl := f.locals[loc.Parent]; pl != nil {
			f.mu.Unlock()
			return nil
		}
		req = gateway.CommitRequest{ParentID: loc.Parent, Name: loc.Name}
	case it != nil:
		req = gateway.CommitRequest{ItemID: key}
	default:
		f.dequeueLocked(key)
		f.mu.Unlock()
		return nil
	}
	e, ok := f.cache.Get(key)
	if !ok || !e.Dirty {
		f.dequeueLocked(key)
		f.mu.Unlock()
		return nil
	}
	if req.ItemID != "" {
		req.IfMatch = e.BaseEtag
	}
	gen := e.Gen
	f.busy[key] = true
	delete(f.queue, key)
	f.mu.Unlock()

	resp, err := f.send(ctx, key, gen, req)

	f.mu.Lock()
	delete(f.busy, key)
	f.mu.Unlock()
	if errors.Is(err, errChanged) {
		f.enqueue(key) // written again meanwhile: next round takes the newer bytes
		return nil
	}
	if err != nil {
		f.uploadFailed(key, req, err)
		return err
	}
	f.settle(ctx, key, gen, req, resp)
	return nil
}

var errChanged = errors.New("changed during upload")

func (f *FS) send(ctx context.Context, key string, gen uint64, req gateway.CommitRequest) (*gateway.CommitResponse, error) {
	path := f.cache.Path(key)
	sha, size, err := gateway.HashFile(path)
	if err != nil {
		return nil, err
	}
	if f.cache.Gen(key) != gen {
		return nil, errChanged
	}
	near := req.ItemID
	if near == "" {
		near = req.ParentID
	}
	id, err := f.c.Upload(ctx, path, sha, size, near, f, nil)
	if err != nil {
		return nil, err
	}
	if f.cache.Gen(key) != gen {
		return nil, errChanged
	}
	resp, err := f.c.Commit(ctx, id, req)
	if err == nil || gateway.IsCode(err, "file_hash_mismatch") || gateway.IsCode(err, "no_such_upload") {
		f.SetResumableUpload("", sha, size)
	}
	return resp, err
}

func (f *FS) uploadFailed(key string, req gateway.CommitRequest, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	retry := func(d time.Duration) {
		f.queue[key] = time.Now().Add(d)
		if _, ok := f.firstQ[key]; !ok {
			f.firstQ[key] = time.Now()
		}
	}
	back := func() time.Duration {
		d := f.backoff[key]
		d = min(max(d*2, 5*time.Second), 10*time.Minute)
		f.backoff[key] = d
		return d
	}
	state := gateway.StateOf(err)
	switch {
	case gateway.IsUnreachable(err), state == gateway.StateCantReachNAS:
		retry(back())
	case state.NeedsSignIn():
		f.stopUp = true // resumes when the token changes (sign-in) or on restart
		f.state = state
		retry(time.Second)
		f.log.Printf("uploads paused: %s", state.Text())
	case gateway.IsCode(err, "not_found") && req.ItemID != "":
		// The file was deleted on the NAS (or its folder moved away) while this
		// machine had changes: upload them again as a new file in the same place.
		if it := f.items[key]; it != nil {
			parent, name := it.Parent(), it.Name
			f.unplace(it)
			delete(f.items, key)
			f.locals[key] = &Local{Key: key, Parent: parent, Name: name, Created: time.Now()}
			f.cache.Rebase(key, "")
			f.pendDirt, f.metaDirt = true, true
		}
		retry(0)
	case gateway.IsCode(err, "not_found") && req.ParentID != "":
		// The folder it was made in is gone: put it in the Loft root.
		if l := f.locals[key]; l != nil && l.Parent != f.rootID {
			f.log.Printf("the folder of %s is gone on the NAS; uploading it to the Loft root", l.Name)
			l.Parent = f.rootID
			f.pendDirt = true
			retry(0)
		} else {
			f.failed[key] = "the folder is gone"
		}
	case gateway.IsCode(err, "name_exists_on_nas"), gateway.IsCode(err, "upload_incomplete"),
		gateway.IsCode(err, "file_hash_mismatch"), gateway.IsCode(err, "chunk_hash_mismatch"),
		gateway.IsCode(err, "no_such_upload"), gateway.IsCode(err, "too_many_attempts"),
		errors.Is(err, gateway.ErrChangedDuringTransfer):
		retry(back())
	default:
		// The gateway refused this file (excluded name, no access, fixed share…). Its
		// bytes stay in the cache; status shows it; a new write tries again.
		why := err.Error()
		if a, ok := gateway.AsAPI(err); ok {
			why = a.Code
		}
		f.failed[key] = why
		f.dequeueLocked(key)
		f.log.Printf("upload of %s refused: %v", key, err)
	}
}

func (f *FS) settle(ctx context.Context, key string, gen uint64, req gateway.CommitRequest, r *gateway.CommitResponse) {
	it := r.Item
	var notes []func()
	if r.Result == gateway.ResultConflict && req.ItemID != "" {
		// Our bytes became the conflict copy; the original keeps the NAS content.
		f.mu.Lock()
		orig := key
		if o := f.items[key]; o != nil {
			orig = o.Name
		}
		f.log.Printf("conflict: %q changed on the NAS too; this computer's version is saved as %q", orig, it.Name)
		f.conflicts++
		f.dequeueLocked(key)
		notes = append(notes, f.upsertLocked(&it)...)
		f.mu.Unlock()
		f.cache.Rekey(key, it.ID)
		if !f.cache.Settle(it.ID, gen, it.ContentEtag) {
			f.enqueue(it.ID) // edited again during the upload: those edits go to the copy too
		}
		if in := f.inodeOf(key); in != nil {
			notes = append(notes, func() { in.NotifyContent(0, 0) })
		}
		f.runNotes(notes)
		f.refreshItem(ctx, key)
		return
	}

	if req.ItemID != "" { // modified in place
		f.mu.Lock()
		notes = f.upsertLocked(&it)
		if f.cache.Settle(key, gen, it.ContentEtag) {
			f.dequeueLocked(key)
		} else {
			f.enqueueLocked(key, f.opt.UploadDelay)
		}
		delete(f.failed, key)
		f.mu.Unlock()
		f.runNotes(notes)
		return
	}

	// A local file got its gateway item (created, replaced, or a conflict copy because
	// the name was taken meanwhile).
	f.mu.Lock()
	if r.Result == gateway.ResultConflict {
		f.conflicts++
		f.log.Printf("conflict: %q was taken on the NAS meanwhile; this file is saved as %q", req.Name, it.Name)
		f.listed[req.ParentID] = time.Time{}
	}
	loc := f.locals[key]
	gone := f.deleted[key]
	delete(f.deleted, key)
	delete(f.locals, key)
	f.dequeueLocked(key)
	delete(f.failed, key)
	if r.ReplacedID != nil {
		notes = append(notes, f.removeRemoteLocked(*r.ReplacedID)...)
	}
	notes = append(notes, f.upsertLocked(&it)...)
	if ino, ok := f.inos[key]; ok {
		f.inos[it.ID] = ino
	}
	if in := f.inodes[key]; in != nil {
		if live, ok := in.Operations().(*Node); ok {
			live.key = it.ID
		}
		f.inodes[it.ID] = in
		delete(f.inodes, key)
		if it.Name != req.Name {
			notes = append(notes, f.entryNote(req.ParentID, req.Name))
		}
	}
	f.pendDirt = true
	f.mu.Unlock()
	f.cache.Rekey(key, it.ID)
	clean := f.cache.Settle(it.ID, gen, it.ContentEtag)
	f.runNotes(notes)

	if gone {
		f.c.Delete(ctx, it.ID, it.ContentEtag)
		f.mu.Lock()
		n := f.removeRemoteLocked(it.ID)
		f.mu.Unlock()
		f.cache.Drop(it.ID, true)
		f.runNotes(n)
		return
	}
	// Renamed or moved while the upload ran: follow on the gateway.
	if loc != nil && (loc.Name != it.Name && r.Result != gateway.ResultConflict || loc.Parent != it.Parent()) {
		var nm, parent string
		if loc.Name != it.Name && r.Result != gateway.ResultConflict {
			nm = loc.Name
		}
		if loc.Parent != it.Parent() {
			parent = loc.Parent
		}
		if got, err := f.c.Patch(ctx, it.ID, nm, parent, ""); err == nil {
			f.mu.Lock()
			n := f.upsertLocked(got)
			f.mu.Unlock()
			f.runNotes(n)
		} else {
			f.log.Printf("follow-up rename of %s: %v", it.Name, err)
		}
	}
	if !clean {
		f.enqueue(it.ID)
	}
}

func (f *FS) inodeOf(key string) *fs.Inode {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.inodes[key]
}
