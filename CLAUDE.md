# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

**Working style:** Discuss approaches before implementing anything non-trivial. Cody prefers
to evaluate options first. Ask for clarification rather than guessing on architecture
questions — he'd rather explain upfront than debug a wrong assumption later.

---

## Project Overview

**Platform Atlas** is an enterprise CLI tool for auditing and validating Itential Platform
(Platform 6) deployments. It captures configuration from Platform and its dependencies
(MongoDB, Redis, Gateways), validates against versioned rulesets, and generates
professional compliance reports. Terminology since 3.0: "Itential Platform"/"Platform"
(not IAP) and "Itential Gateway"/"IG" (not IAG), with `IG-XXX` rule IDs. The `iap`/`iag`
`NodeRole` values and the `iagctl` binary keep their names — they are code identifiers.

- **Package:** `platform-atlas` (entry point: `platform_atlas.main:main`)
- **Python:** `>=3.11,<4.0` (upper bound required to resolve hvac/dependency ceiling conflicts)
- **Dependency management:** Poetry
- **Platform support:** P6 (6.x) only — no 2022.x or 2023.x support
- **Distribution:** `.whl` via GitLab Generic Package Registry; air-gapped offline bundle for RHEL/Rocky 8+9

### Branding & Identity

| Field | Value |
|---|---|
| CLI command | `platform-atlas` |
| Config/sessions dir | `~/.atlas` |
| Config field | `organization_name` (never `company_name`) |
| Brand colors | Navy `#101625`, Blue `#1B93D2`, Orange `#FF6633`, Green `#99CA3C`, Pink `#C5258F` |
| Fonts | Montserrat (headlines), Open Sans (body) |

---

## Commands

### Install & Build

```bash
# Primary: install all dependencies
poetry install

# Build distributable wheel
poetry build

# Run the CLI (primary)
poetry run platform-atlas <command>

# Alternative: editable install (useful for some tooling workflows)
pip install -e .
platform-atlas <command>

# Run a module directly
poetry run python -m platform_atlas <command>
```

### Running Tests

```bash
pytest tests/                                         # all tests
pytest tests/test_capture_engine.py -v               # single file
pytest tests/test_validation_engine.py::test_name    # single test
pytest tests/ --cov=src/platform_atlas               # with coverage
```

### Linting

```bash
pylint src/platform_atlas/ --rcfile=pyproject.toml
bandit -r src/platform_atlas/ --skip B105,B106
```

---

## Architecture

### Core Concepts

**Tiers** (three since 2.0; Standard/Extended split introduced in 1.7):

- **Standard** — application-only audit via Platform OAuth + optional Gateway 4 API token.
  57 rules across the `platform` and `gateway4` categories. No SSH, no MongoDB, no Redis.
  Default for fresh installs.
- **Extended** — full infrastructure audit. Adds the SSH/Mongo/Redis/Gateway/system/filesystem/
  Kubernetes collectors. All 123 rules. Default for installs upgraded from 1.6.x (migration
  shim preserves the existing experience).
- **SaaS** (2.0+; redefined in 3.0) — **Platform-anchored but limited**. Platform OAuth is
  REQUIRED and drives a read-only pull for exactly 8 adapter/application AVC checks
  (`SAAS_AVC_GROUP` in `core/config.py`: the 7 `adapter_*` checks — health_data,
  limit_errors, logger_levels, states, throttling, timeouts, versions — plus
  `application_states`; `adapter_brokers` and `adapter_file_data` are deliberately out).
  The gateway is OPTIONAL and chosen per environment via `saas_gateway_kind` (`gateway4`,
  `gateway5`, `gw4-gw5`, or unset = Platform-only). **No `PLAT-*` compliance rules ever, no
  Platform SSH ever, no Mongo/Redis/Kubernetes.** Gateway SSH IS collected and is required
  for a gateway audit (GW4 has no API-only path; GW5 may instead use its local
  Compose/Helm file source; GW4+GW5 collects SSH for both hosts, which requires
  `capture_scope: all_nodes` because both use `role:"iag"`). Rule categories are the chosen
  gateway's only (`_saas_categories()`; empty for Platform-only) — per-rule `tier:"extended"`
  flags are INCLUDED, since they mean "needs infra access" and SaaS has gateway SSH. Uses the
  `gateway_only` deployment mode, or no topology at all for a Platform-only environment
  (`synthesize_saas_targets` emits the Platform api target, plus a GW4 api target when
  applicable). Never a conversion target — tier fixed at env create. A WebUI SaaS first-run
  *does* write `tier:"saas"` as the global default so future envs default to SaaS (amended
  2026-06-11); the CLI `tier set saas` command stays blocked. Produces `report.html` with the
  Architecture Overview merged into the Compliance page (no separate Architecture page, no
  arch-warnings); the 8 AVC results ride in the viewmodel's `architecture.extended_checks`.
  The AVC gate is enforced at validation time (`validation/extended_validation.py::run_extended_validation`
  and `validation_engine._extended_checks_enabled()` — both had to change), and users may
  disable members of the group but never enable anything outside it.

