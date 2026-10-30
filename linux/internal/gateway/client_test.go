package gateway

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type memTokens struct {
	mu sync.Mutex
	t  *Token
}

func (m *memTokens) Load() (*Token, error) { m.mu.Lock(); defer m.mu.Unlock(); return m.t, nil }
func (m *memTokens) Save(t *Token) error   { m.mu.Lock(); m.t = t; m.mu.Unlock(); return nil }

func tok(s string) *memTokens {
	return &memTokens{t: &Token{Token: s, DeviceID: "d1", IssuedAt: time.Now()}}
}

func health(route string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(Health{OK: true, Service: "loft", Route: route})
	}
}

func pinOf(s *httptest.Server) string { return SPKIPin(s.Certificate().RawSubjectPublicKeyInfo) }

func fast(c *Client) *Client {
	c.RetryDelays = []time.Duration{time.Millisecond, time.Millisecond}
	return c
}

func TestPinnedTLS(t *testing.T) {
	srv := httptest.NewTLSServer(health("direct"))
	defer srv.Close()
	c := New(Endpoints{LAN: srv.URL, Pins: []string{pinOf(srv)}}, tok("t"))
	if h, err := c.Health(context.Background(), RouteDirect); err != nil || h.Route != "direct" {
		t.Fatalf("pinned server refused: %v", err)
	}
	other := SPKIPin([]byte("some other key"))
	c = New(Endpoints{LAN: srv.URL, Pins: []string{other}}, tok("t"))
	_, err := c.Health(context.Background(), RouteDirect)
	var u *UnreachableError
	if !errors.As(err, &u) || !u.PinMismatch {
		t.Fatalf("wrong key accepted or misreported: %v", err)
	}
}

func TestRouteSelection(t *testing.T) {
	lan := httptest.NewTLSServer(health("direct"))
	pub := httptest.NewServer(health("public"))
	defer pub.Close()
	c := New(Endpoints{LAN: lan.URL, Public: pub.URL, PublicMode: "tunnel", Pins: []string{pinOf(lan)}}, tok("t"))
	if r := c.Reprobe(context.Background()); r != RouteDirect {
		t.Fatalf("home answers, want direct, got %s", r)
	}
	lan.Close()
	if r := c.Reprobe(context.Background()); r != RoutePublic {
		t.Fatalf("home gone, want public, got %s", r)
	}
	if c.CanPasswordSignIn() {
		t.Fatal("tunnel route must not allow password sign-in")
	}
}

func TestFailoverToPublicMidSession(t *testing.T) {
	lan := httptest.NewTLSServer(health("direct"))
	var pubHits atomic.Int32
	pub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		pubHits.Add(1)
		json.NewEncoder(w).Encode(Me{NASUser: "u", Route: "public"})
	}))
	defer pub.Close()
	c := New(Endpoints{LAN: lan.URL, Public: pub.URL, PublicMode: "tunnel", Pins: []string{pinOf(lan)}}, tok("t"))
	c.Reprobe(context.Background())
	lan.Close()
	me, err := c.Me(context.Background())
	if err != nil || me.Route != "public" || c.Route() != RoutePublic || pubHits.Load() != 1 {
		t.Fatalf("no failover: %v %+v %s", err, me, c.Route())
	}
}

func TestLoginNeverGoesToTunnel(t *testing.T) {
	lan := httptest.NewTLSServer(health("direct"))
	lanURL, pin := lan.URL, pinOf(lan)
	lan.Close()
	var hits atomic.Int32
	pub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1) }))
	defer pub.Close()
	c := New(Endpoints{LAN: lanURL, Public: pub.URL, PublicMode: "tunnel", Pins: []string{pin}}, &memTokens{})
	_, err := c.Login(context.Background(), "u", "secret", "pc")
	if !errors.Is(err, ErrSignInNeedsHome) {
		t.Fatalf("want ErrSignInNeedsHome, got %v", err)
	}
	if hits.Load() != 0 {
		t.Fatal("the password went to the tunnel")
	}
}

func TestLoginPinMismatchSendsNothing(t *testing.T) {
	var hits atomic.Int32
	lan := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1) }))
	defer lan.Close()
	c := New(Endpoints{LAN: lan.URL, Pins: []string{SPKIPin([]byte("x"))}}, &memTokens{})
	_, err := c.Login(context.Background(), "u", "secret", "pc")
	var u *UnreachableError
	if !errors.As(err, &u) || !u.PinMismatch {
		t.Fatalf("want pin mismatch, got %v", err)
	}
	if hits.Load() != 0 {
		t.Fatal("a request reached a server with the wrong key")
	}
}

