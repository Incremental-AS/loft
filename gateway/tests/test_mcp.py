"""Assistants (MCP): sign-in (oauth.py), folder access (grants), and every tool against the local NAS."""
import base64
import hashlib
import os
import io
import re
import sqlite3
import time
import zipfile
from urllib.parse import parse_qs, urlparse

import pytest

from loft_gateway import mcp
from loft_gateway.db import DB, SCHEMA_VERSION
from loft_gateway.service import MCP_LIVE_ACCESS, ApiError

from conftest import login, rooted

A = "home/alice"


def connect(env, user="alice", working=A, read=None, read_only=False, name="Jeevo"):
    """A host-made connection (no expiry) with general grants."""
    out = env.gw.create_mcp_connection(user, f"pw-{user}", name, read_only)
    h = {"Authorization": f"Bearer {out['token']}"}
    want = {}
    if working is not None:
        want["working_folder"] = working
    if read is not None:
        want["read"] = read
    if want:
        res = rpc(env.public, h, "loft/access/set", want)
        assert "result" in res, res
    return h, out


def rpc(client, h, method, params=None, mid=1):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": mid, "method": method,
                                  "params": params or {}}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def call(client, h, name, ctx=None, **args):
    params = {"name": name, "arguments": args}
    if ctx is not None:
        params["_meta"] = {mcp.CONTEXT_KEY: ctx}
    out = rpc(client, h, "tools/call", params)
    assert "result" in out, out
    return out["result"]


def ok(res):
    assert not res.get("isError"), res["content"][0]["text"]
    return res


def text(res):
    return res["content"][0]["text"]


ALL_TOOLS = ["list_folder", "search_files", "read_file", "download_file", "write_file",
             "upload_start", "upload_chunk", "upload_finish", "create_folder", "move", "delete"]


# ── protocol and auth ─────────────────────────────────────────────────────