Three independent defenses enforce the tier boundaries:

1. **Registry pruning** — `capture/modules_registry.py::_build_modules_standard` returns
   only `platform` + `gateway4_api` modules; `_build_modules_saas` returns the chosen
   gateway's modules (kind-narrowed) plus a scoped Platform pull
   (`PlatformCollector.get_saas_platform_info`: adapter/application endpoints only, no index
   sweep) and never Mongo/Redis/Kubernetes. Its docstring still describes the pre-3.0
   gateway-only model — the code is authoritative.
2. **Guards** (`core/context.py`) — `require_extended()` is strict (raises unless tier is
   Extended; used by mongo/redis/kubernetes collectors), `require_infra()` raises only in
   Standard (used by `transport.py`'s SSH branch, system/filesystem/gateway collectors, and
   the architecture-form launcher — SaaS passes), and `forbid_in_saas()` now blocks only the
   RBAC/authorization collector (`capture/collectors/authorization.py`); the Platform
   collector itself is allowed under SaaS. All raise `TierViolationError` before any
   network connection.
3. **Tier-aware credential store** — per-tier applicable sets in `credentials.py`
   (`applicable_keys()`/`required_keys()`): keys outside the active tier's set are silently
   `None` on read and raise on write. `PLATFORM_SECRET` is required in every tier, SaaS
   included, so `Config.platform_client_secret` raises when it's missing (the old SaaS
   `""` short-circuit is gone). SaaS-applicable keys are `PLATFORM_SECRET`, the SSH
   passphrase/password, and `GATEWAY4_PASSWORD` — never the Mongo/Redis URIs.
   `EXTENDED_ONLY_KEYS` remains as the Standard-hidden back-compat set. (The
   `CredentialKey.required` docstring still says SaaS doesn't need the Platform secret —
   stale; `_TIER_REQUIRED_KEYS` is authoritative.)

Rule filtering happens in `rules.py` before evaluation. Tier resolution order:
`--tier` flag → `ATLAS_TIER` env var → environment overlay → config → default.
Sessions bind tier at create time alongside ruleset/environment — switching sessions
atomically restores the full context.

**Sessions** are the primary unit of work. Each session binds an environment, ruleset, and
profile at creation time. Switching sessions atomically restores all three, ensuring audit
consistency. Session files live at `~/.atlas/sessions/<name>/`:

- `01_capture.json` — raw collected data
- `02_validation.json` — validation results (`ValidationResults`: rows + metadata)
- `report.html` — generated report (Compliance, Operational, and Architecture as pages in one file)

**The session lifecycle:** `create` → `capture` → `validate` → `report`

**AtlasContext** (`core/context.py`) is a singleton initialized once in `main()` and accessed
globally via `ctx()`. It holds the active `Config`, `Theme`, `RulesetManager`, and loaded
`Ruleset`. Never pass context as a parameter — always call `ctx()`. No silent defaults, no
inconsistent init patterns.

### Capture Pipeline (mandatory order)

```
Preflight → Automated Capture → Manual/Extended → Validation → Report
```

### Protocol-Primary Model (critical architecture)

For `mongo_conf`, `redis_conf`, and `gateway4_conf`, the data source hierarchy is:

| Subsystem | Primary source | Fallback source |
|---|---|---|
| MongoDB config | pymongo `getCmdLineOpts` | SSH → `mongod.conf` |
| Redis config | redis-py `CONFIG GET` | SSH → `redis.conf` |
| Gateway4 config | ipsdk `GET /config` | SSH → `properties.yml` |
| Gateway4 version | ipsdk `GET /status` | SSH → pip list |

- SSH conf modules are **never registered** for these — protocol handles them
- SSH fallback is triggered only when protocol fails, post-capture
- Rulesets reflect this: `path` → protocol data, `alt_path` → SSH data
- Gateway4 runtime truth is in `automation-gateway.db` (via `GET /config`); `properties.yml`
  on disk may be stale after first boot

### Data Flow

```
parse_args() → init_context()
                     ↓
              Load config.json + merge active environment overlay
              Load RulesetManager + Ruleset
                     ↓
              dispatch(args) → handler
```

**Capture → Validate → Report:**

1. **Capture:** Collectors (`capture/collectors/`) connect via SSH, pymongo, redis-py, OAuth,
   or ipsdk. Output is **flat** (e.g., `full_capture_json["gateway4_api"]`) and reshaped to a
   **nested** hierarchy (e.g., `structured["gateway4"]["runtime_config"]`) by
   `reshape_capture()` in `capture_engine.py`.
   - **Important:** Deferred/verification checks run **before** reshape — use flat keys there.
   - `finalize_capture()` passthrough blocks work on the **nested** structure.
   - `filter_capture_by_rules()` strips fields not referenced by rule paths — add explicit
     passthrough blocks for sections that must survive (logs, replica set data, etc.).

