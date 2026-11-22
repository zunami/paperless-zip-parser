# paperless-zip-parser

> **Private project, use at your own risk.** Maintained in spare time, no company, no support, no
> warranty. **No liability for damages, data loss or broken documents** — keep backups, test on a
> copy first. Full text: [DISCLAIMER.md](DISCLAIMER.md).
> *Privates Projekt, Benutzung auf eigenes Risiko, keine Haftung für Schäden oder Datenverlust.*

A [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) parser plugin that stores a **ZIP
archive as a single document**: the file is kept byte-identical, never unpacked, never indexed and
never modified. Tags, correspondent, document type, custom fields and workflows behave like they do
for every other document.

A compact note thumbnail (rendered with Pillow, no external service) shows the file name and size in
the document list, and the original archive stays one click away in the download button.

> **No PDF preview.** Since 1.3.0 the plugin produces no PDF archive version, so there is no
> in-browser document preview - only the list thumbnail. Earlier releases (1.2.x and below) rendered a
> one-page note PDF as the archive version; that behaviour is gone on purpose.

## Why

Paperless-ngx rejects archives it cannot extract text from. But some files must be kept as they are:

- **medical records** - invoices, findings and imaging (X-ray, CT, MRI) that only opens in the
  practice's own viewer, but has to be handed over at a later consultation
- exports of a website, a source drop, a scan batch, invoices per year
- any bundle that only makes sense complete and unchanged

Paperless is the place where you *find* it again; the archive is the thing you actually keep. So the
document gets tags, correspondent, date and a preview, while the ZIP stays byte-identical.

## How it works

| Registry contract | Implementation |
|---|---|
| `supported_mime_types()` | `application/zip` (+ `application/octet-stream`, see below) |
| `score()` | 10 for plain ZIP archives, `None` for anything else |
| `parse()` | reads name and size only; never opens the archive, never renders a PDF |
| `get_text()` | `<localised> archive name + size` - makes the document searchable |
| `get_archive_path()` | always `None` (1.3.1: the PDF archive code path is gone) |
| `get_page_count()` | `None` - a ZIP has no page count |
| `get_thumbnail()` | Pillow-rendered note thumbnail, with a placeholder fallback |
| `requires_pdf_rendition` | `False` since 1.3.0 - no archive version, the download returns the original ZIP |
| `can_produce_archive` | `False` - no OCR, no PDF/A, the original is the document |

The `produce_archive` argument of `parse()` is still accepted for API compatibility and ignored.

The archive is **never opened for extraction**. `_is_plain_archive()` reads entry names from the
central directory and, for a `mimetype` entry, its first 64 bytes - never the payload - to tell a
plain archive from a container that just happens to be a ZIP (docx / xlsx / pptx / ODF / EPUB).
Without that guard the parser would hijack office documents from Tika.

## Download behaviour (changed in 1.3.0, unchanged in 1.3.1)

Since **1.3.0** the plugin sets `requires_pdf_rendition = False`, so Paperless creates **no archive
version** for a ZIP document. The plain **Download** button therefore returns the **original ZIP** -
one click, no dropdown, no login dance. This matters when the file is handed to someone else (a
doctor, an accountant) who should not have to discover a hidden menu entry.

| Version | Download button returns | Archive version | Page count |
|---|---|---|---|
| up to 1.2.0 | the rendered note PDF; original only via the arrow menu | note PDF | 1 |
| 1.3.0 | the original ZIP | none | 1 (not rendered any more) |
| **1.3.1** | **the original ZIP, byte-identical** | **none, no code path left** | **none** |

Trade-off, stated plainly: without an archive version there is **no in-browser preview** of the ZIP.
The list thumbnail is still rendered by `get_thumbnail()`, and `get_text()` still makes the document
searchable by file name and size. If you prefer the preview over the one-click original, stay on
1.2.0 or use a share link with `file_version=original`.

**Existing documents keep their old archive version.** Paperless only sets `archive_filename` when a
parser produces an archive; it never clears it (`documents/tasks.py`, no else branch), and the field
is read-only over the API. Re-processing alone does not remove an old note PDF.

If that matters for an existing document, do not simply delete and re-upload it. A manual migration
means, in this order: export the original file, record its SHA-256 and its metadata (tags,
correspondent, document type, dates, custom fields), keep a database backup, and verify the restored
document afterwards. With a few documents that is quick; with many it is not worth the risk - the old
note PDF is cosmetic and the original ZIP is still the file that gets downloaded when the archive is
missing. Leaving existing documents untouched is a valid choice.

## Install

Two options, both without forking the image.

### 1. A zip on `PYTHONPATH` (no pip, no network, works offline)

Upload the release zip into a folder that is already mounted into the container, for example
`data/paperless-zip-parser.zip`, and point `PYTHONPATH` at it:

```yaml
services:
  paperless:
    environment:
      PYTHONPATH: /usr/src/paperless/data/paperless-zip-parser.zip
```

`zipimport` loads the package straight out of the archive and `importlib.metadata` finds the entry
point inside it. Restart the container; the log shows `Loaded third-party parser`.

### 2. `pip install` at container start

```bash
# in the container's shell or in a custom init script
pip install --no-index --find-links=/path/to/wheels paperless-zip-parser
```

Paperless discovers parsers through the `paperless_ngx.parsers` entry point group on each worker
start, so both routes behave the same.

## The `application/octet-stream` fallback

Some `libmagic` builds - including the one shipped in the official Paperless image - report a real
ZIP as `application/octet-stream`; only an *empty* archive matches the `PK\x05\x06` rule. With such a
build the ZIP never reaches any parser.

