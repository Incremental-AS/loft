# loft.incremental.no

The Loft website: static, English at `/` and Norwegian (bokmål) at `/no/`. No framework, no JavaScript toolchain,
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
| `content/en.toml`, `content/no.toml` | All the words. English is the source; Norwegian is written natively (capital after a colon, sentence case). |
| `templates/layout.html` | Head, header, footer. |
| `templates/pages/<slug>.html` | One per page: home, how-it-works, download, gateway, compatibility, privacy, support, security, source, about-the-name, pair, 404. |
| `templates/layout-pair.html` | `/pair` only: one page with both languages (see below). |
| `templates/partials/` | Diagram with HTML labels, platform tiles, page head, prose sections. |
| `static/css/site.css` | Layout. Uses only the `--loft-*` tokens. |
| `static/js/site.js` | Theme choice (stored in `localStorage`, never a cookie), mobile menu, copy buttons. |
| `static/_headers` | CSP and cache headers, and `application/json` for `/.well-known/*`. |
| `static/css/tokens.css`, `static/fonts/`, `static/img/`, favicons | **Generated from the brand kit. Don't edit; run `tools/sync-brand.sh`.** |
| `worker/` | Cloudflare Worker config (assets only) and the three routes. Not deployed. |
| `tests/test_site.py` | The checks (standard library `unittest`). |
| `tools/serve.py`, `tools/screenshots.js` | Local preview; Playwright screenshots of every page variant. |

The template syntax is described at the top of `build.py`. Copy values that start with `DRAFT: ` are placeholders,
and `{CURLY_CAPS}` tokens (`{VERSION}`, `{SHA256}`, `{MIN_IOS}`, `{DOWNLOAD_URL}` ...) are values nobody has decided yet.
Both show with a dashed outline, and a release build fails on them. The words come from the Loft copy files
in the brand kit; change them there first. Links inside copy are written out
(`/no/download/#android`), and every build checks that each internal link and `#fragment` resolves.

`/pair` is built once, as `pair.html` (Workers assets serve it at `/pair`, no trailing slash, like the pairing
links). Pairing links have no language prefix, so the page carries both languages: `site.js` shows Norwegian when
`navigator.language` is nb, nn or no, and has a language button. Without JavaScript both show. The pairing details
sit in the URL fragment: nothing on the site may read `location.hash`, and the CSP has `connect-src 'none'`. A test
enforces it.

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
- `coming_soon = true` on the gateway in `[[platforms]]` (both languages): no download buttons until the .deb and
  image are released. Remove it, and the "Coming soon" lead-ins, then.
- `{VERSION}`, `{SHA256}`, download URLs and `{MIN_*}`.
- The official App Store badge on the iPhone download, once the app is live.
- Download links, versions and SHA-256 (from the release pipeline; files over 25 MiB are served from GitHub Releases).
  The other lane's `data/releases.json` (one data file for versions, downloads and compatibility, plus the public
  `/releases.json` for the update check) was not carried into this copy model; it comes back with the first client
  that has the update check.
- `/.well-known/` files: waiting for the Apple and Android streams.
