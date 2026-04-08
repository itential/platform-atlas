# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

**Working style:** Discuss approaches before implementing anything non-trivial. Cody prefers
to evaluate options first. Ask for clarification rather than guessing on architecture
questions — he'd rather explain upfront than debug a wrong assumption later.

---

## Project Overview

**Platform Atlas** is an enterprise CLI tool for auditing and validating Itential Automation
Platform (IAP / Platform 6) deployments. It captures configuration from IAP and its
dependencies (MongoDB, Redis, Gateways), validates against versioned rulesets, and generates
professional compliance reports.

- **Package:** `platform-atlas` (entry point: `platform_atlas.main:main`)
- **Python:** `>=3.11,<4.0` (upper bound required to resolve hvac/dependency ceiling conflicts)
- **Dependency management:** Poetry
- **Platform support:** P6 (6.x) only — no 2022.x or 2023.x support in new work
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

**Sessions** are the primary unit of work. Each session binds an environment, ruleset, and
profile at creation time. Switching sessions atomically restores all three, ensuring audit
consistency. Session files live at `~/.atlas/sessions/<name>/`:

- `01_capture.json` — raw collected data
- `02_validation.parquet` — validation results (pandas DataFrame)
- `03_report.html` — generated compliance report

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

3. **Report:** `reporting_engine.py` handles JSON/CSV/Markdown; `reporting/report_renderer.py`
   generates the HTML report with themes and interactive modals.

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
`preflight.py`, `customer.py`.

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
| `validation/operators.py` | All validation operators — add new operators here |
| `reporting/report_renderer.py` | HTML report generation + theming |
| `core/handlers/session.py` | Main session workflow logic (capture, validate, report, diff) |
| `rules/rulesets/` | Versioned ruleset JSON files |
| `rules/rulesets/p6-master-ruleset.json` | Primary ruleset (~103 rules, P6 only) |
| `tests/conftest.py` | Shared pytest fixtures (`tmp_atlas_home`, `sample_config`, etc.) |

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

3. **Parquet attrs don't survive serialization** — DataFrame `.attrs` are lost on round-trip.
   Rehydrate metadata from capture JSON files when loading for report generation.

4. **`_parent_section_exists()`** — distinguishes "section captured but leaf missing" from
   "section never captured." Controls whether `default_value` applies in validation.

5. **Optional `None` pattern fields** must be guarded before passing to `lru_cache`-keyed
   regex compilation helpers.

6. **`paramiko` log suppression** — set `logging.getLogger("paramiko").setLevel(logging.CRITICAL)`
   in `transport.py` to suppress raw SSH tracebacks that pollute the UI.

7. **Redis `CONFIG GET`** requires `+config|get` ACL permission on the `itential` user.

8. **`manylinux_2_28` tags** (not legacy `manylinux2014`) for RHEL 8 offline pip downloads.

---

## Pylint Configuration

Max line length is **120 characters**. Broad exception catches (`W0718`) and `too-many-*`
complexity checks are disabled project-wide. See `[tool.pylint]` in `pyproject.toml`.
