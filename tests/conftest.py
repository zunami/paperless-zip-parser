"""Shared test bootstrap: repo on sys.path, django.conf stubbed, no network.

The plugin imports Django only inside methods, but a stub keeps the tests runnable on a
plain CPython without Paperless installed.
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(SRC), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

SCRATCH = Path(tempfile.mkdtemp(prefix="ziptest-"))
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
)

if "django.conf" not in sys.modules:
    django = types.ModuleType("django")
    conf = types.ModuleType("django.conf")
    conf.settings = types.SimpleNamespace(
        SCRATCH_DIR=SCRATCH,
        THUMBNAIL_FONT_NAME=next(
            (f for f in _FONT_CANDIDATES if Path(f).is_file()), "/nonexistent/font.ttf"
        ),
    )
    django.conf = conf
    sys.modules["django"] = django
    sys.modules["django.conf"] = conf
