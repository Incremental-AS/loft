package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/cache"
	"github.com/Incremental-AS/loft/linux/internal/config"
	"github.com/Incremental-AS/loft/linux/internal/gateway"
	"github.com/Incremental-AS/loft/linux/internal/pairing"
	"github.com/Incremental-AS/loft/linux/internal/secrets"
	"github.com/Incremental-AS/loft/linux/internal/status"
	"github.com/Incremental-AS/loft/linux/internal/vfs"
	"golang.org/x/term"
)

func ctxTimeout(d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), d)
}

// client builds a gateway client with the stored token.
func (e *env) client(warn bool) (*gateway.Client, secrets.Store, error) {
	backend := ""
	if e.cfg.Account != nil {
		backend = e.cfg.Account.TokenStore
	}
	var w func(string)
	if warn {
		w = func(s string) { fmt.Fprintln(os.Stderr, "Note:", s) }
	}
	st, err := secrets.Open(backend, e.cfg.GatewayKey(), e.paths.Config, w)
	if err != nil {
		return nil, nil, err
	}
	c := gateway.New(e.cfg.Endpoints(), st)
	c.OnPinsChanged = func(p []string) {
		// Pins come from /v1/identity over a trusted route; keep them for next time.
		if c2, err := config.Load(e.paths); err == nil && c2.Pairing != nil {
			c2.Pairing.Pins = p
			c2.Save()
		}
	}
	return c, st, nil
}

// ── pair ────────────────────────────────────────────────────────────────────

func cmdPair(args []string) error {
	fl := flag.NewFlagSet("pair", flag.ContinueOnError)
	manual := fl.Bool("manual", false, "pair with an address and a key pin typed by hand")
	standby := fl.String("standby-pin", "", "with --manual: the standby key pin as well")
	replace := fl.Bool("replace", false, "pair with another gateway (signs this computer out)")
	login := fl.Bool("login", false, "sign in right after pairing")
	if err := fl.Parse(reorder(args, map[string]bool{"standby-pin": true})); err != nil {
		return err
	}
	e, err := load()
	if err != nil {
		return err
	}
	var p *pairing.Pairing
	switch {
	case *manual && fl.NArg() == 2:
		p, err = pairing.Manual(fl.Arg(0), fl.Arg(1), *standby)
	case *manual:
		return needUser("usage: loft pair --manual https://<gateway address>:<port> <pin>")
	case fl.NArg() == 1:
		p, err = pairing.Parse(fl.Arg(0))
	default:
		return needUser("usage: loft pair <link or code from `loft-gateway pair`>")
	}
	if err != nil {
		return err
	}
	if old := e.cfg.Pairing; old != nil && e.cfg.Account != nil && old.LAN != p.LAN && !*replace {
		return needUser("this computer is signed in to %q. Run `loft logout` first, or pair with --replace.", old.Name)
	}
	if old := e.cfg.Pairing; old != nil && old.LAN != p.LAN && e.cfg.Account != nil {
		if err := logout(e, true); err != nil {
			return err
		}
	}
	e.cfg.Pairing = p
	if err := e.cfg.Save(); err != nil {
		return err
	}
	fmt.Printf("Paired with %q.\n  Home address:    %s (key pinned)\n", p.Name, p.LAN)
	if p.Pub != "" {
		how := "through a tunnel: files only, sign in at home"
		if p.Mode == pairing.ModeDirect {
			how = "direct, key pinned"
		}
		fmt.Printf("  Outside address: %s (%s)\n", p.Pub, how)
	}
	c, _, err := e.client(false)
	if err != nil {
		return err
	}
	ctx, cancel := ctxTimeout(5 * time.Second)
	defer cancel()
	if h, err := c.Health(ctx, gateway.RouteDirect); err == nil && h.Service == "loft" {
		fmt.Println("  The gateway answers at the home address, and its key matches.")
	} else if u := new(gateway.UnreachableError); errors.As(err, &u) && u.PinMismatch {
		fmt.Println("  Warning: something answers at the home address, but its key does NOT match the pairing code.")
	} else {
		fmt.Println("  The home address doesn't answer from here right now.")
	}
	if *login || (e.cfg.Account == nil && term.IsTerminal(int(os.Stdin.Fd())) && askYes("Sign in now?")) {
		return cmdLogin(nil)
	}
	if e.cfg.Account == nil {
		fmt.Println("Next: loft login")
	}
	return nil
}

