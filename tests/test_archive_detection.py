"""Archive classification and parser edge cases. No Paperless, no network.

Covers the type discrimination the plugin relies on (T01-T13, T19-T22 of the review list).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

import paperless_zip_parser.parsers as parsers
from paperless_zip_parser.parsers import ZipArchiveParser

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ZIP_MIME = "application/zip"


def _zip(path: Path, entries: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def _score(path: Path) -> int | None:
    return ZipArchiveParser.score(ZIP_MIME, path.name, path)


# --- T01/T02/T04: plain archives are accepted -------------------------------------------


def test_plain_zip_is_accepted(tmp_path):
    archive = _zip(tmp_path / "docs.zip", {"a.txt": "hello", "sub/b.txt": "world"})
    assert _score(archive) == 10
    assert parsers._is_plain_archive(archive) is True


def test_empty_but_valid_zip_is_accepted(tmp_path):
    archive = _zip(tmp_path / "empty.zip", {})
    assert _score(archive) == 10


# --- T03: broken archives are rejected, upload validation still passes ------------------


def test_broken_zip_is_rejected_when_the_path_is_known(tmp_path):
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"PK\x03\x04 definitely not a zip")
    assert _score(broken) is None


def test_score_without_path_accepts_the_upload_so_the_consumer_can_decide():
    # serialisers.py calls score() without a path; a None there would reject every ZIP.
    assert ZipArchiveParser.score(ZIP_MIME, "whatever.zip") == 10


# --- T05/T06: office and ebook containers are left alone -------------------------------


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param({"[Content_Types].xml": "<Types/>", "word/document.xml": "<w/>"},
                     id="docx"),
        pytest.param({"[Content_Types].xml": "<Types/>", "xl/workbook.xml": "<w/>"},
                     id="xlsx"),
        pytest.param({"[Content_Types].xml": "<Types/>", "ppt/presentation.xml": "<p/>"},
                     id="pptx"),
        pytest.param({"mimetype": "application/vnd.oasis.opendocument.text",
                      "content.xml": "<office/>"}, id="odt"),
        pytest.param({"mimetype": "application/vnd.oasis.opendocument.spreadsheet",
                      "content.xml": "<office/>"}, id="ods"),
        pytest.param({"mimetype": "application/epub+zip",
                      "META-INF/container.xml": "<container/>"}, id="epub"),
    ],
)
def test_foreign_containers_are_not_taken_over(tmp_path, entries):
    archive = _zip(tmp_path / "foreign.zip", entries)
    assert _score(archive) is None, f"{archive} must not be treated as a plain ZIP"


# --- T07/T08: no false exclusion for look-alike entries --------------------------------


def test_meta_inf_without_container_is_still_a_plain_zip(tmp_path):
    archive = _zip(tmp_path / "signed.zip",
                   {"META-INF/MANIFEST.MF": "Manifest-Version: 1.0", "data/x.txt": "x"})
    assert _score(archive) == 10


def test_entry_named_mimetype_does_not_exclude_a_plain_zip(tmp_path):
    archive = _zip(tmp_path / "odd.zip", {"mimetype": "text/plain", "a.txt": "x"})
    assert _score(archive) == 10


# --- T09: unrelated binaries are not archived ------------------------------------------


@pytest.mark.parametrize("suffix", [".iso", ".exe"])
def test_non_archive_binaries_are_not_parsed(tmp_path, suffix):
    blob = tmp_path / f"payload{suffix}"
    blob.write_bytes(b"\x00" * 4096 + b"CD001" + b"\x00" * 512)
    assert _score(blob) is None


# --- T10/T11: generic MIME fallback and strict mode ------------------------------------


def test_generic_mime_fallback_is_enabled_by_default():
    assert parsers.FALLBACK_MIME in ZipArchiveParser.supported_mime_types()


def test_strict_mode_drops_the_generic_fallback():
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import paperless_zip_parser.parsers as p\n"
        "print(p.FALLBACK_MIME in p.ZipArchiveParser.supported_mime_types())\n"
        "print(sorted(p.ZipArchiveParser.supported_mime_types()))\n" % str(SRC)
    )
    env = {**os.environ, "PAPERLESS_ZIP_PARSER_STRICT_ZIP": "1"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env=env, check=True)
    first, second = result.stdout.strip().splitlines()[:2]
    assert first == "False", second
    assert parsers.FALLBACK_MIME not in second


# --- T12/T13: thumbnail survives a broken font and a hostile file name -----------------


def test_thumbnail_survives_a_missing_font(tmp_path):
    from django.conf import settings

    archive = _zip(tmp_path / "a.zip", {"x.txt": "x"})
    original = settings.THUMBNAIL_FONT_NAME
    settings.THUMBNAIL_FONT_NAME = "/nonexistent/font.ttf"
    try:
        with ZipArchiveParser() as parser:
            thumb = parser.get_thumbnail(archive, ZIP_MIME)
            assert thumb.read_bytes()[8:12] == b"WEBP"
    finally:
        settings.THUMBNAIL_FONT_NAME = original


def test_long_unicode_file_name_does_not_break_the_thumbnail(tmp_path):
    name = ("Dokumente_Übersicht_2026_" + "sehr_langer_name_" * 12)[:200] + ".zip"
    archive = _zip(tmp_path / name, {"a.txt": "x"})
    with ZipArchiveParser() as parser:
        parser.parse(archive, ZIP_MIME)
        thumb = parser.get_thumbnail(archive, ZIP_MIME)
        assert thumb.read_bytes()[8:12] == b"WEBP"


# --- T19: many entries stay cheap ------------------------------------------------------


def test_archive_with_many_entries_is_handled(tmp_path):
    archive = _zip(tmp_path / "big.zip",
                   {f"dir{i % 40}/file{i}.txt": "x" for i in range(3000)})
    assert _score(archive) == 10
    with ZipArchiveParser() as parser:
        parser.parse(archive, ZIP_MIME)
        assert parser.get_archive_path() is None
        assert "big.zip" in parser.get_text()


# --- T21/T22: version consistency and untouched original -------------------------------


def test_version_is_identical_in_all_three_places():
    import paperless_zip_parser

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert paperless_zip_parser.__version__ == ZipArchiveParser.version
    assert pyproject["project"]["version"] == ZipArchiveParser.version


def test_original_bytes_are_never_modified(tmp_path):
    archive = _zip(tmp_path / "keep.zip", {"inside.txt": "content stays inside"})
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    with ZipArchiveParser() as parser:
        parser.parse(archive, ZIP_MIME)
        parser.get_thumbnail(archive, ZIP_MIME)
        parser.get_page_count(archive, ZIP_MIME)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before
