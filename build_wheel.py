#!/usr/bin/env python3
"""Baut das Wheel ohne Build-Tool (reine stdlib) und prueft die Discovery.

Erzeugt dist/paperless_zip_parser-1.0.0-py3-none-any.whl und
dist/zip-parser-site.zip (identischer Inhalt, fuer File-Station-Extraktion).
"""

import base64
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
NAME = "paperless_zip_parser"
VERSION = "1.2.0"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"

METADATA = f"""Metadata-Version: 2.1
Name: paperless-zip-parser
Version: {VERSION}
Summary: Paperless-ngx parser that archives ZIP files as-is (no unpacking)
Requires-Python: >=3.12
"""

WHEEL = """Wheel-Version: 1.0
Generator: handbuilt
Root-Is-Purelib: true
Tag: py3-none-any
"""

ENTRY_POINTS = """[paperless_ngx.parsers]
zip_archive = paperless_zip_parser.parsers:ZipArchiveParser
"""


def files() -> list[tuple[str, bytes]]:
    out = []
    for path in sorted(SRC.joinpath(NAME).rglob("*.py")):
        arc = path.relative_to(SRC).as_posix()
        out.append((arc, path.read_bytes()))
    out.append((f"{DIST_INFO}/METADATA", METADATA.encode()))
    out.append((f"{DIST_INFO}/WHEEL", WHEEL.encode()))
    out.append((f"{DIST_INFO}/entry_points.txt", ENTRY_POINTS.encode()))
    out.append((f"{DIST_INFO}/top_level.txt", (NAME + "\n").encode()))
    return out


def record(entries: list[tuple[str, bytes]]) -> bytes:
    lines = []
    for arc, data in entries:
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        lines.append(f"{arc},sha256={digest.decode()},{len(data)}")
    lines.append(f"{DIST_INFO}/RECORD,,")
    return ("\n".join(lines) + "\n").encode()


def main() -> int:
    DIST.mkdir(exist_ok=True)
    entries = files()
    entries.append((f"{DIST_INFO}/RECORD", b""))
    entries[-1] = (f"{DIST_INFO}/RECORD", record(entries[:-1]))

    whl = DIST / f"{NAME}-{VERSION}-py3-none-any.whl"
    site = DIST / "paperless-zip-parser.zip"
    for target in (whl, site):
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for arc, data in entries:
                zf.writestr(arc, data)
        print(f"geschrieben: {target.name} ({target.stat().st_size} Bytes, {len(entries)} Dateien)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
