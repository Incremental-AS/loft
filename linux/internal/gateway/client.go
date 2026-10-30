// Package gateway is the HTTP client for the Loft gateway API. It mirrors the reference
// client (LoftCore GatewayClient.swift + RouteSelector.swift): SPKI pinning on the direct
// route, web PKI with device tokens only on a tunnel route, home first then outside,
// one retry on the other route, token re-read on a rotation race, chunked resumable
// uploads and sliced Range downloads.
package gateway

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Version is the client version sent in the User-Agent.
var Version = "0.2.0"

// Route the client is using.
type Route string

const (
	RouteDirect Route = "direct" // home address, pinned TLS
	RoutePublic Route = "public" // outside address (tunnel: web PKI; direct mode: pinned)
)

// Endpoints come from the pairing.
type Endpoints struct {
	LAN        string   // https://host:port of the direct listener, pinned
	Public     string   // optional outside address
	PublicMode string   // "tunnel" | "direct" | ""
	Pins       []string // base64 SHA-256 of the direct key's SubjectPublicKeyInfo
}

// TokenStore holds the device token.
type TokenStore interface {
	Load() (*Token, error)
	Save(*Token) error
}

const (
	probeTimeout = 1500 * time.Millisecond
	reprobeEvery = 5 * time.Minute
)

// ErrPinMismatch is returned by the TLS layer when the server's key isn't pinned.
var ErrPinMismatch = errors.New("the gateway's key does not match the pairing (pin mismatch)")

// Client talks to one paired gateway. Safe for concurrent use.
type Client struct {
	Tokens        TokenStore
	ChunkLimit    int64           // upload chunk size (gateway max is 32 MiB)
	DownloadSlice int64           // Range slice for whole-file downloads
	RetryDelays   []time.Duration // transport errors only, uploads/downloads
	// OnPinsChanged is called when /v1/identity returned a new pin set.
	OnPinsChanged func([]string)

	mu        sync.Mutex
	ep        Endpoints
	pins      map[string]bool
	route     Route
	lastProbe time.Time

	direct    *http.Client // pinned, for the LAN address
	public    *http.Client // web PKI (tunnel) or pinned (direct mode)
	probe     *http.Client // pinned, 1.5 s
	pubProbe  *http.Client
	userAgent string
}

// New builds a client. A zero Public means home-only.
func New(ep Endpoints, tokens TokenStore) *Client {
	c := &Client{
		Tokens: tokens, ChunkLimit: 32 << 20, DownloadSlice: 32 << 20,
		RetryDelays: []time.Duration{time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second, 16 * time.Second},
		ep:          ep, route: RoutePublic,
		userAgent: "Loft/" + Version + " (Linux)",
	}
	c.setPinsLocked(ep.Pins)
	if ep.Public == "" {
		c.route = RouteDirect
	}
	pinned := c.pinnedTLS()
	c.direct = &http.Client{CheckRedirect: noRedirect, Transport: transport(pinned, 0)}
	c.probe = &http.Client{CheckRedirect: noRedirect, Transport: transport(pinned, probeTimeout), Timeout: probeTimeout}
	if ep.PublicMode == "direct" {
		c.public = &http.Client{CheckRedirect: noRedirect, Transport: transport(pinned, 0)}
		c.pubProbe = &http.Client{CheckRedirect: noRedirect, Transport: transport(pinned, probeTimeout), Timeout: probeTimeout}
	} else {
		c.public = &http.Client{CheckRedirect: noRedirect, Transport: transport(&tls.Config{MinVersion: tls.VersionTLS12}, 0)}
	}
	return c
}

