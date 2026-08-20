"""Adapter Version Check — flags adapters running outdated versions."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from packaging.version import Version

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    ExtendedStatus,
    check,
)
from platform_atlas.validation.utils import get_latest_version

logger = logging.getLogger(__name__)


@check("adapter_versions", name="Adapter Version Check", category=CheckCategory.VERSION,
       group=CheckGroup.ADAPTERS)
def check_adapter_versions(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check if installed adapter versions are up-to-date against Gitlab."""
    try:
        from platform_atlas.core.config import is_network_restricted
        if is_network_restricted():
            return chk.result(
                "SKIP",
                "Adapter version checks skipped — network policy is set to 'disallow'",
                remediation=(
                    "To run adapter version checks, set the network policy to 'allow' "
                    "via platform-atlas config edit."
                ),
            )
    except Exception:
        pass

    adapter_versions = chk.require(data, "adapters.versions", "adapter version")
    outdated, up_to_date, failed = [], [], []

    def _check_one(name: str, installed_str: str):
        installed = Version(installed_str)
        latest = Version(get_latest_version(name))
        return name, installed, latest

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(_check_one, name, ver): name
            for name, ver in adapter_versions.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                _, installed, latest = future.result()
                if installed < latest:
                    outdated.append({"adapter": name, "installed": str(installed), "latest": str(latest)})
                else:
                    up_to_date.append(name)
            except Exception as e:
                logger.debug("Could not check %s: %s", name, e)
                failed.append(name)

    details = {"outdated": outdated, "up_to_date": up_to_date, "failed": failed}

    if outdated:
        total = len(adapter_versions)
        status: ExtendedStatus = "WARN" if len(outdated) < total / 2 else "FAIL"
        return chk.result(status, f"{len(outdated)} adapter(s) outdated",
                           details, "Update outdated adapters to the latest versions")

    if failed:
        return chk.info(f"Unable to verify {len(failed)} adapter(s)", details)

    return chk.passed("All adapters are up-to-date", details)
