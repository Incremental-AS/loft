package pairing

import (
	"encoding/base64"
	"strings"
	"testing"
)

var pin = base64.StdEncoding.EncodeToString(make([]byte, 32))

func payload(js string) string { return base64.RawURLEncoding.EncodeToString([]byte(js)) }

func TestParseForms(t *testing.T) {
	js := `{"v":1,"name":"Home","lan":"https://192.0.2.10:8443/","pins":["` + pin + `"],"pub":"https://files.example.org","mode":"tunnel"}`
	p64 := payload(js)
	for _, in := range []string{
		p64,
		"https://loft.incremental.no/pair#" + p64,
		"loft://pair#" + p64,
		"loft://pair?" + p64,
		"loft://pair?p=" + p64,
		"  " + p64 + "==\n",
	} {
		p, err := Parse(in)
		if err != nil {
			t.Fatalf("%q: %v", in, err)
		}
		if p.Name != "Home" || p.LAN != "https://192.0.2.10:8443" || p.Pub != "https://files.example.org" || p.Mode != ModeTunnel {
			t.Fatalf("%q: got %+v", in, p)
		}
		if r := p.SignInRoutes(); len(r) != 1 || r[0] != p.LAN {
			t.Fatalf("tunnel must never be a sign-in route: %v", r)
		}
	}
}

func TestDirectModeAllowsPinnedSignInOutside(t *testing.T) {
	p, err := Parse(payload(`{"v":1,"name":"H","lan":"https://h:8443","pins":["` + pin + `"],"pub":"https://out:8443","mode":"direct"}`))
	if err != nil {
		t.Fatal(err)
	}
	if r := p.SignInRoutes(); len(r) != 2 || r[1] != "https://out:8443" {
		t.Fatalf("got %v", r)
	}
}

func TestRefusals(t *testing.T) {
	cases := map[string]string{
		"version":   `{"v":2,"name":"H","lan":"https://h","pins":["` + pin + `"]}`,
		"http lan":  `{"v":1,"name":"H","lan":"http://h:8443","pins":["` + pin + `"]}`,
		"no pins":   `{"v":1,"name":"H","lan":"https://h:8443","pins":[]}`,
		"bad pin":   `{"v":1,"name":"H","lan":"https://h:8443","pins":["abc"]}`,
		"http pub":  `{"v":1,"name":"H","lan":"https://h","pins":["` + pin + `"],"pub":"http://o","mode":"tunnel"}`,
		"no mode":   `{"v":1,"name":"H","lan":"https://h","pins":["` + pin + `"],"pub":"https://o"}`,
		"odd mode":  `{"v":1,"name":"H","lan":"https://h","pins":["` + pin + `"],"pub":"https://o","mode":"vpn"}`,
		"no name":   `{"v":1,"name":"","lan":"https://h","pins":["` + pin + `"]}`,
		"mode only": `{"v":1,"name":"H","lan":"https://h","pins":["` + pin + `"],"mode":"tunnel"}`,
	}
	for name, js := range cases {
		if _, err := Parse(payload(js)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	for _, bad := range []string{"", "https://loft.incremental.no/pair", "%%%", payload("not json")} {
		if _, err := Parse(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestManualNeedsPin(t *testing.T) {
	if _, err := Manual("https://192.0.2.10:8443", ""); err == nil {
		t.Fatal("address without a pin accepted")
	}
	if _, err := Manual("http://192.0.2.10:8443", pin); err == nil {
		t.Fatal("http accepted")
	}
	p, err := Manual("https://192.0.2.10:8443/", "sha256//"+pin, pin)
	if err != nil {
		t.Fatal(err)
	}
	if p.LAN != "https://192.0.2.10:8443" || len(p.Pins) != 2 || p.Name != "192.0.2.10" {
		t.Fatalf("%+v", p)
	}
}

func TestRoundTrip(t *testing.T) {
	p := &Pairing{V: 1, Name: "Hytte ÆØÅ", LAN: "https://h:8443", Pins: []string{pin}}
	q, err := Parse(p.Link())
	if err != nil || q.Name != p.Name || !strings.HasPrefix(p.Link(), LinkBase) {
		t.Fatalf("%v %+v", err, q)
	}
}
