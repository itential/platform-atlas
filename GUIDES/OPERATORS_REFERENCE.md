# Atlas Validation Engine — Operator Reference

This guide covers every operator available in the Atlas validation engine. Use it when writing or modifying rules in a ruleset.

Each rule specifies a **type** and an **operator**. The engine looks up the function registered at `(type, operator)` and calls it with two arguments:

- **`a`** — the *actual* value extracted from the capture data
- **`e`** — the *expected* value defined in the rule

---

## How Types & Coercion Work

Many operators coerce their inputs before comparing. Understanding coercion behavior is critical for writing rules that don't silently pass or fail.

| Coercer | Accepts | Notes |
|---|---|---|
| `coerce_int` | `int`, whole `float`, digit strings | Rejects `bool`, empty strings, non-whole floats |
| `coerce_bool` | `bool`, `0`/`1`, common string forms (`"yes"`, `"true"`, `"on"`, etc.) | Case-insensitive for strings |
| `extract_int` | `int`, strings with a leading number (`"512mb"`, `"2g"`) | Strips trailing non-digit characters |
| `parse_version` | PEP 440 strings, or strings containing a dotted version (`"TLSv1.2"`) | Falls back to regex extraction |
| `_normalize_list` | Any single value or list | Wraps scalars in a list, then stringifies every element |

---

## `int` Operators

All `int` operators coerce both sides through `coerce_int` before comparing.

### `int eq`

True when actual equals expected.

```yaml
type: int
operator: eq
expected: 3
```

| Actual | Result |
|---|---|
| `3` | ✅ |
| `"3"` | ✅ (string coerced) |
| `4` | ❌ |

### `int neq`

True when actual does **not** equal expected.

```yaml
type: int
operator: neq
expected: 0
```

| Actual | Result |
|---|---|
| `1` | ✅ |
| `0` | ❌ |

### `int gt`

True when actual is strictly greater than expected.

```yaml
type: int
operator: gt
expected: 1
```

| Actual | Result |
|---|---|
| `3` | ✅ |
| `1` | ❌ |

### `int gte`

True when actual is greater than or equal to expected.

```yaml
type: int
operator: gte
expected: 3
```

| Actual | Result |
|---|---|
| `3` | ✅ |
| `5` | ✅ |
| `2` | ❌ |

### `int lt` / `int lte`

Less-than and less-than-or-equal. Same pattern as `gt`/`gte`.

```yaml
type: int
operator: lt
expected: 10
```

### `int in_range`

True when actual falls within an inclusive range. Expected must be a two-element list: `[low, high]`.

```yaml
type: int
operator: in_range
expected: [1, 5]
```

| Actual | Result |
|---|---|
| `1` | ✅ |
| `3` | ✅ |
| `5` | ✅ |
| `6` | ❌ |

### `int odd`

True when actual is odd. Expected is ignored (but still required by the rule schema — use `true` or `null`).

```yaml
type: int
operator: odd
expected: true
```

| Actual | Result |
|---|---|
| `3` | ✅ |
| `4` | ❌ |

### `int even`

True when actual is even.

```yaml
type: int
operator: even
expected: true
```

### `int min_odd`

True when actual is **both** greater-than-or-equal-to expected **and** odd. Useful for replica set member counts.

```yaml
type: int
operator: min_odd
expected: 3
```

| Actual | Result |
|---|---|
| `3` | ✅ (≥ 3 and odd) |
| `5` | ✅ |
| `4` | ❌ (even) |
| `1` | ❌ (< 3) |

---

## `float` Operators

Float operators pass values through without coercion — the actual and expected values are compared directly as-is. This means both sides should already be numeric.

Supports: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in_range`.

Behavior is identical to `int` operators, just without the `coerce_int` step.

```yaml
type: float
operator: gte
expected: 0.75
```

---

## `parsed_int` Operators

Use `parsed_int` when the actual value may contain a unit suffix (e.g., `"512mb"`, `"2g"`). The engine calls `extract_int` on the actual side to strip trailing non-digit characters, and `coerce_int` on the expected side.

Supports: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in_range`.

```yaml
type: parsed_int
operator: gte
expected: 256
```

| Actual | Result |
|---|---|
| `"512mb"` | ✅ (extracts `512`) |
| `"128mb"` | ❌ (extracts `128`) |
| `256` | ✅ (plain int works too) |

### `parsed_int in_range`

```yaml
type: parsed_int
operator: in_range
expected: [256, 1024]
```

| Actual | Result |
|---|---|
| `"512mb"` | ✅ |
| `"2048mb"` | ❌ |

---

