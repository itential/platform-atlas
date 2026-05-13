# Platform Atlas — User Guide

Welcome to Platform Atlas. This guide walks you through everything you need to get up and running, from installation through generating your first health report. If you get stuck, check the FAQ section at the end — most common issues are covered there.

---

## What is Platform Atlas?

Platform Atlas is a configuration auditing tool for the Itential Automation Platform. It connects to your IAP environment, collects configuration data from Platform, MongoDB, Redis, and Automation Gateway, then validates that data against a set of best-practice rules and generates a professional HTML report.

The typical workflow looks like this:

    Install → Configure → Create Environment → Preflight → Create Session → Capture → Validate → Report

Each step builds on the previous one. Once you've completed the initial setup, day-to-day usage is just two commands: create a session (which binds your environment, ruleset, and profile in one step) and run it. If you work with multiple deployments or organizations, switching between them is a single `platform-atlas session switch` command that restores the full context.

---

## What's New in v1.7

v1.7 is the largest release since v1.5. The headline changes are below; each one has its own
section later in the guide.

### Standard and Extended tiers

Platform Atlas now ships with two distinct audit modes. You choose the tier once — it applies
globally until you change it.

**Standard** collects data only from the Platform API (OAuth) and the Automation Gateway 4 API.
No SSH, no MongoDB, no Redis. Setup takes about five minutes. If you have a Platform URI and
OAuth credentials, you're ready to run. The report covers ~54 rules focused on the application
layer.

**Extended** is the full infrastructure audit that existed in previous versions — SSH into every
server, collect MongoDB and Redis configuration, run all collectors. ~107 rules, full coverage.

Fresh installs default to Standard. Upgrades from 1.6.x default to Extended (nothing changes
for you unless you explicitly switch).

```bash
platform-atlas tier show
platform-atlas tier set standard
platform-atlas tier set extended
```

A `--tier` global flag overrides the active tier for one command without touching the persisted
setting (e.g. `platform-atlas --tier standard preflight`). Sessions bind their tier at
creation time, so cross-tier comparisons are flagged in `session diff`.

### Continuous Audit

Atlas can now monitor an environment between formal audits. The `continuous-audit` family of
commands re-runs a Platform-OAuth-only capture against the active ruleset on a schedule and
records any rule whose observed value drifts from the prior run as an **alert**. Alerts persist
until you ack them, OS-level scheduling (systemd-user / launchd) keeps runs going across
reboots, and you can pipe alert transitions out to Slack or generic webhooks. See *Continuous
Audit* below.

### Fleet dashboard

`platform-atlas fleet status` (and `/fleet` in the WebUI) reads the local cache to show every
configured environment side-by-side: tier, last session age, pass rate, continuous-audit state,
and any unacked alerts. Read-only — it never triggers a capture. See *Fleet*.

### ControlMaster and Local transports

Two new options for environments where direct SSH key-based access to the Platform server isn't
possible:

- **ControlMaster** — Atlas multiplexes through an OpenSSH ControlMaster session you open by
  hand, so privileged-access gateways like CyberArk PSMP authenticate once with their normal
  MFA flow and Atlas runs without ever holding the underlying credentials.
- **Local** — when Atlas is installed *on* the Platform server, it can collect IAP data through
  the local filesystem instead of SSH. MongoDB, Redis, and IAG nodes still use SSH.

See `SSH_SETUP_GUIDE.md` for both.

### What's New page

After upgrading, the first run of `platform-atlas` prints a short upgrade summary in the
terminal and opens a detailed HTML page in your browser. Suppress the page with
`--no-whats-new`; reopen it later with `platform-atlas whats-new`.

### Vault credential improvements

`VaultBackend` now introspects the token's TTL on every connect, refreshes it transparently
when fewer than 5 minutes remain, and surfaces the remaining time in `config credentials`.
AppRole, Token (file), Token (env), and renewable Token auth all refresh without user action;
AppRole-Wrapped and non-renewable Token raise a clear error pointing at the rotation step.

### Optional WebUI

A browser-based interface ships as a separate optional wheel.

```bash
pip install platform_atlas_webui-1.7.0-py3-none-any.whl
platform-atlas-webui
```

It self-signs a TLS certificate, binds to your OS user, and opens your default browser at a
one-time login URL. It shares `~/.atlas/` with the CLI — no separate setup. See *WebUI* below
for the full walkthrough including daemon mode, themes, and the security model.

---

## What's New in v1.5

If you're upgrading from an earlier version of Platform Atlas, here are the key changes in v1.5:

### Sessions are now the primary unit of work

Previously, environments, rulesets, profiles, and sessions were all managed independently. You had to remember to switch each one separately, and it was easy to accidentally run a capture against the wrong environment or validate with the wrong ruleset.

Starting in v1.5, **sessions bind everything together**. When you create a session, you select an environment, ruleset, and profile — and those choices are locked into the session. When you switch sessions, everything switches with it. One command, full context restored.

### Organization name lives on environments

The organization name is no longer just a global setting. Each environment now carries its own `organization_name` field, which makes it easy to audit multiple customers without editing config files between runs. The global `organization_name` in `config.json` serves as a default for new environments.

### Session edit (before capture)

Made a mistake during session creation? Use `platform-atlas session edit` to change the environment, ruleset, or profile — as long as capture hasn't started yet. Once capture begins, the session is locked to prevent inconsistent data.

### Report metadata improvements

JSON and Markdown report exports now include the environment name in the metadata block. The organization name is also correctly preserved across the capture → validation → report pipeline (fixing a bug in earlier versions where it could show as "Unknown" in some report formats).

### Backward compatibility

All existing sessions, environments, and configurations continue to work without changes. Sessions created before v1.5 simply won't have bound environments or rulesets — they'll use whatever is globally active, matching the old behavior. You can upgrade and continue working immediately.

---

## Installation

Platform Atlas is distributed as a Python wheel file. You'll need Python 3.11 or later installed on the machine where you plan to run it.

### Install from a wheel file

Your team lead or Itential contact will provide one or two `.whl` files. The core CLI is
required; the WebUI is optional.

```bash
# Required — core CLI
pip install platform_atlas-1.7.0-py3-none-any.whl

# Optional — browser-based interface
pip install platform_atlas_webui-1.7.0-py3-none-any.whl
```

Once installed, the `platform-atlas` command is available in your terminal. Verify it works:

```bash
platform-atlas --version
```

You should see something like `platform-atlas 1.7.0`.

### A note about your system

Platform Atlas stores its configuration and session data in a folder called `~/.atlas/` in your home directory. This folder is created automatically the first time you run the tool. You don't need to create it yourself.

If you're running on a Linux server (like RHEL), you'll also need a credential store backend. macOS and Windows handle this automatically. See the *Credential Storage* section below for Linux-specific instructions.

---

## First-Time Setup

The first time you run `platform-atlas`, it detects that no configuration exists and launches an interactive setup wizard. You can also run it manually at any time:

