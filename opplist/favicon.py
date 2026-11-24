"""I1: OppList's tab icon, a pink pushpin (web/static/favicon*).

The app links the files; the two pages it writes to be opened from disk
(the month report and the dashboard) cannot reach the static folder, so
they carry the 32 px PNG as a data: URI.
"""

from __future__ import annotations

import base64
import functools
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "web" / "static"
PNG_32 = "favicon-32.png"


@functools.lru_cache(maxsize=1)
def data_uri() -> str:
    """'data:image/png;base64,...' - empty when the file is missing, so a
    lost icon never stops a report from being written."""
    try:
        raw = (STATIC / PNG_32).read_bytes()
    except OSError:
        return ""
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
