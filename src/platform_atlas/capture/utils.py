"""
Utilities for Capture Engine
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from rich.console import Console

# ATLAS Imports
from platform_atlas.core._version import __version__

_MISSING = object()
_MAX_PIPELINE_DEPTH = 100
_PIPELINE_REQUIRED_FIELDS = {"name", "collection", "pipeline"}
DISALLOWED_STAGES = frozenset({
    "$out",
    "$merge",
    "$replaceWith",
    "$function",
    "$accumulator",
    "$where",
})

console = Console()

logger = logging.getLogger(__name__)

def _match_list_item(item: dict, token: str) -> tuple[bool, Any]:
    """Check if a list item matches a token by name, type, or id"""
    name = item.get("name", "")

    # Direct name match (top-level), or underscore-to-space match for names with spaces
    if name == token or name.replace(" ", "_") == token:
        return True, item

    # Unwrapped data.name match
    data = item.get("data")
    if isinstance(data, dict):
        data_name = data.get("name", "")
        if data_name == token or data_name.replace(" ", "_") == token:
            return True, data

    # Type-based match (adapter envelope pattern)
    if item.get("data", {}).get("properties", {}).get("type") == token:
        return True, item.get("data")

    # ID-based match (adapter envelope pattern)
    if item.get("id") == token:
        return True, item

    return False, None

def get_by_path(data: Any, path: list[str]) -> Any:
    """
    Get dict values by path

    List traversal supports:
    - Numeric index: "results.0.name"
    - Name-based lookup: "results.MyAdapter.properties" (matches item["name"])
    - Type-based lookup: "results.NSO.properties" (matches item["data"]["type"])
    - ID-hased lookup: "results.GatewayManager.version" (matches item["item"])
    """
    cur = data
    for token in path:
        if cur is None:
            return _MISSING

        if isinstance(cur, dict):
            if token not in cur:
                return _MISSING
            cur = cur[token]
            continue

        if isinstance(cur, list):
            # numeric list index
            if token.isdigit():
                idx = int(token)
                if idx < 0 or idx >= len(cur):
                    return _MISSING
                cur = cur[idx]
                continue

            found = _MISSING
            for item in cur:
                if isinstance(item, dict):
                    # Direct name match
                    item_name = item.get("name", "")
                    if item_name == token or item_name.replace(" ", "_") == token:
                        found = item
                        break
                    # Unwrapped data.name match
                    item_data = item.get("data")
                    if isinstance(item_data, dict):
                        data_name = item_data.get("name", "")
                        if data_name == token or data_name.replace(" ", "_") == token:
                            found = item_data
                            break
                    # Type-based match (adapter envelope pattern)
                    if item.get("data", {}).get("properties", {}).get("type") == token:
                        found = item.get("data")
                        break
                    # ID-based match (top-level)
                    if item.get("id") == token:
                        found = item
                        break
            if found is _MISSING:
                return _MISSING
            cur = found
            continue

        return _MISSING
    return cur

def set_by_path(
        out: dict[str, Any],
        path: list[str],
        value: Any,
        *,
        source: Any = None
) -> None:
    """Rebuild a nested output dict using the SAME tokens as get_by_path"""
    cur: Any = out
    src: Any = source

    for i, token in enumerate(path[:-1]):
        next_token = path[i + 1]

        if isinstance(cur, dict):
            # Create container if missing
            if token not in cur or cur[token] is None:
                # Check source data to decide list vs dict
                if isinstance(src, dict) and isinstance(src.get(token), list):
                    cur[token] = []
                else:
                    cur[token] = [] if next_token.isdigit() else {}
            cur = cur[token]

            # Advance source pointer
            src = src.get(token) if isinstance(src, dict) else None
            continue

        if isinstance(cur, list):
            # index step
            if token.isdigit():
                idx = int(token)
                while len(cur) <= idx:
                    cur.append({})
                if cur[idx] is None:
                    cur[idx] = {}
                cur = cur[idx]

                # Advance source through index
                if isinstance(src, list) and idx < len(src):
                    src = src[idx]
                else:
                    src = None
                continue

            # Search by name or data.properties.type, create if not found
            found = None
            for item in cur:
                if isinstance(item, dict):
                    matched, resolved = _match_list_item(item, token)
                    if matched:
                        found = resolved
                        break
            if found is None:
                found = {"name": token}
                cur.append(found)
            cur = found

            # Advance source through name search
            if isinstance(src, list):
                src_found = None
                for item in src:
                    if isinstance(item, dict):
                        matched, resolved = _match_list_item(item, token)
                        if matched:
                            src_found = resolved
                            break
                src = src_found
            else:
                src = None
            continue

        raise TypeError(f"Can't set through {type(cur)} at token {token!r}")

    leaf = path[-1]
    if isinstance(cur, dict):
        cur[leaf] = value
    elif isinstance(cur, list) and leaf.isdigit():
        idx = int(leaf)
        while len(cur) <= idx:
            cur.append(None)
        cur[idx] = value
    else:
        raise TypeError(f"Can't set leaf {leaf!r} on {type(cur)}")

def filter_capture_by_rules(
        data: dict[str, Any],
        rules_doc: dict[str, Any] | list[dict[str, Any]],
        *,
        keep_roots: bool = True
) -> dict[str, Any]:
    """
    Rebuild a nested dict containing ONLY the fields referenced by rules[].name
    while preserving the original structure
    """
    rules = rules_doc["rules"] if isinstance(rules_doc, dict) else rules_doc

    result: dict[str, Any] = {}

    if keep_roots:
        for k, v in data.items():
            if isinstance(v, dict):
                result[k] = {}

    for rule in rules:
        dotted = rule.get("path")
        if not dotted:
            continue

        path = dotted.split(".")
        value = get_by_path(data, path)

        # If primary path failed and alt_path exists, try that
        if value is _MISSING and rule.get("alt_path"):
            alt_dotted = rule["alt_path"]
            path = alt_dotted.split(".")
            value = get_by_path(data, path)

        if value is _MISSING:
            continue

        set_by_path(result, path, value, source=data)
    return result

def normalize_acl_entries(raw_acl_list: list) -> list[list]:
    """Normalize Redis ACL list so each user is in its own sub-list"""
    entries = []
    flat_tokens = []

    for item in raw_acl_list:
        if isinstance(item, list):
            # Flush any accumulated flat tokens as the first user
            if flat_tokens:
                entries.append(flat_tokens)
                flat_tokens = []
            entries.append(item)
        else:
            flat_tokens.append(item)

    # Don't forget the flat tokens if there were no nested lists at all
    if flat_tokens:
        entries.append(flat_tokens)

    return entries

# Pipeline Model & Loader
@dataclass(frozen=True, slots=True)
class Pipeline:
    """Immutable representation of a MongoDB aggregation pipeline definition.

    Loaded from a JSON file that wraps the raw aggregation stages with
    metadata (name, description, target collection, etc.).
    """

    name: str
    collection: str
    pipeline: list[dict[str, Any]]
    desc: str = ""
    pipeline_version: str = ""
    example: str = ""
    source_path: Path | None = field(default=None, repr=False)

    def __str__(self) -> str:
        stage_count = len(self.pipeline)
        return f"{self.name} ({stage_count} stages → {self.collection})"

    def __len__(self) -> int:
        """Number of aggregation stages."""
        return len(self.pipeline)

def _validate_pipeline_security(pipeline: list, source_name: str) -> None:
    """Check pipeline stages for disallowed operations"""
    def _walk(obj: Any, path: str = "", depth: int = 0) -> None:
        if depth > _MAX_PIPELINE_DEPTH:
            raise ValueError(
                f"Pipeline '{source_name}' exceeds max nesting depth ({_MAX_PIPELINE_DEPTH})"
            )
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in DISALLOWED_STAGES:
                    raise ValueError(
                        f"Pipeline '{source_name}' contains disallowed stage "
                        f"'{key}' at {path}.{key}"
                    )
                _walk(value, f"{path}.{key}", depth + 1)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                _walk(item, f"{path}[{idx}]", depth + 1)

    _walk(pipeline)

def load_pipeline(path: str | Path) -> Pipeline:
    """Load and validate a pipeline definition from a JSON file"""
    path = Path(path)

    if not path.is_file():
        raise FileNotFoundError(f"Pipeline file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path.name}: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(
            f"Pipeline file must contain a JSON object, got {type(raw).__name__}"
        )

    missing = _PIPELINE_REQUIRED_FIELDS - raw.keys()
    if missing:
        raise ValueError(
            f"Pipeline '{path.name}' missing required fields: {', '.join(sorted(missing))}"
        )

    pipeline_stages = raw["pipeline"]
    if not isinstance(pipeline_stages, list) or not pipeline_stages:
        raise ValueError(
            f"Pipeline '{raw['name']}' must have a non-empty 'pipeline' list"
        )

    _validate_pipeline_security(pipeline_stages, path.name)

    return Pipeline(
        name=raw["name"],
        collection=raw["collection"],
        pipeline=pipeline_stages,
        desc=raw.get("desc", ""),
        pipeline_version=raw.get("pipeline_version", ""),
        example=raw.get("example", ""),
        source_path=path,
    )


def discover_pipelines(directory: str | Path) -> list[Pipeline]:
    """Load all valid pipeline files from a directory"""
    directory = Path(directory)

    if not directory.is_dir():
        logger.warning("Pipeline directory does not exist: %s", directory)
        return []

    pipelines: list[Pipeline] = []

    for json_file in sorted(directory.glob("*.json")):
        try:
            pipelines.append(load_pipeline(json_file))
            logger.debug("Loaded pipeline: %s", json_file.name)
        except (ValueError, FileNotFoundError) as e:
            logger.warning("Skipping invalid pipeline %s: %s", json_file.name, e)

    return pipelines