## `semver` Operators

Both sides are parsed through `parse_version`, which handles PEP 440 version strings and falls back to regex extraction for prefixed formats like `"TLSv1.2"`.

Supports: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in_range`.

```yaml
type: semver
operator: gte
expected: "7.0.0"
```

| Actual | Result |
|---|---|
| `"7.0.12"` | ✅ |
| `"6.0.19"` | ❌ |

### Version strings with prefixes

`parse_version` uses `re.search` to find the dotted-number portion, so prefixed strings work:

```yaml
type: semver
operator: gte
expected: "1.2"
```

| Actual | Result |
|---|---|
| `"TLSv1.3"` | ✅ (parses to `1.3`) |
| `"TLSv1.1"` | ❌ (parses to `1.1`) |

### `semver in_range`

```yaml
type: semver
operator: in_range
expected: ["7.0.0", "8.0.0"]
```

---

## `bool` Operators

### `bool eq`

Both sides are coerced through `coerce_bool`. This handles the type gymnastics common with Redis and other tools that return `"yes"`/`"no"`, `0`/`1`, or Python booleans.

```yaml
type: bool
operator: eq
expected: true
```

| Actual | Result |
|---|---|
| `true` | ✅ |
| `"yes"` | ✅ |
| `"on"` | ✅ |
| `1` | ✅ |
| `"false"` | ❌ |
| `0` | ❌ |

---

## `string` Operators

String operators work on raw string values with no coercion.

### `string eq` / `string neq`

Exact equality or inequality.

```yaml
type: string
operator: eq
expected: "allkeys-lru"
```

### `string in`

True when actual is a member of the expected list.

```yaml
type: string
operator: in
expected: ["error", "warn", "info"]
```

| Actual | Result |
|---|---|
| `"warn"` | ✅ |
| `"debug"` | ❌ |

### `string not_in`

True when actual is **not** in the expected list.

```yaml
type: string
operator: not_in
expected: ["debug", "trace"]
```

### `string contains`

True when expected is a **substring** of actual.

```yaml
type: string
operator: contains
expected: "replica"
```

| Actual | Result |
|---|---|
| `"replicaset"` | ✅ |
| `"standalone"` | ❌ |

### `string not_contains`

True when expected is **not** a substring of actual.

```yaml
type: string
operator: not_contains
expected: "NONE"
```

### `string exists`

True when actual is not `None` and not an empty string. Expected is ignored.

```yaml
type: string
operator: exists
expected: true
```

### `string empty`

True when actual is `None` or an empty string. Expected is ignored.

```yaml
type: string
operator: empty
expected: true
```

### `string safe_chars`

True when actual contains only alphanumeric characters, dots, underscores, and hyphens (`[a-zA-Z0-9._-]`). Useful for validating hostnames or identifiers.

```yaml
type: string
operator: safe_chars
expected: true
```

| Actual | Result |
|---|---|
| `"my-host.01"` | ✅ |
| `"my host!!"` | ❌ |

---

## `string_list` Operators

For comparing lists of strings.

### `string_list eq`

True when actual and expected are identical lists (same elements, same order).

```yaml
type: string_list
operator: eq
expected: ["node1", "node2", "node3"]
```

### `string_list contains`

True when a single expected value exists in the actual list.

```yaml
type: string_list
operator: contains
expected: "arbiter"
```

### `string_list contains_all`

True when **every** element in expected appears in actual.

```yaml
type: string_list
operator: contains_all
expected: ["keyFile", "sendKeyFile"]
```

### `string_list contains_any`

True when **at least one** element in expected appears in actual.

```yaml
type: string_list
operator: contains_any
expected: ["x509", "keyFile"]
```

### `string_list subset_of`

True when **every** element in actual appears in the expected list. Useful for allowlisting.

```yaml
type: string_list
operator: subset_of
expected: ["TLS1_2", "TLS1_3"]
```

| Actual | Result |
|---|---|
| `["TLS1_2"]` | ✅ |
| `["TLS1_2", "TLS1_3"]` | ✅ |
| `["TLS1_1", "TLS1_2"]` | ❌ (`TLS1_1` not in expected) |

### `string_list none_in`

True when **no** element from expected appears in actual. Opposite of `contains_any`.

```yaml
type: string_list
operator: none_in
expected: ["SSLv3", "TLS1_0"]
```

### `string_list empty`

True when actual is an empty list.

```yaml
type: string_list
operator: empty
expected: true
```

---

## `mixed_list` Operators

Use `mixed_list` when actual values may contain a mix of strings, ints, and bools (common with Redis configs, where `redis-py` coerces types). Both sides are normalized to lists of strings before comparison.

### `mixed_list contains_all`

True when every expected element (stringified) appears in the actual list (stringified).

```yaml
type: mixed_list
operator: contains_all
expected: ["TLS1_2", "TLS1_3"]
```

| Actual | Result |
|---|---|
| `["TLS1_2", "TLS1_3"]` | ✅ |
| `["TLS1_2", "TLS1_3", "foo"]` | ✅ |
| `["TLS1_2"]` | ❌ |

### `mixed_list contains_any`

True when at least one expected element appears in actual (both stringified).

```yaml
type: mixed_list
operator: contains_any
expected: ["TLS1_2", "TLS1_3"]
```

### `mixed_list eq`

True when both sides, after normalization and sorting, are identical.

```yaml
type: mixed_list
operator: eq
expected: ["yes", "1"]
```

| Actual | Result |
|---|---|
| `[True, 1]` | ✅ (normalized to `["1", "True"]` vs `["1", "yes"]` — ❌ actually) |
| `["yes", "1"]` | ✅ |

> **Tip:** Be careful with `mixed_list eq` — normalization converts values with `str()`, so `True` becomes `"True"`, not `"yes"`. If you need boolean-aware equality, consider individual `bool eq` checks instead.

---

## `object` Operators

For validating dictionary/object values.

### `object exists`

True when actual is a `dict`.

```yaml
type: object
operator: exists
expected: true
```

| Actual | Result |
|---|---|
| `{"key": "val"}` | ✅ |
| `None` | ❌ |
| `"string"` | ❌ |

### `object empty`

True when actual is a `dict` with no keys.

```yaml
type: object
operator: empty
expected: true
```

### `object not_empty`

True when actual is a `dict` with at least one key.

```yaml
type: object
operator: not_empty
expected: true
```

---

## Quick Reference

| Type | Operator | Expected | Description |
|---|---|---|---|
| `int` | `eq` `neq` `gt` `gte` `lt` `lte` | number | Standard comparison (coerced) |
| `int` | `in_range` | `[low, high]` | Inclusive range |
| `int` | `odd` `even` | ignored | Parity check |
| `int` | `min_odd` | number | ≥ expected AND odd |
| `float` | `eq` `neq` `gt` `gte` `lt` `lte` | number | Standard comparison (no coercion) |
| `float` | `in_range` | `[low, high]` | Inclusive range |
| `parsed_int` | `eq` `neq` `gt` `gte` `lt` `lte` | number | Strips unit suffix from actual |
| `parsed_int` | `in_range` | `[low, high]` | Inclusive range |
| `semver` | `eq` `neq` `gt` `gte` `lt` `lte` | version string | PEP 440 comparison |
| `semver` | `in_range` | `[low, high]` | Inclusive version range |
| `bool` | `eq` | bool | Coerces `"yes"`/`"no"`, `0`/`1`, etc. |
| `string` | `eq` `neq` | string | Exact match |
| `string` | `in` `not_in` | list | Membership test |
| `string` | `contains` `not_contains` | string | Substring test |
| `string` | `exists` `empty` | ignored | Presence check |
| `string` | `safe_chars` | ignored | Alphanumeric + `._-` only |
| `string_list` | `eq` | list | Exact list match (ordered) |
| `string_list` | `contains` | string | Single element membership |
| `string_list` | `contains_all` `contains_any` | list | Multi-element membership |
| `string_list` | `subset_of` | list | All actual elements in expected |
| `string_list` | `none_in` | list | No expected elements in actual |
| `string_list` | `empty` | ignored | Empty list check |
| `mixed_list` | `eq` | list | Sorted stringified equality |
| `mixed_list` | `contains_all` `contains_any` | list | Stringified membership |
| `object` | `exists` `empty` `not_empty` | ignored | Dict presence/emptiness |

---

## Common Gotchas

**`redis-py` type coercion** — Redis config values come back as Python `int` or `bool` instead of strings. Use `bool eq` for yes/no flags and `mixed_list` for lists that may contain coerced types.

**`re.match` vs `re.search`** — `parse_version` uses `re.search`, so prefixed version strings like `"TLSv1.2"` work correctly. If you see version comparison failures, check that the version string actually contains a parseable dotted number.

**`string_list eq` is ordered** — If order doesn't matter, use `contains_all` with the full set in both directions, or consider `mixed_list eq` which sorts before comparing.

**Expected is always required** — Even for operators that ignore expected (`odd`, `even`, `exists`, `empty`, `safe_chars`), the rule schema still requires the field. Use `true` as a conventional placeholder.
