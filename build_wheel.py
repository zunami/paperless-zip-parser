#!/usr/bin/env python3
"""Builds the wheel without a build tool (stdlib only) and checks discovery.

Writes dist/paperless_zip_parser-<version>-py3-none-any.whl and
dist/paperless-zip-parser.zip (identical content, unpacks straight into a data folder).
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
import re

# The version has ONE source: src/paperless_zip_parser/__init__.py (pyproject.toml carries it
# for pip; both are cross-checked here so drift fails loudly instead of passing silently).
_pkg = (SRC / "paperless_zip_parser" / "__init__.py").read_text(encoding="utf-8")
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', _pkg).group(1)
_pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
if f'version = "{VERSION}"' not in _pyproject:
    sys.exit(f"Version drift: __init__.py={VERSION}, pyproject.toml does not match")
DIST_INFO = f"{NAME}-{VERSION}.dist-info"

METADATA = f"""Metadata-Version: 2.4
Name: paperless-zip-parser
Version: {VERSION}
Summary: Paperless-ngx parser that archives ZIP files as-is (no unpacking)
Project-URL: Homepage, https://github.com/zunami/paperless-zip-parser
Project-URL: Issues, https://github.com/zunami/paperless-zip-parser/issues
Project-URL: Source, https://github.com/zunami/paperless-zip-parser
License-Expression: MIT
License-File: LICENSE
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
    license_text = (ROOT / "LICENSE").read_bytes()
    out.append((f"{DIST_INFO}/METADATA", METADATA.encode()))
    out.append((f"{DIST_INFO}/WHEEL", WHEEL.encode()))
    out.append((f"{DIST_INFO}/entry_points.txt", ENTRY_POINTS.encode()))
    out.append((f"{DIST_INFO}/top_level.txt", (NAME + "\n").encode()))
    # License file belongs in the distribution, otherwise the wheel is not self-contained.
    out.append((f"{DIST_INFO}/licenses/LICENSE", license_text))
    out.append(("LICENSE", license_text))
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
        print(f"written: {target.name} ({target.stat().st_size} bytes, {len(entries)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