func TestLoginDirectModeOutside(t *testing.T) {
	lan := httptest.NewTLSServer(health("direct"))
	lanURL := lan.URL
	lan.Close()
	var body map[string]string
	pub := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&body)
		json.NewEncoder(w).Encode(LoginResult{DeviceID: "d9", Token: "nst_d9.x", NASUser: "u", ExpiresAt: 2e9})
	}))
	defer pub.Close()
	c := New(Endpoints{LAN: lanURL, Public: pub.URL, PublicMode: "direct", Pins: []string{pinOf(pub)}}, &memTokens{})
	r, err := c.Login(context.Background(), "u", "secret", "pc")
	if err != nil || r.DeviceID != "d9" || body["password"] != "secret" || body["device_name"] != "pc" {
		t.Fatalf("%v %+v %v", err, r, body)
	}
}

func TestErrorMapping(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/me", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(502)
		io.WriteString(w, "<html>Bad gateway</html>")
	})
	mux.HandleFunc("/v1/items/x", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(401)
		io.WriteString(w, `{"error":"device_revoked"}`)
	})
	mux.HandleFunc("/v1/items/y", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(503)
		io.WriteString(w, `{"error":"nas_unavailable","detail":"down"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	c := New(Endpoints{LAN: "https://127.0.0.1:1", Public: srv.URL, PublicMode: "tunnel"}, tok("t"))
	c.Reprobe(context.Background())
	_, err := c.Me(context.Background())
	if !IsUnreachable(err) || StateOf(err) != StateCantReachHome {
		t.Fatalf("HTML 502 must read as no answer: %v", err)
	}
	_, err = c.Item(context.Background(), "x")
	if a, ok := AsAPI(err); !ok || a.Status != 401 || StateOf(err) != StateDeviceRemoved || !StateOf(err).NeedsSignIn() {
		t.Fatalf("%v", err)
	}
	_, err = c.Item(context.Background(), "y")
	if StateOf(err) != StateCantReachNAS {
		t.Fatalf("%v", err)
	}
	c.Tokens = &memTokens{}
	if _, err := c.Me(context.Background()); !errors.Is(err, ErrNotSignedIn) {
		t.Fatalf("no token: %v", err)
	}
}

func TestRedirectsAreNotFollowed(t *testing.T) {
	var hits atomic.Int32
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1) }))
	defer elsewhere.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL+"/v1/me", http.StatusTemporaryRedirect)
	}))
	defer srv.Close()
	c := New(Endpoints{LAN: "https://127.0.0.1:1", Public: srv.URL, PublicMode: "tunnel"}, tok("t"))
	c.Reprobe(context.Background())
	if _, err := c.Me(context.Background()); err == nil || hits.Load() != 0 {
		t.Fatalf("redirect followed: %v, %d hits elsewhere", err, hits.Load())
	}
}

func TestTokenReReadAfterRotationElsewhere(t *testing.T) {
	store := tok("old")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer new" {
			store.Save(&Token{Token: "new"}) // another process rotated meanwhile
			w.WriteHeader(401)
			io.WriteString(w, `{"error":"invalid_token"}`)
			return
		}
		json.NewEncoder(w).Encode(Me{NASUser: "u"})
	}))
	defer srv.Close()
	c := New(Endpoints{LAN: "https://127.0.0.1:1", Public: srv.URL, PublicMode: "tunnel"}, store)
	c.Reprobe(context.Background())
	if me, err := c.Me(context.Background()); err != nil || me.NASUser != "u" {
		t.Fatalf("%v", err)
	}
}

func TestRotateToken(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/auth/refresh" || r.Header.Get("Authorization") != "Bearer old" {
			w.WriteHeader(400)
			return
		}
		json.NewEncoder(w).Encode(LoginResult{DeviceID: "d1", Token: "new", ExpiresAt: 2e9})
	}))
	defer srv.Close()
	store := tok("old")
	store.t.IssuedAt = time.Now().Add(-8 * 24 * time.Hour)
	if !store.t.DueForRotation() {
		t.Fatal("8 days old should rotate")
	}
	c := New(Endpoints{LAN: "https://127.0.0.1:1", Public: srv.URL, PublicMode: "tunnel"}, store)
	c.Reprobe(context.Background())
	if _, err := c.RotateToken(context.Background()); err != nil || store.t.Token != "new" || store.t.DueForRotation() {
		t.Fatalf("%v %+v", err, store.t)
	}
}

// fakeUploads is the gateway's upload protocol in miniature.
type fakeUploads struct {
	mu       sync.Mutex
	data     []byte
	sha      string
	size     int64
	refused  bool
	commits  []CommitRequest
	chunkMax int64
}

func (f *fakeUploads) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/uploads", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			Size   int64  `json:"size"`
			SHA256 string `json:"sha256"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		f.mu.Lock()
		f.size, f.sha, f.data = b.Size, b.SHA256, nil
		f.mu.Unlock()
		json.NewEncoder(w).Encode(UploadState{UploadID: "u1", Size: b.Size, ChunkMax: &f.chunkMax})
	})
	mux.HandleFunc("PUT /v1/uploads/u1", func(w http.ResponseWriter, r *http.Request) {
		off, _ := strconv.ParseInt(r.URL.Query().Get("offset"), 10, 64)
		body, _ := io.ReadAll(r.Body)
		h := sha256.Sum256(body)
		f.mu.Lock()
		defer f.mu.Unlock()
		if !f.refused && off == 0 && len(body) > 0 {
			// pretend the first chunk already arrived before a dropped connection
			f.refused = true
			f.data = append(f.data, body...)
			w.WriteHeader(409)
			fmt.Fprintf(w, `{"error":"wrong_offset","received":%d}`, len(f.data))
			return
		}
		if off != int64(len(f.data)) {
			w.WriteHeader(409)
			fmt.Fprintf(w, `{"error":"wrong_offset","received":%d}`, len(f.data))
			return
		}
		if r.Header.Get("X-Chunk-Sha256") != hex.EncodeToString(h[:]) {
			w.WriteHeader(422)
			io.WriteString(w, `{"error":"chunk_hash_mismatch"}`)
			return
		}
		f.data = append(f.data, body...)
		json.NewEncoder(w).Encode(UploadState{UploadID: "u1", Received: int64(len(f.data)), Size: f.size})
	})
	mux.HandleFunc("POST /v1/uploads/u1/commit", func(w http.ResponseWriter, r *http.Request) {
		var req CommitRequest
		json.NewDecoder(r.Body).Decode(&req)
		f.mu.Lock()
		defer f.mu.Unlock()
		h := sha256.Sum256(f.data)
		if hex.EncodeToString(h[:]) != f.sha {
			w.WriteHeader(422)
			io.WriteString(w, `{"error":"file_hash_mismatch"}`)
			return
		}
		f.commits = append(f.commits, req)
		res := CommitResponse{Result: ResultCreated, Item: Item{ID: "new", Name: req.Name, Type: "file",
			Size: int64(len(f.data)), ContentEtag: "c1", MetaEtag: "m1"}}
		json.NewEncoder(w).Encode(res)
	})
	return mux
}

