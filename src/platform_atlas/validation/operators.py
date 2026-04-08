"""
ATLAS // Validation Engine Operators
"""

import re
import operator
from typing import Callable
from packaging.version import Version, InvalidVersion

def coerce_bool(value) -> bool:
    """Function to handle boolean value checks"""
    # Already a real bool
    if isinstance(value, bool):
        return value

    # Common numeric forms
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)

    # String forms (case-insensitive)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "t", "yes", "y", "on", "1"}:
            return True
        if s in {"false", "f", "no", "n", "off", "0"}:
            return False

    raise ValueError(f"Cannot coerce to bool: {value!r} ({type(value).__name__})")

def coerce_int(value) -> int:
    """Function to handle int value checks"""
    # Already an int (but not bool)
    if isinstance(value, int) and not isinstance(value, bool):
        return value

    # Float that is actually an int
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"Float is not an integer: {value}")

    # String forms
    if isinstance(value, str):
        s = value.strip()

        # Reject empty strings
        if not s:
            raise ValueError("Empty string cannot be coerced to int")

        # Allow leading +/-
        if s.lstrip("+-").isdigit():
            return int(s)
        raise ValueError(f"String is not an integer: {value!r}")
    raise ValueError(f"Cannot coerce to int: {value!r} ({type(value).__name__})")

def extract_int(value: str | int) -> int:
    """Extract integer from strings like '512mb', '1024', '2g'."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.match(r'^(\d+)', value.strip())
        if match:
            return int(match.group(1))
    raise ValueError(f"Cannot extract int from: {value!r}")

def parse_version(version_str: str) -> Version:
    """Parse version string"""
    try:
        return Version(version_str)
    except InvalidVersion:
        match = re.search(r"[\d.]+", str(version_str))
        if match:
            return Version(match.group())
        raise ValueError(f"Cannot parse version: {version_str}")

def _make_comparison_ops(coercer):
    """Generate standard comparison operators for a type"""
    return {
        "eq": lambda a, e: coercer(a) == coercer(e),
        "neq": lambda a, e: coercer(a) != coercer(e),
        "gt": lambda a, e: coercer(a) > coercer(e),
        "gte": lambda a, e: coercer(a) >= coercer(e),
        "lt": lambda a, e: coercer(a) < coercer(e),
        "lte": lambda a, e: coercer(a) <= coercer(e),
        "in_range": lambda a, e: coercer(e[0]) <= coercer(a) <= coercer(e[1]),
    }

def _normalize_list(val):
    """Coerce a single value or list into a comparable list of strings"""
    if not isinstance(val, list):
        val = [val]
    return [str(x) for x in val]

def _identity(x):
    return x

OPERATORS: dict[tuple[str, str], Callable] = {}

for type_name, coercer in [("int", coerce_int), ("float", _identity), ("semver", parse_version)]:
    for op_name, op_func in _make_comparison_ops(coercer).items():
        OPERATORS[(type_name, op_name)] = op_func

# Add string operators
OPERATORS.update({
    ("string", "eq"): operator.eq,
    ("string", "neq"): operator.ne,
    ("string", "in"): lambda a, e: a in e,
    ("string", "not_in"): lambda a, e: a not in e,
    ("string", "exists"): lambda a, e: a is not None and a != "",
    ("string", "contains"): lambda a, e: e in a,
    ("string", "not_contains"): lambda a, e: e not in a,
    ("string", "safe_chars"): lambda a, e: bool(re.match(r'^[a-zA-Z0-9._-]+$', a)),
    ("string", "empty"): lambda a, e: a is None or a == "",
})

# Add string_list operators
OPERATORS.update({
    ("string_list", "eq"): lambda a, e: a == e,
    ("string_list", "contains"): lambda a, e: e in a,
    ("string_list", "contains_all"): lambda a, e: all(x in a for x in e),
    ("string_list", "contains_any"): lambda a, e: any(x in a for x in e),
    ("string_list", "subset_of"): lambda a, e: all(x in e for x in a),
    ("string_list", "none_in"): lambda a, e: not any(x in a for x in e),
    ("string_list", "empty"): lambda a, e: isinstance(a, list) and len(a) == 0,
})

# Add in mixed dtype operator functions
OPERATORS.update({
    ("mixed_list", "contains_all"): lambda a, e: all(
        x in _normalize_list(a) for x in _normalize_list(e)
    ),
    ("mixed_list", "contains_any"): lambda a, e: any(
        x in _normalize_list(a) for x in _normalize_list(e)
    ),
    ("mixed_list", "eq"): lambda a, e: sorted(_normalize_list(a)) == sorted(_normalize_list(e)),
})

# Parsed int (extracts number from strings like "512mb")
OPERATORS.update({
    ("parsed_int", "eq"): lambda a, e: extract_int(a) == coerce_int(e),
    ("parsed_int", "neq"): lambda a, e: extract_int(a) != coerce_int(e),
    ("parsed_int", "gt"): lambda a, e: extract_int(a) > coerce_int(e),
    ("parsed_int", "gte"): lambda a, e: extract_int(a) >= coerce_int(e),
    ("parsed_int", "lt"): lambda a, e: extract_int(a) < coerce_int(e),
    ("parsed_int", "lte"): lambda a, e: extract_int(a) <= coerce_int(e),
    ("parsed_int", "in_range"): lambda a, e: coerce_int(e[0]) <= extract_int(a) <= coerce_int(e[1]),
})

# Object-specific operators
OPERATORS.update({
    ("object", "exists"): lambda a, e: isinstance(a, dict),
    ("object", "empty"): lambda a, e: isinstance(a, dict) and len(a) == 0,
    ("object", "not_empty"): lambda a, e: isinstance(a, dict) and len(a) > 0,
})

# Int-specific operators
OPERATORS.update({
    ("int", "odd"): lambda a, e: coerce_int(a) % 2 != 0,
    ("int", "even"): lambda a, e: coerce_int(a) % 2 == 0,
    ("int", "min_odd"): lambda a, e: coerce_int(a) >= coerce_int(e) and coerce_int(a) % 2 != 0,
})

# Boolean
OPERATORS.update({
    ("bool", "eq"): lambda a, e: coerce_bool(a) == coerce_bool(e),
})
