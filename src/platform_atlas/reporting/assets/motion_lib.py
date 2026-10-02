"""Vendored Motion (motion.dev) animation library for report.html.

get_motion_js() returns the minified UMD build (exposes a ``window.Motion``
global with ``animate``/``press``/``hover``/``inView``/``scroll``/``stagger``)
so the report can drive real spring-physics animation with zero external
requests — same "no external URLs, embed everything" rule as the fonts and
the Itential logo. MIT licensed; see vendor/motion.min.js for the attribution
header. Read from disk rather than embedded as a Python string literal so the
minified source (arbitrary quotes/backslashes) never has to survive being
quoted inside a ``.py`` file.
"""

from functools import lru_cache
from pathlib import Path

_VENDOR_PATH = Path(__file__).parent / "vendor" / "motion.min.js"


@lru_cache(maxsize=1)
def get_motion_js() -> str:
    """Return the vendored Motion UMD build as a string, ready to inline in a <script> tag."""
    return _VENDOR_PATH.read_text(encoding="utf-8")