def test_initialize_and_tools(env):
    h, out = connect(env)
    init = rpc(env.public, h, "initialize", {"protocolVersion": "2025-06-18",
                                             "capabilities": {}, "clientInfo": {"name": "t"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "loft"
    assert "list_folder" in init["result"]["instructions"]
    names = [t["name"] for t in rpc(env.public, h, "tools/list")["result"]["tools"]]
    assert names == ALL_TOOLS
    assert rpc(env.public, h, "initialize", {"protocolVersion": "1999-01-01"})[
        "result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                        headers=h)
    assert r.status_code == 202 and not r.content
    assert rpc(env.public, h, "nope")["error"]["code"] == -32601
    assert out["expires_at"] > 2e11                       # host-made: doesn't expire


def test_tool_list_never_changes_with_access(env):
    """Jeevo pins the tool list's hash: access changes must not change it."""
    lists = []
    for working, read, ro in [(None, None, False), (A, None, False), (None, [""], True),
                              (f"{A}/Docs", [A], False)]:
        h, _ = connect(env, working=working, read=read, read_only=ro)
        lists.append(rpc(env.public, h, "tools/list")["result"])
    assert all(x == lists[0] for x in lists)


def test_read_only_connection_refuses_writes_at_call_time(env):
    h, _ = connect(env, working=None, read=[A], read_only=True)
    res = call(env.public, h, "write_file", path=f"{A}/x.txt", text="x")
    assert res["isError"] and "read only" in text(res)
    assert not (env.nas / "home/alice/x.txt").exists()
    err = rpc(env.public, h, "loft/access/set", {"working_folder": A})["error"]
    assert "read only" in err["message"]


def test_tool_annotations_are_honest(env):
    h, _ = connect(env)
    tools = {t["name"]: t for t in rpc(env.public, h, "tools/list")["result"]["tools"]}
    for n in ALL_TOOLS[:4]:
        assert tools[n]["annotations"]["readOnlyHint"] is True
    for n in ALL_TOOLS[4:]:
        assert tools[n]["annotations"]["readOnlyHint"] is False
    assert tools["delete"]["annotations"]["destructiveHint"] is True


def test_walls_between_mcp_and_app_tokens(env):
    h, _ = connect(env)
    app = login(env)
    assert env.public.get("/v1/me", headers=h).json()["error"] == "token_not_for_this_api"
    assert env.public.post("/v1/auth/refresh", headers=h).status_code == 403
    assert env.direct.post("/v1/auth/codes", json={}, headers=h).status_code == 403
    assert env.direct.post("/v1/auth/assistant-codes", headers=h).status_code == 403
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers=app)
    assert r.status_code == 403 and r.json()["error"] == "token_not_for_this_api"
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Authorization": "Bearer nst_x.y"})
    assert r.status_code == 401
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={**h, "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert env.public.get("/mcp", headers=h).status_code == 405
    r = env.direct.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={**h, "CF-Connecting-IP": "1.2.3.4"})
    assert r.status_code == 403
    assert rpc(env.direct, h, "ping")["result"] == {}


def test_revoked_connection_stops_at_once(env):
    h, out = connect(env)
    app = login(env)
    devs = env.public.get("/v1/auth/devices", headers=app).json()["devices"]
    mine = next(d for d in devs if d["id"] == out["device_id"])
    assert mine["kind"] == "mcp" and mine["device_name"] == "Jeevo"
    assert env.public.delete(f"/v1/auth/device/{out['device_id']}", headers=app).status_code == 200
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=h)
    assert r.status_code == 401 and r.json()["error"] == "device_revoked"


def test_wrong_password_makes_nothing(env):
    with pytest.raises(ApiError) as e:
        env.gw.create_mcp_connection("alice", "wrong", "J")
    assert e.value.code == "invalid_credentials"
    assert env.gw.db.all("SELECT * FROM devices WHERE kind='mcp' AND revoked_at IS NULL") == []


def test_bad_body(env):
    h, _ = connect(env)
    r = env.public.post("/mcp", content=b"{not json", headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    r = env.public.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=h)
    assert r.json()["error"]["code"] == -32600
    r = env.public.post("/mcp", content=b"x" * 10, headers={**h, "Content-Length": "99999999999"})
    assert r.status_code == 413


# ── access: working folder, read grants, contexts ─────────────────────────

def test_nothing_opens_until_folders_are_chosen(env):
    h, _ = connect(env, working=None)
    for name, args in [("list_folder", {}), ("read_file", {"path": f"{A}/Docs/a.txt"}),
                       ("search_files", {"query": "a"})]:
        res = call(env.public, h, name, **args)
        assert res["isError"] and "Nothing in Loft is shared" in text(res), name
    res = call(env.public, h, "write_file", path=f"{A}/x.md", text="x")
    assert res["isError"] and "No working folder" in text(res)


def test_working_folder_and_read_grants(env):
    env.gw.s.shares = []
    h, _ = connect(env, user="carol", working="Family/notes", read=["media/Film"])
    # the top shows only the way to what is granted
    res = ok(call(env.public, h, "list_folder"))
    assert {e["path"] for e in res["structuredContent"]["items"]} == {"Family", "media"}
    assert "Working folder (read and save): Family/notes/" in text(res)
    assert "Also readable: media/Film/" in text(res)
    res = ok(call(env.public, h, "list_folder", path="Family"))
    assert [e["path"] for e in res["structuredContent"]["items"]] == ["Family/notes"]
    # read in a read grant, not write
    res = ok(call(env.public, h, "download_file", path="media/Film/clip.mov"))
    assert base64.b64decode(res["content"][1]["resource"]["blob"]) == b"0123456789"
    res = call(env.public, h, "write_file", path="media/Film/x.txt", text="x")
    assert res["isError"] and "Family/notes/" in text(res)
    assert not (env.nas / "media/Film/x.txt").exists()
    # nothing outside the grants
    for p in ("Family/Admin/papers/moving.md", "home/bob/x", "Family/Passwords/vault/master.kdbx"):
        res = call(env.public, h, "read_file", path=p)
        assert res["isError"] and "isn't shared" in text(res), p
    # save in the working folder; paths in answers start at the top of the drive
    res = ok(call(env.public, h, "write_file", path="Family/notes/Meetings/standup.md", text="hi"))
    assert res["structuredContent"]["path"] == "Family/notes/Meetings/standup.md"
    assert (env.nas / "Family/notes/Meetings/standup.md").read_text() == "hi"
    # the working folder itself can't be replaced, moved or deleted
    assert call(env.public, h, "delete", path="Family/notes")["isError"]
    assert call(env.public, h, "move", path="Family/notes", new_path="Family/x")["isError"]
    # moves stay inside the working folder
    res = call(env.public, h, "move", path="Family/notes/Meetings/standup.md",
               new_path="media/Film/standup.md")
    assert res["isError"]
    # search covers every grant
    res = ok(call(env.public, h, "search_files", query="clip"))
    assert [e["path"] for e in res["structuredContent"]["items"]] == ["media/Film/clip.mov"]


def test_contexts(env):
    (env.nas / "home/alice/Jeevo").mkdir()
    h, _ = connect(env, working=f"{A}/Jeevo", read=[])
    (env.nas / "home/alice/Projects/Apollo").mkdir(parents=True)
    (env.nas / "home/alice/Projects/Apollo/plan.md").write_text("launch")
    res = rpc(env.public, h, "loft/access/set", {"context": "thread-42",
                                                 "read": [f"{A}/Projects/Apollo"],
                                                 "working_folder": f"{A}/Projects/Apollo"})
    doc = res["result"]
    assert doc["general"] == {"working_folder": f"{A}/Jeevo", "read": []}
    assert doc["contexts"]["thread-42"]["read"] == [f"{A}/Projects/Apollo"]
    assert doc["effective"]["working_folder"] == f"{A}/Projects/Apollo"
    # without the context, Apollo is closed
    res = call(env.public, h, "read_file", path=f"{A}/Projects/Apollo/plan.md")
    assert res["isError"]
    res = call(env.public, h, "read_file", path=f"{A}/Projects/Apollo/plan.md", ctx="other")
    assert res["isError"]
    # with it, readable, and saves go to the context's working folder
    assert "launch" in text(ok(call(env.public, h, "read_file", ctx="thread-42",
                                    path=f"{A}/Projects/Apollo/plan.md")))
    ok(call(env.public, h, "write_file", ctx="thread-42", path=f"{A}/Projects/Apollo/notes.md",
            text="n"))
    res = call(env.public, h, "write_file", ctx="thread-42", path=f"{A}/Jeevo/x.md", text="n")
    assert res["isError"]                                # the context's folder wins
    ok(call(env.public, h, "write_file", path=f"{A}/Jeevo/x.md", text="n"))
    # clearing the context
    rpc(env.public, h, "loft/access/set", {"context": "thread-42", "read": [],
                                           "working_folder": None})
    assert rpc(env.public, h, "loft/access/get")["result"]["contexts"] == {}
    bad = rpc(env.public, h, "tools/call", {"name": "list_folder", "arguments": {},
                                            "_meta": {mcp.CONTEXT_KEY: 5}})
    assert bad["error"]["code"] == -32602


def test_whole_drive_read_keeps_blocks(env):
    env.gw.s.shares = []
    h, _ = connect(env, user="carol", working="Family/notes", read=[""])
    shares = {e["name"] for e in ok(call(env.public, h, "list_folder"))["structuredContent"]["items"]}
    assert "backup" not in shares and {"home", "media", "Family"} <= shares
    for p in ("Family/Passwords/vault/master.kdbx", "Family/Admin/papers/Private/accounts.md",
              "backup/dumps", "Family/PWD7QK~1/vault/master.kdbx"):
        assert call(env.public, h, "read_file", path=p)["isError"], p
    res = call(env.public, h, "search_files", query="master")
    assert res["structuredContent"]["items"] == []
    assert call(env.public, h, "write_file", path="Family/Passwords/x.txt", text="x")["isError"]
    assert not (env.nas / "Family/Passwords/x.txt").exists()


def test_access_methods_validate(env):
    env.gw.s.shares = []
    h, _ = connect(env, user="carol", working=None)
    shares = {f["name"] for f in rpc(env.public, h, "loft/folders")["result"]["folders"]}
    assert "backup" not in shares and "Family" in shares
    sub = rpc(env.public, h, "loft/folders", {"path": "Family/Admin"})["result"]["folders"]
    assert {f["path"] for f in sub} == {"Family/Admin/papers", "Family/Admin/receipts"}
    for bad in ("Family/Passwords", "backup", "Family/PWD7QK~1", "Family/../home", "nope",
                "Family/notes/../Passwords"):
        res = rpc(env.public, h, "loft/access/set", {"read": [bad]})
        assert "error" in res, bad
    res = rpc(env.public, h, "loft/access/set", {"working_folder": "Family"})
    assert "result" in res                                # a whole share is fine
    res = rpc(env.public, h, "loft/access/set", {"working_folder": ""})
    assert "error" in res                                 # the drive's top isn't a folder to save in
    res = rpc(env.public, h, "loft/access/set", {"working_folder": "Family/Jeevo", "create": True})
    assert res["result"]["general"]["working_folder"] == "Family/Jeevo"
    assert (env.nas / "Family/Jeevo").is_dir()
    res = rpc(env.public, h, "loft/access/set", {"working_folder": "Family/Passwords/J",
                                                 "create": True})
    assert "error" in res and not (env.nas / "Family/Passwords/J").exists()
    assert "error" in rpc(env.public, h, "loft/access/set", {})


# ── reading ───────────────────────────────────────────────────────────────

def test_list_and_read(env):
    h, _ = connect(env)
    res = ok(call(env.public, h, "list_folder", path=A))
    assert f"{A}/Docs/" in text(res)
    assert res["structuredContent"]["items"][0]["type"] == "folder"
    res = ok(call(env.public, h, "list_folder", path=f"{A}/Docs"))
    assert res["structuredContent"]["items"][0]["path"] == f"{A}/Docs/a.txt"
    assert text(ok(call(env.public, h, "read_file", path=f"/{A}/Docs/a.txt"))).endswith("hello")
    (env.nas / "home" / "alice" / "Docs" / "new.md").write_text("# Ny fil\nÆØÅ", encoding="utf-8")
    assert "ÆØÅ" in text(ok(call(env.public, h, "read_file", path=f"{A}/Docs/new.md")))


@pytest.mark.parametrize("bad", ["home/alice/../bob", "home/alice/Docs/../../bob/x",
                                 "home\\alice\\Docs\\a.txt", "./home/alice", "home//alice"])
def test_no_climbing_out(env, bad):
    (env.nas / "home" / "bob" / "x").write_text("bob's")
    h, _ = connect(env)
    res = call(env.public, h, "read_file", path=bad)
    assert res["isError"] and "bob's" not in text(res)


def test_read_pages_long_text(env):
    body = "".join(f"line {i:06d}\n" for i in range(5000))
    (env.nas / "home" / "alice" / "long.txt").write_text(body)
    h, _ = connect(env)
    res = ok(call(env.public, h, "read_file", path=f"{A}/long.txt"))
    sc = res["structuredContent"]
    assert sc["next_offset"] == mcp.READ_CHARS and sc["total_chars"] == len(body)
    got = text(res).split("\n\n", 1)[1].rsplit("\n[characters", 1)[0]
    while sc["next_offset"]:
        res = ok(call(env.public, h, "read_file", path=f"{A}/long.txt", offset=sc["next_offset"]))
        sc = res["structuredContent"]
        page = text(res).split("\n\n", 1)[1]
        got += page.rsplit("\n[characters", 1)[0] if sc["next_offset"] else page
    assert got == body


def test_binary_is_not_read_as_text(env):
    (env.nas / "home" / "alice" / "song.mp3").write_bytes(b"ID3\x04\x00\x00" + bytes(range(256)) * 40)
    h, _ = connect(env)
    res = call(env.public, h, "read_file", path=f"{A}/song.mp3")
    assert res["isError"] and "download_file" in text(res)


def test_download_in_pieces(env):
    data = hashlib.sha256(b"x").digest() * 5000
    (env.nas / "home" / "alice" / "blob.bin").write_bytes(data)
    h, _ = connect(env)
    got, off = b"", 0
    while True:
        res = ok(call(env.public, h, "download_file", path=f"{A}/blob.bin", offset=off,
                      length=70_000))
        assert res["content"][1]["type"] == "resource"
        assert len(text(res)) < 300
        got += base64.b64decode(res["content"][1]["resource"]["blob"])
        off += res["structuredContent"]["length"]
        if res["structuredContent"]["eof"]:
            break
    assert got == data


def test_search(env):
    h, _ = connect(env)
    ok(call(env.public, h, "write_file", path=f"{A}/Meetings/2026/Budget review.md", text="x"))
    ok(call(env.public, h, "write_file", path=f"{A}/Docs/budget.xlsx", content_base64=""))
    res = ok(call(env.public, h, "search_files", query="BUDGET"))
    paths = {e["path"] for e in res["structuredContent"]["items"]}
    assert paths == {f"{A}/Meetings/2026/Budget review.md", f"{A}/Docs/budget.xlsx"}
    res = ok(call(env.public, h, "search_files", query="budget", path=f"{A}/Docs"))
    assert [e["path"] for e in res["structuredContent"]["items"]] == [f"{A}/Docs/budget.xlsx"]


# ── document text (sandboxed worker) ─────────────────────────────────────

def _zip(files: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
NSA = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
X = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
T = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
O = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'


def _pdf(lines) -> bytes:
    ops = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({t}) Tj 0 -16 Td" for t in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return out


DOCS = {
    "memo.docx": (_zip({"word/document.xml": f'<w:document {W}><w:body>'
                        '<w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>'
                        '<w:p><w:r><w:t xml:space="preserve">Second </w:t></w:r><w:r><w:t>one</w:t></w:r>'
                        '<w:r><w:tab/><w:t>tabbed</w:t></w:r></w:p></w:body></w:document>'}),
                  ["First paragraph", "Second one\ttabbed"]),
    "deck.pptx": (_zip({"ppt/slides/slide1.xml": f'<p:sld xmlns:p="p" {NSA}><a:p><a:r><a:t>Title slide</a:t></a:r></a:p></p:sld>',
                        "ppt/slides/slide2.xml": f'<p:sld xmlns:p="p" {NSA}><a:p><a:r><a:t>Second slide</a:t></a:r></a:p></p:sld>',
                        "ppt/slides/slide10.xml": f'<p:sld xmlns:p="p" {NSA}><a:p><a:r><a:t>Tenth</a:t></a:r></a:p></p:sld>',
                        "ppt/notesSlides/notesSlide2.xml": f'<p:notes xmlns:p="p" {NSA}><a:p><a:r><a:t>Say hello</a:t></a:r></a:p><a:p><a:r><a:t>2</a:t></a:r></a:p></p:notes>'}),
                  ["--- slide 1 ---\nTitle slide", "--- slide 2 ---\nSecond slide\nNotes: Say hello",
                   "--- slide 3 ---\nTenth"]),
    "sums.xlsx": (_zip({"xl/workbook.xml": f'<workbook {X} {R}><sheets><sheet name="Budget" sheetId="1" r:id="rId1"/></sheets></workbook>',
                        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
                        "xl/sharedStrings.xml": f'<sst {X}><si><t>Item</t></si><si><t>Cost</t></si><si><t>Coffee</t></si></sst>',
                        "xl/worksheets/sheet1.xml": f'<worksheet {X}><sheetData>'
                        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><v>42</v></c></row>'
                        '</sheetData></worksheet>'}),
                  ["--- sheet Budget ---", "Item\tCost", "Coffee\t\t42"]),
    "notes.odt": (_zip({"content.xml": f'<office:document-content {O} {T}><office:body><office:text>'
                        '<text:h>Heading</text:h><text:p>One<text:s text:c="2"/>two<text:tab/>three</text:p>'
                        '</office:text></office:body></office:document-content>'}),
                  ["Heading", "One  two\tthree"]),
    "report.pdf": (_pdf(["Hello from a PDF", "Second line"]), ["Hello from a PDF", "Second line"]),
}


@pytest.mark.parametrize("name", list(DOCS))
def test_document_text(env, name):
    data, want = DOCS[name]
    (env.nas / "home" / "alice" / name).write_bytes(data)
    h, _ = connect(env)
    got = text(ok(call(env.public, h, "read_file", path=f"{A}/{name}")))
    for w in want:
        assert w in got, (w, got)


def test_damaged_document(env):
    (env.nas / "home" / "alice" / "broken.docx").write_bytes(b"not a zip at all")
    h, _ = connect(env)
    res = call(env.public, h, "read_file", path=f"{A}/broken.docx")
    assert res["isError"] and "No text could be read" in text(res)


# ── writing ───────────────────────────────────────────────────────────────

def test_write_rename_replace_and_versions(env):
    h, _ = connect(env)
    app, root = rooted(env)
    W = f"{A}/Meetings/2026-10-31 Weekly.md"
    res = ok(call(env.public, h, "write_file", path=W, text="v1 ÆØÅ"))
    assert res["structuredContent"] == {"path": W, "size": 9, "result": "created",
                                        "content_etag": "c1"}
    assert (env.nas / "home/alice/Meetings/2026-10-31 Weekly.md").read_text() == "v1 ÆØÅ"
    res = ok(call(env.public, h, "write_file", path=W, text="other"))
    assert res["structuredContent"]["path"] == f"{A}/Meetings/2026-10-31 Weekly (2).md"
    assert res["structuredContent"]["result"] == "renamed"
    assert call(env.public, h, "write_file", path=W, text="x", if_exists="fail")["isError"]
    res = ok(call(env.public, h, "write_file", path=W, text="v2", if_exists="replace"))
    assert res["structuredContent"]["result"] == "replaced"
    assert (env.nas / "home/alice/Meetings/2026-10-31 Weekly.md").read_text() == "v2"
    # the app (same folder picked) sees it, with its history
    kids = env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=app).json()["items"]
    meet = next(k for k in kids if k["name"] == "Meetings")
    files = env.public.get(f"/v1/items/{meet['id']}/children", headers=app).json()["items"]
    weekly = next(f for f in files if f["name"] == "2026-10-31 Weekly.md")
    vs = env.public.get(f"/v1/items/{weekly['id']}/versions", headers=app).json()["versions"]
    assert len(vs) == 1


def test_write_binary_and_limits(env):
    h, _ = connect(env)
    data = bytes(range(256)) * 1000
    res = ok(call(env.public, h, "write_file", path=f"{A}/Music/Jeevo/song.mp3",
                  content_base64=base64.b64encode(data).decode()))
    assert (env.nas / "home/alice/Music/Jeevo/song.mp3").read_bytes() == data
    assert res["structuredContent"]["size"] == len(data)
    for args in [dict(path=f"{A}/x.bin", content_base64="!!notbase64"),
                 dict(path=f"{A}/x.txt", text="a", content_base64="YQ=="),
                 dict(path=f"{A}/x.txt"),
                 dict(path=f"{A}/New/x.txt", text="a", create_folders=False),
                 dict(path=f"{A}/Docs/a.txt/inner", text="a"),
                 dict(path=f"{A}/Docs", text="a", if_exists="replace"),
                 dict(path=f"{A}/.loft/x", text="a"),
                 dict(path=A, text="a")]:
        assert call(env.public, h, "write_file", **args)["isError"], args


def test_large_upload(env):
    h, _ = connect(env)
    data = hashlib.sha256(b"big").digest() * 3000
    sha = hashlib.sha256(data).hexdigest()
    P = f"{A}/Decks/Big deck.pptx"
    st = ok(call(env.public, h, "upload_start", path=P, size=len(data), sha256=sha))["structuredContent"]
    uid = st["upload_id"]
    for off in range(0, len(data), 40_000):
        ok(call(env.public, h, "upload_chunk", upload_id=uid, offset=off,
                content_base64=base64.b64encode(data[off:off + 40_000]).decode()))
    res = call(env.public, h, "upload_chunk", upload_id=uid, offset=0, content_base64="YQ==")
    assert res["isError"] and str(len(data)) in text(res)
    res = ok(call(env.public, h, "upload_finish", upload_id=uid, path=P))
    assert res["structuredContent"]["path"] == P
    assert (env.nas / "home/alice/Decks/Big deck.pptx").read_bytes() == data
    st = ok(call(env.public, h, "upload_start", path=f"{A}/Decks/bad.bin", size=1,
                 sha256="0" * 64))["structuredContent"]
    ok(call(env.public, h, "upload_chunk", upload_id=st["upload_id"], offset=0, content_base64="YQ=="))
    res = call(env.public, h, "upload_finish", upload_id=st["upload_id"], path=f"{A}/Decks/bad.bin")
    assert res["isError"] and "SHA-256" in text(res)
    assert call(env.public, h, "upload_finish", upload_id="nope", path=f"{A}/x")["isError"]
    # staging lives in the working folder, not at the top of the share
    assert (env.nas / "home/alice/.loft").is_dir() and not (env.nas / "home/.loft").exists()


def test_folders_move_delete(env):
    h, _ = connect(env)
    J = f"{A}/Jeevo"
    ok(call(env.public, h, "create_folder", path=f"{J}/Music"))
    assert (env.nas / "home/alice/Jeevo/Music").is_dir()
    assert "already" in text(ok(call(env.public, h, "create_folder", path=f"{J}/Music")))
    ok(call(env.public, h, "write_file", path=f"{J}/a.md", text="a"))
    ok(call(env.public, h, "write_file", path=f"{J}/b.md", text="b"))
    res = call(env.public, h, "move", path=f"{J}/a.md", new_path=f"{J}/b.md")
    assert res["isError"] and (env.nas / "home/alice/Jeevo/b.md").read_text() == "b"
    ok(call(env.public, h, "move", path=f"{J}/a.md", new_path=f"{A}/Archive/2026/a.md"))
    assert (env.nas / "home/alice/Archive/2026/a.md").read_text() == "a"
    ok(call(env.public, h, "delete", path=f"{J}/b.md"))
    assert not (env.nas / "home/alice/Jeevo/b.md").exists()
    names = [e["name"] for e in ok(call(env.public, h, "list_folder", path=J))[
        "structuredContent"]["items"]]
    assert names == ["Music"]
    assert call(env.public, h, "delete", path="")["isError"]


# ── sign-in: OAuth with an approval code ──────────────────────────────────

BASE = "https://files.example.org"
CB = "https://api.jeevo.ai/api/v1/mcp/oauth/callback"


def _pkce():
    v = base64.urlsafe_b64encode(hashlib.sha256(b"v").digest() * 2).decode().rstrip("=")[:64]
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    return v, c


def _signin(env, code_for="alice", approve=True):
    """Jeevo's flow: discover, register, authorize page, approve with a code, trade."""
    env.gw.s.public_url = BASE
    c = env.public
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    m = re.search(r'resource_metadata="([^"]+)"', r.headers["www-authenticate"])
    assert r.status_code == 401 and m
    prm = c.get(urlparse(m[1]).path).json()
    assert prm["resource"] == f"{BASE}/mcp" and prm["authorization_servers"] == [BASE]
    asm = c.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["token_endpoint_auth_methods_supported"] == ["none"]
    reg = c.post("/oauth/register", json={"client_name": "Jeevo", "redirect_uris": [CB],
                                          "grant_types": ["authorization_code", "refresh_token"],
                                          "token_endpoint_auth_method": "none"})
    assert reg.status_code == 201
    cid = reg.json()["client_id"]
    v, ch = _pkce()
    page = c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": ch,
        "code_challenge_method": "S256", "state": "st-1", "resource": f"{BASE}/mcp"})
    assert page.status_code == 200 and "Connect Jeevo to Loft" in page.text
    assert "api.jeevo.ai" in page.text and "password" not in page.text.lower().replace(
        "your nas password there", "")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    rid = re.search(r"name=request_id value='([^']+)'", page.text)[1]
    code = env.gw.mint_assistant_code_cli(code_for, f"pw-{code_for}")["code"]
    back = c.post("/oauth/authorize", content=f"request_id={rid}&code={code}&action="
                  + ("allow" if approve else "deny"),
                  headers={"Content-Type": "application/x-www-form-urlencoded"},
                  follow_redirects=False)
    assert back.status_code == 302, back.text
    q = parse_qs(urlparse(back.headers["location"]).query)
    assert back.headers["location"].startswith(CB) and q["state"] == ["st-1"]
    assert q["iss"] == [BASE]
    return cid, v, q


