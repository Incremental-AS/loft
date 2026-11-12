"""Content search (search.py): what is indexed, when it leaves, and that a hit is only ever
shown to someone who may open the file now (REST /v1/search and the MCP search_files tool)."""
import time

import pytest

from loft_gateway import search as content
from loft_gateway.config import Settings, validate

from conftest import login, rooted, upload
from test_mcp import NSA, O, R, T, W, X, A, _pdf, _zip, call, connect, ok, rpc, text


def _docx(*paras):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    return _zip({"word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>"})


def _odt(*paras):
    body = "".join(f"<text:p>{p}</text:p>" for p in paras)
    return _zip({"content.xml": f"<office:document-content {O} {T}><office:body><office:text>"
                 f"{body}</office:text></office:body></office:document-content>"})


def _xlsx(*cells):
    shared = "".join(f"<si><t>{c}</t></si>" for c in cells)
    row = "".join(f'<c r="{chr(65 + i)}1" t="s"><v>{i}</v></c>' for i in range(len(cells)))
    return _zip({"xl/workbook.xml": f'<workbook {X} {R}><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
                 "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
                 "xl/sharedStrings.xml": f"<sst {X}>{shared}</sst>",
                 "xl/worksheets/sheet1.xml": f'<worksheet {X}><sheetData><row r="1">{row}</row></sheetData></worksheet>'})


def _pptx(*lines):
    sl = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines)
    return _zip({"ppt/slides/slide1.xml": f'<p:sld xmlns:p="p" {NSA}>{sl}</p:sld>'})


def _odp(*lines):
    body = "".join(f"<text:p>{p}</text:p>" for p in lines)
    return _zip({"content.xml": f"<office:document-content {O} {T}><office:body><office:presentation>"
                 f"{body}</office:presentation></office:body></office:document-content>"})


def _root(env, user="alice"):
    return env.gw.db.one("SELECT * FROM roots WHERE nas_user=? ORDER BY created_at DESC", (user,))


def scan_and_index(env):
    """What the background threads do: a full pass of every root whose account is
    connected, then the content indexer."""
    for root in env.gw.db.all("SELECT * FROM roots"):
        sess = env.gw.session_for_user(root["nas_user"])
        if sess is not None:
            env.gw.rescan(root, sess)
    assert env.gw.search.run_pending(budget_s=120) is False


