// Package update checks loft.incremental.no/releases.json for a newer Linux release, at
// most once a day. It is the only request Loft makes to Incremental (docs/design.md §E.4), and it
// can be switched off (`loft config set update-check off` or LOFT_NO_UPDATE_CHECK=1).
package update

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Interval between checks.
const Interval = 24 * time.Hour

// Release is what releases.json says about Linux. Accepted shapes (the website stream
// owns the file; see linux/README.md):
//
//	{"linux": {"version": "0.2.1", "url": "https://…", "notes": "…"}}
//	{"platforms": {"linux": {…}}}
type Release struct {
	Version string `json:"version"`
	URL     string `json:"url"`
	Notes   string `json:"notes,omitempty"`
}

type doc struct {
	Linux     *Release            `json:"linux"`
	Platforms map[string]*Release `json:"platforms"`
}

// State is kept in ~/.local/state/loft/update.json.
type State struct {
	CheckedAt time.Time `json:"checked_at"`
	Latest    *Release  `json:"latest,omitempty"`
	Error     string    `json:"error,omitempty"`
}

func statePath(dir string) string { return filepath.Join(dir, "update.json") }

func LoadState(dir string) State {
	var s State
	if b, err := os.ReadFile(statePath(dir)); err == nil {
		json.Unmarshal(b, &s)
	}
	return s
}

func saveState(dir string, s State) {
	b, _ := json.MarshalIndent(s, "", "  ")
	os.WriteFile(statePath(dir), b, 0o600)
}

// Due reports whether a check is allowed now (at most daily).
func Due(dir string, now time.Time) bool {
	s := LoadState(dir)
	return s.CheckedAt.IsZero() || now.Sub(s.CheckedAt) >= Interval || s.CheckedAt.After(now)
}

// Check fetches releases.json if a check is due and returns the newer release, if any.
// The time of the attempt is recorded even when it fails, so a broken network never
// turns into more than one request a day.
func Check(ctx context.Context, url, stateDir, current string, now time.Time) (*Release, error) {
	if !Due(stateDir, now) {
		s := LoadState(stateDir)
		if s.Latest != nil && Newer(s.Latest.Version, current) {
			return s.Latest, nil
		}
		return nil, nil
	}
	st := State{CheckedAt: now}
	rel, err := fetch(ctx, url)
	if err != nil {
		st.Error = err.Error()
		saveState(stateDir, st)
		return nil, err
	}
	st.Latest = rel
	saveState(stateDir, st)
	if rel != nil && Newer(rel.Version, current) {
		return rel, nil
	}
	return nil, nil
}

func fetch(ctx context.Context, url string) (*Release, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	// No identifiers beyond the product: the request says nothing about the user.
	req.Header.Set("User-Agent", "Loft (Linux)")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("releases.json: HTTP %d", resp.StatusCode)
	}
	var d doc
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&d); err != nil {
		return nil, fmt.Errorf("releases.json: %w", err)
	}
	r := d.Linux
	if r == nil && d.Platforms != nil {
		r = d.Platforms["linux"]
	}
	if r == nil || r.Version == "" {
		return nil, errors.New("releases.json has no linux entry")
	}
	if r.URL != "" && !strings.HasPrefix(r.URL, "https://") {
		r.URL = ""
	}
	return r, nil
}

// Newer compares dotted versions numerically ("0.10.0" > "0.9.3"); a pre-release suffix
// (-rc1) counts as older than the plain version.
func Newer(a, b string) bool {
	pa, sa := split(a)
	pb, sb := split(b)
	for i := 0; i < max(len(pa), len(pb)); i++ {
		var x, y int
		if i < len(pa) {
			x = pa[i]
		}
		if i < len(pb) {
			y = pb[i]
		}
		if x != y {
			return x > y
		}
	}
	return sa == "" && sb != ""
}

func split(v string) ([]int, string) {
	v = strings.TrimPrefix(strings.TrimSpace(v), "v")
	suffix := ""
	if i := strings.IndexAny(v, "-+"); i >= 0 {
		v, suffix = v[:i], v[i:]
	}
	var out []int
	for _, p := range strings.Split(v, ".") {
		n, _ := strconv.Atoi(p)
		out = append(out, n)
	}
	return out, suffix
}
