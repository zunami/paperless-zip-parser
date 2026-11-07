# paperless-zip-parser

> **Private project, use at your own risk.** Maintained in spare time, no company, no support, no
> warranty. **No liability for damages, data loss or broken documents** — keep backups, test on a
> copy first. Full text: [DISCLAIMER.md](DISCLAIMER.md).
> *Privates Projekt, Benutzung auf eigenes Risiko, keine Haftung für Schäden oder Datenverlust.*

A [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) parser plugin that stores a **ZIP
archive as a single document**: the file is kept byte-identical, never unpacked, never indexed and
never modified. Tags, correspondent, document type, custom fields and workflows behave like they do
for every other document.

A one-page note PDF (rendered with Pillow, no external service) gives the document a preview; the
original archive stays one click away in the download button.

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
| `parse()` | reads name and size only, renders the note page |
| `get_text()` | `<localised> archive name + size` - makes the document searchable |
| `get_archive_path()` | the note page PDF (used as the preview) |
| `requires_pdf_rendition` | `True` - Paperless shows the PDF instead of trying to render a ZIP |
| `can_produce_archive` | `False` - no OCR, no PDF/A, the original is the document |

The archive is **never opened for extraction**. `_is_plain_archive()` reads entry names from the
central directory only, to tell a plain archive from a container that just happens to be a ZIP
(docx / xlsx / pptx / ODF / EPUB). Without that guard the parser would hijack office documents from
Tika.

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

The plugin therefore also answers for `application/octet-stream`, but only accepts the file when
`_is_plain_archive()` confirms it is a plain ZIP. Everything else is rejected before it is stored.

Consequence to be aware of: with the fallback on, *any* unknown binary passes the upload check and
fails later in the consumer with `Unsupported mime type`. Nothing wrong is archived.

If your `libmagic` behaves (test: `python -c "import magic; print(magic.from_file('x.zip', mime=True))"`),
turn the fallback off:

```yaml
      PAPERLESS_ZIP_PARSER_STRICT_ZIP: "1"
```

## Limitations

- No contents are indexed, by design. The document is searchable by file name and size only.
- The preview is a rendered note page, not the archive contents.
- `_is_plain_archive()` reads the ZIP central directory; the cost grows with the number of entries.
- Tested against Paperless-ngx **3.4.2**. The parser API is young; a change upstream needs a matching
  release here.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install pillow pytest
python tests/test_parser.py      # dependency-free runner
pytest -q                        # same tests, if pytest is available
python build_wheel.py            # dist/*.whl + dist/paperless-zip-parser.zip
bandit -r src/
```

The tests stub `django.conf` only; no Paperless instance is needed. `verify_paperless.py` is a
maintainer script that runs an end-to-end check against a live instance (upload, task state, hash of
the stored original, metadata round-trip).

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Private project, no company behind it. Use at your own risk: **no warranty and no liability for
damages, data loss, broken or unreadable documents, missing metadata or downtime.** Keep independent
backups and test on a copy before using it on a productive archive. This plugin does not encrypt
anything, and archiving a file unchanged does not guarantee it stays readable years from now.

Full text in English and German: [DISCLAIMER.md](DISCLAIMER.md)

## Live verification (maintainer)

`verify_paperless.py` runs an end-to-end check against a real instance:

```bash
export PAPERLESS_URL=http://paperless.example:8000
export PAPERLESS_TOKEN=<api token with add_document>
python verify_paperless.py upload
```
