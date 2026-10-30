"""Checks on the built site. Standard library only:

    python3 -m unittest discover -s tests -v      (from website/)

Builds dist/ once, then checks: every internal link and asset resolves (including #fragments), nothing loads
from another origin (HTML, CSS, JS), /pair never touches the pairing fragment, language and hreflang sets,
landmarks and alt text, the release build refuses placeholders and holds, and no private addresses leak into dist/.
MultiLanguageTest builds a second site into a temporary folder, with English copies standing in for German and
Simplified Chinese, to check the picker, hreflang, link prefixing and /pair with more than two languages.
"""
import shutil
import html.parser
import json
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import build  # noqa: E402

DIST = ROOT / "dist"
SITE = build.SITE
# Attributes that make the browser fetch something on page load (anchors are navigation, not requests).
FETCH_ATTRS = {("img", "src"), ("img", "srcset"), ("source", "srcset"), ("source", "src"), ("script", "src"),
               ("iframe", "src"), ("embed", "src"), ("object", "data"), ("video", "src"), ("video", "poster"),
               ("audio", "src"), ("track", "src"), ("input", "src"), ("image", "href")}
LINK_FETCH_RELS = {"stylesheet", "icon", "apple-touch-icon", "manifest", "preload", "prefetch", "preconnect",
                   "dns-prefetch", "modulepreload", "mask-icon"}


class Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []          # (tag, attrs dict)
        self.ids = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if "id" in a:
            self.ids.append(a["id"])

    handle_startendtag = handle_starttag


def parse(path):
    return parse_text(path.read_text())


def parse_text(text):
    p = Page()
    p.feed(text)
    return p


def srcset_urls(value):
    return [part.strip().split()[0] for part in value.split(",") if part.strip()]


def resolve(url):
    """dist file a same-origin URL points to, the way the Worker serves it (auto-trailing-slash)."""
    path = urlsplit(url).path
    f = DIST / path.lstrip("/")
    if path.endswith("/"):
        return f / "index.html"
    if f.is_file():
        return f
    if (f.with_name(f.name + ".html")).is_file():     # /pair -> pair.html
        return f.with_name(f.name + ".html")
    return f


def js_code(path):
    """A script without its comments (ours use /* */ and // only), so the checks look at code, not prose."""
    t = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return re.sub(r"(^|[^:\\])//[^\n]*", r"\1", t)


def html_files():
    return sorted(DIST.rglob("*.html"))


def built():
    return build.built_languages(ROOT / "content")


def prefix_dir(lang):
    """dist/ subfolder of a language, with a trailing slash ("" for English)."""
    p = build.LANGUAGES[lang]["prefix"].lstrip("/")
    return p + "/" if p else ""


def alternates(page):
    return {a["hreflang"]: a["href"] for t, a in page.tags
            if t == "link" and a.get("rel") == "alternate" and "hreflang" in a}


class SiteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build.build(release=False)
        cls.pages = {f: parse(f) for f in html_files()}

    # ------------------------------------------------------------ links and assets

    def test_internal_links_and_assets_resolve(self):
        problems = []
        for f, page in self.pages.items():
            base = "/" + str(f.relative_to(DIST)).replace("index.html", "")
            for tag, a in page.tags:
                urls = []
                for attr in ("href", "src", "poster", "data"):
                    if attr in a and a[attr] is not None:
                        urls.append(a[attr])
                if "srcset" in a:
                    urls += srcset_urls(a["srcset"])
                if tag == "meta" and a.get("property") == "og:image":
                    urls.append(a["content"])
                for u in urls:
                    if u.startswith(("mailto:", "tel:")):
                        continue
                    if build.PLACEHOLDER.fullmatch(u):
                        continue                      # {DOWNLOAD_URL} etc.: --release refuses them (test below)
                    parts = urlsplit(u)
                    if parts.scheme in ("http", "https") and not u.startswith(SITE):
                        continue                      # external navigation link, checked below for loads
                    if u.startswith(SITE):
                        u = u[len(SITE):] or "/"
                        parts = urlsplit(u)
                    if not parts.path and parts.fragment:      # same-page anchor
                        target, frag = f, parts.fragment
                    else:
                        path = parts.path if parts.path.startswith("/") else base + parts.path
                        target, frag = resolve(path), parts.fragment
                    if not target.is_file():
                        problems.append(f"{f.relative_to(DIST)}: {u} -> missing {target.relative_to(DIST)}")
                        continue
                    if frag and target.suffix == ".html":
                        if frag not in self.pages[target].ids:
                            problems.append(f"{f.relative_to(DIST)}: {u} -> no id {frag!r}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_css_urls_resolve(self):
        problems = []
        for css in DIST.rglob("*.css"):
            for u in re.findall(r"url\(\s*['\"]?([^'\")]+)", css.read_text()):
                if u.startswith("data:"):
                    continue
                if not resolve(u).is_file():
                    problems.append(f"{css.name}: {u}")
        self.assertEqual(problems, [])

    def test_manifest_icons_resolve(self):
        m = json.loads((DIST / "site.webmanifest").read_text())
        for icon in m["icons"]:
            self.assertTrue(resolve(icon["src"]).is_file(), icon["src"])

    # ------------------------------------------------------------ no third-party requests

    def test_html_loads_nothing_from_other_origins(self):
        problems = []
        for f, page in self.pages.items():
            for tag, a in page.tags:
                loads = []
                for (t, attr) in FETCH_ATTRS:
                    if tag == t and a.get(attr):
                        loads += srcset_urls(a[attr]) if attr == "srcset" else [a[attr]]
                if tag == "link" and set((a.get("rel") or "").split()) & LINK_FETCH_RELS:
                    loads.append(a.get("href", ""))
                if tag == "form" and a.get("action"):
                    loads.append(a["action"])
                if "style" in a:
                    problems.append(f"{f.relative_to(DIST)}: inline style attribute (blocked by the CSP)")
                for u in loads:
                    if re.match(r"^(https?:)?//", u):
                        problems.append(f"{f.relative_to(DIST)}: <{tag}> loads {u}")
            text = f.read_text()
            if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text):
                problems.append(f"{f.relative_to(DIST)}: inline <script> (blocked by the CSP)")
            if re.search(r"<style\b", text):
                problems.append(f"{f.relative_to(DIST)}: inline <style> (blocked by the CSP)")
            if re.search(r"<iframe\b", text):
                problems.append(f"{f.relative_to(DIST)}: <iframe>")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_css_and_js_load_nothing_from_other_origins(self):
        problems = []
        for css in DIST.rglob("*.css"):
            t = css.read_text()
            for u in re.findall(r"url\(\s*['\"]?([^'\")]+)", t) + re.findall(r"@import\s+['\"]([^'\"]+)", t):
                if re.match(r"^(https?:)?//", u):
                    problems.append(f"{css.name}: {u}")
        for js in DIST.rglob("*.js"):
            t = js_code(js)
            for pat in (r"https?://", r"\bfetch\s*\(", r"XMLHttpRequest", r"sendBeacon", r"WebSocket", r"EventSource",
                        r"import\s*\(", r"document\.cookie"):
                if re.search(pat, t):
                    problems.append(f"{js.name}: {pat}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_csp_allows_only_self(self):
        headers = (DIST / "_headers").read_text()
        csp = re.search(r"Content-Security-Policy: (.+)", headers).group(1)
        self.assertNotRegex(csp, r"https?:|\*|'unsafe-inline'|'unsafe-eval'")
        self.assertIn("connect-src 'none'", csp)

    def test_no_cookies_set_or_read(self):
        for js in DIST.rglob("*.js"):
            self.assertNotIn("cookie", js_code(js).lower(), js.name)
        self.assertNotIn("Set-Cookie", (DIST / "_headers").read_text())

    # ------------------------------------------------------------ /pair

    def test_pair_page_never_reads_the_fragment(self):
        page = DIST / "pair.html"
        self.assertTrue(page.is_file(), "/pair must be served by pair.html (no redirect to /pair/)")
        self.assertFalse((DIST / "pair").exists(), "a pair/ directory would make /pair redirect")
        scripts = [resolve(a["src"]) for t, a in self.pages[page].tags if t == "script"]
        self.assertTrue(scripts)
        for s in scripts:
            t = js_code(s)
            for pat in ("location", "hash", "URL(", "href.split", "document.URL", "history"):
                self.assertNotIn(pat, t, f"{s.name} mentions {pat}")
        text = page.read_text()
        for lang in built():
            self.assertIn(f'lang="{build.LANGUAGES[lang]["html_lang"]}"', text, lang)

    def test_pair_page_shows_english_without_javascript(self):
        # No JavaScript: the English block is open, every other language is a closed <details> named in its own
        # language, and only the English footer shows. site.js picks one language when it runs.
        page = self.pages[DIST / "pair.html"]
        html_tag = next(a for t, a in page.tags if t == "html")
        tags = [build.LANGUAGES[l]["html_lang"] for l in built()]
        self.assertEqual(html_tag.get("lang"), "en")
        self.assertEqual(html_tag.get("data-langs", "").split(), tags)
        blocks = [(t, a) for t, a in page.tags if "pair-copy" in (a.get("class") or "").split()]
        self.assertEqual([a["data-lang"] for _, a in blocks], tags)
        self.assertEqual(blocks[0][0], "div")
        for t, a in blocks[1:]:
            self.assertEqual(t, "details", a["data-lang"])
            self.assertNotIn("open", a)
        text = (DIST / "pair.html").read_text()
        for lang in built()[1:]:
            name = build.LANGUAGES[lang]["name"]
            self.assertIn(f"<summary>{name}</summary>", text)
        footer = [a for t, a in page.tags if t == "span" and "data-lang" in a]
        self.assertEqual([("hidden" in a) for a in footer], [False] + [True] * (len(tags) - 1))
        body = next(a for t, a in page.tags if t == "body")
        for tag in tags:
            self.assertTrue(body.get(f"data-title-{tag.lower()}"), tag)
        # The header picker on /pair is buttons for site.js; CSS hides it without JavaScript.
        css = (DIST / "css/site.css").read_text()
        self.assertIn(":root:not(.js) .pair-tools .lang-picker { display: none; }", css)

    def test_site_js_picks_from_navigator_languages(self):
        js = js_code(DIST / "js/site.js")
        self.assertIn("navigator.languages", js)
        self.assertIn('return "en"', js)

    # ------------------------------------------------------------ language, landmarks, alt text

    def test_lang_and_hreflang_pairs(self):
        tags = {build.LANGUAGES[l]["html_lang"] for l in built()}
        for f, page in self.pages.items():
            if f.name == "pair.html":
                continue
            rel = f.relative_to(DIST)
            want = build.LANGUAGES[build.lang_of(DIST, f)]["html_lang"]
            html_tag = next(a for t, a in page.tags if t == "html")
            self.assertEqual(html_tag.get("lang"), want, str(rel))
            if f.name == "404.html":
                continue
            alts = alternates(page)
            self.assertEqual(set(alts), tags | {"x-default"}, str(rel))
            self.assertEqual(alts["x-default"], alts["en"], str(rel))
            for lang, href in alts.items():
                other = resolve(href[len(SITE):])
                self.assertTrue(other.is_file(), f"{rel}: hreflang {lang} -> {href}")
                self.assertEqual(alternates(self.pages[other]), alts,
                                 f"{rel}: hreflang not reciprocal with {other.relative_to(DIST)}")

    def test_language_picker(self):
        # A <details> of links: every built language, named in its own language, to the same page in that language.
        langs = built()
        for f, page in self.pages.items():
            if f.name == "pair.html":
                continue
            rel = str(f.relative_to(DIST))
            lang = build.lang_of(DIST, f)
            text = f.read_text()
            m = re.search(r'<details class="lang-picker">(.*?)</details>', text, re.S)
            self.assertIsNotNone(m, rel)
            self.assertEqual(text.count('class="lang-picker"'), 1, rel)
            picker = parse_text(m.group(1))
            links = [a for t, a in picker.tags if t == "a"]
            self.assertEqual([a["hreflang"] for a in links], [build.LANGUAGES[l]["html_lang"] for l in langs], rel)
            self.assertEqual([a["lang"] for a in links], [a["hreflang"] for a in links], rel)
            current = [a for a in links if a.get("aria-current") == "true"]
            self.assertEqual([a["hreflang"] for a in current], [build.LANGUAGES[lang]["html_lang"]], rel)
            for l, a in zip(langs, links):
                self.assertIn(f'>{build.LANGUAGES[l]["name"]}</a>', m.group(1), rel)
                self.assertTrue(resolve(a["href"]).is_file(), f"{rel}: {a['href']}")
                if f.name != "404.html":
                    self.assertEqual(SITE + a["href"], alternates(page)[a["hreflang"]], rel)
            # The summary says what it is to a screen reader ("Language: English") and shows the current language.
            summary = re.search(r"<summary>(.*?)</summary>", m.group(1), re.S).group(1)
            self.assertIn('class="visually-hidden"', summary, rel)
            self.assertIn(build.LANGUAGES[lang]["name"], summary, rel)
            self.assertIn('aria-hidden="true"', summary, rel)     # the globe icon

    def test_copy_links_stay_in_their_language(self):
        # Every link to a site page from a page in language L goes to L's page, except the picker and the alternates.
        for f in html_files():
            if f.name == "pair.html":
                continue
            lang = build.lang_of(DIST, f)
            text = re.sub(r'<details class="lang-picker">.*?</details>', "", f.read_text(), flags=re.S)
            for u in re.findall(r'<a [^>]*href="(/[^"]*)"', text):
                if u.startswith("//"):
                    continue
                path = urlsplit(u).path
                if resolve(path).suffix != ".html" or path == "/pair":
                    continue
                self.assertEqual(build.lang_of(DIST, resolve(path)), lang, f"{f.relative_to(DIST)}: {u}")

    def test_og_image_per_language_or_english(self):
        for f, page in self.pages.items():
            og = next((a["content"] for t, a in page.tags if t == "meta" and a.get("property") == "og:image"), None)
            if og is None:
                continue
            lang = build.lang_of(DIST, f)
            own = DIST / f"img/og/og-{lang}.jpg"
            want = f"{SITE}/img/og/og-{lang}.jpg" if own.is_file() else f"{SITE}/img/og/og-en.jpg"
            self.assertEqual(og, want, str(f.relative_to(DIST)))

    def test_sitemap_lists_every_language(self):
        locs = re.findall(r"<loc>([^<]+)</loc>", (DIST / "sitemap.xml").read_text())
        pages = [s for s in build.PAGES if s not in ("404", "pair")]
        self.assertEqual(len(locs), len(pages) * len(built()))
        for u in locs:
            self.assertTrue(resolve(u[len(SITE):]).is_file(), u)

    def test_landmarks_headings_alt(self):
        for f, page in self.pages.items():
            rel = str(f.relative_to(DIST))
            tags = [t for t, _ in page.tags]
            self.assertEqual(tags.count("main"), 1, rel)
            self.assertIn("header", tags, rel)
            self.assertIn("footer", tags, rel)
            if f.name != "pair.html":
                self.assertEqual(tags.count("h1"), 1, rel)
                self.assertTrue(any(t == "a" and a.get("class") == "skip" for t, a in page.tags), rel)
            for t, a in page.tags:
                if t == "img":
                    self.assertIn("alt", a, f"{rel}: <img src={a.get('src')}> without alt")
                if t == "nav":
                    self.assertTrue(a.get("aria-label"), f"{rel}: <nav> without aria-label")
            dup = {i for i in page.ids if page.ids.count(i) > 1}
            self.assertEqual(dup, set(), f"{rel}: duplicate ids")

    def test_focus_style_present(self):
        self.assertIn(":focus-visible", (DIST / "css/site.css").read_text())

    def test_flag_is_an_svg_labelled_in_its_language(self):
        # Windows has no flag emoji: it would show the letters N O.
        for f in html_files():
            text = f.read_text()
            self.assertNotIn(build.FLAG_NO, re.sub(r"<[^>]*>", "", text), f"{f.relative_to(DIST)}: flag emoji in the text")
        self.assertIn('aria-label="Norway"', (DIST / "index.html").read_text())
        self.assertIn('aria-label="Norge"', (DIST / "no/index.html").read_text())
        pair = (DIST / "pair.html").read_text()
        self.assertIn('aria-label="Norway"', pair)
        self.assertIn('aria-label="Norge"', pair)

    def test_short_inline_code_kept_together(self):
        doc = build.keep_short_code_together(
            '<p><code>smb_encryption = "server"</code> <code>https://loft.incremental.no/pair#abcdef</code></p>'
            '<pre><code>short</code></pre>')
        self.assertIn('<code class="short">smb_encryption = "server"</code>', doc)
        self.assertIn('<code>https://loft.incremental.no/pair#abcdef</code>', doc)
        self.assertIn('<pre><code>short</code></pre>', doc)
        self.assertIn('<code class="short">smb_encryption = "server"</code>', (DIST / "no/gateway/index.html").read_text())

    def test_coming_soon_platform_has_no_downloads(self):
        # The gateway .deb and image are built but not released: its Download card offers nothing to download.
        r = build.Renderer("en", {"x": {"on": True, "off": False}}, {}, release=False)
        self.assertEqual(r.render("{{#unless x.on}}a{{/unless}}{{#unless x.off}}b{{/unless}}{{#unless x.none}}c{{/unless}}"), "bc")
        for lang in built():
            prefix = prefix_dir(lang)
            copy = build.tomllib.loads((ROOT / f"content/{lang}.toml").read_text())
            soon = [p["id"] for p in copy["platforms"] if p.get("coming_soon")]
            self.assertIn("gateway", soon, lang)
            doc = (DIST / f"{prefix}download/index.html").read_text()
            for pid in soon:
                card = re.search(rf'<article class="card download" id="{pid}">(.*?)</article>', doc, re.S).group(1)
                self.assertNotIn("btn", card, f"{lang}/{pid}: download button while coming soon")
                self.assertNotIn('class="dl-version"', card, f"{lang}/{pid}")
                self.assertNotIn('class="meta"', card, f"{lang}/{pid}")
                self.assertIn('class="dl-soon"', card, f"{lang}/{pid}: no coming-soon label")
            # The other cards still get theirs.
            for pid in (p["id"] for p in copy["platforms"] if not p.get("coming_soon") and p.get("downloads")):
                self.assertIn("btn", re.search(rf'id="{pid}">(.*?)</article>', doc, re.S).group(1), f"{lang}/{pid}")

    def test_gateway_step_text_before_the_command(self):
        for prefix in ("", "no/"):       # the Coming-soon wording is checked in the two languages written in-house
            doc = (DIST / f"{prefix}gateway/index.html").read_text()
            m = re.search(r'<section class="guide-part" id="debian">(.*?)</section>', doc, re.S)
            if m is None:   # pre-release: step 1 is one Coming-soon paragraph, with no install command to copy
                self.assertNotIn("apt install", doc, prefix)
                self.assertIn("<strong>Coming soon.</strong>" if not prefix else "<strong>Kommer snart.</strong>", doc)
                continue
            part = m.group(1)
            self.assertLess(part.index(".deb</code>"), part.index("<pre"), prefix)

    # ------------------------------------------------------------ release build

    def test_release_build_refuses_placeholders_and_holds(self):
        # A hold is injected, so this doesn't depend on what the live copy still holds.
        loads = build.tomllib.loads
        def held(text):
            d = loads(text)
            d.setdefault("release", {})["hold"] = ["test hold"]
            return d
        try:
            with mock.patch.object(build.tomllib, "loads", held), self.assertRaises(build.BuildError):
                build.build(release=True)
        finally:
            build.build(release=False)      # the other tests read dist/

    # ------------------------------------------------------------ .well-known

    def test_well_known_copied_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            aasa = tmp / "aasa.json"
            aasa.write_text('{"applinks": {"details": []}}')
            saved = dict(build.WELL_KNOWN)
            try:
                build.WELL_KNOWN.clear()
                build.WELL_KNOWN.update({"apple-app-site-association": aasa,
                                         "assetlinks.json": tmp / "missing.json"})
                out = tmp / "dist"
                out.mkdir()
                done = build.write_well_known(out)
                self.assertEqual(done, ["apple-app-site-association"])
                self.assertEqual(json.loads((out / ".well-known/apple-app-site-association").read_text()),
                                 {"applinks": {"details": []}})
                aasa.write_text("{not json")
                with self.assertRaises(build.BuildError):
                    build.write_well_known(out)
            finally:
                build.WELL_KNOWN.clear()
                build.WELL_KNOWN.update(saved)
        headers = (DIST / "_headers").read_text()
        for name in ("apple-app-site-association", "assetlinks.json"):
            block = headers.split(f"/.well-known/{name}\n", 1)[1].split("\n/", 1)[0]
            self.assertIn("Content-Type: application/json", block)

    # ------------------------------------------------------------ nothing private in the public build

    def test_no_private_addresses(self):
        pat = re.compile(r"\b(192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)\b")
        for f in DIST.rglob("*"):
            if f.suffix in (".html", ".css", ".js", ".json", ".xml", ".txt", ".webmanifest") or f.name == "_headers":
                m = pat.search(f.read_text())
                self.assertIsNone(m, f"{f.relative_to(DIST)}: {m and m.group(0)}")


class LanguageUnitTest(unittest.TestCase):
    """The language table, link prefixing and the key check, without a build."""

    def test_language_table(self):
        L = build.LANGUAGES
        self.assertEqual(list(L)[:2], ["en", "no"])
        self.assertEqual((L["en"]["prefix"], L["en"]["html_lang"]), ("", "en"))
        self.assertEqual((L["no"]["prefix"], L["no"]["html_lang"]), ("/no", "nb"))
        self.assertEqual((L["pt-BR"]["prefix"], L["pt-BR"]["html_lang"]), ("/pt", "pt-BR"))
        self.assertEqual((L["zh-Hans"]["prefix"], L["zh-Hans"]["html_lang"]), ("/zh", "zh-Hans"))
        prefixes = [v["prefix"] for v in L.values()]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        # A prefix must not shadow a static folder, a page, /pair or a path the Worker routes elsewhere.
        routes = json.loads((ROOT / "worker/routes.json").read_text())["routes"]
        taken = {p.name for p in (ROOT / "static").iterdir()} | set(build.PAGES) | {".well-known"}
        taken |= {r["pattern"].split("/", 1)[1].split("/")[0] for r in routes}
        for code, v in L.items():
            if v["prefix"]:
                self.assertNotIn(v["prefix"].lstrip("/"), taken, code)

    def test_localize(self):
        loc = build.localize
        self.assertEqual(loc("/download/#android", "de"), "/de/download/#android")
        self.assertEqual(loc("/", "zh-Hans"), "/zh/")
        self.assertEqual(loc("/#how", "pt-BR"), "/pt/#how")
        self.assertEqual(loc("/gateway", "de"), "/de/gateway")          # no trailing slash is still a page
        self.assertEqual(loc("/download/?x=1#a", "ja"), "/ja/download/?x=1#a")
        for keep in ("/no/download/#android", "/de/download/", "/pair", "/pair#abc", "/img/og/og-en.jpg",
                     "/.well-known/assetlinks.json", "/releases.json", "/fonts/manrope-var-latin.woff2",
                     "https://github.com/Incremental-AS/loft", "//example.com/download/", "#android",
                     "mailto:hello@example.com", "/v1/health", "/healthz", "/404.html", "/unknown/"):
            self.assertEqual(loc(keep, "de"), keep, keep)
        self.assertEqual(loc("/download/", "en"), "/download/")         # English is at the root

    def test_localize_links_in_copy(self):
        html_copy = ('See <a href="/download/#android">Android</a>, <a href="/no/security/">sikkerhet</a>, '
                     '<a href="https://github.com/Incremental-AS/loft">GitHub</a> and <a href="/pair">pair</a>.')
        self.assertEqual(build.localize_links(html_copy, "fr"),
                         'See <a href="/fr/download/#android">Android</a>, <a href="/no/security/">sikkerhet</a>, '
                         '<a href="https://github.com/Incremental-AS/loft">GitHub</a> and <a href="/pair">pair</a>.')
        self.assertEqual(build.localize_links("/gateway/", "ko"), "/ko/gateway/")
        self.assertEqual(build.localize_links("/ text with a slash", "ko"), "/ text with a slash")

    def test_renderer_prefixes_copy_but_not_build_values(self):
        copy = {"x": {"a": '<a href="/support/">s</a>'}, "links": [{"href": "/source/"}]}
        ctx = {"languages": [{"href": "/support/"}], "page": {"b": "/privacy/"}}
        r = build.Renderer("de", copy, ctx, release=False)
        self.assertEqual(r.render("{{x.a}}"), '<a href="/de/support/">s</a>')
        self.assertEqual(r.render("{{#each links}}{{attr:it.href}}{{/each}}"), "/de/source/")
        self.assertEqual(r.render("{{page.b}}"), "/de/privacy/")
        self.assertEqual(r.render("{{#each languages}}{{it.href}}{{/each}}"), "/support/")   # the picker's English link
        self.assertEqual(r.render("{{link:support}}"), "/de/support/")

    def test_key_paths_and_parity(self):
        en = {"release": {"hold": []}, "ui": {"a": "x", "b": "y"},
              "items": [{"t": "1", "href": "/"}, {"t": "2", "alt": "z"}]}
        self.assertEqual(build.key_paths(en), {"release", "release.hold", "ui", "ui.a", "ui.b",
                                               "items", "items[].t", "items[].href", "items[].alt"})
        # Same keys (an array may have other items or another order of optional keys; [release] is not copy).
        same = {"ui": {"b": "Y", "a": "X"}, "items": [{"t": "1", "href": "/", "alt": "z"}]}
        build.check_keys({"en": en, "de": same})
        bad = {"release": {"hold": []}, "ui": {"a": "X", "c": "?"}, "items": [{"t": "1", "href": "/", "alt": "z"}]}
        with self.assertRaises(build.BuildError) as e:
            build.check_keys({"en": en, "de": bad})
        msg = str(e.exception)
        self.assertIn("content/de.toml missing: ui.b", msg)
        self.assertIn("content/de.toml extra: ui.c", msg)

    def test_live_copy_files_match(self):
        langs = built()
        build.check_keys({l: build.tomllib.loads((ROOT / f"content/{l}.toml").read_text()) for l in langs})

    def test_built_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ("zh-Hans", "en", "no"):
                (tmp / f"{name}.toml").write_text("")
            self.assertEqual(build.built_languages(tmp), ["en", "no", "zh-Hans"])
            (tmp / "sv.toml").write_text("")
            with self.assertRaisesRegex(build.BuildError, "unknown language.*sv"):
                build.built_languages(tmp)
            (tmp / "sv.toml").unlink()
            (tmp / "en.toml").unlink()
            with self.assertRaises(build.BuildError):
                build.built_languages(tmp)


class MultiLanguageTest(unittest.TestCase):
    """A site with English, Norwegian, German and Simplified Chinese (English copies stand in for the
    translations), built into a temporary folder so dist/ and content/ are left alone."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        content = tmp / "content"
        content.mkdir()
        for lang in ("en", "no"):
            shutil.copyfile(ROOT / f"content/{lang}.toml", content / f"{lang}.toml")
        for lang in ("de", "zh-Hans"):
            shutil.copyfile(ROOT / "content/en.toml", content / f"{lang}.toml")
        cls.dist = tmp / "dist"
        with mock.patch("sys.stdout"):
            build.build(release=False, content=content, dist=cls.dist)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def read(self, rel):
        return (self.dist / rel).read_text()

    def test_every_language_is_built(self):
        for prefix in ("", "no/", "de/", "zh/"):
            for slug in ("index.html", "download/index.html", "404.html"):
                self.assertTrue((self.dist / prefix / slug).is_file(), prefix + slug)
        self.assertFalse((self.dist / "zh-Hans").exists())
        self.assertIn('<html lang="zh-Hans">', self.read("zh/index.html"))
        self.assertIn('<html lang="de">', self.read("de/index.html"))

    def test_hreflang_has_every_language_and_x_default(self):
        alts = alternates(parse(self.dist / "de/download/index.html"))
        self.assertEqual(alts, {"en": f"{SITE}/download/", "nb": f"{SITE}/no/download/", "de": f"{SITE}/de/download/",
                                "zh-Hans": f"{SITE}/zh/download/", "x-default": f"{SITE}/download/"})
        self.assertEqual(alternates(parse(self.dist / "zh/download/index.html")), alts)

    def test_picker(self):
        text = self.read("zh/how-it-works/index.html")
        picker = re.search(r'<details class="lang-picker">(.*?)</details>', text, re.S).group(1)
        self.assertEqual(re.findall(r'<a href="([^"]+)" hreflang="([^"]+)"', picker),
                         [("/how-it-works/", "en"), ("/no/how-it-works/", "nb"), ("/de/how-it-works/", "de"),
                          ("/zh/how-it-works/", "zh-Hans")])
        self.assertIn('lang="zh-Hans" aria-current="true">简体中文</a>', picker)
        self.assertEqual(picker.count("aria-current"), 1)
        self.assertIn('>Deutsch</a>', picker)
        self.assertIn('<span class="lang-name" lang="zh-Hans">简体中文</span>', picker)
        # From a 404, the picker goes to each language's front page.
        p404 = re.search(r'<details class="lang-picker">(.*?)</details>', self.read("de/404.html"), re.S).group(1)
        self.assertEqual(re.findall(r'<a href="([^"]+)"', p404), ["/", "/no/", "/de/", "/zh/"])

    def test_copy_links_are_prefixed(self):
        home = self.read("de/index.html")
        self.assertIn('href="/de/how-it-works/#security"', home)
        self.assertIn('href="/de/gateway/"', home)                  # a bare href value in an {{#each}}
        dl = self.read("zh/download/index.html")
        self.assertNotIn('href="/download/', re.sub(r'<details class="lang-picker">.*?</details>', "", dl, flags=re.S))
        # Norwegian copy writes its prefix out: still once.
        self.assertIn('href="/no/download/#android"', self.read("no/index.html") + self.read("no/how-it-works/index.html")
                      + self.read("no/download/index.html") + self.read("no/gateway/index.html"))
        for f in self.dist.rglob("*.html"):
            self.assertNotRegex(f.read_text(), r'href="/(no|de|zh)/(no|de|zh)/', str(f))
            self.assertNotIn('href="/de/pair', f.read_text())

    def test_og_image_falls_back_to_english(self):
        self.assertIn(f'content="{SITE}/img/og/og-en.jpg"', self.read("de/index.html"))
        self.assertIn(f'content="{SITE}/img/og/og-no.jpg"', self.read("no/index.html"))

    def test_pair_carries_every_language(self):
        text = self.read("pair.html")
        self.assertIn('data-langs="en nb de zh-Hans"', text)
        self.assertIn('<details class="pair-copy" data-lang="de" lang="de">', text)
        self.assertIn('<details class="pair-copy" data-lang="zh-Hans" lang="zh-Hans">', text)
        self.assertIn("<summary>简体中文</summary>", text)
        self.assertIn('href="/zh/download/"', text)
        self.assertIn('data-to="zh-Hans" lang="zh-Hans">简体中文</button>', text)
        self.assertLess(len(text.encode()), 40_000, "pair.html grew past 40 kB")

    def test_sitemap(self):
        locs = re.findall(r"<loc>([^<]+)</loc>", self.read("sitemap.xml"))
        self.assertIn(f"{SITE}/zh/", locs)
        self.assertIn(f"{SITE}/de/download/", locs)
        self.assertEqual(len(locs), 4 * (len(build.PAGES) - 2))

    def test_missing_key_fails_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content"
            content.mkdir()
            en = (ROOT / "content/en.toml").read_text()
            shutil.copyfile(ROOT / "content/en.toml", content / "en.toml")
            (content / "ja.toml").write_text(en.replace('\nlanguage = "Language"\n', '\nlanguag = "Language"\n'))
            with self.assertRaisesRegex(build.BuildError, r"ja\.toml missing: ui\.language(.|\n)*ja\.toml extra: ui\.languag"):
                build.build(release=False, content=content, dist=Path(tmp) / "dist")
            self.assertFalse((Path(tmp) / "dist").exists(), "dist/ is left alone when the copy doesn't match")


if __name__ == "__main__":
    unittest.main()