func askYes(q string) bool {
	fmt.Printf("%s [Y/n] ", q)
	line, _ := bufio.NewReader(os.Stdin).ReadString('\n')
	line = strings.ToLower(strings.TrimSpace(line))
	return line == "" || line == "y" || line == "yes" || line == "j" || line == "ja"
}

// reorder lets flags follow positional arguments (`loft pair <link> --login`).
func reorder(args []string, withValue map[string]bool) []string {
	var flags, pos []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			pos = append(pos, args[i+1:]...)
			break
		}
		if strings.HasPrefix(a, "-") && len(a) > 1 {
			flags = append(flags, a)
			name := strings.TrimLeft(a, "-")
			if withValue[name] && !strings.Contains(a, "=") && i+1 < len(args) {
				flags = append(flags, args[i+1])
				i++
			}
			continue
		}
		pos = append(pos, a)
	}
	return append(flags, pos...)
}

// ── login ───────────────────────────────────────────────────────────────────

func cmdLogin(args []string) error {
	fl := flag.NewFlagSet("login", flag.ContinueOnError)
	user := fl.String("user", "", "NAS user name")
	pwStdin := fl.Bool("password-stdin", false, "read the password from standard input (one line)")
	share := fl.String("share", "", "folder to show: share name ('*' = every share)")
	path := fl.String("path", "", "folder inside the share")
	device := fl.String("device-name", "", "name of this computer on the gateway (default: host name)")
	if err := fl.Parse(args); err != nil {
		return err
	}
	e, err := load()
	if err != nil {
		return err
	}
	if err := e.needPairing(); err != nil {
		return err
	}
	if s := status.Read(e.paths.State); s != nil && e.cfg.Account != nil && e.cfg.Account.Root != nil {
		// A running mount keeps working: only the token changes.
		fmt.Println("(Loft is mounted at", s.Mountpoint+"; it picks up the new sign-in by itself.)")
	}
	in := bufio.NewReader(os.Stdin)
	tty := term.IsTerminal(int(os.Stdin.Fd()))
	if *user == "" {
		if !tty {
			return needUser("give --user when not on a terminal")
		}
		fmt.Print("NAS user name: ")
		l, _ := in.ReadString('\n')
		*user = strings.TrimSpace(l)
	}
	if *user == "" {
		return needUser("no user name")
	}
	var pw string
	switch {
	case *pwStdin:
		l, err := in.ReadString('\n')
		if err != nil && l == "" {
			return needUser("no password on standard input")
		}
		pw = strings.TrimRight(l, "\r\n")
	case tty:
		fmt.Printf("Password for %s: ", *user)
		b, err := term.ReadPassword(int(os.Stdin.Fd()))
		fmt.Println()
		if err != nil {
			return err
		}
		pw = string(b)
	default:
		return needUser("not on a terminal: use --password-stdin")
	}
	if pw == "" {
		return needUser("no password")
	}
	if *device == "" {
		*device = deviceName()
	}

	// Sign-in goes only to a pinned route; choose the token store before it, so the
	// token is never shown or written anywhere else.
	backend := ""
	if e.cfg.Account != nil && e.cfg.Account.TokenStore != "" {
		backend = e.cfg.Account.TokenStore
	}
	st, err := secrets.Open(backend, e.cfg.GatewayKey(), e.paths.Config,
		func(s string) { fmt.Fprintln(os.Stderr, "Note:", s) })
	if err != nil {
		return err
	}
	c := gateway.New(e.cfg.Endpoints(), st)
	ctx, cancel := ctxTimeout(60 * time.Second)
	defer cancel()
	fmt.Printf("Signing in to %q over a connection checked against the pairing key…\n", e.cfg.Pairing.Name)
	res, err := c.Login(ctx, *user, pw, *device)
	pw = ""
	if err != nil {
		return loginError(err)
	}
	if err := st.Save(gateway.TokenFrom(res)); err != nil {
		return fmt.Errorf("could not store the device token in %s: %w", st.Where(), err)
	}
	acct := &config.Account{NASUser: *user, DeviceID: res.DeviceID, DeviceName: *device, TokenStore: st.Backend()}
	if res.NASUser != "" {
		acct.NASUser = res.NASUser
	}
	if old := e.cfg.Account; old != nil && old.NASUser == acct.NASUser {
		acct.Root = old.Root
	}
	e.cfg.Account = acct
	if err := e.cfg.Save(); err != nil {
		return err
	}
	fmt.Printf("Signed in as %s. The token is kept in %s.\n", acct.NASUser, st.Where())
	if _, err := c.RefreshPins(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "Note: could not refresh the key pins:", err)
	}

	me, err := c.Me(ctx)
	if err != nil {
		return err
	}
	switch {
	case *share != "":
		return setRoot(e, c, *share, *path)
	case me.Root != nil:
		acct.Root = me.Root
		e.cfg.Save()
		fmt.Printf("Folder: %s\n", me.Root.Display())
	default:
		if err := chooseRoot(e, c, in, tty); err != nil {
			return err
		}
	}
	fmt.Println("Next: loft mount   (or start it with your session: systemctl --user enable --now loft)")
	return nil
}

