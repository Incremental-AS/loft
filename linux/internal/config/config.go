// Package config holds where Loft keeps things (XDG base directories) and the small
// settings file: the pairing, the account (no secrets) and the user's settings.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/Incremental-AS/loft/linux/internal/gateway"
	"github.com/Incremental-AS/loft/linux/internal/pairing"
)

// Paths are the directories Loft uses.
type Paths struct {
	Config string // ~/.config/loft          settings, token file fallback
	Cache  string // ~/.cache/loft           file contents (size-capped), metadata
	State  string // ~/.local/state/loft     status, upload queue, change anchor, log
}

func xdg(env, fallback string) string {
	if v := os.Getenv(env); v != "" && filepath.IsAbs(v) {
		return v
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, fallback)
}

// DefaultPaths follows the XDG base directory spec.
func DefaultPaths() Paths {
	return Paths{
		Config: filepath.Join(xdg("XDG_CONFIG_HOME", ".config"), "loft"),
		Cache:  filepath.Join(xdg("XDG_CACHE_HOME", ".cache"), "loft"),
		State:  filepath.Join(xdg("XDG_STATE_HOME", ".local/state"), "loft"),
	}
}

func (p Paths) Ensure() error {
	for _, d := range []string{p.Config, p.Cache, p.State} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return err
		}
	}
	return nil
}

// Account is who is signed in. The token itself lives in the secret store.
type Account struct {
	NASUser    string            `json:"nas_user"`
	DeviceID   string            `json:"device_id"`
	DeviceName string            `json:"device_name"`
	TokenStore string            `json:"token_store"` // secrets.SecretService | secrets.File
	Root       *gateway.RootInfo `json:"root,omitempty"`
}

// Settings the user can change with `loft config`.
type Settings struct {
	Mountpoint       string `json:"mountpoint,omitempty"`        // default ~/Loft
	CacheLimitBytes  int64  `json:"cache_limit_bytes,omitempty"` // default 5 GiB
	EvictAfterMin    int    `json:"evict_after_minutes,omitempty"`
	UpdateCheck      *bool  `json:"update_check,omitempty"` // default on
	Tray             *bool  `json:"tray,omitempty"`         // default on (where the desktop has one)
	ReleasesURL      string `json:"releases_url,omitempty"` // tests/mirrors; default loft.incremental.no
	MaxUploadWorkers int    `json:"max_upload_workers,omitempty"`
}

// Config is ~/.config/loft/config.json.
type Config struct {
	Pairing  *pairing.Pairing `json:"pairing,omitempty"`
	Account  *Account         `json:"account,omitempty"`
	Settings Settings         `json:"settings"`
	path     string
}

const (
	DefaultCacheLimit = 5 << 30
	DefaultEvictAfter = 10 // minutes, docs/design.md §E.1
	ReleasesURL       = "https://loft.incremental.no/releases.json"
)

// Load reads the config (an empty one if there is none yet).
func Load(p Paths) (*Config, error) {
	c := &Config{path: filepath.Join(p.Config, "config.json")}
	b, err := os.ReadFile(c.path)
	if errors.Is(err, os.ErrNotExist) {
		return c, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(b, c); err != nil {
		return nil, fmt.Errorf("%s: %w", c.path, err)
	}
	return c, nil
}

// Save writes atomically, 0600.
func (c *Config) Save() error {
	if err := os.MkdirAll(filepath.Dir(c.path), 0o700); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(c, "", "  ")
	tmp := c.path + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, c.path)
}

func (c *Config) Path() string { return c.path }

// GatewayKey names the paired gateway in the secret store.
func (c *Config) GatewayKey() string {
	if c.Pairing == nil {
		return ""
	}
	return c.Pairing.LAN
}

func (c *Config) Mountpoint() string {
	if c.Settings.Mountpoint != "" {
		return c.Settings.Mountpoint
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Loft")
}

func (c *Config) CacheLimit() int64 {
	if c.Settings.CacheLimitBytes > 0 {
		return c.Settings.CacheLimitBytes
	}
	return DefaultCacheLimit
}

func (c *Config) EvictAfterMinutes() int {
	if c.Settings.EvictAfterMin > 0 {
		return c.Settings.EvictAfterMin
	}
	return DefaultEvictAfter
}

func (c *Config) UpdateCheck() bool {
	if os.Getenv("LOFT_NO_UPDATE_CHECK") != "" {
		return false
	}
	return c.Settings.UpdateCheck == nil || *c.Settings.UpdateCheck
}

func (c *Config) TrayEnabled() bool { return c.Settings.Tray == nil || *c.Settings.Tray }

func (c *Config) ReleasesURL() string {
	if c.Settings.ReleasesURL != "" {
		return c.Settings.ReleasesURL
	}
	return ReleasesURL
}

// Endpoints turns the pairing into gateway endpoints.
func (c *Config) Endpoints() gateway.Endpoints {
	p := c.Pairing
	return gateway.Endpoints{LAN: p.LAN, Public: p.Pub, PublicMode: p.Mode, Pins: append([]string(nil), p.Pins...)}
}

// Set changes one setting by its CLI name.
func (c *Config) Set(key, value string) error {
	on := func() (*bool, error) {
		switch strings.ToLower(value) {
		case "on", "true", "yes", "1":
			t := true
			return &t, nil
		case "off", "false", "no", "0":
			f := false
			return &f, nil
		}
		return nil, fmt.Errorf("%s: say on or off", key)
	}
	var err error
	switch key {
	case "update-check":
		c.Settings.UpdateCheck, err = on()
	case "tray":
		c.Settings.Tray, err = on()
	case "mountpoint":
		if !filepath.IsAbs(value) {
			return errors.New("mountpoint: give an absolute path")
		}
		c.Settings.Mountpoint = value
	case "cache-limit":
		var n int64
		n, err = ParseSize(value)
		if err == nil && n < 64<<20 {
			err = errors.New("cache-limit: at least 64M")
		}
		c.Settings.CacheLimitBytes = n
	case "evict-after":
		var n int
		n, err = strconv.Atoi(strings.TrimSuffix(value, "m"))
		if err == nil && n < 1 {
			err = errors.New("evict-after: minutes, at least 1")
		}
		c.Settings.EvictAfterMin = n
	default:
		return fmt.Errorf("unknown setting %q (update-check, tray, mountpoint, cache-limit, evict-after)", key)
	}
	return err
}

// ParseSize reads 5G, 500M, 1024K or a byte count.
func ParseSize(s string) (int64, error) {
	s = strings.TrimSpace(strings.ToUpper(strings.TrimSuffix(strings.ToUpper(s), "B")))
	mult := int64(1)
	if n := len(s); n > 0 {
		switch s[n-1] {
		case 'K':
			mult, s = 1<<10, s[:n-1]
		case 'M':
			mult, s = 1<<20, s[:n-1]
		case 'G':
			mult, s = 1<<30, s[:n-1]
		case 'T':
			mult, s = 1<<40, s[:n-1]
		}
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil || f < 0 {
		return 0, fmt.Errorf("%q is not a size like 5G", s)
	}
	return int64(f * float64(mult)), nil
}

// HumanSize prints 1.2 GB style sizes.
func HumanSize(n int64) string {
	const u = 1024
	if n < u {
		return fmt.Sprintf("%d B", n)
	}
	f, i := float64(n), 0
	units := []string{"KB", "MB", "GB", "TB"}
	for f /= u; f >= u && i < len(units)-1; i++ {
		f /= u
	}
	return fmt.Sprintf("%.1f %s", f, units[i])
}