def find(env, h, q, **params):
    r = env.public.get("/v1/search", params={"q": q, **params}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def names(res):
    return sorted(i["name"] for i in res["items"])


# ── words ─────────────────────────────────────────────────────────────────

def test_words_keep_marks_and_pair_cjk():
    assert list(content.words("Hello, WORLD! hello_there")) == ["hello", "world", "hello", "there"]
    assert list(content.words("हिन्दी भाषा")) == ["हिन्दी", "भाषा"]
    assert list(content.words("日本語の文章")) == ["日本", "本語", "語の", "の文", "文章"]
    assert list(content.words("ｆｕｌｌ ﬁle")) == ["full", "file"]          # NFKC
    assert list(content.words("a" * 60 + " ok")) == ["ok"]                 # noise dropped


def test_forms_stem_in_each_language():
    f = content.Forms(["nb", "en"])
    assert f.key == "norwegian,english"
    assert "regnskap" in f.of("regnskapet")
    assert "budget" in f.of("budgets")
    assert f.of("regnskapet")[0] == "regnskapet"                       # the word itself first
    assert content.Forms([]).of("budgets") == ("budgets",)


def test_language_setting_is_checked():
    validate(Settings(search_languages=["nb", "en", "es-419", "pt-BR", "zh-Hans", "ko"]))
    with pytest.raises(ValueError, match="search_languages"):
        validate(Settings(search_languages=["klingon"]))


def test_every_listed_stemmer_exists():
    import snowballstemmer
    assert set(content.STEMMERS.values()) <= set(snowballstemmer.algorithms())
    assert not set(content.STEMMERS) & set(content.WORDS_ONLY)


def test_passage_is_short_and_around_the_match():
    f = content.Forms(["en"])
    body = ("filler words here. " * 80) + "The quarterly budgets were approved by the board. " \
        + ("more filler. " * 80)
    p = content.passage(body, "budget", f)
    assert len(p) <= content.SNIPPET_MAX and "budgets were approved" in p
    assert p.startswith("…") and p.endswith("…")


# ── indexing: formats, changes, deletes ──────────────────────────────────

FORMATS = {
    "plain.txt": "Saffron harvest notes".encode(),
    "readme.md": "# Plan\n\nThe *lighthouse* project".encode(),
    "memo.docx": _docx("Minutes about the glacier survey"),
    "sheet.xlsx": _xlsx("Item", "Cardamom"),
    "slides.pptx": _pptx("Roadmap for the observatory"),
    "story.odt": _odt("Once upon a fjord"),
    "calc.ods": _zip({"content.xml": f"<office:document-content {O} {T}><office:body><office:spreadsheet>"
                      "<table:table xmlns:table=\"urn:oasis:names:tc:opendocument:xmlns:table:1.0\">"
                      "<table:table-row><table:table-cell><text:p>Vanilla</text:p></table:table-cell>"
                      "</table:table-row></table:table></office:spreadsheet></office:body>"
                      "</office:document-content>"}),
    "talk.odp": _odp("Aurora borealis"),
    "scan.pdf": _pdf(["Invoice from the tannery"]),
}
WORDS = {"plain.txt": "saffron", "readme.md": "lighthouse", "memo.docx": "glacier",
         "sheet.xlsx": "cardamom", "slides.pptx": "observatory", "story.odt": "fjord",
         "calc.ods": "vanilla", "talk.odp": "aurora", "scan.pdf": "tannery"}


def test_every_format_is_indexed(env):
    for n, data in FORMATS.items():
        (env.nas / "home/alice" / n).write_bytes(data)
    (env.nas / "home/alice/photo.jpg").write_bytes(b"\xff\xd8saffron")   # not a document
    h, _ = rooted(env)
    scan_and_index(env)
    for n, w in WORDS.items():
        res = find(env, h, w)
        assert names(res) == [n], (w, res)
        assert w in res["items"][0]["snippet"].casefold()
        assert res["complete"] is True
    assert names(find(env, h, "saffron")) == ["plain.txt"]                # not the jpg
    states = {r["state"] for r in env.gw.db.all("SELECT state FROM search_docs")}
    assert states == {"indexed"}


def test_changes_are_followed(env):
    f = env.nas / "home/alice/Docs/plan.txt"
    f.write_text("the walrus migrates north")
    h, _ = rooted(env)
    scan_and_index(env)
    assert names(find(env, h, "walrus")) == ["plan.txt"]
    f.write_text("the narwhal migrates south, more text")
    scan_and_index(env)
    assert find(env, h, "walrus")["items"] == []
    assert names(find(env, h, "narwhal")) == ["plan.txt"]
    # saved through Keeps: indexed again with the new content
    pid = env.gw.db.one("SELECT id FROM items WHERE name='Docs' AND deleted=0")["id"]
    uid = upload(env.public, h, b"a beluga appears", near=pid)
    r = env.public.post(f"/v1/uploads/{uid}/commit", json={"parent_id": pid, "name": "new.md"},
                        headers=h)
    assert r.status_code == 200, r.text
    env.gw.search.run_pending()
    assert names(find(env, h, "beluga")) == ["new.md"]


def test_trash_and_delete_leave_the_index_at_once(env):
    (env.nas / "home/alice/Docs/one.txt").write_text("pelican report")
    (env.nas / "home/alice/Docs/two.txt").write_text("pelican summary")
    (env.nas / "home/alice/Docs/three.txt").write_text("pelican appendix")
    h, _ = rooted(env)
    scan_and_index(env)
    assert names(find(env, h, "pelican")) == ["one.txt", "three.txt", "two.txt"]
    ids = {r["name"]: r["id"] for r in env.gw.db.all("SELECT id, name FROM items WHERE deleted=0")}
    # to the trash through the REST API: gone from the index in the same transaction
    assert env.public.delete(f"/v1/items/{ids['one.txt']}", headers=h).status_code == 200
    assert not env.gw.db.one("SELECT 1 FROM search_docs WHERE item_id=?", (ids["one.txt"],))
    # deleted on the NAS itself: gone with the rescan that notices it
    (env.nas / "home/alice/Docs/two.txt").unlink()
    scan_and_index(env)
    assert not env.gw.db.one("SELECT 1 FROM search_docs WHERE item_id=?", (ids["two.txt"],))
    # an assistant's delete (Keeps' trash)
    hm, _ = connect(env)
    ok(call(env.public, hm, "delete", path=f"{A}/Docs/three.txt"))
    assert names(find(env, h, "pelican")) == []
    left = env.gw.db.one("SELECT COUNT(*) n FROM search_docs d JOIN items i ON i.id = d.item_id"
                         " WHERE i.deleted = 1")["n"]
    assert left == 0


def test_contentless_index_keeps_no_words(env):
    (env.nas / "home/alice/a.txt").write_text("zebra crossing")
    h, _ = rooted(env)
    scan_and_index(env)
    sql = env.gw.db.one("SELECT sql FROM sqlite_master WHERE name='search_fts'")["sql"]
    if "contentless_delete" in sql:
        assert not env.gw.db.one("SELECT name FROM sqlite_master WHERE name='search_fts_content'")


def test_nothing_is_read_without_a_connected_device(env):
    (env.nas / "home/alice/a.txt").write_text("tapir")
    h, root = rooted(env)
    scan_and_index(env)
    for d in env.gw.list_devices("alice"):
        env.gw._drop_session(d["id"])
    (env.nas / "home/alice/b.txt").write_text("tapir two")
    env.gw.reconcile(_root(env), env.gw.backend.authenticate("alice", "pw-alice"), root["id"])
    assert env.gw.search.run_pending() is False
    assert not env.gw.db.one("SELECT 1 FROM search_docs d JOIN items i ON i.id=d.item_id"
                             " WHERE i.name='b.txt'")
    res = find(env, h, "tapir")                         # the request brings a session back
    assert res["complete"] is False
    env.gw.search.run_pending()
    assert names(find(env, h, "tapir")) == ["a.txt", "b.txt"]


def test_failures_are_retried_then_recorded(env, monkeypatch):
    (env.nas / "home/alice/a.docx").write_bytes(_docx("okapi"))
    h, _ = rooted(env)
    for root in env.gw.db.all("SELECT * FROM roots"):
        env.gw.rescan(root, env.gw.session_for_user("alice"))

    def boom(*a, **k):
        raise TimeoutError("document workers busy")
    monkeypatch.setattr(env.gw.thumbs, "text", boom)
    for _ in range(content.MAX_ATTEMPTS):
        env.gw.db.conn.execute("UPDATE search_docs SET retry_at=0")
        env.gw.search.run_pending()
    row = env.gw.db.one("SELECT state, attempts FROM search_docs d JOIN items i"
                        " ON i.id = d.item_id WHERE i.name = 'a.docx'")
    assert (row["state"], row["attempts"]) == ("failed", content.MAX_ATTEMPTS)
    assert [r["name"] for r in env.gw.search.pending(_root(env)["id"])] == []   # until it changes


def test_big_files_are_skipped(env):
    env.gw.search.max_bytes = 10
    (env.nas / "home/alice/big.txt").write_text("hippo " * 10)
    h, _ = rooted(env)
    scan_and_index(env)
    assert find(env, h, "hippo")["items"] == []
    assert env.gw.db.one("SELECT state FROM search_docs d JOIN items i ON i.id = d.item_id"
                         " WHERE i.name = 'big.txt'")["state"] == "skipped"


# ── who sees a hit ────────────────────────────────────────────────────────

def test_another_persons_file_never_appears(env):
    (env.nas / "home/alice/mine.txt").write_text("alice writes about the albatross")
    (env.nas / "home/bob/his.txt").write_text("bob's secret albatross plan")
    ha, _ = rooted(env, "alice", "alice")
    hb, _ = rooted(env, "bob", "bob")
    scan_and_index(env)
    assert names(find(env, ha, "albatross")) == ["mine.txt"]
    assert names(find(env, hb, "albatross")) == ["his.txt"]
    # even with both in one share, each account searches only its own root
    hm, _ = connect(env, "alice", working=A, read=[A])
    res = ok(call(env.public, hm, "search_files", query="albatross", **{"in": "content"}))
    assert [e["path"] for e in res["structuredContent"]["items"]] == [f"{A}/mine.txt"]
    res = call(env.public, hm, "search_files", query="albatross", path="home/bob")
    assert res["isError"]


def test_rights_are_checked_when_asked(env):
    """The index still has the file; the NAS says no now: no hit, no passage."""
    (env.nas / "home/alice/Docs/x.txt").write_text("the condor nests")
    h, _ = rooted(env)
    scan_and_index(env)
    assert names(find(env, h, "condor")) == ["x.txt"]
    env.gw.backend.users["alice"]["shares"] = []        # the NAS took the share away
    r = env.public.get("/v1/search", params={"q": "condor"}, headers=h)
    assert r.status_code != 200 or r.json()["items"] == []
    env.gw.backend.users["alice"]["shares"] = ["home"]
    env.gw.s.blocked_paths = ["home/alice/Docs"]        # blocked on the gateway
    assert find(env, h, "condor")["items"] == []


def test_revoked_grant_never_appears(env):
    (env.nas / "home/alice/Projects/Apollo").mkdir(parents=True)
    (env.nas / "home/alice/Projects/Apollo/plan.md").write_text("moon landing checklist")
    (env.nas / "home/alice/Jeevo").mkdir()
    hm, out = connect(env, working=f"{A}/Jeevo", read=[f"{A}/Projects/Apollo"])
    ok(call(env.public, hm, "list_folder", path=f"{A}/Projects/Apollo"))
    scan_and_index(env)
    res = ok(call(env.public, hm, "search_files", query="landing"))
    hit = [e for e in res["structuredContent"]["items"] if e["match"] == "content"]
    assert [e["path"] for e in hit] == [f"{A}/Projects/Apollo/plan.md"]
    assert hit[0]["ref"].startswith("loft:item:") and hit[0]["title"] == "plan.md"
    assert "landing" in hit[0]["snippet"]
    # the user takes the folder away: the very next search doesn't see it
    rpc(env.public, hm, "loft/access/set", {"read": []})
    res = ok(call(env.public, hm, "search_files", query="landing"))
    assert res["structuredContent"]["items"] == []
    # a grant tied to a context counts only in that context
    rpc(env.public, hm, "loft/access/set", {"context": "t-1", "read": [f"{A}/Projects/Apollo"]})
    assert ok(call(env.public, hm, "search_files", query="landing"))["structuredContent"]["items"] == []
    res = ok(call(env.public, hm, "search_files", query="landing", ctx="t-1"))
    assert len(res["structuredContent"]["items"]) == 1
    # a revoked connection can't search at all
    env.gw.revoke(out["device_id"])
    r = env.public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "search_files", "_meta": {
                                          "no.incremental.loft/context": "t-1"},
                                          "arguments": {"query": "landing"}}}, headers=hm)
    assert r.status_code == 401 and r.json()["error"] == "device_revoked"