// noRedirect: the API never redirects, and a token or password must not follow one.
func noRedirect(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

func transport(tc *tls.Config, dialTimeout time.Duration) *http.Transport {
	if dialTimeout == 0 {
		dialTimeout = 15 * time.Second
	}
	return &http.Transport{
		Proxy:                 nil, // never through a proxy: the pin is end to end
		DialContext:           (&net.Dialer{Timeout: dialTimeout, KeepAlive: 30 * time.Second}).DialContext,
		TLSClientConfig:       tc,
		TLSHandshakeTimeout:   15 * time.Second,
		MaxIdleConnsPerHost:   4,
		IdleConnTimeout:       90 * time.Second,
		ResponseHeaderTimeout: 0,
		ForceAttemptHTTP2:     false,
	}
}

// pinnedTLS trusts exactly the pinned keys: the chain is not used (the direct key is
// self-signed), the SPKI hash of the leaf must be in the pin set. A mismatch fails the
// handshake; it is never "accept any cert".
func (c *Client) pinnedTLS() *tls.Config {
	return &tls.Config{
		MinVersion:         tls.VersionTLS12,
		InsecureSkipVerify: true, // replaced by VerifyConnection below
		VerifyConnection: func(cs tls.ConnectionState) error {
			if len(cs.PeerCertificates) == 0 {
				return ErrPinMismatch
			}
			if c.pinned(SPKIPin(cs.PeerCertificates[0].RawSubjectPublicKeyInfo)) {
				return nil
			}
			return ErrPinMismatch
		},
	}
}

// SPKIPin is base64(SHA-256(SubjectPublicKeyInfo)), the form pairing codes carry.
func SPKIPin(spki []byte) string {
	h := sha256.Sum256(spki)
	return base64.StdEncoding.EncodeToString(h[:])
}

func (c *Client) pinned(pin string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.pins[pin]
}

func (c *Client) setPinsLocked(p []string) {
	c.pins = map[string]bool{}
	for _, x := range p {
		c.pins[x] = true
	}
	c.ep.Pins = append([]string(nil), p...)
}

// Pins returns the current pin set.
func (c *Client) Pins() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]string(nil), c.ep.Pins...)
}

// Endpoints returns the pairing endpoints in use.
func (c *Client) Endpoints() Endpoints {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.ep
	e.Pins = append([]string(nil), c.ep.Pins...)
	return e
}

// Route is the route requests currently go to.
func (c *Client) Route() Route {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.route
}

// CanPasswordSignIn: a pinned route is up (home, or a direct-mode outside address).
func (c *Client) CanPasswordSignIn() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.route == RouteDirect || c.ep.PublicMode == "direct"
}

func (c *Client) base(r Route) (string, *http.Client) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if r == RouteDirect {
		return c.ep.LAN, c.direct
	}
	return c.ep.Public, c.public
}

// Reprobe checks the home address now (start, network change, every 5 min).
func (c *Client) Reprobe(ctx context.Context) Route {
	ok := c.probeDirect(ctx)
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lastProbe = time.Now()
	if ok || c.ep.Public == "" {
		c.route = RouteDirect
	} else {
		c.route = RoutePublic
	}
	return c.route
}

// ensureFresh re-probes when the last probe is older than 5 minutes.
func (c *Client) ensureFresh(ctx context.Context) {
	c.mu.Lock()
	stale := time.Since(c.lastProbe) > reprobeEvery
	homeOnly := c.ep.Public == ""
	c.mu.Unlock()
	if stale && !homeOnly {
		c.Reprobe(ctx)
	}
}

// ProbeDirect: GET <lan>/healthz over the pinned transport with a 1.5 s budget. Only a
// 200 with {"service":"loft","route":"direct"} counts.
func (c *Client) probeDirect(ctx context.Context) bool {
	h, err := c.health(ctx, c.ep.LAN, c.probe)
	return err == nil && h.Service == "loft" && h.Route == "direct"
}

// Health fetches /healthz from a route without auth (for `loft status`).
func (c *Client) Health(ctx context.Context, r Route) (*Health, error) {
	b, hc := c.base(r)
	if b == "" {
		return nil, &UnreachableError{Reason: "no outside address"}
	}
	if r == RouteDirect {
		hc = c.probe
	} else if c.pubProbe != nil {
		hc = c.pubProbe
	}
	return c.health(ctx, b, hc)
}

