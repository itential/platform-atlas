"""Auto-discovery for AVC (Additional Validation Check) plugin modules.

Every check module in this package registers itself via the @check
decorator (defined in extended_validation.py) purely as an import side
effect — importing each module here is what makes that happen. Dropping a
new <check_id>.py file into this directory is enough to add a check; there
is no separate list of modules to keep in sync.

Modules are imported in sorted-by-name order so registration order (and
anything downstream that iterates the registry) is deterministic across
platforms, since raw directory-listing order isn't guaranteed to match
between filesystems.
"""
from __future__ import annotations

import importlib
import pkgutil

for _module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    importlib.import_module(f"{__name__}.{_module.name}")
