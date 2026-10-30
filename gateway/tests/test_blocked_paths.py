"""Blocked paths inside allowed shares (Family/Passwords): single-share roots honour
a later block, and a site's service accounts are refused."""
import re
import unicodedata

from loft_gateway.rootfs import is_path_blocked, path_key

from conftest import all_shares, child, login, upload


def kids(env, h, iid):
    return [i["name"] for i in env.public.get(f"/v1/items/{iid}/children", headers=h).json()["items"]]


def family(env):
    h, root = all_shares(env)
    bs = child(env.public, h, root["root_item_id"], "Family")
    assert bs, "Family must be offered to an account that can open it"
    return h, root, bs


def nothing_new_under_passwords(env):
    got = sorted(p.relative_to(env.nas).as_posix()
                 for p in (env.nas / "Family").rglob("*") if "passwords" in p.as_posix().lower()
                 or p.name.casefold().startswith("passwords"))
    assert got == ["Family/Passwords", "Family/Passwords/vault",
                   "Family/Passwords/vault/master.kdbx"], got


def test_path_key_normalises_case_nfd_and_dots():
    nfd = unicodedata.normalize("NFD", "Family/Bjørn/Ære")
    assert path_key(nfd) == path_key("FAMILY/bjørn/ære/")
    for p in ("Passwords", "PASSWORDS/vault", "notes/../Passwords/x",
              "./passwords", "Passwords/"):
        assert is_path_blocked("family", p, ["Family/Passwords"]), p
    for p in ("", "notes", "Passwords_old", "Pass", "x/Passwords"):
        assert not is_path_blocked("Family", p, ["Family/Passwords"]), p
    assert is_path_blocked("Lageret", unicodedata.normalize("NFD", "Bjørn"), ["lageret/BJØRN"])
    # either spelling of a mapped character matches, whichever the config uses
    for p in ("a: secret", "a\uf022 secret/x", "A\uf022 Secret"):
        assert is_path_blocked("Family", p, ["Family/a: secret"]), p
        assert is_path_blocked("Family", p, ["Family/a\uf022 secret"]), p
    assert not is_path_blocked("Family", "a\uf027 secret", ["Family/a: secret"])


def test_blocked_path_hidden_from_listing_and_rescan(env):
    h, root, bs = family(env)
    assert kids(env, h, bs["id"]) == ["Admin", "notes"]
    env.gw.reconcile(env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],)),
                     env.gw.backend.authenticate("carol", "pw-carol"))
    assert kids(env, h, bs["id"]) == ["Admin", "notes"]
    assert env.gw.db.one("SELECT COUNT(*) n FROM items WHERE name_key LIKE '%kdbx%'"
                         " OR name_key LIKE '%passwords%'")["n"] == 0


def test_blocked_path_cannot_be_created_or_renamed_into(env):
    h, root, bs = family(env)
    notes = child(env.public, h, bs["id"], "notes")
    for name in ("Passwords", "passwords", "PASSWORDS"):
        r = env.public.post("/v1/folders", json={"parent_id": bs["id"], "name": name}, headers=h)
        assert r.status_code == 404, (name, r.status_code, r.text)
        r = env.public.patch(f"/v1/items/{notes['id']}", json={"name": name}, headers=h)
        assert r.status_code == 404, (name, r.status_code, r.text)
        uid = upload(env.public, h, b"x", near=bs["id"])
        r = env.public.post(f"/v1/uploads/{uid}/commit", json={"parent_id": bs["id"], "name": name},
                            headers=h)
        assert r.status_code == 404, (name, r.status_code, r.text)
    assert (env.nas / "Family" / "notes").is_dir()
    nothing_new_under_passwords(env)


