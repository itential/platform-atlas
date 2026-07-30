# Platform Atlas — User Guide

Welcome to Platform Atlas. This guide walks you through everything you need to get up and running, from installation through generating your first health report. If you get stuck, check the FAQ section at the end — most common issues are covered there.

---

## What is Platform Atlas?

Platform Atlas is a configuration auditing tool for the Itential Automation Platform. It connects to your IAP environment, collects configuration data from Platform, MongoDB, Redis, and Automation Gateway, then validates that data against a set of best-practice rules and generates a professional HTML report.

The typical workflow looks like this:

    Install → Configure → Choose Tier → Create Environment → Preflight → Create Session → Capture → Validate → Report

Each step builds on the previous one. Once you've completed the initial setup, day-to-day usage is just two commands: create a session (which binds your environment, ruleset, tier, and profile in one step) and run it. If you work with multiple deployments or organizations, switching between them is a single `platform-atlas session switch` command that restores the full context.

---

## What's New in v2.0

Version 2.0 is a major release. The headline changes are below; each feature has its own section later in this guide.

### SaaS tier — single-gateway audits

A third audit mode joins Standard and Extended. **SaaS** audits a single Automation Gateway — Gateway 4 *or* Gateway 5 — with **no Platform, MongoDB, or Redis** anywhere in the flow. You pick it per-environment at create time and answer only that gateway's questions; the audit produces one merged report. See the *Tiers* section.

### Explicit credential backends — no more auto-fallback

Atlas now asks where each environment's secrets should live and uses exactly that store on every run: **OS Keyring**, an **Encrypted Local File**, or **HashiCorp Vault**. There is no probing and no silent fallback or switch. The encrypted local file is now a first-class choice (ideal for a headless Linux server with no keyring), not a hidden fallback. Switch later with `config credentials --use-keyring` or `--use-file-store`. See *Credential Storage*.

> **Upgrading note:** the old automatic file-store fallback — and its `--auto` flag, the `ATLAS_USE_FILE_STORE` variable, and the `~/.atlas/.use_file_store` sentinel — has been removed. Existing keyring and Vault installs are unaffected; if you were relying on the fallback, pick the **Encrypted Local File** backend explicitly.

### Record architecture details any time

The new `env architecture` command opens the architecture form (browser or CLI prompts) for any environment, independent of capture — so capture no longer stops to ask. `config architecture` is kept as an alias.

### Gateway 5 config from a file

Containerized Gateway 5 deployments no longer need SSH — point Atlas at a local Docker Compose file or Helm `values.yaml` and it reads the `GATEWAY_*` variables directly. Offered wherever you add a Gateway 5 (Extended and SaaS).

### Legacy 2023.x rulesets hidden by default

2023.x rulesets and profiles stay bundled but are hidden from every listing and picker unless the active environment is explicitly marked legacy — so day-to-day lists show only P6 content.

### Credential redaction in capture files

Inline-credential connection strings (most often a `mongo_url` like `mongodb://user:pass@host/...`) are now masked to `scheme://*****:*****@` before any capture file is written, so reports, exports, and support bundles never carry the secret.

### Earlier highlights (1.7.x – 1.8.x)

If you're coming from 1.6.x, these arrived along the way — each has its own section below:

- **Standard / Extended tiers** and the `tier` commands (1.7) — see *Tiers*.
- **Continuous Audit** drift monitoring and the **Fleet dashboard** (1.7) — see those sections.
- **Outbound drift notifications** (Slack / webhooks) and **ControlMaster SSH transport** for CyberArk PSMP (1.7).
- **`support-bundle`** diagnostic ZIPs and **`ruleset update`** (1.8) — see *Diagnostics & Support* and *Rulesets and Profiles*.
- **Per-environment rule suppression** (`ruleset skip-rule`) and **resume-interrupted-capture** (1.8).
- **`config edit`**, **`config doctor`**, **`config plain`**, **`session prune`**, and **`session repair`** (1.7.2 – 1.8.1).
- **Windows 11 support** (1.8) — Atlas runs on Windows 11 workstations with no extra setup.

### Backward compatibility

All existing sessions, environments, and configurations continue to work without changes. Installs upgraded from 1.6.x default to **Extended** tier so capture behavior is preserved exactly. Sessions created before tiers existed do not have a bound tier — they use the globally active tier.

---

## Installation

Platform Atlas is distributed as a Python wheel file. You'll need Python 3.11 or later installed on the machine where you plan to run it.

### Install from a wheel file

