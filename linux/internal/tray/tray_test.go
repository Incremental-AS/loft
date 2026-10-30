package tray

import (
	"bufio"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/godbus/dbus/v5"
)

// privateBus starts a throwaway session bus (dbus-daemon), so the test never touches
// the desktop's own.
func privateBus(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("dbus-daemon"); err != nil {
		t.Skip("dbus-daemon not installed")
	}
	cmd := exec.Command("dbus-daemon", "--session", "--nofork", "--print-address=1")
	out, _ := cmd.StdoutPipe()
	if err := cmd.Start(); err != nil {
		t.Skip("dbus-daemon:", err)
	}
	t.Cleanup(func() { cmd.Process.Kill(); cmd.Wait() })
	addr, err := bufio.NewReader(out).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("DBUS_SESSION_BUS_ADDRESS", strings.TrimSpace(addr))
}

type fakeWatcher struct{ got chan string }

func (w *fakeWatcher) RegisterStatusNotifierItem(service string) *dbus.Error {
	w.got <- service
	return nil
}

func TestNoHostWithoutWatcher(t *testing.T) {
	privateBus(t)
	if _, err := Start(Options{}); err != ErrNoHost {
		t.Fatalf("want ErrNoHost, got %v", err)
	}
}

func TestStatusNotifierItem(t *testing.T) {
	privateBus(t)
	t.Setenv("LANG", "en_GB.UTF-8")
	host, err := dbus.ConnectSessionBus()
	if err != nil {
		t.Fatal(err)
	}
	defer host.Close()
	w := &fakeWatcher{got: make(chan string, 1)}
	host.Export(w, "/StatusNotifierWatcher", watcher)
	if r, err := host.RequestName(watcher, 0); err != nil || r != dbus.RequestNameReplyPrimaryOwner {
		t.Fatal(err)
	}
	opened := make(chan bool, 1)
	tr, err := Start(Options{OnOpen: func() { opened <- true }, IconThemePath: "/opt/loft/share/icons"})
	if err != nil {
		t.Fatal(err)
	}
	defer tr.Close()
	var name string
	select {
	case name = <-w.got:
	case <-time.After(5 * time.Second):
		t.Fatal("not registered with the watcher")
	}
	if !strings.HasPrefix(name, "org.kde.StatusNotifierItem-") {
		t.Fatalf("registered as %q", name)
	}
	item := host.Object(name, itemPath)
	get := func(p string) any {
		v, err := item.GetProperty(itemIface + "." + p)
		if err != nil {
			t.Fatalf("%s: %v", p, err)
		}
		return v.Value()
	}
	if get("IconName") != "no.incremental.loft-symbolic" || get("Id") != "no.incremental.loft" ||
		get("IconThemePath") != "/opt/loft/share/icons" || get("Menu") != menuPath {
		t.Fatalf("properties: %v %v %v", get("IconName"), get("Id"), get("Menu"))
	}
	tr.Set(Syncing, "Uploading 2 files…", "Home network")
	if get("IconName") != "no.incremental.loft-syncing-symbolic" {
		t.Fatalf("icon after Set: %v", get("IconName"))
	}

	menu := host.Object(name, menuPath)
	var rev uint32
	var root layout
	if err := menu.Call(menuIface+".GetLayout", 0, int32(0), int32(-1), []string{}).Store(&rev, &root); err != nil {
		t.Fatal(err)
	}
	var labels []string
	for _, ch := range root.Children {
		var l layout
		if err := dbus.Store([]any{ch.Value()}, &l); err != nil {
			t.Fatal(err)
		}
		if v, ok := l.Props["label"]; ok {
			labels = append(labels, v.Value().(string))
		}
	}
	joined := strings.Join(labels, "|")
	if !strings.Contains(joined, "Uploading 2 files…") || !strings.Contains(joined, "Open Loft folder") ||
		!strings.Contains(joined, "Quit Loft") {
		t.Fatalf("menu: %q", joined)
	}
	if err := menu.Call(menuIface+".Event", 0, int32(idOpen), "clicked", dbus.MakeVariant(""), uint32(0)).Err; err != nil {
		t.Fatal(err)
	}
	select {
	case <-opened:
	case <-time.After(2 * time.Second):
		t.Fatal("Open Loft folder did nothing")
	}
	var xml string
	if err := item.Call("org.freedesktop.DBus.Introspectable.Introspect", 0).Store(&xml); err != nil ||
		!strings.Contains(xml, "org.kde.StatusNotifierItem") {
		t.Fatalf("introspection: %v", err)
	}
}

func TestNorwegian(t *testing.T) {
	t.Setenv("LC_ALL", "")
	t.Setenv("LC_MESSAGES", "")
	t.Setenv("LANG", "nb_NO.UTF-8")
	if tr("Quit Loft") != "Avslutt Loft" {
		t.Fatal(tr("Quit Loft"))
	}
	os.Unsetenv("LANG")
}
