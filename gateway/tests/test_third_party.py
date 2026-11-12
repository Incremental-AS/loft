"""packaging/third_party/thirdparty.py: the image's SBOM, notices and copyleft source check.

Offline: a fake image root file system and file:// sources stand in for the image and upstream.
"""
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TP_DIR = HERE.parent / "packaging" / "third_party"
spec = importlib.util.spec_from_file_location("thirdparty", TP_DIR / "thirdparty.py")
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_wheel(site: Path, dist: str, version: str, libs: dict[str, bytes], license_expr: str):
    di = site / f"{dist.replace('-', '_')}-{version}.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {dist}\nVersion: {version}\n"
                                 f"License-Expression: {license_expr}\n\nreadme\n")
    pkg = dist.replace("-", "_")
    rec = [f"{pkg}/__init__.py,,", f"{pkg}/_core.cpython-312-x86_64-linux-gnu.so,,"]
    (site / pkg).mkdir()
    (site / pkg / "__init__.py").write_text("")
    (site / pkg / "_core.cpython-312-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    if libs:
        (site / f"{pkg}.libs").mkdir()
        for name, data in libs.items():
            (site / f"{pkg}.libs" / name).write_bytes(data)
            rec.append(f"{pkg}.libs/{name},,")
    (di / "RECORD").write_text("\n".join(rec) + "\n")


@pytest.fixture
def world(tmp_path):
    """A fake image with one wheel that bundles an LGPL, a GPL and a BSD library, plus one
    Debian package; upstream tarballs served from file://."""
    up = tmp_path / "upstream"
    up.mkdir()
    tars = {}
    for name in ("ffmpeg-9.tar", "x264-1.tar", "opus-1.tar", "wrapper-1.tar"):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for member, data in ((f"{name[:-4]}/COPYING", b"licence text\n"), (f"{name[:-4]}/main.c", b"int main;\n")):
                ti = tarfile.TarInfo(member)
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
        (up / name).write_bytes(buf.getvalue())
        tars[name] = sha(buf.getvalue())

    root = tmp_path / "rootfs"
    site = root / "opt/loft/venv/lib/python3.12/site-packages"
    site.mkdir(parents=True)
    make_wheel(site, "av", "1.0", {"libavcodec-1a.so.63": b"a", "libx264-2b.so.165": b"b",
                                   "libopus-3c.so.0": b"c"}, "BSD-3-Clause")
    make_wheel(site, "loft-gateway", "0.9.0", {}, "Apache-2.0")
    (root / "var/lib/dpkg").mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text(
        "Package: bash\nStatus: install ok installed\nArchitecture: amd64\nVersion: 5.2-1\n\n"
        "Package: libc6\nStatus: install ok installed\nArchitecture: amd64\nSource: glibc (2.36-9)\nVersion: 2.36-9\n")
    (root / "usr/share/doc/bash").mkdir(parents=True)
    (root / "usr/share/doc/bash/copyright").write_text(
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n\nFiles: *\nLicense: GPL-3+\n")

    def src(key, name):
        return (f'[source."{key}"]\nurl = "{(up / name).as_uri()}"\nsha256 = "{tars[name]}"\nby = "upstream"\n')
    comps = tmp_path / "components.toml"
    comps.write_text(f"""schema = 1
[own]
dist = "loft-gateway"
license = "AGPL-3.0-or-later"

[[wheel]]
dist = "av"
version = "1.0"
libs = "av.libs"
build = ["wrapper"]

[[component]]
wheel = "av@1.0"
id = "ffmpeg"
name = "FFmpeg"
version = "9"
license = "LGPL-3.0-or-later"
files = ["libavcodec-*"]
sources = ["ffmpeg"]

[[component]]
wheel = "av@1.0"
id = "x264"
name = "x264"
version = "1"
license = "GPL-2.0-or-later"
files = ["libx264-*"]
sources = ["x264"]

[[component]]
wheel = "av@1.0"
id = "opus"
name = "Opus"
version = "1"
license = "BSD-3-Clause"
files = ["libopus-*"]
sources = ["opus"]

{src("ffmpeg", "ffmpeg-9.tar")}
{src("x264", "x264-1.tar")}
{src("opus", "opus-1.tar")}
{src("wrapper", "wrapper-1.tar")}
""")
    return {"root": root, "site": site, "components": comps, "tmp": tmp_path, "up": up}


def collect(world, out="report"):
    comp = tp.load_components(world["components"])
    return tp.collect(world["root"], world["tmp"] / out, comp, "loft-gateway", "", "")


def by_id(inv):
    return {c["id"]: c for c in inv["components"]}


def test_collect_lists_bundled_parts_with_their_licences(world):
    inv = collect(world)
    assert inv["problems"] == []
    assert inv["image"] == {"name": "loft-gateway", "version": "0.9.0", "arch": "amd64"}
    c = by_id(inv)
    assert c["lib:av:ffmpeg"]["license"] == "LGPL-3.0-or-later" and c["lib:av:ffmpeg"]["needs_source"]
    assert c["lib:av:x264"]["license"] == "GPL-2.0-or-later" and c["lib:av:x264"]["needs_source"]
    assert c["lib:av:x264"]["files"] == ["av.libs/libx264-2b.so.165"]
    assert not c["lib:av:opus"]["needs_source"]
    assert c["py:loft-gateway:0.9.0"]["license"] == "AGPL-3.0-or-later"
    assert c["deb:bash:amd64"]["license"] == "GPL-3.0-or-later" and c["deb:bash:amd64"]["needs_source"]
    assert c["deb:libc6:amd64"]["debian_source"] == ["glibc", "2.36-9"]
    sbom = json.loads((world["tmp"] / "report/sbom.spdx.json").read_text())
    assert sbom["spdxVersion"] == "SPDX-2.3"
    names = {p["name"]: p["licenseDeclared"] for p in sbom["packages"]}
    assert names["x264"] == "GPL-2.0-or-later" and names["FFmpeg"] == "LGPL-3.0-or-later"
    assert (world["tmp"] / "report/licenses/deb-bash/copyright").is_file()


def test_unexplained_bundled_library_is_a_problem(world):
    (world["site"] / "av.libs" / "libx265-9z.so.217").write_bytes(b"x")
    inv = collect(world)
    assert any("libx265-9z.so.217 matches no component" in p for p in inv["problems"])


def test_wheel_version_not_described_is_a_problem(world):
    di = world["site"] / "av-1.0.dist-info"
    (di / "METADATA").write_text((di / "METADATA").read_text().replace("Version: 1.0", "Version: 2.0"))
    inv = collect(world)
    assert any("av 2.0 bundles libraries and is not in components.toml" in p for p in inv["problems"])


def test_check_refuses_an_image_without_a_source_bundle(world):
    inv = collect(world)
    errs = tp.check(inv, world["tmp"] / "no-bundle")
    assert errs and "no source bundle" in errs[0]


def fake_debian(monkeypatch, world):
    """Debian sources from file:// instead of snapshot.debian.org."""
    def debian_files(src, ver, cache):
        p = world["up"] / f"{src}_{ver}.dsc"
        p.write_text(f"Source: {src}\nVersion: {ver}\n")
        return [(p.name, p)]
    monkeypatch.setattr(tp, "debian_files", debian_files)


def test_sources_then_check_passes(world, monkeypatch):
    fake_debian(monkeypatch, world)
    inv = collect(world)
    comp = tp.load_components(world["components"])
    bundle = world["tmp"] / "bundle"
    idx = tp.build_sources(inv, bundle, comp, world["tmp"] / "cache")
    assert "component:av@1.0:x264" in idx and "debian:glibc=2.36-9" in idx
    assert any("x264-1.tar" in f for f in idx["component:av@1.0:x264"])
    assert any("wrapper-1.tar" in f for f in idx["component:av@1.0:x264"])  # the build scripts too
    assert (bundle / "notices" / "x264" / "COPYING").is_file()
    assert tp.check(inv, bundle) == []
    sums = (bundle / "SHA256SUMS").read_text()
    assert re.search(r"^[0-9a-f]{64}  upstream/x264/x264-1.tar$", sums, re.M)


def test_check_refuses_when_a_gpl_part_has_no_source(world, monkeypatch):
    fake_debian(monkeypatch, world)
    inv = collect(world)
    bundle = world["tmp"] / "bundle"
    tp.build_sources(inv, bundle, tp.load_components(world["components"]), world["tmp"] / "cache")
    data = json.loads((bundle / "sources.json").read_text())
    del data["sources"]["component:av@1.0:x264"]
    (bundle / "sources.json").write_text(json.dumps(data))
    errs = tp.check(inv, bundle)
    assert errs == ["lib:av:x264 (GPL-2.0-or-later): no source in the bundle"]


def test_check_refuses_a_tampered_source_file(world, monkeypatch):
    fake_debian(monkeypatch, world)
    inv = collect(world)
    bundle = world["tmp"] / "bundle"
    tp.build_sources(inv, bundle, tp.load_components(world["components"]), world["tmp"] / "cache")
    f = bundle / "upstream" / "ffmpeg" / "ffmpeg-9.tar"
    f.unlink()                     # a hard link to the cache: replace, don't write through
    f.write_bytes(b"not the source")
    errs = tp.check(inv, bundle)
    assert len(errs) == 1 and errs[0].startswith("lib:av:ffmpeg: source files missing or not matching")


def test_check_refuses_a_problem_even_with_a_bundle(world, monkeypatch):
    fake_debian(monkeypatch, world)
    (world["site"] / "av.libs" / "libmystery.so").write_bytes(b"?")
    inv = collect(world)
    bundle = world["tmp"] / "bundle"
    tp.build_sources(inv, bundle, tp.load_components(world["components"]), world["tmp"] / "cache")
    assert any("libmystery.so" in e for e in tp.check(inv, bundle))


def test_wrong_upstream_checksum_is_refused(world):
    t = world["components"].read_text()
    world["components"].write_text(re.sub(r'(\[source\."x264"\][^\[]*sha256 = ")[0-9a-f]{64}', r"\g<1>" + "0" * 64, t))
    inv = collect(world)
    with pytest.raises(tp.Refused, match="checksum mismatch"):
        tp.build_sources(inv, world["tmp"] / "b", tp.load_components(world["components"]), world["tmp"] / "c")


def test_label_must_match_what_the_image_holds(world, monkeypatch):
    fake_debian(monkeypatch, world)
    inv = collect(world)
    bundle = world["tmp"] / "bundle"
    tp.build_sources(inv, bundle, tp.load_components(world["components"]), world["tmp"] / "cache")
    want = tp.label(inv)
    assert "GPL-2.0-or-later" in want and "LGPL-3.0-or-later" in want and "AGPL-3.0-or-later" in want
    assert tp.check(inv, bundle, want) == []
    errs = tp.check(inv, bundle, "Apache-2.0")
    assert len(errs) == 1 and "label" in errs[0]


@pytest.mark.parametrize("expr,copyleft", [
    ("GPL-2.0-or-later", True), ("LGPL-3.0-or-later OR GPL-2.0-or-later", True),
    ("FTL OR GPL-2.0-or-later", False), ("BSD-3-Clause OR GPL-2.0-only", False), ("MIT", False),
    ("MPL-2.0", True), ("Apache-2.0 AND LGPL-2.1-or-later", True), ("NOASSERTION", False),
])
def test_is_copyleft(expr, copyleft):
    assert tp.is_copyleft(expr) is copyleft


def test_split_and_keeps_or_groups():
    assert tp.split_and("(Apache-2.0 OR BSD-3-Clause) AND MIT AND FTL OR GPL-2.0-or-later") == \
        ["(Apache-2.0 OR BSD-3-Clause)", "MIT", "(FTL OR GPL-2.0-or-later)"]


# ------------------------------------------------------------ the real manifest

def test_real_components_file_loads_and_every_copyleft_part_has_a_source():
    comp = tp.load_components(TP_DIR / "components.toml")
    for c in comp["component"]:
        if tp.is_copyleft(c["license"]):
            assert c.get("sources"), f"{c['id']} is copyleft and has no source"
        assert c.get("files") or c.get("inside"), c["id"]
    names = {c["name"] for c in comp["component"]}
    for must in ("x264", "x265", "FFmpeg", "libheif", "libde265"):
        assert must in names


def test_real_lock_matches_the_components_file():
    lock = HERE.parent / "requirements.lock"
    assert tp.lock_check(lock, tp.load_components(TP_DIR / "components.toml")) == []


def test_lock_check_refuses_a_stale_components_file(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text((HERE.parent / "requirements.lock").read_text().replace("av==19.0.2", "av==20.0.0"))
    errs = tp.lock_check(lock, tp.load_components(TP_DIR / "components.toml"))
    assert errs and errs[0].startswith("av==20.0.0 is locked")


def test_dockerfile_label_holds_the_copyleft_licences():
    text = (HERE.parent / "packaging/docker/Dockerfile").read_text()
    m = re.search(r'org\.opencontainers\.image\.licenses="([^"]+)"', text)
    assert m, "no licenses label"
    parts = set(tp.split_and(m.group(1)))
    comp = tp.load_components(TP_DIR / "components.toml")
    for c in comp["component"]:
        for p in tp.split_and(c["license"]):
            assert p in parts, f"{c['id']}: {p} missing from the Dockerfile's licenses label"
    assert comp["own"]["license"] in parts


def test_directions_to_the_source_sit_next_to_the_image():
    # GPLv3 §6(d): the source may be on another server if clear directions sit next to the object code.
    # The package page shows the image description and the repo's README; both name the *-sources.tar files.
    repo = HERE.parents[1]
    m = re.search(r'org\.opencontainers\.image\.description="([^"]*)"',
                  (HERE.parent / "packaging/docker/Dockerfile").read_text())
    assert m and "-sources.tar" in m.group(1) and "/releases" in m.group(1)
    wf = repo / ".github/workflows/gateway-image.yml"
    if wf.exists():
        descs = [line for line in wf.read_text().splitlines() if "image.description=" in line]
        assert len(descs) == 2 and all("-sources.tar" in d and "/releases/tag/" in d for d in descs)
    readme = (repo / "README.md").read_text()
    sec = readme.split("## Third-party source code", 1)[1].split("\n## ", 1)[0]
    assert "-sources.tar" in sec and "pyav-ffmpeg" in sec and "/releases" in sec