def test_search_below_a_folder(env):
    (env.nas / "home/alice/Docs/a.txt").write_text("heron")
    (env.nas / "home/alice/Other").mkdir()
    (env.nas / "home/alice/Other/b.txt").write_text("heron too")
    hm, _ = connect(env, working=A, read=[A])
    ok(call(env.public, hm, "list_folder", path=A))
    scan_and_index(env)
    res = ok(call(env.public, hm, "search_files", query="heron", path=f"{A}/Docs", **{"in": "content"}))
    assert [e["path"] for e in res["structuredContent"]["items"]] == [f"{A}/Docs/a.txt"]


# ── queries ───────────────────────────────────────────────────────────────

def test_stemming_and_every_word(env):
    env.gw.search.forms = env.gw.search.index.forms = content.Forms(["nb", "en"])
    (env.nas / "home/alice/no.txt").write_text("Regnskapet for 2025 er klart")
    (env.nas / "home/alice/en.txt").write_text("Budgets for the marketing team")
    h, _ = rooted(env)
    scan_and_index(env)
    assert names(find(env, h, "regnskap")) == ["no.txt"]
    assert names(find(env, h, "REGNSKAPENE")) == ["no.txt"]
    assert names(find(env, h, "budget")) == ["en.txt"]
    assert names(find(env, h, "budget marketing")) == ["en.txt"]
    assert find(env, h, "budget regnskap")["items"] == []          # every word must be there
    assert names(find(env, h, "2025")) == ["no.txt"]


