"""
Platform Atlas // Shared browser-guide companion assets

All three browser guide pages — env-setup.html, tier-upgrade.html, and
architecture-form.html — reference the same companion files from a sibling
``assets/`` directory:

  - ``atlas-guide.css``  — shared design system (the single styling source)
  - ``anime.min.js``     — vendored anime.js library
  - ``atlas-motion.js``  — shared motion layer
  - ``atlas-a11y.js``    — shared accessibility layer (state/ARIA mirroring).
    Kept separate from the motion layer on purpose: that file returns early
    under ``prefers-reduced-motion`` and none of this may be skipped.

Each guide handler syncs its HTML into ``~/.atlas/guides/`` and then calls
:func:`sync_guide_assets` to place these files under ``~/.atlas/guides/assets/``
so the ``file://`` page can load them. A missing asset only degrades the page
(no styling / no animation) and is never a hard failure.
"""
from __future__ import annotations

from pathlib import Path

# Order is cosmetic; all three are synced independently.
GUIDE_ASSETS = ("atlas-guide.css", "anime.min.js", "atlas-motion.js", "atlas-a11y.js")


def read_guide_bytes(*relparts: str) -> bytes | None:
    """Read a packaged guide file, falling back to the source tree in dev installs."""
    try:
        from importlib.resources import files as pkg_files
        node = pkg_files("platform_atlas.guides")
        for part in relparts:
            node = node.joinpath(part)
        return node.read_bytes()
    except Exception:  # pylint: disable=broad-except
        pass

    fallback = Path(__file__).parent.parent / "guides"
    for part in relparts:
        fallback = fallback / part
    if fallback.exists():
        return fallback.read_bytes()
    return None


def sync_guide_assets(guides_dir: Path) -> None:
    """Copy the shared CSS + motion assets into ``<guides_dir>/assets/``.

    Best-effort: a file that can't be read or written is skipped so a partial
    asset set never blocks a guide page from opening.
    """
    assets_dir = guides_dir / "assets"
    for name in GUIDE_ASSETS:
        data = read_guide_bytes("assets", name)
        if data is None:
            continue
        try:
            assets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            (assets_dir / name).write_bytes(data)
        except OSError:
            pass