func (c *Client) health(ctx context.Context, base string, hc *http.Client) (*Health, error) {
	ctx, cancel := context.WithTimeout(ctx, 2*probeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, "GET", base+"/healthz", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", c.userAgent)
	resp, err := hc.Do(req)
	if err != nil {
		return nil, unreachable(err)
	}
	defer resp.Body.Close()
	var h Health
	if resp.StatusCode != 200 || json.NewDecoder(io.LimitReader(resp.Body, 1<<16)).Decode(&h) != nil {
		return nil, &UnreachableError{Reason: fmt.Sprintf("healthz %d", resp.StatusCode)}
	}
	return &h, nil
}

func unreachable(err error) *UnreachableError {
	return &UnreachableError{Reason: err.Error(), PinMismatch: errors.Is(err, ErrPinMismatch) ||
		strings.Contains(err.Error(), ErrPinMismatch.Error())}
}

// request describes one API call.
type request struct {
	method  string
	path    string
	query   url.Values
	body    []byte
	ctype   string
	headers map[string]string
	noAuth  bool
	route   Route         // "" = the selected route (with failover)
	timeout time.Duration // per attempt
}

type response struct {
	status int
	header http.Header
	body   []byte
}

// do runs one request. On the selected route it falls over once to the other route when
// there is no answer, and re-reads the token once when another process rotated it.
func (c *Client) do(ctx context.Context, r request) (*response, error) {
	fixed := r.route != ""
	if !fixed {
		c.ensureFresh(ctx)
	}
	triedOther := fixed
	triedNewToken := false
	route := r.route
	if route == "" {
		route = c.Route()
	}
	for {
		base, hc := c.base(route)
		if base == "" {
			return nil, &UnreachableError{Reason: "no address for route " + string(route)}
		}
		u := base + "/" + strings.TrimLeft(r.path, "/")
		if len(r.query) > 0 {
			u += "?" + r.query.Encode()
		}
		actx, cancel := ctx, context.CancelFunc(func() {})
		if r.timeout > 0 {
			actx, cancel = context.WithTimeout(ctx, r.timeout)
		}
		var body io.Reader
		if r.body != nil {
			body = bytes.NewReader(r.body)
		}
		req, err := http.NewRequestWithContext(actx, r.method, u, body)
		if err != nil {
			cancel()
			return nil, err
		}
		req.Header.Set("User-Agent", c.userAgent)
		req.Header.Set("Cache-Control", "no-store")
		if r.body != nil {
			ct := r.ctype
			if ct == "" {
				ct = "application/json"
			}
			req.Header.Set("Content-Type", ct)
		}
		for k, v := range r.headers {
			req.Header.Set(k, v)
		}
		var sent string
		if !r.noAuth {
			t, err := c.Tokens.Load()
			if err != nil || t == nil || t.Token == "" {
				cancel()
				return nil, ErrNotSignedIn
			}
			sent = t.Token
			req.Header.Set("Authorization", "Bearer "+t.Token)
		}
		resp, err := hc.Do(req)
		if err != nil {
			cancel()
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			if !triedOther {
				triedOther = true
				if route == RouteDirect && c.hasPublic() {
					c.directFailed()
					route = RoutePublic
					continue
				}
				if route == RoutePublic && c.probeDirect(ctx) {
					c.setRoute(RouteDirect)
					route = RouteDirect
					continue
				}
			}
			return nil, unreachable(err)
		}
		data, rerr := io.ReadAll(resp.Body)
		resp.Body.Close()
		cancel()
		if rerr != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return nil, unreachable(rerr)
		}
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return &response{resp.StatusCode, resp.Header, data}, nil
		}
		var ae APIError
		decoded := json.Unmarshal(data, &ae) == nil && ae.Code != ""
		// A proxy in front of a dead tunnel answers 502/521/523/530 with HTML: that is
		// "no answer from home", not a gateway verdict.
		if !decoded && resp.StatusCode >= 500 {
			return nil, &UnreachableError{Reason: fmt.Sprintf("edge %d", resp.StatusCode)}
		}
		if !decoded {
			ae = APIError{Code: fmt.Sprintf("http_%d", resp.StatusCode)}
		}
		ae.Status = resp.StatusCode
		if resp.StatusCode == 401 && ae.Code == "invalid_token" && !triedNewToken && !r.noAuth {
			if t, err := c.Tokens.Load(); err == nil && t != nil && t.Token != sent {
				triedNewToken = true
				continue
			}
		}
		return nil, &ae
	}
}

