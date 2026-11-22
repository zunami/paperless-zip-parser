# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.3.1] - unreleased

Hardening release. No behaviour change for a normal ZIP import: the document is still stored
byte-identical, nothing is extracted, the download button still returns the original file.

### First review round (P0/P1 behaviour)

- `verify_paperless.py`: cleanup no longer searches the instance for documents that "look like" test
  documents and deletes them. Every upload stage opens a run id in `.verify/run_manifest.json`,
  records the document, tag and correspondent ids it creates, and cleanup works from that manifest
  only. Missing or unreadable manifest means no deletion. Cleanup is a dry run unless `--apply` is
  passed.
- `parsers.py`: the thumbnail renderer no longer aborts an import. A missing font falls back to
  Pillow's default, a failed layout falls back to a white placeholder page.
- `parsers.py`: no PDF archive is produced under any circumstance. The `produce_archive` branch,
  `_note_pdf()` and the stored archive path are gone; `get_archive_path()` always returns `None` and
  `get_page_count()` no longer reports a made-up single page.
- `parsers.py`: a plain ZIP is no longer excluded because it contains a `META-INF/` entry or a file
  named `mimetype`; ODF and EPUB are recognised by the content of their `mimetype` entry.
- `verify_paperless.py`: plain `http://` is refused for non-local hosts unless
  `PAPERLESS_ALLOW_HTTP=1` is set, so an API token is not sent in clear text.

### Second review round (confirmed findings)

- `verify_paperless.py`: redirects are refused. `urllib` follows a 302 and resends the
  `Authorization` header to the redirect target, which would leak the API token to a foreign host;
  the request now goes through an opener that turns every redirect into an error status.
- `verify_paperless.py`: the run manifest is bound to the instance it was written against. A
  manifest from a different `PAPERLESS_URL`, or one written by an older script version without an
  instance tag, is refused instead of deleting ids that belong to another database.
- `verify_paperless.py`: `call(raw=True)` returns `bytes` on success and `None` on an error status; a
  JSON error body can no longer be sliced as file content (`TypeError`/`KeyError`). A SHA-256 is only
  computed for a body that actually arrived.
- `parsers.py`: the `mimetype` probe no longer calls `ZipFile.read()`. It streams at most 64 bytes
  through `ZipFile.open()`, so a crafted `mimetype` entry can not inflate memory. Additionally a
  central directory with more than `PAPERLESS_ZIP_PARSER_MAX_ENTRIES` entries (default 200000) or
  more than `PAPERLESS_ZIP_PARSER_MAX_UNCOMPRESSED` declared bytes (default 64 GiB) is declined.
- `parsers.py`: a plain ZIP is no longer excluded because it contains a folder named `word/`, `xl/`
  or `ppt/`. OOXML is detected by `[Content_Types].xml` or by a real part path
  (`word/document.xml`, `xl/workbook.xml`, `ppt/presentation.xml`); office/ebook suffixes are
  rejected as well.
- `parsers.py`: the generic MIME fallback only accepts names ending in `.zip`. An `.iso`, `.exe` or
  image reported as `application/octet-stream` is no longer claimed by `score()`.
- `parsers.py`: the instance language (`LANGUAGE_CODE`) is evaluated before the worker's active
  language, so a background task running with `en` no longer hides a German instance setting.
- `parsers.py`: `get_thumbnail()` raises a `RuntimeError` naming the target path if even the
  placeholder can not be written, instead of returning a path to a missing file.
- `build_wheel.py` / `pyproject.toml`: the wheel now carries `LICENSE` (and `.dist-info/licenses/`),
  declares `License-Expression: MIT` and `License-File`, uses Metadata-Version 2.4, and the project
  URLs point to this repository instead of github.com.
- `parsers.py`: `ZipArchiveParser.version` is taken from the package instead of a second hard-coded
  copy.
- `verify_paperless.py`: the DOCX probe now uploads a genuinely valid OOXML package instead of an
  empty archive with one entry, and checks that the document does not end up as `application/zip`.

### Third review round (verified in the code)

- `verify_paperless.py`: the upload stage no longer deletes its tag and correspondent itself. Both are
  written to the manifest right after creation and removed by `cleanup`, so a run that aborts leaves
  no orphaned objects and no unregistered ones.
- `verify_paperless.py`: the manifest keeps the object **name** next to the id (`{"id": 11, "name": ...}`)
  and `cleanup` fetches each object before deleting it. A mismatching name - an id reused for a real
  document - is never deleted, it is reported and stays in the manifest. Entries without a name are
  never deleted either.
