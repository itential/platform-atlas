"""
Shared Helm-values parsing helpers.

Both the Gateway5 collector and the Kubernetes collector parse Helm chart
values files. Real deployments commonly layer a chart's default
``values.yaml`` under an environment-specific override file (the same way
``helm install -f values.yaml -f production-overrides.yaml`` works) — a
setting left at the chart default won't appear in the override file at all,
so reading the override alone silently misses it. This module gives every
Helm-aware capture path the same merge behavior.
"""

from __future__ import annotations

from typing import Any


def merge_helm_values(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge two parsed Helm values files, Helm ``-f`` style.

    Dict values are merged key-by-key; any other value in ``override``
    (including lists) replaces the corresponding value in ``base`` outright —
    this matches Helm's own merge semantics, where later files win and lists
    are replaced rather than concatenated.
    """
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge_helm_values(existing, value)
        else:
            merged[key] = value
    return merged


if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
