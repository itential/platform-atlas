"""ATLAS // Validation Results — replaces the pandas DataFrame + .attrs pairing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platform_atlas.core.json_utils import load_json
from platform_atlas.core.utils import atomic_write_json

SCHEMA_VERSION = 1


@dataclass
class ValidationResults:
    """Rule evaluation rows plus run metadata.

    Used for both a single session's validation output (rows = rule results)
    and a diff between two sessions (rows = diff rows) — same shape, same
    persistence/serialization needs either way.
    """
    rows: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def empty(self) -> bool:
        return not self.rows


def save_validation_results(path: Path, results: ValidationResults) -> None:
    """Atomically write validation results as JSON."""
    atomic_write_json(path, {
        "schema_version": SCHEMA_VERSION,
        "metadata": results.metadata,
        "results": results.rows,
    })


def load_validation_results(path: Path) -> ValidationResults:
    """Read a previously written validation results JSON file."""
    payload = load_json(path)
    return ValidationResults(
        rows=payload.get("results", []),
        metadata=payload.get("metadata", {}),
    )
