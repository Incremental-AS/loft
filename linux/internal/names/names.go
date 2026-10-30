// Package names holds the file-name rules shared with the gateway and the other clients.
package names

import (
	"strings"

	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

var (
	prefixes = []string{"~$", "._", ".~lock.", "Word Work File ", "~WRL", "~WRD", ".loft"}
	exact    = map[string]bool{".DS_Store": true, "Icon\r": true, "Thumbs.db": true, "desktop.ini": true}
	folder   = cases.Fold()
)

// Excluded reports names that never leave this machine (docs/design.md §D.3): Office lock and
// scratch files, Finder/Explorer metadata and safe-save temps. Same list as the
// reference client (LoftCore Exclusions.swift); the gateway refuses them as well.
func Excluded(name string) bool {
	if exact[name] {
		return true
	}
	for _, p := range prefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return strings.HasSuffix(name, ".tmp") || strings.Contains(name, ".sb-")
}

// NFC is the form the gateway stores names in.
func NFC(name string) string { return norm.NFC.String(name) }

// Key is the gateway's name_key: NFC + casefold. SMB is case-insensitive, so two names
// with the same key can't live in one folder on the NAS.
func Key(name string) string { return folder.String(norm.NFC.String(name)) }

// Valid mirrors the gateway's basic name check (no separators, not . or ..).
func Valid(name string) bool {
	return name != "" && name != "." && name != ".." && !strings.ContainsAny(name, "/\\\x00") &&
		len(name) <= 255
}
