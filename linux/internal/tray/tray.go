// Package tray shows Loft in the desktop's tray through StatusNotifierItem (KDE, GNOME
// with the AppIndicator extension, Xfce, Cinnamon, Budgie, waybar…), pure Go over D-Bus.
//
// Why not fyne.io/systray: it is pure Go on Linux too, but it only sends pixmaps (its
// IconName is fixed to ""), so the desktop can't recolour Loft's symbolic icons, and
// their #2e3436 grey would vanish on a dark panel such as GNOME's top bar. Here the icon
// is sent by name (no.incremental.loft[-state]-symbolic) and the desktop draws it in
// its own panel colour.
package tray

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"

	"github.com/godbus/dbus/v5"
	"github.com/godbus/dbus/v5/introspect"
	"github.com/godbus/dbus/v5/prop"
)

// State of the icon: the same four as the Mac menu bar and the Windows tray.
type State int

const (
	Idle State = iota
	Syncing
	Offline
	Error
)

func (s State) icon() string {
	switch s {
	case Syncing:
		return "no.incremental.loft-syncing-symbolic"
	case Offline:
		return "no.incremental.loft-offline-symbolic"
	case Error:
		return "no.incremental.loft-error-symbolic"
	}
	return "no.incremental.loft-symbolic"
}

// ErrNoHost: nothing on this desktop shows StatusNotifierItems.
var ErrNoHost = errors.New("this desktop shows no tray icons (no StatusNotifierWatcher)")

// Options for the tray.
type Options struct {
	OnOpen        func() // "Open Loft folder"
	OnUpdate      func() // "Loft x.y is available"
	OnQuit        func() // "Quit Loft"
	IconThemePath string // where the icons are when not installed in /usr/share/icons
}

// Tray is a running tray icon.
type Tray struct {
	opt   Options
	conn  *dbus.Conn
	props *prop.Properties
	name  string

	mu       sync.Mutex
	state    State
	status   string
	detail   string
	update   string
	revision uint32
}

const (
	itemPath  = dbus.ObjectPath("/StatusNotifierItem")
	menuPath  = dbus.ObjectPath("/MenuBar")
	itemIface = "org.kde.StatusNotifierItem"
	menuIface = "com.canonical.dbusmenu"
	watcher   = "org.kde.StatusNotifierWatcher"
)

// Start shows the icon. It fails with ErrNoHost where no tray exists.
func Start(opt Options) (*Tray, error) {
	conn, err := dbus.ConnectSessionBus()
	if err != nil {
		return nil, fmt.Errorf("no D-Bus session: %w", err)
	}
	var has bool
	if err := conn.BusObject().Call("org.freedesktop.DBus.NameHasOwner", 0, watcher).Store(&has); err != nil || !has {
		conn.Close()
		return nil, ErrNoHost
	}
	t := &Tray{opt: opt, conn: conn, state: Idle, status: tr("Starting…"), revision: 1,
		name: fmt.Sprintf("org.kde.StatusNotifierItem-%d-1", os.Getpid())}
	if err := t.export(); err != nil {
		conn.Close()
		return nil, err
	}
	if _, err := conn.RequestName(t.name, dbus.NameFlagDoNotQueue); err != nil {
		conn.Close()
		return nil, err
	}
	if err := t.register(); err != nil {
		conn.Close()
		return nil, err
	}
	go t.reRegister()
	return t, nil
}

func (t *Tray) register() error {
	return t.conn.Object(watcher, "/StatusNotifierWatcher").
		Call(watcher+".RegisterStatusNotifierItem", 0, t.name).Err
}

// reRegister follows a restarted panel (the watcher comes back with a new owner).
func (t *Tray) reRegister() {
	t.conn.AddMatchSignal(dbus.WithMatchInterface("org.freedesktop.DBus"),
		dbus.WithMatchMember("NameOwnerChanged"), dbus.WithMatchArg(0, watcher))
	ch := make(chan *dbus.Signal, 8)
	t.conn.Signal(ch)
	for s := range ch {
		if s.Name == "org.freedesktop.DBus.NameOwnerChanged" && len(s.Body) == 3 {
			if n, _ := s.Body[2].(string); n != "" {
				t.register()
			}
		}
	}
}

// Close removes the icon.
func (t *Tray) Close() {
	if t != nil && t.conn != nil {
		t.conn.Close()
	}
}

type tooltip struct {
	Icon    string
	Pixmaps []pixmap
	Title   string
	Text    string
}

type pixmap struct {
	W, H int32
	Data []byte
}

