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

Languages: LANGUAGES below. A language is built when content/<code>.toml exists, and every copy file must have
exactly en.toml's keys (the build names the missing and extra ones). English is at the root, the others under their prefix.
Links inside copy are written as English paths (/download/#android) or already prefixed (/no/download/#android):
the build prefixes root-relative links to site pages with the page's language, and leaves /pair, static files,
/.well-known, other languages' links and external URLs alone.

/pair is built once, as /pair.html (served at /pair): pairing links carry no language prefix or slash. It carries every
built language: English shows, the others are <details> to open (without JavaScript), and site.js shows only the
best match for navigator.languages.

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
# Code (= content/<code>.toml) -> URL prefix, <html lang> and hreflang, and the name in its own language (the picker).
# Order here is the order in the picker. Only codes with a content file are built.
LANGUAGES = {
    "en": {"prefix": "", "html_lang": "en", "name": "English"},
    "no": {"prefix": "/no", "html_lang": "nb", "name": "Norsk"},
    "de": {"prefix": "/de", "html_lang": "de", "name": "Deutsch"},
    "es": {"prefix": "/es", "html_lang": "es", "name": "Español"},
    "fr": {"prefix": "/fr", "html_lang": "fr", "name": "Français"},
    "it": {"prefix": "/it", "html_lang": "it", "name": "Italiano"},
    "nl": {"prefix": "/nl", "html_lang": "nl", "name": "Nederlands"},
    "pt-BR": {"prefix": "/pt", "html_lang": "pt-BR", "name": "Português (Brasil)"},
    "ja": {"prefix": "/ja", "html_lang": "ja", "name": "日本語"},
    "ko": {"prefix": "/ko", "html_lang": "ko", "name": "한국어"},
    "zh-Hans": {"prefix": "/zh", "html_lang": "zh-Hans", "name": "简体中文"},
}
SOURCE = "en"                # the source copy, at the root; also the x-default and the fallback everywhere
# Read from content/en.toml only (build config, not copy), so the key check ignores it.
NOT_COPY = {"release"}
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

HREF = re.compile(r'(\shref=")([^"]*)(")')
BARE_URL = re.compile(r"/[^\s<>\"]*")

TAG = re.compile(r"\{\{(#each|#if|#unless|/each|/if|/unless|>|attr:|link:|asset:)?\s*([^}]*?)\s*\}\}")


class BuildError(Exception):
    pass


def page_url(lang, slug):
    prefix = "" if slug in ONE_LANG else LANGUAGES[lang]["prefix"]
    if slug == "home":
        return prefix + "/"
    if slug == "404":
        return prefix + "/404.html"
    if slug in ONE_LANG:
        return f"/{slug}"
    return f"{prefix}/{slug}/"


def out_path(dist, lang, slug):
    base = dist / ("" if slug in ONE_LANG else LANGUAGES[lang]["prefix"].lstrip("/"))
    if slug == "home":
        return base / "index.html"
    if slug == "404":
        return base / "404.html"
    if slug in ONE_LANG:
        return base / f"{slug}.html"
    return base / slug / "index.html"


def localize(url, lang):
    """A root-relative link to a site page (/download/#android) in lang (/de/download/#android). Anything else is
    left alone: links that already carry a language prefix, /pair, static files, /.well-known, external URLs."""
    prefix = LANGUAGES[lang]["prefix"]
    if not prefix or not url.startswith("/") or url.startswith("//"):
        return url
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    pages = {page_url(SOURCE, s) for s in PAGES if s not in ONE_LANG and s != "404"}
    if (path if path.endswith("/") else path + "/") not in pages:
        return url
    return prefix + url


def localize_links(val, lang):
    """Copy is either a bare URL (href = "/gateway/") or HTML with href="..." attributes in it."""
    if BARE_URL.fullmatch(val):
        return localize(val, lang)
    return HREF.sub(lambda m: m.group(1) + localize(m.group(2), lang) + m.group(3), val)


def key_paths(d, path=""):
    """Every key in a copy file, as dotted paths. An array of tables counts as one table, path[], with the keys of
    all its items: items may differ (an optional key on one source), and the number of items may too."""
    out = set()
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{path}.{k}" if path else k
            out.add(p)
            out |= key_paths(v, p)
    elif isinstance(d, list):
        for item in d:
            out |= key_paths(item, f"{path}[]")
    return out


def check_keys(copies):
    """Every translation has exactly the source's keys."""
    def paths(c):
        return key_paths({k: v for k, v in c.items() if k not in NOT_COPY})
    want, bad = paths(copies[SOURCE]), []
    for lang, copy in copies.items():
        have = paths(copy)
        if have != want:
            for label, keys in (("missing", want - have), ("extra", have - want)):
                if keys:
                    bad.append(f"content/{lang}.toml {label}: " + ", ".join(sorted(keys)))
    if bad:
        raise BuildError(f"copy files don't match content/{SOURCE}.toml:\n  " + "\n  ".join(bad))


def built_languages(content):
    """The languages with a content file, in LANGUAGES order."""
    found = {f.stem for f in content.glob("*.toml")}
    unknown = sorted(found - set(LANGUAGES))
    if unknown:
        raise BuildError(f"unknown language(s) {', '.join(unknown)} in {content}: add them to LANGUAGES in build.py")
    if SOURCE not in found:
        raise BuildError(f"no content/{SOURCE}.toml")
    return [lang for lang in LANGUAGES if lang in found]


def lang_of(dist, f):
    """The language a built file belongs to, from its path (/pair.html and the root pages are English)."""
    first = f.relative_to(dist).parts[0]
    return next((l for l, v in LANGUAGES.items() if v["prefix"] and v["prefix"][1:] == first), SOURCE)


class Renderer:
    # Values the build put together itself (rendered HTML): passed through as they are.
    RAW = {"content", "pair_blocks", "pair_footer", "pair_titles"}

    def __init__(self, lang, copy, page_ctx, release):
        self.lang = lang
        self.scopes = [{**copy, **page_ctx}]
        self.release = release
        self.drafts = []
        self.hashes = {}
        # Which values are copy (their links get the language prefix) and which the build made (page URLs, the
        # language list): "page" is the page's own copy; {{#each}} items inherit from the list they come from.
        self.built = set(page_ctx) - {"page"}
        self.each_copy = []

    def is_copy(self, path):
        head = path.split(".")[0]
        if head == "it" and self.each_copy:
            return self.each_copy[-1]
        return head not in self.built

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
        if path in self.RAW:
            return val
        if self.is_copy(path):
            val = localize_links(val, self.lang)
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
            label = self.lookup("ui.flag_norway")
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
                    self.each_copy.append(self.is_copy(arg))
                    for i, item in enumerate(val, 1):
                        self.scopes.append({"it": {**item, "n": i} if isinstance(item, dict) else item})
                        out.append(self.render(body))
                        self.scopes.pop()
                    self.each_copy.pop()
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
    """Every internal href/src in dist/ must hit a file, and every #fragment an id on that page, in every language.
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
            bad.append(f"[{lang_of(dist, f)}] {f.relative_to(dist)}: {u} (no such file)")
        elif frag and t.suffix == ".html" and frag not in ids.get(t, set()):
            bad.append(f"[{lang_of(dist, f)}] {f.relative_to(dist)}: {u} (no id {frag!r})")
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


def og_image(lang):
    """og-<lang>.jpg when the brand kit has one, else the English card."""
    name = f"/img/og/og-{lang}.jpg"
    return SITE + (name if (ROOT / "static" / name.lstrip("/")).is_file() else f"/img/og/og-{SOURCE}.jpg")


def page_ctx(lang, slug, langs):
    meta = langs[lang]["pages"].get(slug)
    if meta is None:
        raise BuildError(f"[{lang}] no [pages.{slug}] in content/{lang}.toml")
    # The picker links to the same page in each language; from a 404, to each language's front page.
    target = "home" if slug == "404" else slug
    return {
        "lang": lang,
        "html_lang": LANGUAGES[lang]["html_lang"],
        "lang_name": LANGUAGES[lang]["name"],
        "slug": slug,
        "page": meta,
        "url_self": SITE + page_url(lang, slug),
        "url_default": SITE + page_url(SOURCE, slug),
        "alternates": [{"hreflang": LANGUAGES[l]["html_lang"], "href": SITE + page_url(l, slug)} for l in langs],
        "languages": [{"code": l, "hreflang": LANGUAGES[l]["html_lang"], "name": LANGUAGES[l]["name"],
                       "href": page_url(l, target), "current": l == lang} for l in langs],
        "multilingual": len(langs) > 1,
        "og_image": og_image(lang),
        "is_home": slug == "home",
        "noindex": slug in ("404", "pair"),
    }


def render_pair(copies, release):
    """/pair: one page with every language. English shows; the others are <details> until site.js picks one."""
    blocks, footer, titles, drafts = [], [], [], []
    block_tpl = (ROOT / "templates/partials/pair-copy.html").read_text()
    for lang, copy in copies.items():
        ctx = {**page_ctx(lang, "pair", copies), "pair_first": lang == SOURCE}
        r = Renderer(lang, copy, ctx, release)
        blocks.append(r.render(block_tpl))
        hidden = "" if lang == SOURCE else " hidden"
        footer.append(r.render(f'<span data-lang="{{{{html_lang}}}}" lang="{{{{html_lang}}}}"{hidden}>'
                               '{{footer.endorsement}}</span>'))
        titles.append(r.render(' data-title-{{html_lang}}="{{attr:page.title}}"'))
        drafts += [f"{lang}/pair: {d}" for d in dict.fromkeys(r.drafts)]
    first, *others = blocks
    body = first + (f'\n<div class="pair-others">\n{"".join(others)}</div>' if others else "")
    return {"pair_blocks": body, "pair_footer": "".join(footer), "pair_titles": "".join(titles),
            "pair_langs": " ".join(LANGUAGES[l]["html_lang"] for l in copies)}, drafts


def build(release, content=None, dist=None):
    content = content or ROOT / "content"
    dist = dist or ROOT / "dist"
    langs = built_languages(content)
    copies = {lang: tomllib.loads((content / f"{lang}.toml").read_text()) for lang in langs}
    check_keys(copies)
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(ROOT / "static", dist)
    layout = (ROOT / "templates/layout.html").read_text()
    drafts = []
    for lang, copy in copies.items():
        for slug in PAGES:
            if slug in ONE_LANG and lang != SOURCE:
                continue
            ctx = page_ctx(lang, slug, copies)
            if slug == "pair":
                extra, pair_drafts = render_pair(copies, release)
                ctx.update(extra)
                drafts += pair_drafts
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
            f = out_path(dist, lang, slug)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(doc)
    check_links(dist)
    well_known = write_well_known(dist)
    holds = copies[SOURCE].get("release", {}).get("hold", [])
    urls = [SITE + page_url(lang, s) for lang in langs for s in PAGES if s not in ("404", "pair")]
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
    n = sum(1 if s in ONE_LANG else len(langs) for s in PAGES)
    print(f"built {n} pages in {len(langs)} languages ({', '.join(langs)}) into {dist}")
    print(f".well-known: {well_known or 'none (docs/aasa.json, android/docs/assetlinks.json not present)'}")


if __name__ == "__main__":
    try:
        build(release="--release" in sys.argv[1:])
    except BuildError as e:
        sys.exit(f"build.py: {e}")
