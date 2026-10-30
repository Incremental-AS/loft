#!/usr/bin/env python3
"""Build loft.incremental.no: templates/ + content/<lang>.toml + static/ -> dist/.

Standard library only (Python 3.11+). No JavaScript toolchain, no network.

    python3 build.py            # draft build: unfinished copy is outlined on the page
    python3 build.py --release  # refuses to build while any copy is still marked DRAFT

Template syntax (small on purpose):
    {{a.b}}                  copy or page value, trusted HTML (copy is ours)
    {{attr:a.b}}             same, HTML-escaped for attributes and <title>
    {{#each a.b}}..{{/each}} loop over a TOML array of tables; inside, {{it.x}}
    {{#if a.b}}..{{/if}}     render when the value is set and not empty
    {{#unless a.b}}..{{/unless}}  render when it isn't (missing, false or empty)
    {{> name}}               templates/partials/name.html
    {{link:slug}}            URL of a page in the current language ("home" = front page)
    {{asset:/css/site.css}}  static file URL with a content hash for cache-busting

Copy that starts with "DRAFT: " is a placeholder: it renders inside <span class="draft"> so it shows,
and --release fails on it. So does a {CURLY_CAPS} token ({VERSION}, {STATUS} ...): a value that isn't decided yet.
And so does any entry in [release].hold in content/en.toml: copy that reads finished but promises something that
isn't true yet (a command that doesn't exist, a page for a repo that isn't public). Remove the entry when it is.

/pair is built once, as /pair.html (served at /pair): pairing links carry no language prefix or slash. Its template sees both copies
as en.* and no.*, and site.js shows the Norwegian one when the browser asks for nb, nn or no.

App links: docs/aasa.json and android/docs/assetlinks.json (repo root) are copied to /.well-known/ when present.
"""
import hashlib
import html
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SITE = "https://loft.incremental.no"
LANGS = {"en": {"prefix": "", "html_lang": "en"}, "no": {"prefix": "/no", "html_lang": "nb"}}
# slug -> output path below the language prefix. "home" is the front page.
PAGES = ["home", "how-it-works", "download", "gateway", "compatibility",
         "privacy", "support", "security", "source", "about-the-name", "pair", "404"]
ONE_LANG = {"pair"}          # built once, at the root, with both languages on the page
DRAFT = "DRAFT: "
# /.well-known files written by the app streams (they may not exist yet).
WELL_KNOWN = {
    "apple-app-site-association": REPO / "docs/aasa.json",
    "assetlinks.json": REPO / "android/docs/assetlinks.json",
}
PLACEHOLDER = re.compile(r"\{[A-Z][A-Z0-9_]*\}")

# The flag emoji in the copy renders as an inline SVG: Windows has no flag emoji and shows the letters N O.
FLAG_NO = "\U0001F1F3\U0001F1F4"
FLAG_NO_SVG = ('<svg class="flag" role="img" aria-label="{label}" viewBox="0 0 22 16" width="22" height="16">'
               '<path fill="#ba0c2f" d="M0 0h22v16H0z"/><path fill="#fff" d="M6 0h4v16H6zM0 6h22v4H0z"/>'
               '<path fill="#00205b" d="M7 0h2v16H7zM0 7h22v2H0z"/></svg>')
# Inline code up to this many characters stays on one line; longer code may still break anywhere.
SHORT_CODE = 30
INLINE_CODE = re.compile(r"(<pre\b.*?</pre>)|<code>([^<]*)</code>", re.S)

TAG = re.compile(r"\{\{(#each|#if|#unless|/each|/if|/unless|>|attr:|link:|asset:)?\s*([^}]*?)\s*\}\}")


class BuildError(Exception):
    pass


def page_url(lang, slug):
    prefix = "" if slug in ONE_LANG else LANGS[lang]["prefix"]
    if slug == "home":
        return prefix + "/"
    if slug == "404":
        return prefix + "/404.html"
    if slug in ONE_LANG:
        return f"/{slug}"
    return f"{prefix}/{slug}/"


def out_path(lang, slug):
    base = ROOT / "dist" / ("" if slug in ONE_LANG else LANGS[lang]["prefix"].lstrip("/"))
    if slug == "home":
        return base / "index.html"
    if slug == "404":
        return base / "404.html"
    if slug in ONE_LANG:
        return base / f"{slug}.html"
    return base / slug / "index.html"


