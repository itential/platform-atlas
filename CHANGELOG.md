# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.6] - 2026-04-09

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
