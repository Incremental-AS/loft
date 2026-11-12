# Content search

The gateway finds files by the words inside them, not only by their names: the REST API's
`GET /v1/search` (apps) and the MCP tool `search_files` (assistants). The index lives on the
gateway, beside the NAS, in the gateway's own database. Nothing is sent anywhere else to build it.

## What is indexed

- Plain text: `.txt`, `.text`, `.md`, `.markdown`, `.mdown`, `.csv`, `.tsv`, `.log`, `.json`,
  `.yaml`, `.yml` (UTF-8, UTF-16 with a byte order mark, else Windows-1252), the first 8 MiB.
- Documents: PDF (the text layer; scanned pages without one have no text), Word `.docx`,
  Excel `.xlsx`, PowerPoint `.pptx` (with speaker notes), OpenDocument `.odt`, `.ods`, `.odp`.
  Their text is extracted in the sandboxed worker that also makes thumbnails
  (`thumbworker.py`), never in the gateway process.
- Up to `search_text_max_chars` characters of each file's text (2 000 000 by default), files
  up to `search_max_file_mb` (256 MiB by default).

A file is read as the account whose folder it is in, through a session of one of that
account's signed-in devices or assistant connections. The gateway never keeps a NAS password,
so a folder is indexed while someone of that account is connected (the Keeps apps stay
connected while they run). New and changed files are indexed as the gateway learns of them
(change notify, the NAS agent, rescans, saves through Keeps). A file that is deleted, moved to
the trash or replaced leaves the index in the same database transaction that records it; the
indexer's sweep removes anything else that is gone. A file that can't be read is tried three
times, then left until it changes.

The index stores each document's words and their stems, and the extracted text compressed
(for the short passage shown with a hit). Expect the database to grow by roughly the size of
the documents' text. `search_index = false` turns content search off (names only).

## Who sees a hit

The index only proposes candidates. Each hit is checked when it is asked for:

1. the file is still live in the caller's own index root (an app: the folder its device
   chose; an assistant: the folders the user has granted it at the time of the call, in the
   call's context);
2. the caller's own NAS session can still open it (a fresh stat), and it isn't in a blocked
   share or path.

A hit that fails either is dropped, so another account's file, a folder the NAS no longer
lets the account open, or a folder taken away from an assistant never shows up. If the file
changed since it was indexed, the hit is shown without a passage and the file is indexed again.

## Queries

A file matches when it has every word of the query, each in any of its forms. Words are
case-insensitive and Unicode-normalised (NFKC); accents are kept (`får` and `far` are different
words). Chinese and Japanese text is split into overlapping pairs of characters. Hits come
best first (BM25).

## Languages

Each word is indexed as itself and as its stem in every language of `search_languages`, so
"budgets" finds "budget" and "regnskapet" finds "regnskap". The stems come from the Snowball
stemmers (`snowballstemmer`, BSD-3-Clause). Default: `["en"]`. Each language added makes the
index somewhat bigger and the matches a little looser; changing the list re-indexes every
document.

| Language | Tag | Stemming |
|---|---|---|
| English | `en` | Snowball english |
| Norwegian (Bokmål, Nynorsk) | `nb`, `nn` | Snowball norwegian |
| Swedish | `sv` | Snowball swedish |
| Danish | `da` | Snowball danish |
| Finnish | `fi` | Snowball finnish |
| German | `de` | Snowball german |
| French | `fr` | Snowball french |
| Spanish | `es` | Snowball spanish |
| Portuguese | `pt` | Snowball portuguese |
| Italian | `it` | Snowball italian |
| Dutch | `nl` | Snowball dutch |
| Polish | `pl` | Snowball polish |
| Czech | `cs` | Snowball czech |
| Hungarian | `hu` | Snowball hungarian |
| Romanian | `ro` | Snowball romanian |
| Greek | `el` | Snowball greek |
| Estonian | `et` | Snowball estonian |
| Lithuanian | `lt` | Snowball lithuanian |
| Hindi | `hi` | Snowball hindi |
| Arabic | `ar` | Snowball arabic |
| Turkish | `tr` | Snowball turkish |
| Indonesian | `id` | Snowball indonesian |

Searched as whole words, no stemming (no Snowball stemmer for them): Slovak `sk`, Slovenian
`sl`, Croatian `hr`, Bulgarian `bg`, Latvian `lv`, Ukrainian `uk`, Korean `ko`; Chinese `zh`
and Japanese `ja` by character pairs. Region and script subtags are accepted and ignored
(`es-419`, `pt-BR`, `zh-Hans`).

## Settings (gateway.toml)

```toml
search_index = true
search_languages = ["en"]          # e.g. ["nb", "en"]
search_text_max_chars = 2000000
search_max_file_mb = 256
```