func loginError(err error) error {
	var u *gateway.UnreachableError
	if errors.As(err, &u) && u.PinMismatch {
		return exitErr{1, "the gateway did not show the key from the pairing code. Your password was not sent."}
	}
	if errors.Is(err, gateway.ErrSignInNeedsHome) {
		return exitErr{1, "sign-in only works where the gateway's home address answers (or a pinned outside address). " +
			"Your password was not sent. Try again at home or on the VPN."}
	}
	if a, ok := gateway.AsAPI(err); ok {
		switch a.Code {
		case "invalid_credentials":
			return exitErr{1, "wrong user name or password"}
		case "account_not_allowed":
			return exitErr{1, "this gateway doesn't allow that account"}
		case "too_many_attempts":
			wait := ""
			if a.RetryAfter > 0 {
				wait = fmt.Sprintf(" Wait %d minutes.", (a.RetryAfter+59)/60)
			}
			return exitErr{1, "too many sign-in attempts." + wait}
		case "nas_unavailable":
			return exitErr{1, "the gateway can't reach the NAS right now"}
		case "password_login_direct_only":
			return exitErr{1, "the gateway refused sign-in on this route"}
		}
	}
	return err
}

func deviceName() string {
	h, err := os.Hostname()
	if err != nil || h == "" {
		return "Linux"
	}
	if i := strings.IndexByte(h, '.'); i > 0 {
		h = h[:i]
	}
	return h
}

// ── root ────────────────────────────────────────────────────────────────────

func cmdRoot(args []string) error {
	e, err := load()
	if err != nil {
		return err
	}
	if err := e.needAccount(); err != nil {
		return err
	}
	c, _, err := e.client(false)
	if err != nil {
		return err
	}
	if len(args) == 0 {
		ctx, cancel := ctxTimeout(30 * time.Second)
		defer cancel()
		if r := e.cfg.Account.Root; r != nil {
			fmt.Println("Folder:", r.Display())
		}
		shares, err := c.Shares(ctx)
		if err != nil {
			return err
		}
		fmt.Println("Shares you can open:")
		for _, s := range shares {
			fmt.Println("  " + s)
		}
		fmt.Println("Change with: loft root <share>[/<folder>]   or   loft root '*' (every share)")
		return nil
	}
	share, path, _ := strings.Cut(strings.Trim(args[0], "/"), "/")
	return setRoot(e, c, share, path)
}