def test_folder_picker_and_single_share_root_refuse_blocked_path(env):
    env.gw.s.shares = []
    h = login(env, "carol")
    r = env.public.get("/v1/roots", params={"share": "Family", "path": ""}, headers=h)
    assert r.status_code == 200 and r.json()["folders"] == ["Admin", "notes"], r.text
    for p in ("Passwords", "passwords/vault", "Admin/router-backups",
              "ADMIN/wifi-settings", "Admin/Medical-Records/x",
              "Admin/papers/private", "Admin/PAPERS/Private/parts"):
        assert env.public.get("/v1/roots", params={"share": "Family", "path": p},
                              headers=h).status_code == 404
        r = env.public.put("/v1/me/root", json={"share": "Family", "path": p}, headers=h)
        assert r.status_code == 404, r.text
    # a whole-share root on Family still hides it
    r = env.public.put("/v1/me/root", json={"share": "Family", "path": ""}, headers=h)
    assert r.status_code == 200, r.text
    assert kids(env, h, r.json()["root"]["root_item_id"]) == ["Admin", "notes"]


def test_single_share_root_survives_later_block(env):
    # A root saved before its share is blocked stops working.
    env.gw.s.shares = []
    h = login(env, "carol")
    r = env.public.put("/v1/me/root", json={"share": "media", "path": ""}, headers=h)
    assert r.status_code == 200, r.text
    film = child(env.public, h, r.json()["root"]["root_item_id"], "Film")
    clip = child(env.public, h, film["id"], "clip.mov")
    env.gw.s.blocked_shares = env.gw.s.blocked_shares + ["media"]
    env.gw._share_cache.clear()
    r = env.public.get(f"/v1/items/{clip['id']}/content", headers=h)
    assert r.status_code == 403, r.status_code


def test_denied_accounts_refused(env):
    env.gw.s.denied_users_re = "nas-backup|replica-.*|svc"   # a site's service accounts
    for user in ("nas-backup", "replica-1234", "REPLICA-1", "svc", "SVC"):
        r = env.direct.post("/v1/auth/login", json={"user": user, "password": "x",
                                                     "device_name": "Mac"})
        assert r.status_code == 403 and r.json()["error"] == "account_not_allowed", (user, r.text)
    login(env, "carol")                       # plain accounts still work
    rows = env.gw.db.all("SELECT nas_user FROM audit WHERE event='login_refused_account'")
    assert len(rows) == 5


CLIMBS = ("../Passwords", "a/../../Passwords", "a/b/../../../Passwords",
          "a/../../Passwords/vault", "notes/../Passwords", "./Passwords",
          "..\\Passwords", "notes\\..\\..\\Passwords")


def test_dotdot_stops_at_the_share_root_like_smbclient():
    # '..' must not climb past the share name in the key: smbclient stops at the
    # share root and would open Family/Passwords.
    import ntpath
    for p in CLIMBS:
        assert is_path_blocked("Family", p, ["Family/Passwords"]), p
        nas = ntpath.normpath("\\\\nas\\Family\\" + p.replace("/", "\\")).lower()
        assert nas.startswith("\\\\nas\\family\\passwords"), nas   # the premise


def test_picker_and_root_refuse_dot_parts(env):
    env.gw.s.shares = []
    h = login(env, "carol")
    for p in CLIMBS + ("notes/..", "notes\\x"):
        r = env.public.get("/v1/roots", params={"share": "Family", "path": p}, headers=h)
        assert r.status_code == 400 and r.json()["error"] == "bad_path", (p, r.text)
        r = env.public.put("/v1/me/root", json={"share": "Family", "path": p}, headers=h)
        assert r.status_code == 400 and r.json()["error"] == "bad_path", (p, r.text)
    assert env.gw.db.conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 0


def test_saved_root_with_dot_parts_reaches_nothing():
    # A single-share root stored before '..' was refused.
    from loft_gateway.rootfs import RootFS
    from loft_gateway.storage.base import NotFound
    fs = RootFS({"share": "Family", "path": "../Passwords"}, None, [], [],
                blocked_paths=["Family/Passwords"])
    for vp in ("../Passwords", "../Passwords/vault", "notes/../x", "notes\\..\\x"):
        try:
            fs.nas(vp)
        except NotFound:
            continue
        raise AssertionError(vp)
    assert fs.nas("notes/a.txt") == ("Family", "notes/a.txt")