def _trade(env, cid, v, code):
    return env.public.post("/oauth/token", content="&".join(f"{k}={val}" for k, val in {
        "grant_type": "authorization_code", "code": code, "redirect_uri": CB,
        "code_verifier": v, "client_id": cid, "resource": f"{BASE}/mcp"}.items()),
        headers={"Content-Type": "application/x-www-form-urlencoded"})


def _refresh(env, cid, rt):
    return env.public.post("/oauth/token", content=f"grant_type=refresh_token&refresh_token={rt}"
                           f"&client_id={cid}",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})


def test_oauth_signin_end_to_end(env):
    cid, v, q = _signin(env)
    tok = _trade(env, cid, v, q["code"][0])
    assert tok.status_code == 200 and tok.headers["cache-control"] == "no-store"
    t = tok.json()
    assert t["token_type"] == "Bearer" and t["expires_in"] == 7200 and t["refresh_token"]
    h = {"Authorization": f"Bearer {t['access_token']}"}
    assert rpc(env.public, h, "initialize")["result"]["serverInfo"]["name"] == "loft"
    # signed in, nothing opens until the user picks folders in the assistant
    assert "Nothing in Loft is shared" in text(call(env.public, h, "list_folder"))
    rpc(env.public, h, "loft/access/set", {"working_folder": f"{A}/Jeevo", "create": True})
    ok(call(env.public, h, "write_file", path=f"{A}/Jeevo/hi.md", text="hi"))
    # the code works once; a second trade ends the connection it made
    again = _trade(env, cid, v, q["code"][0])
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=h)
    assert r.status_code == 401 and r.json()["error"] == "device_revoked"
    # it was in alice's device list as Jeevo
    app = login(env)
    devs = env.public.get("/v1/auth/devices", headers=app).json()["devices"]
    assert any(d["kind"] == "mcp" and d["device_name"] == "Jeevo" for d in devs)


