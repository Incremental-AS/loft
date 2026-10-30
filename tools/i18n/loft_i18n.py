#!/usr/bin/env python3
"""Loft translations: one tool for the Mac, iPhone, Android and Windows apps, the website and the store listings.

English is the source everywhere; Norwegian (bokmål) is written natively and is not handled here. Every other
language is translated from English. The tool never translates by itself: it exports what is missing or stale for
a language, someone translates that file (see STYLE.md), and the tool imports the result after checking it.

    tools/i18n/loft_i18n.py status                     what is missing or stale, per language
    tools/i18n/loft_i18n.py export de [--all]          -> tools/i18n/work/de.todo.json (only missing/stale; --all = everything)
    tools/i18n/loft_i18n.py import de [file]           tools/i18n/work/de.done.json -> the app/site/store files
    tools/i18n/loft_i18n.py check                      every language complete, placeholders and markup intact

A todo file is {"lang", "style", "items": [{"id", "en", "nb", "note", "max", "kind"}]}. The done file is a flat
{"<id>": "<translation>"} object; a plural item ("kind": "plural") takes {"one": ..., "other": ...}.
state/<lang>.json remembers a hash of the English each translation was made from, so a changed English string comes back
as stale on the next export. Standard library only, Python 3.11+.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WORK = HERE / "work"
STATE = HERE / "state"  # <lang>.json each, so languages can be imported in parallel

# Language code (the Apple lproj name and the website/Windows file name) -> per-platform names.
LANGS = {
    "de": {"android": "de", "appstore": ["de-DE"], "play": ["de-DE"], "name": "German"},
    "es": {"android": "es", "appstore": ["es-ES", "es-MX"], "play": ["es-ES", "es-419"], "name": "Spanish (neutral, works for Spain and Latin America)"},
    "fr": {"android": "fr", "appstore": ["fr-FR", "fr-CA"], "play": ["fr-FR", "fr-CA"], "name": "French"},
    "it": {"android": "it", "appstore": ["it"], "play": ["it-IT"], "name": "Italian"},
    "nl": {"android": "nl", "appstore": ["nl-NL"], "play": ["nl-NL"], "name": "Dutch"},
    "pt-BR": {"android": "pt-rBR", "appstore": ["pt-BR"], "play": ["pt-BR"], "name": "Brazilian Portuguese"},
    "ja": {"android": "ja", "appstore": ["ja"], "play": ["ja-JP"], "name": "Japanese"},
    "ko": {"android": "ko", "appstore": ["ko"], "play": ["ko-KR"], "name": "Korean"},
    "zh-Hans": {"android": "b+zh+Hans", "appstore": ["zh-Hans"], "play": ["zh-CN"], "name": "Simplified Chinese"},
}
# Android plural quantities each language must carry (CLDR; lint wants "many" for these Romance languages).
ANDROID_QUANTITIES = {"ja": ["other"], "ko": ["other"], "zh-Hans": ["other"],
                      "fr": ["one", "many", "other"], "es": ["one", "many", "other"],
                      "it": ["one", "many", "other"], "pt-BR": ["one", "many", "other"]}


# ---------------------------------------------------------------------------------------------- formats
APPLE_PAIR = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)";\s*$')


def apple_unescape(s: str) -> str:
    return re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def apple_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def apple_read(path: Path) -> dict[str, str]:
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    out = {}
    for line in text.splitlines():
        m = APPLE_PAIR.match(line.strip())
        if m:
            out[apple_unescape(m.group(1))] = apple_unescape(m.group(2))
    return out


def apple_write(path: Path, pairs: dict[str, str], header: str):
    lines = [f"/* {header} */", ""]
    lines += [f'"{apple_escape(k)}" = "{apple_escape(v)}";' for k, v in pairs.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def android_unescape(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def android_escape(s: str) -> str:
    s = s.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")
    if s[:1] in ("@", "?"):
        s = "\\" + s
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def android_read(path: Path) -> list[tuple[str, str, object]]:
    """[(kind, name, value)]: kind 'string' (value str) or 'plural' (value {quantity: str}); translatable only."""
    out = []
    for el in ET.parse(path).getroot():
        if el.get("translatable") == "false":
            continue
        if el.tag == "string":
            out.append(("string", el.get("name"), android_unescape("".join(el.itertext()))))
        elif el.tag == "plurals":
            out.append(("plural", el.get("name"),
                        {i.get("quantity"): android_unescape("".join(i.itertext())) for i in el}))
    return out


def toml_flatten(obj, prefix="") -> dict[str, object]:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(toml_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(toml_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def toml_set(obj, path: str, value):
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    for p in parts[:-1]:
        obj = obj[int(p[1:-1])] if p.startswith("[") else obj[p]
    last = parts[-1]
    if last.startswith("["):
        obj[int(last[1:-1])] = value
    else:
        obj[last] = value


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid TOML basic string
    if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    raise ValueError(f"can't write {type(v)} inline")


def toml_dump(obj: dict, header: str) -> str:
    lines = [f"# {line}" for line in header.splitlines()]

    def is_table(v):
        return isinstance(v, dict)

    def is_aot(v):
        return isinstance(v, list) and v and all(isinstance(x, dict) for x in v)

    def emit(table: dict, name: str, aot: bool = False):
        if name:
            lines.append("")
            lines.append(f"[[{name}]]" if aot else f"[{name}]")
        for k, v in table.items():
            if not is_table(v) and not is_aot(v):
                lines.append(f"{k} = {toml_value(v)}")
        for k, v in table.items():
            key = f"{name}.{k}" if name else k
            if is_table(v):
                emit(v, key)
            elif is_aot(v):
                for item in v:
                    emit(item, key, aot=True)

    emit(obj, "")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------- catalogs
# Each catalog yields source items {id: (kind, en, nb, note, max)} and writes a language's file from {id: value}.
class Catalog:
    key = ""
    note = ""

    def items(self) -> dict[str, dict]:
        raise NotImplementedError

    def read(self, lang: str) -> dict[str, object]:
        raise NotImplementedError

    def write(self, lang: str, values: dict[str, object]):
        raise NotImplementedError

    def available(self) -> bool:
        return True


class AppleStrings(Catalog):
    """A Localizable.strings whose keys are the English text (nb.lproj lists every translatable key)."""

    def __init__(self, key, folder, note):
        self.key, self.folder, self.note = key, ROOT / folder, note

    def items(self):
        nb = apple_read(self.folder / "nb.lproj/Localizable.strings")
        return {f"{self.key}:{k}": {"en": k, "nb": v} for k, v in nb.items()}

    def path(self, lang):
        return self.folder / f"{lang}.lproj/Localizable.strings"

    def read(self, lang):
        p = self.path(lang)
        return {f"{self.key}:{k}": v for k, v in apple_read(p).items()} if p.exists() else {}

    def write(self, lang, values):
        order = list(apple_read(self.folder / "nb.lproj/Localizable.strings"))
        pairs = {k: values[f"{self.key}:{k}"] for k in order if f"{self.key}:{k}" in values}
        apple_write(self.path(lang), pairs, f"Loft, {LANGS[lang]['name']}. Generated by tools/i18n/loft_i18n.py "
                    "from the English keys; edit through the tool (export/import), not by hand.")


class ApplePlist(Catalog):
    """InfoPlist.strings: en.lproj holds the English values."""

    def __init__(self, key, folder, note):
        self.key, self.folder, self.note = key, ROOT / folder, note

    def items(self):
        en = apple_read(self.folder / "en.lproj/InfoPlist.strings")
        nb = apple_read(self.folder / "nb.lproj/InfoPlist.strings")
        return {f"{self.key}:{k}": {"en": v, "nb": nb.get(k, ""), "verbatim": v == "Loft"} for k, v in en.items()}

    def path(self, lang):
        return self.folder / f"{lang}.lproj/InfoPlist.strings"

    def read(self, lang):
        p = self.path(lang)
        return {f"{self.key}:{k}": v for k, v in apple_read(p).items()} if p.exists() else {}

    def write(self, lang, values):
        order = list(apple_read(self.folder / "en.lproj/InfoPlist.strings"))
        pairs = {k: values[f"{self.key}:{k}"] for k in order if f"{self.key}:{k}" in values}
        apple_write(self.path(lang), pairs, f"Loft, {LANGS[lang]['name']}. Generated by tools/i18n/loft_i18n.py.")


class AndroidStrings(Catalog):
    key = "android"
    note = "Loft for Android (phone app: pairing, browsing the NAS, Files app provider, share sheet, camera upload)."
    res = ROOT / "android/app/src/main/res"

    def items(self):
        nb = {n: v for _, n, v in android_read(self.res / "values-nb/strings.xml")}
        out = {}
        for kind, name, v in android_read(self.res / "values/strings.xml"):
            out[f"android:{name}"] = {"en": v, "nb": nb.get(name, ""), "kind": kind}
        return out

    def path(self, lang):
        return self.res / f"values-{LANGS[lang]['android']}/strings.xml"

    def read(self, lang):
        p = self.path(lang)
        if not p.exists():
            return {}
        out = {}
        for kind, name, v in android_read(p):
            out[f"android:{name}"] = {"one": v.get("one", v.get("other")), "other": v["other"]} if kind == "plural" else v
        return out

    def write(self, lang, values):
        lines = ['<?xml version="1.0" encoding="utf-8"?>',
                 f"<!-- Loft, {LANGS[lang]['name']}. Generated by tools/i18n/loft_i18n.py; edit through the tool. -->",
                 "<resources>"]
        for kind, name, _ in android_read(self.res / "values/strings.xml"):
            v = values.get(f"android:{name}")
            if v is None:
                continue
            if kind == "plural":
                lines.append(f'    <plurals name="{name}">')
                for q in ANDROID_QUANTITIES.get(lang, ["one", "other"]):
                    text = v["one"] if q == "one" else v["other"]
                    lines.append(f'        <item quantity="{q}">{android_escape(text)}</item>')
                lines.append("    </plurals>")
            else:
                lines.append(f'    <string name="{name}">{android_escape(v)}</string>')
        lines.append("</resources>")
        self.path(lang).parent.mkdir(parents=True, exist_ok=True)
        self.path(lang).write_text("\n".join(lines) + "\n", encoding="utf-8")


class JsonCatalog(Catalog):
    """A flat {key: text} JSON per language (Windows app, store listings)."""

    def __init__(self, key, folder, source, target, note, limits=None, verbatim=(), bytes_limits=()):
        self.key, self.folder, self.source, self.target = key, ROOT / folder, source, target
        self.note, self.limits, self.verbatim, self.bytes_limits = note, limits or {}, set(verbatim), set(bytes_limits)

    def available(self):
        return (self.folder / self.source).exists()

    def items(self):
        en = json.loads((self.folder / self.source).read_text(encoding="utf-8"))
        nbp = self.folder / self.target("nb")
        nb = json.loads(nbp.read_text(encoding="utf-8")) if nbp.exists() else {}
        out = {}
        for k, v in en.items():
            if not isinstance(v, str) or k.startswith("_"):
                continue
            out[f"{self.key}:{k}"] = {"en": v, "nb": nb.get(k, ""), "max": self.limits.get(k),
                                      "max_bytes": k in self.bytes_limits,
                                      "verbatim": k in self.verbatim or not v.strip()}
        return out

    def read(self, lang):
        p = self.folder / self.target(lang)
        if not p.exists():
            return {}
        return {f"{self.key}:{k}": v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if not k.startswith("_")}

    def write(self, lang, values):
        en = json.loads((self.folder / self.source).read_text(encoding="utf-8"))
        out = {"_note": f"{LANGS[lang]['name']}. Generated by tools/i18n/loft_i18n.py from {self.source}; edit through the tool."}
        for k, v in en.items():
            if f"{self.key}:{k}" in values:
                out[k] = values[f"{self.key}:{k}"]
        p = self.folder / self.target(lang)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# Website values that are not words: URLs, paths, file names, versions, hashes, hreflang codes.
NOT_WORDS = re.compile(r"^(https?://\S+|/\S*|[\w.-]+\.(?:sh|deb|dmg|apk|exe|msi|zip|json|toml)|[A-Za-z]{2}(-[A-Za-z]+)?|"
                       r"\{[A-Z_]+\}|[0-9a-f]{7,64}|v?\d[\d.]*)$")


# Website keys whose values are HTML ids / #anchors / CSS class suffixes, never shown as words.
IDENTIFIER_KEYS = {"id", "status"}


class WebsiteCopy(Catalog):
    key = "web"
    note = ("loft.incremental.no, the product website (marketing + docs). Values may hold HTML (<p>, <code>, <a>, "
            "<strong>, <em>, <li>): keep every tag and attribute exactly, translate only the text between tags. "
            "Keep link paths as they are in English (/download/#android): the build adds the language prefix.")
    content = ROOT / "website/content"

    def items(self):
        en = toml_flatten(tomllib.loads((self.content / "en.toml").read_text(encoding="utf-8")))
        nb = toml_flatten(tomllib.loads((self.content / "no.toml").read_text(encoding="utf-8")))
        out = {}
        for k, v in en.items():
            if not isinstance(v, str) or k.startswith("release."):
                continue
            out[f"web:{k}"] = {"en": v, "nb": nb.get(k, "") if isinstance(nb.get(k), str) else "",
                               "verbatim": not v.strip() or bool(NOT_WORDS.match(v.strip()))
                               or k.rsplit(".", 1)[-1] in IDENTIFIER_KEYS}
        return out

    def path(self, lang):
        return self.content / f"{lang}.toml"

    def read(self, lang):
        p = self.path(lang)
        if not p.exists():
            return {}
        return {f"web:{k}": v for k, v in toml_flatten(tomllib.loads(p.read_text(encoding="utf-8"))).items()
                if isinstance(v, str)}

    def write(self, lang, values):
        tree = tomllib.loads((self.content / "en.toml").read_text(encoding="utf-8"))
        for k, v in toml_flatten(tree).items():
            if isinstance(v, str) and f"web:{k}" in values:
                toml_set(tree, k, values[f"web:{k}"])
        header = (f"{LANGS[lang]['name']} copy for loft.incremental.no, translated from en.toml.\n"
                  "Generated by tools/i18n/loft_i18n.py: change the English, then export/translate/import.")
        self.path(lang).write_text(toml_dump(tree, header), encoding="utf-8")


APPSTORE_LIMITS = {"name": 30, "subtitle": 30, "promotional_text": 170, "description": 4000, "keywords": 100,
                   "whats_new": 4000}
PLAY_LIMITS = {"title": 30, "short_description": 80, "full_description": 4000, "release_notes": 500}

CATALOGS: list[Catalog] = [
    AppleStrings("mac", "apple/App", "Loft for Mac: menu bar app and Finder integration (File Provider), "
                 "pairing, sign-in, settings. Mac UI terms: Finder, menu bar, System Settings."),
    AppleStrings("ios", "apple/iOS/App", "Loft for iPhone: pairing, sign-in, browsing the NAS, camera, "
                 "settings. iPhone UI terms: the Files app, Save to Files, Share."),
    AppleStrings("ios-share", "apple/iOS/Share", "Loft for iPhone, the Share extension (\"Save to Loft\" from another app's share sheet)."),
    ApplePlist("mac-plist", "apple/App", "Loft for Mac, Info.plist: the Local Network permission prompt macOS shows."),
    ApplePlist("mac-fp-plist", "apple/FileProvider", "Loft for Mac, Info.plist of the Finder extension (same Local Network prompt)."),
    ApplePlist("ios-plist", "apple/iOS/App", "Loft for iPhone, Info.plist: home screen name and the iOS permission prompts."),
    ApplePlist("ios-share-plist", "apple/iOS/Share", "Loft for iPhone, Info.plist of the Share extension (its name in the share sheet)."),
    ApplePlist("ios-fp-plist", "apple/iOS/FileProviderStrings", "Loft for iPhone, Info.plist of the File Provider (its name in the Files app)."),
    AndroidStrings(),
    JsonCatalog("windows", "windows/src/Loft.App/Strings", "en.json", lambda l: f"{l}.json",
                "Loft for Windows: tray app and File Explorer integration. Placeholders are {name} in braces. "
                "Keys ending .one/.other are the singular/plural forms of one message; languages without "
                "plural forms use the same text in both."),
    WebsiteCopy(),
    JsonCatalog("appstore", "store/appstore", "en-US.json", lambda l: f"{l}.json",
                "App Store listing for Loft for iPhone. Keywords: comma-separated, no spaces after commas, the "
                "words people in this language would search for (not a literal translation), max 100 BYTES in UTF-8. "
                "Name: keep \"Loft by Incremental\" untranslated.",
                limits=APPSTORE_LIMITS, verbatim=("support_url", "privacy_policy_url", "marketing_url"),
                bytes_limits=("keywords",)),
    JsonCatalog("play", "store/play", "en-US.json", lambda l: f"{l}.json",
                "Google Play listing for Loft for Android. Plain text, line breaks and • bullets only. Title: keep "
                "\"Loft by Incremental\" untranslated. No promotional or price words in title or short description "
                "(Play policy). Release notes max 500.",
                limits=PLAY_LIMITS),
]


# ---------------------------------------------------------------------------------------------- checks
FORMAT = re.compile(r"%(?:\d+\$)?(?:@|lld|ld|d|s|f)|\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}|\{\{[^}]*\}\}|\{[A-Z_]+\}")
TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
HREF = re.compile(r'href="([^"]*)"')
CODE = re.compile(r"<code>(.*?)</code>", re.S)
URL = re.compile(r"https?://[^\s<>\"')]+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:no|com|org|io)\b[^\s<>\"')]*")


def problems(item: dict, value) -> list[str]:
    en, out = item["en"], []
    if item.get("kind") == "plural":
        if not isinstance(value, dict) or set(value) != {"one", "other"}:
            return ['plural needs {"one": ..., "other": ...}']
        for q in ("one", "other"):
            out += [f"[{q}] {p}" for p in problems({"en": en.get(q, en["other"])}, value[q])]
        return out
    if not isinstance(value, str) or not value.strip():
        return ["empty or not a string"]
    for name, rx in (("placeholders", FORMAT), ("tags", TAG), ("links", HREF), ("<code>", CODE)):
        a, b = sorted(rx.findall(en)), sorted(rx.findall(value))
        if a != b:
            out.append(f"{name} differ: {a} vs {b}")
    for u in {u.rstrip(".,;:!?") for u in URL.findall(en)}:  # a sentence's full stop is not part of the URL
        if u not in value:
            out.append(f"URL/host missing: {u}")
    if en.startswith("DRAFT: ") != value.startswith("DRAFT: "):
        out.append('"DRAFT: " prefix must stay')
    if item.get("max"):
        n = len(value.encode()) if item.get("max_bytes") else len(value)
        if n > item["max"]:
            out.append(f"too long: {n} > {item['max']}{' bytes' if item.get('max_bytes') else ''}")
    return out


# ---------------------------------------------------------------------------------------------- commands
def sha(en) -> str:
    return hashlib.sha1(json.dumps(en, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def load_state() -> dict:
    return {p.stem: json.loads(p.read_text()) for p in STATE.glob("*.json")} if STATE.exists() else {}


def all_items() -> dict[str, tuple[Catalog, dict]]:
    out = {}
    for c in CATALOGS:
        if c.available():
            for k, item in c.items().items():
                out[k] = (c, item)
    return out


def current(lang) -> dict:
    out = {}
    for c in CATALOGS:
        if c.available():
            out.update(c.read(lang))
    return out


def pending(lang, items, state) -> dict[str, str]:
    """id -> 'missing' | 'stale' for every non-verbatim item that needs translating."""
    have, seen, out = current(lang), state.get(lang, {}), {}
    for k, (_, item) in items.items():
        if item.get("verbatim"):
            continue
        if k not in have:
            out[k] = "missing"
        elif seen.get(k) != sha(item["en"]):
            out[k] = "stale"
    return out


def cmd_status():
    items, state = all_items(), load_state()
    print(f"{len(items)} source strings in {sum(c.available() for c in CATALOGS)} catalogs")
    for lang in LANGS:
        p = pending(lang, items, state)
        by = {}
        for k, why in p.items():
            by.setdefault(k.split(":")[0], [0, 0])[why == "stale"] += 1
        print(f"  {lang:8} {len(p):4} to do" + ("  " + ", ".join(f"{c} {m}+{s}s" for c, (m, s) in by.items()) if p else ""))


def cmd_export(lang, everything=False):
    items, state = all_items(), load_state()
    todo = pending(lang, items, state)
    if everything:
        todo = {k: "all" for k, (_, it) in items.items() if not it.get("verbatim")}
    out = {"lang": lang, "language": LANGS[lang]["name"], "style": "tools/i18n/STYLE.md", "catalogs": {}, "items": []}
    have = current(lang)
    for k in todo:
        c, it = items[k]
        out["catalogs"][c.key] = c.note
        row = {"id": k, "en": it["en"]}
        for f in ("nb", "kind", "max", "max_bytes"):
            if it.get(f):
                row[f] = it[f]
        if todo[k] == "stale" and k in have:
            row["previous"] = have[k]
        out["items"].append(row)
    WORK.mkdir(exist_ok=True)
    p = WORK / f"{lang}.todo.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{p.relative_to(ROOT)}: {len(out['items'])} items")


def cmd_import(lang, path=None):
    items, state = all_items(), load_state()
    done = json.loads(Path(path or WORK / f"{lang}.done.json").read_text(encoding="utf-8"))
    bad, values = [], current(lang)
    for k, v in done.items():
        if k not in items:
            bad.append(f"{k}: unknown id")
            continue
        for p in problems(items[k][1], v):
            bad.append(f"{k}: {p}")
        values[k] = v
    if bad:
        print("\n".join(bad))
        sys.exit(f"{len(bad)} problem(s); nothing written")
    for k, (_, it) in items.items():  # verbatim items: always the English
        if it.get("verbatim"):
            values[k] = it["en"]
    for c in CATALOGS:
        if c.available():
            mine = {k: v for k, v in values.items() if k.split(":", 1)[0] == c.key and k in items}
            if mine:
                c.write(lang, mine)
    st = state.setdefault(lang, {})
    for k in done:
        st[k] = sha(items[k][1]["en"])
    STATE.mkdir(exist_ok=True)
    (STATE / f"{lang}.json").write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    left = pending(lang, items, state)
    print(f"{lang}: imported {len(done)}; still to do {len(left)}")


def cmd_check():
    items, state, fails = all_items(), load_state(), 0
    for lang in LANGS:
        have = current(lang)
        if not have:
            continue
        for k, (_, it) in items.items():
            if k not in have:
                print(f"{lang} {k}: missing")
                fails += 1
                continue
            if it.get("verbatim"):
                if have[k] != it["en"]:
                    print(f"{lang} {k}: must stay as in English")
                    fails += 1
                continue
            for p in problems(it, have[k]):
                print(f"{lang} {k}: {p}")
                fails += 1
        stale = [k for k, why in pending(lang, items, state).items() if why == "stale"]
        if stale:
            print(f"{lang}: {len(stale)} stale (English changed since translation), e.g. {stale[0]}")
    print("OK" if not fails else f"{fails} problem(s)")
    sys.exit(1 if fails else 0)


def main(argv):
    if not argv or argv[0] not in ("status", "export", "import", "check"):
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    if cmd in ("export", "import") and (not rest or rest[0] not in LANGS):
        sys.exit(f"language must be one of {', '.join(LANGS)}")
    if cmd == "status":
        cmd_status()
    elif cmd == "export":
        cmd_export(rest[0], "--all" in rest)
    elif cmd == "import":
        cmd_import(rest[0], rest[1] if len(rest) > 1 else None)
    else:
        cmd_check()


if __name__ == "__main__":
    main(sys.argv[1:])
