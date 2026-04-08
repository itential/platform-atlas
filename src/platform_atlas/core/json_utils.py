"""Unified JSON loading utilities for Platform Atlas"""

import json
from pathlib import Path
from typing import Any

from platform_atlas.core.exceptions import AtlasError

def load_json(
        path: str | Path,
        *,
        error_class: type[AtlasError] = AtlasError,
        required_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Load and validate a JSON file with consistent error handling"""
    p = Path(path)

    if not p.exists():
        raise error_class(f"File not found: {p.name}",
                          details={"path": str(p.absolute())})

    if not p.is_file():
        raise error_class(f"Not a file: {p.name}",
                          details={"path": str(p.absolute())})

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise error_class(
            f"Invalid JSON in {p.name}",
            details={"line": e.lineno, "column": e.colno, "error": e.msg}
        ) from None
    except UnicodeDecodeError:
        raise error_class(f"Encoding error in {p.name}") from None

    if required_keys:
        missing = [k for k in required_keys if k not in data]
        if missing:
            raise error_class(f"Missing keys in {p.name}: {missing}")

    return data

def load_json_safe(path: str | Path) -> tuple[bool, str | None]:
    """Load JSON without raising, returns (success, error_message)"""
    try:
        load_json(path)
        return True, None
    except AtlasError as e:
        return False, e.message
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
