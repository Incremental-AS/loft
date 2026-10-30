# Translating Loft

Loft keeps your files on the NAS at home and opens them on your Mac, PC, iPhone and Android phone, through a small
server at home called the Loft gateway. It is free and open source, made by Incremental AS in Norway. No cloud, no
tracking. English is the source; Norwegian (bokmål) is written natively and shown to you as a second reading of the
meaning, not as something to copy.

Workflow: `tools/i18n/loft_i18n.py export <lang>` writes `tools/i18n/work/<lang>.todo.json`. Translate every item
into `tools/i18n/work/<lang>.done.json` as a flat object `{"<id>": "<translation>"}` (plural items:
`{"one": "...", "other": "..."}`), then `tools/i18n/loft_i18n.py import <lang>` checks and writes the files. Fix
whatever it reports and import again. An item with `"previous"` is one whose English changed: start from the
previous translation and bring it in line.

## Voice

- Plain, short, calm. Sentence case in UI text (no Title Case on buttons). Say what happens, not how clever it is.
- Name the place, not the technology: "at home", "the NAS", "this Mac", "this phone". Never call Loft "the cloud";
  the tagline "Not the cloud. The loft." plays on the name: keep the contrast; keep "Loft" as the name and translate
  "loft" (the attic room) if the play works, otherwise rephrase the contrast naturally.
- Address the user the way well-made apps in your language do today: German *du*, French *vous*, Spanish *tú*,
  Italian *tu*, Dutch *je*, Brazilian Portuguese *você*, Japanese polite です/ます without honorific excess, Korean
  polite 해요체 (~하세요/~합니다 for notices is fine), Simplified Chinese 你.
- UI strings must fit: a button or status line should not grow much beyond the English (aim for at most ~30% longer).
  Menu items and buttons: verbs, short.

## Never translate

- Product and company names: Loft, Loft by Incremental, Incremental, Incremental AS. "Loft gateway": keep "Loft",
  translate "gateway" with the term your language's IT users actually use (often the English word) and use it
  identically everywhere.
- Commands, code, file names, paths, URLs, hosts, versions, hashes, anything inside `<code>…</code>`:
  `loft-gateway pair`, `releases.json`, `loft.incremental.no`, `/download/#android`.
- Placeholders and tokens, exactly as written and as many times: `%@`, `%lld`, `%1$d`, `%1$s`, `{n}`, `{route}`,
  `{VERSION}`, `{{...}}`. You may move them within the sentence. `DRAFT: ` at the start of a value stays as is.
- Other brands and technical terms: NAS, SMB, TLS, UniFi, UNAS, Synology, QNAP, TrueNAS, Samba, WireGuard,
  Tailscale, Cloudflare, QR code (use the local form if one is standard), Apache-2.0.

## Platform words

Use the official localized names Apple, Microsoft and Google use in your language for their own features: Finder,
menu bar, System Settings, Files app, Save to Files, share sheet, Photos, Local Network, File Explorer, the
notification area, Google Play, the file picker, Wi-Fi. If unsure, prefer the term the OS itself shows in that
language.

## Markup (website and store)

- Website values can contain HTML. Keep every tag, attribute and link target exactly; translate only the text
  between tags. Keep link paths in English form (`/download/#android`): the build adds the language prefix.
- Store text is plain text: line breaks and "•" bullets only. App Store keywords are written natively (what people
  search for), comma-separated without spaces, max 100 bytes in UTF-8 (CJK characters are 3 bytes each).
  Respect the `max` on each item; the tool counts.
- Use your language's typography: French narrow no-break space (U+202F) before ; : ! ? and «  » quotes; German
  „…“; Japanese 「」 and full-width punctuation; Chinese “” and full-width punctuation; Spanish ¿ ¡. Keep "…" as
  one character. The Norwegian "capital letter after a colon" rule is Norwegian only.

## Be faithful

Translate what the English says and nothing more: no added claims, features, promises or adjectives, nothing
dropped. Privacy, security and licence statements must say exactly what the English says. If an English string is
ambiguous, the Norwegian and the item's catalog note usually settle it.

## Glossary

Record your key term choices in `tools/i18n/glossary/<lang>.md` (gateway, pair/pairing/pairing link, key
fingerprint, at home/away, sign in, sync, upload, the tagline, and any other recurring term) and follow it.
Read it first if it exists: later rounds must stay consistent with it.
