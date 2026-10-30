package names

import "testing"

func TestExcluded(t *testing.T) {
	for _, n := range []string{"~$report.docx", ".~lock.a.odt#", "._x", ".DS_Store", "Thumbs.db", "desktop.ini",
		"Icon\r", "lu12345.tmp", "a.sb-1234-x", "Word Work File L_1.tmp", ".loft", ".loft-x"} {
		if !Excluded(n) {
			t.Errorf("%q not excluded", n)
		}
	}
	for _, n := range []string{"report.docx", "tmp", "a.tmpl", "~notes.txt", "Icon"} {
		if Excluded(n) {
			t.Errorf("%q excluded", n)
		}
	}
}

func TestKey(t *testing.T) {
	// NFD "é" and NFC "é", and case, give one key: the NAS can't hold both.
	if Key("Café.TXT") != Key("café.txt") {
		t.Fatal("NFC + casefold mismatch")
	}
	if NFC("Café") != "Café" {
		t.Fatal("NFC")
	}
	if Valid("a/b") || Valid("..") || Valid("") || !Valid("ok.txt") {
		t.Fatal("Valid")
	}
}