type memResume struct{ m map[string]string }

func (r *memResume) ResumableUpload(sha string, size int64) string { return r.m[fmt.Sprint(sha, size)] }
func (r *memResume) SetResumableUpload(id, sha string, size int64) {
	if id == "" {
		delete(r.m, fmt.Sprint(sha, size))
	} else {
		r.m[fmt.Sprint(sha, size)] = id
	}
}

func TestChunkedUploadResumes(t *testing.T) {
	fu := &fakeUploads{chunkMax: 4}
	srv := httptest.NewServer(fu.handler())
	defer srv.Close()
	c := fast(New(Endpoints{LAN: "https://127.0.0.1:1", Public: srv.URL, PublicMode: "tunnel"}, tok("t")))
	c.Reprobe(context.Background())
	p := filepath.Join(t.TempDir(), "f")
	content := []byte("0123456789abcdefghij-xyz")
	os.WriteFile(p, content, 0o600)
	rs := &memResume{m: map[string]string{}}
	r, err := c.UploadAndCommit(context.Background(), p, CommitRequest{ParentID: "root", Name: "f.txt"}, rs, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(fu.data, content) || r.Result != ResultCreated || fu.commits[0].Name != "f.txt" {
		t.Fatalf("got %q %+v", fu.data, r)
	}
	if len(rs.m) != 0 {
		t.Fatal("resume entry not forgotten after commit")
	}
}

// contentServer serves one item with Range, like api.py.
func contentServer(t *testing.T, get func() ([]byte, string)) *httptest.Server {
	re := regexp.MustCompile(`^bytes=(\d*)-(\d*)$`)
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, etag := get()
		size := int64(len(data))
		w.Header().Set("ETag", `"`+etag+`"`)
		rg := r.Header.Get("Range")
		if rg == "" {
			w.Write(data)
			return
		}
		m := re.FindStringSubmatch(rg)
		start, _ := strconv.ParseInt(m[1], 10, 64)
		end := size - 1
		if m[2] != "" {
			e, _ := strconv.ParseInt(m[2], 10, 64)
			end = min(e, size-1)
		}
		if start >= size {
			w.WriteHeader(416)
			io.WriteString(w, `{"error":"range_not_satisfiable"}`)
			return
		}
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, size))
		w.WriteHeader(206)
		w.Write(data[start : end+1])
	}))
}