def _ping(env, t):
    return env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           headers={"Authorization": f"Bearer {t['access_token']}"})


def test_oauth_refresh_keeps_refresh_token_for_synced_devices(env):
    # Jeevo syncs one connection to the phone and the Mac; each refreshes by itself,
    # the Mac hours later with the refresh token the phone already used
    cid, v, q = _signin(env)
    t1 = _trade(env, cid, v, q["code"][0]).json()
    phone = _refresh(env, cid, t1["refresh_token"]).json()
    assert phone["refresh_token"] == t1["refresh_token"]       # doesn't rotate
    assert phone["access_token"] != t1["access_token"] and phone["expires_in"] == 7200
    assert _ping(env, t1).status_code == 200                   # the Mac's, still in its 2 h
    env.gw.db.conn.execute("UPDATE devices SET prev_valid_until=1 WHERE kind='mcp'")
    mac = _refresh(env, cid, t1["refresh_token"])
    assert mac.status_code == 200
    mac = mac.json()
    for t in (phone, mac):                                     # both devices keep working
        assert _ping(env, t).status_code == 200
    # an earlier access token still ends at its own expiry
    env.gw.db.conn.execute("UPDATE oauth_access SET expires_at=1")
    r = _ping(env, phone)
    assert r.status_code == 401 and r.json()["error"] == "token_expired"
    assert _ping(env, mac).status_code == 200
    # the refresh token lasts 90 days from its last use
    exp = env.gw.db.one("SELECT refresh_expires_at FROM devices WHERE kind='mcp'")[0]
    assert exp > time.time() + 89 * 86400
    env.gw.db.conn.execute("UPDATE devices SET refresh_expires_at=1 WHERE kind='mcp'")
    assert _refresh(env, cid, t1["refresh_token"]).json()["error"] == "invalid_grant"