def test_new_languages_reindex(env):
    (env.nas / "home/alice/no.txt").write_text("Regnskapet")
    h, _ = rooted(env)
    scan_and_index(env)
    assert find(env, h, "regnskap")["items"] == []                  # English only
    env.gw.search.forms = env.gw.search.index.forms = content.Forms(["nb"])
    assert env.gw.search.pending(_root(env)["id"])
    env.gw.search.run_pending()
    assert names(find(env, h, "regnskap")) == ["no.txt"]


def test_paging_and_bad_input(env):
    for i in range(7):
        (env.nas / f"home/alice/f{i}.txt").write_text(
            "kestrel " * (i + 1) + " ".join(f"filler{n}" for n in range(30 - i)))
    h, _ = rooted(env)
    scan_and_index(env)
    seen, cursor = [], None
    for _ in range(5):
        res = find(env, h, "kestrel", limit=3, **({"cursor": cursor} if cursor else {}))
        seen += [i["name"] for i in res["items"]]
        cursor = res["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == [f"f{i}.txt" for i in range(7)]
    assert seen[0] == "f6.txt"                                        # most matches first
    assert env.public.get("/v1/search", params={"q": "!!"}, headers=h).json()["error"] == "bad_query"
    r = env.public.get("/v1/search", params={"q": "kestrel", "cursor": "nope"}, headers=h)
    assert r.json()["error"] == "bad_cursor"
    assert env.public.get("/v1/search", params={"q": "x", "limit": 51}, headers=h).status_code == 422


def test_mcp_paging_and_modes(env):
    for i in range(4):
        (env.nas / f"home/alice/n{i}.md").write_text("osprey sighting")
    (env.nas / "home/alice/osprey.txt").write_text("nothing here")
    hm, _ = connect(env, working=A, read=[A])
    ok(call(env.public, hm, "list_folder", path=A))
    scan_and_index(env)
    res = ok(call(env.public, hm, "search_files", query="osprey", limit=2))
    sc = res["structuredContent"]
    assert [e["match"] for e in sc["items"]] == ["name", "content"]
    assert sc["items"][0]["path"] == f"{A}/osprey.txt" and sc["next_cursor"]
    got = [sc["items"][1]["path"]]
    while sc["next_cursor"]:
        sc = ok(call(env.public, hm, "search_files", query="osprey", limit=2,
                     cursor=sc["next_cursor"]))["structuredContent"]
        assert all(e["match"] == "content" for e in sc["items"])
        got += [e["path"] for e in sc["items"]]
    assert sorted(got) == [f"{A}/n{i}.md" for i in range(4)]
    names_only = ok(call(env.public, hm, "search_files", query="osprey", **{"in": "name"}))
    assert [e["path"] for e in names_only["structuredContent"]["items"]] == [f"{A}/osprey.txt"]
    assert "osprey sighting" in text(ok(call(env.public, hm, "search_files", query="osprey")))
    assert call(env.public, hm, "search_files", query="osprey", cursor="zz")["isError"]
    assert call(env.public, hm, "search_files", query="osprey", **{"in": "body"})["isError"]


def test_switched_off(env):
    env.gw.search.enabled = False
    (env.nas / "home/alice/a.txt").write_text("ibis")
    h, _ = rooted(env)
    scan_and_index(env)
    assert env.public.get("/v1/search", params={"q": "ibis"}, headers=h).json()["error"] == \
        "search_disabled"
    hm, _ = connect(env, working=A, read=[A])
    out = text(ok(call(env.public, hm, "search_files", query="ibis")))
    assert "switched off" in out


def test_passage_hidden_when_the_file_changed(env):
    f = env.nas / "home/alice/a.txt"
    f.write_text("the original about a gecko")
    h, _ = rooted(env)
    scan_and_index(env)
    f.write_text("the rewrite still mentions a gecko, and more")
    res = find(env, h, "gecko")
    assert names(res) == ["a.txt"] and res["items"][0]["snippet"] is None
    env.gw.search.run_pending()
    assert "rewrite" in find(env, h, "gecko")["items"][0]["snippet"]


def test_the_index_survives_a_restart(env, tmp_path):
    (env.nas / "home/alice/a.txt").write_text("lemur")
    h, _ = rooted(env)
    scan_and_index(env)
    from loft_gateway.db import DB
    from loft_gateway.service import Gateway
    gw2 = Gateway(env.gw.s, DB(env.gw.s.db_path), env.gw.backend, env.gw.master)
    assert gw2.search.index.candidates([_root(env)["id"]], "lemur", 0, 5)
    gw2.watches.stop_all()