Your team lead or Itential contact will provide you with a `.whl` file. Install it with pip:

```bash
pip install platform_atlas-<version>-py3-none-any.whl
```

Once installed, the `platform-atlas` command is available in your terminal. Verify it works:

```bash
platform-atlas --version
```

You should see something like `platform-atlas 2.0.0`.

### A note about your system

Platform Atlas stores its configuration and session data in a folder called `~/.atlas/` in your home directory. This folder is created automatically the first time you run the tool. You don't need to create it yourself.

If you're running on a Linux server (like RHEL), you'll also need a credential store backend. macOS and Windows handle this automatically. See the *Credential Storage* section below for Linux-specific instructions.

---

## First-Time Setup

The first time you run `platform-atlas`, it detects that no configuration exists and launches an interactive setup wizard. You can also run it manually at any time:

```bash
platform-atlas config init
```

Global setup asks only two things — a color theme (chosen first, so the rest of the wizard renders in your chosen colors) and your **Organization Name**, which appears on reports and serves as the default when creating environments (each environment can override it, useful if you audit multiple customers). These are saved to `~/.atlas/config.json`, along with defaults you can change later via `config edit` — SSL verification starts off, and the tier defaults to Standard until you create an environment.

### Creating your first environment (optional)

Global setup ends with a prompt: *"Would you like to create your first environment now?"* This step is optional. Decline it and setup finishes right there — Atlas shows a summary of what still works with no environment configured (`config doctor`, `config edit`, `guide`, the dashboard) and which commands need one (`session`, `preflight`, `fleet`, `continuous-audit`, `support-bundle` — each shows a clear "no environment configured" message and exits until you create one).

Accept the prompt and you're walked straight into the terminal environment wizard — the same one described below for `env create`. An environment represents one IAP deployment — for example, "production" or "dev". The environment file is saved to `~/.atlas/environments/<n>.json`.

The wizard's first question is always the audit tier — Standard, Extended, or SaaS. Then you're asked for a name, an organization name (defaults from global config), and an optional description. Then, depending on tier, you're walked through some or all of the following sections:

#### Credential Storage

Atlas needs to store sensitive values like your Platform client secret and database URIs. It never stores these in plain text on disk. Instead, you choose one of **three credential backends** during setup — **OS Keyring**, an **Encrypted Local File**, or **HashiCorp Vault** — and Atlas uses exactly that store on every run. There is no probing and no automatic fallback or switch (this changed in v2.0): the setup wizard shows whether the OS keyring actually works on this host, so you can pick the right backend up front. Your choice is saved in the environment's `credential_backend` field.

**OS Keyring** (recommended) — Uses your operating system's built-in credential store. Credentials are scoped per environment (stored under `platform-atlas/<env-name>` in the keyring), so each environment has fully isolated secrets.

- macOS: Keychain (built-in, no extra setup)
- Windows: Credential Locker (built-in, no extra setup)
- Linux: `gnome-keyring` / KWallet with a D-Bus session (desktop). On a headless server with no usable keyring, choose the **Encrypted Local File** backend instead — the wizard flags a broken keyring rather than letting you pick it.

**Encrypted Local File** — A self-contained encrypted file at `~/.atlas/credentials.enc`, ideal for a headless Linux server with no keyring, or anywhere you'd rather keep secrets off the OS keyring. Encryption is AES-256-GCM with a key derived from this host and user plus a random per-install salt (`~/.atlas/.keysalt`), so the file won't decrypt if copied to another machine and a leaked file is useless without the salt. It is written with `0o600` permissions. Preflight, `config doctor`, and the banner report it honestly as an encrypted file (never as an "encrypted keyring"). If the file or salt is later deleted or corrupted, Atlas detects it and offers to recreate it and re-enter credentials, instead of a confusing "credential not found."

**HashiCorp Vault** — If your organization manages secrets in Vault, Atlas can read credentials from a KV v2 secrets engine. In this mode, Atlas only *reads* from Vault — it never writes secrets. Your Vault administrator manages the actual credentials. Because Vault still needs a local home for its own connection settings (URL/token/AppRole), choosing Vault asks where those live — the OS keyring (recommended) or the encrypted file. Atlas supports several authentication methods: a static token, AppRole (role_id + secret_id), and three automated options designed for environments where credentials rotate — see the *Vault Integration* section in the FAQ for details on choosing the right one.

> **Switching backends later:** run `platform-atlas config credentials --use-keyring` or `platform-atlas config credentials --use-file-store`. You re-enter the secrets for the new store — nothing is silently copied between backends.