func TestRangeAndDownload(t *testing.T) {
	data := []byte("The quick brown fox jumps over the lazy dog")
	srv := contentServer(t, func() ([]byte, string) { return data, "c3" })
	defer srv.Close()
	c := New(Endpoints{LAN: "https://127.0.0.1:1", Public: srv.URL, PublicMode: "tunnel"}, tok("t"))
	c.Reprobe(context.Background())
	r, err := c.ReadRange(context.Background(), "i", 4, 5)
	if err != nil || string(r.Data) != "quick" || r.Total != int64(len(data)) || r.Etag != "c3" || r.Start != 4 {
		t.Fatalf("%v %+v", err, r)
	}
	c.DownloadSlice = 7
	f, _ := os.Create(filepath.Join(t.TempDir(), "d"))
	defer f.Close()
	tag, err := c.Download(context.Background(), "i", f, nil)
	got, _ := os.ReadFile(f.Name())
	if err != nil || tag != "c3" || !bytes.Equal(got, data) {
		t.Fatalf("%v %q %q", err, tag, got)
	}
}

func TestEmptyFileAndChangeMidDownload(t *testing.T) {
	empty := contentServer(t, func() ([]byte, string) { return nil, "c1" })
	defer empty.Close()
	c := New(Endpoints{LAN: "https://127.0.0.1:1", Public: empty.URL, PublicMode: "tunnel"}, tok("t"))
	c.Reprobe(context.Background())
	if r, err := c.ReadRange(context.Background(), "i", 0, 10); err != nil || r.Total != 0 || len(r.Data) != 0 {
		t.Fatalf("empty file: %v %+v", err, r)
	}
	var n atomic.Int32
	changing := contentServer(t, func() ([]byte, string) {
		if n.Add(1) <= 2 {
			return []byte("aaaaaaaaaa"), "c1"
		}
		return []byte("bbbbbbbbbbbb"), "c2"
	})
	defer changing.Close()
	c = New(Endpoints{LAN: "https://127.0.0.1:1", Public: changing.URL, PublicMode: "tunnel"}, tok("t"))
	c.Reprobe(context.Background())
	c.DownloadSlice = 4
	f, _ := os.Create(filepath.Join(t.TempDir(), "d"))
	defer f.Close()
	tag, err := c.Download(context.Background(), "i", f, nil)
	got, _ := os.ReadFile(f.Name())
	if err != nil || tag != "c2" || string(got) != "bbbbbbbbbbbb" {
		t.Fatalf("restart after change: %v %q %q", err, tag, got)
	}
}

func TestRefreshPins(t *testing.T) {
	lan := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			health("direct")(w, r)
			return
		}
		json.NewEncoder(w).Encode(Identity{DirectSPKISHA256: []string{"P1", "P2"}})
	}))
	defer lan.Close()
	live := pinOf(lan)
	var got []string
	c := New(Endpoints{LAN: lan.URL, Pins: []string{live}}, tok("t"))
	c.OnPinsChanged = func(p []string) { got = p }
	// The gateway's answer replaces the set (rotation): our live pin is not in it, so
	// the next connection would fail. That is the gateway's call, as on the Mac.
	if _, err := c.RefreshPins(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || c.pinned(live) {
		t.Fatalf("pins not adopted: %v", got)
	}
}
