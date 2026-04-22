# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.6.3] - 2026-04-21

### Added

- **Three-report system** — `session run report` now generates all three HTML reports in a single pass: `03_report.html` (compliance), `04_operational.html` (logs + MongoDB pipelines), and `05_arch.html` (architecture & maintenance); the browser opens the compliance report automatically on completion
- **Operational Report** (`04_operational.html`) — log analysis sections (platform, webserver, MongoDB) moved here from the compliance report; MongoDB aggregation pipeline results appear above the log sections
- **Architecture & Maintenance Report** (`05_arch.html`) — additional validation checks (adapter states, Redis ACL, index status, IAG4 paths, etc.) rendered as tabbed panels matching the compliance report style; architecture overview data displayed below
- **Cross-report navigation** — all three reports share a header nav bar linking to each other; active report is highlighted (blue for compliance, green for operational, orange for architecture)
- **MongoDB Operational Pipelines prompt** — after capture completes, Atlas asks whether to run MongoDB aggregation pipelines; a colored Rich Panel callout explains the option; if declined, the operational report renders with logs only and a clear notice
- **`keep_logs_file` config option** — controls whether `01_logs.json` is retained after all reports are generated; defaults to `false` (delete after use)
- **Log date-range filtering** — `session run capture` accepts two new optional flags: `--log-since DATE` and `--log-until DATE` (format `YYYY-MM-DD`); either flag can be used independently or together; when active, all three log collectors switch to grep-based extraction instead of `tail -n 50000` — platform logs use `grep -lE` to identify relevant files before reading, webserver and MongoDB logs use `grep -E` with per-day or per-month date patterns; normal mode behavior is unchanged when neither flag is supplied
- **Date range banner in Operational Report** — when a log date range was used during capture, a green calendar banner is displayed at the top of `04_operational.html` showing the captured window (e.g., `2026-04-01 — 2026-04-21`, `2026-04-01 through capture date`, or `up to 2026-04-21`); the range persists in `session.json` so it survives report re-runs and `01_logs.json` cleanup
- `log_since` and `log_until` fields added to `SessionMetadata` — stored in `session.json` at capture time; older session files without these fields default to empty strings safely

### Changed