#### Connection Credentials

You'll be prompted for the credentials Atlas needs to connect to this environment:

- **Platform URI** — The URL of your IAP instance (for example, `https://iap.yourcompany.com:3443`).
- **Platform Client ID** — The OAuth2 client ID for API access.
- **Platform Client Secret** — The OAuth2 secret that pairs with the Client ID. This is entered as a hidden field.
- **MongoDB URI** — The full connection string for your MongoDB instance. You can skip this if MongoDB auditing isn't needed.
- **Redis URI** — The full connection string for your Redis instance. You can skip this if Redis auditing isn't needed.

All of these are stored in the credential backend you chose — the OS keyring (scoped to the environment name), the encrypted local file, or Vault — never in config files.

#### Deployment Topology

> **Extended Tier only** — The topology wizard and all SSH configuration apply only when running in Extended tier. Standard tier does not connect to any servers via SSH and skips this section entirely.

Atlas needs to know how this environment's IAP deployment is set up so it knows which servers to connect to and what collectors to run. The wizard asks you to pick a deployment mode:

**Standalone** — A single-server deployment where IAP, MongoDB, and Redis all run on one machine (or are split across a few machines, but with one instance of each).

**HA2** — A highly available setup with multiple IAP nodes, a MongoDB replica set (typically 3 members), and Redis Sentinel (typically 3 members). You'll be asked for the hostname or IP of each server.

**Custom** — A free-form layout where you manually assign roles and modules to each node.

For each server in your topology, you'll configure the transport method:

- **SSH** (recommended) — Atlas SSHes into each server to read configuration files and run lightweight commands. The SSH user needs read access to config files in `/etc/` and `/opt/` — passwordless sudo is used as a fallback for root-owned files.
- **ControlMaster** — For CyberArk PSMP and other PAM-gateway environments. You open one ControlMaster session per node before running Atlas; Atlas multiplexes on those sessions with no credentials.
- **Local** — For the Platform (IAP) node only, when Atlas is installed on the same server. Reads config files and runs commands directly via the local filesystem. All other nodes remain SSH-connected.

**Gateway 5 configuration source.** When you tell the wizard you have a Gateway 5, it asks how Atlas should read its `GATEWAY_*` configuration:

- **SSH (`printenv`)** — read the environment variables from the running gateway server (for VM / bare-metal gateways). Uses the SSH transport above.
- **Docker Compose file** or **Helm values.yaml** — for containerized gateways, point Atlas at a local file on your machine (for example `~/iag5-compose.yml`). Atlas parses the `GATEWAY_*` variables directly from the file's `environment` / `env` block — or from an IAG5 Helm chart's `serverSettings` / `applicationSettings` — so **no SSH access to the gateway host is needed**. The file you choose is authoritative (SSH is not attempted), preflight checks that it exists, parses, and contains recognized variables, and only the variables Atlas audits are read (everything else in the file is ignored).

### Creating Additional Environments

If you just finished your first environment inside the setup wizard, you'll be asked "Create another environment?" in a loop — accept to set up more right away (dev, staging, production), or decline and add them later.

Later, run the same wizard any time with:

```bash
platform-atlas env create
```

Unlike the inline setup flow, this command first asks **how** you'd like to fill it in:

- **Here in the terminal** — the same guided wizard described above.
- **In my browser** — opens a form in your browser that collects the whole environment, credentials included, and exports it as one encrypted bundle (`.atlasenv.enc`, AES-256-GCM) protected by a one-time passphrase shown in the browser. Finish the import with:

  ```bash
  platform-atlas env create --from-file <bundle>
  ```

  This decrypts the bundle, tests connections, and stores your secrets — no re-typing credentials into CLI prompts. The bundle is shredded after a successful import (pass `--keep-file` to keep it), and you can reload a bundle back into the wizard to fix a field and re-encrypt.

Or copy an existing environment and tweak it (fully CLI-driven, skips the terminal/browser choice):

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

---

## Diagnostics & Support

Beyond preflight's live-connectivity probes, Atlas includes a one-shot configuration health check and a diagnostic-bundle collector for when you need to share state with Itential support.

### Config doctor

For a broader configuration health check that goes beyond live-connectivity probing, use:

```bash
platform-atlas config doctor
```

`config doctor` is a one-shot diagnostic that verifies the following in a single pass, grouped under four headings — Runtime, Environment & Tier, Credentials, and Connectivity & Rules:

- **Config file** — Exists at `~/.atlas/config.json` and has secure permissions (chmod 600 on POSIX).
- **Active environment** — The env file referenced by `active_environment` actually exists on disk.
- **Tier** — Reports the active tier (Standard / Extended / SaaS).
- **Credential backend** — Confirms the configured backend (OS keyring, encrypted file, or Vault) is functional and warns when a keyring is unencrypted.
- **Platform Client Secret** — Confirms the secret is present in the active credential store (skipped under SaaS, which has no Platform secret).
- **Platform URL** — TCP-reaches the host:port from `platform_uri`.
- **Gateway4 URL** — TCP-reaches the gateway URL when one is configured.
- **Active ruleset** — Reports the active ruleset ID and rule count.
- **SSH key path** (Extended / SaaS only) — Confirms the key file exists, is readable, and isn't a `.pub` public key.

Each check prints a one-line status (`✓ OK`, `⚠ warning`, or `✘ error`) plus a specific fix-it suggestion when something's wrong. Run it after `config init`, after editing an environment, or any time a capture fails for an unclear reason. Exits 0 when everything passes, 1 on warnings, 2 on failures — so it can drive CI gates. Add `--json` for machine-readable output, or `--no-url-probes` to skip the slower TCP reachability checks in CI/offline use.

### Support bundles

When you open a support case with Itential, `support-bundle` packages the diagnostics they typically ask for into a single ZIP, so you don't have to collect logs and config by hand:

```bash
platform-atlas support-bundle
```

An interactive wizard collects a ticket number, an optional description, and (Extended only) how many days of logs to include (1–30, default 7). Before gathering anything, Atlas verifies the active credential backend can actually produce the Platform OAuth secret both tiers depend on — if it can't (Vault unreachable, auth failed, or the secret missing), the bundle is aborted with a fix hint instead of written empty.

What's collected depends on your tier:

- **Standard** — five Platform health API endpoints plus a redacted copy of your Atlas config.
- **Extended** — the above plus SSH-based platform / webserver / MongoDB logs and system info.
- **SaaS** — a config-snapshot bundle labeled for the gateway audit.

The output is `atlas-support-bundle-<timestamp>.zip` in the current directory (or `--output PATH`). Useful flags: `--log-days N` (Extended; overrides the prompt, clamped 1–30) and `--yes` to skip the confirmation prompt. Secrets are redacted from the bundled config, but review the ZIP before sharing if your environment is sensitive.

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

### Inspect individual rules

To see the actual rules in a ruleset — useful when deciding what to suppress or which profile fits — list them in a table, optionally filtered:

```bash
platform-atlas ruleset rules                        # all rules in the active ruleset
platform-atlas ruleset rules --category redis        # only Redis rules
platform-atlas ruleset rules --severity critical     # only critical rules
```

### Suppress a rule for one environment

Sometimes a rule doesn't apply to a particular deployment. Rather than disabling it everywhere, you can suppress it for the **active environment** only. A suppressed rule still runs, but appears as **Suppressed** (amber) in the report and ruleset table, so the intent stays visible — it isn't silently hidden:

```bash
platform-atlas ruleset skip-rule PLAT-001 --reason "Non-prod sandbox, control not required"
platform-atlas ruleset unskip-rule PLAT-001          # restore it
```

A justification reason (minimum 10 characters) is required — pass `--reason` or you'll be prompted for one. The reason is stored with the suppression and shown inline in the compliance report (under the Suppressed badge and in the rule detail modal). Only rule numbers that exist in the active ruleset are accepted.

### Update rulesets

`ruleset update` checks for newer ruleset versions and downloads compatible ones:

```bash
platform-atlas ruleset update
```

It fetches a signed manifest from GitHub, compares available versions against what you have installed, and downloads only rulesets compatible with your Atlas version. Downloads are SHA-256 verified and written atomically. If you decline an available update, the CLI dashboard shows a slim notice until you apply it.

### Sync bundled rulesets

Every Atlas release bundles its own rulesets and profiles; on startup Atlas copies new and newer ones into `~/.atlas` automatically. To force that sync manually — for example after a reinstall — use:

```bash
platform-atlas ruleset sync                # version-aware copy (keeps locally-newer files)
platform-atlas ruleset sync --force        # wipe local rulesets/profiles, then re-copy fresh
```

`--force` is destructive — it deletes **all** local rulesets and profiles first, including custom files and anything downloaded via `ruleset update`, takes no backup, and prompts to confirm unless `--yes` is given.

