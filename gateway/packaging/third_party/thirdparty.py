#!/usr/bin/env python3
"""Third-party inventory, SBOM, licence notices and copyleft source bundle for the gateway's
container image (and any other root file system that holds the gateway's venv).

    thirdparty.py collect <rootfs> <out-dir> [--components F] [--name N] [--image-version V] [--arch A]
        Reads the root file system of an image (docker export, or / inside a build) and writes
        <out-dir>/inventory.json, <out-dir>/sbom.spdx.json (SPDX 2.3) and <out-dir>/licenses/
        (every licence and copyright file the image carries, by component).

    thirdparty.py sources <inventory.json> <bundle-dir> [--components F] [--cache DIR]
        Fetches the exact source of every component the inventory marks as needing it (the
        GPL/LGPL/MPL parts of the wheels with the scripts that built them, and every Debian
        package), checks each file's SHA-256 (Debian: snapshot.debian.org's SHA-1 and the .dsc's
        SHA-256), and writes <bundle-dir>/sources.json and <bundle-dir>/SHA256SUMS. Also
        extracts the licence files of every fetched archive into <bundle-dir>/notices/.

    thirdparty.py check <inventory.json> <bundle-dir>
        Refuses (exit 1) when a component needs its source and the bundle does not hold it with
        matching checksums, or when the inventory has a bundled library nobody identified.

    thirdparty.py lock <requirements.lock> [--components F]
        Refuses (exit 1) when a wheel the components file describes is locked at a version the
        file does not describe (the manifest went stale).

    thirdparty.py label <inventory.json>
        Prints the SPDX expression for the image's org.opencontainers.image.licenses label.

Standard library only (Python 3.11+). Never executes anything from the inspected tree.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_COMPONENTS = HERE / "components.toml"
UA = "loft-gateway-thirdparty/1 (+https://github.com/Incremental-AS/loft)"
SNAPSHOT = "https://snapshot.debian.org"
COPYLEFT = re.compile(r"^(?:A?GPL|LGPL|MPL|EUPL|EPL|CDDL|OSL|CPL)-", re.I)
LICENSE_NAME = re.compile(r"^(?:COPYING|COPYRIGHT|LICEN[CS]E|NOTICE|PATENTS)([._-].*|v\d.*)?$", re.I)
SPDX_TOKEN = re.compile(r"^[A-Za-z0-9.+-]+$")

# DEP-5 short names seen in Debian copyright files -> SPDX ids (others stay as written).
DEP5 = {
    "GPL-1+": "GPL-1.0-or-later", "GPL-2": "GPL-2.0-only", "GPL-2+": "GPL-2.0-or-later",
    "GPL-3": "GPL-3.0-only", "GPL-3+": "GPL-3.0-or-later", "LGPL-2": "LGPL-2.0-only",
    "LGPL-2+": "LGPL-2.0-or-later", "LGPL-2.1": "LGPL-2.1-only", "LGPL-2.1+": "LGPL-2.1-or-later",
    "LGPL-3": "LGPL-3.0-only", "LGPL-3+": "LGPL-3.0-or-later", "Expat": "MIT", "BSD-2-clause": "BSD-2-Clause",
    "BSD-3-clause": "BSD-3-Clause", "BSD-4-clause": "BSD-4-Clause", "public-domain": "LicenseRef-public-domain",
    "MPL-2.0": "MPL-2.0", "Apache-2.0": "Apache-2.0", "Zlib": "Zlib", "zlib": "Zlib", "ISC": "ISC",
    "Artistic": "Artistic-1.0-Perl", "PSF-2": "PSF-2.0",
}


class Refused(Exception):
    pass


# ------------------------------------------------------------------ helpers

def load_components(path: Path) -> dict:
    data = tomllib.loads(Path(path).read_text())
    if data.get("schema") != 1:
        raise Refused(f"{path}: unknown schema {data.get('schema')!r}")
    wheels = {f"{w['dist']}@{w['version']}": w for w in data.get("wheel", [])}
    for c in data.get("component", []):
        if c["wheel"] not in wheels:
            raise Refused(f"component {c['id']}: no [[wheel]] {c['wheel']}")
        for s in c.get("sources", []):
            if s not in data.get("source", {}):
                raise Refused(f"component {c['id']}: no [source.{s}]")
    for w in wheels.values():
        for s in w.get("build", []):
            if s not in data.get("source", {}):
                raise Refused(f"wheel {w['dist']}: no [source.{s}]")
    for k, s in data.get("source", {}).items():
        if not re.fullmatch(r"[0-9a-f]{64}", s.get("sha256", "")):
            raise Refused(f"[source.{k}]: no SHA-256")
    data["_wheels"] = wheels
    data["_path"] = str(path)
    return data


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_copyleft(expr: str | None) -> bool:
    """True when every alternative of an SPDX expression carries a copyleft licence."""
    if not expr or expr == "NOASSERTION":
        return False
    alternatives = [a for a in re.split(r"\s+OR\s+", expr.replace("(", " ").replace(")", " ")) if a.strip()]

    def alt_copyleft(alt: str) -> bool:
        ids = [t for t in re.split(r"\s+(?:AND|WITH)\s+|\s+", alt) if t and t not in ("AND", "WITH")]
        return any(COPYLEFT.match(t) for t in ids)
    # "A AND (B OR C)" is split too coarsely by the above for nested groups; treat any top-level
    # AND that holds a copyleft id as copyleft unless a permissive-only alternative exists.
    return all(alt_copyleft(a) for a in alternatives)


def spdx_or_none(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    tokens = [x for x in re.split(r"[\s()]+", t) if x]
    if tokens and all(x in ("AND", "OR", "WITH") or SPDX_TOKEN.match(x) for x in tokens) \
            and not any(x.lower() in ("license", "licence", "licenses") for x in tokens):
        return t
    return None


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", s)


def inside(root: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ collect

def parse_metadata(text: str) -> dict:
    head = text.split("\n\n", 1)[0]
    out: dict[str, list[str]] = {}
    key = None
    for line in head.splitlines():
        if line[:1] in (" ", "\t") and key:
            out[key][-1] += "\n" + line.strip()
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            out.setdefault(key.strip(), []).append(val.strip())
    return out


def python_dists(root: Path) -> list[dict]:
    dists = []
    for sp in sorted(root.glob("opt/*/venv/lib/python3*/site-packages")) + \
            sorted(root.glob("usr/local/lib/python3*/site-packages")) + \
            sorted(root.glob("usr/lib/python3/dist-packages")):
        if not inside(root, sp):
            continue
        for di in sorted(sp.glob("*.dist-info")):
            meta = di / "METADATA"
            if not meta.is_file():
                continue
            m = parse_metadata(meta.read_text(errors="replace"))
            record = []
            if (di / "RECORD").is_file():
                record = [ln.split(",", 1)[0] for ln in (di / "RECORD").read_text(errors="replace").splitlines() if ln]
            dists.append({
                "site": sp, "distinfo": di, "name": m.get("Name", [di.name.split("-")[0]])[0],
                "version": m.get("Version", [""])[0], "meta": m, "record": record,
            })
    return dists


def dist_license(d: dict, comp: dict) -> tuple[str, str]:
    """(SPDX expression or NOASSERTION, how it was found)."""
    own = comp.get("own", {})
    if norm(d["name"]) == norm(own.get("dist", "")):
        return own["license"], "components.toml [own]"
    over = {norm(k): v for k, v in comp.get("dist_license", {}).items()}
    if norm(d["name"]) in over:
        return over[norm(d["name"])], "components.toml [dist_license]"
    m = d["meta"]
    if m.get("License-Expression"):
        return m["License-Expression"][0], "License-Expression"
    lic = spdx_or_none((m.get("License") or [None])[0])
    if lic:
        return lic, "License"
    return "NOASSERTION", "none readable: " + "; ".join((m.get("License") or []) + m.get("Classifier", []))[:200]


def dpkg_packages(root: Path) -> list[dict]:
    status = root / "var/lib/dpkg/status"
    if not status.is_file():
        return []
    pkgs = []
    for block in status.read_text(errors="replace").split("\n\n"):
        f = dict(re.findall(r"^([A-Za-z-]+): (.*)$", block, re.M))
        if not f.get("Package") or "installed" not in f.get("Status", ""):
            continue
        src, srcver = f.get("Source", f["Package"]), f["Version"]
        mm = re.match(r"(\S+) \((.*)\)", src)
        if mm:
            src, srcver = mm.group(1), mm.group(2)
        pkgs.append({"name": f["Package"], "version": f["Version"], "arch": f.get("Architecture", ""),
                     "source": src, "source_version": srcver})
    return pkgs


def dep5_license(copyright_text: str) -> tuple[str, list[str]]:
    names = []
    if not copyright_text.startswith("Format:"):
        return "NOASSERTION", names
    for ln in copyright_text.splitlines():
        if ln.startswith("License:"):
            n = ln[8:].strip()
            if n and n not in names:
                names.append(n)
    ids = []
    for n in names:
        for part in re.split(r"\s+or\s+|\s*\|\s*|\s+and\s+|,\s*", n):
            part = part.strip()
            if not part:
                continue
            if part in DEP5:
                ids.append(DEP5[part])
            elif SPDX_TOKEN.match(part):
                ids.append("LicenseRef-Debian-" + safe_name(part))
    ids = sorted(set(ids))
    return (" AND ".join(ids) if ids else "NOASSERTION"), names


def collect(root: Path, out: Path, comp: dict, name: str, version: str, arch: str) -> dict:
    root = root.resolve()
    out.mkdir(parents=True, exist_ok=True)
    lic_dir = out / "licenses"
    items: list[dict] = []
    problems: list[str] = []

    def copy_license(src: Path, comp_id: str, rel: str):
        if not src.is_file() or src.is_symlink() or not inside(root, src):
            return None
        dst = lic_dir / safe_name(comp_id) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return str(dst.relative_to(out))

    # Debian packages: binaries, so their source is always required (most hold GPL/LGPL code).
    for p in dpkg_packages(root):
        cid = f"deb:{p['name']}:{p['arch']}"
        cfile = root / "usr/share/doc" / p["name"] / "copyright"
        expr, raw = ("NOASSERTION", [])
        lic_files = []
        if cfile.is_file() and inside(root, cfile):
            expr, raw = dep5_license(cfile.read_text(errors="replace"))
            f = copy_license(cfile, f"deb-{p['name']}", "copyright")
            if f:
                lic_files.append(f)
        items.append({
            "id": cid, "kind": "deb", "name": p["name"], "version": p["version"], "license": expr,
            "license_note": "Debian copyright file: " + ", ".join(raw) if raw else "Debian copyright file (not machine-readable)",
            "needs_source": True, "source_key": f"debian:{p['source']}={p['source_version']}",
            "debian_source": [p["source"], p["source_version"]], "license_files": lic_files,
            "purl": f"pkg:deb/debian/{p['name']}@{urllib.parse.quote(p['version'])}?arch={p['arch']}",
        })

    # CPython built into /usr/local (the official python images).
    for pl in sorted(root.glob("usr/local/include/python3*/patchlevel.h")):
        mm = re.search(r'#define PY_VERSION\s+"([^"]+)"', pl.read_text(errors="replace"))
        if not mm:
            continue
        pyver = mm.group(1)
        lic = []
        f = copy_license(pl.parents[2] / "lib" / pl.parent.name / "LICENSE.txt", "cpython", "LICENSE.txt")
        if f:
            lic.append(f)
        items.append({"id": f"cpython:{pyver}", "kind": "runtime", "name": "CPython", "version": pyver,
                      "license": "PSF-2.0", "needs_source": False, "license_files": lic,
                      "purl": f"pkg:generic/cpython@{pyver}"})

    wheels = comp["_wheels"]
    for d in python_dists(root):
        key = f"{d['name']}@{d['version']}"
        nkey = f"{norm(d['name'])}@{d['version']}"
        expr, how = dist_license(d, comp)
        binaries = [r for r in d["record"] if re.search(r"\.so(\.|$)", r)]
        lic = []
        for lf in sorted((d["distinfo"] / "licenses").rglob("*")) if (d["distinfo"] / "licenses").is_dir() else []:
            if lf.is_file() and lf.suffix != ".pyc":
                f = copy_license(lf, f"py-{d['name']}", str(lf.relative_to(d["distinfo"] / "licenses")))
                if f:
                    lic.append(f)
        for lf in sorted(d["distinfo"].glob("*")):
            if lf.is_file() and LICENSE_NAME.match(lf.name):
                f = copy_license(lf, f"py-{d['name']}", lf.name)
                if f:
                    lic.append(f)
        # Our own code ships as Python source and is published as a tagged release of this repo.
        cl = is_copyleft(expr) and how != "components.toml [own]"
        where = "" if "/venv/" in str(d["site"]) else "@" + safe_name(str(d["site"].relative_to(root)).split("/lib/")[0])
        item = {"id": f"py:{norm(d['name'])}:{d['version']}{where}", "kind": "python", "name": d["name"],
                "version": d["version"], "license": expr, "license_note": how,
                "binary": bool(binaries), "needs_source": cl and bool(binaries),
                "source_key": f"python:{norm(d['name'])}={d['version']}", "license_files": lic,
                "purl": f"pkg:pypi/{norm(d['name'])}@{d['version']}"}
        if expr == "NOASSERTION" and binaries:
            item["problem"] = "compiled code under a licence the tool can't read: add it to [dist_license]"
        items.append(item)

        # Libraries auditwheel grafted into <pkg>.libs/
        libdirs = sorted({r.split("/", 1)[0] for r in d["record"] if re.match(r"[^/]+\.libs/", r)})
        if not libdirs:
            continue
        wheel = wheels.get(key) or next((w for k, w in wheels.items()
                                          if f"{norm(k.split('@')[0])}@{k.split('@')[1]}" == nkey), None)
        files = sorted(Path(d["site"], ld, f).name for ld in libdirs
                       for f in os.listdir(Path(d["site"], ld)) if Path(d["site"], ld, f).is_file())
        if not wheel:
            for f in files:
                items.append({"id": f"lib:{norm(d['name'])}:{f}", "kind": "bundled", "name": f, "version": "",
                              "license": "NOASSERTION", "needs_source": True, "in": item["id"],
                              "problem": f"{d['name']} {d['version']} bundles libraries and is not in components.toml"})
            continue
        wkey = f"{wheel['dist']}@{wheel['version']}"
        comps = [c for c in comp.get("component", []) if c["wheel"] == wkey]
        claimed: set[str] = set()
        notices_root = Path(comp.get("_path", DEFAULT_COMPONENTS)).parent / "notices"
        for c in comps:
            mine = [f for f in files if any(fnmatch.fnmatchcase(f, g) for g in c.get("files", []))]
            host = [f for f in files if c.get("inside") and fnmatch.fnmatchcase(f, c["inside"])]
            if not mine and not host:
                continue  # not in this platform's wheel
            claimed.update(mine)
            lic = []
            for sid in c.get("sources", []):
                nd = notices_root / safe_name(sid)
                for nf in sorted(nd.glob("*")) if nd.is_dir() else []:
                    dst = lic_dir / safe_name(f"lib-{c['id']}") / nf.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(nf, dst)
                    lic.append(str(dst.relative_to(out)))
            items.append({
                "id": f"lib:{norm(d['name'])}:{c['id']}", "kind": "bundled", "name": c["name"],
                "version": c["version"], "license": c["license"], "in": item["id"],
                "files": [f"{wheel['libs']}/{f}" for f in mine],
                "inside": [f"{wheel['libs']}/{f}" for f in host],
                "needs_source": is_copyleft(c["license"]),
                "source_key": f"component:{wkey}:{c['id']}",
                "sources": list(c.get("sources", [])) + list(wheel.get("build", [])), "license_files": lic,
                "purl": f"pkg:generic/{safe_name(c['id'])}@{c['version']}",
            })
        for f in files:
            if f not in claimed:
                items.append({"id": f"lib:{norm(d['name'])}:{f}", "kind": "bundled", "name": f, "version": "",
                              "license": "NOASSERTION", "needs_source": True, "in": item["id"],
                              "problem": f"{wheel['libs']}/{f} matches no component of {wkey} in components.toml"})

    if not version:
        own = norm(comp.get("own", {}).get("dist", ""))
        version = next((it["version"] for it in items if it["kind"] == "python" and norm(it["name"]) == own), "")
    if not arch:
        archs = [it["id"].rsplit(":", 1)[1] for it in items if it["kind"] == "deb"]
        archs = [a for a in archs if a != "all"]
        arch = max(set(archs), key=archs.count) if archs else ""
    for it in items:
        if it.get("problem"):
            problems.append(f"{it['id']}: {it['problem']}")
    inv = {"schema": 1, "image": {"name": name, "version": version, "arch": arch},
           "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "components": items, "problems": problems}
    (out / "inventory.json").write_text(json.dumps(inv, indent=1, sort_keys=False) + "\n")
    (out / "sbom.spdx.json").write_text(json.dumps(to_spdx(inv), indent=1) + "\n")
    (out / "THIRD_PARTY_NOTICES.md").write_text(notices_md(inv))
    return inv


def to_spdx(inv: dict) -> dict:
    img = inv["image"]
    top = "SPDXRef-image"
    pkgs = [{"SPDXID": top, "name": img["name"], "versionInfo": img["version"], "downloadLocation": "NOASSERTION",
             "filesAnalyzed": False, "licenseConcluded": "NOASSERTION", "licenseDeclared": label(inv),
             "copyrightText": "NOASSERTION", "comment": f"container image, {img['arch']}"}]
    rels = [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": top}]
    ids = {}
    for n, it in enumerate(inv["components"]):
        sid = f"SPDXRef-{n}-{safe_name(it['id']).replace('_', '-').replace('+', '-').replace(':', '-')}"[:120]
        sid = re.sub(r"[^A-Za-z0-9.-]", "-", sid)
        ids[it["id"]] = sid
        lic = it["license"] if it["license"] else "NOASSERTION"
        p = {"SPDXID": sid, "name": it["name"], "versionInfo": it.get("version") or "NOASSERTION",
             "downloadLocation": "NOASSERTION", "filesAnalyzed": False, "licenseConcluded": "NOASSERTION",
             "licenseDeclared": lic, "copyrightText": "NOASSERTION",
             "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                               "referenceLocator": it["purl"]}] if it.get("purl") else []}
        notes = [x for x in (it.get("license_note"), ", ".join(it.get("files", []) + it.get("inside", []))) if x]
        if notes:
            p["comment"] = "; ".join(notes)
        pkgs.append(p)
    for it in inv["components"]:
        parent = ids.get(it.get("in", ""), top)
        rels.append({"spdxElementId": parent, "relationshipType": "CONTAINS", "relatedSpdxElement": ids[it["id"]]})
    refs = sorted({t for p in pkgs for t in re.findall(r"LicenseRef-[A-Za-z0-9.-]+", p["licenseDeclared"])})
    doc = {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{img['name']}-{img['version']}-{img['arch']}",
        "documentNamespace": f"https://github.com/Incremental-AS/loft/sbom/{safe_name(img['name'])}-{img['version']}-{img['arch']}",
        "creationInfo": {"created": inv["generated"], "creators": ["Tool: loft-gateway thirdparty.py"]},
        "packages": pkgs, "relationships": rels,
    }
    if refs:
        doc["hasExtractedLicensingInfos"] = [
            {"licenseId": r, "name": r[len("LicenseRef-"):],
             "extractedText": "See the copyright file of the package under licenses/ next to this SBOM."} for r in refs]
    return doc


def split_and(expr: str) -> list[str]:
    """Top-level AND operands of an SPDX expression (parentheses respected)."""
    parts, depth, cur = [], 0, []
    for tok in re.findall(r"\(|\)|[^\s()]+", expr):
        depth += tok == "("
        depth -= tok == ")"
        if tok == "AND" and depth == 0:
            parts.append(" ".join(cur))
            cur = []
        else:
            cur.append(tok)
    parts.append(" ".join(cur))
    out = []
    for p in parts:
        p = p.replace("( ", "(").replace(" )", ")").strip()
        while p.startswith("(") and p.endswith(")") and p.count("(") == 1:
            p = p[1:-1].strip()
        if " OR " in p or " WITH " in p:
            p = f"({p})"
        if p:
            out.append(p)
    return out


def label(inv: dict) -> str:
    """SPDX expression of what the image holds: the AND of every component's licence (deduplicated).
    Debian licence names with no SPDX id are left out; the SBOM and copyright files carry them."""
    seen: list[str] = []
    for it in inv["components"]:
        lic = it.get("license") or "NOASSERTION"
        if lic == "NOASSERTION":
            continue
        for part in split_and(lic):
            if "LicenseRef-" not in part and part not in seen:
                seen.append(part)
    return " AND ".join(sorted(seen, key=lambda s: (s.strip("()").lower(), s))) or "NOASSERTION"


def notices_md(inv: dict) -> str:
    img = inv["image"]
    lines = [f"# Third-party software in {img['name']} {img['version']} ({img['arch']})", "",
             "Licence and copyright files are under licenses/; the SBOM is sbom.spdx.json. The exact source of",
             "every component marked \"source\" is in the source bundle published with this release.", "",
             "| Component | Version | Licence | Source |", "|---|---|---|---|"]
    for it in inv["components"]:
        lines.append(f"| {it['name']} | {it.get('version', '')} | {it.get('license', '')} | "
                     f"{'source' if it.get('needs_source') else ''} |")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ sources

def fetch(url: str, dst: Path, retries: int = 4) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            tmp.replace(dst)
            return
        except Exception as e:  # network: retry, then give up
            if attempt == retries - 1:
                raise Refused(f"fetch {url}: {e}") from e
            time.sleep(3 * (attempt + 1))


def fetch_json(url: str) -> dict:
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 3:
                raise Refused(f"fetch {url}: {e}") from e
            time.sleep(3 * (attempt + 1))
    raise AssertionError


def cached(cache: Path, name: str, url: str, sha256: str | None = None, sha1: str | None = None) -> Path:
    p = cache / name
    def ok() -> bool:
        return p.is_file() and (not sha256 or sha256_file(p) == sha256) and (not sha1 or sha1_file(p) == sha1)
    if not ok():
        fetch(url, p)
        if not ok():
            raise Refused(f"{name}: checksum mismatch for {url}")
    return p


def leading_comment(data: bytes) -> bytes:
    """The comments at the top of a source file, before the first line of code (the licence)."""
    text = data.decode("utf-8", "replace")
    out, pos = [], 0
    while True:
        rest = text[pos:]
        ws = len(rest) - len(rest.lstrip())
        rest = rest.lstrip()
        if rest.startswith("/*") and "*/" in rest:
            end = rest.index("*/") + 2
        elif rest.startswith("//"):
            end = rest.find("\n") + 1 or len(rest)
        else:
            break
        out.append(rest[:end].rstrip())
        pos += ws + end
    return ("\n".join(out) + "\n").encode()


def extract_notices(archive: Path, dst: Path, extra: tuple[str, ...] = ()) -> list[str]:
    """Licence files at depth <= 2 of a tar archive (plus the members named in extra, for
    sources whose licence is only in a header), written under dst with flattened names."""
    out = []
    try:
        with tarfile.open(archive) as t:
            for m in t:
                parts = m.name.strip("/").split("/")
                wanted = m.name.strip("/") in extra or (len(parts) <= 3 and LICENSE_NAME.match(parts[-1]))
                if not m.isfile() or not wanted or m.size > 2_000_000:
                    continue
                f = t.extractfile(m)
                if f is None:
                    continue
                name = safe_name("__".join(parts[1:]) if len(parts) > 1 else parts[0])
                data = f.read()
                if m.name.strip("/") in extra:
                    data = leading_comment(data)
                dst.mkdir(parents=True, exist_ok=True)
                (dst / name).write_bytes(data)
                out.append(name)
    except (tarfile.TarError, OSError):
        pass
    return out


def place(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def debian_files(src: str, ver: str, cache: Path) -> list[tuple[str, Path]]:
    q = urllib.parse.quote
    info = fetch_json(f"{SNAPSHOT}/mr/package/{q(src)}/{q(ver)}/srcfiles?fileinfo=1")
    files = []
    for h in [r["hash"] for r in info.get("result", [])]:
        names = {fi["name"] for fi in info["fileinfo"].get(h, [])}
        if len(names) != 1:
            raise Refused(f"debian {src} {ver}: ambiguous file name for {h}: {names}")
        name = names.pop()
        files.append((name, cached(cache / "debian", name, f"{SNAPSHOT}/file/{h}", sha1=h)))
    dsc = [p for n, p in files if n.endswith(".dsc")]
    if len(dsc) != 1:
        raise Refused(f"debian {src} {ver}: expected one .dsc, got {len(dsc)}")
    # The .dsc names every other file with its SHA-256.
    text = dsc[0].read_text(errors="replace")
    block = text.split("Checksums-Sha256:", 1)[1] if "Checksums-Sha256:" in text else ""
    want = dict((m.group(3), m.group(1)) for m in re.finditer(r"^ ([0-9a-f]{64}) (\d+) (\S+)$", block, re.M))
    for n, p in files:
        if n.endswith(".dsc") or n.endswith(".asc"):
            continue
        if want.get(n) != sha256_file(p):
            raise Refused(f"debian {src} {ver}: {n} does not match the .dsc")
    return files


def build_sources(inv: dict, bundle: Path, comp: dict, cache: Path) -> dict:
    bundle.mkdir(parents=True, exist_ok=True)
    srcdefs = comp.get("source", {})
    index: dict[str, list[str]] = {}
    done: dict[str, list[str]] = {}
    for it in inv["components"]:
        if not it.get("needs_source") and not it.get("sources"):
            continue
        key = it.get("source_key")
        if it.get("problem") or not key:
            continue
        if key in index:
            continue
        paths: list[str] = []
        if it["kind"] == "deb":
            src, ver = it["debian_source"]
            dkey = f"{src}={ver}"
            if dkey not in done:
                rel = []
                for n, p in debian_files(src, ver, cache):
                    d = bundle / "debian" / safe_name(f"{src}_{ver}") / n
                    place(p, d)
                    rel.append(str(d.relative_to(bundle)))
                done[dkey] = rel
            paths = done[dkey]
        elif it["kind"] == "bundled":
            for s in it.get("sources", []):
                sd = srcdefs[s]
                fname = sd.get("filename") or Path(urllib.parse.urlparse(sd["url"]).path).name
                if s not in done:
                    p = cached(cache / "upstream", fname, sd["url"], sha256=sd["sha256"])
                    d = bundle / "upstream" / safe_name(s) / fname
                    place(p, d)
                    notes = extract_notices(p, bundle / "notices" / safe_name(s), tuple(sd.get("notice_files", [])))
                    done[s] = [str(d.relative_to(bundle))] + [f"notices/{safe_name(s)}/{n}" for n in notes]
                paths += [x for x in done[s] if not x.startswith("notices/")]
        else:
            continue  # a copyleft Python dist with compiled code: needs a [source] entry first
        if paths:
            index[key] = sorted(set(paths))
    sums = {}
    for p in sorted(bundle.rglob("*")):
        if p.is_file() and p.name not in ("SHA256SUMS", "sources.json"):
            sums[str(p.relative_to(bundle))] = sha256_file(p)
    (bundle / "sources.json").write_text(json.dumps(
        {"schema": 1, "image": inv["image"], "sources": index}, indent=1, sort_keys=True) + "\n")
    (bundle / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sorted(sums.items())))
    return index


# ------------------------------------------------------------------ check

def check(inv: dict, bundle: Path, image_label: str | None = None) -> list[str]:
    errs = list(inv.get("problems", []))
    if image_label is not None:
        want = label(inv)
        if set(split_and(image_label)) != set(split_and(want)):
            errs.append(f"the image's org.opencontainers.image.licenses label is {image_label!r}, but what it "
                        f"holds is {want!r}: put that in packaging/docker/Dockerfile")
    sj, ss = bundle / "sources.json", bundle / "SHA256SUMS"
    if not sj.is_file() or not ss.is_file():
        need = [it["id"] for it in inv["components"] if it.get("needs_source")]
        if need:
            errs.append(f"no source bundle at {bundle} (sources.json + SHA256SUMS), and {len(need)} "
                        f"components need their source, e.g. {', '.join(need[:5])}")
        return errs
    index = json.loads(sj.read_text()).get("sources", {})
    sums = {}
    for ln in ss.read_text().splitlines():
        h, _, n = ln.partition("  ")
        sums[n] = h
    verified: dict[str, bool] = {}

    def good(rel: str) -> bool:
        if rel not in verified:
            p = bundle / rel
            verified[rel] = (rel in sums and p.is_file() and inside(bundle, p) and sha256_file(p) == sums[rel])
        return verified[rel]

    for it in inv["components"]:
        if not it.get("needs_source") or it.get("problem"):
            continue
        files = index.get(it.get("source_key", ""))
        if not files:
            errs.append(f"{it['id']} ({it.get('license')}): no source in the bundle")
            continue
        bad = [f for f in files if not good(f)]
        if bad:
            errs.append(f"{it['id']}: source files missing or not matching SHA256SUMS: {', '.join(bad[:3])}")
    return errs


def lock_check(lock: Path, comp: dict) -> list[str]:
    pins = {norm(m.group(1)): m.group(2)
            for m in re.finditer(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)", lock.read_text(), re.M)}
    errs = []
    described = {}
    for w in comp["_wheels"].values():
        described.setdefault(norm(w["dist"]), set()).add(w["version"])
    for dist, versions in described.items():
        if dist in pins and pins[dist] not in versions:
            errs.append(f"{dist}=={pins[dist]} is locked but components.toml describes only "
                        f"{', '.join(sorted(versions))}: describe the new wheel's bundled libraries")
    return errs


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("rootfs", type=Path)
    c.add_argument("out", type=Path)
    c.add_argument("--components", type=Path, default=DEFAULT_COMPONENTS)
    c.add_argument("--name", default="loft-gateway")
    c.add_argument("--image-version", default="")
    c.add_argument("--arch", default="")
    s = sub.add_parser("sources")
    s.add_argument("inventory", type=Path)
    s.add_argument("bundle", type=Path)
    s.add_argument("--components", type=Path, default=DEFAULT_COMPONENTS)
    s.add_argument("--cache", type=Path, default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                   / "loft-third-party")
    k = sub.add_parser("check")
    k.add_argument("inventory", type=Path)
    k.add_argument("bundle", type=Path)
    k.add_argument("--label", help="the image's org.opencontainers.image.licenses label, to compare")
    lk = sub.add_parser("lock")
    lk.add_argument("lockfile", type=Path)
    lk.add_argument("--components", type=Path, default=DEFAULT_COMPONENTS)
    lb = sub.add_parser("label")
    lb.add_argument("inventory", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "collect":
            inv = collect(a.rootfs, a.out, load_components(a.components), a.name, a.image_version, a.arch)
            n = len(inv["components"])
            ns = sum(1 for i in inv["components"] if i.get("needs_source"))
            print(f"collect: {n} components, {ns} need their source, {len(inv['problems'])} problems -> {a.out}")
            for p in inv["problems"]:
                print(f"  PROBLEM {p}")
            return 1 if inv["problems"] else 0
        if a.cmd == "sources":
            inv = json.loads(a.inventory.read_text())
            idx = build_sources(inv, a.bundle, load_components(a.components), a.cache)
            print(f"sources: {len(idx)} components with source -> {a.bundle}")
            return 0
        if a.cmd == "check":
            errs = check(json.loads(a.inventory.read_text()), a.bundle, a.label)
            for e in errs:
                print(f"REFUSED {e}")
            if not errs:
                print("check: every component that needs its source has it in the bundle")
            return 1 if errs else 0
        if a.cmd == "lock":
            errs = lock_check(a.lockfile, load_components(a.components))
            for e in errs:
                print(f"REFUSED {e}")
            return 1 if errs else 0
        if a.cmd == "label":
            print(label(json.loads(a.inventory.read_text())))
            return 0
    except Refused as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