def test_oauth_refresh_refusals_and_revoke(env):
    cid, v, q = _signin(env)
    t1 = _trade(env, cid, v, q["code"][0]).json()
    cid2, _, _ = _signin(env)
    assert _refresh(env, cid2, t1["refresh_token"]).json()["error"] == "invalid_grant"
    forged = t1["refresh_token"][:-4] + ("AAAA" if not t1["refresh_token"].endswith("AAAA") else "BBBB")
    assert _refresh(env, cid, forged).json()["error"] == "invalid_grant"
    assert _refresh(env, cid, t1["access_token"]).json()["error"] == "invalid_grant"
    # at most MCP_LIVE_ACCESS access tokens per connection: the oldest go first
    toks = [_refresh(env, cid, t1["refresh_token"]).json() for _ in range(MCP_LIVE_ACCESS + 2)]
    assert _ping(env, t1).status_code == 401 and _ping(env, toks[0]).status_code == 401
    assert all(_ping(env, t).status_code == 200 for t in toks[-MCP_LIVE_ACCESS:])
    # signing it out from the device list ends every token and leaves no wrapped password
    dev = env.gw.db.one("SELECT id FROM devices WHERE kind='mcp' AND client_id=?", (cid,))[0]
    env.gw.revoke(dev)
    assert not env.gw.db.all("SELECT * FROM oauth_access WHERE device_id=?", (dev,))
    assert env.gw.db.one("SELECT refresh_wrapped FROM devices WHERE id=?", (dev,))[0] is None
    for t in toks[-3:]:
        r = _ping(env, t)
        assert r.status_code == 401
    assert _refresh(env, cid, t1["refresh_token"]).json()["error"] == "invalid_grant"


def test_oauth_access_token_expires(env):
    cid, v, q = _signin(env)
    t = _trade(env, cid, v, q["code"][0]).json()
    env.gw.db.conn.execute("UPDATE devices SET expires_at=1 WHERE kind='mcp'")
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Authorization": f"Bearer {t['access_token']}"})
    assert r.status_code == 401 and r.json()["error"] == "token_expired"
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    t2 = _refresh(env, cid, t["refresh_token"]).json()
    assert rpc(env.public, {"Authorization": f"Bearer {t2['access_token']}"}, "ping")["result"] == {}


def test_oauth_refusals(env):
    cid, v, q = _signin(env)
    code = q["code"][0]
    assert _trade(env, cid, "x" * 64, code).json()["error"] == "invalid_grant"   # wrong verifier
    assert _trade(env, cid, v, code).json()["error"] == "invalid_grant"          # now used
    cid2, v2, q2 = _signin(env)
    assert _trade(env, cid, v2, q2["code"][0]).json()["error"] == "invalid_grant"  # other client
    # deny
    _, _, q3 = _signin(env, approve=False)
    assert q3["error"] == ["access_denied"] and "code" not in q3
    c = env.public
    # unknown client / unregistered redirect: an error page, never a redirect
    for params in ({"client_id": "lc_nope", "redirect_uri": CB},
                   {"client_id": cid, "redirect_uri": "https://evil.example/cb"}):
        r = c.get("/oauth/authorize", params={**params, "response_type": "code",
                                              "code_challenge": "a" * 43,
                                              "code_challenge_method": "S256"},
                  follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers
    # no PKCE, plain PKCE, wrong resource: back to the client with an error
    for extra in ({"code_challenge_method": "plain", "code_challenge": "a" * 43},
                  {"code_challenge_method": "S256", "code_challenge": "short"},
                  {"code_challenge_method": "S256", "code_challenge": "a" * 43,
                   "resource": "https://other.example/mcp"}):
        r = c.get("/oauth/authorize", params={"client_id": cid, "redirect_uri": CB,
                                              "response_type": "code", **extra},
                  follow_redirects=False)
        assert r.status_code == 302 and "error=" in r.headers["location"]
    # registration
    for body in ({"redirect_uris": ["http://evil.example/cb"]}, {"redirect_uris": []},
                 {"redirect_uris": ["https://x.example/cb#frag"]}, ["x"]):
        assert c.post("/oauth/register", json=body).status_code == 400
    assert c.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1:7777/cb"]}
                  ).status_code == 201
    # a wrong approval code keeps the page, with a message
    v, ch = _pkce()
    page = c.get("/oauth/authorize", params={"response_type": "code", "client_id": cid,
                                             "redirect_uri": CB, "code_challenge": ch,
                                             "code_challenge_method": "S256"})
    rid = re.search(r"name=request_id value='([^']+)'", page.text)[1]
    r = c.post("/oauth/authorize", content=f"request_id={rid}&code=ABCD-EFGH-JKLM&action=allow",
               headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 200 and "That code isn&#x27;t right" in r.text
    # no public_url: no OAuth on that listener
    env.gw.s.public_url = ""
    assert c.get("/.well-known/oauth-authorization-server").status_code == 404


def test_approval_codes(env):
    app = login(env)
    env.gw.s.assistant_codes_over_internet = False
    r = env.public.post("/v1/auth/assistant-codes", headers=app)
    assert r.status_code == 403 and r.json()["error"] == "codes_direct_only"
    r = env.direct.post("/v1/auth/assistant-codes", headers=app)
    assert r.status_code == 200 and re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}",
                                                 r.json()["code"])
    code = r.json()["code"]
    user, pw = env.gw.redeem_assistant_code(code.lower(), "1.2.3.4", "public")
    assert (user, pw) == ("alice", "pw-alice")
    with pytest.raises(ApiError) as e:                       # once only
        env.gw.redeem_assistant_code(code, "1.2.3.4", "public")
    assert e.value.code == "code_expired"
    # an approval code never signs in a device, and a device code never approves
    assert env.public.post("/v1/auth/code", json={"code": code, "device_name": "x"}
                           ).status_code == 401
    env.gw.s.assistant_codes_over_internet = True
    assert env.public.post("/v1/auth/assistant-codes", headers=app).status_code == 200
    # wrong guesses hit the limits
    for _ in range(25):
        try:
            env.gw.redeem_assistant_code("ZZZZ-ZZZZ-ZZZZ", "9.9.9.9", "public")
        except ApiError as e:
            last = e.code
    assert last == "too_many_attempts"


# ── schema migration, CLI ─────────────────────────────────────────────────

