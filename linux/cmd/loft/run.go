package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/Incremental-AS/loft/linux/internal/cache"
	"github.com/Incremental-AS/loft/linux/internal/config"
	"github.com/Incremental-AS/loft/linux/internal/gateway"
	"github.com/Incremental-AS/loft/linux/internal/status"
	"github.com/Incremental-AS/loft/linux/internal/tray"
	"github.com/Incremental-AS/loft/linux/internal/update"
	"github.com/Incremental-AS/loft/linux/internal/vfs"
	"golang.org/x/sys/unix"
)

// ── mount / daemon ──────────────────────────────────────────────────────────

func cmdMount(args []string) error {
	fl := flag.NewFlagSet("mount", flag.ContinueOnError)
	fg := fl.Bool("foreground", false, "stay in the foreground (logs to the terminal)")
	debug := fl.Bool("debug", false, "log every FUSE call")
	if err := fl.Parse(reorder(args, nil)); err != nil {
		return err
	}
	e, err := load()
	if err != nil {
		return err
	}
	dir := e.cfg.Mountpoint()
	if fl.NArg() > 0 {
		dir, err = filepath.Abs(fl.Arg(0))
		if err != nil {
			return err
		}
	}
	if *fg {
		return run(e, dir, runOpts{debug: *debug})
	}
	return mountBackground(e, dir, *debug)
}

func cmdDaemon(args []string) error {
	fl := flag.NewFlagSet("daemon", flag.ContinueOnError)
	noTray := fl.Bool("no-tray", false, "no tray icon")
	debug := fl.Bool("debug", false, "log every FUSE call")
	if err := fl.Parse(args); err != nil {
		return err
	}
	e, err := load()
	if err != nil {
		return err
	}
	return run(e, e.cfg.Mountpoint(), runOpts{tray: !*noTray && e.cfg.TrayEnabled(), updates: true, debug: *debug})
}

// mountBackground starts `loft mount --foreground` detached and returns once the folder
// is mounted (or the child has failed).
func mountBackground(e *env, dir string, debug bool) error {
	if err := e.needAccount(); err != nil {
		return err
	}
	if s := status.Read(e.paths.State); s != nil {
		return needUser("Loft is already mounted at %s", s.Mountpoint)
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	logPath := filepath.Join(e.paths.State, "loft.log")
	lf, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer lf.Close()
	a := []string{"mount", "--foreground"}
	if debug {
		a = append(a, "--debug")
	}
	cmd := exec.Command(exe, append(a, dir)...)
	cmd.Stdout, cmd.Stderr = lf, lf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	deadline := time.After(60 * time.Second)
	for {
		select {
		case err := <-done:
			return fmt.Errorf("mount failed (%v); see %s:\n%s", err, logPath, tail(logPath, 8))
		case <-deadline:
			return fmt.Errorf("mount did not finish within 60 s; see %s", logPath)
		case <-time.After(100 * time.Millisecond):
			if mounted(dir) {
				fmt.Printf("Loft is mounted at %s (log: %s)\n", dir, logPath)
				return nil
			}
		}
	}
}

func tail(path string, n int) string {
	b, _ := os.ReadFile(path)
	lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return indent(strings.Join(lines, "\n"))
}

// mounted reports whether dir is a Loft FUSE mount.
func mounted(dir string) bool {
	f, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return false
	}
	defer f.Close()
	dir = filepath.Clean(dir)
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 10 {
			continue
		}
		mp := unescape(fields[4])
		for i, x := range fields {
			if x == "-" && i+1 < len(fields) {
				if mp == dir && strings.HasPrefix(fields[i+1], "fuse") && strings.Contains(sc.Text(), "loft") {
					return true
				}
				break
			}
		}
	}
	return false
}

