"""ATLAS // Legacy Validation File Cleanup

Pre-3.0 sessions may still have a 02_validation.parquet file on disk — 3.0+
never reads it (validation moved to 02_validation.json). This module only
scans for them; core/handlers/config.py surfaces the finding in `config
doctor`, and core/handlers/session.py's prune handler does the actual delete.
"""

from pathlib import Path

from platform_atlas.core.session_manager import Session, get_session_manager


def scan_legacy_validation_files() -> list[tuple[Session, Path, int]]:
    """Find orphaned pre-3.0 parquet validation files across all sessions.

    Checks the literal ``02_validation.parquet`` filename directly, not the
    ``validation_file`` property — that property always points at the
    current ``.json`` name, so a parquet file next to it is necessarily a
    leftover from before the upgrade.
    """
    hits: list[tuple[Session, Path, int]] = []
    for session in get_session_manager().list():
        legacy_path = session.directory / "02_validation.parquet"
        if legacy_path.is_file():
            hits.append((session, legacy_path, legacy_path.stat().st_size))
    return hits


def split_by_report_status(
    hits: list[tuple[Session, Path, int]],
) -> tuple[list[tuple[Session, Path, int]], list[tuple[Session, Path, int]]]:
    """Split scan results into (safe-to-delete, needs-revalidate-first).

    "Safe" means the session already has a generated report — its
    validation data is baked into that report, so the cache is redundant.
    """
    safe = [h for h in hits if h[0].metadata.report_completed]
    needs_revalidate = [h for h in hits if not h[0].metadata.report_completed]
    return safe, needs_revalidate
