# Changelog — Platform Atlas (CLI / core)

All notable changes to the `platform-atlas` package are documented here.
WebUI changes ship in a separate wheel (`platform-atlas-webui`) and are
documented in [`webui/CHANGELOG.md`](webui/CHANGELOG.md).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.8.1] - 2026-06-05

### Security

- **Path-traversal hardening in `ruleset update`** — ruleset IDs read from the remote manifest are now validated against a strict allowlist (letters, digits, hyphen, underscore) before being used to build any file path, so a tampered or compromised manifest can no longer cause a downloaded file to be written or read outside `~/.atlas/rulesets/` (Snyk CWE-23). The manifest's `download_url` is additionally pinned to `https://`, blocking redirection of the fetch to a `file://` or other local-scheme URL.
- **DOM-XSS hardening in the architecture HTML form** — Gateway cluster server/runner counts restored from browser storage are now coerced to digits-only before being rendered, so a stored value can no longer break out of the input's `value` attribute (Snyk CWE-79).

### Added

- **Updated Dev Profiles** - The Dev profiles now disable PLAT-001, PLAT-028, PLAT-047, and RDS-003 as these don't apply to Production environments
- **`config edit` command** — interactively tune individual settings without hand-editing config.json: manual input mode (browser/terminal), log retention, validation depth, and connection/request timeouts. Timeouts pick from safe bounded sets — MongoDB aggregation (1/5/10/15/30 min, killed server-side the instant it's hit), SSH connect (10–60s), Platform API (15–90s), Redis (5–45s). Every option defaults to current behavior, so nothing changes unless you opt in.
- **Next-step hints** after a passing `preflight` (→ run capture) and in `session show` (→ the session's next stage), matching the rest of the CLI.

### Changed

- **Pre-capture summary now shows the tier** (Standard / Extended) so you can see whether SSH/Mongo/Redis collectors will run before you confirm.
- **Friendlier errors** — known errors show a short message plus a fix hint; full technical detail and tracebacks go to the log or `--debug`, instead of "Something went wrong" or a raw stack trace.

### Fixed

- **`config doctor`** now points to the real command (`env switch`, not the non-existent `env use`).
- **Empty-state hints** show `session create <name>` (was a truncated `<n>`).
- **Clearer MongoDB timeout errors** — a hit aggregation timeout now reports the exact limit it exceeded instead of a generic failure (its dedicated handler was previously shadowed by a broader `except`).

## [1.8.0] - 2026-06-01

### Added

- **`ruleset update` command** — fetches a versioned manifest from GitHub, compares available ruleset versions against what is installed, and downloads compatible updates. Downloads are SHA-256 verified and written atomically; a declined update is remembered in `~/.atlas/.ruleset_update_available.json` so the dashboard can surface a notice. The manifest URL is hardcoded and not user-configurable.
- **Ruleset update notice on the CLI dashboard** — a slim info panel appears above the footer when a ruleset update has been checked but not yet applied. Disappears automatically once `ruleset update` is run.
- **Per-environment rule suppression** (`ruleset skip-rule` / `ruleset unskip-rule`) — adds a rule number to the active environment's `skip_rules` list. Suppressed rules run but appear as **Suppressed** (amber) in reports and the WebUI ruleset table, making user intent visible. Merges additively with global `skip_rules`; clearing per-env `skip_rules` restores full validation without touching the global config. A required justification reason (min 10 chars) is collected via `--reason` or an interactive prompt; the reason is stored with the entry and surfaced inline in the compliance report (under the Suppressed badge in the table and in the rule detail modal). Only rule numbers that exist in the active ruleset are accepted.
- **Resume interrupted captures** — after a Ctrl-C or connection drop mid-capture, re-running `session run capture` prompts to resume from where it left off. Each collector result is checkpointed atomically to `00_checkpoint.json` inside the session directory; the checkpoint is cleared on successful completion.
- **`support-bundle` command** — collects a diagnostic ZIP for triage. Standard tier: five Platform health API endpoints + redacted Atlas config. Extended tier: above + SSH-based platform/webserver/MongoDB logs + system info. The interactive wizard collects the ticket number, an optional description, and (Extended only) the log window — days of logs to collect (1–30, default 7) — so the value no longer has to be remembered as a flag. Before any data is gathered, a credential pre-flight verifies the active backend (HashiCorp Vault or OS keyring) can actually produce the Platform OAuth secret both tiers depend on; if it can't — Vault unreachable, authentication failed, or the secret missing from the backend — the bundle is aborted with a fix hint instead of written empty. Output: `atlas-support-bundle-<timestamp>.zip` in the current directory (or `--output PATH`). Flags: `--log-days N` (Extended; overrides the prompt, clamped to 1–30), `--output`, `--yes`.
- **Architecture discovery warnings** — computed from the existing per-environment architecture data collected during capture. Warns on cross-datacenter latency (IAP, MongoDB, Redis, Gateway nodes in different DCs), single-node availability risk (no HA, no replica set, no standby), and cloud cross-region MongoDB. Rendered in both the standalone architecture report and the WebUI `/architecture` page.
- **ControlMaster socket preflight before capture** — when the active environment topology contains ControlMaster nodes, Atlas verifies each socket file exists before capture starts. If any socket is missing, capture is blocked immediately (including in `--headless` mode) and an error is printed with the exact `ssh -M -S ...` command to open each missing session.
- **ControlMaster session reminder panel** — in interactive mode, when ControlMaster nodes are present and all sockets are confirmed open, a warning-tinted hint panel appears before the "Ready to start capture?" prompt listing each node and its socket path. Ensures the user is aware the sessions must remain open for the duration of capture.
- **Expanded Kubernetes capture** — `KubernetesCollector` now collects eleven additional fields from `values.yaml` and `kubectl` for use in compliance rules: `startup_probe_enabled`, `liveness_probe_timeout_seconds`, `readiness_probe_timeout_seconds`, `startup_probe_timeout_seconds`, `liveness_probe_initial_delay_seconds`, `readiness_probe_initial_delay_seconds`, `startup_probe_failure_threshold`, `cpu_requests_set`, `cpu_limits_set`, `memory_requests_set`, `memory_limits_set`. When kubectl is available, `_enhance_system_with_kubectl()` also computes `max_restart_count` (highest pod restart count across all Platform pods), `hpa_enabled` / `hpa_min_replicas` (via `kubectl get hpa`), `node_count` / `node_instance_types` (node info), and `deployment_kind` (Deployment / StatefulSet).
- **11 new Kubernetes compliance rules** — KBS-005 through KBS-015 added to the P6 Master Ruleset (total: 119 rules). KBS-001–004 enabled (previously disabled). New rules:
  - **KBS-005 — Startup Probe Enabled** (warning): startup probe prevents liveness probe from killing slow-starting Platform pods during initialization.
  - **KBS-006 — Liveness Probe Timeout** (warning): `livenessProbe.timeoutSeconds` must be ≤ 10s; evaluates against the Kubernetes default (1s) when not set in values.yaml.
  - **KBS-007 — Readiness Probe Timeout** (warning): `readinessProbe.timeoutSeconds` must be ≤ 10s; same default-value behavior as KBS-006.
  - **KBS-008 — Startup Probe Failure Threshold** (warning): `startupProbe.failureThreshold` must be ≥ 10 so the combined startup window (`failureThreshold × periodSeconds`) is large enough for Platform to initialize.
  - **KBS-009 — CPU Requests Defined** (warning): `resources.requests.cpu` must be set so the scheduler can make informed pod placement decisions.
  - **KBS-010 — CPU Limits Defined** (warning): `resources.limits.cpu` must be set to prevent a runaway Platform process from starving other workloads.
  - **KBS-011 — Memory Requests Defined** (warning): `resources.requests.memory` must be set to prevent placement on memory-constrained nodes.
  - **KBS-012 — Memory Limits Defined** (high): `resources.limits.memory` must be set; without it a memory leak will eventually trigger the node OOM killer.
  - **KBS-013 — Pod Restart Count** (high, kubectl-only): maximum restart count across all Platform pods must be ≤ 5; higher counts indicate crash-looping.
  - **KBS-014 — HPA Enabled** (warning, kubectl-only): a Horizontal Pod Autoscaler must be configured so Platform scales automatically under load.
  - **KBS-015 — HPA Minimum Replicas** (warning, kubectl-only): HPA `minReplicas` must be ≥ 2 to maintain redundancy at minimum load.

### Added (Windows 11 compatibility)

- **Windows 11 support** — Atlas now runs on Windows 11 workstations with no code changes required. All POSIX-only constructs are guarded:
  - `stdout`/`stderr` reconfigured to UTF-8 at startup (`main.py`) so em-dashes, box-drawing characters, and checkmarks render correctly on Windows consoles (cp1252/cp850 default).
  - `fcntl` file-locking replaced with platform-aware helpers in `session_manager.py` and `continuous/storage.py` — no-ops on Windows, full `flock` semantics on POSIX.
  - `os.fchmod()` calls guarded with `if os.name == "posix":` in `session_manager.py`, `ruleset_manager.py`, and `handlers/session.py` (not available on Windows; `chmod` is DEGRADED but not crash).
  - `os.O_CLOEXEC` replaced with `_O_CLOEXEC = 0` stub in `session_manager.py` for the `os.open()` lock-file path.
  - `webbrowser.open()` calls in `whats_new.py` and `handlers/session.py` switched from `f"file://{path.absolute()}"` to `path.as_uri()` so `file:///C:/...` URLs are correctly formed on Windows.
  - Keyring config file (`keyringrc.cfg`) written to `%APPDATA%/Python/` on Windows, `~/.local/share/python_keyring/` on Linux/macOS.
  - `init_setup.py` socket path defaults to `tempfile.gettempdir()` instead of hardcoded `/tmp/`.
  - `read_text()` calls in `main.py` now pass `encoding="utf-8"` — prevents silent data corruption when `config.json` contains non-ASCII characters on non-UTF-8 Windows locales.
  - Unguarded `os.chmod()` calls in reporting renderers and `continuous/` atomic-write helpers now skip on Windows (`if os.name == "posix":`) — consistent with the existing guards in core modules.
  - SSH agent setup probe now works on Windows — queries `ssh-add -l` directly instead of requiring `SSH_AUTH_SOCK` (Windows OpenSSH uses a named pipe, not a Unix socket).
  - `ControlMasterTransport` raises a clear error on Windows instead of failing opaquely at connect time (OpenSSH for Windows does not implement the `-M`/`-S` multiplexing protocol).

### Fixed

- **ControlMaster transport label in capture progress** — `system`, `gateway4`, `gateway5`, `mongo_logs`, and `filesystem` modules running on a ControlMaster node now display **CONTROL_MASTER** in the capture progress panel instead of **SSH**. The `_TRANSPORT_BOUND` override logic previously did not preserve the `control_master` label the same way it preserved `local`, causing these modules to fall through to the `COLLECTOR_TRANSPORT` default of `"ssh"`.
- **`session run all` no longer continues after declining capture** — if the user presses Enter (or selects No) at the "Ready to start capture?" prompt, validate and report no longer run. Previously the declined prompt returned exit code 0, which `run all` treated as success.
- **`config doctor` credential backend display** — when an environment uses HashiCorp Vault, the doctor command now shows two rows: **OS Keyring backend** (e.g. "macOS Keychain — stores Vault URL and token") and **Credential backend** ("HashiCorp Vault — secrets stored in Vault KV"). Previously it always showed only the OS keyring name regardless of the configured backend, which was misleading for Vault users.
- **XSS in HTML reports** — plain-string items in extended-check `outdated` lists and nested-dict section headers were rendered without escaping; now wrapped with `html.escape`.
- **`ruleset update` SHA-256 bypass** — a manifest entry with no `sha256` field previously allowed unsigned downloads to be installed silently; now rejected with an error.
- **`ruleset update` partial-success clears dashboard notice** — the pending-update notice was cleared as soon as any ruleset succeeded, hiding remaining failures; now only cleared when every ruleset in the batch updated successfully.
- **`ruleset update` unbounded download** — manifest and ruleset fetches had no size cap; now capped at 512 KB and 10 MB respectively.
- **`ruleset skip-rule --reason ''` falls through to interactive prompt** — an empty or whitespace-only `--reason` flag now fails immediately with a clear error instead of triggering an interactive prompt in headless contexts.
- **`ruleset skip-rule` bare `except` swallows ruleset load errors** — a malformed or unreadable ruleset silently accepted any rule number; exceptions are now logged and the `rule_number` field absence is guarded.
- **`session prune` Ctrl-C causes silent session deletion** — pressing Ctrl-C at the "Delete all N sessions?" confirmation returned `None` (falsy), bypassing the cancellation branch and proceeding to delete; now raises `KeyboardInterrupt`.
- **Suppressed rules break `depends_on` logic** — rules with `depends_on: {when_status: "SKIP"}` were incorrectly satisfied by a user-suppressed parent (both stored `status: "SKIP"`); `should_execute_rule` now checks `user_suppressed` before the status comparison.
- **ControlMaster socket preflight accepts non-socket files** — `Path.exists()` was used for socket verification, passing for regular files and directories; now also checks `stat.S_ISSOCK` on POSIX.
- **ControlMaster `_read_direct`/`_read_sudo` skip 10 MB size limit when `stat` fails** — if `stat` exited non-zero (e.g. busybox stat, missing binary), the size guard was skipped entirely and unbounded file reads proceeded; now raises `CollectorError` on stat failure, matching `SSHTransport` behavior. Non-numeric `stat` output no longer raises a bare `ValueError`.
- **Architecture discovery warnings crash on non-numeric instance counts** — bare `int()` casts on user-supplied fields (`active_instance_count`, `standby_instance_count`, `replica_count`, `redis_node_count`) raised `ValueError` for blank or `N/A` values; replaced with a `_safe_int()` helper.
- **False-positive MongoDB "no replica set" warning** — a MongoDB section marked `present: False` triggered a critical availability warning; warning now skipped when `present` is explicitly false. Gateway4/5 latency warnings also tightened to require `present: True` rather than merely `present is not False`.
- **Windows session lock non-exclusive** — `_flock_acquire_nb` was a no-op on Windows, allowing two concurrent Atlas processes to both acquire the lock and race on session files; replaced with `msvcrt.locking`-based byte-range locking.
- **Capture checkpoint file world-readable** — `00_checkpoint.json` was written via `mkstemp` without an explicit `fchmod(0o600)`, unlike every other atomic-write path; permissions are now set consistently.
- **Manual capture stores wrong `modules_ran`** — `list(captured_data.keys())` was evaluated after `reshape_capture()` replaced the flat collector keys with nested top-level keys; now captured before the reshape call.

---

## [1.7.3] - 2026-05-15

### Added

- **Environment banner tint** — optional `env_tint` field (`low` / `medium` / `high`) on environment overlays tints the banner border and capture status panel (green / amber / pink). Configurable in `env create` and `env edit`. Plain/compatibility mode uses `[DEV]` / `[STAGE]` / `[PROD]` prefix instead.
- **`session prune`** rework — `--older-than DURATION` (`30d`, `7d`, `1w`, `24h`, …), `--keep-last N`, `--status`, `--env` filters (ANDed). Dry-run by default; `--no-dry-run` to delete with confirmation. Shows name, env, age, status, and size; prompts to show all sessions when more than 10 are truncated. Active session always excluded.
- **`config doctor --json`** — emits structured JSON (`atlas-doctor/v1` schema) to stdout; exit codes unchanged (`0` / `1` / `2`).
- **`config doctor --no-url-probes`** — skips TCP reachability checks for CI/offline use.
- **`config doctor` spinner** — shown while probing Platform and Gateway4 URLs; suppressed with `--json` or `--no-url-probes`.
- **`core/shutdown.py`** — cooperative shutdown registry used by the SIGINT handler (`register_cleanup`, `request_shutdown`, `shutdown_requested`, `run_cleanups`).
- **MongoDB/Redis connectivity test in `env create`** — after entering each URI, Atlas probes the connection directly. On failure: re-enter, skip test and save anyway, clear and continue without, or cancel. Notes that SSH-tunnelled hosts are expected to fail the direct test.
- **URI credential redaction** — MongoDB and Redis URIs in the `env create` review screen show `scheme://•••:•••@host:port/db` instead of exposing credentials.

### Changed

- **Plain-mode glyph fallbacks** — `--plain` mode now maps all status glyphs (`●○✓✘◌⚠`) and pipeline stage markers (`◉◯`) to ASCII equivalents (`* o OK X - ! * o`). Dashboard pipeline connectors (`━━━`) render as `---`. Previously these were passed through unchanged and appeared as `?` on terminals that don't support the characters.
- **Truncated error messages show log path** — When the capture status footer has errors or warnings, a dim hint line `Full details: ~/.atlas/atlas.log` is appended. The hardcoded panel height constraint on the footer panel was removed to accommodate the extra line.
- **`session list` status column** — Status values now render with theme colors (green for `validated`/`reported`, blue for `capturing`, yellow for `validating`/`aborted`, red for `failed`, dim for `created`). The previous `style="yellow"` column default was removed since inline markup handles all cases. `aborted` added to the color map.
- **Next-step chips after all terminal session commands** — `session run report` now shows an `Audit Complete — View Dashboard` chip on completion. `env create` now shows an `Environment Ready` chip pointing to `session create <name>`.
- **Duration formatting standardized** — Module durations and capture elapsed time now use a consistent `Xm Ys` / `Xs` / `Xms` format. Previously displayed as raw `1234ms` or `83.4s` with inconsistent precision.
- **Validation summary panel** — `session run validate` now prints a styled `Validation Results` panel (pass / fail / skip counts + compliance %) instead of a plain text line, consistent with the existing report score panel.
- **`session create`** validates name (`[a-z0-9-]`, 3–64 chars, no leading digit or trailing hyphen) before prompting. Interactive: shows a cleaned suggestion with rename / use-suggestion / cancel. Non-interactive: exits 1 immediately.
- **`session create` collision handling** — offers timestamp suffix, replace-with-backup (`<name>.bak-<HHMM>`), or cancel. No silent overwrite.
- `SessionStatus` gains `ABORTED = "aborted"`; `SessionManager` gains `set_status()`.
- `--older-than` duration grammar extended with `1d`, `7d`, `30d`.

### Performance

- **Faster startup** — `pandas`, all CLI handler modules, and the continuous-audit banner module are no longer imported on every invocation. Dashboard load time reduced by ~50%.

### Fixed

- **Graceful Ctrl-C during capture** — SIGINT stops the capture loop, lets in-flight modules finish, releases the Rich Live panel, marks the session `aborted`, saves partial capture JSON, and restores the cursor. Exit code `130`. Second Ctrl-C within 2s force-exits.
- `handle_errors` now exits `130` (was `1`) on `KeyboardInterrupt`.
- **`ChainerBackend` falsely flagged as unencrypted on macOS** — `verify_keyring_backend()` now inspects the chain. If it wraps a native OS keyring (`macOS.Keyring`, `WinVaultKeyring`, `SecretService`), it is classified as secure and the display name reflects the actual backend (e.g. "macOS Keychain") instead of "ChainerBackend".

## [1.7.2] - 2026-05-12

### Added

- Kubernetes environment setup now verifies the `kubectl` binary during configuration. If `kubectl` is not found in PATH, the CLI prompts for the full binary path (with up to three retries) and validates it before continuing. If kubectl is unavailable and no values.yaml is provided, the setup warns that no data source is configured and suggests using a non-Kubernetes environment instead. The WebUI equivalent checks the binary via an inline status indicator when the Kubernetes section is enabled.
- Standard Mode first-time setup wizard now includes a **"Select credential backend"** prompt (OS Keyring or HashiCorp Vault), matching the existing Extended Mode flow. Selecting Vault skips the local secret prompts, runs a connection test, shows the expected Vault key layout (`platform_client_secret`, `gateway4_password`), and verifies the required secret is present before continuing.
- **Plain/compatibility mode** (`--plain` flag, or `platform-atlas config plain` to toggle). Designed for terminals that don't support Rich formatting — disables all ANSI color codes, replaces Unicode box-drawing characters with ASCII equivalents (`+`, `-`, `|`), suppresses emojis, and disables syntax highlighting. Pass `--plain` once and it persists to config automatically; the flag is never needed again.
- `keyrings.alt` and `pycryptodome` are now bundled as core dependencies. On Linux/headless systems without gnome-keyring, the wheel automatically provides `CryptFileKeyring` (AES-encrypted file) as a fallback — no manual `pip install` required.
- Unencrypted keyring backend (`PlaintextKeyring`, `ChainerBackend`) now shows a warning and continues instead of hard-blocking setup and capture. Completely non-functional backends (`NullKeyring`, `FailKeyring`) still block with an error. Preflight downgrades this to a `WARN` rather than `FAIL`.
- `ChainerBackend` is now probed with a real write/read on startup. If it fails (e.g. SecretService requires a GUI unlock that isn't available), Atlas automatically switches to `CryptFileKeyring` so credentials can still be stored without the user hitting a cryptic error mid-setup.
- Simplified the Standard Mode CLI initial setup: removed the duplicate keyring check, moved "Verify SSL" to global settings (Phase 1), replaced the credential backend section with a single yes/no Vault prompt, removed the environment description prompt, and dropped the hardcoded rule count from the success panel.
- **`platform-atlas config doctor`** — new one-shot health-check command that verifies the global config, Python interpreter (version + binary path), available disk space, active environment, credential backend, Platform/Gateway URL reachability, active ruleset, and SSH key path in a single pass. Exits non-zero on warnings or failures so it composes cleanly with shell scripts.
- Platform OAuth credentials are now tested in the wizard immediately after they're entered — wrong client ID/secret surfaces in a few seconds, not 45 seconds into the first capture. Failed handshakes offer re-enter / re-enter-everything / skip-anyway / cancel.
- New "Same as IAP" shortcut in the split-standalone wizard so co-located MongoDB / Redis hosts only need to be typed once.
- Editable topology review: after the wizard summary, the user can fix a single node's hostname or change capture scope without restarting the whole flow.
- Redesigned the first-run welcome screen — punchy minimal hero: the Atlas wordmark, a personal greeting in place of the tagline (`Good evening, <user> — let's get you set up.`), three value-prop bullets, and a single-line system status (`System ready: ● Python 3.x  ● Keyring encrypted  ● N GB free`) with status-colored dots. All probe checks complete in well under a second and nothing requires network access.
- The end-of-setup panel is now a checklist (global config, environment, credential backend, Platform OAuth, tier, gateway4) with the next command to run, replacing the previous terse "saved" message.
- SSH-agent option in the key picker now probes the agent (`ssh-add -l`) and warns when no identities are loaded, so users don't pick "skip — use ssh-agent" only to fail later with a cryptic paramiko error.
- ControlMaster default socket path moved from `/tmp/` to `~/.atlas/sockets/` (chmod 0700) — keeps the socket out of a world-writable directory.
- Topology review now includes a per-node ControlMaster table (socket path + SSH destination) when any node uses CM transport.

### Fixed

- Encrypted file keyring (`keyrings.alt`) now surfaces an actionable error when the user enters an incorrect keyring password, instead of crashing with `UNEXPECTED ERROR: ValueError: Incorrect Password`. The error message instructs the user to re-run with the correct password or delete the keyring file to start fresh.
- Hardened the initial setup wizard: cancellation is handled consistently across every prompt, partially completed setups can be resumed on the next run, and configuration is now written in an order that prevents orphaned credentials or half-saved environments if setup is interrupted.
- Prompts that advertised a default (`Gateway4 Username`, etc.) now actually honor pressing Enter — previously they were validated as required-non-empty and silently rejected the default they suggested.
- MongoDB / Redis URI prompts now validate the scheme (`mongodb://`, `redis://`) instead of accepting any well-formed URI — pasting an HTTPS URL by mistake is caught in the wizard rather than during capture.
- The K8s Helm values.yaml prompt now confirms the file actually parses as a YAML mapping before accepting it; previously any existing file passed validation.
- HA2 MongoDB even-count warning fires once and trusts the next answer instead of looping silently if the user re-enters an even count.
- The deployment-topology review no longer wipes everything when the user picks "doesn't look right" — they can edit a single node or capture scope in place. The full restart option is still available.
- Platform, Gateway4, and Vault URL prompts (in both `config init` and `env edit`) now require a valid `http://` or `https://` scheme and a real hostname. The previous generic URI regex accepted typo schemes such as `htttp://`, `https:/`, and `https:///` that would later fail with confusing connection errors during capture.
- Kubernetes capture now operates correctly with any combination of `values.yaml` and `kubectl` — either source alone is sufficient, and both are used together when available. Previously, capture would fail if `values.yaml` was not configured regardless of kubectl availability.

### Changed

- Support contact updated across the README, user guides, and HTML guides to the official Itential support documentation: <https://docs.itential.com/itential-platform/resources/get-support>.

## [1.7.1] - 2026-05-11

### Fixed

- `tier set` now correctly persists when the active environment has its own `tier` field. Previously, the environment overlay (which takes precedence over `config.json` at load time) would silently overwrite the updated value on every subsequent invocation, making the command appear to succeed while the effective tier remained unchanged. The fix propagates the new tier to the environment file when the active environment carries an explicit `tier` override.
- `env edit` Gateway4 setup prompts now correctly raise `KeyboardInterrupt` on Ctrl-C. Previously, pressing Ctrl-C at any of the Gateway4 URI, username, or password prompts would silently skip that field and continue to the next prompt, trapping the user mid-flow.
- `config init` "Create another environment?" loop now correctly raises `KeyboardInterrupt` on Ctrl-C. Previously, `if not add_more:` treated a `None` return from questionary as `True`, causing the loop to break silently rather than exit with the standard interrupt signal.

## [1.7.0] - 2026-05-03

### Added

- **Fleet dashboard** — multi-environment compliance overview from local cache (read-only, never triggers captures): `platform-atlas fleet status` (with `--json`) showing per-env tier, last session age, pass rate, continuous-audit state, and unacked alerts
- **Outbound drift notifications** — Slack incoming webhooks and generic JSON webhooks (with optional HMAC-SHA256 signing via `X-Atlas-Signature`). Per-environment channels persisted on the env overlay, fired only on alert-state transitions (new alerts and re-opened acked alerts) so persistent unacked drift doesn't spam every cycle. CLI: `continuous-audit notify add|list|remove|test`
- **Continuous-audit robustness pass** — fcntl-locked atomic appends + 10MB rotation on `events.ndjson`; centralized atomic-write helper used across runs, status, and alerts; `prune_runs` now repoints `latest.json` if the pointed-at run was pruned; `make_run_id` gains a microsecond + nonce suffix to prevent collisions; drift comparator handles unhashable list items, cross-type coercion, and treats `True != 1` at every nesting level; `previous_unreadable` flag surfaced in run reports and the heartbeat when prior runs exist on disk but can't be read; endpoint planner warns on malformed/unmapped `platform.*` paths; macOS launchd install confirms plist-on-disk before bootstrapping
- **Standard / Extended tier system** — Atlas now ships with two distinct audit modes:
  - **Standard** — Platform OAuth + optional IAG4 API (~55 rules). No SSH, MongoDB, or Redis required. Designed for quick application-layer audits or environments where infrastructure access is restricted.
  - **Extended** — Full infrastructure audit via SSH, MongoDB, Redis, Kubernetes, and Gateways (~108 rules). The default for all installs upgraded from 1.6.x.
- **Tier CLI commands** — `tier show`, `tier set [standard|extended]`, `tier upgrade`, `tier downgrade` for managing the active tier interactively or non-interactively
- **`--tier` global flag** — Override the active tier for a single command without changing the persisted setting
- Sessions now bind a tier at creation time (alongside environment, ruleset, and profile)
- Cross-tier session diffs are flagged with a notice banner in the diff report
- Three-layer tier enforcement: module registry pruning, `require_extended()` collector guards, and tier-aware credential store (Extended-only keys silently return `None` in Standard and raise on write)
- **Continuous Audit** — scheduled drift monitor that re-runs a Platform-OAuth-only capture against the active ruleset and surfaces changed observed values as alerts:
  - Per-environment enable/disable; requires a successful `run-once` test before enabling
  - OS-level scheduling installed on enable so runs survive process restarts: `systemctl --user` timer on Linux, `launchctl` agent on macOS; in-process scheduler defers when an OS scheduler is active
  - Endpoint set pruned to only what the active ruleset references; capture is locked to Platform OAuth regardless of tier
  - Bare JSON report written to `~/.atlas/continuous/<env>/runs/<run_id>.json` for external alert systems; per-rule `previous → current` drift attached inline
  - Append-only `events.ndjson` timeline + `alerts.json` aggregate state with ack / ack-all; acked alerts re-open if drift recurs
  - CLI: `continuous-audit run-once|status|alerts|ack|enable|disable`; banner printed at the top of every invocation while enabled
- **Continuous-audit alert policy + watchlist** — `alert_policy` (`any` default, `regression` to alert only on PASS→FAIL) and a rule-number `watchlist` filter applied at alert/notification time; full drift history still goes to `events.ndjson`. CLI: `continuous-audit policy <any|regression>` and `continuous-audit watch add|remove|list|clear`
- **What's New page** — On first run after upgrading, Atlas shows a version-specific upgrade summary in the terminal and opens a detailed HTML page in the browser
- **Kubernetes kubectl rule fallbacks** — 13 rules in the P6 Master Ruleset now have an `alt_path` that Atlas uses when the primary data source (Platform OAuth API) is unavailable, enabling fuller coverage on Kubernetes-only deployments where SSH, MongoDB, and Redis are not accessible:
  - **10 rules via pod `printenv`** (`ITENTIAL_*` env vars → `platform.config_file.*`): Platform Default User, Platform Core Logging Level, Server ID, Mongo Auth Enabled, Mongo TLS Enabled, Log Max Files, Log File Max Size, Webserver HTTPS Enabled, Webserver HTTP Enabled, Webserver Timeout
  - **3 rules via kubectl system data** (`system.kubernetes.*`): Platform Version (from `release_metadata.json`), Node Version (from `node --version`), Gateway Manager Version Check (from `app-ag_manager/package.json`)
- **Node.js version collection** — `KubernetesCollector` now runs `kubectl exec <pod> -- node --version` during system info enhancement and stores the result at `system.kubernetes.node_version`, making the Node Version rule evaluable on Kubernetes without the Platform OAuth API
- **kubectl debug logging** — every `kubectl` command now logs the full invocation, exit code, elapsed time, and any stderr to `~/.atlas/atlas.log` at DEBUG level; higher-level collection phases (preflight probe, pod search, system enhancement, platform version, service collection, kubectl env) each emit a phase-entry log line
- **ControlMaster SSH transport** — new `control_master` transport mode lets Atlas piggyback on an existing OpenSSH ControlMaster session with zero credentials. Designed for environments where direct SSH key access is not available — most notably CyberArk PSMP (Privileged Session Manager Proxy) deployments where all privileged SSH is routed through a PAM gateway and target credentials are vault-managed. The user opens one master connection per target node before running Atlas (`ssh -M -S <socket> -N <destination>`); Atlas multiplexes on those sessions with no MFA interaction, no key configuration, and no knowledge of the PAM mechanism. Full parity with SSHTransport: remote path validation, symlink rejection, allowed-prefix enforcement, size cap, and a passwordless-sudo fallback for files owned by root. Selected interactively during topology setup — the existing SSH (recommended) and Local options are unchanged.
- **Local transport for Platform server** (Extended mode) — the topology setup wizard now offers a `Local` option when configuring the Platform (IAP) node. When selected, Atlas reads config files and runs system commands directly via the local filesystem instead of SSH — intended for environments where Atlas is installed on the Platform server itself to bypass restrictive SSH access. All other nodes (MongoDB, Redis, IAG) remain SSH-connected. Never the default; SSH is still recommended.
- **PLAT-048 — Template Builder Execution Timeout** — new rule that checks the `templateExecutionTimeout` setting on the Template Builder application (`@itential/app-template_builder`). Marks Non-Compliant when the value is present and exceeds 10000ms; skipped automatically when Template Builder is not installed or the setting is absent.

### Changed

- Fresh installs default to **Standard** tier; upgrades from 1.6.x default to **Extended** (preserving existing behavior)
- Standard mode reports do not show the partial-capture obelisk (†) — a limited module set is the full expected capture in Standard, not a deficiency
- `--customer` CLI flag removed (was deprecated in 1.6.4)

### Fixed

- Capture job reporting `SUCCEEDED` when no modules ran — target initialization errors (e.g. missing credentials) were silently swallowed in `_resolve_modules`; errors are now surfaced and the job correctly fails
- Validation `modules_ran` filter never fired — was reading `metadata.modules_ran` instead of `_atlas.metadata.modules_ran`, so rules for non-captured categories were evaluated and produced misleading SKIP messages
- Cross-tier diff banner never showed — `_rehydrate_attrs` did not restore `df.attrs["tier"]` from the capture JSON; both sides defaulted to "extended"
- Capture file could be left half-written on SIGINT / disk-full — capture and parquet writes now use `tempfile + os.replace` for atomicity
- Validation crashed on list items with non-string `name` (e.g. `None`, integers) — values are now coerced to `str` before path matching
- `SSHRetryConfig` was defined but unused — `SSHTransport` now accepts a `retry=` argument and retries `OSError`s only (auth and protocol errors still fail fast)
- Concurrent `engine.run_once` invocations from the in-process scheduler, OS timer (systemd / launchd), and CLI no longer race — per-env `flock` serializes the OAuth fetch, drift detection, alert state update, and `latest.json` swap
- `alerts.json` read-modify-write is now flock'd — concurrent ack / ack-all operations no longer lose transitions
- systemd unit files now double-quote env names — environments with spaces (e.g. `Acme Prod`) no longer produce a broken `ExecStart` or `Environment=` line
- Notification dispatch caps events per payload (25 webhook / 10 Slack) and honors HTTP 429 `Retry-After` (capped at 30 s) — large drift bursts no longer stall the engine on a slow receiver
- `runtime._write_raw` switched to `tempfile.mkstemp` — concurrent env-overlay writes can no longer clobber each other's tempfile
- `events.ndjson` rotation rewritten with `collections.deque(maxlen=N)` — O(1) eviction in place of the previous O(n²) `list.pop(0)` loop
- `latest.json` symlink target now verified to resolve under `runs/` before being followed
- Notification dispatch error logs scrub URL and bare-IP substrings before logging — Slack webhook URLs and internal hostnames in receiver responses no longer reach the audit log
- systemd timer no longer uses `Persistent=true` and adds `RandomizedDelaySec=120` — long-downtime reboots no longer fire a burst of catch-up runs
- Corrupt `alerts.json` is renamed to `alerts.json.corrupt-<ts>` before the empty state takes over — historical alert data is recoverable instead of silently overwritten
- Environment edit crashed with `'tuple' object has no attribute 'get'` — `ask_deployment()` returns a `(mode, k8s_meta)` tuple; both callers in `env.py` / `config.py` now destructure it and persist the k8s metadata
- Standard / Extended init wizard double-prompted for `organization_name` on existing installs — now silently inherits from caller or `~/.atlas/config.json`
- Architecture HTML form did not pre-fill `organization_name` — `html_collector` now passes `?org=…` and the form reapplies it (and the saved `org-`/`legacy-` payload) on load
- MTU "Other" had no free-text path — added `mtu_size_other` to the form, schema, and CLI prompt; reports render the custom value
- `platform_logs` collection failed silently when logs lived outside the default path — new `log_path_override` config field; capture engine retries the collector with the override after a failed first pass
- Report filter pills showed mismatched counts when the active rule filter excluded rows — `allStats` now counts all rows and pill recount uses a shared `countBuckets()` helper

### Vault credential backend improvements

- **Token TTL introspection** — after any Vault auth method succeeds, Atlas calls `lookup_self()` to capture the token's remaining TTL and renewability; surfaced in the CLI wizard and `config credentials`
- **Automatic token refresh** — `VaultBackend` transparently re-authenticates when the token has less than 5 minutes remaining, with no user action required:
  - `APPROLE` — calls `login()` again with the stored `role_id` and `secret_id`; supports dynamic short-lived tokens (1h, 24h TTL) set on the Vault role by the admin
  - `TOKEN_FILE` — re-reads the Vault Agent sink file for a fresh token
  - `TOKEN_ENV` — re-reads `VAULT_TOKEN` from the environment
  - `TOKEN` (renewable) — calls `renew_self()`
  - `APPROLE_WRAPPED` / non-renewable `TOKEN` — raises a clear error; these cannot be automatically refreshed
- Thread-safe refresh via double-checked locking — concurrent callers cannot stampede Vault during a refresh
- `revoke_token()` method on `VaultBackend` for explicit cleanup at session end
- `TOKEN` auth now raises immediately at connect time if the token has fewer than 60 seconds remaining

### Security

- Continuous-audit notifications now ship rule identity only (number, name, severity, alert ID); previous/current drift values stay local so a misconfigured Slack/webhook channel cannot exfiltrate captured Platform secrets
- Webhook URLs blocked from pointing at private, loopback, link-local, or cloud-metadata addresses (`127.0.0.0/8`, RFC1918, `169.254.0.0/16`, etc.); DNS-resolved at validation time so domains that resolve to private space are also rejected; opt-out via `ATLAS_ALLOW_PRIVATE_WEBHOOKS=1`
- Slack webhook URLs, HMAC signing secrets, and custom headers now persist in the OS keyring under `platform-atlas/<env>` instead of plaintext env JSON; legacy channels migrate transparently on first read
- Path-traversal defense in the continuous-audit storage and runtime layers — env names containing `..`, `/`, or `\\` are rejected before constructing any path under `~/.atlas/continuous/` or `~/.atlas/environments/`
- `~/.atlas/continuous/**` files (run reports, `alerts.json`, `events.ndjson`, `status.json`) and env overlay JSON files are now written with mode `0o600`

### Performance

- Capture collectors now run in parallel across targets (capped at 8 worker threads) — multi-node topologies where each target was previously waited on serially see roughly N× wall-clock improvement
- SSH file reads use the SFTP `lstat` size directly instead of running a separate `stat -c %s` exec channel — saves ~1 round trip per file (~50–150 ms on high-RTT links)

### Dependencies

- `paramiko` updated to 5.0.0
- `rich-argparse` updated to 1.8.0
- `rich` updated to 15.0.0
- `pyarrow` updated to 24.0.0
- `packaging` updated to 26.2
- `urllib3` updated to 2.7.0

---

## [1.6.4] - 2026-05-01

### Added

- **HashiCorp Vault AppRole (Wrapped) authentication** — New `approle_wrapped` auth method for deployments that use response-wrapped secret IDs. Instead of storing a static secret_id, Atlas stores a one-time-use wrapping token that Vault unwraps at connect time to retrieve the actual secret_id, which is then used for a standard AppRole login. The wrapping token is consumed on first use and expires after its configured TTL. Useful when a pipeline or Vault admin generates a fresh wrapped secret_id on a schedule and wants Atlas to consume it without storing the raw credential.
- **HashiCorp Vault Token (file) authentication** — New `token_file` auth method for deployments running Vault Agent on the same host as Atlas. Vault Agent authenticates to Vault independently, renews the token automatically, and writes it to a file (a "sink"). Atlas reads that file at connect time — no credentials are stored in the keyring beyond the file path, and token rotation is fully transparent. The configured path is stored in the OS keyring under `vault_token_file_path`.
- **HashiCorp Vault Token (env) authentication** — New `token_env` auth method for pipeline and orchestrated environments. Atlas reads the `VAULT_TOKEN` environment variable at runtime instead of loading a token from the keyring. The pipeline or orchestrator (systemd, CI, Ansible, etc.) is responsible for injecting a valid token before Atlas runs. No token value is stored in Atlas at all.
- Auth method selector in `config credentials` and `config init` now groups choices into **Standard** (`token`, `approle`) and **Automated / rotating credentials** (`approle_wrapped`, `token_file`, `token_env`) with visual separators, so existing users are not exposed to the new options unless they are looking for them.
- Added JSON schema for Report JSON files
- Added new theme `Dracula` to Platform Atlas CLI

### Fixed

- `NameError` crash in manual capture mode (`--manual`) — `log_since_str` and `log_until_str` were only initialized inside the automated capture branch but referenced in the shared post-capture path, leaving the session stuck in `CAPTURING` status on every manual mode run
- `credential_store()` silently falling back to keyring when `credential_backend` in `config.json` holds an invalid value — `ValueError` from `CredentialBackendType()` was swallowed by a bare `except Exception`; it now logs a `warning` so the misconfiguration is visible instead of silently using the wrong backend
- Vault `_read_all()` silently returning an empty dict on token expiry or any non-connection read failure — collectors would see `None` credentials and fail silently with no user-visible error; now raises `CredentialError` so the failure surfaces at the collector level with a clear message; also caches the Vault KV read per session, eliminating redundant round-trips on every `get()` / `exists()` call during preflight and capture
- `_find_iap_pod()` falling through to an unlabeled `kubectl get pods` query when no IAP-labeled pod matched — the first pod in the namespace would be returned regardless of type (e.g., a database or metrics pod), causing subsequent `kubectl exec` calls to silently target the wrong container; the fallback label is now removed and the command always includes a `-l` selector
- `collect_kubectl_env()` confirmation prompt now explicitly states that captured `ITENTIAL_*` environment variables include credential values (MongoDB URI, Redis URI, client secret), and directs users to `session export --redact` for safe sharing
- Fixed the `diff.html` template to match existing `report.html` CSS styles and colors

### Deprecated

- Disabled `--customer` CLI flag. This was an older feature never fully implemented, will remove all code for this in 1.7 or higher

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
