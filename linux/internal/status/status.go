// Package status is the small file the running mount writes and `loft status` reads.
package status

import (
	"encoding/json"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// Status of a running mount.
type Status struct {
	PID        int       `json:"pid"`
	Mountpoint string    `json:"mountpoint"`
	Route      string    `json:"route"`       // direct | public
	State      string    `json:"state"`       // gateway.SyncState
	StateText  string    `json:"state_text"`  // plain English
	Pending    int       `json:"pending"`     // files waiting for upload
	Uploading  int       `json:"uploading"`   // in flight now
	Failed     []string  `json:"failed"`      // uploads the gateway refused (names)
	LastSync   time.Time `json:"last_sync"`   // last good round of the change feed
	CacheUsed  int64     `json:"cache_used"`  // bytes
	CacheLimit int64     `json:"cache_limit"` // bytes
	Update     string    `json:"update,omitempty"`
	UpdateURL  string    `json:"update_url,omitempty"`
	Tray       string    `json:"tray,omitempty"` // "shown" | why not
	UpdatedAt  time.Time `json:"updated_at"`
	MountedAt  time.Time `json:"mounted_at"`
	TokenStore string    `json:"token_store"`
	Unmounting bool      `json:"unmounting,omitempty"`
}

func File(stateDir string) string { return filepath.Join(stateDir, "status.json") }

// Write saves atomically.
func Write(stateDir string, s *Status) error {
	s.UpdatedAt = time.Now()
	b, _ := json.MarshalIndent(s, "", "  ")
	tmp := File(stateDir) + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, File(stateDir))
}

// Read returns the status of a live mount, or nil when none runs.
func Read(stateDir string) *Status {
	b, err := os.ReadFile(File(stateDir))
	if err != nil {
		return nil
	}
	var s Status
	if json.Unmarshal(b, &s) != nil || !Alive(s.PID) {
		return nil
	}
	return &s
}

// Alive reports whether a process exists.
func Alive(pid int) bool {
	if pid <= 0 {
		return false
	}
	return syscall.Kill(pid, 0) == nil
}

// Remove deletes the status file (clean exit).
func Remove(stateDir string) { os.Remove(File(stateDir)) }