def test_smb_layer_refuses_dot_parts():
    from loft_gateway.storage.base import NotFound
    from loft_gateway.storage.smb import SMBSession
    s = object.__new__(SMBSession)
    s.server = "nas"
    assert s._unc("Family", "notes/a b") == "\\\\nas\\Family\\notes\\a b"
    assert s._unc("IPC$") == "\\\\nas\\IPC$"
    for share, p in [("Family", "../Passwords"), ("Family", "a/../../x"),
                     ("Family", "a\\..\\x"), ("Family", "./x"), ("..", "x"),
                     ("Family\\..", "x")]:
        try:
            s._unc(share, p)
        except NotFound:
            continue
        raise AssertionError((share, p))


def test_folder_above_a_blocked_path_is_fixed(env):
    # Admin holds Admin/router-backups: renaming it would carry router-backups out from
    # under its block, deleting or moving it would take the hidden folder along.
    h, root, bs = family(env)
    admin = child(env.public, h, bs["id"], "Admin")
    assert kids(env, h, admin["id"]) == ["papers", "receipts"]
    media = child(env.public, h, root["root_item_id"], "media")
    notes = child(env.public, h, bs["id"], "notes")
    for body in ({"name": "Admin2"}, {"parent_id": notes["id"]}, {"parent_id": media["id"]}):
        r = env.public.patch(f"/v1/items/{admin['id']}", json=body, headers=h)
        assert r.status_code == 403 and r.json()["error"] == "share_is_fixed", (body, r.text)
    r = env.public.delete(f"/v1/items/{admin['id']}", headers=h)
    assert r.status_code == 403, r.text
    assert (env.nas / "Family" / "Admin" / "router-backups" / "router.tar").read_bytes() == b"CFG"
    assert not (env.nas / "media" / "Admin").exists()
    # papers holds papers/private: listed without it, and fixed too
    docs = child(env.public, h, admin["id"], "papers")
    assert kids(env, h, docs["id"]) == ["moving.md"]
    r = env.public.patch(f"/v1/items/{docs['id']}", json={"name": "papers2"}, headers=h)
    assert r.status_code == 403, r.text
    r = env.public.delete(f"/v1/items/{docs['id']}", headers=h)
    assert r.status_code == 403, r.text
    assert (env.nas / "Family" / "Admin" / "papers" / "Private" / "accounts.md").read_bytes() == b"KEYS"
    # what is beside the blocked paths stays an ordinary folder
    mig = child(env.public, h, admin["id"], "receipts")
    r = env.public.patch(f"/v1/items/{mig['id']}", json={"name": "receipts-old"}, headers=h)
    assert r.status_code == 200, r.text
    r = env.public.delete(f"/v1/items/{mig['id']}", headers=h)
    assert r.status_code == 200, r.text


def test_holds_blocked_path():
    from loft_gateway.rootfs import holds_blocked_path
    bp = ["Family/Passwords", "Family/Admin/router-backups"]
    for p in ("Admin", "ADMIN", "x/../Admin", ""):
        assert holds_blocked_path("Family", p, bp), p
    for p in ("Admin/papers", "Admin/receipts", "Admin/router-backups", "notes", "Admin2",
              "Passwords"):
        assert not holds_blocked_path("Family", p, bp), p
    assert not holds_blocked_path("media", "", bp)


ALIASES = ("PWD7QK~1", "pwd7qk~1", "PWD7QK~1/vault", "pwd7qk~1/vault/master.kdbx")


def test_short_name_alias_cannot_reach_a_blocked_path(env):
    # Samba opens Passwords by its 8.3 alias PWD7QK~1 too, which no name-based block
    # check recognises. The fake backend resolves it the same way.
    import pytest
    from loft_gateway.storage.base import NotFound
    sess = env.gw.backend.authenticate("carol", "pw-carol")
    for p in ALIASES:
        with pytest.raises(NotFound):
            sess.stat("Family", p)
    env.gw.s.shares = []
    h = login(env, "carol")
    for p in ALIASES:
        r = env.public.get("/v1/roots", params={"share": "Family", "path": p}, headers=h)
        assert r.status_code == 404, (p, r.text)
        r = env.public.put("/v1/me/root", json={"share": "Family", "path": p}, headers=h)
        assert r.status_code == 404, (p, r.text)
    assert env.gw.db.conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 0