2. **Validate:** `validation_engine.py` evaluates each rule using dot-notation path extraction
   against the nested capture data. Rules use typed operators defined in `validation/operators.py`.
   Returns a `ValidationResults` (`validation/results.py`) — `rows: list[dict]` plus a `metadata`
   dict — persisted as-is to `02_validation.json`.

3. **Report:** `reporting_engine.py` handles JSON/CSV/Markdown; `reporting/webui_viewmodel.py`
   builds the viewmodel and `reporting/unified_renderer.py` renders it client-side into
   `report.html` (Compliance, Operational, and Architecture as pages in one file).

### Configuration & Environments

**Config** (`core/config.py`) is a frozen dataclass loaded from `~/.atlas/config.json`. If an
`active_environment` is set, the corresponding `~/.atlas/environments/<name>.json` is loaded
and merged as an overlay on top of the global config.

Environment resolution order: `--env` flag → `ATLAS_ENV` env var → `active_environment` in
config → no environment.

`config.py` is a **pure data-loading module** — no UI or interactive imports. Those belong in
modules that already own those responsibilities.

**Credentials are never stored in config files.** Retrieved at runtime from OS keyring
(`keyring` library, scoped per environment as `platform-atlas/<env_name>`) or HashiCorp Vault
KV v2 (`hvac`). The `credential_backend` field in config controls which is used.

### Topology & Capture

`DeploymentTopology` (`core/topology.py`) models the target infrastructure. Deployment modes:
`standalone`, `ha2`, `custom`, `kubernetes`. Each `TargetNode` has a `NodeRole` (`iap`,
`mongo`, `redis`, `iag`, etc.) that determines which collector modules run on it.

`CaptureScope` controls breadth: `PRIMARY_ONLY` (default, one node per role) vs `ALL_NODES`
(every node in topology).

### Rules System

Rulesets are versioned JSON files in `rules/rulesets/`. Active ruleset and profile are managed
by `RulesetManager` (`core/ruleset_manager.py`). Profiles are JSON overlays in
`rules/rulesets/profiles/` that enable/disable rules for specific environments.

Profile visibility is scoped by tier, filtered centrally in the manager's
`discover_profiles()` so every listing and picker (CLI + WebUI) inherits it: a profile with
`"tier": "saas"` in its JSON (the bundled `saas-gateway4`/`saas-gateway5`/`saas-gw4-gw5`
trio — each keeps only the `IG-` rules for its gateway kind enabled) is listed ONLY under the
SaaS tier, and the SaaS tier lists ONLY those (ctx-resolved tier; `include_all_tiers=True` to bypass).

`ensure_profile_allowed()` guards explicit activation (`ruleset profile set`,
`ruleset load --profile`, WebUI activate) — session switching bypasses the
guard on purpose, because session bindings restore atomically with their own tier/env.

Rules follow the schema in `rules.schema.json`. Each rule has a `path` (dot-notation into
capture data), a `validation` block with `operator` and `expected`, and optional `alt_path`
fallback.

**Operator types:** `int`, `string`, `bool`, `semver`, `parsed_int`, `string_list`,
`mixed_list`, `object`. New operators go in `operators.py` — never embed logic in rules.

`mixed_list` exists because redis-py coerces some numeric config values to integers.
`bool eq true` handles redis-py coercing yes/no values to Python booleans.

### Command Dispatch

CLI args are parsed in `core/cli.py` using `argparse` + `RichHelpFormatter`. `core/dispatch.py`
routes commands via a registry (`core/registry.py`) to handler functions in `core/handlers/`.
Handler files map 1:1 to command groups: `session.py`, `config.py`, `env.py`, `ruleset.py`,
`tier.py`, `preflight.py`, `guide.py`, `continuous.py`, `fleet.py`, `support_bundle.py`.

---

## Key File Locations

