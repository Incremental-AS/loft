# loft.incremental.no

The Loft website: static, English at `/` and every other language under its own prefix (`/no/`, `/de/` ...; see
[Languages](#languages)). No framework, no JavaScript toolchain,
no third-party requests, no cookies, no analytics.

```
python3 build.py                          # draft build -> dist/  (Python 3.11+, standard library only)
python3 build.py --release                # refuses while any copy is DRAFT, a {TOKEN} or held
python3 -m unittest discover -s tests -v  # builds, then checks links, third-party loads, /pair, a11y basics
python3 tools/serve.py 8000               # preview dist/ the way the Worker serves it (127.0.0.1 only)
```

## Layout

| Path | What |
|---|---|
| `content/<code>.toml` | All the words, one file per language. English is the source; Norwegian is written natively (capital after a colon, sentence case); the others are translated from English. |
| `templates/layout.html` | Head, header, footer. |
| `templates/pages/<slug>.html` | One per page: home, how-it-works, download, gateway, compatibility, privacy, support, security, source, about-the-name, pair, 404. |
| `templates/layout-pair.html`, `templates/partials/pair-copy.html` | `/pair` only: one page with every language (see below). |
| `templates/partials/` | Diagram with HTML labels, platform tiles, page head, prose sections. |
| `static/css/site.css` | Layout. Uses only the `--loft-*` tokens. |
| `static/js/site.js` | Theme choice (stored in `localStorage`, never a cookie), mobile menu, copy buttons, closing the language picker, the language on `/pair`. |
| `static/_headers` | CSP and cache headers, and `application/json` for `/.well-known/*`. |
| `static/css/tokens.css`, `static/fonts/`, `static/img/`, favicons | **Generated from the brand kit. Don't edit; run `tools/sync-brand.sh`.** |
| `worker/` | Cloudflare Worker config (assets only) and the three routes. Not deployed. |
| `tests/test_site.py` | The checks (standard library `unittest`). |
| `tools/serve.py`, `tools/screenshots.js` | Local preview; Playwright screenshots of every page variant. |

The template syntax is described at the top of `build.py`. Copy values that start with `DRAFT: ` are placeholders,
and `{CURLY_CAPS}` tokens (`{VERSION}`, `{SHA256}`, `{MIN_IOS}`, `{DOWNLOAD_URL}` ...) are values nobody has decided yet.
Both show with a dashed outline, and a release build fails on them. The words come from the Loft copy files
in the brand kit; change them there first. Links inside copy are written out (`/download/#android`), and every build
checks, in every language, that each internal link and `#fragment` resolves.

`/pair` is built once, as `pair.html` (Workers assets serve it at `/pair`, no trailing slash, like the pairing
links). Pairing links have no language prefix, so the page carries every built language (about 1.1 kB each, 2.6 kB
gzipped for all eleven). With JavaScript, `site.js` shows the best match for `navigator.languages` (the exact tag,
else the same language: `pt-PT` gets `pt-BR`, `zh-TW` gets `zh-Hans`, `nn` and `no` get bokmål; English if nothing
matches) and a language picker in the header switches it in place. Without JavaScript English shows, and each other
language is a closed `<details>` under it, named in its own language; opening one never touches the URL. The pairing
details sit in the URL fragment: nothing on the site may read `location.hash`, and the CSP has `connect-src 'none'`.
A test enforces it.

## Languages

`LANGUAGES` at the top of `build.py` is the list: code (= the content file), URL prefix, `<html lang>`/hreflang, and
the name in its own language for the picker.

| Code | Prefix | `lang` | Picker |
|---|---|---|---|
| `en` | `/` | `en` | English |
| `no` | `/no/` | `nb` | Norsk |
| `de` | `/de/` | `de` | Deutsch |
| `es` | `/es/` | `es` | Español |
| `fr` | `/fr/` | `fr` | Français |
| `it` | `/it/` | `it` | Italiano |
| `nl` | `/nl/` | `nl` | Nederlands |
| `pt-BR` | `/pt/` | `pt-BR` | Português (Brasil) |
| `ja` | `/ja/` | `ja` | 日本語 |
| `ko` | `/ko/` | `ko` | 한국어 |
| `zh-Hans` | `/zh/` | `zh-Hans` | 简体中文 |

- A language is built only when `content/<code>.toml` exists. A content file with an unknown code fails the build.
- Every copy file must have exactly `en.toml`'s keys; the build fails and names the missing and extra ones. An array
  of tables (`[[platforms]]`, sources ...) counts as one table with the keys of all its items, so an optional key on
  one item, or a different number of items, is fine. `[release]` is build config, read from `en.toml` only.
- Links in copy can keep the English paths: on a page in another language, the build prefixes every root-relative
  link to a site page (`/download/#android` becomes `/de/download/#android`). Links that already carry a language
  prefix (`/no/download/`), `/pair`, static files, `/.well-known/` and external URLs are left alone.
- Every page has `hreflang` alternates for every built language plus `x-default` (English); the sitemap lists every
  page in every language. `og:image` is `img/og/og-<code>.jpg` when the brand kit has one, else `og-en.jpg`.
- The picker is a `<details>` of plain links to the same page in each language (from a 404: each front page), the
  current one marked `aria-current="true"`; it works without JavaScript, and `site.js` only closes it on Escape or a
  click outside. In the mobile menu it opens in place.
- The brand fonts are Latin subsets (Basic Latin and Latin-1: fine for de, es, fr, it, nl, pt-BR). Japanese, Korean
  and Chinese fall back to system fonts: `site.css` sets per-language stacks with `:lang()` (brand font first, then
  Hiragino / Yu Gothic / Meiryo, Apple SD Gothic Neo / Malgun Gothic, PingFang SC / Microsoft YaHei, Noto CJK), no
  negative tracking, looser leading, strict line breaking, `word-break: keep-all` for Korean.
- The Worker needs nothing per language: `not_found_handling = "404-page"` serves the nearest `404.html`
  (`/de/x` gets `/de/404.html`), and no prefix collides with `/v1/*`, `/healthz` or a static folder (a test checks).

To add a language: add it to `LANGUAGES` (if it isn't there), add `content/<code>.toml` with `en.toml`'s keys, and
optionally `static/img/og/og-<code>.jpg` from the brand kit. Then `python3 build.py` and the tests. A script that isn't
Latin-1 needs a font stack in `site.css` like the CJK ones. `LANGS="en:,de:/de" node tools/screenshots.js ...` shoots
the pages in other languages and reports horizontal overflow (longer nav labels).

## App links: `/.well-known/`

The Apple stream writes `docs/aasa.json` and the Android stream `android/docs/assetlinks.json` (repo root). When those
files exist, `build.py` checks they are valid JSON and copies them to `dist/.well-known/apple-app-site-association` and
`dist/.well-known/assetlinks.json`. `_headers` serves both as `application/json` with no redirect. If they don't exist yet,
the build says so and carries on.

## Hosting (not live)

An assets-only Cloudflare Worker (Workers Static Assets) on `loft.incremental.no/*`, with `/v1/*` and `/healthz` routed
to no Worker so gateway traffic keeps going straight to the tunnel. `worker/wrangler.toml` has the Worker;
`worker/routes.json` the three routes, which are created separately (the deploy token has no route permission). Nothing
has been deployed yet.

## Brand rules the layout follows

From the Loft brand guidelines:

- One Fraunces headline (`.display`) per page. Sora for headings and buttons, Manrope for text.
- Rust (`--loft-accent`) is the only accent: one primary button per view (**Download**). **Set up a gateway** is secondary.
- The wide hero keeps the loft on the right and the copy in the left 45 %. On mobile the 4:5 painting sits at the bottom
  at its own proportions and the copy sits in the sky above it, so text never covers the building. The scrim is mandatory.
- Light (morning) and dark (night) follow `prefers-color-scheme`. `<html data-theme>` overrides them, and `site.js`
  rewrites the dark `<source>` media queries so the paintings and logos follow the choice too.
- Never call Loft "cloud". No third-party logos or store badges, except the official App Store badge for the iPhone link.
- Footer endorsement only: "Built in 🇳🇴 by Incremental AS". No Incremental wordmark, no gradient button. The 🇳🇴 renders as an inline SVG (build.py, label `ui.flag_norway`): Windows has no flag emoji.

## Not done yet

- `[release].hold` in `content/en.toml`: copy that reads finished but isn't true yet (the 0.2 gateway commands,
  the Source page before the repo is public). `--release` refuses while any entry remains.
- `{DEB_INSTALL}`, `{CONTAINER_RUN}` and `{REMOTE_SETUP_LINK}` on Set up a gateway, from the real 0.2 artefacts.
- `coming_soon = true` on the gateway in `[[platforms]]` (every language): no download buttons until the .deb and
  image are released. Remove it, and the "Coming soon" lead-ins, then.
- `{VERSION}`, `{SHA256}`, download URLs and `{MIN_*}`.
- The official App Store badge on the iPhone download, once the app is live.
- Download links, versions and SHA-256 (from the release pipeline; files over 25 MiB are served from GitHub Releases).
  The other lane's `data/releases.json` (one data file for versions, downloads and compatibility, plus the public
  `/releases.json` for the update check) was not carried into this copy model; it comes back with the first client
  that has the update check.
- `/.well-known/` files: waiting for the Apple and Android streams.