---

## Running an Audit

An audit in Platform Atlas is organized into **sessions**. Each session represents a complete audit cycle: capture data, validate it against rules, and generate a report. Sessions keep everything organized and let you compare results over time.

### Step 1 — Create a session

```bash
platform-atlas session create prod-q1-2026
```

The session name should be descriptive — something like `prod-audit-march` or `staging-q1-2026`. Names must be 3-64 characters using letters, numbers, hyphens, and underscores.

When you create a session, Atlas walks you through prompts to bind the session to its context:

1. **Select tier** — Choose **Standard** (Platform OAuth + optional IAG4 API, ~54 rules, no SSH required) or **Extended** (full infrastructure audit via SSH/MongoDB/Redis/Kubernetes, ~107 rules). Defaults to your currently active tier.
2. **Select environment** — Pick which IAP deployment to audit. The list shows each environment's organization name and platform URI so you can easily tell them apart. If you need a new environment, there's a "Create new environment..." option right in the picker.
3. **Select ruleset** — Pick which set of validation rules to use. The list shows version and rule count.
4. **Select profile** — Pick a deployment profile overlay (e.g., standalone, HA2, HA2 with gateway). You can also choose "No profile" to use the ruleset as-is.

These bindings are locked into the session. When you switch between sessions later, the tier, environment, ruleset, and profile all switch with it. Cross-tier diffs (comparing a Standard session against an Extended session) are flagged with a notice banner in the diff report.

You can also bypass the interactive prompts with flags:

```bash
platform-atlas session create prod-q1-2026 --env production --ruleset p6-master-ruleset --profile ha2-gateway --tier extended
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

This connects to your deployment and collects configuration data. You'll see a live progress display showing each collector as it runs. What gets collected depends on your tier:

**Standard tier** collects:
- Platform API health, adapter configurations, application states
- IAG4 API configuration and version (if configured)

**Extended tier** additionally collects:
- System info (CPU, memory, disk, kernel version) via SSH
- MongoDB server status and database statistics
- Redis INFO, ACL rules, and Sentinel topology
- Configuration files (mongod.conf, redis.conf, platform.properties) via SSH
- Platform log analysis (error/warning frequency) via SSH
- Webserver access log analysis via SSH
- Gateway packages and environment variables

If a collector fails (for example, because a config file is missing), Atlas will offer to let you provide the data manually through a guided prompt. You can skip this with `--skip-guided`.

#### Resuming an interrupted capture

If a capture is interrupted partway through — a Ctrl-C, a dropped SSH connection, or a network blip — Atlas checkpoints each collector's result as it completes to `00_checkpoint.json` inside the session directory. Re-running `session run capture` detects the checkpoint and offers to resume from where it left off, so you don't re-collect everything. The checkpoint is cleared automatically once capture completes successfully.

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

To package a session as a complete delivery bundle (for example, to attach to an Itential Enablement Request, or to hand a report to your team):

```bash
platform-atlas session export prod-q1-2026
```

Run with no name to confirm the active session or arrow-pick from the most recent ones. The ZIP is named `ATLAS-<org>-<session>-<date>` and bundles the full report set (`03_report.html`, `04_operational.html`, `05_arch.html` — a single merged report under SaaS), a fresh `report.json`, session metadata, and a README. A summary panel lists exactly what was packaged.

By default, raw capture data is redacted from exports for security. Add `--include-debug` (alias `--no-redact`) to also include the troubleshooting files — `session.log`, `01_capture.json`, and `debug.log` — when Itential support asks for them. Embedded credentials inside capture files are masked regardless of this flag (see the credential-redaction note in *What's New*).

### Delete a session

```bash
platform-atlas session delete prod-q1-2026
```

You'll be asked to confirm. To skip the confirmation prompt, add `--force`.

### Prune old sessions

Over time, audit sessions accumulate. `session prune` cleans them up by age, count, status, or environment — and is **dry-run by default**, so you always see what would be deleted before anything is removed:

```bash
platform-atlas session prune                                 # preview: sessions older than 30d
platform-atlas session prune --older-than 7d                 # a different age cutoff (30d, 7d, 1w, 24h…)
platform-atlas session prune --keep-last 10                  # keep the 10 newest, preview the rest
platform-atlas session prune --status aborted                # only sessions with this final status
platform-atlas session prune --env dev                       # only sessions bound to one environment
platform-atlas session prune --older-than 30d --no-dry-run   # actually delete (asks to confirm)
```

All filter flags AND together. The active session is always excluded. Add `--yes` to skip the confirmation on a real (`--no-dry-run`) run.

### Repair older sessions

If you have sessions created before environment/ruleset bindings existed, `session repair` backfills their missing metadata (organization name, environment, ruleset, profile) from the capture data already on disk:

```bash
platform-atlas session repair                # repair all sessions
platform-atlas session repair my-session     # repair one
platform-atlas session repair --dry-run      # preview changes without writing
```

It's safe to run multiple times — it only fills in blanks and never overwrites existing values.

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
platform-atlas env architecture staging             # Record/update architecture details (see below)
platform-atlas env remove dev                       # Delete an environment
```

