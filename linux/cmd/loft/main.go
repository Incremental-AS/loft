// Command loft is the Linux client of Loft: your NAS folders in ~/Loft, through your own
// Loft gateway. See linux/README.md.
package main

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/Incremental-AS/loft/linux/internal/config"
	"github.com/Incremental-AS/loft/linux/internal/gateway"
)

// version is set at build time (-ldflags "-X main.version=…").
var version = "0.2.0"

const usage = `Loft: your NAS folders in ~/Loft, through your own Loft gateway.

Usage:
  loft pair <link|code>            pair with a gateway (the link or code from loft-gateway pair)
  loft pair --manual <url> <pin>   pair by hand: the gateway's home address and its key pin
  loft login                       sign in with your NAS user name and password
  loft logout                      sign out and forget this computer's files in the cache
  loft status                      what Loft is doing
  loft mount [dir]                 show your files in dir (default ~/Loft)
  loft unmount                     stop showing them
  loft daemon                      mount, keep in sync, tray icon (what the service runs)
  loft devices [revoke <id>]       computers and phones signed in to your account
  loft root [share[/folder]|'*']   pick the folder on the NAS that Loft shows
  loft pin|unpin <file>            keep a file on this computer (or let the cache evict it)
  loft config [set <key> <value>]  settings: mountpoint, cache-limit, evict-after, update-check, tray
  loft version

Your password is only sent to the gateway over a connection whose key matches the
pairing code. Loft stores a device token, never the password.
`

func main() {
	gateway.Version = version
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	cmd, args := os.Args[1], os.Args[2:]
	var err error
	switch cmd {
	case "pair":
		err = cmdPair(args)
	case "login":
		err = cmdLogin(args)
	case "logout":
		err = cmdLogout(args)
	case "status":
		err = cmdStatus(args)
	case "mount":
		err = cmdMount(args)
	case "unmount", "umount":
		err = cmdUnmount(args)
	case "daemon":
		err = cmdDaemon(args)
	case "devices":
		err = cmdDevices(args)
	case "root":
		err = cmdRoot(args)
	case "pin", "unpin":
		err = cmdPin(cmd == "pin", args)
	case "config":
		err = cmdConfig(args)
	case "open":
		err = cmdOpen(args)
	case "version", "--version", "-v":
		fmt.Println("loft", version)
	case "help", "--help", "-h":
		fmt.Print(usage)
	default:
		fmt.Fprintf(os.Stderr, "loft: unknown command %q\n\n%s", cmd, usage)
		os.Exit(2)
	}
	if err != nil {
		if cmd == "open" {
			desktopNotice(err.Error()) // started from the menu: no terminal to read stderr
		}
		var ex exitErr
		if errors.As(err, &ex) {
			if ex.msg != "" {
				fmt.Fprintln(os.Stderr, "loft:", ex.msg)
			}
			os.Exit(ex.code)
		}
		fmt.Fprintln(os.Stderr, "loft:", err)
		os.Exit(1)
	}
}

// exitErr carries a specific exit code (2 = needs the user: pair/login first).
type exitErr struct {
	code int
	msg  string
}

func (e exitErr) Error() string { return e.msg }

func needUser(format string, a ...any) error { return exitErr{2, fmt.Sprintf(format, a...)} }

// env is what most commands need.
type env struct {
	paths config.Paths
	cfg   *config.Config
}

func load() (*env, error) {
	p := config.DefaultPaths()
	if err := p.Ensure(); err != nil {
		return nil, err
	}
	c, err := config.Load(p)
	if err != nil {
		return nil, err
	}
	return &env{paths: p, cfg: c}, nil
}

func (e *env) needPairing() error {
	if e.cfg.Pairing == nil {
		return needUser("not paired with a gateway yet. Run: loft pair <link from loft-gateway pair>")
	}
	return nil
}

func (e *env) needAccount() error {
	if err := e.needPairing(); err != nil {
		return err
	}
	if e.cfg.Account == nil {
		return needUser("not signed in. Run: loft login")
	}
	return nil
}

func plural(n int, one, many string) string {
	if n == 1 {
		return fmt.Sprintf("%d %s", n, one)
	}
	return fmt.Sprintf("%d %s", n, many)
}

func indent(s string) string {
	return "  " + strings.ReplaceAll(strings.TrimRight(s, "\n"), "\n", "\n  ")
}
