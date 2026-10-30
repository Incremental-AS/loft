# Loft for Windows: UI text

Every word Loft for Windows shows is in this folder, one JSON file per language. `en.json` is the
source; `nb.json` is Norwegian (bokmål). Code never contains UI text and never branches on language:
`Strings.cs` (class `S`) names each string by key, `Localization.cs` loads the files and picks one.

## Wording rules

The wording follows the Mac app's (`apple/App/Copy.swift`), with "this PC" and Explorer where the Mac
says "this Mac" and Finder.

- Sentence case everywhere: buttons, menu items, headings ("Pause syncing", not "Pause Syncing").
- Name the place: "home", "the NAS", "this PC". Never call Loft or the NAS "cloud".
- Use Windows' own names for Windows things, in that language's Windows wording: Explorer (Utforsker),
  "Always keep on this device", "Free up space", Ctrl+S, AutoSave, AutoRecover.
- Norwegian: a capital letter after every colon ("Borte: Ikke satt opp"), but not in times or URLs.
  Norwegian puts a space before the ellipsis ("Logg inn …"); English doesn't ("Sign in…").
- Change the English in its own commit, not while translating. Don't reword an existing string while
  translating another.

## The files

- One flat JSON object: `"key": "text"`, UTF-8, no comments. Keys are lower case, dotted, stable
  (`tray.up_to_date`, `settings.cache_help`); a translation must use English's keys and may lack some.
- Keys starting with `_` (e.g. `_note` from the translation tool) are notes and are ignored.
- Placeholders are named in braces: `{route}`, `{n}`, `{version}`. A translation keeps exactly the same
  placeholders as English (it may move them, not add, drop or rename them). Braces mean nothing else.
- Plurals are two keys, `<key>.one` and `<key>.other`; `{n}` is the count. Which one is used:
  - `one` when n = 1: en, nb, de, es, it, nl (and any language not listed)
  - `one` when n is 0 or 1: fr, pt-BR
  - always `other`: ja, ko, zh-Hans. Still write both keys; they may be identical.
- A key missing from a translation shows the English text (English plural rule included), so a
  half-done file is safe to ship. A key missing from `en.json` shows as `⟦key⟧`, and the tests fail.

## Which language Loft uses

Windows' display language (`CultureInfo.CurrentUICulture`), read once at start:

1. The exact name, then each parent (`de-AT` → `de`, `zh-CN` → `zh-Hans`, `pt-BR` stays `pt-BR`).
2. nb, nn and no (any region) → `nb`. Plain `pt` (so also `pt-PT`) → `pt-BR`. Chinese that isn't
   Traditional (zh-TW, zh-HK, zh-MO, zh-Hant) → `zh-Hans`; Traditional falls through to English.
3. Otherwise `en`.

## Adding a language

1. Add `Strings/<name>.json` with .NET's culture name as the file name: `de.json`, `fr.json`,
   `pt-BR.json`, `ja.json`, `zh-Hans.json`. Nothing else: the csproj embeds `Strings/*.json` by
   wildcard, and the language is picked up automatically.
2. If its plural rule isn't one of the three above, add it to `PluralRules.Category` in
   `Localization.cs` (and a test).
3. Run `dotnet test tests/Loft.Core.Tests` (any OS). It checks every file in this folder: keys are
   English keys, placeholders match English, plural keys come in pairs, the file name resolves to
   itself, and every `S` member renders without a missing key or a leftover placeholder.

## Adding a string

Add the key to `en.json` and `nb.json` (nb must stay complete; the tests check it), then a member to
`S` in `Strings.cs`. Other languages show English until their translators catch up.