def test_schema_1_database_gets_the_new_columns(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE devices (id TEXT PRIMARY KEY, nas_user TEXT NOT NULL,
        device_name TEXT NOT NULL, token_hash BLOB NOT NULL, wrapped_cred BLOB NOT NULL,
        prev_token_hash BLOB, prev_wrapped_cred BLOB, prev_valid_until REAL, root_id TEXT,
        created_at REAL NOT NULL, last_used_at REAL, last_ip TEXT, expires_at REAL NOT NULL,
        revoked_at REAL);
        CREATE TABLE signin_codes (id TEXT PRIMARY KEY, nas_user TEXT NOT NULL,
        code_hash BLOB NOT NULL, wrapped_cred BLOB NOT NULL, created_by TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL, max_uses INTEGER NOT NULL,
        uses INTEGER NOT NULL DEFAULT 0, revoked_at REAL);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        INSERT INTO signin_codes (id, nas_user, code_hash, wrapped_cred, created_by, created_at,
        expires_at, max_uses) VALUES ('c1', 'alice', x'00', x'00', 'd1', 1, 2, 1);
        INSERT INTO devices (id, nas_user, device_name, token_hash, wrapped_cred, created_at,
        expires_at) VALUES ('d1', 'alice', 'Mac', x'00', x'00', 1, 2);""")
    c.commit()
    c.close()
    db = DB(str(p))
    row = db.one("SELECT kind, read_only, client_id FROM devices WHERE id='d1'")
    assert (row["kind"], row["read_only"], row["client_id"]) == ("app", 0, None)
    assert db.one("SELECT purpose FROM signin_codes WHERE id='c1'")["purpose"] == "device"
    assert db.one("SELECT v FROM meta WHERE k='schema_version'")["v"] == str(SCHEMA_VERSION)
    assert db.all("SELECT * FROM mcp_grants") == []


def test_schema_2_grants_count_as_approved(tmp_path):
    p = tmp_path / "v2.db"
    DB(str(p)).conn.close()
    c = sqlite3.connect(p)
    c.executescript("""DROP TABLE mcp_approved; UPDATE meta SET v='2' WHERE k='schema_version';
        INSERT INTO mcp_grants VALUES ('d1', '', 'write', 'home/alice/Jeevo', 5);
        INSERT INTO mcp_grants VALUES ('d1', 't', 'read', 'home/alice/Jeevo', 7);
        INSERT INTO mcp_grants VALUES ('d1', '', 'read', 'home/alice/Docs', 6);""")
    c.commit()
    c.close()
    db = DB(str(p))
    rows = [tuple(r) for r in db.all("SELECT device_id, path, access, created_at FROM mcp_approved"
                                     " ORDER BY path")]
    # the working folder's grant is a write approval, a read grant a read approval
    assert rows == [("d1", "home/alice/Docs", "read", 6), ("d1", "home/alice/Jeevo", "write", 5)]
    assert db.one("SELECT v FROM meta WHERE k='schema_version'")["v"] == str(SCHEMA_VERSION)


def test_approvals_table_from_0bca895_gets_access(tmp_path):
    p = tmp_path / "v3.db"
    DB(str(p)).conn.close()
    c = sqlite3.connect(p)
    c.executescript("""DROP TABLE mcp_approved; CREATE TABLE mcp_approved (device_id TEXT NOT NULL,
        path TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (device_id, path));
        INSERT INTO mcp_approved VALUES ('d1', 'home/alice/Docs', 1);""")
    c.commit()
    c.close()
    # what 0bca895 approved counts as read only: fails closed for working folders
    assert DB(str(p)).one("SELECT access FROM mcp_approved")["access"] == "read"


# ── widening needs the user's approval ────────────────────────────────────

def _set_up_long_ago(env, out):
    env.gw.db.conn.execute("UPDATE devices SET created_at=created_at-? WHERE id=?",
                           (mcp.FIRST_CHOICE_S + 60, out["device_id"]))


def test_widening_needs_an_approval_code(env):
    for d in ("Jeevo", "Docs/Old", "Projects/Apollo"):
        (env.nas / A / d).mkdir(parents=True, exist_ok=True)
    h, out = connect(env, working=f"{A}/Jeevo", read=[f"{A}/Docs"])
    # while it is being set up, right after the sign-in, choices count as approved
    ok_ = rpc(env.public, h, "loft/access/set", {"read": [f"{A}/Docs", f"{A}/Projects"]})
    assert "result" in ok_
    _set_up_long_ago(env, out)
    # narrowing, inside what was approved, and in a context: no code needed
    for want in ({"read": [f"{A}/Docs/Old"]}, {"read": []},
                 {"context": "meeting-1", "working_folder": f"{A}/Jeevo",
                  "read": [f"{A}/Docs", f"{A}/Projects/Apollo"]},
                 {"read": [f"{A}/Projects"]}):            # approved before, narrowed since
        assert "result" in rpc(env.public, h, "loft/access/set", want), want
    # wider: refused with the paths, and nothing changes
    before = rpc(env.public, h, "loft/access/get")["result"]
    for want, paths in (({"read": [f"{A}/Docs", A]}, [{"path": A, "access": "read"}]),
                        ({"read": [""]}, [{"path": "", "access": "read"}]),
                        ({"context": "m2", "working_folder": "home/alice"},
                         [{"path": A, "access": "write"}])):
        err = rpc(env.public, h, "loft/access/set", want)["error"]
        assert err["data"] == {"error": "approval_needed", "paths": paths}, want
        assert "approval code" in err["message"] and "Connect an assistant" in err["message"]
    assert rpc(env.public, h, "loft/access/get")["result"] == before
    # bob's code doesn't approve for alice, and it is used up trying
    bob = env.gw.mint_assistant_code_cli("bob", "pw-bob")["code"]
    err = rpc(env.public, h, "loft/access/set", {"read": [A], "approval_code": bob})["error"]
    assert "another account" in err["message"]
    err = rpc(env.public, h, "loft/access/set", {"read": [A], "approval_code": "ZZZZ-ZZZZ-ZZZZ"})
    assert err["error"]["data"] == {"error": "invalid_code"}
    # alice's code approves; once approved it stays approved after narrowing
    code = env.gw.mint_assistant_code_cli("alice", "pw-alice")["code"]
    res = rpc(env.public, h, "loft/access/set", {"read": [A], "approval_code": code})
    assert res["result"]["general"]["read"] == [A]
    assert "result" in rpc(env.public, h, "loft/access/set", {"read": []})
    assert "result" in rpc(env.public, h, "loft/access/set", {"read": [A]})
    # the code is spent: the whole drive needs another
    err = rpc(env.public, h, "loft/access/set", {"read": [""], "approval_code": code})["error"]
    assert err["data"] == {"error": "code_expired"}
    # a code given when nothing widens isn't used up
    code2 = env.gw.mint_assistant_code_cli("alice", "pw-alice")["code"]
    assert "result" in rpc(env.public, h, "loft/access/set", {"read": [A], "approval_code": code2})
    assert "result" in rpc(env.public, h, "loft/access/set", {"read": [""], "approval_code": code2})
    events = [r["event"] for r in env.gw.db.all("SELECT event FROM audit ORDER BY id")]
    assert "assistant_access_refused" in events and events.count("assistant_access_approved") >= 3


def test_a_read_approval_doesnt_give_write(env):
    """Review of 0bca895, M1: making a readable folder the working folder widens."""
    for d in ("Jeevo/Notes", "Docs"):
        (env.nas / A / d).mkdir(parents=True, exist_ok=True)
    (env.nas / A / "Docs/contract.txt").write_text("keep me")
    h, out = connect(env, working=f"{A}/Jeevo", read=[f"{A}/Docs"])
    _set_up_long_ago(env, out)
    err = rpc(env.public, h, "loft/access/set", {"working_folder": f"{A}/Docs"})["error"]
    assert err["data"]["paths"] == [{"path": f"{A}/Docs", "access": "write"}]
    assert call(env.public, h, "delete", path=f"{A}/Docs/contract.txt")["isError"]
    assert (env.nas / A / "Docs/contract.txt").exists()
    # inside the approved working folder, a context may write without a code
    assert "result" in rpc(env.public, h, "loft/access/set",
                           {"context": "m1", "working_folder": f"{A}/Jeevo/Notes"})
    # with a code it may, and the read approval becomes a write one
    code = env.gw.mint_assistant_code_cli("alice", "pw-alice")["code"]
    assert "result" in rpc(env.public, h, "loft/access/set",
                           {"working_folder": f"{A}/Docs", "approval_code": code})
    assert env.gw.db.one("SELECT access FROM mcp_approved WHERE path=?",
                         (f"{A}/Docs",))["access"] == "write"


def test_nothing_is_made_before_approval(env):
    """Review of 0bca895, S1: create made the folder before the check."""
    (env.nas / A / "Jeevo").mkdir(parents=True, exist_ok=True)
    h, out = connect(env, working=f"{A}/Jeevo")
    _set_up_long_ago(env, out)
    res = rpc(env.public, h, "loft/access/set",
              {"working_folder": f"{A}/Unapproved/New", "create": True})
    assert res["error"]["data"]["error"] == "approval_needed"
    assert not (env.nas / A / "Unapproved").exists()
    code = env.gw.mint_assistant_code_cli("alice", "pw-alice")["code"]
    res = rpc(env.public, h, "loft/access/set", {"working_folder": f"{A}/Unapproved/New",
                                                 "create": True, "approval_code": code})
    assert "result" in res and (env.nas / A / "Unapproved/New").is_dir()
    # a bad name is refused before the code is spent (review of 56dd02a, N1)
    code = env.gw.mint_assistant_code_cli("alice", "pw-alice")["code"]
    res = rpc(env.public, h, "loft/access/set", {"working_folder": f"{A}/Bad~Name",
                                                 "create": True, "approval_code": code})
    assert "error" in res and not (env.nas / A / "Bad~Name").exists()
    res = rpc(env.public, h, "loft/access/set", {"working_folder": f"{A}/Good",
                                                 "create": True, "approval_code": code})
    assert "result" in res


def test_approvals_tell_sharp_s_from_ss(env):
    """Review of 0bca895, P3: Samba tells Straße from Strasse; casefold() doesn't."""
    for d in ("Jeevo", "Straße", "Strasse"):
        (env.nas / A / d).mkdir(parents=True, exist_ok=True)
    h, out = connect(env, working=f"{A}/Jeevo", read=[f"{A}/Straße"])
    _set_up_long_ago(env, out)
    err = rpc(env.public, h, "loft/access/set", {"read": [f"{A}/Strasse"]})["error"]
    assert err["data"]["error"] == "approval_needed"
    # plain case differences are the same folder on Samba, and stay covered (the test NAS
    # is a case-sensitive disk, so this is checked on the comparison itself)
    assert mcp._covers(f"{A}/Straße", "HOME/Alice/STRAßE/x")
    assert not mcp._covers(f"{A}/Straße", f"{A}/STRASSE")


def test_cli_lists_approvals(env, monkeypatch, capsys):
    (env.nas / A / "Jeevo").mkdir(parents=True, exist_ok=True)
    connect(env, working=f"{A}/Jeevo", read=[""])
    import loft_gateway.config
    monkeypatch.setattr(loft_gateway.config, "load", lambda _cfg=None: env.gw.s)
    assert mcp.cli(["list"]) == 0
    assert f"approved: (whole drive) (read), {A}/Jeevo (write)" in capsys.readouterr().out


def test_assistants_need_their_own_account_setting(env):
    env.gw.s.assistant_users_re = "alice"
    with pytest.raises(ApiError) as e:
        env.gw.create_mcp_connection("bob", "pw-bob", "Jeevo")
    assert e.value.code == "account_not_allowed"
    h, _ = connect(env)
    assert "result" in rpc(env.public, h, "ping")
    # taken out of the setting: its connections stop at the next call
    env.gw.s.assistant_users_re = ""
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "account_not_allowed"
    # still allowed_users_re too: denied_users_re wins over both
    env.gw.s.assistant_users_re = ".*"
    with pytest.raises(ApiError):
        env.gw.create_mcp_connection("guest", "x", "Jeevo")


def test_assistants_default_to_everyone_who_may_sign_in(env):
    from loft_gateway.config import Settings
    env.gw.s.assistant_users_re = Settings().assistant_users_re
    out = env.gw.create_mcp_connection("bob", "pw-bob", "Jeevo")
    assert out["nas_user"] == "bob"
    # denied_users_re and allowed_users_re still apply
    with pytest.raises(ApiError) as e:
        env.gw.create_mcp_connection("guest", "x", "Jeevo")
    assert e.value.code == "account_not_allowed"

def test_cli(env, monkeypatch, capsys):
    import loft_gateway.config
    import loft_gateway.main
    env.gw.s.public_url = "https://files.example.org"
    monkeypatch.setattr(loft_gateway.main, "build", lambda _cfg=None: env.gw)
    monkeypatch.setattr(loft_gateway.config, "load", lambda _cfg=None: env.gw.s)
    monkeypatch.setattr("sys.stdin", io.StringIO("pw-alice\n"))
    assert mcp.cli(["add", "--user", "alice", "--name", "Jeevo", "--working-folder",
                    f"{A}/Jeevo", "--read", f"{A}/Docs"]) == 1          # no such folder yet
    out = capsys.readouterr()
    assert "No such folder" in out.err
    monkeypatch.setattr("sys.stdin", io.StringIO("pw-alice\n"))
    assert mcp.cli(["add", "--user", "alice", "--name", "Jeevo", "--read", f"{A}/Docs",
                    "--read-only"]) == 0
    out = capsys.readouterr().out
    assert "https://files.example.org/mcp" in out and "read only" in out
    tok = next(l.split()[-1] for l in out.splitlines() if l.startswith("Token:"))
    h = {"Authorization": f"Bearer {tok}"}
    assert text(ok(call(env.public, h, "read_file", path=f"{A}/Docs/a.txt"))).endswith("hello")
    assert mcp.cli(["list"]) == 0
    listing = capsys.readouterr().out
    assert "Jeevo" in listing and f"reads: {A}/Docs" in listing
    dev = next(l.split()[0] for l in listing.splitlines() if " active " in l)
    assert mcp.cli(["revoke", dev]) == 0
    assert env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           headers=h).status_code == 401
    monkeypatch.setattr("sys.stdin", io.StringIO("pw-alice\n"))
    assert mcp.cli(["code", "--user", "alice"]) == 0
    code = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", capsys.readouterr().out)[0]
    assert env.gw.redeem_assistant_code(code, None, "public")[0] == "alice"
    monkeypatch.setattr("sys.stdin", io.StringIO("wrong\n"))
    assert mcp.cli(["code", "--user", "alice"]) == 1