```bash
platform-atlas config init
```

The wizard has two phases: global settings that apply across all environments, then creating your first named environment. Here's what to expect at each step.

### Phase 1 — Global Settings

You'll be asked for settings that apply to all environments:

- **Organization Name** — Your company or team name. This appears on reports and serves as the default when creating new environments. Each environment can override this with its own organization name (useful if you audit multiple customers).

These are saved to `~/.atlas/config.json`.

### Phase 2 — First Environment

After global settings, the wizard immediately walks you through creating your first environment. An environment represents one IAP deployment — for example, "production" or "dev". The environment file is saved to `~/.atlas/environments/<n>.json`.

You'll be asked for a name, an organization name (defaults from global config), and an optional description. Then you're walked through three sections:

#### Credential Storage

Atlas needs to store sensitive values like your Platform client secret and database URIs. It never stores these in plain text on disk. Instead, it uses one of two backends:

**OS Keyring** (default) — Uses your operating system's built-in credential store. Credentials are scoped per environment (stored under `platform-atlas/<env-name>` in the keyring), so each environment has fully isolated secrets.

- macOS: Keychain (built-in, no extra setup)
- Windows: Credential Locker (built-in, no extra setup)
- Linux: Requires `gnome-keyring` with D-Bus, or the `keyrings.alt` package for headless/server environments

> **Updating or rotating credentials later:** Use `platform-atlas config credentials` at any time to add, change, or rotate any stored credential without recreating the environment. This is the right command whenever a credential is rotated upstream, when retrofitting a credential that wasn't collected during the original setup (e.g. a Gateway4 password on an existing env), or when populating the keyring on a new machine after restoring `~/.atlas/` from backup. Credentials live outside `~/.atlas/`, so they don't travel with the config directory.

**HashiCorp Vault** — If your organization manages secrets in Vault, Atlas can read credentials from a KV v2 secrets engine. In this mode, Atlas only *reads* from Vault — it never writes secrets. Your Vault administrator manages the actual credentials. Atlas supports several authentication methods: a static token, AppRole (role_id + secret_id), and three automated options designed for environments where credentials rotate — see the *Vault Integration* section in the FAQ for details on choosing the right one.

#### Connection Credentials

You'll be prompted for the credentials Atlas needs to connect to this environment:

- **Platform URI** — The URL of your IAP instance (for example, `https://iap.yourcompany.com:3443`).
- **Platform Client ID** — The OAuth2 client ID for API access.
- **Platform Client Secret** — The OAuth2 secret that pairs with the Client ID. This is entered as a hidden field.
- **MongoDB URI** — The full connection string for your MongoDB instance. You can skip this if MongoDB auditing isn't needed.
- **Redis URI** — The full connection string for your Redis instance. You can skip this if Redis auditing isn't needed.

All of these are stored in your OS keyring (scoped to the environment name) or Vault — never in config files.

#### Deployment Topology

Atlas needs to know how this environment's IAP deployment is set up so it knows which servers to connect to and what collectors to run. The wizard asks you to pick a deployment mode:

**Standalone** — A single-server deployment where IAP, MongoDB, and Redis all run on one machine (or are split across a few machines, but with one instance of each).

**HA2** — A highly available setup with multiple IAP nodes, a MongoDB replica set (typically 3 members), and Redis Sentinel (typically 3 members). You'll be asked for the hostname or IP of each server.

**Kubernetes** — IAP is deployed via the Itential Helm chart. Atlas reads `values.yaml` directly and uses `kubectl` for system info, log tail, and service-status checks. No SSH involved.

**Custom** — A free-form layout where you manually assign roles and modules to each node.

For each server, the wizard asks how Atlas should connect to it. The default is **SSH** (key-based, recommended). When configuring the **Platform (IAP) server** specifically, you'll also see two alternatives:

- **ControlMaster** — Pick this when direct SSH to the IAP server is not possible (CyberArk PSMP, jump hosts that require MFA, etc.). You open one `ssh -M` master session by hand before running Atlas, and Atlas multiplexes through that socket. The wizard asks for the socket path and the full SSH destination string. MongoDB and Redis nodes still use direct SSH.
- **Local** — Pick this when Atlas itself is installed on the IAP server. Atlas reads config files and runs system commands through the local filesystem instead of SSH. MongoDB, Redis, and Gateway nodes still use SSH.

For SSH targets, the wizard collects the username, key file, and port. Atlas uses these to read configuration files and run lightweight commands. The SSH user needs read access to config files in `/etc/` and `/opt/` — it does not need root access, though passwordless sudo is used as a fallback if a file can't be read directly. See `SSH_SETUP_GUIDE.md` for ControlMaster and Local transport details.

### Creating Additional Environments

At the end of setup, you'll be asked "Create another environment?" — if you have multiple deployments (dev, staging, production), you can set them all up in one session. You can also create environments later at any time:

```bash
platform-atlas env create
```

Or copy an existing environment and tweak it:

```bash
platform-atlas env create staging --from production
```

### After Setup

Once the wizard finishes, your global config is at `~/.atlas/config.json` and your environment file is at `~/.atlas/environments/<n>.json`. You can review them at any time:

```bash
platform-atlas config show
platform-atlas env show
```

Sensitive values are masked by default. If you need to see the actual values (for troubleshooting), add `--full`:

```bash
platform-atlas config show --full
```

---

## Choosing a Tier

Before you run your first audit, decide which tier fits your environment.

### Standard tier

Use Standard if:
- You only need to audit the Platform application layer
- You don't have (or don't want to provide) SSH access to the servers
- You want a fast setup — just a Platform URI and OAuth credentials

Standard runs capture entirely over HTTPS using Platform OAuth and (optionally) the IAG4 REST
API. No SSH keys, no MongoDB URI, no Redis URI needed.

To set Standard tier:

```bash
platform-atlas tier set standard
```

### Extended tier

Use Extended if:
- You need full infrastructure coverage (MongoDB, Redis, config files, system info)
- You are conducting a formal quarterly health assessment
- You already have SSH access and database credentials set up

Extended is the default for upgrades from 1.6.x.

To set Extended tier:

```bash
platform-atlas tier set extended
```

### Switching tiers

You can switch tiers at any time. Existing sessions are not affected — each session stores the
tier it was created with. New sessions will use the current global tier.

```bash
platform-atlas tier show        # see the current tier
platform-atlas tier upgrade     # guided Standard → Extended with explanations
platform-atlas tier downgrade   # guided Extended → Standard with explanations
```

---

## WebUI

The optional WebUI package (`platform-atlas-webui` 1.0.0) is a browser-based interface for
managing sessions, running audits, viewing reports, and operating Continuous Audit and the
fleet view. It's a thin presentation layer over the same engines the CLI uses — there is no
duplicated logic. Both interfaces read and write the same `~/.atlas/` directory; what you do
in one shows up immediately in the other.