class Renderer:
    def __init__(self, lang, copy, page_ctx, release):
        self.lang = lang
        self.scopes = [{**copy, **page_ctx}]
        self.release = release
        self.drafts = []
        self.hashes = {}

    def lookup(self, path):
        head, *rest = path.split(".")
        for scope in reversed(self.scopes):
            if head in scope:
                val = scope[head]
                break
        else:
            raise BuildError(f"[{self.lang}] unknown key {path!r}")
        for part in rest:
            if not isinstance(val, dict) or part not in val:
                raise BuildError(f"[{self.lang}] unknown key {path!r}")
            val = val[part]
        return val

    def text(self, path, attr):
        val = self.lookup(path)
        if not isinstance(val, (str, int, float)):
            raise BuildError(f"[{self.lang}] {path!r} is not text")
        val = str(val)
        if path == "content":           # the page body, already rendered
            return val
        # The marker may sit right after opening tags: "<p>DRAFT: ..."
        m = re.match(r"((?:<[^>]+>)*)" + re.escape(DRAFT), val)
        draft = m is not None
        if draft:
            val = m.group(1) + val[m.end():]
            self.drafts.append(path)
        holes = PLACEHOLDER.findall(val)
        self.drafts += [f"{path} {h}" for h in holes]
        if attr:
            return html.escape(re.sub(r"<[^>]+>", "", val), quote=True)
        if FLAG_NO in val:
            # /pair renders both copies (en.*, no.*): label the flag in the language of the copy it sits in.
            head = path.split(".")[0]
            label = self.lookup((head + "." if head in LANGS else "") + "ui.flag_norway")
            val = val.replace(FLAG_NO, FLAG_NO_SVG.format(label=html.escape(label, quote=True)))
        if holes:
            val = PLACEHOLDER.sub(lambda m: f'<span class="draft">{m.group(0)}</span>', val)
        if not draft:
            return val
        tag = "div" if re.search(r"<(p|ul|ol|div|h[1-6]|pre|table)\b", val) else "span"
        return f'<{tag} class="draft">{val}</{tag}>'

    def asset(self, url):
        if url not in self.hashes:
            f = ROOT / "static" / url.lstrip("/")
            if not f.is_file():
                raise BuildError(f"missing static file {url}")
            self.hashes[url] = hashlib.sha256(f.read_bytes()).hexdigest()[:10]
        return f"{url}?v={self.hashes[url]}"

    def render(self, tpl):
        out, pos = [], 0
        while True:
            m = TAG.search(tpl, pos)
            if not m:
                out.append(tpl[pos:])
                return "".join(out)
            out.append(tpl[pos:m.start()])
            kind, arg = m.group(1) or "", m.group(2)
            pos = m.end()
            if kind in ("#each", "#if", "#unless"):
                body, pos = self.block(tpl, pos, kind[1:])
                if kind in ("#if", "#unless"):
                    try:
                        val = self.lookup(arg)
                    except BuildError:      # an optional key that isn't there
                        val = None
                    if bool(val) == (kind == "#if"):
                        out.append(self.render(body))
                else:
                    val = self.lookup(arg)
                    if not isinstance(val, list):
                        raise BuildError(f"[{self.lang}] {arg!r} is not a list")
                    for i, item in enumerate(val, 1):
                        self.scopes.append({"it": {**item, "n": i} if isinstance(item, dict) else item})
                        out.append(self.render(body))
                        self.scopes.pop()
            elif kind in ("/each", "/if", "/unless"):
                raise BuildError(f"stray {{{{{kind}}}}}")
            elif kind == ">":
                out.append(self.render((ROOT / "templates/partials" / f"{arg}.html").read_text()))
            elif kind == "link:":
                if arg not in PAGES:
                    raise BuildError(f"link to unknown page {arg!r}")
                out.append(page_url(self.lang, arg))
            elif kind == "asset:":
                out.append(self.asset(arg))
            else:
                out.append(self.text(arg, attr=(kind == "attr:")))

    @staticmethod
    def block(tpl, pos, name):
        """Body of a block up to its matching close tag, and the position after it."""
        depth, i = 1, pos
        opener = re.compile(r"\{\{(#" + name + r"\b|/" + name + r")[^}]*\}\}")
        while True:
            m = opener.search(tpl, i)
            if not m:
                raise BuildError(f"unclosed {{{{#{name}}}}}")
            depth += 1 if m.group(1).startswith("#") else -1
            if depth == 0:
                return tpl[pos:m.start()], m.end()
            i = m.end()


def keep_short_code_together(doc):
    """<code class="short"> on inline code of up to SHORT_CODE characters, so it doesn't break at a space or '='."""
    def mark(m):
        if m.group(1) or len(html.unescape(m.group(2))) > SHORT_CODE:
            return m.group(0)
        return f'<code class="short">{m.group(2)}</code>'
    return INLINE_CODE.sub(mark, doc)