# ── limits from the review of 51991cc ──────────────────────────────────

def _chunks(n_bytes, piece=64 * 1024, fill=b"x"):
    def gen():
        left = n_bytes
        while left > 0:
            yield fill * min(piece, left)
            left -= piece
    return gen()


@pytest.mark.parametrize("path", ["/mcp", "/oauth/register", "/oauth/token", "/oauth/authorize",
                                  "/v1/auth/code", "/v1/auth/login"])
def test_chunked_bodies_are_capped(env, path):
    """B1: no Content-Length, so the cap must count the bytes as they arrive."""
    env.gw.s.public_url = BASE
    client = env.direct if path == "/v1/auth/login" else env.public
    h = connect(env)[0] if path == "/mcp" else {}      # without a token /mcp reads nothing: 401
    size = mcp.BODY_MAX + 1 if path == "/mcp" else 70_000
    r = client.post(path, content=_chunks(size), headers=h)
    assert "content-length" not in r.request.headers
    assert r.status_code == 413 and r.json() == {"error": "too_large"}
    r = client.post(path, content=b"x" * size, headers=h)       # declared: refused up front
    assert r.status_code == 413
    r = env.public.post("/v1/auth/code", content=_chunks(1000))  # small chunked bodies still work
    assert r.status_code == 422