func unescape(s string) string {
	r := strings.NewReplacer(`\040`, " ", `\011`, "\t", `\012`, "\n", `\134`, `\`)
	return r.Replace(s)
}

type runOpts struct {
	tray    bool
	updates bool
	debug   bool
}

// run mounts and keeps everything going until unmounted or signalled.
func run(e *env, dir string, o runOpts) error {
	if err := e.needAccount(); err != nil {
		return err
	}
	logger := log.New(os.Stderr, "", log.LstdFlags)
	if s := status.Read(e.paths.State); s != nil && s.PID != os.Getpid() {
		return needUser("Loft is already mounted at %s (pid %d)", s.Mountpoint, s.PID)
	}
	c, st, err := e.client(true)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c.Reprobe(ctx)
	acct := e.cfg.Account

	// The folder may have been changed on another device of this account: follow it.
	mctx, mcancel := context.WithTimeout(ctx, 20*time.Second)
	me, err := c.Me(mctx)
	mcancel()
	switch {
	case err == nil && me.Root == nil:
		return needUser("no folder picked yet. Run: loft root")
	case err == nil && (acct.Root == nil || acct.Root.ID != me.Root.ID):
		if n := pendingUploads(e); n > 0 && acct.Root != nil {
			logger.Printf("the gateway shows another folder (%s), but %d changes for %s wait for upload; keeping %s",
				me.Root.Display(), n, acct.Root.Display(), acct.Root.Display())
		} else {
			resetLocal(e)
			acct.Root = me.Root
			e.cfg.Save()
		}
	case err != nil && gateway.StateOf(err).NeedsSignIn():
		return needUser("%s", gateway.StateOf(err).Text())
	case err != nil && acct.Root == nil:
		return fmt.Errorf("can't reach the gateway to learn the folder: %w", err)
	case err != nil:
		logger.Printf("gateway not reachable now (%v); starting offline with the cached files", err)
	}

	cch, err := cache.Open(e.paths.Cache, e.cfg.CacheLimit())
	if err != nil {
		return err
	}
	defer cch.Close()
	fsys, err := vfs.New(vfs.Options{
		Client: c, Cache: cch, CacheDir: e.paths.Cache, StateDir: e.paths.State, RootID: acct.Root.RootItemID,
		EvictAfter: time.Duration(e.cfg.EvictAfterMinutes()) * time.Minute, Log: logger,
	})
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		// A dead earlier mount ("transport endpoint is not connected"): clear it.
		exec.Command(fusermount(), "-uz", dir).Run()
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return err
		}
	}
	if _, err := os.Stat(dir); err != nil {
		exec.Command(fusermount(), "-uz", dir).Run()
	}
	server, err := vfs.Mount(dir, fsys, o.debug)
	if err != nil {
		return fmt.Errorf("mount %s: %w (is FUSE available? fusermount3 must be installed)", dir, err)
	}
	fsys.Start(ctx)
	logger.Printf("Loft %s mounted at %s: %s on %q (%s route)", version, dir, acct.Root.Display(),
		e.cfg.Pairing.Name, c.Route())

	stop := func() {
		if err := server.Unmount(); err != nil {
			logger.Printf("unmount: %v; detaching lazily", err)
			exec.Command(fusermount(), "-uz", dir).Run()
		}
	}
	sig := make(chan os.Signal, 2)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sig
		stop()
	}()

	stat := &status.Status{PID: os.Getpid(), Mountpoint: dir, TokenStore: st.Where(),
		CacheLimit: e.cfg.CacheLimit(), MountedAt: time.Now()}
	var tr *tray.Tray
	if o.tray {
		tr, err = tray.Start(tray.Options{
			OnOpen:        func() { openPath(dir) },
			OnQuit:        stop,
			OnUpdate:      func() { openPath(stat.UpdateURL) },
			IconThemePath: iconThemePath(),
		})
		if err != nil {
			stat.Tray = err.Error()
			logger.Printf("no tray icon: %v", err)
		} else {
			stat.Tray = "shown"
			defer tr.Close()
		}
	}
	go maintain(ctx, e, c, fsys, cch, stat, tr, o, logger)
	server.Wait()
	cancel()
	fsys.Persist()
	status.Remove(e.paths.State)
	logger.Printf("Loft unmounted from %s", dir)
	return nil
}

// maintain: status file and tray every 2 s, token rotation, pin refresh, update check.
func maintain(ctx context.Context, e *env, c *gateway.Client, fsys *vfs.FS, cch *cache.Cache,
	stat *status.Status, tr *tray.Tray, o runOpts, logger *log.Logger) {
	tick := time.NewTicker(2 * time.Second)
	defer tick.Stop()
	var lastRotate, lastPins, lastUpdate time.Time
	for {
		now := time.Now()
		if now.Sub(lastRotate) > time.Hour {
			lastRotate = now
			if t, err := c.Tokens.Load(); err == nil && t != nil && t.DueForRotation() {
				if _, err := c.RotateToken(ctx); err != nil {
					logger.Printf("token rotation: %v (will retry)", err)
				} else {
					logger.Printf("device token rotated")
				}
			}
		}
		if now.Sub(lastPins) > 6*time.Hour {
			lastPins = now
			if _, err := c.RefreshPins(ctx); err != nil && !gateway.IsUnreachable(err) {
				logger.Printf("key pin refresh: %v", err)
			}
		}
		if o.updates && e.cfg.UpdateCheck() && now.Sub(lastUpdate) > time.Hour {
			lastUpdate = now
			rel, err := update.Check(ctx, e.cfg.ReleasesURL(), e.paths.State, version, now)
			if err != nil {
				logger.Printf("update check: %v", err)
			}
			if rel != nil {
				stat.Update, stat.UpdateURL = rel.Version, rel.URL
				if stat.UpdateURL == "" {
					stat.UpdateURL = "https://loft.incremental.no/"
				}
				if tr != nil {
					tr.SetUpdate(rel.Version)
				}
			}
		}
		snap := fsys.Snapshot()
		stat.Route = string(snap.Route)
		stat.State = string(snap.State)
		stat.StateText = snap.State.Text()
		stat.Pending, stat.Uploading, stat.Failed = snap.Pending, snap.Uploading, snap.Failed
		stat.LastSync = snap.LastSync
		stat.CacheUsed = cch.Used()
		status.Write(e.paths.State, stat)
		if tr != nil {
			s, line, detail := trayState(snap)
			tr.Set(s, line, detail)
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

func trayState(s vfs.Snapshot) (tray.State, string, string) {
	detail := ""
	if s.Route == gateway.RouteDirect {
		detail = tray.Tr("Home network")
	} else {
		detail = tray.Tr("Internet")
	}
	switch {
	case s.State.NeedsSignIn() || s.State == gateway.StateNoAccess || s.State == gateway.StateStopped:
		return tray.Error, s.State.Text(), ""
	case len(s.Failed) > 0:
		return tray.Error, plural(len(s.Failed), "file could not be uploaded", "files could not be uploaded"), ""
	case s.State == gateway.StateCantReachHome || s.State == gateway.StateCantReachNAS:
		if s.Pending > 0 {
			detail = plural(s.Pending, "change waiting to upload", "changes waiting to upload")
		}
		return tray.Offline, s.State.Text(), detail
	case s.Uploading > 0 || s.Pending > 0:
		return tray.Syncing, "Uploading " + plural(s.Pending, "file…", "files…"), detail
	}
	return tray.Idle, s.State.Text(), detail
}

func fusermount() string {
	if p, err := exec.LookPath("fusermount3"); err == nil {
		return p
	}
	return "fusermount"
}

// desktopNotice shows a message as a desktop notification when there is no terminal.
func desktopNotice(msg string) {
	if fi, err := os.Stderr.Stat(); err == nil && fi.Mode()&os.ModeCharDevice != 0 {
		return
	}
	if p, err := exec.LookPath("notify-send"); err == nil {
		exec.Command(p, "--app-name=Loft", "--icon=no.incremental.loft", "Loft", msg).Run()
	}
}

func openPath(p string) {
	if p == "" {
		return
	}
	cmd := exec.Command("xdg-open", p)
	cmd.Start()
	go cmd.Wait()
}

// iconThemePath points the tray at icons shipped next to the binary (AppImage,
// tarball). Installed packages put them in /usr/share/icons, where the desktop looks.
func iconThemePath() string {
	exe, err := os.Executable()
	if err != nil || strings.HasPrefix(exe, "/usr/") {
		return ""
	}
	p := filepath.Join(filepath.Dir(exe), "..", "share", "icons")
	if _, err := os.Stat(filepath.Join(p, "hicolor")); err == nil {
		return filepath.Clean(p)
	}
	return ""
}

// ── unmount ─────────────────────────────────────────────────────────────────

func cmdUnmount(args []string) error {
	fl := flag.NewFlagSet("unmount", flag.ContinueOnError)
	lazy := fl.Bool("lazy", false, "detach now even if files are open")
	if err := fl.Parse(reorder(args, nil)); err != nil {
		return err
	}
	e, err := load()
	if err != nil {
		return err
	}
	s := status.Read(e.paths.State)
	dir := e.cfg.Mountpoint()
	if s != nil {
		dir = s.Mountpoint
	}
	if fl.NArg() > 0 {
		dir, _ = filepath.Abs(fl.Arg(0))
	}
	if !mounted(dir) {
		fmt.Println("Loft is not mounted at", dir)
		return nil
	}
	if s != nil && s.Pending > 0 {
		fmt.Printf("Note: %s waiting for upload; they go up the next time Loft runs.\n",
			plural(s.Pending, "change is", "changes are"))
	}
	a := "-u"
	if *lazy {
		a = "-uz"
	}
	out, err := exec.Command(fusermount(), a, dir).CombinedOutput()
	if err != nil {
		msg := strings.TrimSpace(string(out))
		if strings.Contains(msg, "busy") {
			return exitErr{1, fmt.Sprintf("%s is in use (a terminal or program has it open). Close it, or: loft unmount --lazy", dir)}
		}
		return fmt.Errorf("%s: %s", fusermount(), msg)
	}
	if s != nil {
		for i := 0; i < 100 && status.Alive(s.PID); i++ {
			time.Sleep(100 * time.Millisecond)
		}
	}
	fmt.Println("Loft unmounted from", dir)
	return nil
}

// ── status ──────────────────────────────────────────────────────────────────

func cmdStatus(args []string) error {
	fl := flag.NewFlagSet("status", flag.ContinueOnError)
	asJSON := fl.Bool("json", false, "machine-readable")
	waitIdle := fl.Duration("wait-idle", 0, "wait until nothing waits for upload (scripts, tests)")
	if err := fl.Parse(args); err != nil {
		return err
	}
	e, err := load()
	if err != nil {
		return err
	}
	if *waitIdle > 0 {
		return waitForIdle(e, *waitIdle)
	}
	s := status.Read(e.paths.State)
	if *asJSON {
		out := map[string]any{"paired": e.cfg.Pairing != nil, "signed_in": e.cfg.Account != nil, "mount": s}
		if e.cfg.Pairing != nil {
			out["gateway"] = e.cfg.Pairing.Name
		}
		if a := e.cfg.Account; a != nil {
			out["nas_user"], out["root"] = a.NASUser, a.Root
		}
		b, _ := json.MarshalIndent(out, "", "  ")
		fmt.Println(string(b))
		return nil
	}
	if e.cfg.Pairing == nil {
		fmt.Println("Not paired with a gateway. Run: loft pair <link>")
		return nil
	}
	p := e.cfg.Pairing
	fmt.Printf("Gateway:   %s\n", p.Name)
	fmt.Printf("           home %s (key pinned)\n", p.LAN)
	if p.Pub != "" {
		fmt.Printf("           outside %s (%s)\n", p.Pub, p.Mode)
	}
	if a := e.cfg.Account; a != nil {
		fmt.Printf("Signed in: %s, as %q\n", a.NASUser, a.DeviceName)
		if a.Root != nil {
			fmt.Printf("Folder:    %s\n", a.Root.Display())
		}
	} else {
		fmt.Println("Signed in: no (run: loft login)")
	}
	if s == nil {
		fmt.Println("Mounted:   no (run: loft mount)")
		c, _, err := e.client(false)
		if err == nil {
			ctx, cancel := ctxTimeout(4 * time.Second)
			defer cancel()
			if _, err := c.Health(ctx, gateway.RouteDirect); err == nil {
				fmt.Println("Route:     home network answers")
			} else if p.Pub != "" {
				if _, err := c.Health(ctx, gateway.RoutePublic); err == nil {
					fmt.Println("Route:     outside address answers")
				} else {
					fmt.Println("Route:     the gateway doesn't answer")
				}
			} else {
				fmt.Println("Route:     the home address doesn't answer")
			}
		}
		if n := pendingUploads(e); n > 0 {
			fmt.Printf("Waiting:   %s to upload\n", plural(n, "change", "changes"))
		}
		return nil
	}
	route := "Internet"
	if s.Route == string(gateway.RouteDirect) {
		route = "Home network"
	}
	fmt.Printf("Mounted:   %s (pid %d), %s\n", s.Mountpoint, s.PID, route)
	fmt.Printf("Status:    %s\n", s.StateText)
	if !s.LastSync.IsZero() {
		fmt.Printf("Last sync: %s\n", s.LastSync.Format("2006-01-02 15:04:05"))
	}
	if s.Pending+s.Uploading > 0 {
		fmt.Printf("Uploads:   %d waiting, %d in progress\n", s.Pending, s.Uploading)
	}
	for _, f := range s.Failed {
		fmt.Printf("Not uploaded: %s\n", f)
	}
	fmt.Printf("Cache:     %s of %s\n", config.HumanSize(s.CacheUsed), config.HumanSize(s.CacheLimit))
	fmt.Printf("Token:     %s\n", s.TokenStore)
	if s.Tray != "" && s.Tray != "shown" {
		fmt.Printf("Tray:      none (%s)\n", s.Tray)
	}
	if s.Update != "" {
		fmt.Printf("Update:    Loft %s is available: %s\n", s.Update, s.UpdateURL)
	}
	return nil
}

func waitForIdle(e *env, d time.Duration) error {
	start := time.Now()
	deadline := start.Add(d)
	for time.Now().Before(deadline) {
		s := status.Read(e.paths.State)
		if s == nil {
			return exitErr{1, "Loft is not mounted"}
		}
		if s.UpdatedAt.After(start) && s.Pending == 0 && s.Uploading == 0 {
			if len(s.Failed) > 0 {
				return exitErr{1, "not uploaded: " + strings.Join(s.Failed, "; ")}
			}
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return exitErr{1, "uploads still waiting after " + d.String()}
}

// ── pin ─────────────────────────────────────────────────────────────────────

func cmdPin(on bool, args []string) error {
	if len(args) == 0 {
		return needUser("usage: loft pin|unpin <file in ~/Loft>…")
	}
	v := []byte("0")
	if on {
		v = []byte("1")
	}
	for _, a := range args {
		if err := unix.Setxattr(a, "user.loft.pinned", v, 0); err != nil {
			if errors.Is(err, unix.ENOTSUP) || errors.Is(err, unix.ENODATA) {
				return fmt.Errorf("%s: not a file in a Loft folder", a)
			}
			return fmt.Errorf("%s: %w", a, err)
		}
		if on {
			fmt.Println("Kept on this computer:", a)
		} else {
			fmt.Println("May leave the cache:", a)
		}
	}
	return nil
}

// ── open (desktop entry) ────────────────────────────────────────────────────

// cmdOpen is what the desktop entry runs: with a loft:// or pairing link it pairs;
// otherwise it makes sure Loft runs and opens the folder.
func cmdOpen(args []string) error {
	if len(args) > 0 && args[0] != "" {
		return cmdPair(args)
	}
	e, err := load()
	if err != nil {
		return err
	}
	if e.cfg.Account == nil {
		return needUser("not signed in. Open a terminal and run: loft login")
	}
	if status.Read(e.paths.State) == nil {
		if exec.Command("systemctl", "--user", "start", "loft.service").Run() != nil {
			if err := mountBackground(e, e.cfg.Mountpoint(), false); err != nil {
				return err
			}
		}
		for i := 0; i < 100 && !mounted(e.cfg.Mountpoint()); i++ {
			time.Sleep(100 * time.Millisecond)
		}
	}
	openPath(e.cfg.Mountpoint())
	return nil
}