The WebUI is **local-only** — it serves on `localhost` over self-signed TLS and authenticates
the OS user that started it. It is not a multi-tenant remote console. You run it on the same
machine as your `~/.atlas/`.

### Installing and first launch

```bash
pip install platform_atlas_webui-1.7.0-py3-none-any.whl
platform-atlas-webui
```

On first launch you'll see something like:

```
[atlas-webui] Self-signed TLS certificate written to ~/.atlas/.webui-cert.pem
[atlas-webui] Certificate fingerprint (SHA-256): 7a:33:…
[atlas-webui] Listening on https://127.0.0.1:8765
[atlas-webui] One-time login URL: https://127.0.0.1:8765/auth?nonce=…
```

Atlas opens that URL in your default browser automatically. The first request consumes the
nonce and sets a signed session cookie; subsequent navigation just works.

If you've never set Atlas up before, the WebUI redirects to `/setup` and walks you through
the same wizard you'd see in `platform-atlas config init`. Existing CLI installs skip
straight to the dashboard.

### Daemon mode

For a long-running install — typically on a workstation that should always have the WebUI
available — run it detached:

```bash
platform-atlas-webui --daemon         # double-fork, write PID to ~/.atlas/webui.pid
platform-atlas-webui status            # is it running? on which port? since when?
platform-atlas-webui restart           # rotate the process; sessions survive
platform-atlas-webui stop              # clean shutdown
```

In daemon mode the login URL is tucked into `~/.atlas/webui.log`. To mint a fresh URL after
a restart without grepping the log:

```bash
platform-atlas-webui login-url
```

Daemon mode supports Linux and macOS; on Windows, run the foreground command in a terminal you
keep open.

### Security model

| Layer | What it does |
|---|---|
| **TLS** | Self-signed certificate at `~/.atlas/.webui-cert.pem` (regenerate with `--reset-tls`). Browsers will warn about the certificate the first time — accept it for `localhost`. |
| **OS-user binding** | The WebUI reads a token file from `~/.atlas/.webui-token` (mode 0600). Only processes running as the same OS user can read it. Browser sessions are signed cookies derived from that token plus a separate cookie secret at `~/.atlas/.webui-cookie-secret`. Rotating either file invalidates every outstanding cookie (`--reset-token`). |
| **CSRF** | Stateless HMAC tokens injected into every form and required on AJAX `POST` / `PATCH` / `DELETE`. |
| **CSP & headers** | Strict response headers (CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, HSTS, `Referrer-Policy`). HTML reports themselves render under a sandboxed CSP — captured data cannot reach back into the WebUI's authenticated origin. |
| **Audit log** | Every state-changing request appends one JSON line to `~/.atlas/webui-audit.log` (rotates at 10 MB) — OS user, path, status, redacted form payload. |

### Page-by-page tour

When the dashboard loads, the left sidebar lists every page; the topbar shows the active
**ORG**, **ENV**, and **TIER** pills, plus a continuous-audit state pill and an alert bell
when there are unacked alerts.

| Page | What you do here |
|---|---|
| **Dashboard** (`/`) | KPI tiles and an audit-activity heatmap of the last 48 h. Empty tiles show a one-line teaching helper instead of a bare zero. |
| **Sessions** (`/sessions`) | Create, activate, run capture/validate/report, and delete sessions. Each running stage streams live output via Server-Sent Events; a force-kill button appears on jobs that run more than 60 s. |
| **Environments** (`/environments`) | Create, edit, activate, and delete environments. Activation atomically restores tier, ruleset, and profile alongside the active environment, matching the CLI. The form pane covers tier, deployment topology (including ControlMaster IAP transport), Kubernetes settings, and Gateway 4 connection details. |
| **Credentials** (`/credentials`) | Reconfigure the active environment's credential backend (Keyring or Vault). All five Vault auth methods are supported — Token, AppRole, AppRole (Wrapped), Token (file), Token (env). |
| **Reports** (`/reports`) | Direct links to every session's compliance, operational, and architecture HTML reports. |
| **Tier** (`/tier`) | Side-by-side cards for Standard and Extended with a "when to use" hint. The active tier is highlighted; switching opens a confirmation modal that explains what changes. |
| **Fleet** (`/fleet`) | The same data as `fleet status` rendered as a sortable card grid — tier, last session age, pass rate, continuous-audit state, unacked alerts. Read-only; never triggers a capture. |
| **Continuous** (`/continuous`) | Status, settings, and run history for the active environment's continuous audit. Buttons for run-now, enable, disable, and an Alerting block with `alert_policy` selector and a chip editor for the watchlist. |
| **Alerts** (`/alerts`) | Drift timeline with ack and ack-all. The bell icon in the topbar shows unacked count and links here. |
| **Notifications** (`/notifications`) | Add, edit, test, and remove Slack and webhook channels. HMAC signing toggle for webhooks. |
| **Settings** (`/settings`) | Theme picker (Aurora and Horizon palettes with mini-palette previews), light/dark mode segmented control, and the editable scope hints next to each fieldset. |

### Themes — Aurora and Horizon

Theme palette and light/dark mode are independent axes:

- **Aurora** — confident, technical (deep navy + electric blue). Default.
- **Horizon** — warm, editorial (charcoal + terracotta).

The moon/sun toggle in the topbar swaps light/dark and preserves your palette choice. Settings
persist in `config.json` under `webui_theme`, `webui_accent`, and `webui_mode`. Legacy values
from earlier dev builds (`cyan`, `amber`, `violet`, `lime`, `mono`) migrate transparently the
first time a recent build reads them.

### Notes

- The WebUI must be on the same machine as your `~/.atlas/` directory. It does not support remote Atlas installations.
- Run only one capture per session at a time, regardless of which interface starts it. CLI and WebUI write to the same session directory.
- Tier, environment, ruleset, and profile changes made in the WebUI are immediately reflected in the CLI and vice versa.
- The WebUI exposes everything the CLI does for day-to-day operations. Setup wizards that don't fit a web form (custom topology, manual capture import) are still CLI-only — the WebUI tells you which command to run.

---

## Preflight Checks

Before running your first audit, it's a good idea to verify that Atlas can reach all the services in your environment. The preflight command tests connectivity to each configured target:

```bash
platform-atlas preflight
```

Preflight checks each service independently and reports results as pass, fail, warn, or skip:

- **Platform API** — Tests OAuth2 authentication against your Platform instance.
- **MongoDB** — Verifies the connection using your MongoDB URI.
- **Redis** — Connects and auto-detects whether it's a standalone Redis or Sentinel setup.
- **SSH targets** — Tests SSH connectivity to each server in your topology.
- **Config files** — Checks that configuration files (mongod.conf, redis.conf, etc.) exist and are readable on the target servers.
- **Gateway4 / Gateway5** — Checks for the Automation Gateway virtual environment or environment variables.

