"""Checks on the built site. Standard library only:

    python3 -m unittest discover -s tests -v      (from website/)

Builds dist/ once, then checks: every internal link and asset resolves (including #fragments), nothing loads
from another origin (HTML, CSS, JS), /pair never touches the pairing fragment, language and hreflang pairs,
landmarks and alt text, the release build refuses placeholders and holds, and no private addresses leak into dist/.
"""
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
    p = Page()
    p.feed(path.read_text())
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
        self.assertIn('lang="en"', text)
        self.assertIn('lang="nb"', text)

    # ------------------------------------------------------------ language, landmarks, alt text

    def test_lang_and_hreflang_pairs(self):
        for f, page in self.pages.items():
            if f.name == "pair.html":
                continue
            rel = f.relative_to(DIST)
            want = "nb" if rel.parts[0] == "no" else "en"
            html_tag = next(a for t, a in page.tags if t == "html")
            self.assertEqual(html_tag.get("lang"), want, str(rel))
            if f.name == "404.html":
                continue
            alts = {a["hreflang"]: a["href"] for t, a in page.tags
                    if t == "link" and a.get("rel") == "alternate" and "hreflang" in a}
            self.assertEqual(set(alts), {"en", "nb", "x-default"}, str(rel))
            for lang, href in alts.items():
                other = resolve(href[len(SITE):])
                self.assertTrue(other.is_file(), f"{rel}: hreflang {lang} -> {href}")
                back = {a["hreflang"]: a["href"] for t, a in self.pages[other].tags
                        if t == "link" and a.get("rel") == "alternate" and "hreflang" in a}
                self.assertEqual(back, alts, f"{rel}: hreflang not reciprocal with {other.relative_to(DIST)}")

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
        for lang, prefix in (("en", ""), ("no", "no/")):
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
        for prefix in ("", "no/"):
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


if __name__ == "__main__":
    unittest.main()
