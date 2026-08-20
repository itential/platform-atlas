"""IAG4 Default Paths — flags missing default Ansible module/collection/role paths."""
from __future__ import annotations

from typing import Any

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)

_IAG4_DEFAULT_PATHS: dict[str, list[str]] = {
    "module_path": [
        "/usr/local/lib/python3.9/site-packages/ansible/modules/network",
        "/usr/local/lib/python3.9/site-packages/ansible_collections",
        "/home/itential/.local/lib/python3.9/site-packages/ansible/modules/network",
        "/home/itential/.local/lib/python3.9/site-packages/ansible_collections",
        "/home/itential/automation-gateway/lib/python3.9/site-packages/ansible/modules/network",
        "/home/itential/automation-gateway/lib/python3.9/site-packages/ansible_collections",
        "/usr/share/automation-gateway/ansible/modules",
    ],
    "collection_path": [
        "/opt/automation-gateway/.ansible/collections",
        "/usr/share/automation-gateway/ansible/collections",
    ],
    "role_path": [
        "/opt/automation-gateway/.ansible/roles",
        "/usr/share/automation-gateway/ansible/roles",
    ],
}


@check(
    "iag4_default_paths",
    name="IG4 Default Paths",
    category=CheckCategory.CONFIGURATION,
    group=CheckGroup.GATEWAY,
    requires=("gateway4.configured_paths",),
)
def check_iag4_default_paths(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check for default paths in Automation Gateway 4"""
    configured = chk.require(data, "gateway4.configured_paths", "Gateway4 path config")

    missing: dict[str, list[str]] = {}
    present: dict[str, list[str]] = {}

    for category, defaults in _IAG4_DEFAULT_PATHS.items():
        actual = configured.get(category, [])
        found = [p for p in defaults if p in actual]
        not_found = [p for p in defaults if p not in actual]

        if found:
            present[category] = found
        if not_found:
            missing[category] = not_found

    details: dict[str, Any] = {}
    if present:
        details["present"] = present
    if missing:
        details["missing"] = missing

    if not missing:
        return chk.passed(
            f"All default paths present across {len(_IAG4_DEFAULT_PATHS)} categories",
            details=details,
        )

    total_missing = sum(len(v) for v in missing.values())
    affected = ", ".join(missing.keys())

    return chk.warn(
        f"{total_missing} default path(s) missing in: {affected}",
        details=details,
        remediation=(
            "Missing default paths in the Gateway4 SQLite config may prevent "
            "Ansible from locating modules, collections, or roles. Review the "
            "Configuration Settings and ensure all expected paths are present."
        ),
    )
