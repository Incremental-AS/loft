//go:build integration

// Integration test: the real loft binary against the real gateway code
// (gateway/scripts/dev_server.py: LocalBackend, both listeners, real TLS on direct).
//
//	LOFT_IT_PYTHON=/path/to/venv/bin/python go test -tags integration -v ./itest
//
// The venv needs gateway/requirements.lock. FUSE (/dev/fuse + fusermount3) is required;
// without it the test skips. A small TCP proxy sits between loft and the gateway's
// direct listener, so the test can take the network away and give it back.
package itest

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/pairing"
)

var loftBin string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "loft-it-bin")
	if err != nil {
		panic(err)
	}
	loftBin = filepath.Join(dir, "loft")
	cmd := exec.Command("go", "build", "-o", loftBin, "../cmd/loft")
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := cmd.CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "build: %v\n%s", err, out)
		os.Exit(1)
	}
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

func repoRoot() string {
	_, f, _, _ := runtime.Caller(0)
	return filepath.Clean(filepath.Join(filepath.Dir(f), "..", ".."))
}

func freePort(t *testing.T) int {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// proxy forwards TCP to the gateway and can drop the network on demand.
type proxy struct {
	ln     net.Listener
	target string
	mu     sync.Mutex
	down   bool
	conns  []net.Conn
}

func newProxy(t *testing.T, target string) *proxy {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	p := &proxy{ln: ln, target: target}
	go p.serve()
	t.Cleanup(func() { ln.Close(); p.cut() })
	return p
}

func (p *proxy) port() int { return p.ln.Addr().(*net.TCPAddr).Port }

func (p *proxy) serve() {
	for {
		c, err := p.ln.Accept()
		if err != nil {
			return
		}
		p.mu.Lock()
		if p.down {
			p.mu.Unlock()
			c.Close()
			continue
		}
		s, err := net.Dial("tcp", p.target)
		if err != nil {
			p.mu.Unlock()
			c.Close()
			continue
		}
		p.conns = append(p.conns, c, s)
		p.mu.Unlock()
		go func() { io.Copy(s, c); s.Close() }()
		go func() { io.Copy(c, s); c.Close() }()
	}
}

func (p *proxy) cut() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.down = true
	for _, c := range p.conns {
		c.Close()
	}
	p.conns = nil
}

func (p *proxy) resume() {
	p.mu.Lock()
	p.down = false
	p.mu.Unlock()
}

type world struct {
	t      *testing.T
	nas    string // the share on "the NAS" (dev server's LocalBackend)
	mnt    string
	env    []string
	proxy  *proxy
	link   string
	cfgDir string
}

