package update

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestNewer(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"0.2.1", "0.2.0", true}, {"0.10.0", "0.9.9", true}, {"0.2.0", "0.2.0", false},
		{"v1.0", "0.9", true}, {"0.2.0-rc1", "0.2.0", false}, {"0.2.0", "0.2.0-rc1", true},
		{"0.1.9", "0.2.0", false},
	} {
		if got := Newer(c.a, c.b); got != c.want {
			t.Errorf("Newer(%s,%s)=%v", c.a, c.b, got)
		}
	}
}

func TestAtMostDaily(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		io.WriteString(w, `{"linux":{"version":"0.3.0","url":"https://loft.incremental.no/download/"}}`)
	}))
	defer srv.Close()
	dir := t.TempDir()
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	rel, err := Check(context.Background(), srv.URL, dir, "0.2.0", now)
	if err != nil || rel == nil || rel.Version != "0.3.0" {
		t.Fatalf("%v %+v", err, rel)
	}
	// Within the day: answered from the saved state, no request.
	rel, _ = Check(context.Background(), srv.URL, dir, "0.2.0", now.Add(23*time.Hour))
	if hits.Load() != 1 || rel == nil {
		t.Fatalf("hits=%d rel=%v", hits.Load(), rel)
	}
	Check(context.Background(), srv.URL, dir, "0.2.0", now.Add(25*time.Hour))
	if hits.Load() != 2 {
		t.Fatalf("hits=%d", hits.Load())
	}
	// Up to date: nothing to show.
	if rel, _ := Check(context.Background(), srv.URL, t.TempDir(), "0.3.0", now); rel != nil {
		t.Fatal("same version reported as an update")
	}
}

func TestFailureStillCountsAsTheDailyCheck(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.WriteHeader(500)
	}))
	defer srv.Close()
	dir := t.TempDir()
	now := time.Now()
	if _, err := Check(context.Background(), srv.URL, dir, "0.2.0", now); err == nil {
		t.Fatal("500 accepted")
	}
	Check(context.Background(), srv.URL, dir, "0.2.0", now.Add(time.Hour))
	if hits.Load() != 1 {
		t.Fatalf("retried within the day: %d", hits.Load())
	}
}

func TestPlatformsShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"platforms":{"linux":{"version":"1.0.0","url":"http://insecure.example"}}}`)
	}))
	defer srv.Close()
	rel, err := Check(context.Background(), srv.URL, t.TempDir(), "0.2.0", time.Now())
	if err != nil || rel.Version != "1.0.0" || rel.URL != "" {
		t.Fatalf("%v %+v (a non-https URL must be dropped)", err, rel)
	}
}