- `--operational` flag removed from `session run report`; all three reports are always generated together
- Additional validation log checks (`platform_log_analysis`, `webserver_log_analysis`, `mongo_log_analysis`) moved out of the compliance report's "Additional Validation" tab into the Operational Report
- Architecture & Maintenance Report renders additional validation checks as interactive tabbed panels (same behavior as the compliance report's extended section), with checks sorted by severity and passing checks collapsed by default
- Log grep commands use multiple `-e FLAG` arguments instead of a single `|`-joined pattern to satisfy the transport layer's shell-metacharacter security validator

### Fixed

- `KeyError` on missing `stateStr` key in MongoDB replica set member documents during health derivation in `capture_engine.py` — changed unsafe `m["stateStr"]` to `m.get("stateStr")` so a missing field is treated as an unhealthy state rather than crashing the capture
- `IndexError` risk in `_pick_profile()` when the `available` profile list is empty — the `available[0].id` fallback now guards against the empty-list case; normal flow is unchanged since the early-exit guard already returns `None` before this line is reached
- Missing `ensure_ascii=False` in `OperationalReport.to_json()` — Unicode characters (pipeline names, descriptions, error messages containing em dashes) were being escaped as `\uXXXX` sequences instead of being written as-is; aligns with the project-wide encoding contract
- Removed dead `df.attrs["organization_name"]` assignment in `handle_session_run_validate()` — the attrs dict is discarded when the DataFrame is immediately saved to Parquet on the next line; `_rehydrate_attrs()` already correctly restores this value from the capture JSON during report generation
- Sticky table columns in Operational and Architecture reports now use hardcoded hex backgrounds to prevent transparency bleed-through
- Collapsible section toggle in Architecture Report corrected to use `classList.toggle('collapsed')` instead of `maxHeight` approach, which conflicted with the CSS `display:none` rule
- `OperationalReport.from_json()` classmethod added to support deserializing cached MongoDB pipeline results when generating the Operational Report in a separate `session run report` invocation
- `--log-since` used alone no longer raises `OverflowError`; the `until` bound defaults to `datetime.now(UTC)` and the `since` bound defaults to one year before `until` when only one flag is provided

---

## [1.6.2] - 2026-04-09

### Added

- Split the User Guide into two parts, `USER-GUIDE-INSTALLATION-AND-USAGE.md` and `USER-GUIDE-READING-THE-REPORT.md`
- Created two HTML user guides to give a visual overview of both user guides as well
- HTML Architecture Overview collector — opens `architecture-form.html` in the user's browser, waits for the JSON export, then imports it automatically; falls back to CLI prompts if the user opts out or the file cannot be found
- `manual_input_mode` config field (`"html"` default / `"cli"`) controls which architecture collector mode is used; set via `config set manual_input_mode cli` to prefer terminal prompts
- Architecture form is bundled inside the package under `platform_atlas/guides/` and synced to `~/.atlas/architecture-form.html` on first use (stale copies are replaced automatically using size + SHA-256 comparison)
- `PROJECT_GUIDES` path constant added to `core/paths.py` pointing to the bundled guides directory
- Architecture capture now reuses existing `~/.atlas/architecture.json` data without re-prompting when a completed collection is already present

## [1.6.1] - 2026-04-08

### Added

- `--version` now includes Python version, Python executable path, and OS/platform alongside the Atlas version string
- `session prune --older-than DAYS` bulk-deletes uncaptured sessions (created but never captured) older than the specified number of days; supports `--dry-run` to preview and `--force` to skip confirmation

### Changed

- Fixed the licensing issues in a few files to reflect the GPL 3.0 license properly

## [1.6] - 2026-04-08

### Added

- MongoDB logs are also now gathered to run top-10 and heuristic keyword analysis on
- Environment setup now asks if environment is kubernetes, will handle parsing values.yaml and some basic kubectl commands to gather data, since ssh is not applicable in these environments

## [1.5] - 2026-04-03

### Added

- Sessions are now the primary organizational unit in Atlas — each session carries an assigned Environment, Ruleset, Profile, and Organization, establishing a consistent context across capture, validation, and reporting
- Gateway4 API connectivity via `ipsdk` for direct runtime config collection without SSH
- Protocol-primary collection model for `mongo_conf` and `redis_conf` — SSH config file parsing now serves as an automatic fallback when direct protocol collection is unavailable
- Additional Validation and Architecture Overview sections to JSON and Markdown report exports
- `env edit` command to modify an environment's configuration after initial creation
- Improved breakdown of top endpoints and related metrics in the Additional Validation log output
- Keyboard arrow and tab navigation between rule entries in the modal detail window
- Severity level tooltip in the modal window to clarify rule impact
- Knowledge Base remediation steps now appear by default inside the modal Details view

### Changed

- Collector architecture revised to reduce over-reliance on SSH connectivity in favor of direct protocol connections
- Report dashboard visual style refreshed; next steps are now displayed more prominently
- `--fixes` flag inverted to `--no-fixes` — KB remediation steps are shown by default and can be suppressed explicitly
- `PLAT-015` expected value updated to `in_range 5–10`
- `PLAT-040` now validates against the parsed semantic version of Python rather than a boolean check
- `PLAT-038` now depends on `PLAT-010` with a dynamic `when_version_below` conditional check
- `rules.schema.json` updated to include the new `when_version_below` property
- CHANGELOG converted to markdown format to better align with software documentation at Itential

### Fixed

- SSH connection error output now surfaces cleanly without polluting the capture UI
- Spelling error corrected in the `PLAT-004` rule name

---

## [1.4.2] - 2026-03-28

- Modified Operator Report to use a tab-based layout for better organization of data
- Modified Health Report to auto-disable a few columns, and move that data into a modal window to view more
- Report modal window shows human-readable recommendations and actual/expected operators to know what a rule actually checks for
- Removed redundant summary cards and subtitles on report that repeated data
- Moved Additional Validation checks into a tabbed layout for better organization
- Added auto-sync feature to keep bundled pipelines and rulesets with Atlas synced with local copies
- Updated rule recommendation messages for better clarity on next steps
- Fixed rule IAG-003 to calculate and check 3x CPU cores, not 4x, and changed the operator to `eq`
- Fixed rule IAG-007 to check the correct value of `ldap_auth_enabled` in the properties file
- Adjusted `pyproject.toml` for constraints against installing major versions without being updated first

---

## [1.4.1] - 2026-03-25

- Added a unified template for both Report and Operational modes
- Updated the unified templates for better visuals and display of information
- Added a credential redact function for rule `PLAT-027`
- Added additional pipelines to run during the operational report
- Various bugfixes throughout the code

---

## [1.4] - 2026-03-20

- New: Operational Reports (`session run report --operational`)
- Operational Reports utilize IAP Metrics pipeline(s) to run MongoDB aggregations
- Added `--import-dir` to `--manual` capture to batch import manual capture files to make it faster
- Refined the `collect_platform.sh` script to be more user-friendly
- Added a `MANUAL_CAPTURE` guide for when you can't install Atlas or use it to connect to remote service(s)
- Moved all guides into the `GUIDES` directory (README stays in the top-level)
- Significantly improved the manual capture, fixed bugs that prevented some data from being processed
- Fixed Changed/Unchanged button logic in diff reports to work correctly

---

## [1.3.2] - 2026-03-16

- Fixed issue if environment no longer exists to provide a `env switch` fallback
- Now asks if using IAP 2023.x during environment setup and sets `legacy_profile` value if so
- Fixed issues with `session.json` not showing updated or correct values

---

## [1.3.1] - 2026-03-12

- Lots of user experience updates in this version
- Removed `env edit` command to remove any subprocess usage for better security
- Changed diff template to use the same CSS style as `report-light`
- Diff reports are now saved into `~/.atlas/diff`
- Session switching is faster now; `session active` without any arguments gives an interactive selection list
- Set an alias option for `session active` called `session switch` to give a uniform naming scheme like `env switch`
- Additional interactive selections for `session diff` and `session delete` when no arguments provided
- Added new option `ruleset setup` that gives an interactive ruleset and profile selection together
- Added dashboard improvements to be more user-friendly
- Updated markdown files to include the new `ruleset setup` as the primary option

---

## [1.3] - 2026-03-11

- Added Environments so that multiple different environments could be used and switched between
- Re-worked the manual collector questions to be more clear and concise
- Manual collector now uses environment details to auto-fill a lot of initial questions to save time
- Added questions into the manual collector about monitoring systems and practices
- Moved the architecture JSON into `~/.atlas` so it covers all sessions rather than per-session
- Copied the architecture values into the HTML Report, as we weren't currently displaying that info
- Adjusted the light HTML theme to match the Itential branding style better
- Moved rulesets and profiles into `~/.atlas` directory to decouple them from being part of the main application
- Added `chmod 0o600` to validation file and report file
- Updated guided collector (`--manual`) to ask for IAG4 conf and Redis conf
- Updated guided collector to only ask for modules that would have run automatically
- Various bug and performance fixes

---

## [1.2.2] - 2026-03-09

- Split capture logs into `01_capture` and `01_logs`
- Captured logs (`01_logs.json`) are deleted for security purposes after running `session run report`
- Added some additional MongoDB commands to get configuration data from `mongosh`
- Removed a couple of mongo test rules that hadn't been removed from the rulesets
- Added a `--fixes` flag for the report to include remediation steps in the HTML report
- Moved `RULES_KNOWLEDGEBASE` into platform src directory
- Enhanced `report-dark.html` for better Itential styling
- Added a `SSH_SETUP_GUIDE` file to provide detailed instructions for adding SSH access

---

## [1.2.1] - 2026-03-07

- Re-worked the permissions setup for the MongoDB user to work correctly
- Added `QUICKSTART` and `USER-GUIDE` markdown files
- Changed the guide option to load the `USER-GUIDE` rather than the README
- Changed the HTML report so that it auto-collapses the LOGS section due to lengthy output
- Added `--log-mode`, `--log-top-n`, `--log-levels`, and `--skip-logs` to the capture engine flags
- Fixed issues with heuristics mode not working correctly
- Added `ThreadPoolExecutor()` for SSH to read files faster (2 workers, kind to server)
- Set the `find` command for platform logs to `-3M` size and `-mtime -7` to capture only the relevant info
- Added `ruleset_profile` to capture metadata through to the HTML report
- Updated the HTML templates to better reflect Itential branding guidelines, colors, etc.
- Removed review for captured data due to large size; users can manually review the JSON file if needed
- Added additional security checks to various sensitive dataclasses

---

## [1.2] - 2026-03-06

- Replaced `requests-oauthlib` with the Itential `ipsdk` library for the platform collector
- Added Markdown file support to `report --export` format
- Added webserver log parsing in additional validation checks
- Added additional fallback modes to reading files during the capture process
- Fixed dependent rules on `RDS-007` and implemented proper Redis sentinel checks
- Modified report templates for easier readability
- Updated README with new information on HashiCorp Vault usage

---

## [1.1] - 2026-03-04

- Added HashiCorp Vault support with token or AppRole authentication
- Added Mongo and Redis collectors as optional during init setup
- Added Platform Log Analysis into Additional Validation checks
- Added SSH key selector during init setup to make it easier to select an SSH key
- Added `--headless` mode to `session run all` to remove any interactive prompts
- Fixed filesystem modules to only run on the respective servers
- Fixed session exports not working correctly
- Fixed logic on exports to `--no-redact` for optionally including `01_capture.json`
- Fixed issue with init setup not able to re-run if interrupted by user
- Adjusted layout of dashboard to make it a bit easier to read
- Adjusted the font size of the HTML report to make it slightly larger and easier to read
- New dependencies: `hvac` (HashiCorp Vault)
- Various additional bug and security fixes

---

## [1.0.0] - 2026-03-02

- Added more details for the `RULES_KNOWLEDGEBASE` file
- Fixed bug that prevented other modules from running if SSH module(s) failed
- Added some additional `alt_path` fallbacks in `p6-master-ruleset.json`

---

## [1.0.0rc2] - 2026-02-26

- Added support and rulesets for IAP 2023.x
- Fixed preflight SSH connection issues
- Added scripts with a platform collector script to make manual platform collection easier
- Updated README to include keyring instructions for servers if needed
- Updated `RULES_KNOWLEDGEBASE` with more rule guides and fix details
- Visual color fixes to make it easier to read the guided questions
- Implemented lazy-loading for heavy dependencies to reduce CLI startup times
- Improved error messaging in a few locations
- Fixed various bugs and spelling mistakes throughout codebase

---

## [1.0.0rc1] - 2026-02-23

- Moved credentials from `config.json` into the OS-level keyring
- Init Setup stores credentials in keyring; `Config` class pulls from keyring
- Added a new menu: `config credentials`, for modifying or deleting credentials
- Fixed show active profiles overrides count value
- Bandit and Pylint check fixes throughout codebase
- Enhanced debug logging throughout the codebase

---

## [0.9.0] - 2026-02-20

- Added in all rules
- Created ruleset variations for Platform 6
- Ruleset Profiles: used to disable/enable rules from the master rule list
- Added requirement checks for some Extended Validation checks
- Various bug, security, and performance fixes

---

## [0.8.3] - 2026-02-18

- Refactored capture JSON hierarchy for better organization
- Removed duplicate processing from capture engine `run_capture()`
- Removed some leftover debug print statements
- Fixed issues with dispatch exception handling
- Fixed issue with `_resolve_remote_path()` not using the cached values
- Changed unix-like file permissions check to use `posix` instead of `stat`
- Added HTML escaping for `_render_outdated_item()`
- Fixed missing memory and platform arch system info details
- Finalized Reporting and Diff HTML templates

---

## [0.8.2] - 2026-02-18

- Removed unused argparse flags
- Added report filtering and column selection to the report HTML templates
- Added `skip_rules` to the config to allow skipping specific rule(s) if needed
- Added Gateway5 collector and most of the Gateway5 rules to the ruleset
- Added a manual data collector for Platform Architecture, can be bypassed with `--skip-architecture`
- Added a manual data collector for modules that fail (users can provide the config files manually)

---

## [0.8.0] - 2026-02-13

- Modified how the deployment setup works; now each system can have a primary node for each

---

## [0.8.0] - 2026-02-12

- Additional bugfixes with mongo collector and capture UI

---

## [0.7.6] - 2026-02-12

- More rules added
- First test of multiple different rulesets
- Mongo HA rules added

---

## [0.7.5] - 2026-02-12

- More rules added
- Redis ACL checks

---

## [0.7.4] - 2026-02-11

- Added a new operator for `string_list`: `empty`
- Added better SSH key handling for nodes
- Added better logging for SSH connectivity
- Adjusted `parse_version()` operator to handle semver with leading strings better
- Added more rules into the `p6-production` ruleset

---

## [0.7.1] - 2026-02-10

- Added a guided manual data collector for edge cases where remote connections aren't feasible

---

## [0.7] - 2026-02-09

- Reworked the reports for a more modern aesthetic, both in dark and light mode
- Added more extended reporting sections
- Added a topology section for handling different types of Platform deployments (standalone, HA2, etc.)
- Updated init setup to handle the new topology settings
- More bugfixes and security fixes
- Syntax fixes

---

## [0.6.1] - 2026-02-07

- Bug and security fixes
- Performance improvements
- Added a context manager for centralized management

---

## [0.6] - 2026-02-06

- Added a theme switcher and a few themes
- Added a dark mode HTML template
- Added the ability to export in CSV or JSON

---

## [0.5.1] - 2026-02-06

- Switched back to using Parquet due to security risks with Python pickle data files
- Bug and security fixes
- Refactored some validation engine code
- Added additional configuration functions for the CLI
- Added new CLI arguments for customer importing and management of data
- Added CSS/JS collapsible sections for Extended Validation
- Modified CSS of template

---

## [0.4] - 2026-02-04

- Added session manager for handling the flow processing
- Updated argparse for better command handling

---

## [0.3.3] - 2026-02-03

- Updated theme to a more consistent style
- Updated most of the code to use the theme class
- Added the ability to disable extended validation if needed

---

## [0.3.2] - 2026-02-03

- Added adapter version checks
- Updated HTML report with adapter version checks
- Added Extended Validation to be able to add as many new sections to the HTML report
- Fixed a few bugs in the HTML template so the resulting HTML file is now W3-compliant

---

## [0.3] - 2026-02-02

- Added multiple different security checks in `transport()`
- Lots of bug fixes

---

## [0.2.1] - 2026-02-02

- Added a transport layer with `paramiko` to allow for remote SSH connections

---

## [0.2.1] - 2026-02-01

- Added preflight checks with `--preflight`
- Updated the schema for `alt_path` values for alternative JSON paths
- Alt-paths allow for trying to locate the same data from different sources
- Added progress bar and live data output on the capture engine UI
- Added rule validation progress UI for the validation engine
- Added a theming system for UI dark/light modes

---

## [0.2] - 2026-01-31

- Refactored some code to remove redundant functions
- Added the ability to specify specific modules to run with `--modules`
- Updated the HTML template to include an obelisk mark to indicate the score may not be valid when only a limited set of modules has run
- Updated the capture process to use `Rich Live()` with a more structured output
- Captured errors and warnings to display nicely during the capture process