func setRoot(e *env, c *gateway.Client, share, path string) error {
	if s := status.Read(e.paths.State); s != nil {
		return needUser("Loft is mounted at %s. Run `loft unmount` first.", s.Mountpoint)
	}
	if old := e.cfg.Account.Root; old != nil && (old.Share != share || old.Path != path) {
		if n := pendingUploads(e); n > 0 {
			return needUser("%s still waiting for upload in the current folder. Mount and let them finish first.",
				plural(n, "file is", "files are"))
		}
	}
	ctx, cancel := ctxTimeout(30 * time.Second)
	defer cancel()
	me, err := c.SetRoot(ctx, share, path)
	if err != nil {
		if gateway.IsCode(err, "not_found") || gateway.IsCode(err, "no_such_share") {
			return exitErr{1, "no such folder on the NAS (or this account can't see it)"}
		}
		return err
	}
	if old := e.cfg.Account.Root; old == nil || old.ID != me.Root.ID {
		resetLocal(e) // listings and cache belong to the old folder
	}
	e.cfg.Account.Root = me.Root
	if err := e.cfg.Save(); err != nil {
		return err
	}
	fmt.Println("Folder:", me.Root.Display())
	return nil
}

func chooseRoot(e *env, c *gateway.Client, in *bufio.Reader, tty bool) error {
	ctx, cancel := ctxTimeout(30 * time.Second)
	defer cancel()
	shares, err := c.Shares(ctx)
	if err != nil {
		return err
	}
	sort.Strings(shares)
	switch {
	case len(shares) == 0:
		return exitErr{1, "this account can't open any share on the NAS"}
	case len(shares) == 1:
		return setRoot(e, c, shares[0], "")
	case !tty:
		return setRoot(e, c, gateway.AllShares, "")
	}
	fmt.Println("Which folder should Loft show?")
	fmt.Println("  0) every share, each as a folder")
	for i, s := range shares {
		fmt.Printf("  %d) %s\n", i+1, s)
	}
	for {
		fmt.Print("Number (or share/folder): ")
		l, err := in.ReadString('\n')
		l = strings.TrimSpace(l)
		if err != nil && l == "" {
			return needUser("no folder chosen. Run: loft root <share>")
		}
		if n, err := strconv.Atoi(l); err == nil && n >= 0 && n <= len(shares) {
			if n == 0 {
				return setRoot(e, c, gateway.AllShares, "")
			}
			return setRoot(e, c, shares[n-1], "")
		}
		if l != "" {
			share, path, _ := strings.Cut(strings.Trim(l, "/"), "/")
			if err := setRoot(e, c, share, path); err == nil {
				return nil
			} else {
				fmt.Println(err)
			}
		}
	}
}

// pendingUploads counts files with changes the gateway hasn't acknowledged.
// Read from the saved files only, so it is safe while a mount runs; files with names
// that never leave this computer don't count.
func pendingUploads(e *env) int {
	local := vfs.LocalOnly(e.paths.State)
	n := 0
	for _, k := range cache.DirtyInIndex(e.paths.Cache) {
		if !local[k] {
			n++
		}
	}
	return n
}

// resetLocal forgets listings, cache and queue (another folder or account).
func resetLocal(e *env) {
	if c, err := cache.Open(e.paths.Cache, e.cfg.CacheLimit()); err == nil {
		c.Clear()
		c.Close()
	}
	os.Remove(filepath.Join(e.paths.Cache, "meta.json"))
	os.Remove(filepath.Join(e.paths.State, "pending.json"))
}

// ── logout ──────────────────────────────────────────────────────────────────

func cmdLogout(args []string) error {
	fl := flag.NewFlagSet("logout", flag.ContinueOnError)
	force := fl.Bool("force", false, "sign out even if changes are still waiting for upload (they are lost)")
	if err := fl.Parse(args); err != nil {
		return err
	}
	e, err := load()
	if err != nil {
		return err
	}
	if e.cfg.Account == nil {
		fmt.Println("Not signed in.")
		return nil
	}
	if s := status.Read(e.paths.State); s != nil {
		return needUser("Loft is mounted at %s. Run `loft unmount` first.", s.Mountpoint)
	}
	if n := pendingUploads(e); n > 0 && !*force {
		return needUser("%s still waiting for upload. Mount and let them finish, or use --force to lose them.",
			plural(n, "file is", "files are"))
	}
	return logout(e, true)
}

