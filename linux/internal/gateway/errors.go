package gateway

import (
	"errors"
	"fmt"
)

// APIError is a gateway verdict: {"error": "<code>", ...extra} with the HTTP status.
type APIError struct {
	Status     int    `json:"-"`
	Code       string `json:"error"`
	Received   *int64 `json:"received,omitempty"`    // 409 wrong_offset / upload_incomplete
	ExistingID string `json:"existing_id,omitempty"` // 409 name_exists
	Current    *Item  `json:"current,omitempty"`     // 412 *_etag_mismatch
	Detail     string `json:"detail,omitempty"`
	RetryAfter int    `json:"retry_after,omitempty"`
}

func (e *APIError) Error() string { return fmt.Sprintf("gateway %d %s", e.Status, e.Code) }

// NASUnreachable: the gateway answered, the NAS behind it did not.
func (e *APIError) NASUnreachable() bool { return e.Code == "nas_unavailable" || e.Code == "nas_error" }

// UnreachableError: no answer from the gateway (network, TLS, pin mismatch, timeout, or
// a proxy in front of a dead tunnel answering 5xx with HTML).
type UnreachableError struct {
	Reason      string
	PinMismatch bool
}

func (e *UnreachableError) Error() string { return "can't reach the gateway: " + e.Reason }

// BadResponseError: the gateway answered with something we don't understand.
type BadResponseError struct {
	Status int
	Msg    string
}

func (e *BadResponseError) Error() string { return fmt.Sprintf("bad response %d: %s", e.Status, e.Msg) }

var (
	// ErrNotSignedIn: no device token stored.
	ErrNotSignedIn = errors.New("not signed in")
	// ErrChangedDuringTransfer: the file changed on the NAS under a download.
	ErrChangedDuringTransfer = errors.New("the file changed on the NAS during the transfer")
	// ErrSignInNeedsHome: no pinned route for a password (only a tunnel answered).
	ErrSignInNeedsHome = errors.New("sign-in needs the gateway's home address (or a pinned outside address); it didn't answer")
)

// AsAPI returns the gateway verdict inside err, if any.
func AsAPI(err error) (*APIError, bool) {
	var a *APIError
	if errors.As(err, &a) {
		return a, true
	}
	return nil, false
}

// IsCode reports a gateway verdict with this code.
func IsCode(err error, code string) bool {
	a, ok := AsAPI(err)
	return ok && a.Code == code
}

func IsUnreachable(err error) bool {
	var u *UnreachableError
	return errors.As(err, &u)
}

// SyncState is what the status line shows after a round of the sync loop (LoftCore
// LoopState + SyncError, same error-state words).
type SyncState string

const (
	StateOK                SyncState = "ok"
	StateCantReachHome     SyncState = "cant_reach_home"      // no answer from the gateway
	StateCantReachNAS      SyncState = "cant_reach_nas"       // gateway up, NAS down
	StateNASPasswordChange SyncState = "nas_password_changed" // 401 nas_credentials_invalid
	StateSignInExpired     SyncState = "sign_in_expired"      // token_expired / invalid_token / …
	StateDeviceRemoved     SyncState = "device_removed"       // 401 device_revoked
	StateNoAccess          SyncState = "no_access"            // access_denied / root_missing
	StateStopped           SyncState = "stopped"              // anything else (code in the log)
)

// StateOf maps a sync-loop failure (changes long-poll, /v1/me, refresh) to a state.
// Per-item errors never stop sync and must not be fed here.
func StateOf(err error) SyncState {
	if err == nil {
		return StateOK
	}
	if errors.Is(err, ErrNotSignedIn) {
		return StateSignInExpired
	}
	if IsUnreachable(err) {
		return StateCantReachHome
	}
	if a, ok := AsAPI(err); ok {
		switch a.Code {
		case "nas_unavailable", "nas_error":
			return StateCantReachNAS
		case "nas_credentials_invalid":
			return StateNASPasswordChange
		case "token_expired", "invalid_token", "token_superseded", "missing_token":
			return StateSignInExpired
		case "device_revoked":
			return StateDeviceRemoved
		case "access_denied", "root_missing":
			return StateNoAccess
		case "anchor_expired":
			return StateOK
		}
		return StateStopped
	}
	if errors.Is(err, ErrChangedDuringTransfer) {
		return StateOK
	}
	return StateStopped
}

// NeedsSignIn: only a fresh `loft login` fixes it.
func (s SyncState) NeedsSignIn() bool {
	return s == StateNASPasswordChange || s == StateSignInExpired || s == StateDeviceRemoved
}

// Text is the plain-English status line.
func (s SyncState) Text() string {
	switch s {
	case StateOK:
		return "Up to date"
	case StateCantReachHome:
		return "Can't reach home. Retrying…"
	case StateCantReachNAS:
		return "Can't reach the NAS. Retrying…"
	case StateNASPasswordChange:
		return "Your NAS password changed. Run: loft login"
	case StateSignInExpired:
		return "Signed out. Run: loft login"
	case StateDeviceRemoved:
		return "This computer was removed from Loft. Run: loft login"
	case StateNoAccess:
		return "No access: the Loft folder is unavailable"
	default:
		return "Sync stopped (see the log)"
	}
}
