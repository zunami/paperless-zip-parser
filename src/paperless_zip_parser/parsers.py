"""Paperless-ngx 3.x parser plugin that stores a ZIP archive as one document.

The archive is kept byte-identical and is never unpacked, scanned or modified.
Tags, correspondent, document type, custom fields and workflows work as usual.

Design notes (checked against Paperless-ngx 3.4.2):
- Plain class, no inheritance from documents.parsers.DocumentParser: its __init__
  calls get_settings() and raises NotImplementedError. The built-in text parser
  does not inherit either.
- Heavy imports (django.conf, PIL) live inside the methods. Discovery imports the
  class at startup, so a module-level import error makes the whole plugin vanish
  with only a warning in the log. Side benefit: the module is testable without
  Paperless installed.
- Two MIME types, not one: libmagic in the official image reports real ZIPs as
  application/octet-stream (only an empty archive matches the PK\x05\x06 rule).
  A plugin that registers application/zip alone is never called there.
- requires_pdf_rendition = False: no PDF wrapper, the download returns the original
  ZIP. The note page is rendered with Pillow, not Gotenberg - Pillow needs no
  network service and ships with the image.
- can_produce_archive = False: no PDF/A archive version, the original stays original.
- Registering every type from /etc/mime.types (score=0) would also claim .exe/.iso/.dwg.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile

from . import __version__ as _pkg_version
from pathlib import Path
from typing import Self

logger = logging.getLogger("paperless.parsing.zip_archive")

MIME = "application/zip"
FALLBACK_MIME = "application/octet-stream"
# Some libmagic builds (e.g. the one in the official image) report real ZIPs as
# application/octet-stream - only empty archives match the PK\x05\x06 rule. The
# content is verified before such a file is accepted.
# PAPERLESS_ZIP_PARSER_STRICT_ZIP=1 disables the fallback and accepts ZIP only.
_STRICT = os.environ.get("PAPERLESS_ZIP_PARSER_STRICT_ZIP", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
_SUPPORTED: dict[str, str] = (
    {MIME: ".zip"} if _STRICT else {MIME: ".zip", FALLBACK_MIME: ".zip"}
)

# ZIP is also the container format of OOXML (docx/xlsx/pptx), ODF and EPUB. Depending on the
# libmagic version these are NOT reported as their own type (file --mime-type says application/zip).
# Without this guard the parser claims every office file with score=10 and bypasses Tika.
_OOXML_MARKER = "[Content_Types].xml"
# A part path is more specific than a bare directory name: a plain archive may well contain a
# folder called "word/", which must not make it look like a DOCX.
_OOXML_PARTS = ("word/document.xml", "xl/workbook.xml", "ppt/presentation.xml")
_FOREIGN_SUFFIXES = frozenset(
    {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm",
     ".odt", ".ods", ".odp", ".odg", ".epub"}
)
# ODF and EPUB both carry a stored "mimetype" entry; a plain ZIP may contain a file of that
# name without being an office container, so only the entry content decides.
_FOREIGN_MIME_PREFIXES = (b"application/vnd.oasis.opendocument.", b"application/epub+zip")
# Cheap guard rails against decompression bombs: only the first bytes of "mimetype" are read and
# the central directory is refused when it is absurdly large. Both are configurable.
_MIMETYPE_PROBE_BYTES = 64
_MAX_ENTRIES = int(os.environ.get("PAPERLESS_ZIP_PARSER_MAX_ENTRIES", "200000"))
_MAX_UNCOMPRESSED = int(
    os.environ.get("PAPERLESS_ZIP_PARSER_MAX_UNCOMPRESSED", str(64 * 1024**3))
)

# Do not rely on get_language() alone: the Celery worker has no request context and returns
# 'en', which would put English search text on a German instance.
# Order: own env var -> Django language -> English.
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
    """Language for the note text.

    Order matters: an explicit override wins, then the instance configuration (LANGUAGE_CODE),
    then the worker's active language, then English. A background task often runs with the
    worker default ("en"), which must not hide a German instance setting.
    """
    forced = os.environ.get("PAPERLESS_ZIP_PARSER_LANGUAGE", "").strip()
    if forced:
        return forced[:2].lower()
    candidates: list[str] = []
    try:
        from django.conf import settings

        candidates.append((getattr(settings, "LANGUAGE_CODE", "") or "")[:2].lower())
    except Exception:  # noqa: BLE001 - outside Django (CLI, tests) English is fine
        pass
    try:
        from django.utils.translation import get_language

        candidates.append((get_language() or "")[:2].lower())
    except Exception:  # noqa: BLE001 - no active translation outside Django
        pass
    for code in candidates:
        if code in _STRINGS:
            return code
    return "en"


def _t(key: str, **kwargs) -> str:
    lang = _language()
    template = _STRINGS.get(lang, _STRINGS["en"]).get(key) or _STRINGS["en"][key]
    return template.format(**kwargs)


def _is_plain_archive(path: Path, filename: str = "") -> bool:
    """True if the file is a plain ZIP, not an office/EPUB container.

    Reads the central directory and at most the first bytes of a "mimetype" entry - the payload
    is never decompressed further, so a crafted "mimetype" entry can not inflate memory here.
    """
    suffix = Path(filename or path).suffix.lower()
    if suffix in _FOREIGN_SUFFIXES:
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ENTRIES:
                logger.warning(
                    "Archive has %d entries (limit %d) - not treated as a plain ZIP.",
                    len(infos),
                    _MAX_ENTRIES,
                )
                return False
            unpacked = sum(info.file_size for info in infos)
            if unpacked > _MAX_UNCOMPRESSED:
                logger.warning(
                    "Archive declares %d unpacked bytes (limit %d) - not treated as a plain ZIP.",
                    unpacked,
                    _MAX_UNCOMPRESSED,
                )
                return False
            names = {info.filename for info in infos}
            if _OOXML_MARKER in names or any(part in names for part in _OOXML_PARTS):
                return False
            if "mimetype" in names:
                try:
                    with archive.open("mimetype") as handle:
                        head = handle.read(_MIMETYPE_PROBE_BYTES)
                except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
                    head = b""
                if head.startswith(_FOREIGN_MIME_PREFIXES):
                    return False
    except (zipfile.BadZipFile, OSError):
        return False
    return True


class ZipArchiveParser:
    name: str = "ZIP Archive Parser"
    # One version source: the package itself. A hard-coded copy here would silently drift.
    version: str = _pkg_version
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
    # Registry-Schnittstelle
    # ------------------------------------------------------------------

    @classmethod
    def supported_mime_types(cls) -> dict[str, str]:
        return _SUPPORTED

    @classmethod
    def score(cls, mime_type: str, filename: str, path: Path | None = None) -> int | None:
        expected_suffix = _SUPPORTED.get(mime_type)
        if expected_suffix is None:
            return None
        # The generic fallback must not claim arbitrary binaries: an .iso or .exe reported as
        # application/octet-stream is not a ZIP, no matter what the magic database says.
        given_suffix = Path(filename).suffix.lower()
        if given_suffix and given_suffix != expected_suffix:
            return None
        # Upload validation (serialisers.py) calls score() WITHOUT a path - it has to
        # accept there, otherwise the upload is rejected before the consumer runs.
        if path is not None and not _is_plain_archive(Path(path), filename):
            return None
        return 10

    @property
    def can_produce_archive(self) -> bool:
        return False

    @property
    def requires_pdf_rendition(self) -> bool:
        # False = Paperless verlangt keine PDF-Vorschau und legt deshalb KEINE Archiv-Version an.
        # Folge: Der Download-Knopf liefert das Original-ZIP (views.py: use_archive = has_archive_version).
        return False

    def configure(self, context) -> None:
        pass

    # ------------------------------------------------------------------
    # Parsen
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
        # Only name and size go into the full text: the archive stays searchable without
        # extracting anything. The original is never opened.
        self._text = _t("text", name=source.name, size=size)
        # 1.3.x behaviour on purpose: no archive PDF at all. Paperless only keeps the
        # original ZIP, so a plain download returns exactly the uploaded bytes. The
        # produce_archive flag is accepted for API compatibility and deliberately ignored.
        self._archive_path = None

    def _font(self, size: int):
        """Thumbnail font with a fallback: a missing or unreadable font must not fail."""
        from django.conf import settings
        from PIL import ImageFont

        try:
            return ImageFont.truetype(
                font=settings.THUMBNAIL_FONT_NAME,
                size=size,
                layout_engine=ImageFont.Layout.BASIC,
            )
        except Exception:  # noqa: BLE001 - any font problem falls back to Pillow's default
            logger.warning("Thumbnail font %r unavailable, using Pillow default.", size)
            try:
                return ImageFont.load_default(size=size)
            except TypeError:  # Pillow < 10.1 has no size argument
                return ImageFont.load_default()

    def _note_image(self, filename: str, size: int):
        """One draw routine for the thumbnail (kept for both text and layout paths)."""
        from PIL import Image
        from PIL import ImageDraw

        img = Image.new("RGB", (1000, 1414), color="white")  # A4 at 120 dpi
        draw = ImageDraw.Draw(img)
        draw.multiline_text((80, 110), _t("title"), font=self._font(46), fill="black")
        draw.multiline_text(
            (80, 240),
            f"{_t('file')}\n{filename}\n\n{_t('size', size=size)}\n\n{_t('body')}",
            font=self._font(28),
            fill="black",
            spacing=14,
        )
        return img

    # ------------------------------------------------------------------
    # Ergebnis-Zugriff
    # ------------------------------------------------------------------

    def get_text(self) -> str:
        return self._text

    def get_date(self):
        return None

    def get_archive_path(self) -> Path | None:
        # 1.3.x: never a PDF archive, so a download always returns the original ZIP.
        return None

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None:
        # A ZIP has no page count; a made-up 1 would show a bogus page number in the UI.
        return None

    def extract_metadata(self, document_path: Path, mime_type: str) -> list:
        return []

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        """Thumbnail of the note layout; falls back to a minimal image instead of raising.

        Two fallback levels: a plain white page if the layout fails, and - because a filesystem
        problem is not a layout problem - a clear RuntimeError naming the target path if even
        that write fails. Silently returning a path to a missing file would hide the cause.
        """
        source = Path(document_path)
        out_path = self._tempdir / "thumb.webp"
        try:
            image = self._note_image(source.name, source.stat().st_size)
            image.thumbnail((500, 700))
            image.save(out_path, format="WEBP")
            return out_path
        except Exception:  # noqa: BLE001 - a broken thumbnail must never abort the import
            logger.warning("Thumbnail rendering failed, writing placeholder.", exc_info=True)
        try:
            from PIL import Image

            Image.new("RGB", (300, 424), color="white").save(out_path, format="WEBP")
            return out_path
        except Exception as exc:  # noqa: BLE001 - report the filesystem problem, do not hide it
            logger.error("Thumbnail placeholder could not be written to %s: %s", out_path, exc)
            raise RuntimeError(f"thumbnail could not be written to {out_path}: {exc}") from exc
