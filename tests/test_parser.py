"""Behaviour tests - runnable with pytest or plain `python tests/test_parser.py`.

No Paperless, no network: only django.conf is stubbed, Pillow is real.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

SCRATCH = Path(tempfile.mkdtemp(prefix="ziptest-"))
_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

if "django.conf" not in sys.modules:
    django = types.ModuleType("django")
    conf = types.ModuleType("django.conf")
    conf.settings = types.SimpleNamespace(SCRATCH_DIR=SCRATCH, THUMBNAIL_FONT_NAME=_FONT)
    django.conf = conf
    sys.modules["django"] = django
    sys.modules["django.conf"] = conf

parsers = importlib.import_module("paperless_zip_parser.parsers")
ZipArchiveParser = parsers.ZipArchiveParser


def _zip(name: str, entries: dict[str, str]) -> Path:
    path = SCRATCH / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry, content in entries.items():
            archive.writestr(entry, content)
    return path


ARCHIVE = _zip("archiv-2026.zip", {"notizen.txt": "hallo " * 500, "unterordner/bild.txt": "x"})
DOCX = _zip("brief.docx", {"[Content_Types].xml": "<Types/>", "word/document.xml": "<d/>"})
EPUB = _zip("buch.epub", {"mimetype": "application/epub+zip", "META-INF/container.xml": "<c/>"})
BROKEN = SCRATCH / "kaputt.zip"
BROKEN.write_bytes(b"PK-kein-echtes-zip")


def test_registry_attributes_present():
    for attr in ("name", "version", "author", "url", "supported_mime_types", "score"):
        assert hasattr(ZipArchiveParser, attr), attr


def test_supported_mime_types():
    supported = ZipArchiveParser.supported_mime_types()
    assert supported["application/zip"] == ".zip"
    assert "application/pdf" not in supported


def test_score_accepts_zip():
    assert ZipArchiveParser.score("application/zip", "a.zip") == 10
    assert ZipArchiveParser.score("application/zip", "a.zip", ARCHIVE) == 10


def test_score_ignores_other_types():
    for mime in (
        "application/pdf",
        "image/jpeg",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        assert ZipArchiveParser.score(mime, "x.bin") is None


def test_score_rejects_office_and_broken_archives():
    for path in (DOCX, EPUB, BROKEN):
        assert ZipArchiveParser.score("application/zip", path.name, path) is None, path


def test_flags():
    parser = ZipArchiveParser()
    try:
        assert parser.can_produce_archive is False
        assert parser.requires_pdf_rendition is True
    finally:
        parser.__exit__(None, None, None)


def test_parse_produces_text_pdf_and_thumbnail():
    payload = ARCHIVE.read_bytes()
    with ZipArchiveParser() as parser:
        parser.configure(None)
        parser.parse(ARCHIVE, "application/zip")

        text = parser.get_text()
        assert "archiv-2026.zip" in text
        assert str(len(payload)) in text

        archive = parser.get_archive_path()
        assert archive is not None and archive.read_bytes()[:5] == b"%PDF-"
        assert parser.get_page_count(ARCHIVE, "application/zip") == 1
        assert parser.extract_metadata(ARCHIVE, "application/zip") == []
        assert parser.get_date() is None

        thumb = parser.get_thumbnail(ARCHIVE, "application/zip")
        assert thumb.read_bytes()[8:12] == b"WEBP"

    assert ARCHIVE.read_bytes() == payload  # original untouched


def test_render_failure_keeps_document():
    original = ZipArchiveParser._note_image

    def boom(self, filename, size):
        raise RuntimeError("no font")

    ZipArchiveParser._note_image = boom
    try:
        with ZipArchiveParser() as parser:
            parser.parse(ARCHIVE, "application/zip")
            assert parser.get_archive_path() is None
            assert "archiv-2026.zip" in parser.get_text()
    finally:
        ZipArchiveParser._note_image = original


def test_english_and_german_text():
    with ZipArchiveParser() as parser:
        parser.parse(ARCHIVE, "application/zip")
        assert parsers._t("text", name="a.zip", size=1).startswith(
            ("ZIP archive:", "ZIP-Archiv:")
        )
        assert parsers._t("title")


def test_language_override():
    import os as _os

    old = _os.environ.get("PAPERLESS_ZIP_PARSER_LANGUAGE")
    try:
        _os.environ["PAPERLESS_ZIP_PARSER_LANGUAGE"] = "de"
        assert parsers._language() == "de"
        assert parsers._t("title") == "ZIP-Archiv"
        _os.environ["PAPERLESS_ZIP_PARSER_LANGUAGE"] = "en"
        assert parsers._t("title") == "ZIP archive"
    finally:
        if old is None:
            del _os.environ["PAPERLESS_ZIP_PARSER_LANGUAGE"]
        else:
            _os.environ["PAPERLESS_ZIP_PARSER_LANGUAGE"] = old


if __name__ == "__main__":
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as exc:  # noqa: BLE001 - runner
                failed.append(name)
                print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(failed)} failed" if failed else "\nall tests passed")
    sys.exit(1 if failed else 0)