func (t *Tray) export() error {
	item := &sni{t}
	if err := t.conn.Export(item, itemPath, itemIface); err != nil {
		return err
	}
	props, err := prop.Export(t.conn, itemPath, prop.Map{itemIface: {
		"Category":      {Value: "ApplicationStatus", Emit: prop.EmitTrue},
		"Id":            {Value: "no.incremental.loft", Emit: prop.EmitTrue},
		"Title":         {Value: "Loft", Emit: prop.EmitTrue},
		"Status":        {Value: "Active", Emit: prop.EmitTrue},
		"WindowId":      {Value: int32(0), Emit: prop.EmitTrue},
		"IconName":      {Value: Idle.icon(), Emit: prop.EmitTrue},
		"IconThemePath": {Value: t.opt.IconThemePath, Emit: prop.EmitTrue},
		"ToolTip":       {Value: tooltip{Icon: Idle.icon(), Title: "Loft", Text: t.status}, Emit: prop.EmitTrue},
		"ItemIsMenu":    {Value: true, Emit: prop.EmitTrue},
		"Menu":          {Value: menuPath, Emit: prop.EmitTrue},
	}})
	if err != nil {
		return err
	}
	t.props = props
	menu := &dbusmenu{t}
	if err := t.conn.Export(menu, menuPath, menuIface); err != nil {
		return err
	}
	if _, err := prop.Export(t.conn, menuPath, prop.Map{menuIface: {
		"Version":       {Value: uint32(3), Emit: prop.EmitTrue},
		"TextDirection": {Value: "ltr", Emit: prop.EmitTrue},
		"Status":        {Value: "normal", Emit: prop.EmitTrue},
		"IconThemePath": {Value: []string{}, Emit: prop.EmitTrue},
	}}); err != nil {
		return err
	}
	for path, xml := range map[dbus.ObjectPath]string{itemPath: itemXML, menuPath: menuXML} {
		if err := t.conn.Export(introspect.Introspectable(introspectHead+xml), path,
			"org.freedesktop.DBus.Introspectable"); err != nil {
			return err
		}
	}
	return nil
}

// Set updates the icon and the two status lines.
func (t *Tray) Set(s State, status, detail string) {
	t.mu.Lock()
	changedIcon := s != t.state
	changedText := status != t.status || detail != t.detail
	t.state, t.status, t.detail = s, status, detail
	if changedText {
		t.revision++
	}
	rev := t.revision
	t.mu.Unlock()
	if changedIcon {
		t.props.SetMust(itemIface, "IconName", s.icon())
		t.conn.Emit(itemPath, itemIface+".NewIcon")
	}
	if changedText || changedIcon {
		text := status
		if detail != "" {
			text += "\n" + detail
		}
		t.props.SetMust(itemIface, "ToolTip", tooltip{Icon: s.icon(), Title: "Loft", Text: text})
		t.conn.Emit(itemPath, itemIface+".NewToolTip")
	}
	if changedText {
		t.conn.Emit(menuPath, menuIface+".LayoutUpdated", rev, int32(0))
	}
}

// SetUpdate shows "Loft x.y is available" ("" hides it).
func (t *Tray) SetUpdate(version string) {
	t.mu.Lock()
	if version == t.update {
		t.mu.Unlock()
		return
	}
	t.update = version
	t.revision++
	rev := t.revision
	t.mu.Unlock()
	t.conn.Emit(menuPath, menuIface+".LayoutUpdated", rev, int32(0))
}

// ── StatusNotifierItem methods ──────────────────────────────────────────────

type sni struct{ t *Tray }

func (s *sni) Activate(x, y int32) *dbus.Error {
	if s.t.opt.OnOpen != nil {
		go s.t.opt.OnOpen()
	}
	return nil
}
func (s *sni) SecondaryActivate(x, y int32) *dbus.Error           { return nil }
func (s *sni) ContextMenu(x, y int32) *dbus.Error                 { return nil }
func (s *sni) Scroll(delta int32, orientation string) *dbus.Error { return nil }

// ── com.canonical.dbusmenu ──────────────────────────────────────────────────

const (
	idStatus = 1 + iota
	idDetail
	idSep1
	idOpen
	idUpdate
	idSep2
	idQuit
)

type layout struct {
	ID       int32
	Props    map[string]dbus.Variant
	Children []dbus.Variant
}

type groupProps struct {
	ID    int32
	Props map[string]dbus.Variant
}

type menuEvent struct {
	ID        int32
	EventID   string
	Data      dbus.Variant
	Timestamp uint32
}

type dbusmenu struct{ t *Tray }

func (t *Tray) itemProps(id int32) map[string]dbus.Variant {
	t.mu.Lock()
	defer t.mu.Unlock()
	v := dbus.MakeVariant
	switch id {
	case 0:
		return map[string]dbus.Variant{"children-display": v("submenu")}
	case idStatus:
		return map[string]dbus.Variant{"label": v(t.status), "enabled": v(false)}
	case idDetail:
		return map[string]dbus.Variant{"label": v(t.detail), "enabled": v(false), "visible": v(t.detail != "")}
	case idSep1, idSep2:
		return map[string]dbus.Variant{"type": v("separator")}
	case idOpen:
		return map[string]dbus.Variant{"label": v(tr("Open Loft folder"))}
	case idUpdate:
		return map[string]dbus.Variant{"label": v(fmt.Sprintf(tr("Loft %s is available"), t.update)),
			"visible": v(t.update != "")}
	case idQuit:
		return map[string]dbus.Variant{"label": v(tr("Quit Loft"))}
	}
	return map[string]dbus.Variant{}
}