### Update credentials

```bash
platform-atlas config credentials
```

This shows the status of all stored credentials (stored or missing) for the active environment and lets you update or delete individual ones through an interactive menu. The list is tier-aware — it shows only the credentials your active tier actually uses. To change *where* this environment's secrets are stored, add `--use-keyring` or `--use-file-store` (you re-enter the secrets; see *Credential Storage*).

### Change your deployment topology

```bash
platform-atlas config deployment
```

This re-runs just the topology wizard. If an environment is active, it updates the environment file. Otherwise it updates `config.json` directly. Useful when servers are added or removed from your environment.

### Switch themes

Atlas supports multiple color themes for its terminal output:

```bash
platform-atlas config edit
```

Pick a theme from the interactive list. The change takes effect the next time you run a command.

### Edit individual settings

`config edit` tunes individual behavior and timeout settings without hand-editing `config.json`:

```bash
platform-atlas config edit
```

It presents a grouped menu: manual input mode (browser vs. terminal), log retention, validation depth, and connection/request timeouts — MongoDB aggregation, SSH connect, Platform API, and Redis. Timeouts pick from safe bounded sets. Every option defaults to current behavior, so nothing changes unless you opt in.

### Plain / compatibility mode

For terminals that don't render Rich formatting (no color, no Unicode box-drawing), turn on plain mode:

```bash
platform-atlas config plain         # toggle plain mode on/off
platform-atlas --plain <command>    # one-off; passing it once also persists the setting
```

Plain mode disables ANSI colors, replaces Unicode borders and status glyphs with ASCII equivalents, and suppresses emoji and syntax highlighting.

### Record or update architecture details

The audit report includes an architecture overview (deployment layout, load balancer, monitoring, security posture). You can fill it in — or revisit it — any time, independent of a capture:

```bash
platform-atlas env architecture                 # active environment
platform-atlas env architecture production       # a named environment
platform-atlas env architecture --force          # re-walk every section, even answered ones
```

This opens the browser form (or CLI prompts if no browser is available) and saves answers per-environment at `~/.atlas/architecture/<env>.json`. Under SaaS, the form is scoped to the chosen gateway. Because you can manage it here, capture no longer interrupts to ask — it shows one notice if the form is unfinished and otherwise proceeds. (`config architecture` is a synonym.)

---

## Tiers

Platform Atlas ships with three audit modes that control which collectors run, which rules are evaluated, and what credentials are required.

### Standard tier

Audits over Platform OAuth and the optional IAG4 API only (~54 rules). No SSH, MongoDB, or Redis access required. Designed for:
- Quick application-layer health checks
- Environments where infrastructure access is restricted
- Teams that only need Platform-level compliance data

### SaaS tier

Audits a **single standalone Automation Gateway** — Gateway 4 *or* Gateway 5 — with no Platform, MongoDB, or Redis anywhere in the flow. Chosen per-environment at create time (`platform-atlas env create`), where you pick the gateway kind once and answer only that gateway's questions:
- **Gateway 4** — audited over its REST API (ipsdk), plus an optional SSH block for deeper config (properties.yml, venv Python version, DB sizes, host facts). Declining SSH gives an API-only audit where SSH-dependent rules simply SKIP.
- **Gateway 5** — environment variables read over SSH `printenv`, or from a local Docker Compose / Helm values file (no SSH needed for containerized gateways).

A SaaS audit evaluates only the chosen gateway's rule category and produces a **single report file** — the compliance report with the Architecture Overview merged in (no operational report, no separate architecture report). The architecture form is gateway-scoped: Platform/MongoDB/Redis sections never appear.

SaaS is never the global default and a SaaS environment cannot be converted to another tier — its shape (one gateway, no Platform) is fixed at create time. Create a new environment for a different audit type.

### Extended tier

