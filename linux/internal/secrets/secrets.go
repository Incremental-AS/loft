// Package secrets stores the device token: in the desktop's Secret Service (GNOME
// Keyring, KWallet, KeePassXC…) over D-Bus, or, where there is none, in a 0600 file
// under ~/.config/loft that says what it is. The NAS password is never stored.
package secrets

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/gateway"
	"github.com/zalando/go-keyring"
)

// Backend names, as stored in the config so every process uses the same one.
const (
	SecretService = "secret-service"
	File          = "file"
)

const service = "no.incremental.loft"

// Store is a gateway.TokenStore with a name. Load caches in memory; Reload re-reads
// (another process may have rotated the token).
type Store interface {
	gateway.TokenStore
	Clear() error
	Backend() string
	Where() string
}

// account is the key the token is stored under: one per paired gateway.
func account(gatewayKey string) string { return "device-token:" + gatewayKey }

// Open returns the store named by backend; "" picks Secret Service when it answers, else
// the file (warn tells the user once).
func Open(backend, gatewayKey, configDir string, warn func(string)) (Store, error) {
	if env := os.Getenv("LOFT_SECRET_STORE"); env != "" && backend == "" {
		backend = env
	}
	file := &fileStore{path: filepath.Join(configDir, "token.json"), key: gatewayKey}
	switch backend {
	case File:
		return file, nil
	case SecretService:
		return &keyringStore{acct: account(gatewayKey)}, nil
	case "":
		if err := Probe(); err == nil {
			return &keyringStore{acct: account(gatewayKey)}, nil
		} else if warn != nil {
			warn(fmt.Sprintf("No Secret Service on this desktop (%v).\n"+
				"The device token is stored in %s instead, readable only by you.", short(err), file.path))
		}
		return file, nil
	default:
		return nil, fmt.Errorf("unknown secret store %q (secret-service or file)", backend)
	}
}

func short(err error) string {
	s := err.Error()
	if len(s) > 120 {
		s = s[:120] + "…"
	}
	return s
}

// Probe checks that a Secret Service answers on the session bus.
func Probe() error {
	if os.Getenv("DBUS_SESSION_BUS_ADDRESS") == "" {
		if _, err := os.Stat(fmt.Sprintf("/run/user/%d/bus", os.Getuid())); err != nil {
			return errors.New("no D-Bus session bus")
		}
	}
	done := make(chan error, 1)
	go func() {
		_, err := keyring.Get(service, "probe")
		if errors.Is(err, keyring.ErrNotFound) {
			err = nil
		}
		done <- err
	}()
	select {
	case err := <-done:
		return err
	case <-time.After(5 * time.Second):
		return errors.New("Secret Service did not answer")
	}
}

type keyringStore struct {
	acct string
	mu   sync.Mutex
	tok  *gateway.Token
}

func (k *keyringStore) Backend() string { return SecretService }
func (k *keyringStore) Where() string   { return "the desktop's Secret Service (keyring)" }

func (k *keyringStore) Load() (*gateway.Token, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	s, err := keyring.Get(service, k.acct)
	if errors.Is(err, keyring.ErrNotFound) {
		return nil, nil
	}
	if err != nil {
		if k.tok != nil { // keyring briefly unavailable: keep using what we read
			return k.tok, nil
		}
		return nil, err
	}
	var t gateway.Token
	if err := json.Unmarshal([]byte(s), &t); err != nil {
		return nil, err
	}
	k.tok = &t
	return &t, nil
}

func (k *keyringStore) Save(t *gateway.Token) error {
	b, _ := json.Marshal(t)
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := keyring.Set(service, k.acct, string(b)); err != nil {
		return err
	}
	k.tok = t
	return nil
}

func (k *keyringStore) Clear() error {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.tok = nil
	err := keyring.Delete(service, k.acct)
	if errors.Is(err, keyring.ErrNotFound) {
		return nil
	}
	return err
}

// fileStore is the fallback: one JSON file, mode 0600, in a 0700 directory.
type fileStore struct {
	path string
	key  string
	mu   sync.Mutex
}

type fileDoc struct {
	Note   string                    `json:"note"`
	Tokens map[string]*gateway.Token `json:"tokens"`
}

func (f *fileStore) Backend() string { return File }
func (f *fileStore) Where() string   { return f.path + " (no Secret Service; file readable only by you)" }

func (f *fileStore) read() (*fileDoc, error) {
	d := &fileDoc{Tokens: map[string]*gateway.Token{}}
	b, err := os.ReadFile(f.path)
	if errors.Is(err, os.ErrNotExist) {
		return d, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(b, d); err != nil {
		return nil, err
	}
	if d.Tokens == nil {
		d.Tokens = map[string]*gateway.Token{}
	}
	return d, nil
}

func (f *fileStore) Load() (*gateway.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, err := f.read()
	if err != nil {
		return nil, err
	}
	return d.Tokens[f.key], nil
}

func (f *fileStore) write(d *fileDoc) error {
	if err := os.MkdirAll(filepath.Dir(f.path), 0o700); err != nil {
		return err
	}
	d.Note = "Loft device token. No Secret Service was available, so it is kept here. " +
		"Anyone who can read this file can use your Loft until you run `loft logout`."
	b, _ := json.MarshalIndent(d, "", "  ")
	tmp := f.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, f.path)
}

func (f *fileStore) Save(t *gateway.Token) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, err := f.read()
	if err != nil {
		d = &fileDoc{Tokens: map[string]*gateway.Token{}}
	}
	d.Tokens[f.key] = t
	return f.write(d)
}

func (f *fileStore) Clear() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, err := f.read()
	if err != nil {
		return os.Remove(f.path)
	}
	delete(d.Tokens, f.key)
	if len(d.Tokens) == 0 {
		err := os.Remove(f.path)
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	return f.write(d)
}