func filter(p map[string]dbus.Variant, names []string) map[string]dbus.Variant {
	if len(names) == 0 {
		return p
	}
	out := map[string]dbus.Variant{}
	for _, n := range names {
		if v, ok := p[n]; ok {
			out[n] = v
		}
	}
	return out
}

func (m *dbusmenu) GetLayout(parent, depth int32, names []string) (uint32, layout, *dbus.Error) {
	m.t.mu.Lock()
	rev := m.t.revision
	m.t.mu.Unlock()
	if parent != 0 {
		return rev, layout{ID: parent, Props: filter(m.t.itemProps(parent), names), Children: []dbus.Variant{}}, nil
	}
	root := layout{ID: 0, Props: filter(m.t.itemProps(0), names), Children: []dbus.Variant{}}
	if depth != 0 {
		for id := int32(idStatus); id <= idQuit; id++ {
			root.Children = append(root.Children, dbus.MakeVariant(
				layout{ID: id, Props: filter(m.t.itemProps(id), names), Children: []dbus.Variant{}}))
		}
	}
	return rev, root, nil
}

func (m *dbusmenu) GetGroupProperties(ids []int32, names []string) ([]groupProps, *dbus.Error) {
	if len(ids) == 0 {
		for id := int32(0); id <= idQuit; id++ {
			ids = append(ids, id)
		}
	}
	var out []groupProps
	for _, id := range ids {
		out = append(out, groupProps{ID: id, Props: filter(m.t.itemProps(id), names)})
	}
	return out, nil
}

func (m *dbusmenu) GetProperty(id int32, name string) (dbus.Variant, *dbus.Error) {
	if v, ok := m.t.itemProps(id)[name]; ok {
		return v, nil
	}
	return dbus.MakeVariant(""), nil
}

func (m *dbusmenu) Event(id int32, event string, data dbus.Variant, ts uint32) *dbus.Error {
	if event != "clicked" {
		return nil
	}
	var fn func()
	switch id {
	case idOpen:
		fn = m.t.opt.OnOpen
	case idUpdate:
		fn = m.t.opt.OnUpdate
	case idQuit:
		fn = m.t.opt.OnQuit
	}
	if fn != nil {
		go fn()
	}
	return nil
}

func (m *dbusmenu) EventGroup(events []menuEvent) ([]int32, *dbus.Error) {
	for _, e := range events {
		m.Event(e.ID, e.EventID, e.Data, e.Timestamp)
	}
	return []int32{}, nil
}

func (m *dbusmenu) AboutToShow(id int32) (bool, *dbus.Error) { return false, nil }

func (m *dbusmenu) AboutToShowGroup(ids []int32) ([]int32, []int32, *dbus.Error) {
	return []int32{}, []int32{}, nil
}

// ── words ───────────────────────────────────────────────────────────────────

var nb = map[string]string{
	"Starting…":            "Starter…",
	"Open Loft folder":     "Åpne Loft-mappen",
	"Loft %s is available": "Loft %s er klar",
	"Quit Loft":            "Avslutt Loft",
}

// tr gives the Norwegian text when the desktop language is Norwegian.
func tr(s string) string {
	for _, env := range []string{"LC_ALL", "LC_MESSAGES", "LANG"} {
		v := os.Getenv(env)
		if v == "" {
			continue
		}
		if strings.HasPrefix(v, "nb") || strings.HasPrefix(v, "nn") || strings.HasPrefix(v, "no") {
			if t, ok := nb[s]; ok {
				return t
			}
		}
		break
	}
	return s
}

// Tr is tr for the daemon's status lines.
func Tr(s string) string { return tr(s) }

const introspectHead = introspect.IntrospectDeclarationString + "<node>" +
	introspect.IntrospectDataString + prop.IntrospectDataString

const itemXML = `
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
 </interface>
</node>`

const menuXML = `
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
   <arg type="as" name="propertyNames" direction="in"/>
   <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
   <arg type="a(ia{sv})" name="properties" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
   <arg type="v" name="value" direction="out"/>
  </method>
  <method name="Event">
   <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
   <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
  </method>
  <method name="EventGroup">
   <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
  </method>
  <method name="AboutToShow">
   <arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
   <arg type="ai" name="ids" direction="in"/><arg type="ai" name="updatesNeeded" direction="out"/>
   <arg type="ai" name="idErrors" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
   <arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/>
  </signal>
  <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
  <signal name="ItemActivationRequested"><arg type="i" name="id"/><arg type="u" name="timestamp"/></signal>
 </interface>
</node>`