If anything fails, the output includes a description of what went wrong. Fix the issue and re-run preflight until everything passes. Common fixes include updating SSH keys, opening firewall ports, or correcting a URI in your credentials.

### Config doctor

For a broader configuration health check that goes beyond live-connectivity probing, use:

```bash
platform-atlas config doctor
```

`config doctor` is a one-shot diagnostic that verifies the global config, active environment, credential backend, Platform Client Secret, Platform / Gateway4 URL reachability, active ruleset, and SSH key path in a single pass. Each check prints a `✓ OK`, `⚠ warning`, or `✘ error` line plus a specific fix-it suggestion when something is wrong. Run it after `config init`, after editing an environment, or any time a capture fails for an unclear reason — it surfaces every issue at once instead of letting them appear one-by-one across multiple capture runs. Exits 0 when everything passes, 1 on warnings, 2 on failures, so it can drive CI gates.

---

## Rulesets and Profiles

Before you can validate captured data, you need a **ruleset** — the set of rules that Atlas checks your configuration against. Think of a ruleset as the "answer key" for what a healthy deployment should look like.

Starting in v1.5, you select a ruleset and profile when you create a session — they're bound to the session and switch automatically when you switch sessions. You don't need to manage them separately unless you want to inspect or compare rulesets outside of a session.

### Interactive setup (recommended)

The fastest way to get a ruleset and profile loaded is the interactive setup command:

```bash
platform-atlas ruleset setup
```

This walks you through two prompts: first you pick a ruleset, then you pick a profile. Your selection is saved and stays active until you change it. Note that if you create a session afterwards, the session will inherit whatever is currently active, so this is still a useful command for setting defaults before creating multiple sessions.

### Manual commands

If you prefer explicit control (or need non-interactive commands for scripts and CI), you can use the individual commands:

#### List available rulesets

```bash
platform-atlas ruleset list
```

This shows all rulesets that ship with Atlas. Each one has an ID and a description.

#### Load a ruleset

```bash
platform-atlas ruleset load <ruleset-id>
```

For example:

```bash
platform-atlas ruleset load p6-master-ruleset
```

The loaded ruleset stays active across sessions until you change it.

### Profiles

Profiles are optional overlays that customize a ruleset for specific environments. For example, a profile might relax certain rules for development environments or tighten them for production. If you used `platform-atlas ruleset setup`, you've already selected a profile.

To manage profiles individually:

```bash
# See available profiles
platform-atlas ruleset profile list

# Set a profile
platform-atlas ruleset profile set <profile-id>

# Check which profile is active
platform-atlas ruleset profile active

# Remove the profile overlay
platform-atlas ruleset profile clear
```

### Check what's active

```bash
# See the active ruleset
platform-atlas ruleset active

# See detailed info about a ruleset (rules count, categories, etc.)
platform-atlas ruleset info
```

---

## Running an Audit

An audit in Platform Atlas is organized into **sessions**. Each session represents a complete audit cycle: capture data, validate it against rules, and generate a report. Sessions keep everything organized and let you compare results over time.

### Step 1 — Create a session

```bash
platform-atlas session create prod-q1-2026
```

The session name should be descriptive — something like `prod-audit-march` or `staging-q1-2026`. Names must be 3-64 characters using letters, numbers, hyphens, and underscores.

When you create a session, Atlas walks you through three quick prompts to bind the session to its context:

1. **Select environment** — Pick which IAP deployment to audit. The list shows each environment's organization name and platform URI so you can easily tell them apart. If you need a new environment, there's a "Create new environment..." option right in the picker.
2. **Select ruleset** — Pick which set of validation rules to use. The list shows version and rule count.
3. **Select profile** — Pick a deployment profile overlay (e.g., standalone, HA2, HA2 with gateway). You can also choose "No profile" to use the ruleset as-is.

These bindings are locked into the session. When you switch between sessions later, the environment, ruleset, and profile switch with it — no more forgetting to change one of them.

You can also bypass the interactive prompts with flags:

```bash
platform-atlas session create prod-q1-2026 --env production --ruleset p6-master-ruleset --profile ha2-gateway
```

The session is automatically set as active after creation. You'll see a status summary showing the bound environment, ruleset, organization, and the next step to run.

You can add an optional description:

```bash
platform-atlas session create prod-q1-2026 --description "Q1 production health check"
```

### Step 2 — Capture

```bash
platform-atlas session run capture
```

This connects to every server in your deployment topology and collects configuration data. You'll see a live progress display showing each collector as it runs. The capture phase collects things like:

- System info (CPU, memory, disk, kernel version)
- MongoDB server status and database statistics
- Redis INFO, ACL rules, and Sentinel topology
- Platform API health, adapter configurations, application states
- Configuration files (mongod.conf, redis.conf, platform.properties)
- Platform log analysis (error/warning frequency)
- Webserver access log analysis
- Gateway packages and environment variables

If a collector fails (for example, because a config file is missing), Atlas will offer to let you provide the data manually through a guided prompt. You can skip this with `--skip-guided`.

#### Manual capture with batch import

If you can't connect Atlas directly to your infrastructure, you can collect the data files manually and import them all at once by pointing Atlas at the directory:

```bash
platform-atlas session run capture --manual --import-dir ~/atlas-capture/
```

Atlas matches files by name — no interactive prompts, no typing paths one at a time. It shows you exactly what it found and what's still missing. You can add more files to the directory and re-run the same command to fill in the gaps. See the `MANUAL-COLLECTION-GUIDE.md` for the full list of expected filenames and collection commands.

If you prefer the interactive walkthrough instead, use `--manual` without `--import-dir`:

### Step 3 — Validate

```bash
platform-atlas session run validate
```

This takes the captured data and checks it against every rule in your active ruleset. Rules cover things like:

- Is MongoDB's WiredTiger cache size set correctly for your memory?
- Are Redis persistence settings configured as recommended?
- Are all Platform adapters running and connected?
- Are healthcheck intervals within acceptable ranges?
- Are there excessive errors in the platform logs?

Each rule produces a PASS, FAIL, SKIP, or ERROR result. After primary validation, Atlas also runs a set of Extended Validation checks that analyze patterns across the full dataset (adapter versions, log error rates, ACL configurations, etc.).

### Step 4 — Report

```bash
platform-atlas session run report
```

This generates an HTML report from the validation results and opens it in your default browser. The report includes:

- An overall compliance score
- A breakdown by category (Redis, MongoDB, Platform)
- A detailed results table with every rule and its status
- Extended validation findings with remediation recommendations
- Platform log analysis with error group breakdowns

The report is saved inside your session directory at `~/.atlas/sessions/<name>/03_report.html`.

### Step 5 — Generate Reports

`session run report` generates all three HTML reports in a single pass:

- **`03_report.html`** — Compliance report: overall score, category breakdown, rule results table, and extended validation findings
- **`04_operational.html`** — Operational report: platform, webserver, and MongoDB log analysis, plus MongoDB aggregation pipeline results
- **`05_arch.html`** — Architecture & Maintenance report: adapter states, Redis ACL, index status, IAG paths, and architecture overview data

The compliance report opens automatically in your browser when generation completes. All three reports share a header navigation bar so you can switch between them without leaving your browser.

#### MongoDB Aggregation Pipelines prompt

After capture completes, Atlas will ask whether you want to run MongoDB aggregation pipelines for the operational report. These pipelines query live workflow and task data from your Platform’s MongoDB database (using the `mongo_uri` you have configured) and produce execution statistics, top workflows, and runtime metrics.

- If you **accept**, the pipeline results appear in `04_operational.html` above the log sections.
- If you **decline**, the operational report still generates with log analysis only — a clear notice appears where the pipeline results would be.

#### Custom pipelines

Atlas discovers pipeline definitions automatically from `~/.atlas/pipelines/`. Each pipeline is a JSON file that defines a MongoDB aggregation — a name, target collection, and the pipeline stages. Atlas ships with a default set, but you can add your own by dropping new JSON files into that directory. No code changes or configuration needed — they’re picked up on the next run.

See the bundled `topworkflows.json` for an example of the pipeline format.

### The shortcut — Run everything at once

If you want to run capture, validate, and report in one go:

```bash
platform-atlas session run all
```

This executes all three stages in sequence. If any stage fails, it stops and reports the error.

For automated or scripted runs (no interactive prompts), add `--headless`:

```bash
platform-atlas session run all --headless
```

This skips all confirmation prompts, guided fallbacks, and won't try to open the report in a browser.

---

## Managing Sessions

### Switch between sessions

Switching sessions restores the full context — the environment, ruleset, and profile that were bound when the session was created all switch automatically:

```bash
platform-atlas session switch
```

This opens an interactive picker showing all sessions with their environment and organization. You can also switch directly by name:

```bash
platform-atlas session switch prod-q1-2026
```

After switching, Atlas shows the session's current pipeline status and the next step to run.

### Edit session bindings

If you picked the wrong environment, ruleset, or profile during creation, you can change them — but only before capture begins. Once capture starts, the session is locked to its bindings:

```bash
platform-atlas session edit
```

This opens an interactive menu where you can change the organization name, environment, ruleset, or profile.

### List your sessions

```bash
platform-atlas session list
```

This shows all sessions with their environment, organization, status, creation date, and completion progress.

### View session details

```bash
platform-atlas session show prod-q1-2026
```

Or, if it's the active session, just:

```bash
platform-atlas session show
```

### Compare two sessions

If you've run audits at different times, you can generate a diff report showing what changed:

```bash
platform-atlas session diff baseline-session latest-session
```

This creates an HTML comparison report highlighting rules that improved, regressed, or stayed the same.

### Export a session

To package a session for sharing (for example, sending a report to your team):

```bash
platform-atlas session export prod-q1-2026
```

This creates a ZIP file containing the report, session metadata, and a README. By default, raw capture data is redacted from exports for security. If you need to include it, add `--no-redact`.

### Delete a session

```bash
platform-atlas session delete prod-q1-2026
```

You'll be asked to confirm. To skip the confirmation prompt, add `--force`.

---

## Continuous Audit

A formal session run is a point-in-time snapshot. Continuous Audit fills the gap between
formal runs by re-executing a Platform-OAuth-only capture against the active ruleset on a
schedule and recording any rule whose observed value drifted from the prior run as an alert.

The pieces:

- **Captures** are locked to Platform OAuth regardless of your tier. Continuous Audit never
  touches MongoDB, Redis, SSH, or kubectl — drift detection is fast and uses the Platform API
  exclusively, so it works in Standard and Extended without any extra access.
- **Endpoints** are pruned to only what the active ruleset actually references. If you switch
  rulesets, the endpoint set updates on the next run.
- **Reports** for each run are written to `~/.atlas/continuous/<env>/runs/<run_id>.json` —
  bare JSON for external alerting systems to scrape.
- **Alerts** are surfaced when a rule's observed value changes between runs. They persist in
  `alerts.json` until you ack them. Acked alerts re-open if drift recurs.
- **Events** stream into an append-only `events.ndjson` timeline (rotated at 10 MB).

### One-shot test

Before enabling the schedule, run one capture on demand to confirm the OAuth path works and
to seed the prior-run state:

```bash
platform-atlas continuous-audit run-once
```

This is required at least once before Atlas will let you enable the OS scheduler.

### Enabling the scheduler

```bash
platform-atlas continuous-audit enable
```

On Linux this installs a `systemctl --user` timer; on macOS it installs a `launchctl` user
agent. Both keep running across reboots. Atlas's in-process scheduler defers when an OS
scheduler is active, so you never get duplicate runs.

### Status, alerts, and ack

```bash
platform-atlas continuous-audit status      # last run, next scheduled run, alert counts
platform-atlas continuous-audit alerts      # list unacked alerts
platform-atlas continuous-audit ack <id>    # ack a single alert
platform-atlas continuous-audit ack --all   # ack everything
```

A banner at the top of every Atlas command shows whether continuous audit is enabled for the
active environment.

### Alert policy and watchlist

By default, every rule-state change becomes an alert (`alert_policy = any`). Switch to
regression-only — alert only when a rule transitions PASS → FAIL — when you only want to hear
about new problems:

```bash
platform-atlas continuous-audit policy regression
platform-atlas continuous-audit policy any        # back to default
```

The watchlist filters notifications and the alert list down to specific rule numbers.
Everything still goes to the events timeline, but only watched rules raise alerts and fire
notifications:

```bash
platform-atlas continuous-audit watch add 12 47
platform-atlas continuous-audit watch list
platform-atlas continuous-audit watch remove 12
platform-atlas continuous-audit watch clear
```

### Notifications

Per-environment outbound channels deliver alert-state transitions to Slack or any HTTP
receiver. Notifications fire only on **state transitions** — newly opened alerts and re-opened
alerts after ack — so a persistent unacked drift doesn't spam your channel every cycle.

```bash
platform-atlas continuous-audit notify add slack          # interactive: paste webhook URL
platform-atlas continuous-audit notify add webhook        # generic JSON receiver
platform-atlas continuous-audit notify list
platform-atlas continuous-audit notify test <channel-id>  # send a synthetic alert
platform-atlas continuous-audit notify remove <channel-id>
```

Webhooks support optional HMAC-SHA256 signing — Atlas signs the payload with a shared secret
and your receiver verifies the `X-Atlas-Signature` header. Slack URLs, signing secrets, and
custom headers are stored in the OS keyring under `platform-atlas/<env>`, never in the env
JSON.

> **Notification payloads carry rule identity only** — number, name, severity, alert ID. The
> previous and current observed values stay local. This means a misconfigured channel cannot
> exfiltrate captured Platform secrets like client IDs or token values.

