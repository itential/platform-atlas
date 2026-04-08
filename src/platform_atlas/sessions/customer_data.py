"""
Customer Data Management for Multi-Tenant Mode
"""

from __future__ import annotations

from pathlib import Path
import json
import shutil
from dataclasses import dataclass
from datetime import datetime

from platform_atlas.core.utils import secure_mkdir
from platform_atlas.core.paths import ATLAS_CUSTOMER_DATA

__all__ = [
    "CaptureSession",
    "normalize_organization_name",
    "import_capture",
    "list_companies",
    "list_sessions",
    "get_capture_path",
    "get_all_sessions",
]

@dataclass(frozen=True, slots=True)
class CaptureSession:
    """Simple metadata for a capture file"""
    organization_name: str
    filename: str
    filepath: Path
    captured_at: str  # from metadata in JSON
    ruleset_id: str
    hostname: str
    has_validation: bool = False
    has_report: bool = False

def normalize_organization_name(name: str) -> str:
    """Convert organization name to directory-safe format"""
    return name.strip().lower().replace(" ", "-").replace("_", "-")

def import_capture(
    capture_file: Path,
    organization_name: str | None = None,
    session_name: str | None = None
) -> Path:
    """Import a customer capture file into organized storage"""
    # Load capture data to extract metadata
    with open(capture_file, encoding="utf-8") as f:
        data = json.load(f)

    metadata = data.get("_atlas", {}).get("metadata", {})

    # Auto-extract organization name from metadata if not provided
    if organization_name is None:
        organization_name = metadata.get("organization_name")
        if not organization_name:
            raise ValueError(
                "Organization name not found in capture metadata. "
                "Please provide --organization argument or ensure config.json has 'organization_name' set."
            )

    # Create organization directory
    organization_dir = ATLAS_CUSTOMER_DATA / normalize_organization_name(organization_name)
    secure_mkdir(organization_dir)

    # Determine session name from metadata if not provided
    if session_name is None:
        # Try to extract quarter from captured_at timestamp
        captured_at = metadata.get("captured_at", "")
        if captured_at:
            # Parse "2026-01-15 12:34:56 UTC" -> "2026-q1"
            year = captured_at[:4]
            month = int(captured_at[5:7])
            quarter = (month - 1) // 3 + 1
            session_name = f"{year}-q{quarter}"
        else:
            # Fallback to timestamp
            session_name = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Copy file to customer directory
    dest_file = organization_dir / f"{session_name}-capture.json"
    shutil.copy2(capture_file, dest_file)

    return dest_file


def list_companies() -> list[str]:
    """List all companies with capture data"""
    if not ATLAS_CUSTOMER_DATA.exists():
        return []

    return sorted([
        d.name for d in ATLAS_CUSTOMER_DATA.iterdir()
        if d.is_dir()
    ])

def list_sessions(organization_name: str) -> list[CaptureSession]:
    """List all capture sessions for an organization"""
    organization_dir = ATLAS_CUSTOMER_DATA / normalize_organization_name(organization_name)

    if not organization_dir.exists():
        return []

    sessions = []
    for capture_file in sorted(organization_dir.glob("*-capture.json")):
        # Extract session name from filename
        session_name = capture_file.stem.replace("-capture", "")

        # Load metadata
        with open(capture_file, encoding="utf-8") as f:
            data = json.load(f)

        metadata = data.get("_atlas", {}).get("metadata", {})

        # Check for associated validation and report files
        validation_file = organization_dir / f"{session_name}-validation.parquet"
        report_file = organization_dir / f"{session_name}-report.html"

        sessions.append(CaptureSession(
            organization_name=organization_name,
            filename=capture_file.name,
            filepath=capture_file,
            captured_at=metadata.get("captured_at", "Unknown"),
            ruleset_id=metadata.get("ruleset_id", "Unknown"),
            hostname=data.get("_atlas", {}).get("system_facts", {}).get("hostname", "Unknown"),
            has_validation=validation_file.exists(),
            has_report=report_file.exists()
        ))

    return sessions


def get_capture_path(organization_name: str, session_name: str) -> Path | None:
    """Get the path to a specific capture file"""
    organization_dir = ATLAS_CUSTOMER_DATA / normalize_organization_name(organization_name)
    capture_file = organization_dir / f"{session_name}-capture.json"

    return capture_file if capture_file.exists() else None

def get_all_sessions() -> list[CaptureSession]:
    """Get all capture sessions across all organizations"""
    all_sessions = []
    for organization in list_companies():
        all_sessions.extend(list_sessions(organization))
    return all_sessions
