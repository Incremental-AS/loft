package secrets

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/gateway"
	"github.com/zalando/go-keyring"
)

func TestFileStoreIs0600AndPerGateway(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "loft")
	a, err := Open(File, "https://a:8443", dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := Open(File, "https://b:8443", dir, nil)
	if err := a.Save(&gateway.Token{Token: "nst_a.secret", DeviceID: "a", IssuedAt: time.Now()}); err != nil {
		t.Fatal(err)
	}
	b.Save(&gateway.Token{Token: "nst_b.secret", DeviceID: "b"})
	st, err := os.Stat(filepath.Join(dir, "token.json"))
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v %v", st.Mode(), err)
	}
	if d, _ := os.Stat(dir); d.Mode().Perm() != 0o700 {
		t.Fatalf("dir mode %v", d.Mode())
	}
	if got, _ := a.Load(); got == nil || got.Token != "nst_a.secret" {
		t.Fatalf("%+v", got)
	}
	a.Clear()
	if got, _ := a.Load(); got != nil {
		t.Fatal("not cleared")
	}
	if got, _ := b.Load(); got == nil || got.DeviceID != "b" {
		t.Fatal("clearing one gateway removed another")
	}
	b.Clear()
	if _, err := os.Stat(filepath.Join(dir, "token.json")); !os.IsNotExist(err) {
		t.Fatal("empty token file left behind")
	}
}

func TestSecretServiceStore(t *testing.T) {
	keyring.MockInit() // go-keyring's in-memory provider; a real keyring is checked by hand
	s, err := Open(SecretService, "https://a:8443", t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := s.Load(); err != nil || got != nil {
		t.Fatalf("%v %v", got, err)
	}
	s.Save(&gateway.Token{Token: "nst_x.y", DeviceID: "x"})
	if got, _ := s.Load(); got == nil || got.Token != "nst_x.y" {
		t.Fatalf("%+v", got)
	}
	if s.Clear() != nil {
		t.Fatal("clear")
	}
	if got, _ := s.Load(); got != nil {
		t.Fatal("not cleared")
	}
}

// TestRealSecretService talks to a real Secret Service (run it inside a session with
// gnome-keyring or KeePassXC): LOFT_TEST_REAL_KEYRING=1 go test -run Real ./internal/secrets
func TestRealSecretService(t *testing.T) {
	if os.Getenv("LOFT_TEST_REAL_KEYRING") == "" {
		t.Skip("set LOFT_TEST_REAL_KEYRING=1 in a session with a Secret Service")
	}
	if err := Probe(); err != nil {
		t.Fatalf("probe: %v", err)
	}
	s, err := Open("", "https://it.example:8443", t.TempDir(), func(m string) { t.Fatalf("fell back: %s", m) })
	if err != nil || s.Backend() != SecretService {
		t.Fatalf("%v %v", s, err)
	}
	if err := s.Save(&gateway.Token{Token: "nst_it.real", DeviceID: "it"}); err != nil {
		t.Fatal(err)
	}
	again, _ := Open(SecretService, "https://it.example:8443", t.TempDir(), nil)
	if got, err := again.Load(); err != nil || got == nil || got.Token != "nst_it.real" {
		t.Fatalf("%+v %v", got, err)
	}
	if err := again.Clear(); err != nil {
		t.Fatal(err)
	}
	if got, _ := s.Load(); got != nil {
		t.Fatal("still there after Clear")
	}
}

func TestFallbackWarns(t *testing.T) {
	// No Secret Service answering (go-keyring's mock in its failing mode).
	keyring.MockInitWithError(errors.New("org.freedesktop.secrets was not provided"))
	t.Setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
	t.Setenv("LOFT_SECRET_STORE", "")
	var warned string
	s, err := Open("", "k", t.TempDir(), func(m string) { warned = m })
	if err != nil || s.Backend() != File || !strings.Contains(warned, "token.json") {
		t.Fatalf("%v %v %q", s, err, warned)
	}
	keyring.MockInit()
	s, _ = Open("", "k", t.TempDir(), func(m string) { t.Fatalf("warned although a keyring answers: %s", m) })
	if s.Backend() != SecretService {
		t.Fatal("keyring available but not used")
	}
}