### Disabling

```bash
platform-atlas continuous-audit disable
```

Removes the OS scheduler. Existing runs, alerts, and event history stay on disk — re-enabling
picks up where you left off.

---

## Fleet

The fleet command is a read-only overview of every environment Atlas knows about. It reads the
local cache only — it never triggers a capture, hits the Platform API, or opens an SSH
connection. Use it to see the current state of all environments at once:

```bash
platform-atlas fleet status
platform-atlas fleet status --json   # for piping into jq / monitoring
```

For each environment you'll see:

- Current tier (Standard / Extended)
- Most recent session and its age
- Pass rate from that session
- Continuous-audit state (disabled / enabled / running / failed) and last-run age
- Count of unacked alerts

The same data renders at `/fleet` in the WebUI as a sortable card grid.

---

## Updating Your Configuration

You don't need to re-run the full setup wizard to make changes. Atlas has targeted commands for common updates.

### Switch environments

If you have multiple environments configured, the easiest way to switch is through sessions — each session is bound to an environment, so `platform-atlas session switch` handles everything.

If you need to switch environments outside of a session context (for example, to run preflight checks), you can switch directly:

```bash
platform-atlas env switch staging
```

Or use the `--env` flag for a one-off command without changing the active environment:

```bash
platform-atlas --env dev preflight
```

### Manage environments

```bash
platform-atlas env list                             # See all environments (with org names)
platform-atlas env create                           # Create a new environment
platform-atlas env create staging --from production  # Copy and customize
platform-atlas env show                             # Details of active environment
platform-atlas env edit staging                     # Edit settings (org name, URIs, topology, etc.)
platform-atlas env remove dev                       # Delete an environment
```

### Update credentials

```bash
platform-atlas config credentials
```

This shows the status of all stored credentials (stored or missing) for the active environment and lets you update or delete individual ones through an interactive menu.

### Change your deployment topology

```bash
platform-atlas config deployment
```

This re-runs just the topology wizard. If an environment is active, it updates the environment file. Otherwise it updates `config.json` directly. Useful when servers are added or removed from your environment.

### Switch themes

Atlas supports multiple color themes for its terminal output:

```bash
platform-atlas config theme
```

Pick a theme from the interactive list. The change takes effect the next time you run a command.

---

## Frequently Asked Questions

### Setup and Installation

**Q: I get "command not found" after installing the wheel file.**

Make sure the Python `bin` or `Scripts` directory is in your system PATH. If you installed with `--user`, the location is typically `~/.local/bin` on Linux/macOS. Try running `python -m platform_atlas` as an alternative.

**Q: The setup wizard says "Insecure keyring backend detected."**

This happens on Linux servers without a graphical desktop environment. The `keyring` library can't find a secure credential store. You have two options:

1. Install the encrypted file backend: `pip install keyrings.alt` — this stores credentials in an encrypted file.
2. Use HashiCorp Vault as your credential backend instead of the OS keyring.

**Q: Can I run Atlas on my laptop and connect to remote servers?**

Yes. Atlas uses SSH to connect to your IAP servers. Configure your deployment topology with `transport: ssh` and provide the hostnames, SSH user, and key file. Atlas will SSH into each server to collect data. The Platform API, MongoDB, and Redis connections go over the network directly using their respective URIs.

### Preflight and Connectivity

**Q: Preflight says "SSH authentication failed."**

Double-check that your SSH key file path is correct and that the key is authorized on the target server. If your key is encrypted (password-protected), Atlas will detect this and suggest adding the passphrase to your credentials. Run `platform-atlas config credentials` to update it.

**Q: Preflight says "Config files not found" but the services are running.**

Some config file paths are different depending on your IAP version or installation method. The default paths Atlas checks are:

- MongoDB: `/etc/mongod.conf`
- Redis: `/etc/redis/redis.conf`
- Sentinel: `/etc/redis/sentinel.conf`
- Platform: `/etc/itential/platform.properties`
- Gateway4: `/etc/automation-gateway/properties.yml`

If your files are in different locations, the capture will still work for the services Atlas can reach through their APIs (Platform, MongoDB, Redis). The config file collectors will be skipped, and those rules will show as SKIP in the validation report.

**Q: Preflight shows "skip" for MongoDB or Redis.**

This means the URI for that service isn't configured. If you don't need to audit that service, this is fine. If you do, run `platform-atlas config credentials` and enter the URI.

### Capture and Validation

**Q: Some collectors failed during capture. Is my data incomplete?**

Partial captures are normal and expected. Atlas is designed to work with whatever data it can collect. Failed collectors are logged and the corresponding validation rules will show as SKIP in the report. The overall compliance score is calculated only against rules that had data to evaluate.

If a collector fails because of a permissions issue, Atlas will try again with `sudo` if your SSH user has passwordless sudo access. If that also fails, you'll be offered a guided prompt to provide the data manually (like pasting a config file).

If you have the data files already collected, you can skip the interactive prompts entirely by pointing Atlas at a directory:

```bash
platform-atlas session run capture --manual --import-dir ~/atlas-capture/
```

**Q: Validation says "No ruleset loaded."**

If you created your session with v1.5 or later, the ruleset should already be bound. Try switching to the session to restore its context:

```bash
platform-atlas session switch <session-name>
```

If you're using a session from an older version (before session binding), load a ruleset manually:

```bash
platform-atlas ruleset setup
```

**Q: Validation says "No profile set."**

Same as above — switch to the session to restore its bindings, or set a profile manually:

```bash
platform-atlas session switch <session-name>
```

Or manually:

```bash
platform-atlas ruleset profile list
platform-atlas ruleset profile set <profile-id>
```

**Q: What does the compliance score mean?**

The score is the percentage of evaluated rules that passed. Rules that were skipped (due to missing data) are not counted against the score. For example, if 40 out of 50 evaluated rules passed, the score is 80% — even if 20 other rules were skipped.

### Reports

**Q: The report didn't open in my browser.**

If you're running Atlas over SSH or on a headless server, there's no browser to open. The report is still saved as an HTML file. You can find it at:

```
~/.atlas/sessions/<session-name>/03_report.html
```

Copy this file to your local machine and open it in any browser. Or use `platform-atlas session export` to create a ZIP you can transfer.

**Q: Can I get the report in a format other than HTML?**

Yes. Use the `--format` flag when generating the report:

```bash
platform-atlas session run report --format csv
platform-atlas session run report --format json
platform-atlas session run report --format md
```

**Q: What is the operational report?**

The operational report (`04_operational.html`) is generated automatically every time you run `session run report` — alongside the compliance report (`03_report.html`) and the architecture report (`05_arch.html`). It contains log analysis for platform, webserver, and MongoDB logs, and optionally MongoDB aggregation pipeline results.

