#!/usr/bin/env python3
"""Lokaler Selbsttest des Plugins - ohne Paperless, ohne Netz.

Prueft echt:
  1. Entry-Point-Discovery aus dem gebauten Wheel (importlib.metadata)
  2. ep.load() + die 6 Pflicht-Attribute, die die Registry verlangt
  3. MIME-/Score-Verhalten inkl. octet-stream-Fallback und Office-Schutz
  4. parse() -> Text, echtes PDF, Seite 1
  5. get_thumbnail() erzeugt ein echtes WebP

Stub ersetzt nur django.conf (settings).
"""

import importlib.metadata as md
import sys
import tempfile
import types
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WHEEL = next(HERE.glob("dist/paperless_zip_parser-*.whl"))
FAILED = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(label)


def install_stubs(scratch: Path) -> None:
    django = types.ModuleType("django")
    conf = types.ModuleType("django.conf")
    conf.settings = types.SimpleNamespace(
        SCRATCH_DIR=scratch,
        THUMBNAIL_FONT_NAME="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    django.conf = conf
    sys.modules["django"] = django
    sys.modules["django.conf"] = conf


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="ziptest-scratch-"))
    site = Path(tempfile.mkdtemp(prefix="ziptest-site-"))
    with zipfile.ZipFile(WHEEL) as zf:
        zf.extractall(site)
    sys.path.insert(0, str(site))
    install_stubs(scratch)

    eps = [e for e in md.entry_points(group="paperless_ngx.parsers")]
    check("entry point 'zip_archive' wird gefunden", len(eps) == 1, f"{[e.name for e in eps]}")
    if not eps:
        return 1

    cls = eps[0].load()
    check(
        "Registry-Pflichtattribute vorhanden",
        all(hasattr(cls, a) for a in
            ("name", "version", "author", "url", "supported_mime_types", "score")),
    )
    check(
        "supported_mime_types deckt zip + octet-stream",
        cls.supported_mime_types() == {"application/zip": ".zip",
                                       "application/octet-stream": ".zip"},
    )
    for good in ("application/zip", "application/octet-stream"):
        check(f"score({good}) == 10", cls.score(good, "x.zip") == 10)
    for bad in ("application/pdf", "application/x-iso9660-image", "image/jpeg",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "text/plain"):
        check(f"score({bad}) is None", cls.score(bad, "x.bin") is None)

    data = scratch / "archiv-2026.zip"
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("notizen.txt", "hallo " * 500)
        zf.writestr("unterordner/bild.txt", "x")
    payload = data.read_bytes()

    # Office/EPUB sind ebenfalls Zips und duerfen NICHT uebernommen werden
    fake_docx = scratch / "brief.docx"
    with zipfile.ZipFile(fake_docx, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    fake_epub = scratch / "buch.epub"
    with zipfile.ZipFile(fake_epub, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", "<container/>")
    broken = scratch / "kaputt.zip"
    broken.write_bytes(b"PK-kein-echtes-zip")

    for mime in ("application/zip", "application/octet-stream"):
        check(f"{mime} + echtes ZIP -> 10", cls.score(mime, "a.zip", data) == 10)
        check(f"{mime} + docx-Zip -> None", cls.score(mime, "brief.docx", fake_docx) is None)
        check(f"{mime} + epub-Zip -> None", cls.score(mime, "buch.epub", fake_epub) is None)
        check(f"{mime} + kaputtes Zip -> None", cls.score(mime, "k.zip", broken) is None)
    check("score ohne Pfad (Upload-Validierung) -> 10", cls.score("application/octet-stream", "a.zip") == 10)

    with cls() as parser:
        check("can_produce_archive == False", parser.can_produce_archive is False)
        check("requires_pdf_rendition == True", parser.requires_pdf_rendition is True)
        parser.configure(None)
        parser.parse(data, "application/octet-stream")

        text = parser.get_text()
        check("Text enthaelt Dateiname", "archiv-2026.zip" in text, text)
        check("Text enthaelt Groesse", str(len(payload)) in text, text)

        archive = parser.get_archive_path()
        check("Archiv-PDF erzeugt", archive is not None and archive.is_file())
        check("Archiv ist echtes PDF", archive.read_bytes()[:5] == b"%PDF-", str(archive)[-20:])
        check("Original byte-identisch", data.read_bytes() == payload)
        check("get_page_count == 1", parser.get_page_count(data, "application/zip") == 1)

        thumb = parser.get_thumbnail(data, "application/zip")
        check("Thumbnail ist WebP", thumb.is_file() and thumb.read_bytes()[8:12] == b"WEBP")
        check("extract_metadata leer", parser.extract_metadata(data, "application/zip") == [])
        check("get_date None", parser.get_date() is None)

    # Renderfehler darf das Dokument nicht verhindern
    cls_broken = cls
    original = cls_broken._note_image

    def boom(self, filename, size):
        raise RuntimeError("kein Font")

    cls_broken._note_image = boom
    try:
        with cls_broken() as parser:
            parser.parse(data, "application/zip")
            check("Renderfehler: kein Absturz, kein Archiv", parser.get_archive_path() is None)
            check("Renderfehler: Text trotzdem gesetzt", "archiv-2026.zip" in parser.get_text())
    finally:
        cls_broken._note_image = original

    print()
    if FAILED:
        print(f"{len(FAILED)} Test(s) fehlgeschlagen: {FAILED}")
        return 1
    print("Alle Tests bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
