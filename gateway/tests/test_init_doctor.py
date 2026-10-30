"""`loft-gateway init` / `doctor` / `pair` and the CLI dispatch (docs/design.md §B.5)."""
import datetime as dt
import json
import socket
from pathlib import Path

import pytest
from cryptography import x509

from loft_gateway import admin, config, doctor, initcmd, main as gwmain
from loft_gateway.storage.local import LocalBackend


def _init(tmp_path, *extra):
    cfg = tmp_path / "etc" / "gateway.toml"
    rc = initcmd.main(["-y", "--config", str(cfg), "--smb-server", "nas.example.org",
                       "--lan-url", "https://10.0.0.5:8443", "--state-dir",
                       str(tmp_path / "state"), "--master-key", "file", "--no-qr", *extra])
    assert rc == 0
    return cfg


def test_init_writes_a_working_setup(tmp_path, capsys):
    cfg = _init(tmp_path)
    s = config.load(str(cfg))
    assert s.smb_server == "nas.example.org" and s.smb_encryption == "required"
    assert s.lan_url == "https://10.0.0.5:8443" and s.public_mode == "none"
    assert s.db_path == str(tmp_path / "state" / "index.db")
    live = admin.spki_pin(s.direct.certfile)
    standby = admin.spki_pin(str(Path(s.direct.certfile).parent / "standby" / "direct.crt"))
    assert s.direct_spki_pins == [live, standby] and live != standby
    for f in (s.direct.keyfile, s.master_key_path,
              str(Path(s.direct.keyfile).parent / "standby" / "direct.key")):
        assert Path(f).stat().st_mode & 0o077 == 0, f
    from loft_gateway import crypto
    assert len(crypto.load_master_key(s.master_key_path)) == 32
    cert = x509.load_pem_x509_certificate(Path(s.direct.certfile).read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "10.0.0.5" in {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    out = capsys.readouterr().out
    link = next(ln for ln in out.splitlines() if ln.startswith("https://loft.incremental.no/pair#"))
    assert admin.decode_payload(link)["pins"] == [live, standby]
    # The master key never reaches the terminal.
    assert Path(s.master_key_path).read_text().strip() not in out


def test_init_refuses_to_overwrite(tmp_path):
    cfg = _init(tmp_path)
    before = cfg.read_text()
    with pytest.raises(SystemExit):
        _init(tmp_path)
    assert cfg.read_text() == before
    _init(tmp_path, "--force", "--smb-encryption", "server")
    assert config.load(str(cfg)).smb_encryption == "server"


def test_init_direct_mode(tmp_path):
    cfg = _init(tmp_path, "--public-mode", "direct", "--public-url", "https://files.example.org:8443",
                "--name", "Home NAS")
    s = config.load(str(cfg))
    assert s.public_mode == "direct" and s.gateway_name == "Home NAS"
    cert = x509.load_pem_x509_certificate(Path(s.direct.certfile).read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "files.example.org" in san.get_values_for_type(x509.DNSName)
    p = admin.pairing_payload(s)
    assert p["pub"] == "https://files.example.org:8443" and p["mode"] == "direct"


def test_init_rejects_bad_urls(tmp_path):
    with pytest.raises(SystemExit):
        initcmd.main(["-y", "--config", str(tmp_path / "g.toml"), "--smb-server", "nas",
                      "--lan-url", "http://10.0.0.5:8443", "--master-key", "file"])
    with pytest.raises(SystemExit):     # non-interactive and no SMB server
        initcmd.main(["-y", "--config", str(tmp_path / "g.toml"), "--lan-url",
                      "https://10.0.0.5:8443", "--master-key", "file"])


def test_init_interactive(tmp_path, monkeypatch, capsys):
    answers = iter(["nas.home.arpa", "", "Garage", "https://10.1.1.2:8443", "tunnel",
                    "https://loft.example.org"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    cfg = tmp_path / "gateway.toml"
    assert initcmd.main(["--config", str(cfg), "--master-key", "file", "--no-qr",
                         "--state-dir", str(tmp_path)]) == 0
    s = config.load(str(cfg))
    assert (s.smb_server, s.smb_encryption, s.gateway_name, s.lan_url, s.public_mode,
            s.public_url) == ("nas.home.arpa", "required", "Garage", "https://10.1.1.2:8443",
                              "tunnel", "https://loft.example.org")


def test_cli_dispatch(tmp_path, monkeypatch, capsys):
    cfg = _init(tmp_path)
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        gwmain.main(["pair", "--config", str(cfg), "--no-qr"])
    assert e.value.code in (0, None)
    assert "https://loft.incremental.no/pair#" in capsys.readouterr().out
    seen = []
    monkeypatch.setattr(gwmain, "build", lambda path: seen.append(path))
    monkeypatch.setattr(gwmain, "serve", lambda gw: None)
    monkeypatch.setattr(gwmain.asyncio, "run", lambda coro: None)
    monkeypatch.setattr(gwmain.threading, "Thread", lambda **k: type("T", (), {"start": lambda s: None})())
    gwmain.main(["--config", "/x/gateway.toml"])          # the systemd unit's form
    gwmain.main(["serve", "--config", "/y.toml"])
    gwmain.main([])
    assert seen == ["/x/gateway.toml", "/y.toml", None]
    with pytest.raises(SystemExit):
        gwmain.main(["frobnicate"])


# ── doctor ────────────────────────────────────────────────────────────────

def _run(cfg, capsys, *extra):
    rc = doctor.main(["--config", str(cfg), "--json", "--timeout", "1", *extra])
    out = json.loads(capsys.readouterr().out)
    return rc, {c["check"]: c for c in out["checks"]}


@pytest.fixture
def smb_up(monkeypatch):
    """A reachable SMB server whose NEGOTIATE says 3.1.1 + encryption, clock in step."""
    state = {"dialect": 0x0311, "enc": True, "skew": 0}

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _Sock())
    monkeypatch.setattr(doctor, "negotiate", lambda host, timeout=10: (
        state["dialect"], state["enc"],
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=state["skew"])))
    return state


def test_doctor_all_good(tmp_path, capsys, smb_up):
    cfg = _init(tmp_path, "--thumb-socket", "")
    capsys.readouterr()
    rc, c = _run(cfg, capsys)
    assert c["smb dialect"]["status"] == "ok" and "3.1.1" in c["smb dialect"]["detail"]
    assert c["smb encryption"]["status"] == "ok" and c["clock"]["status"] == "ok"
    assert c["pins"]["status"] == "ok" and c["tls key"]["status"] == "ok"
    assert c["shares"]["status"] == "skip"
    assert rc == 0


def test_doctor_missing_config(tmp_path, capsys):
    rc, c = _run(tmp_path / "nope.toml", capsys)
    assert rc == 1 and c["config"]["status"] == "FAIL"


def test_doctor_unreachable_smb(tmp_path, capsys, monkeypatch):
    cfg = _init(tmp_path, "--thumb-socket", "")
    capsys.readouterr()

    def refuse(*a, **k):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(socket, "create_connection", refuse)
    rc, c = _run(cfg, capsys)
    assert rc == 1 and c["smb reachable"]["status"] == "FAIL"


def test_doctor_encryption_and_dialect(tmp_path, capsys, smb_up):
    cfg = _init(tmp_path, "--thumb-socket", "")
    capsys.readouterr()
    smb_up.update(dialect=0x0210, enc=False)
    rc, c = _run(cfg, capsys)
    assert rc == 1 and c["smb dialect"]["status"] == "FAIL"
    assert c["smb encryption"]["status"] == "FAIL"
    _init(tmp_path, "--force", "--thumb-socket", "", "--smb-encryption", "server")
    capsys.readouterr()
    rc, c = _run(cfg, capsys)
    assert rc == 2 and c["smb dialect"]["status"] == "warn"
    assert c["smb encryption"]["status"] == "warn"


def test_doctor_clock_skew(tmp_path, capsys, smb_up):
    cfg = _init(tmp_path, "--thumb-socket", "")
    capsys.readouterr()
    smb_up["skew"] = 90
    assert _run(cfg, capsys)[1]["clock"]["status"] == "warn"
    smb_up["skew"] = -600
    rc, c = _run(cfg, capsys)
    assert rc == 1 and c["clock"]["status"] == "FAIL" and c["clock"]["detail"].startswith("-600 s")


def test_doctor_cert_problems(tmp_path, capsys, smb_up):
    cfg = _init(tmp_path, "--thumb-socket", "")
    capsys.readouterr()
    s = config.load(str(cfg))
    # The pin of the live cert dropped from the list.
    text = cfg.read_text().replace(f'"{s.direct_spki_pins[0]}", ', "")
    cfg.write_text(text)
    rc, c = _run(cfg, capsys)
    assert rc == 1 and c["pins"]["status"] == "FAIL"
    # Key and cert from different pairs.
    _init(tmp_path, "--force", "--thumb-socket", "")
    capsys.readouterr()
    s = config.load(str(cfg))
    sb = Path(s.direct.keyfile).parent / "standby" / "direct.key"
    Path(s.direct.keyfile).write_bytes(sb.read_bytes())
    rc, c = _run(cfg, capsys)
    assert rc == 1 and c["tls key"]["status"] == "FAIL"


def test_doctor_world_readable_master_key(tmp_path, capsys, smb_up):
    cfg = _init(tmp_path, "--thumb-socket", "")
    capsys.readouterr()
    Path(config.load(str(cfg)).master_key_path).chmod(0o644)
    rc, c = _run(cfg, capsys)
    assert rc == 1 and c["master key"]["status"] == "FAIL"


def test_doctor_signs_in_and_lists_shares(tmp_path, capsys, smb_up, monkeypatch):
    cfg = _init(tmp_path, "--thumb-socket", "")
    capsys.readouterr()
    nas = tmp_path / "nas"
    for sh in ("home", "media", "backup"):
        (nas / sh).mkdir(parents=True)
    pw = "-".join(("doctor", "test", "pw"))      # built, so no scanner mistakes it for a secret
    local = LocalBackend(nas, {"alice": {"password": pw, "shares": ["home", "media", "backup"]}})
    from loft_gateway.storage import smb
    monkeypatch.setattr(smb.SMBBackend, "authenticate",
                        lambda self, u, p: local.authenticate(u, p))
    text = cfg.read_text().replace("blocked_shares = []", 'blocked_shares = ["backup"]')
    cfg.write_text(text)
    monkeypatch.setenv("LOFT_DOCTOR_PASSWORD", pw)
    rc, c = _run(cfg, capsys, "--user", "alice")
    assert c["smb sign-in"]["status"] == "ok"
    assert c["shares"]["status"] == "ok" and "home, media" in c["shares"]["detail"]
    assert "backup" not in c["shares"]["detail"].split(":")[-1]
    assert pw not in json.dumps(c)
    monkeypatch.setenv("LOFT_DOCTOR_PASSWORD", "wrong")
    rc, c = _run(cfg, capsys, "--user", "alice")
    assert rc == 1 and c["smb sign-in"]["status"] == "FAIL"