During capture, Atlas will prompt you to confirm whether to run MongoDB aggregation pipelines. If you accept, the pipeline results (top workflows, runtime statistics, task frequency) appear in the operational report above the log sections. If you decline, the report still generates with log analysis only. You can add custom pipeline JSON files to `~/.atlas/pipelines/` to extend the pipeline output with your own aggregations.

### Vault Integration

**Q: Which Vault authentication method should I use?**

It depends on whether your organization's security policy requires credentials to rotate. Atlas groups the options into two categories:

*Standard — for static credentials:*

- **Token** — Paste a Vault token with read access to the secrets path. Use this for simple setups where the token is long-lived or you rotate it manually.
- **AppRole** — Provide a `role_id` and `secret_id`. Use this for machine-to-machine authentication where the secret_id doesn't need to rotate automatically.

*Automated / rotating — for environments where credentials rotate:*

- **AppRole (Wrapped)** — Your pipeline or Vault admin generates a response-wrapped secret_id on a schedule and updates Atlas's keyring entry with the new wrapping token. Atlas unwraps it at connect time and uses it once. The token is consumed on first use, so a stolen keyring entry is useless after the first run.
- **Token (file)** — Vault Agent (a separate HashiCorp tool) runs as a service on the same host as Atlas, authenticates to Vault on its own, and writes a continuously-renewed token to a file. Atlas reads that file at runtime. Set this up once, then never think about it again — Vault Agent handles all rotation transparently.
- **Token (env)** — Set the `VAULT_TOKEN` environment variable before running Atlas. Your pipeline, systemd unit, or orchestrator is responsible for injecting a valid token. Nothing is stored in Atlas at all.

If your Vault admin or security team requires rotating credentials, the **Token (file)** option (using Vault Agent) is the most transparent — once the agent is configured, Atlas runs without any credential management on your part.

**Q: How do I switch from OS Keyring to Vault (or vice versa)?**

If you're using environments, create a new environment and select the desired backend during the wizard:

```bash
platform-atlas env create
```

For legacy setups without environments, re-run the setup wizard:

```bash
platform-atlas config init
```

During the credential storage step, select the backend you want. If switching to Vault, you'll need to provide the Vault URL and choose an authentication method. Your Atlas secrets must already exist in Vault at the configured path — Atlas only reads from Vault, it never writes.

**Q: My Vault token expired and Atlas won't connect.**

Run the credential update command:

```bash
platform-atlas config credentials
```

Atlas will detect the failed connection and offer to update the settings. If you are on the standard **Token** method, enter your new token. If you are on **AppRole**, re-enter your `role_id` and `secret_id`. If you are on **AppRole (Wrapped)**, paste a new wrapping token obtained from your pipeline or Vault admin.

If you find yourself doing this frequently, consider switching to **Token (file)** (Vault Agent) or **Token (env)** so rotation is handled automatically.

**Q: I'm using AppRole (Wrapped) and Atlas fails on the second run.**

This is expected. A wrapping token is a one-time-use credential — Atlas consumes it on the first connect. For subsequent runs, you need a fresh wrapping token in the keyring. This is typically handled by a pipeline or cron job that generates a new wrapped secret_id and updates the keyring entry before each Atlas run. If you want fully automatic rotation without any pipeline work, switch to **Token (file)** using Vault Agent instead.

**Q: How do I set up Vault Agent for the Token (file) method?**

Vault Agent is a separate binary distributed by HashiCorp alongside the Vault server. Your Vault admin would:

1. Download and install Vault Agent on the Atlas host.
2. Configure it with an `auto_auth` block (typically AppRole, with the role_id and secret_id stored securely by the agent).
3. Add a `sink` block pointing to a file path (e.g. `/run/vault-agent/atlas.token`) with the `type = "file"` sink.
4. Run Vault Agent as a systemd service so it starts at boot and stays running.

In Atlas, you select **Token (file)** as the auth method and provide that file path. From that point on, Atlas reads a valid token from the file every time it runs, and Vault Agent handles renewal in the background. No ongoing maintenance is required on the Atlas side.

### Environments

**Q: I have dev, staging, and production IAP deployments. Do I need separate Atlas installations?**

No. Create an environment for each deployment (each with its own organization name, credentials, and topology), then create sessions bound to each one:

```bash
platform-atlas env create              # walk through the wizard for each
platform-atlas session create prod-audit --env production --ruleset p6-master-ruleset --profile ha2-gateway
platform-atlas session run all
```

Switching between audits is one command:

```bash
platform-atlas session switch prod-audit   # restores environment, ruleset, and profile
```

**Q: Can I run a quick check against a different environment without switching?**

Yes. Use the `--env` flag on any command:

```bash
platform-atlas --env dev preflight
```

This overrides the active environment for just that command. Your active environment stays unchanged.

**Q: I upgraded from an older version that didn't have environments. Do I need to redo my setup?**

No. If you have an existing `config.json` with connection details, Atlas will continue to use it as-is. The environment system only activates when you explicitly create environments. You can migrate at your own pace by running `platform-atlas env create` to move your connection details into a named environment.

**Q: I upgraded to v1.5 and my existing sessions don't have environment or organization info.**

Sessions created before v1.5 don't have bound environments, rulesets, or organization names — those features were introduced in v1.5. Existing sessions continue to work as they did before. The `session list` output will show blank values in the Environment and Organization columns for older sessions.

If you want to backfill metadata for cosmetic purposes, you can hand-edit the `session.json` file inside `~/.atlas/sessions/<session-name>/` and add `organization_name`, `environment`, `ruleset_id`, and `ruleset_profile` fields. But this is purely cosmetic — it doesn't change the captured or validated data.

**Q: Where are my credentials stored when using environments?**

When using the OS keyring, credentials for each environment are stored under a scoped service name: `platform-atlas/<env-name>`. So your production credentials are completely isolated from your dev credentials. If you're using Vault, credentials come from your configured Vault path regardless of environment. Non-sensitive settings like organization name, platform URI, and topology are stored in the environment JSON file at `~/.atlas/environments/<n>.json`.

### General

**Q: Where does Atlas store its data?**

Everything lives under `~/.atlas/` in your home directory:

- `config.json` — Global configuration (default org name, theme, debug settings, WebUI appearance — no secrets)
- `settings.json` — Active ruleset and profile pointers
- `environments/` — One file per named deployment target (production.json, dev.json, etc.), each with its own org name, credentials backend, topology, tier, and notification channel index
- `sessions/` — One folder per audit session containing capture data, validation results, reports, and a `session.json` with the session's bound environment, ruleset, profile, and tier
- `continuous/` — Per-env continuous-audit state: `runs/<run_id>.json` per scheduled run, `alerts.json` (aggregate state), `events.ndjson` (append-only timeline), `status.json` (heartbeat). Files mode `0600`.
- `architecture-form.html` — Bundled architecture overview form (synced from the package on first use)
- `atlas.log` — Application log (rotated at 5 MB)
- `webui-audit.log`, `webui.log`, `webui.pid`, `.webui-cert.pem`, `.webui-token`, `.webui-cookie-secret` — WebUI runtime files (only present when the WebUI is installed)

