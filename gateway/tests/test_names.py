"""Names with characters SMB can't carry ('|', ':', '?' ...): on a Samba NAS they are
Private Use code points (the SFM mapping a Mac's own SMB client uses), on the Mac
the real characters. The local backend stores them on disk the way Samba does."""
import loft_gateway.storage.local as local
from loft_gateway.service import valid_name
from loft_gateway.storage.base import from_nas, to_nas

from conftest import child, rooted, upload

ON_NAS = "Anmeldelse  Revyen 2025 Ingen bitter pille.pdf"
ON_MAC = "Anmeldelse | Revyen 2025: Ingen bitter pille.pdf"


def kids(env, h, iid):
    return [i["name"] for i in env.public.get(f"/v1/items/{iid}/children", headers=h).json()["items"]]


def test_mapping_round_trips_and_leaves_the_rest():
    assert from_nas(ON_NAS) == ON_MAC and to_nas(ON_MAC) == ON_NAS
    allc = "".join(chr(c) for c in range(0xF020, 0xF028))
    assert from_nas(allc) == '"*:<>?|'        # '\\' would split the path: left alone
    assert to_nas("Ærlig/øl å") == "Ærlig/øl å" and from_nas("x") == "x"


def test_nas_name_reads_with_real_characters(env):
    (env.nas / "home" / "alice" / "Docs" / ON_NAS).write_bytes(b"PDF")
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    assert kids(env, h, docs["id"]) == ["a.txt", ON_MAC]
    it = child(env.public, h, docs["id"], ON_MAC)
    assert env.public.get(f"/v1/items/{it['id']}/content", headers=h).content == b"PDF"


def test_mac_names_are_stored_the_samba_way(env):
    h, root = rooted(env)
    rid = root["root_item_id"]
    f = env.public.post("/v1/folders", json={"parent_id": rid, "name": "Q3: plan?"},
                        headers=h).json()
    assert (env.nas / "home/alice/Q3 plan").is_dir()
    uid = upload(env.public, h, b"hello")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"parent_id": f["id"], "name": "a|b.txt"}, headers=h)
    assert r.status_code == 200, r.text
    assert (env.nas / "home/alice/Q3 plan/ab.txt").read_bytes() == b"hello"
    it = child(env.public, h, f["id"], "a|b.txt")
    r = env.public.patch(f"/v1/items/{it['id']}", json={"name": '"x" <y>*.txt',
                                                        "parent_id": rid}, headers=h)
    assert r.status_code == 200, r.text
    assert (env.nas / "home/alice/x y.txt").read_bytes() == b"hello"
    assert kids(env, h, rid) == ['"x" <y>*.txt', "Docs", "Q3: plan?"]
    assert kids(env, h, f["id"]) == []


def test_item_indexed_under_the_raw_name_keeps_its_id(env, monkeypatch):
    # Rows written before the mapping hold the Private Use name: the next listing
    # renames the row in place (same NAS file id), it is not a delete + create.
    (env.nas / "home" / "alice" / "Docs" / ON_NAS).write_bytes(b"PDF")
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    monkeypatch.setattr(local, "from_nas", lambda n: n)
    old = child(env.public, h, docs["id"], ON_NAS)
    monkeypatch.undo()
    anchor = env.public.get("/v1/changes", params={"anchor": root["anchor"]}, headers=h).json()["anchor"]
    assert kids(env, h, docs["id"]) == ["a.txt", ON_MAC]
    new = child(env.public, h, docs["id"], ON_MAC)
    assert new["id"] == old["id"]
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    assert [i["name"] for i in r["updated"]] == [ON_MAC] and not r.get("deleted")


def test_private_use_names_from_the_mac_are_refused(env):
    assert valid_name("a|b") and valid_name("a:b") and not valid_name("ab")
    h, root = rooted(env)
    r = env.public.post("/v1/folders", json={"parent_id": root["root_item_id"], "name": "xy"},
                        headers=h)
    assert r.status_code in (400, 422), r.text
