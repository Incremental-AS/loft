"""Pairing code (docs/pairing.md): carries address + pins, round-trips, and its pin is the
same SPKI hash the apps check (openssl computes it the same way)."""
import base64
import subprocess

import pytest

from loft_gateway import admin, config


def _cert(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-keyout", key, "-out", crt, "-subj", "/CN=loft", "-days", "1"],
                   check=True, capture_output=True)
    der = subprocess.run(["openssl", "x509", "-in", crt, "-pubkey", "-noout"], check=True,
                         capture_output=True).stdout
    der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "der"], input=der, check=True,
                         capture_output=True).stdout
    want = base64.b64encode(subprocess.run(["openssl", "dgst", "-sha256", "-binary"], input=der,
                                           check=True, capture_output=True).stdout).decode()
    return str(crt), want


def test_pin_matches_openssl(tmp_path):
    crt, want = _cert(tmp_path)
    assert admin.spki_pin(crt) == want


def test_payload_round_trip(tmp_path):
    crt, pin = _cert(tmp_path)
    s = config.Settings(gateway_name="Home", lan_url="https://10.0.0.5:8443/",
                        direct_spki_pins=[pin, "standby="])
    s.direct.certfile = crt
    p = admin.pairing_payload(s)
    assert p == {"v": 1, "name": "Home", "lan": "https://10.0.0.5:8443", "pins": [pin, "standby="]}
    link = admin.pairing_link(s)
    assert link.startswith("https://loft.incremental.no/pair#") and "=" not in link.split("#")[1]
    assert admin.decode_payload(link) == p
    # "Set up manually" in the apps: the same address and fingerprints, labelled as the apps label them
    m = admin.manual_text(p)
    assert "Address          10.0.0.5:8443" in m and f"Key fingerprint  {pin}" in m
    assert "(standby)        standby=" in m and "https://" not in m


def test_payload_public_route(tmp_path):
    crt, pin = _cert(tmp_path)
    s = config.Settings(lan_url="https://10.0.0.5:8443", public_url="https://files.example.org",
                        public_mode="direct")
    s.direct.certfile = crt
    p = admin.pairing_payload(s)
    assert p["pub"] == "https://files.example.org" and p["mode"] == "direct" and p["pins"] == [pin]


def test_payload_needs_lan_url_and_pins():
    with pytest.raises(SystemExit):
        admin.pairing_payload(config.Settings())
    s = config.Settings(lan_url="https://10.0.0.5:8443")
    s.direct.certfile = None
    with pytest.raises(SystemExit):
        admin.pairing_payload(s)


def test_config_validation(tmp_path):
    f = tmp_path / "g.toml"
    for bad in ('smb_encryption = "preferred"\n', 'public_mode = "tunnel"\n',
                'public_mode = "vpn"\n', '[public]\nhost = "127.0.0.1"\nlan_url = "https://x"\n'):
        f.write_text(bad)
        with pytest.raises(ValueError):
            config.load(str(f))
    f.write_text('smb_server = "nas.local"\nsmb_encryption = "server"\npublic_mode = "direct"\n'
                 'public_url = "https://files.example.org"\n')
    s = config.load(str(f))
    assert (s.smb_server, s.smb_encryption, s.public_mode) == ("nas.local", "server", "direct")


def test_effective_config_lists_defaults(tmp_path, capsys):
    f = tmp_path / "g.toml"
    f.write_text('smb_server = "nas.local"\nblocked_shares = ["backup"]\n')
    admin.main(["--config", str(f), "config", "--effective"])
    import json
    d = json.loads(capsys.readouterr().out)
    assert d["smb_server"] == "nas.local" and d["blocked_shares"] == ["backup"]
    assert d["smb_encryption"] == "required" and d["denied_users_re"] == "guest" and "port" in d["direct"]
