"""Adapter File Data — flags duplicate log filenames and undersized log files."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check("adapter_file_data", name="Adapter File Data", category=CheckCategory.CONFIGURATION,
       group=CheckGroup.ADAPTERS)
def check_adapter_file_data(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check for duplicate log filenames and undersized log files."""
    filenames = chk.require(data, "adapters.filedata", "adapter file info")
    expected_size = 1_048_576  # 1 MB

    # Build reverse map: filename -> list of adapters sharing it
    owners: dict[str, list[str]] = {}
    for adapter, cfg in filenames.items():
        owners.setdefault(cfg["filename"], []).append(adapter)
    duplicates = {name for name, owning in owners.items() if len(owning) > 1}

    def _inspect(_name: str, cfg: dict) -> str | None:
        problems = []
        if cfg["filename"] in duplicates:
            shared = [a for a in owners[cfg["filename"]] if a != _name]
            problems.append(f"Duplicate filename '{cfg['filename']}' (shared with: {', '.join(shared)})")
        if cfg["filesize"] < expected_size:
            problems.append(f"Log size {cfg['filesize']:,} bytes is below expected {expected_size:,}")
        return "; ".join(problems) if problems else None

    issues = chk.scan(filenames, _inspect)
    return chk.report(
        issues,
        pass_msg="All adapter log files are unique and sizes are correct",
        warn_msg=f"{len(issues)} adapter(s) with log file issues",
        remediation=(
            "Duplicate log filenames can cause adapters to overwrite each "
            "other's logs. Undersized log files may indicate the log rotation "
            "size is set too low, which can result in lost diagnostic data."
        ),
    )