func (c *Client) hasPublic() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ep.Public != ""
}

func (c *Client) directFailed() { c.setRoute(RoutePublic) }

func (c *Client) setRoute(r Route) {
	c.mu.Lock()
	c.route = r
	c.mu.Unlock()
}

func (c *Client) call(ctx context.Context, out any, r request) error {
	resp, err := c.do(ctx, r)
	if err != nil {
		return err
	}
	if out == nil {
		return nil
	}
	if err := json.Unmarshal(resp.body, out); err != nil {
		return &BadResponseError{Status: resp.status, Msg: fmt.Sprintf("%T: %v", out, err)}
	}
	return nil
}

func jsonBody(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}

// ── unauthenticated ─────────────────────────────────────────────────────────

// Identity fetches the current pins over a trusted route (pinned home address, or the
// outside address: web PKI for a tunnel) and adopts them.
func (c *Client) RefreshPins(ctx context.Context) ([]string, error) {
	var id Identity
	if err := c.call(ctx, &id, request{method: "GET", path: "v1/identity", noAuth: true, timeout: 10 * time.Second}); err != nil {
		return nil, err
	}
	if len(id.DirectSPKISHA256) == 0 {
		return c.Pins(), nil
	}
	c.mu.Lock()
	changed := len(id.DirectSPKISHA256) != len(c.ep.Pins)
	for _, p := range id.DirectSPKISHA256 {
		if !c.pins[p] {
			changed = true
		}
	}
	if changed {
		c.setPinsLocked(id.DirectSPKISHA256)
	}
	cb := c.OnPinsChanged
	c.mu.Unlock()
	if changed && cb != nil {
		cb(id.DirectSPKISHA256)
	}
	return id.DirectSPKISHA256, nil
}

// ── auth ────────────────────────────────────────────────────────────────────

// Login sends the NAS password, only ever to a pinned route: the home address, or the
// outside address when the pairing says it is a direct (pinned) port forward. Never to a
// tunnel. The password is not kept.
func (c *Client) Login(ctx context.Context, user, password, deviceName string) (*LoginResult, error) {
	body := jsonBody(map[string]string{"user": user, "password": password, "device_name": deviceName})
	var out LoginResult
	err := c.call(ctx, &out, request{method: "POST", path: "v1/auth/login", body: body, noAuth: true,
		route: RouteDirect, timeout: 30 * time.Second})
	if err == nil {
		c.setRoute(RouteDirect)
		return &out, nil
	}
	if !IsUnreachable(err) {
		return nil, err
	}
	e := c.Endpoints()
	if e.Public == "" || e.PublicMode != "direct" {
		var u *UnreachableError
		if errors.As(err, &u) && u.PinMismatch {
			return nil, err
		}
		return nil, fmt.Errorf("%w (%v)", ErrSignInNeedsHome, err)
	}
	if err := c.call(ctx, &out, request{method: "POST", path: "v1/auth/login", body: body, noAuth: true,
		route: RoutePublic, timeout: 30 * time.Second}); err != nil {
		return nil, err
	}
	return &out, nil
}

// RotateToken issues a new device token and stores it before returning.
func (c *Client) RotateToken(ctx context.Context) (*Token, error) {
	var r LoginResult
	if err := c.call(ctx, &r, request{method: "POST", path: "v1/auth/refresh", timeout: 30 * time.Second}); err != nil {
		return nil, err
	}
	t := TokenFrom(&r)
	if err := c.Tokens.Save(t); err != nil {
		return nil, err
	}
	return t, nil
}

func (c *Client) Devices(ctx context.Context) (*DevicesResponse, error) {
	var out DevicesResponse
	return &out, c.call(ctx, &out, request{method: "GET", path: "v1/auth/devices"})
}

func (c *Client) Revoke(ctx context.Context, deviceID string) error {
	return c.call(ctx, nil, request{method: "DELETE", path: "v1/auth/device/" + url.PathEscape(deviceID)})
}

// ── roots ───────────────────────────────────────────────────────────────────

