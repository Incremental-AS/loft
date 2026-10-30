package vfs

import (
	"context"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/gateway"
)

// LongPollWait is the change feed's long-poll (the gateway caps it at 25 s; Cloudflare
// at 100 s).
var LongPollWait = 25

// watch long-polls the change feed (docs/design.md §D.2) and applies it to the tree, so
// listings stay current without re-reading folders.
func (f *FS) watch(ctx context.Context) {
	backoff := 2 * time.Second
	for ctx.Err() == nil {
		f.mu.Lock()
		anchor := f.anchor
		f.mu.Unlock()
		if anchor == "" {
			a, err := f.c.LatestAnchor(ctx)
			if err != nil {
				f.loopFailed(ctx, err, &backoff)
				continue
			}
			f.mu.Lock()
			f.anchor = a
			f.pendDirt = true
			// We missed whatever happened before: re-read folders when next opened.
			for id := range f.listed {
				f.listed[id] = time.Time{}
			}
			f.mu.Unlock()
			anchor = a
		}
		page, err := f.c.Changes(ctx, anchor, LongPollWait, 500)
		if err != nil {
			if gateway.IsCode(err, "anchor_expired") {
				f.mu.Lock()
				f.anchor = ""
				f.mu.Unlock()
				continue
			}
			f.loopFailed(ctx, err, &backoff)
			continue
		}
		f.apply(page)
		backoff = 2 * time.Second
	}
}

func (f *FS) loopFailed(ctx context.Context, err error, backoff *time.Duration) {
	if ctx.Err() != nil {
		return
	}
	st := gateway.StateOf(err)
	f.mu.Lock()
	f.state = st
	f.feedLive = time.Time{}
	f.mu.Unlock()
	if st != gateway.StateCantReachHome {
		f.log.Printf("change feed: %v", err)
	}
	sleepCtx(ctx, *backoff)
	*backoff = min(*backoff*2, 60*time.Second)
}

// apply takes one page of the feed.
func (f *FS) apply(p *gateway.ChangesPage) {
	f.mu.Lock()
	var notes []func()
	for i := range p.Updated {
		it := &p.Updated[i]
		if _, mine := f.locals[it.ID]; mine {
			continue
		}
		notes = append(notes, f.upsertLocked(it)...)
	}
	for _, id := range p.Deleted {
		notes = append(notes, f.removeRemoteLocked(id)...)
	}
	f.anchor = p.Anchor
	f.pendDirt = true
	f.state = gateway.StateOK
	f.feedLive = time.Now()
	f.lastSync = time.Now()
	if f.stopUp {
		f.stopUp = false // the token works again (signed in anew)
		select {
		case f.wake <- struct{}{}:
		default:
		}
	}
	f.mu.Unlock()
	f.runNotes(notes)
}

// State returns the sync state (for the tray and status).
func (f *FS) State() gateway.SyncState {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.state
}