func setup(t *testing.T) *world {
	py := os.Getenv("LOFT_IT_PYTHON")
	if py == "" {
		t.Skip("set LOFT_IT_PYTHON to a python with gateway/requirements.lock installed")
	}
	if _, err := os.Stat("/dev/fuse"); err != nil {
		t.Skip("no /dev/fuse: FUSE is not available here")
	}
	if _, err := exec.LookPath("fusermount3"); err != nil {
		t.Skip("no fusermount3")
	}
	root := t.TempDir()
	w := &world{t: t, nas: filepath.Join(root, "nas", "home"), mnt: filepath.Join(root, "mnt")}
	dport, pport := freePort(t), freePort(t)
	srv := exec.Command(py, filepath.Join(repoRoot(), "gateway", "scripts", "dev_server.py"),
		filepath.Join(root, "nas"), filepath.Join(root, "work"), fmt.Sprint(dport), fmt.Sprint(pport))
	logf, _ := os.Create(filepath.Join(root, "server.log"))
	srv.Stdout, srv.Stderr = logf, logf
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		srv.Process.Kill()
		srv.Wait()
		if t.Failed() {
			b, _ := os.ReadFile(filepath.Join(root, "server.log"))
			t.Logf("server log (tail):\n%s", tailStr(string(b), 30))
			b, _ = os.ReadFile(filepath.Join(root, "state", "loft", "loft.log"))
			t.Logf("loft log:\n%s", tailStr(string(b), 60))
		}
	})
	deadline := time.Now().Add(30 * time.Second)
	for {
		if r, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/healthz", pport)); err == nil {
			r.Body.Close()
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("dev server did not start")
		}
		time.Sleep(200 * time.Millisecond)
	}
	pin, err := os.ReadFile(filepath.Join(root, "work", "direct.pin"))
	if err != nil {
		t.Fatal(err)
	}
	w.proxy = newProxy(t, fmt.Sprintf("127.0.0.1:%d", dport))
	p := &pairing.Pairing{V: 1, Name: "Integration", LAN: fmt.Sprintf("https://127.0.0.1:%d", w.proxy.port()),
		Pins: []string{strings.TrimSpace(string(pin))}}
	w.link = p.Link()
	w.cfgDir = filepath.Join(root, "config")
	w.env = append(os.Environ(),
		"XDG_CONFIG_HOME="+filepath.Join(root, "config"),
		"XDG_CACHE_HOME="+filepath.Join(root, "cache"),
		"XDG_STATE_HOME="+filepath.Join(root, "state"),
		"LOFT_SECRET_STORE=file", // never touch the desktop's keyring from a test
		"LOFT_NO_UPDATE_CHECK=1", // and never call loft.incremental.no
	)
	t.Cleanup(func() { exec.Command("fusermount3", "-uz", w.mnt).Run() })
	return w
}

func tailStr(s string, n int) string {
	l := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(l) > n {
		l = l[len(l)-n:]
	}
	return strings.Join(l, "\n")
}

func (w *world) loft(stdin string, args ...string) (string, error) {
	cmd := exec.Command(loftBin, args...)
	cmd.Env = w.env
	cmd.Stdin = strings.NewReader(stdin)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func (w *world) must(stdin string, args ...string) string {
	w.t.Helper()
	out, err := w.loft(stdin, args...)
	if err != nil {
		w.t.Fatalf("loft %s: %v\n%s", strings.Join(args, " "), err, out)
	}
	return out
}

func (w *world) nasFile(rel string) string {
	w.t.Helper()
	b, err := os.ReadFile(filepath.Join(w.nas, rel))
	if err != nil {
		w.t.Fatalf("on the NAS: %v", err)
	}
	return string(b)
}

func (w *world) noNASFile(rel string) {
	w.t.Helper()
	if _, err := os.Stat(filepath.Join(w.nas, rel)); err == nil {
		w.t.Fatalf("%s should not be on the NAS", rel)
	}
}

func (w *world) put(rel, content string) {
	w.t.Helper()
	p := filepath.Join(w.mnt, rel)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		w.t.Fatalf("write %s: %v", rel, err)
	}
}

func (w *world) get(rel string) string {
	w.t.Helper()
	b, err := os.ReadFile(filepath.Join(w.mnt, rel))
	if err != nil {
		w.t.Fatalf("read %s: %v", rel, err)
	}
	return string(b)
}

func (w *world) idle() {
	w.t.Helper()
	w.must("", "status", "--wait-idle", "90s")
}