func (c *Client) Me(ctx context.Context) (*Me, error) {
	var out Me
	return &out, c.call(ctx, &out, request{method: "GET", path: "v1/me"})
}

func (c *Client) Shares(ctx context.Context) ([]string, error) {
	var out SharesResponse
	return out.Shares, c.call(ctx, &out, request{method: "GET", path: "v1/roots"})
}

func (c *Client) Folders(ctx context.Context, share, path string) (*FoldersResponse, error) {
	var out FoldersResponse
	return &out, c.call(ctx, &out, request{method: "GET", path: "v1/roots",
		query: url.Values{"share": {share}, "path": {path}}})
}

func (c *Client) SetRoot(ctx context.Context, share, path string) (*Me, error) {
	var out Me
	return &out, c.call(ctx, &out, request{method: "PUT", path: "v1/me/root",
		body: jsonBody(map[string]string{"share": share, "path": path})})
}

// ── items ───────────────────────────────────────────────────────────────────

func (c *Client) Item(ctx context.Context, id string) (*Item, error) {
	var out Item
	return &out, c.call(ctx, &out, request{method: "GET", path: "v1/items/" + url.PathEscape(id)})
}

func (c *Client) Children(ctx context.Context, id, cursor string, limit int) (*ChildrenPage, error) {
	q := url.Values{"limit": {fmt.Sprint(limit)}}
	if cursor != "" {
		q.Set("cursor", cursor)
	}
	var out ChildrenPage
	return &out, c.call(ctx, &out, request{method: "GET", path: "v1/items/" + url.PathEscape(id) + "/children",
		query: q, timeout: 120 * time.Second})
}

// AllChildren reads every page of a folder.
func (c *Client) AllChildren(ctx context.Context, id string) ([]Item, error) {
	var all []Item
	cursor := ""
	for {
		p, err := c.Children(ctx, id, cursor, 2000)
		if err != nil {
			return nil, err
		}
		all = append(all, p.Items...)
		if p.NextCursor == nil || *p.NextCursor == "" {
			return all, nil
		}
		cursor = *p.NextCursor
	}
}

func (c *Client) Changes(ctx context.Context, anchor string, wait, limit int) (*ChangesPage, error) {
	var out ChangesPage
	return &out, c.call(ctx, &out, request{method: "GET", path: "v1/changes",
		query:   url.Values{"anchor": {anchor}, "wait": {fmt.Sprint(wait)}, "limit": {fmt.Sprint(limit)}},
		timeout: time.Duration(wait)*time.Second + 30*time.Second})
}

func (c *Client) LatestAnchor(ctx context.Context) (string, error) {
	var out struct {
		Anchor string `json:"anchor"`
	}
	return out.Anchor, c.call(ctx, &out, request{method: "GET", path: "v1/changes/latest"})
}

func (c *Client) Mkdir(ctx context.Context, parentID, name string) (*Item, error) {
	var out Item
	return &out, c.call(ctx, &out, request{method: "POST", path: "v1/folders",
		body: jsonBody(map[string]string{"parent_id": parentID, "name": name})})
}

// Patch renames and/or moves an item. ifMatch is the meta etag ("" = last writer wins).
func (c *Client) Patch(ctx context.Context, id, name, parentID, ifMatch string) (*Item, error) {
	b := map[string]string{}
	if name != "" {
		b["name"] = name
	}
	if parentID != "" {
		b["parent_id"] = parentID
	}
	if ifMatch != "" {
		b["if_match"] = ifMatch
	}
	var out Item
	return &out, c.call(ctx, &out, request{method: "PATCH", path: "v1/items/" + url.PathEscape(id), body: jsonBody(b)})
}

// Delete moves an item to the gateway's trash. ifMatch is the content etag (files only).
func (c *Client) Delete(ctx context.Context, id, ifMatch string) (*DeleteResponse, error) {
	var q url.Values
	if ifMatch != "" {
		q = url.Values{"if_match": {ifMatch}}
	}
	var out DeleteResponse
	return &out, c.call(ctx, &out, request{method: "DELETE", path: "v1/items/" + url.PathEscape(id), query: q})
}
