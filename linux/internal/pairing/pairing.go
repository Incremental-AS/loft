// Package pairing reads the pairing payload a Loft gateway prints (docs/pairing.md).
//
// The payload carries the gateway's addresses and the SPKI pins of its direct key, so
// the very first connection is already authenticated. There is deliberately no way to
// pair with an address alone: Manual needs a pin too.
package pairing

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
)

// LinkBase is the web form of a pairing link. The payload sits in the fragment, so it
// never reaches loft.incremental.no.
const LinkBase = "https://loft.incremental.no/pair#"

// Pairing is one gateway this device trusts.
type Pairing struct {
	V    int      `json:"v"`
	Name string   `json:"name"`
	LAN  string   `json:"lan"`
	Pins []string `json:"pins"`
	Pub  string   `json:"pub,omitempty"`
	Mode string   `json:"mode,omitempty"` // "tunnel" | "direct" (with Pub)
}

// Public route modes.
const (
	ModeTunnel = "tunnel" // TLS ends at a proxy: web PKI, device tokens only, never sign-in
	ModeDirect = "direct" // port forward to the direct listener: pinned, sign-in allowed
)

// Parse accepts a pairing link (https://loft.incremental.no/pair#…, loft://pair#…,
// loft://pair?…) or the bare base64url payload, and validates it.
func Parse(s string) (*Pairing, error) {
	raw := strings.TrimSpace(s)
	if raw == "" {
		return nil, errors.New("empty pairing code")
	}
	if i := strings.Index(raw, "#"); i >= 0 {
		raw = raw[i+1:]
	} else if strings.HasPrefix(strings.ToLower(raw), "loft:") {
		// loft://pair?<payload> (docs/pairing.md) or loft://pair?p=<payload>
		u, err := url.Parse(raw)
		if err != nil {
			return nil, fmt.Errorf("not a pairing link: %w", err)
		}
		raw = u.RawQuery
		if v := u.Query().Get("p"); v != "" {
			raw = v
		}
	} else if strings.Contains(raw, "://") {
		return nil, errors.New("this link holds no pairing code (it has no #…)")
	}
	raw = strings.TrimRight(raw, "=")
	data, err := base64.RawURLEncoding.DecodeString(raw)
	if err != nil {
		return nil, errors.New("the pairing code is damaged (not base64url)")
	}
	var p Pairing
	dec := json.NewDecoder(strings.NewReader(string(data)))
	if err := dec.Decode(&p); err != nil {
		return nil, errors.New("the pairing code is damaged (not JSON)")
	}
	if err := p.Validate(); err != nil {
		return nil, err
	}
	return &p, nil
}

// Manual builds a pairing from an address and a pin typed by hand ("Set up manually").
// The address is the gateway's direct listener; the pin is required.
func Manual(addr, pin string, extraPins ...string) (*Pairing, error) {
	addr = strings.TrimRight(strings.TrimSpace(addr), "/")
	u, err := url.Parse(addr)
	if err != nil || u.Host == "" {
		return nil, fmt.Errorf("%q is not an address like https://host:8443", addr)
	}
	p := &Pairing{V: 1, Name: u.Hostname(), LAN: addr}
	for _, x := range append([]string{pin}, extraPins...) {
		if x = strings.TrimSpace(x); x != "" {
			p.Pins = append(p.Pins, strings.TrimPrefix(x, "sha256//"))
		}
	}
	if err := p.Validate(); err != nil {
		return nil, err
	}
	return p, nil
}

// Validate applies the rules of docs/pairing.md: known version, https everywhere, at
// least one well-formed pin, and a mode with every public URL.
func (p *Pairing) Validate() error {
	if p.V != 1 {
		return fmt.Errorf("pairing code version %d is not supported by this Loft (update the app)", p.V)
	}
	if strings.TrimSpace(p.Name) == "" {
		return errors.New("the pairing code has no gateway name")
	}
	if err := httpsURL(p.LAN); err != nil {
		return fmt.Errorf("home address: %w", err)
	}
	if len(p.Pins) == 0 {
		return errors.New("the pairing code has no key pins, so the gateway can't be trusted")
	}
	for _, pin := range p.Pins {
		b, err := base64.StdEncoding.DecodeString(pin)
		if err != nil || len(b) != 32 {
			return fmt.Errorf("%q is not a key pin (base64 of a SHA-256)", pin)
		}
	}
	if p.Pub != "" {
		if err := httpsURL(p.Pub); err != nil {
			return fmt.Errorf("outside address: %w", err)
		}
		if p.Mode != ModeTunnel && p.Mode != ModeDirect {
			return fmt.Errorf("outside address mode %q is not tunnel or direct", p.Mode)
		}
	} else if p.Mode != "" {
		return errors.New("the pairing code has a mode but no outside address")
	}
	p.LAN = strings.TrimRight(p.LAN, "/")
	p.Pub = strings.TrimRight(p.Pub, "/")
	return nil
}

func httpsURL(s string) error {
	u, err := url.Parse(s)
	if err != nil || u.Host == "" {
		return fmt.Errorf("%q is not a URL", s)
	}
	if u.Scheme != "https" {
		return fmt.Errorf("%q is not https", s)
	}
	return nil
}

// Encode is the inverse of Parse's payload step (base64url, no padding, compact JSON).
func (p *Pairing) Encode() string {
	b, _ := json.Marshal(p)
	return base64.RawURLEncoding.EncodeToString(b)
}

// Link is the shareable form.
func (p *Pairing) Link() string { return LinkBase + p.Encode() }

// SignInRoutes lists the base URLs a password may be sent to: always the pinned home
// address, plus the outside address only when it is a pinned direct port forward.
func (p *Pairing) SignInRoutes() []string {
	r := []string{p.LAN}
	if p.Pub != "" && p.Mode == ModeDirect {
		r = append(r, p.Pub)
	}
	return r
}