- `verify_paperless.py`: `cleanup --apply` now requires `--run-id`; without it the delete phase is
  refused. `cleanup` no longer overwrites the manifest with `{"runs": ...}` - the instance tag
  survives, so the binding from round 2 is not lost on the first cleanup.
- `verify_paperless.py`: `record()` aborts the stage when the manifest can not be saved and prints
  `CLEANUP REQUIRED BY HAND` with kind, id and name. Ids are registered immediately after creation
  (document before any further check, tag and correspondent right after their POST).
- `verify_paperless.py`: the DOCX probe no longer counts a rejection as a pass. "Not claimed by the
  ZIP parser" and "parsed by Tika (import succeeded)" are two separate checks.
- Tests: 69 (6 new for the cleanup changes: `--apply` without run id, identity mismatch, entry
  without name, already-gone object, instance tag survives cleanup, manifest write failure aborts).

### Fourth review round (identity and ordering in the upload stage)

- `verify_paperless.py`: the document id is written to the manifest **before** the next request.
  Previously the read-back GET came first, so a connection error there left a created document with
  no manifest entry at all.
- `verify_paperless.py`: the manifest name is the uploaded file name (`original_file_name` in
  Paperless), not the title - it is available before any read-back.
- `verify_paperless.py`: `cleanup` treats an object as identified when the recorded name matches
  `title`, `original_file_name`, `name`, or the title without its extension. A renamed document
  whose id was reused is still not deleted.
- `verify_paperless.py`: the metadata stage writes the new title into the manifest **only** when the
  PATCH succeeded and the read-back shows exactly that title. A rejected PATCH used to leave a name
  that matches nothing, so cleanup would refuse to delete the test document.
- `verify_paperless.py`: the DOCX and unexpected-document probes record the real file name instead of
  placeholders like `docx-probe 11`, which no identity check could ever match.
- Tests: 73 (4 new: identity via `original_file_name`, identity via title without extension, failed
  PATCH keeps the file name, id recorded before the first read-back).

### Fifth review round (id extraction, honest verdicts, archive cleanliness)

- `verify_paperless.py`: new `created_ids(task)` collects the document id from **both**
  `result_data.document_id` and `related_document_ids`, deduplicated. Baseline, negative probe, DOCX
  probe and the main upload all register every id it returns - before any read-back. A version that
  reports only `related_document_ids` (or only `result_data`) can no longer leave an unregistered
  document behind.
- `verify_paperless.py`: the metadata stage keeps `patch_status` and `get_status` apart and checks
  them in `metadata PATCH accepted`. Overwriting the PATCH status with the GET result hid a rejected
  PATCH.
- `verify_paperless.py`: the DOCX probe no longer reports one merged verdict. Five checks:
  consumer task succeeded, document readable, document id confirmed, mime type is not
  `application/zip`, import succeeded via a non-ZIP parser. No label claims direct Tika evidence.
- Repository: the review archive `paperless-zip-parser-1.3.1-review.zip` was committed by mistake and
  shipped inside the next archive. Removed, and `*.zip` is now ignored (test fixtures exempt).
- Tests: 77 (4 new: id union and dedupe, missing task data, visible PATCH failure, DOCX verdicts).

### Sixth review round (baseline iso, rename gate)

- `verify_paperless.py`: the baseline stage now records the documents of an **unexpectedly accepted**
  `.iso` upload. It awaited the task and dropped the ids - the ZIP branch already recorded, the iso
  branch did not.
- `verify_paperless.py`: the manifest title is only renamed when the PATCH returned `200`/`204` **and**
  the read-back shows the new title. The read-back alone was not enough: an instance can apply the
  title and still answer with an error status.
- Tests: 79 (2 new: baseline records an accepted iso, rename needs a successful PATCH).

### Added

- Regression tests for every item above (`tests/test_hardening.py`, extended
  `tests/test_cleanup_manifest.py`): 79 tests total.
- README: suffix rule of the MIME fallback, entry/size guard rails, the honest limits of the
  thumbnail fallback, the difference between unit-tested parser logic and unverified live
  integration, and the removed contradictory plain-HTTP example.


### Documentation

- README: thumbnail vs. PDF preview clarified, download table extended, ZIP validation limits stated
  plainly, MIME fallback and strict mode explained, requirements listed, HTTPS requirement for the
  verification script documented, and the "just delete and re-upload" advice replaced by a cautious
  migration note.

## [1.3.0]

- No archive version for ZIP documents (`requires_pdf_rendition = False`); the download button
  returns the original ZIP, no dropdown.
- Byte-identical storage verified by SHA-256 in `verify_paperless.py`.

## [1.2.0]

- Note page rendered as the archive version (retired in 1.3.0).