Full infrastructure audit (~107 rules). Adds SSH-based collectors for system info, config files, and logs, plus MongoDB, Redis, Kubernetes, and Gateway collectors. Designed for:
- Comprehensive compliance audits
- All installs upgraded from 1.6.x (default)
- Environments where the full Atlas ruleset applies

### Managing your tier

```bash
platform-atlas tier show                 # current tier and what is enabled
platform-atlas tier set standard         # switch to Standard
platform-atlas tier set extended         # switch to Extended
platform-atlas tier upgrade              # guided walkthrough (topology, SSH, Mongo/Redis)
platform-atlas tier upgrade --from-file <bundle>   # finish an upgrade started in the browser form
platform-atlas tier downgrade            # interactive downgrade
```

`tier upgrade` walks through the extra pieces Extended needs one stage at a time, in the terminal or in a browser form. The tier only switches at the very end, so backing out at any point leaves the environment unchanged in Standard Mode.

`tier set`, `tier upgrade`, and `tier downgrade` move between Standard and Extended. SaaS is selected only when creating an environment.

Tier resolution order: `--tier` flag → `ATLAS_TIER` env var → environment overlay → config → default.

The `--tier` flag overrides for a single command without changing the persisted setting:

```bash
platform-atlas --tier standard session run capture   # one-off Standard capture
```

---

## Fleet Dashboard

The fleet dashboard provides a read-only compliance overview across all your configured environments from local session cache. No captures are triggered — it reads only from data already on disk.

```bash
platform-atlas fleet status              # overview table of all environments
platform-atlas fleet status --json       # machine-readable output for scripts/CI
```

Each row shows: environment name, tier, last session age, compliance pass rate, continuous-audit state, and unacknowledged alert count. The fleet view is also available in the WebUI at `/fleet`.

---

## Continuous Audit

Continuous audit schedules automatic drift monitoring for an environment. Each run re-captures via Platform OAuth against the active ruleset and surfaces changed observed values as alerts. An OS-level schedule is installed when enabled (systemd timer on Linux, launchd agent on macOS) so runs survive process restarts.

### Setup

A successful `run-once` test is required before you can enable continuous audit:

```bash
platform-atlas continuous-audit run-once   # test run — validates credentials and ruleset
platform-atlas continuous-audit enable     # install OS schedule and start monitoring
```

### Status and alerts

```bash
platform-atlas continuous-audit status               # enabled state, last run, next run, alert count
platform-atlas continuous-audit alerts               # view unacknowledged alerts with drift details
platform-atlas continuous-audit ack <alert-id>       # acknowledge an alert
platform-atlas continuous-audit ack-all              # acknowledge all alerts
```

Acked alerts automatically re-open if the same drift recurs.

### Policy and filtering

```bash
platform-atlas continuous-audit policy any           # alert on any rule change (default)
platform-atlas continuous-audit policy regression    # alert only on PASS → FAIL transitions
platform-atlas continuous-audit watch add <rule-id>  # restrict alerts to specific rules
platform-atlas continuous-audit watch remove <rule-id>
platform-atlas continuous-audit watch list
platform-atlas continuous-audit watch clear
```

### Notifications

```bash
platform-atlas continuous-audit notify add           # add a Slack webhook or generic JSON webhook
platform-atlas continuous-audit notify list          # see configured channels
platform-atlas continuous-audit notify remove        # remove a channel
platform-atlas continuous-audit notify test          # send a test notification
```

Notifications fire only on alert-state transitions (new alert, re-opened acked alert) — not on every drift cycle, so persistent unacknowledged drift does not spam every run. Webhook URLs are validated against private/loopback address ranges at configuration time. Webhook URLs and HMAC signing secrets are stored in the OS keyring rather than the environment JSON file.

### Data storage

Continuous audit data for each environment is stored under `~/.atlas/continuous/<env>/`:

| Path | Contents |
|------|----------|
| `runs/<run_id>.json` | Bare JSON report for each run with per-rule drift inline |
| `events.ndjson` | Append-only event timeline (all drift, all transitions) |
| `alerts.json` | Current aggregate alert state (ack status, transition history) |
| `status.json` | Last run timestamp, enabled state, next scheduled run |
| `latest.json` | Pointer to the most recent run |

### Stopping

```bash
platform-atlas continuous-audit disable   # remove OS schedule and stop monitoring
```

Existing run data and alerts are preserved.

---

## Frequently Asked Questions

### Tiers

**Q: I upgraded from 1.6.x. Do I need to change anything for tiers?**