| File | Purpose |
|---|---|
| `core/context.py` | AtlasContext singleton, `ctx()` global accessor |
| `core/config.py` | Config dataclass, environment overlay merge — pure data loading |
| `core/topology.py` | Deployment modes, TargetNode, CaptureScope |
| `core/transport.py` | SSH/local/Kubernetes connectivity + retry logic |
| `core/credentials.py` | CredentialKey enum, keyring + Vault backends |
| `core/ruleset_manager.py` | Profile system — rule enable/disable overlays |
| `capture/capture_engine.py` | Collector orchestration, `reshape_capture()`, `finalize_capture()` |
| `capture/modules_registry.py` | Role-based collector module resolution |
| `validation/validation_engine.py` | Rule evaluation, dot-notation path extraction |
| `validation/results.py` | `ValidationResults` (rows + metadata) — save/load `02_validation.json` |
| `validation/operators.py` | All validation operators — add new operators here |
| `reporting/webui_viewmodel.py` | Builds the report/WebUI viewmodel (typed JSON contract) |
| `reporting/unified_renderer.py` | Renders the viewmodel client-side into `report.html` |
| `core/handlers/session.py` | Main session workflow logic (capture, validate, report, diff) |
| `rules/rulesets/` | Versioned ruleset JSON files |
| `rules/rulesets/p6-master-ruleset.json` | Primary ruleset (123 rules, P6 only) |
| `tests/conftest.py` | Shared pytest fixtures (`tmp_atlas_home`, `write_config`, `sample_environment`, `sample_ruleset`, etc.) |

---

## Coding Conventions

- **Dataclasses** for data-holding structures
- **Enums** for known named sets (`CredentialKey`, `NodeRole`, etc.)
- **Direct functions** for utilities and stateless operations
- **Classes with methods** for stateful components (collectors, engines)
- Decorators and dunder methods only when they genuinely add value — no over-engineering
- `ensure_ascii=False` on **all** `json.dumps` calls (em dashes appear in rule messages)
- Fix problems upstream so downstream loaders never see them (e.g., `ensure_valid_environment()`
  called before `load_config()`)
- Partial failure = still success — never abort the whole capture on a single collector failure

---

## HTML / CSS Rules (Reports)

- **Sticky table columns:** always hardcode hex backgrounds — **never** `rgba` or CSS variables.
  They cause transparency bleed-through on sticky columns.
- Explicit z-index hierarchy: `td: 25`, `th: 35 !important`
- Report dark-mode background: `#101625`
- No external URLs in reports — base64-encode all assets including the Itential logo
- Script execution order matters: scripts exposing globals must appear before scripts consuming them

---

## Hard-Won Lessons

Read before touching these areas — these came from real debugging sessions:

1. **`questionary` swallows KeyboardInterrupt** — returns `None` instead of raising. All
   `_ask_*` helpers must check for `None` and raise `KeyboardInterrupt`.

2. **Rich Live + logging** — `logger.warning/error` during Rich Live corrupts terminal output.
   Use `logger.debug` only for anything that fires during capture.

3. **Validation metadata lives inline, nothing to rehydrate** — `02_validation.json`
   (`validation/results.py::ValidationResults`) stores `rows` and `metadata` together as one
   JSON document, so `load_validation_results()` always returns both in one read. This replaces
   the old pandas-DataFrame-+-`.attrs`-+-Parquet design, where `.attrs` did not survive a
   `to_parquet`/`read_parquet` round-trip and had to be rehydrated from the capture JSON on every
   load (`rehydrate_validation_attrs()`, now deleted). Note for context: in ad-hoc testing on the
   versions pinned right before this migration (pandas 3.0.5 / pyarrow 24.0.0), `.attrs` actually
   *did* survive a fresh-process round-trip — the original failure mode this lesson described may
   have been fixed upstream at some point. Moot either way now that parquet is gone entirely.

4. **`_parent_section_exists()`** — distinguishes "section captured but leaf missing" from
   "section never captured." Controls whether `default_value` applies in validation.

5. **Optional `None` pattern fields** must be guarded before passing to `lru_cache`-keyed
   regex compilation helpers.

6. **`paramiko` log suppression** — set `logging.getLogger("paramiko").setLevel(logging.CRITICAL)`
   in `transport.py` to suppress raw SSH tracebacks that pollute the UI.

7. **Redis `CONFIG GET`** requires `+config|get` ACL permission on the `itential` user.

8. **`manylinux_2_28` tags** (not legacy `manylinux2014`) for RHEL 8 offline pip downloads.

9. **`ValidationStatus` rows: never `str(row["status"])`** — `ValidationStatus` is a `str, Enum`
   whose value equality (`row["status"] == ValidationStatus.PASS`) works fine, but calling the
   `str()` builtin on a member invokes `Enum.__str__`, returning `"ValidationStatus.PASS"` instead
   of `"PASS"`. Calling `.upper()`/`.lower()` etc. directly on the value is safe (those methods are
   inherited straight from `str` and operate on the actual character data), which is why
   `validate()`'s row-normalization pass calls `row['status'].upper()` — never `str(row['status'])`
   — to collapse every row onto a plain `str` before it goes anywhere near a `02_validation.json`
   write or a `.count("PASS")` check.

---

## Pylint Configuration

Max line length is **120 characters**. Broad exception catches (`W0718`) and `too-many-*`
complexity checks are disabled project-wide. See `[tool.pylint]` in `pyproject.toml`.