def check_links(dist):
    """Every internal href/src in dist/ must hit a file, and every #fragment an id on that page.
    Copy carries its own links (/privacy/, /no/download/#android), so typos are caught here."""
    ids, refs = {}, []
    for f in dist.rglob("*.html"):
        doc = f.read_text()
        ids[f] = set(re.findall(r'\sid="([^"]+)"', doc))
        for attr, url in re.findall(r'\s(href|src|srcset)="([^"]+)"', doc):
            for u in ([x.strip().split(" ")[0] for x in url.split(",")] if attr == "srcset" else [url]):
                if u.startswith("/") and not u.startswith("//"):
                    refs.append((f, u))
                elif u.startswith("#") and len(u) > 1:
                    refs.append((f, f"{'/' + str(f.relative_to(dist))}{u}"))
    bad = []
    for f, u in refs:
        path, _, frag = u.partition("#")
        path = path.split("?")[0]
        t = dist / path.lstrip("/")
        if t.is_dir():
            t = t / "index.html"
        elif not t.is_file() and t.with_name(t.name + ".html").is_file():
            t = t.with_name(t.name + ".html")      # /pair -> pair.html, as Workers assets serve it
        if not t.is_file():
            bad.append(f"{f.relative_to(dist)}: {u} (no such file)")
        elif frag and t.suffix == ".html" and frag not in ids.get(t, set()):
            bad.append(f"{f.relative_to(dist)}: {u} (no id {frag!r})")
    if bad:
        raise BuildError("broken links:\n  " + "\n  ".join(sorted(set(bad))))


def write_well_known(dist):
    out = dist / ".well-known"
    done = []
    for name, src in WELL_KNOWN.items():
        if not src.is_file():
            continue
        try:
            json.loads(src.read_text())
        except ValueError as e:
            raise BuildError(f"{src} is not valid JSON: {e}")
        out.mkdir(exist_ok=True)
        shutil.copyfile(src, out / name)
        done.append(name)
    return done


def build(release):
    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(ROOT / "static", dist)
    layout = (ROOT / "templates/layout.html").read_text()
    copies = {lang: tomllib.loads((ROOT / f"content/{lang}.toml").read_text()) for lang in LANGS}
    drafts = []
    for lang, copy in copies.items():
        other = "no" if lang == "en" else "en"
        for slug in PAGES:
            if slug in ONE_LANG and lang != "en":
                continue
            meta = copy["pages"].get(slug)
            if meta is None:
                raise BuildError(f"[{lang}] no [pages.{slug}] in content/{lang}.toml")
            ctx = {
                "lang": lang,
                "html_lang": LANGS[lang]["html_lang"],
                "slug": slug,
                "page": meta,
                "url_self": SITE + page_url(lang, slug),
                "url_en": SITE + page_url("en", slug),
                "url_no": SITE + page_url("no", slug),
                "url_other": page_url(other, slug),
                "og_image": f"{SITE}/img/og/og-{lang}.jpg",
                "is_home": slug == "home",
                "noindex": slug in ("404", "pair"),
            }
            if slug in ONE_LANG:
                # Both languages on one page; links into the rest of the site per language.
                for l, c in copies.items():
                    ctx[l] = {**c, "url": {s: page_url(l, s) for s in PAGES}}
            r = Renderer(lang, copy, ctx, release)
            body = r.render((ROOT / f"templates/pages/{slug}.html").read_text())
            r.scopes[0]["content"] = body
            tpl = ROOT / f"templates/layout-{slug}.html"
            doc = r.render(tpl.read_text() if tpl.exists() else layout)
            if slug not in {"home", "404"} | ONE_LANG:    # mark the current page in the header and footer menus
                cur = page_url(lang, slug)
                doc = doc.replace(f'<li><a href="{cur}">', f'<li><a href="{cur}" aria-current="page">')
            doc = keep_short_code_together(doc)
            drafts += [f"{lang}/{slug}: {d}" for d in dict.fromkeys(r.drafts)]
            f = out_path(lang, slug)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(doc)
    check_links(dist)
    well_known = write_well_known(dist)
    holds = copies["en"].get("release", {}).get("hold", [])
    urls = [SITE + page_url(lang, s) for lang in LANGS for s in PAGES if s not in ("404", "pair")]
    (dist / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls) + "</urlset>\n")
    if drafts:
        print(f"{len(drafts)} draft strings:" if release else f"draft build, {len(drafts)} draft strings")
        if release:
            print("\n".join("  " + d for d in drafts))
    if holds:
        print(f"{len(holds)} release holds:")
        print("\n".join("  " + h for h in holds))
    if release and (drafts or holds):
        shutil.rmtree(dist)
        raise BuildError("release build refused: copy is still marked DRAFT or held")
    n = sum(1 if s in ONE_LANG else len(LANGS) for s in PAGES)
    print(f"built {n} pages into {dist}")
    print(f".well-known: {well_known or 'none (docs/aasa.json, android/docs/assetlinks.json not present)'}")


if __name__ == "__main__":
    try:
        build(release="--release" in sys.argv[1:])
    except BuildError as e:
        sys.exit(f"build.py: {e}")
