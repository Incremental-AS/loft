package cache

import (
	"bytes"
	"testing"
	"time"
)

func TestPartialFillAndComplete(t *testing.T) {
	c, err := Open(t.TempDir(), 1<<30)
	if err != nil {
		t.Fatal(err)
	}
	size := int64(2*BlockSize + 100)
	if err := c.Prepare("a", "c1", size); err != nil {
		t.Fatal(err)
	}
	if m := c.Missing("a", BlockSize+5, 10); len(m) != 1 || m[0] != (Span{BlockSize, BlockSize}) {
		t.Fatalf("missing: %v", m)
	}
	blk := bytes.Repeat([]byte{7}, BlockSize)
	if err := c.Fill("a", "c1", BlockSize, blk); err != nil {
		t.Fatal(err)
	}
	if m := c.Missing("a", 0, size); len(m) != 2 || m[0].Off != 0 || m[1] != (Span{2 * BlockSize, 100}) {
		t.Fatalf("missing after fill: %v", m)
	}
	if err := c.Fill("a", "c2", 0, blk); err != ErrStale {
		t.Fatal("fill for another version accepted")
	}
	c.Fill("a", "c1", 0, blk)
	c.Fill("a", "c1", 2*BlockSize, bytes.Repeat([]byte{9}, 100))
	e, _ := c.Get("a")
	if !e.Complete || c.Used() != size {
		t.Fatalf("%v used=%d", e, c.Used())
	}
	buf := make([]byte, 4)
	if n, _ := c.ReadAt("a", buf, size-2); n != 2 || buf[0] != 9 {
		t.Fatalf("read at end: %d %v", n, buf)
	}
	// A new version empties a clean entry.
	c.Prepare("a", "c2", 10)
	if e, _ := c.Get("a"); e.Complete || e.Size != 10 {
		t.Fatalf("not reset: %v", e)
	}
}

func TestDirtyIsNeverEvictedAndSettles(t *testing.T) {
	dir := t.TempDir()
	c, _ := Open(dir, 10) // tiny cap: everything clean must go
	c.Prepare("clean", "c1", 5)
	c.Fill("clean", "c1", 0, []byte("hello"))
	c.Prepare("pinned", "c1", 5)
	c.Fill("pinned", "c1", 0, []byte("hello"))
	c.SetPinned("pinned", true)
	c.Prepare("open", "c1", 5)
	c.Fill("open", "c1", 0, []byte("hello"))
	c.Opened("open", false)
	c.Prepare("edit", "c1", 5)
	c.Fill("edit", "c1", 0, []byte("hello"))
	c.WriteAt("edit", []byte("HELLO WORLD"), 0)
	e, _ := c.Get("edit")
	if !e.Dirty || e.BaseEtag != "c1" || e.Size != 11 {
		t.Fatalf("%v", e)
	}
	c.Prepare("edit", "c9", 3) // the feed says the NAS moved on: local edits stay
	if e, _ := c.Get("edit"); !e.Dirty || e.Size != 11 {
		t.Fatalf("dirty entry reset: %v", e)
	}
	got := c.Evict(0)
	if len(got) != 1 || got[0] != "clean" {
		t.Fatalf("evicted %v", got)
	}
	gen := c.Gen("edit")
	c.WriteAt("edit", []byte("!"), 11) // written again during the upload
	if c.Settle("edit", gen, "c2") {
		t.Fatal("settled although written again")
	}
	if e, _ := c.Get("edit"); !e.Dirty || e.BaseEtag != "c2" {
		t.Fatalf("%v", e)
	}
	if !c.Settle("edit", c.Gen("edit"), "c3") {
		t.Fatal("not settled")
	}
	c.Close()
	// The index survives a restart.
	c2, _ := Open(dir, 1<<30)
	if e, ok := c2.Get("edit"); !ok || e.Dirty || e.Etag != "c3" || e.Size != 12 {
		t.Fatalf("after reopen: %v %v", e, ok)
	}
}

func TestEvictAfterUploadIdle(t *testing.T) {
	c, _ := Open(t.TempDir(), 1<<30)
	c.NewLocal("l")
	c.WriteAt("l", []byte("data"), 0)
	c.Settle("l", c.Gen("l"), "c1")
	c.Prepare("r", "c1", 4)
	c.Fill("r", "c1", 0, []byte("read"))
	if got := c.Evict(time.Hour); len(got) != 0 {
		t.Fatalf("evicted too early: %v", got)
	}
	c.mu.Lock()
	c.entries["l"].LastAccess = time.Now().Add(-2 * time.Hour)
	c.entries["r"].LastAccess = time.Now().Add(-2 * time.Hour)
	c.mu.Unlock()
	// Only the uploaded file leaves on the timer; the read one stays until space is short.
	if got := c.Evict(time.Hour); len(got) != 1 || got[0] != "l" {
		t.Fatalf("evicted %v", got)
	}
}

func TestRekeyKeepsContent(t *testing.T) {
	c, _ := Open(t.TempDir(), 1<<30)
	c.NewLocal("local-1")
	c.WriteAt("local-1", []byte("abc"), 0)
	if err := c.Rekey("local-1", "item-9"); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 3)
	if n, _ := c.ReadAt("item-9", buf, 0); n != 3 || string(buf) != "abc" {
		t.Fatalf("%q", buf)
	}
	if _, ok := c.Get("local-1"); ok {
		t.Fatal("old key still there")
	}
}