def test_upload_chunks_keep_their_own_cap(env):
    h, _ = rooted(env)
    data = b"y" * 100_000
    u = env.public.post("/v1/uploads", json={"size": len(data),
                                             "sha256": hashlib.sha256(data).hexdigest()},
                        headers=h).json()
    r = env.public.put(f"/v1/uploads/{u['upload_id']}", params={"offset": 0},
                       content=_chunks(len(data), 10_000, b"y"),
                       headers={**h, "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()})
    assert r.status_code == 200, r.text
    env.gw.s.upload_chunk_max = 50_000
    r = env.public.put(f"/v1/uploads/{u['upload_id']}", params={"offset": len(data)},
                       content=_chunks(60_000), headers=h)
    assert r.status_code == 413 and r.json() == {"error": "chunk_too_large"}


def test_wrong_codes_from_others_dont_lock_out_a_real_code(env):
    """S1: limits are per code and per IP; only a swarm hits the global one."""
    code = env.gw.mint_assistant_code_cli("alice", "pw-alice")["code"]
    for i in range(30):
        try:
            env.gw.redeem_assistant_code(f"Z{'ABCDEFGHJK'[i % 10]}ZZ-ZZZZ-ZZZZ", f"10.0.0.{i}",
                                         "public")
        except ApiError as e:
            assert e.code == "invalid_code"
    assert env.gw.redeem_assistant_code(code, "1.2.3.4", "public")[0] == "alice"
    # five wrong guesses at one code lock that code
    code = env.gw.mint_assistant_code_cli("alice", "pw-alice")["code"]
    wrong = code[:5] + ("ZZZZ" if code[5:9] != "ZZZZ" else "YYYY") + code[9:]
    for i in range(5):
        with pytest.raises(ApiError):
            env.gw.redeem_assistant_code(wrong, f"10.1.0.{i}", "public")
    with pytest.raises(ApiError) as e:
        env.gw.redeem_assistant_code(code, "1.2.3.4", "public")
    assert e.value.code == "too_many_attempts"
    # and a swarm hits the global cap
    env.gw.s.assistant_code_max_fail_global = 40
    for i in range(10):
        try:
            env.gw.redeem_assistant_code("QQQQ-QQQQ-QQQQ", f"10.2.0.{i}", "public")
        except ApiError:
            pass
    with pytest.raises(ApiError) as e:
        env.gw.redeem_assistant_code("RRRR-RRRR-RRRR", "10.3.0.1", "public")
    assert e.value.code == "too_many_attempts"


def test_open_signin_pages_are_capped(env):
    """S2."""
    env.gw.s.public_url = BASE
    cid = env.public.post("/oauth/register", json={"redirect_uris": [CB]}).json()["client_id"]
    _, ch = _pkce()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": ch,
         "code_challenge_method": "S256"}
    codes = [env.public.get("/oauth/authorize", params=q).status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429
    other = env.public.get("/oauth/authorize", params=q, headers={"CF-Connecting-IP": "5.6.7.8"})
    assert other.status_code == 200


def test_return_hosts(env):
    """S3."""
    env.gw.s.public_url = BASE
    old = env.public.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]})
    env.gw.s.assistant_return_hosts = ["api.jeevo.ai"]
    assert env.public.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]}
                           ).status_code == 400
    for ok_uri in (CB, "http://127.0.0.1:7777/cb", "https://API.jeevo.ai/x"):
        assert env.public.post("/oauth/register", json={"redirect_uris": [ok_uri]}
                               ).status_code == 201, ok_uri
    _, ch = _pkce()
    r = env.public.get("/oauth/authorize", params={
        "response_type": "code", "client_id": old.json()["client_id"],
        "redirect_uri": "https://evil.example/cb", "code_challenge": ch,
        "code_challenge_method": "S256"}, follow_redirects=False)
    assert r.status_code == 403 and "location" not in r.headers
    cid, _, _ = _signin(env)                   # Jeevo itself still signs in
    assert cid


def test_assistant_url_moves_oauth_not_pairing(env):
    from loft_gateway import admin
    env.gw.s.public_url, env.gw.s.public_mode = "https://tunnel.example.org", "tunnel"
    env.gw.s.lan_url = "https://10.0.0.2:8443"
    env.gw.s.assistant_url = "https://files.example.org"
    assert env.public.get("/.well-known/oauth-authorization-server").json()["issuer"] == \
        "https://files.example.org"
    assert env.public.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == \
        "https://files.example.org/mcp"
    assert admin.pairing_payload(env.gw.s)["pub"] == "https://tunnel.example.org"


# ── review of af68b8f ──────────────────────────────────────────────

def test_anonymous_upload_chunk_is_refused_before_it_is_kept(env, monkeypatch):
    """B1b: the token is checked first; the body is read and dropped."""
    h, _ = rooted(env)
    data = b"z" * 300_000
    u = env.public.post("/v1/uploads", json={"size": len(data),
                                             "sha256": hashlib.sha256(data).hexdigest()},
                        headers=h).json()
    kept = []
    monkeypatch.setattr(env.gw, "upload_chunk", lambda *a: kept.append(a))
    r = env.public.put(f"/v1/uploads/{u['upload_id']}", params={"offset": 0},
                       content=_chunks(len(data), 10_000, b"z"))
    assert r.status_code == 401 and not kept
    r = env.public.put(f"/v1/uploads/{u['upload_id']}", params={"offset": 0}, content=data,
                       headers={"Authorization": "Bearer nst_bad.bad"})
    assert r.status_code == 401 and not kept


def test_upload_chunks_written_in_pieces(env, monkeypatch):
    from loft_gateway import service
    monkeypatch.setattr(service, "UPLOAD_WRITE_PIECE", 7_000)
    h, root = rooted(env)
    data = os.urandom(50_000)
    u = env.public.post("/v1/uploads", json={"size": len(data),
                                             "sha256": hashlib.sha256(data).hexdigest()},
                        headers=h).json()
    bad = env.public.put(f"/v1/uploads/{u['upload_id']}", params={"offset": 0}, content=data[:30_000],
                         headers={**h, "X-Chunk-SHA256": "0" * 64})
    assert bad.status_code == 422
    for off in (0, 30_000):                                    # 30 000 = 4 pieces + a part
        r = env.public.put(f"/v1/uploads/{u['upload_id']}", params={"offset": off},
                           content=data[off:off + 30_000],
                           headers={**h, "X-Chunk-SHA256": hashlib.sha256(
                               data[off:off + 30_000]).hexdigest()})
        assert r.status_code == 200, r.text
    r = env.public.post(f"/v1/uploads/{u['upload_id']}/commit",
                        json={"parent_id": root["root_item_id"], "name": "pieces.bin"}, headers=h)
    assert r.status_code in (200, 201), r.text
    assert (env.nas / "home/alice/pieces.bin").read_bytes() == data


def test_ipv6_limits_count_the_64(env):
    from loft_gateway.service import rate_ip
    assert rate_ip("2001:db8:1:2:aaaa::1") == rate_ip("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert rate_ip("2001:db8:1:3::1") != rate_ip("2001:db8:1:2::1")
    assert rate_ip("10.0.0.1") == "10.0.0.1" and rate_ip(None) is None
    assert rate_ip("::ffff:192.0.2.7") == "192.0.2.7" != rate_ip("::ffff:192.0.2.8")
    for i in range(20):
        with pytest.raises(ApiError):
            env.gw.redeem_assistant_code("QQQQ-QQQQ-QQQQ", f"2001:db8:1:2::{i + 1:x}", "public")
    with pytest.raises(ApiError) as e:
        env.gw.redeem_assistant_code("QQQQ-QQQQ-QQQQ", "2001:db8:1:2:dead::1", "public")
    assert e.value.code == "too_many_attempts"


def test_two_refreshes_at_once_keep_both_access_tokens(env, monkeypatch):
    from loft_gateway import crypto, service
    cid, v, q = _signin(env)
    t = _trade(env, cid, v, q["code"][0]).json()
    real, nested, busy = crypto.unwrap_credential, [], []

    def unwrap(*a, **k):        # the second refresh runs while the first is between its
        if not busy:            # read of the device row and its transaction
            busy.append(1)
            nested.append(env.gw.refresh_oauth(t["refresh_token"], cid, None, "public"))
        return real(*a, **k)
    monkeypatch.setattr(service.crypto, "unwrap_credential", unwrap)
    first = env.gw.refresh_oauth(t["refresh_token"], cid, None, "public")
    for tok in (t["access_token"], nested[0]["access_token"], first["access_token"]):
        assert env.gw.authenticate(tok, None, "public", "mcp")


def test_memory_report_on_usr2(env, capsys, monkeypatch):
    import signal
    from loft_gateway import main
    old = signal.getsignal(signal.SIGUSR2)
    monkeypatch.setenv("LOFT_TRACEMALLOC", "3")
    try:
        main._memory_report(env.gw)
        os.kill(os.getpid(), signal.SIGUSR2)
        err = capsys.readouterr().err
    finally:
        signal.signal(signal.SIGUSR2, old)
        import tracemalloc
        tracemalloc.stop()
    assert "memory report: VmRSS" in err and "mmapped blocks" in err
    assert "0 SMB sessions, thumbnail cache 0 MiB" in err
    assert "x dict" in err and "tracemalloc:" in err


def test_cli_approval_code_closes_its_nas_session(env, monkeypatch):
    """Review of bef7c4a, N2: the session that proves the password is closed."""
    opened, real = [], env.gw.backend.authenticate

    def auth(user, pw):
        s = real(user, pw)
        s.closed = False

        def close(s=s):
            s.closed = True
        s.close = close
        opened.append(s)
        return s
    monkeypatch.setattr(env.gw.backend, "authenticate", auth)
    assert env.gw.mint_assistant_code_cli("alice", "pw-alice")["code"]
    assert len(opened) == 1 and opened[0].closed
    assert all(s is not opened[0] for s, _ in env.gw._sessions.values())
