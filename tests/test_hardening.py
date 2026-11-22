"""Regression tests for the second review round (areas 3, 4, 5, 6, 8).

Each test names the finding it guards, so a failure points at the review item.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

import paperless_zip_parser.parsers as parsers
from paperless_zip_parser.parsers import ZipArchiveParser

ZIP_MIME = "application/zip"
FALLBACK = parsers.FALLBACK_MIME


def _zip(path: Path, entries: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


# --- area 4: a directory named "word/" must not exclude a plain ZIP ---------------------


def test_folder_named_word_does_not_exclude_a_plain_zip(tmp_path):
    archive = _zip(tmp_path / "photos.zip", {
        "word/notes.txt": "just a folder called word",
        "xl/table.csv": "a, b\n1, 2",
        "ppt/readme.md": "# not a presentation",
    })
    assert ZipArchiveParser.score(ZIP_MIME, archive.name, archive) == 10


def test_real_ooxml_parts_are_still_excluded(tmp_path):
    for part in ("word/document.xml", "xl/workbook.xml", "ppt/presentation.xml"):
        archive = _zip(tmp_path / f"{part.split('/')[0]}.zip", {part: "<xml/>"})
        assert ZipArchiveParser.score(ZIP_MIME, archive.name, archive) is None, part


def test_office_suffix_is_excluded_even_without_ooxml_marker(tmp_path):
    archive = _zip(tmp_path / "letter.docx", {"notes.txt": "no marker inside"})
    assert ZipArchiveParser.score(ZIP_MIME, archive.name, archive) is None


# --- area 3: no decompression bomb through the mimetype probe --------------------------


def test_mimetype_probe_does_not_inflate_a_bomb(tmp_path):
    bomb = _zip(tmp_path / "bomb.zip", {"mimetype": b"\0" * (200 * 1024 * 1024), "a.txt": "x"})
    started = time.monotonic()
    accepted = ZipArchiveParser.score(ZIP_MIME, bomb.name, bomb)
    elapsed = time.monotonic() - started
    assert accepted == 10, "a plain archive with a mimetype file must stay accepted"
    assert elapsed < 2.0, f"probe took {elapsed:.1f}s - the entry was inflated"


def test_entry_limit_rejects_absurd_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "_MAX_ENTRIES", 5)
    archive = _zip(tmp_path / "many.zip", {f"f{i}.txt": "x" for i in range(20)})
    assert ZipArchiveParser.score(ZIP_MIME, archive.name, archive) is None


def test_uncompressed_limit_rejects_absurd_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "_MAX_UNCOMPRESSED", 1024)
    archive = _zip(tmp_path / "big.zip", {"big.txt": "x" * 8192})
    assert ZipArchiveParser.score(ZIP_MIME, archive.name, archive) is None


# --- area 5: the generic MIME fallback must not claim foreign binaries -----------------


@pytest.mark.parametrize("name", ["malware.iso", "setup.exe", "photo.jpeg", "noext"])
def test_fallback_mime_refuses_foreign_suffixes(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"not a zip at all")
    assert ZipArchiveParser.score(FALLBACK, name, path) is None


def test_fallback_mime_accepts_a_real_zip(tmp_path):
    archive = _zip(tmp_path / "data.zip", {"a.txt": "x"})
    assert ZipArchiveParser.score(FALLBACK, archive.name, archive) == 10


def test_zip_mime_refuses_a_foreign_suffix(tmp_path):
    path = tmp_path / "renamed.tar"
    path.write_bytes(b"not a zip")
    assert ZipArchiveParser.score(ZIP_MIME, path.name, path) is None


# --- area 6: instance language wins over the worker default ---------------------------


def test_instance_language_beats_worker_language(monkeypatch):
    """A worker running with en-US must not hide LANGUAGE_CODE="de-at" from the instance."""
    import sys
    import types

    from django.conf import settings

    monkeypatch.setattr(settings, "LANGUAGE_CODE", "de-at", raising=False)
    monkeypatch.delenv("PAPERLESS_ZIP_PARSER_LANGUAGE", raising=False)
    worker = types.ModuleType("django.utils.translation")
    worker.get_language = lambda: "en-us"
    parent = types.ModuleType("django.utils")
    parent.translation = worker
    monkeypatch.setitem(sys.modules, "django.utils", parent)
    monkeypatch.setitem(sys.modules, "django.utils.translation", worker)
    assert parsers._language() == "de"


def test_explicit_override_still_wins(monkeypatch):
    monkeypatch.setenv("PAPERLESS_ZIP_PARSER_LANGUAGE", "en")
    assert parsers._language() == "en"


# --- area 8: thumbnail failure on the filesystem is reported, not hidden --------------


def test_thumbnail_filesystem_failure_raises_a_clear_error(tmp_path, monkeypatch):
    archive = _zip(tmp_path / "a.zip", {"x.txt": "x"})

    def broken_layout(self, filename, size):
        raise RuntimeError("layout broken on purpose")

    with ZipArchiveParser() as parser:
        parser._tempdir = tmp_path / "does-not-exist" / "deeper"
        monkeypatch.setattr(ZipArchiveParser, "_note_image", broken_layout)
        with pytest.raises(RuntimeError) as err:
            parser.get_thumbnail(archive, ZIP_MIME)
    assert "thumb.webp" in str(err.value)


# --- area 7: the built distribution carries license and correct version ---------------


def test_built_distribution_contains_license_and_metadata():
    import build_wheel

    entries = dict(build_wheel.files())
    metadata = entries[f"{build_wheel.DIST_INFO}/METADATA"].decode()
    assert "License-Expression: MIT" in metadata
    assert f"Version: {build_wheel.VERSION}" in metadata
    assert "https://github.com/zunami/paperless-zip-parser/issues" in metadata
    assert entries[f"{build_wheel.DIST_INFO}/licenses/LICENSE"].strip(), "LICENSE missing in wheel"


def test_parser_version_comes_from_the_package():
    import paperless_zip_parser

    assert ZipArchiveParser.version == paperless_zip_parser.__version__