The plugin therefore also answers for `application/octet-stream`, but only accepts the file when the
**name ends in `.zip`** *and* `_is_plain_archive()` confirms a plain ZIP. A file called `setup.exe`
or `image.iso` that libmagic reports as `application/octet-stream` is rejected by the parser, so
`score()` never claims it.

Consequence to be aware of: a *renamed* plain ZIP without the `.zip` suffix is not picked up by the
fallback. That is intentional - the suffix is the only cheap discriminator available at upload
validation time, where `score()` is called without a path.

If your `libmagic` behaves (test: `python -c "import magic; print(magic.from_file('x.zip', mime=True))"`),
turn the fallback off:

```yaml
      PAPERLESS_ZIP_PARSER_STRICT_ZIP: "1"
```

## Limitations

- No contents are indexed, by design. The document is searchable by file name and size only.
- **No PDF preview.** The list shows a rendered thumbnail; the document view has no preview page.
- The thumbnail has a fallback chain (default Pillow font, then a white placeholder page). It is not
  unconditionally safe: if the scratch directory cannot be written, `get_thumbnail()` raises a
  `RuntimeError` naming the target path instead of returning a path to a missing file.
- What the format check proves: `_is_plain_archive()` reads the central directory, refuses
  `[Content_Types].xml`, the OOXML part paths (`word/document.xml`, `xl/workbook.xml`,
  `ppt/presentation.xml`), the office suffixes (`.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`, `.odp`,
  `.epub`) and an ODF/EPUB `mimetype` entry - of which at most the first 64 bytes are decompressed.
  It is a discrimination rule, **not** a security scan: a malicious or corrupt archive is not
  detected, only a container that belongs to another parser.
- Guard rails against absurd archives: at most `PAPERLESS_ZIP_PARSER_MAX_ENTRIES` entries
  (default 200000) and `PAPERLESS_ZIP_PARSER_MAX_UNCOMPRESSED` declared unpacked bytes
  (default 64 GiB). Beyond that the file is not treated as a plain ZIP and the parser declines it -
  which means Paperless reports "unsupported" rather than archiving it.
- Reading the central directory costs time proportional to the entry count; see the limit above.
- Tested against Paperless-ngx **3.4.2** (Python 3.12 and 3.13). The parser API is young; a change
  upstream needs a matching release here. Compatibility with other versions is unconfirmed.
- **What is actually tested:** the parser logic and the cleanup safety are covered by offline unit
  tests (no Paperless). Everything that involves a live instance - original download and its
  SHA-256, metadata round-trip, reprocessing of an existing document, which parser wins - is only
  covered by `verify_paperless.py` and stays unverified until that script has run against a test
  instance. The two are not interchangeable.

## Requirements

- Paperless-ngx with the third-party parser registry (3.4.x; 3.5.x unconfirmed).
- Python 3.12 or newer.
- Pillow (already a Paperless dependency, used for the thumbnail).
- No network access, no extra service, no database changes.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install pillow pytest bandit
python tests/test_parser.py      # dependency-free runner for the parser tests
pytest -q                        # whole suite, if pytest is available
python build_wheel.py            # dist/*.whl + dist/paperless-zip-parser.zip
bandit -q -ll -r src/ verify_paperless.py
```

The tests stub `django.conf` only; no Paperless instance is needed.

## Verification against a live instance

`verify_paperless.py` is a maintainer script (not part of the package). It talks to the REST API only,
never to the container or the database.

```bash
export PAPERLESS_URL=https://paperless.example.net   # https for anything that is not localhost
export PAPERLESS_TOKEN=...                           # never committed
python verify_paperless.py baseline   # plugin not yet installed: ZIP and .iso must be rejected
python verify_paperless.py upload     # upload, task, hash, metadata round-trip, negative probes
python verify_paperless.py cleanup    # dry run: prints what it would remove
python verify_paperless.py cleanup --apply --run-id <id>   # removes exactly that run
```

Notes:

- Plain `http://` is refused for non-local hosts unless `PAPERLESS_ALLOW_HTTP=1` is set, so the API
  token is not sent in clear text.
- `upload` opens a **run id** in `.verify/run_manifest.json` and records every document, tag and
  correspondent *with its name, immediately after it was created*. If the manifest can not be
  written, the stage aborts and prints the object so it can be removed by hand - a recorded id is
  the only thing cleanup will ever touch.
- The manifest also stores the instance URL; a manifest written for a different `PAPERLESS_URL` is
  refused, because document ids are only meaningful in that database.
- `cleanup` works from that manifest only - it never searches the instance. Deleting requires
  `--apply --run-id <id>`; a dry run is the default. Before each delete the object is fetched and
  its **name compared with the recorded name**: an id that now belongs to a real document is left
  alone and reported. Objects that are already gone count as done.
- Redirects are refused: `urllib` would otherwise resend the `Authorization` header to whatever host
  the redirect names. A redirect is reported as an error status instead of being followed.
- The upload stage also probes the neighbours: a generated, valid DOCX must be imported and must not
  end up with `mime_type = application/zip`. A rejected DOCX is reported as "Tika not proven", not as
  a pass - a rejection says nothing about the office parser. A `.iso` must be rejected. If the
  instance reports the test ZIP as `application/octet-stream` the import still succeeds via the
  documented MIME fallback - the document then proves the fallback works on that libmagic build.
- Document deletion in Paperless moves the document to the trash, it does not remove the files.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Private project, no company behind it. Use at your own risk: **no warranty and no liability for
damages, data loss, broken or unreadable documents, missing metadata or downtime.** Keep independent
backups and test on a copy before using it on a productive archive. This plugin does not encrypt
anything, and archiving a file unchanged does not guarantee it stays readable years from now.

Full text in English and German: [DISCLAIMER.md](DISCLAIMER.md)