No. Upgrades from 1.6.x default to **Extended** tier, which preserves the exact capture behavior you had before. The tier system is additive — nothing is removed or changed from your existing workflow.

**Q: Which tier should I use?**

Start with **Standard** if you only have Platform credentials (client ID + secret) and don't have SSH access or MongoDB/Redis URIs. Use **Extended** if you want full infrastructure coverage and have the necessary access configured. You can upgrade at any time with `platform-atlas tier upgrade`. Use **SaaS** (chosen during `env create`) when you're auditing a standalone Gateway 4 or Gateway 5 with no Platform behind it.

**Q: Can I mix tiers within the same environment?**

Yes. Each session binds a tier at creation time, so you can create both a Standard session and an Extended session against the same environment. The `--tier` flag also lets you run a one-off command in a different tier without changing the persisted setting. If you diff a Standard session against an Extended session, the diff report will flag the tier mismatch with a notice banner.

**Q: The report shows a "†" obelisk next to some rules in Standard mode.**

It shouldn't — Standard reports do not show the partial-capture obelisk. That symbol only appears in Extended mode when a collector didn't run. If you are seeing it in Standard, the session may have been created with Extended tier and subsequently switched. Check `platform-atlas session show` to see which tier the session was bound to at creation.

**Q: I set the tier to Standard but capture is still trying to connect via SSH.**

Make sure the session itself was created under Standard tier. The session's bound tier (set at creation) takes precedence over the global tier setting. Run `platform-atlas session show` to check the session's tier. If needed, create a new session with `--tier standard`.

### Setup and Installation

**Q: I get "command not found" after installing the wheel file.**

Make sure the Python `bin` or `Scripts` directory is in your system PATH. If you installed with `--user`, the location is typically `~/.local/bin` on Linux/macOS. Try running `python -m platform_atlas` as an alternative.

**Q: The setup wizard says my keyring is unavailable or unencrypted — or I'm on a headless server.**

This happens on Linux servers without a graphical desktop, where the `keyring` library can't reach a secure credential store. As of v2.0 there is **no automatic fallback** — you choose a backend explicitly, and the setup wizard shows whether the keyring actually works on this host so you can pick the right one up front. Your options:

1. Choose the **Encrypted Local File** backend at setup (or switch later with `platform-atlas config credentials --use-file-store`). Secrets go in an encrypted, machine-bound file at `~/.atlas/credentials.enc` — see *Credential Storage*.
2. Use **HashiCorp Vault** as your credential backend instead of the OS keyring.
3. Configure an encrypted OS keyring by hand: `export PYTHON_KEYRING_BACKEND=keyrings.alt.file.EncryptedKeyring`, then pick the OS Keyring backend.

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

- `config.json` — Global configuration (default org name, theme, debug settings — no secrets)
- `settings.json` — Active ruleset and profile pointers
- `environments/` — One file per named deployment target (production.json, dev.json, etc.), each with its own org name, credential backend, and topology
- `sessions/` — One folder per audit session containing capture data, validation results, reports, and a `session.json` with the session's bound environment, ruleset, tier, and profile
- `architecture/` — Per-environment architecture form answers (`<env>.json`), included in the report
- `rulesets/` — Bundled and downloaded rulesets and profiles (synced from the package on startup)
- `pipelines/` — User-defined MongoDB aggregation pipeline definitions
- `continuous/` — Continuous audit data per environment: runs, events timeline, alerts state, status
- `credentials.enc` + `.keysalt` — Encrypted credential file and its salt (only when the Encrypted Local File backend is in use)
- `atlas.log` — Application log (rotated at 5 MB)

**Q: How do I get debug output for troubleshooting?**

Add `--debug` to any command:

```bash
platform-atlas --debug session run capture
```

This enables verbose logging to both the console and the log file at `~/.atlas/atlas.log`.

**Q: How do I see all available commands?**

Run `platform-atlas --help` for the top-level command list, or `platform-atlas <command> --help` (e.g. `platform-atlas session --help`) to drill into a command group and its flags. `platform-atlas guide` renders the project README in the terminal for a quick reference without leaving your shell.

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

Make sure your credential store works in non-interactive environments. The OS keyring can fail silently under cron when no desktop session is present, so for scheduled runs choose a backend that doesn't need one: the **Encrypted Local File** backend (`platform-atlas config credentials --use-file-store`, secrets in `~/.atlas/credentials.enc`) or **HashiCorp Vault**. Either avoids the `gnome-keyring`/D-Bus dependency.