func TestPairLoginMountReadWrite(t *testing.T) {
	w := setup(t)
	// What "the NAS" already holds.
	os.MkdirAll(filepath.Join(w.nas, "docs"), 0o755)
	os.WriteFile(filepath.Join(w.nas, "hello.txt"), []byte("hello from the NAS\n"), 0o644)
	big := make([]byte, 6<<20+12345)
	rand.Read(big)
	os.WriteFile(filepath.Join(w.nas, "docs", "big.bin"), big, 0o644)
	os.WriteFile(filepath.Join(w.nas, "docs", "never-read.bin"), big[:3<<20], 0o644)
	os.WriteFile(filepath.Join(w.nas, "docs", "pin-me.bin"), big[:2<<20], 0o644)

	w.run(t, "wrong pin: password never sent", func(t *testing.T) {
		bad := &pairing.Pairing{V: 1, Name: "Fake", LAN: fmt.Sprintf("https://127.0.0.1:%d", w.proxy.port()),
			Pins: []string{"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}}
		env := w.env
		w.env = append(append([]string{}, env...), "XDG_CONFIG_HOME="+filepath.Join(t.TempDir(), "c"))
		defer func() { w.env = env }()
		out := w.must("", "pair", bad.Link())
		if !strings.Contains(out, "does NOT match") {
			t.Fatalf("no pin warning: %s", out)
		}
		out, err := w.loft("dev-password\n", "login", "--user", "loft-dev", "--password-stdin", "--share", "home")
		if err == nil || !strings.Contains(out, "password was not sent") {
			t.Fatalf("login to a server with the wrong key: %v %s", err, out)
		}
	})

	out := w.must("", "pair", w.link)
	if !strings.Contains(out, "its key matches") {
		t.Fatalf("pair: %s", out)
	}
	out = w.must("dev-password\n", "login", "--user", "loft-dev", "--password-stdin", "--share", "home",
		"--device-name", "it-linux")
	if !strings.Contains(out, "Signed in as loft-dev") || strings.Contains(out, "nst_") {
		t.Fatalf("login: %s", out)
	}
	tokFile := filepath.Join(w.cfgDir, "loft", "token.json")
	if st, err := os.Stat(tokFile); err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("token file: %v %v", st, err)
	}
	w.must("", "mount", w.mnt)

	w.run(t, "list and read", func(t *testing.T) {
		ents, err := os.ReadDir(w.mnt)
		if err != nil {
			t.Fatal(err)
		}
		var names []string
		for _, e := range ents {
			names = append(names, e.Name())
		}
		if strings.Join(names, ",") != "docs,hello.txt" {
			t.Fatalf("listing: %v", names)
		}
		if w.get("hello.txt") != "hello from the NAS\n" {
			t.Fatal("hello.txt content")
		}
		got, err := os.ReadFile(filepath.Join(w.mnt, "docs", "big.bin"))
		if err != nil || !bytes.Equal(got, big) {
			t.Fatalf("big.bin differs (%v, %d bytes)", err, len(got))
		}
		// A read in the middle of a file fetches just that part (Range).
		f, _ := os.Open(filepath.Join(w.mnt, "docs", "never-read.bin"))
		buf := make([]byte, 100)
		if _, err := f.ReadAt(buf, 2<<20); err != nil || !bytes.Equal(buf, big[2<<20:2<<20+100]) {
			t.Fatalf("ranged read: %v", err)
		}
		f.Close()
		out := w.must("", "status")
		if !strings.Contains(out, "Home network") || !strings.Contains(out, "Up to date") {
			t.Fatalf("status: %s", out)
		}
	})

	w.run(t, "write, mkdir, rename, delete", func(t *testing.T) {
		w.put("new.txt", "made on Linux\n")
		if err := os.Mkdir(filepath.Join(w.mnt, "folder"), 0o755); err != nil {
			t.Fatal(err)
		}
		w.put("folder/a.txt", "a\n")
		f, _ := os.OpenFile(filepath.Join(w.mnt, "hello.txt"), os.O_APPEND|os.O_WRONLY, 0)
		f.WriteString("appended on Linux\n")
		f.Sync() // fsync uploads right away
		f.Close()
		if w.nasFile("hello.txt") != "hello from the NAS\nappended on Linux\n" {
			t.Fatalf("after fsync: %q", w.nasFile("hello.txt"))
		}
		w.idle()
		if w.nasFile("new.txt") != "made on Linux\n" || w.nasFile("folder/a.txt") != "a\n" {
			t.Fatal("uploads missing")
		}
		must(t, os.Rename(filepath.Join(w.mnt, "new.txt"), filepath.Join(w.mnt, "folder", "moved.txt")))
		must(t, os.Rename(filepath.Join(w.mnt, "folder"), filepath.Join(w.mnt, "renamed")))
		w.noNASFile("new.txt")
		w.noNASFile("folder")
		if w.nasFile("renamed/moved.txt") != "made on Linux\n" {
			t.Fatal("move")
		}
		must(t, os.Remove(filepath.Join(w.mnt, "renamed", "a.txt")))
		w.noNASFile("renamed/a.txt")
		if err := os.Remove(filepath.Join(w.mnt, "renamed")); err == nil {
			t.Fatal("removed a folder that still has a file")
		}
		must(t, os.Remove(filepath.Join(w.mnt, "renamed", "moved.txt")))
		must(t, os.Remove(filepath.Join(w.mnt, "renamed")))
		w.noNASFile("renamed")
		// Overwrite in place (truncate + write).
		w.put("hello.txt", "replaced\n")
		w.idle()
		if w.nasFile("hello.txt") != "replaced\n" {
			t.Fatal("overwrite")
		}
	})

	w.run(t, "editor safe-save is one new version", func(t *testing.T) {
		w.put("doc.txt", "v1\n")
		w.idle()
		w.put(".doc.txt.swp.tmp", "v2\n") // a temp name never leaves this computer…
		must(t, os.Rename(filepath.Join(w.mnt, ".doc.txt.swp.tmp"), filepath.Join(w.mnt, "doc.txt")))
		w.idle()
		if w.nasFile("doc.txt") != "v2\n" {
			t.Fatalf("doc.txt = %q", w.nasFile("doc.txt"))
		}
		w.noNASFile(".doc.txt.swp.tmp")
		vs, _ := os.ReadDir(filepath.Join(w.nas, ".loft", "versions"))
		if len(vs) == 0 {
			t.Fatal("the old content did not go to versions")
		}
		trash, _ := os.ReadDir(filepath.Join(w.nas, ".loft", "trash"))
		for _, e := range trash {
			if strings.Contains(e.Name(), "doc.txt") {
				t.Fatal("safe-save put the old file in the trash")
			}
		}
	})

	w.run(t, "excluded names stay local", func(t *testing.T) {
		w.put("~$report.docx", "lock")
		w.idle()
		if w.get("~$report.docx") != "lock" {
			t.Fatal("local file unreadable")
		}
		w.noNASFile("~$report.docx")
	})

	w.run(t, "large file in chunks, pin", func(t *testing.T) {
		data := make([]byte, 40<<20+777) // two 32 MiB chunks
		rand.Read(data)
		if err := os.WriteFile(filepath.Join(w.mnt, "large.bin"), data, 0o644); err != nil {
			t.Fatal(err)
		}
		w.idle()
		if got := w.nasFile("large.bin"); got != string(data) {
			t.Fatalf("large.bin on the NAS differs (%d bytes)", len(got))
		}
		w.must("", "pin", filepath.Join(w.mnt, "docs", "pin-me.bin"))
		state := make([]byte, 64)
		n, err := syscall.Getxattr(filepath.Join(w.mnt, "docs", "pin-me.bin"), "user.loft.state", state)
		if err != nil || string(state[:n]) != "cached" {
			t.Fatalf("pinned file state %q %v", state[:n], err)
		}
		w.must("", "unpin", filepath.Join(w.mnt, "docs", "pin-me.bin"))
	})

	w.run(t, "conflict copy", func(t *testing.T) {
		w.put("shared.txt", "base\n")
		w.idle()
		_ = w.get("shared.txt")
		time.Sleep(1100 * time.Millisecond) // a visible mtime change on the NAS side
		os.WriteFile(filepath.Join(w.nas, "shared.txt"), []byte("edited over SMB\n"), 0o644)
		w.put("shared.txt", "edited on Linux\n")
		w.idle()
		if w.nasFile("shared.txt") != "edited over SMB\n" {
			t.Fatalf("the NAS edit was overwritten: %q", w.nasFile("shared.txt"))
		}
		ents, _ := os.ReadDir(w.nas)
		var copyName string
		for _, e := range ents {
			if strings.HasPrefix(e.Name(), "shared (conflict, it-linux, ") && strings.HasSuffix(e.Name(), ").txt") {
				copyName = e.Name()
			}
		}
		if copyName == "" || w.nasFile(copyName) != "edited on Linux\n" {
			t.Fatalf("no conflict copy with our bytes (%q)", copyName)
		}
		time.Sleep(1500 * time.Millisecond) // entry/attr timeouts
		if w.get("shared.txt") != "edited over SMB\n" || w.get(copyName) != "edited on Linux\n" {
			t.Fatalf("mount shows %q / %q", w.get("shared.txt"), w.get(copyName))
		}
	})

	w.run(t, "changes on the NAS show up", func(t *testing.T) {
		os.WriteFile(filepath.Join(w.nas, "from-elsewhere.txt"), []byte("hi\n"), 0o644)
		// The gateway notices SMB-side changes when a folder is listed or on its rescan;
		// a listing after the entry timeout must show it.
		deadline := time.Now().Add(50 * time.Second) // folder listings are re-read after 30 s
		for {
			if b, err := os.ReadFile(filepath.Join(w.mnt, "from-elsewhere.txt")); err == nil && string(b) == "hi\n" {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("a file made on the NAS never appeared")
			}
			time.Sleep(500 * time.Millisecond)
		}
	})

	w.run(t, "offline: cached files readable, writes queued", func(t *testing.T) {
		_ = w.get("doc.txt") // cached
		w.proxy.cut()
		if w.get("doc.txt") != "v2\n" {
			t.Fatal("cached file not readable offline")
		}
		f, err := os.OpenFile(filepath.Join(w.mnt, "doc.txt"), os.O_APPEND|os.O_WRONLY, 0)
		if err != nil {
			t.Fatalf("open cached file for writing offline: %v", err)
		}
		f.WriteString("v3 offline\n")
		if err := f.Sync(); err != nil {
			t.Fatalf("fsync offline must succeed (queued): %v", err)
		}
		f.Close()
		w.put("offline-new.txt", "made offline\n")
		if _, err := os.ReadFile(filepath.Join(w.mnt, "docs", "never-read.bin")); err == nil {
			t.Fatal("an uncached file was readable offline")
		}
		time.Sleep(2500 * time.Millisecond)
		var st struct {
			Mount struct {
				Pending int    `json:"pending"`
				State   string `json:"state"`
			} `json:"mount"`
		}
		json.Unmarshal([]byte(w.must("", "status", "--json")), &st)
		if st.Mount.Pending < 2 {
			t.Fatalf("pending = %d", st.Mount.Pending)
		}
		w.noNASFile("offline-new.txt")
		w.proxy.resume()
		w.must("", "status", "--wait-idle", "120s")
		if w.nasFile("doc.txt") != "v2\nv3 offline\n" || w.nasFile("offline-new.txt") != "made offline\n" {
			t.Fatalf("after reconnect: %q %q", w.nasFile("doc.txt"), w.nasFile("offline-new.txt"))
		}
	})

	w.run(t, "unmount and mount again", func(t *testing.T) {
		w.must("", "unmount")
		if ents, _ := os.ReadDir(w.mnt); len(ents) != 0 {
			t.Fatal("still showing files after unmount")
		}
		w.must("", "mount", w.mnt)
		if w.get("offline-new.txt") != "made offline\n" {
			t.Fatal("after remount")
		}
		w.must("", "unmount")
	})

	w.run(t, "logout", func(t *testing.T) {
		w.must("", "logout")
		if _, err := os.Stat(tokFile); !os.IsNotExist(err) {
			t.Fatal("token file left after logout")
		}
		out, err := w.loft("", "mount", w.mnt)
		if err == nil || !strings.Contains(out, "not signed in") {
			t.Fatalf("mount after logout: %v %s", err, out)
		}
	})
}

// run is t.Run with the world's helpers failing the subtest, not its parent.
func (w *world) run(t *testing.T, name string, fn func(t *testing.T)) {
	t.Run(name, func(st *testing.T) {
		old := w.t
		w.t = st
		defer func() { w.t = old }()
		fn(st)
	})
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