func logout(e *env, revoke bool) error {
	c, st, err := e.client(false)
	if err == nil && revoke {
		ctx, cancel := ctxTimeout(10 * time.Second)
		if t, _ := st.Load(); t != nil {
			if err := c.Revoke(ctx, t.DeviceID); err != nil && !gateway.IsUnreachable(err) {
				fmt.Fprintln(os.Stderr, "Note: the gateway did not confirm removing this computer:", err)
			} else if err != nil {
				fmt.Fprintln(os.Stderr, "Note: the gateway didn't answer; remove this computer later with `loft devices` elsewhere.")
			}
		}
		cancel()
	}
	if st != nil {
		if err := st.Clear(); err != nil {
			fmt.Fprintln(os.Stderr, "Note: could not remove the token from", st.Where()+":", err)
		}
	}
	resetLocal(e)
	e.cfg.Account = nil
	if err := e.cfg.Save(); err != nil {
		return err
	}
	fmt.Println("Signed out. The pairing stays; sign in again with: loft login")
	return nil
}

// ── devices ─────────────────────────────────────────────────────────────────

func cmdDevices(args []string) error {
	e, err := load()
	if err != nil {
		return err
	}
	if err := e.needAccount(); err != nil {
		return err
	}
	c, _, err := e.client(false)
	if err != nil {
		return err
	}
	ctx, cancel := ctxTimeout(30 * time.Second)
	defer cancel()
	if len(args) == 2 && args[0] == "revoke" {
		if err := c.Revoke(ctx, args[1]); err != nil {
			return err
		}
		fmt.Println("Removed", args[1])
		return nil
	}
	if len(args) > 0 {
		return needUser("usage: loft devices [revoke <id>]")
	}
	d, err := c.Devices(ctx)
	if err != nil {
		return err
	}
	ts := func(f *float64) string {
		if f == nil {
			return "-"
		}
		return time.Unix(int64(*f), 0).Format("2006-01-02 15:04")
	}
	fmt.Printf("%-34s %-24s %-17s %s\n", "ID", "NAME", "LAST USED", "")
	for _, x := range d.Devices {
		note := ""
		if x.ID == d.Current {
			note = "this computer"
		}
		if x.RevokedAt != nil {
			note = "removed"
		}
		fmt.Printf("%-34s %-24s %-17s %s\n", x.ID, x.DeviceName, ts(x.LastUsedAt), note)
	}
	return nil
}

// ── config ──────────────────────────────────────────────────────────────────

func cmdConfig(args []string) error {
	e, err := load()
	if err != nil {
		return err
	}
	if len(args) == 3 && args[0] == "set" {
		if err := e.cfg.Set(args[1], args[2]); err != nil {
			return err
		}
		if err := e.cfg.Save(); err != nil {
			return err
		}
		fmt.Println("Saved. A running Loft picks it up when restarted.")
		return nil
	}
	if len(args) > 0 {
		return needUser("usage: loft config [set <key> <value>]")
	}
	onoff := func(b bool) string {
		if b {
			return "on"
		}
		return "off"
	}
	fmt.Printf("mountpoint    %s\n", e.cfg.Mountpoint())
	fmt.Printf("cache-limit   %s\n", config.HumanSize(e.cfg.CacheLimit()))
	fmt.Printf("evict-after   %d minutes (uploaded files leave the cache when unused this long)\n", e.cfg.EvictAfterMinutes())
	fmt.Printf("update-check  %s (asks %s at most once a day)\n", onoff(e.cfg.UpdateCheck()), e.cfg.ReleasesURL())
	fmt.Printf("tray          %s\n", onoff(e.cfg.TrayEnabled()))
	fmt.Printf("\nconfig file   %s\ncache         %s\nstate         %s\n", e.cfg.Path(), e.paths.Cache, e.paths.State)
	return nil
}
