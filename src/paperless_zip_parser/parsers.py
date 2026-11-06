"""Paperless-ngx parser that stores a ZIP archive as one document.

The archive is kept byte-identical and is never unpacked, scanned or modified.
Tags, correspondent, document type, custom fields and workflows work as usual.

A one-page note PDF is rendered with Pillow for the preview pane - no Gotenberg
and no other external service. If rendering fails the document is still created,
only without an archive version.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Self

logger = logging.getLogger("paperless.parsing.zip_archive")

MIME = "application/zip"
# Some libmagic builds (e.g. the one in the official image) report real ZIPs as
# application/octet-stream - only empty archives match the PK\x05\x06 rule. The
# content is verified before such a file is accepted.
# PAPERLESS_ZIP_PARSER_STRICT_ZIP=1 disables the fallback and accepts ZIP only.
FALLBACK_MIME = "application/octet-stream"
_STRICT = os.environ.get("PAPERLESS_ZIP_PARSER_STRICT_ZIP", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
_SUPPORTED: dict[str, str] = (
    {MIME: ".zip"} if _STRICT else {MIME: ".zip", FALLBACK_MIME: ".zip"}
)

# ZIP is also the container format of OOXML (docx/xlsx/pptx), ODF and EPUB.
# Without this guard the parser would take over office documents and bypass Tika.
_FOREIGN_MARKERS = ("[Content_Types].xml", "mimetype", "META-INF/container.xml")

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "title": "ZIP archive",
        "file": "File:",
        "size": "Size: {size:,} bytes",
        "body": (
            "Stored unchanged. The content was not extracted or indexed -\n"
            "the original is available through the download function."
        ),
        "text": "ZIP archive: {name} ({size:,} bytes)",
    },
    "de": {
        "title": "ZIP-Archiv",
        "file": "Datei:",
        "size": "Gr\u00f6\u00dfe: {size:,} Bytes",
        "body": (
            "Unver\u00e4ndert archiviert. Der Inhalt wurde nicht entpackt oder\n"
            "indiziert - das Original steht \u00fcber die Download-Funktion bereit."
        ),
        "text": "ZIP-Archiv: {name} ({size:,} Bytes)",
    },
}


def _language() -> str:
    """Language for the note text: env override, then Django, then English."""
    forced = os.environ.get("PAPERLESS_ZIP_PARSER_LANGUAGE", "").strip()
    if forced:
        return forced[:2].lower()
    try:
        from django.conf import settings
        from django.utils.translation import get_language

        code = get_language() or getattr(settings, "LANGUAGE_CODE", "en")
    except Exception:
        code = "en"
    return (code or "en")[:2].lower()


def _t(key: str, **kwargs) -> str:
    """Translate one label, falling back to English."""
    lang = _language()
    template = _STRINGS.get(lang, _STRINGS["en"]).get(key) or _STRINGS["en"][key]
    return template.format(**kwargs)


def _is_plain_archive(path: Path) -> bool:
    """True if the ZIP is not an office/EPUB container (reads only entry names)."""
    try:
        with zipfile.ZipFile(path) as archive:
            # shortcut: reads the central directory, cost grows with entry count
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    for name in names:
        if name in _FOREIGN_MARKERS or name.startswith(("word/", "xl/", "ppt/", "META-INF/")):
            return False
    return True


class ZipArchiveParser:
    name: str = "ZIP Archive Parser"
    version: str = "1.2.0"
    author: str = "paperless-zip-parser contributors"
    url: str = "https://github.com/zunami/paperless-zip-parser"

    def __init__(self, logging_group: object = None) -> None:
        from django.conf import settings

        settings.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        self._tempdir = Path(
            tempfile.mkdtemp(prefix="paperless-zip-", dir=settings.SCRATCH_DIR),
        )
        self._text: str = ""
        self._archive_path: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        shutil.rmtree(self._tempdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Registry interface
    # ------------------------------------------------------------------

    @classmethod
    def supported_mime_types(cls) -> dict[str, str]:
        return _SUPPORTED

    @classmethod
    def score(cls, mime_type: str, filename: str, path: Path | None = None) -> int | None:
        if mime_type not in _SUPPORTED:
            return None
        # Without a path (upload validation) there is nothing to inspect.
        if path is not None and not _is_plain_archive(Path(path)):
            return None
        return 10

    @property
    def can_produce_archive(self) -> bool:
        return False

    @property
    def requires_pdf_rendition(self) -> bool:
        return True

    def configure(self, context) -> None:
        pass

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None:
        source = Path(document_path)
        size = source.stat().st_size
        # Only name and size become searchable text - the archive is never opened.
        self._text = _t("text", name=source.name, size=size)
        try:
            self._archive_path = self._note_pdf(source.name, size)
        except Exception:
            logger.warning(
                "Note page for %s could not be rendered - document stored without "
                "an archive version.",
                source.name,
                exc_info=True,
            )
            self._archive_path = None

    def _note_image(self, filename: str, size: int):
        from django.conf import settings
        from PIL import Image
        from PIL import ImageDraw
        from PIL import ImageFont

        image = Image.new("RGB", (1000, 1414), color="white")  # A4 at 120 dpi
        draw = ImageDraw.Draw(image)
        title_font = ImageFont.truetype(
            font=settings.THUMBNAIL_FONT_NAME,
            size=46,
            layout_engine=ImageFont.Layout.BASIC,
        )
        body_font = ImageFont.truetype(
            font=settings.THUMBNAIL_FONT_NAME,
            size=28,
            layout_engine=ImageFont.Layout.BASIC,
        )
        draw.multiline_text((80, 110), _t("title"), font=title_font, fill="black")
        draw.multiline_text(
            (80, 240),
            f"{_t('file')}\n{filename}\n\n{_t('size', size=size)}\n\n{_t('body')}",
            font=body_font,
            fill="black",
            spacing=14,
        )
        return image

    def _note_pdf(self, filename: str, size: int) -> Path:
        pdf_path = self._tempdir / "note.pdf"
        self._note_image(filename, size).save(pdf_path, "PDF", resolution=100.0)
        return pdf_path

    # ------------------------------------------------------------------
    # Result accessors
    # ------------------------------------------------------------------

    def get_text(self) -> str:
        return self._text

    def get_date(self):
        return None

    def get_archive_path(self) -> Path | None:
        return self._archive_path

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None:
        return 1 if self._archive_path is not None else None

    def extract_metadata(self, document_path: Path, mime_type: str) -> list:
        return []

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        source = Path(document_path)
        image = self._note_image(source.name, source.stat().st_size)
        image.thumbnail((500, 700))
        out_path = self._tempdir / "thumb.webp"
        image.save(out_path, format="WEBP")
        return out_path