def test_short_name_alias_cannot_be_written_through(env):
    h, root, bs = family(env)
    notes = child(env.public, h, bs["id"], "notes")
    for name in ("PWD7QK~1", "pwd7qk~1"):
        r = env.public.post("/v1/folders", json={"parent_id": bs["id"], "name": name}, headers=h)
        assert r.status_code == 404, (name, r.text)
        r = env.public.patch(f"/v1/items/{notes['id']}", json={"name": name}, headers=h)
        assert r.status_code == 404, (name, r.text)
        uid = upload(env.public, h, b"x", near=bs["id"])
        r = env.public.post(f"/v1/uploads/{uid}/commit", json={"parent_id": bs["id"], "name": name},
                            headers=h)
        assert r.status_code == 404, (name, r.text)
    assert (env.nas / "Family" / "notes").is_dir()
    nothing_new_under_passwords(env)


def test_saved_root_through_an_alias_reaches_nothing(env):
    import pytest
    from loft_gateway.rootfs import RootFS
    from loft_gateway.storage.base import NotFound
    sess = env.gw.backend.authenticate("carol", "pw-carol")
    fs = RootFS({"share": "Family", "path": "PWD7QK~1"}, sess, [], ["Family"],
                blocked_paths=env.gw.s.blocked_paths)
    for vp in ("PWD7QK~1", "pwd7qk~1/vault"):        # RootFS paths are share-relative
        with pytest.raises(NotFound):
            fs.scandir(vp)
        with pytest.raises(NotFound):
            fs.stat(vp)
    with pytest.raises(NotFound):          # an alias of an ordinary folder is refused too
        sess.scandir("Family", "NOTESO~1")


def test_real_names_with_a_tilde_still_work(env):
    # Editors write names like draft~1.txt and report.txt~: only aliases go.
    h, root, bs = family(env)
    notes = child(env.public, h, bs["id"], "notes")
    for name in ("draft~1.txt", "report.txt~", "PWD7QK~2"):
        uid = upload(env.public, h, b"t", near=notes["id"])
        r = env.public.post(f"/v1/uploads/{uid}/commit", json={"parent_id": notes["id"], "name": name},
                            headers=h)
        assert r.status_code in (200, 201), (name, r.text)
        assert (env.nas / "Family" / "notes" / name).read_bytes() == b"t"
    r = env.public.post("/v1/folders", json={"parent_id": notes["id"], "name": "old~stuff"}, headers=h)
    assert r.status_code in (200, 201), r.text
    assert sorted(kids(env, h, notes["id"])) == ["PWD7QK~2", "draft~1.txt", "old~stuff", "report.txt~"]


def test_smb_unc_refuses_aliases(monkeypatch):
    # The same guard on the real backend: a fake listdir that answers like Samba
    # (a search pattern matches long and short names, returning the long one).
    import pytest
    import smbclient
    from loft_gateway.storage.base import NotFound
    from loft_gateway.storage.smb import SMBSession
    dirs = {"\\\\nas\\Family": {"Passwords": "PWD7QK~1", "notes": "NOTES",
                                        "real~x": "REAL~X"},
            "\\\\nas\\Family\\notes": {}}

    def listdir(path, search_pattern="*", **kw):
        if search_pattern == "boom~1":
            raise OSError("lookup failed")
        return [n for n, alt in dirs.get(path, {}).items()
                if search_pattern.casefold() in (n.casefold(), alt.casefold())]
    monkeypatch.setattr(smbclient, "listdir", listdir)
    s = SMBSession.__new__(SMBSession)
    s.server, s._kw = "nas", {}
    for p in ALIASES + ("notes/../PWD7QK~1", "boom~1", "notes/boom~1"):
        with pytest.raises(NotFound):
            s._unc("Family", p)
    assert s._unc("Family", "real~x") == "\\\\nas\\Family\\real~x"
    assert s._unc("Family", "REAL~X/f") == "\\\\nas\\Family\\REAL~X\\f"
    assert s._unc("Family", "notes/new~1.txt") == "\\\\nas\\Family\\notes\\new~1.txt"
    assert s._unc("Family", "notes") == "\\\\nas\\Family\\notes"