**Q: How do I get debug output for troubleshooting?**

Add `--debug` to any command:

```bash
platform-atlas --debug session run capture
```

This enables verbose logging to both the console and the log file at `~/.atlas/atlas.log`.

**Q: Is it safe to run Atlas against a production system?**

Yes. Atlas is strictly read-only. It uses SSH to read config files and run informational commands (`uname`, `cat`, `stat`, etc.). It connects to MongoDB, Redis, and the Platform API using read-only operations. It never modifies configuration, restarts services, or writes data to your IAP environment.

**Q: I need to run Atlas on a schedule (cron).**

Use the `--headless` flag to suppress all interactive prompts:

```bash
platform-atlas session run all --headless --session scheduled-audit
```

For environments where Atlas cannot connect directly, combine `--manual` with `--import-dir` to batch-import pre-collected files without any prompts:

```bash
platform-atlas session run capture --manual --import-dir /data/atlas-capture/
```

Make sure your credential store works in non-interactive environments. On Linux, this typically means using `keyrings.alt` (encrypted file backend) or Vault instead of `gnome-keyring`, which requires a D-Bus session.

---

### Tiers

**Q: Which tier should I use?**

Start with **Standard** if you just have a Platform URI and OAuth credentials and want a quick
audit. It covers ~54 application-layer rules with zero server access needed. Switch to
**Extended** when you need full coverage — MongoDB config, Redis config, system info, SSH-based
checks, and ~107 rules total.

**Q: Can I switch tiers without breaking existing sessions?**

Yes. Sessions store the tier they were created with. Switching the global tier only affects new
sessions. Old sessions stay on their original tier, and diff reports flag cross-tier comparisons
with a notice banner.

**Q: I want to audit a Kubernetes-only deployment but I'm in Standard.**

The Standard ruleset covers the application layer using the Platform OAuth API only — that
works on Kubernetes deployments without any extra setup. If you need infrastructure rules too,
switch to Extended (`platform-atlas tier set extended`) and pick **Kubernetes** as the
deployment mode during topology setup. The Extended ruleset has 13 kubectl-based fallbacks
that close most of the gap from inaccessible MongoDB / Redis / SSH on K8s.

### Continuous Audit and Notifications

**Q: Does Continuous Audit work with both tiers?**

Yes. Continuous Audit captures via the Platform OAuth API regardless of tier, and validates
against your active ruleset. It will silently SKIP rules that need data outside the OAuth API
(MongoDB config, Redis config, SSH-based collectors). You'll see those skips in any normal
session report; the continuous-audit run reports list only the rules that actually had data.

**Q: My continuous-audit timer fires but the run reports are empty.**

Check `~/.atlas/atlas.log` first. If you see "endpoint planner: 0 endpoints to fetch" the
ruleset references no `platform.*` paths — usually because no ruleset is loaded for the active
environment. Run `platform-atlas ruleset setup` to fix that.

**Q: Slack or webhook notifications never fire even though I have unacked alerts.**

Notifications fire only on **state transitions** — newly opened alerts, or acked alerts that
re-open. A persistent unacked alert won't re-notify on every cycle by design. To force a test:
`platform-atlas continuous-audit notify test <channel-id>`.

**Q: Can I send a webhook to an internal Jenkins or Slack-on-prem URL?**

By default Atlas blocks webhook URLs that resolve to private, loopback, link-local, or
cloud-metadata addresses to prevent SSRF. Set `ATLAS_ALLOW_PRIVATE_WEBHOOKS=1` in the
environment that runs Atlas (and your scheduler unit) to opt out.

### ControlMaster and Local transports

**Q: When should I pick ControlMaster over plain SSH?**

When direct key-based SSH to the Platform server isn't available — typically because all SSH
goes through CyberArk PSMP or another PAM gateway that requires interactive MFA. Open one
master session by hand (which performs the MFA tap), then Atlas multiplexes through the
socket. Plain SSH is still recommended whenever it's possible — it's simpler.

**Q: Can I use ControlMaster for the MongoDB and Redis nodes too?**

Yes via the CLI wizard — the standalone and HA2 wizards apply your ControlMaster choice to
every node when you select it for IAP. The WebUI form, by design, only exposes the IAP
transport selector; on a no-op WebUI save the existing CM transport on Mongo/Redis is
preserved. To add or change CM on Mongo/Redis after the fact, use `platform-atlas config
deployment` from the CLI.

**Q: My Atlas runs are slow and the master keeps timing out mid-capture.**

Increase `ControlPersist` on the master command. The 10-minute default is comfortable for a
Standard capture but tight for a full Extended capture on a slow link:

```bash
ssh -M -S /tmp/atlas-cm.sock -o ControlPersist=30m -fN <user>@<target>@<gateway>
```

**Q: When should I use the Local transport?**

Only when Atlas itself is installed on the IAP server. It bypasses SSH entirely for the IAP
node — config files are read directly from the local filesystem. MongoDB, Redis, and IAG
nodes still need their normal SSH or protocol access. Avoid Local when SSH works — keeping
Atlas on a separate workstation is the cleaner deployment.

### WebUI

**Q: I installed the WebUI but the `platform-atlas-webui` command isn't found.**

Check that the Python `bin` or `Scripts` directory is in your PATH. If you installed into a
virtual environment, activate it first. Try `python -m platform_atlas_webui` as a fallback.

**Q: Can I run the WebUI and CLI at the same time?**

Yes. Both read and write the same `~/.atlas/` directory. Be aware that if you run a capture
in the CLI while a WebUI capture is also running, they will both write to the same session
directory — only run one capture at a time per session.

**Q: My browser warns me about the certificate.**

That's expected — the WebUI generates a self-signed cert on first launch. Accept it for
`localhost`. If you want to verify, the SHA-256 fingerprint is printed to stderr at startup
and is also at the top of `~/.atlas/webui.log` when running in daemon mode. To regenerate:
`platform-atlas-webui --reset-tls`.

**Q: How do I get back into the WebUI after a daemon restart?**

`platform-atlas-webui login-url` mints a fresh nonce-signed URL on demand. Or open
`~/.atlas/webui.log` to find the URL printed at last startup.

**Q: Does the WebUI require any additional configuration beyond the CLI setup?**

No. Install the WebUI wheel, run `platform-atlas-webui`, and it uses whatever the CLI already
has configured — environments, credentials, sessions, tier. The first launch on a fresh
install redirects to a setup wizard that mirrors `platform-atlas config init`.

**Q: Is the WebUI safe on a shared workstation?**

The WebUI binds to your OS user via a token file at mode 0600. Another user on the same box
can't read your token, can't forge a session cookie, and can't reach the listening socket
without copying both the token file and the cookie-secret file. Rotate either with
`--reset-token` to invalidate every outstanding browser session.