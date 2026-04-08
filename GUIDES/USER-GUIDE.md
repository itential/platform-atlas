# Platform Atlas — User Guide

Welcome to Platform Atlas. This guide walks you through everything you need to get up and running, from installation through generating your first health report. If you get stuck, check the FAQ section at the end — most common issues are covered there.

---

## What is Platform Atlas?

Platform Atlas is a configuration auditing tool for the Itential Automation Platform. It connects to your IAP environment, collects configuration data from Platform, MongoDB, Redis, and Automation Gateway, then validates that data against a set of best-practice rules and generates a professional HTML report.

The typical workflow looks like this:

    Install → Configure → Create Environment → Preflight → Create Session → Capture → Validate → Report

Each step builds on the previous one. Once you've completed the initial setup, day-to-day usage is just two commands: create a session (which binds your environment, ruleset, and profile in one step) and run it. If you work with multiple deployments or organizations, switching between them is a single `platform-atlas session switch` command that restores the full context.

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

Your team lead or Itential contact will provide you with a `.whl` file. Install it with pip:

```bash
pip install platform_atlas-1.5-py3-none-any.whl
```

Once installed, the `platform-atlas` command is available in your terminal. Verify it works:

```bash
platform-atlas --version
```

You should see something like `platform-atlas 1.5`.

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

**HashiCorp Vault** — If your organization manages secrets in Vault, Atlas can read credentials from a KV v2 secrets engine. In this mode, Atlas only *reads* from Vault — it never writes secrets. Your Vault administrator manages the actual credentials. You'll need either a Vault token or AppRole credentials (role_id and secret_id).

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

**Custom** — A free-form layout where you manually assign roles and modules to each node.

For each server in your topology, you'll configure SSH access (username, key file, port). Atlas uses SSH to read configuration files and run lightweight commands on each server. The SSH user needs read access to config files in `/etc/` and `/opt/` — it does not need root access, though passwordless sudo is used as a fallback if a file can't be read directly.

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

### Step 5 (Optional) — Operational Report

In addition to the standard configuration health report, Atlas can generate a separate **operational metrics report** that queries live workflow and task data from your Platform’s MongoDB database:

```bash
platform-atlas session run report --operational
```

This runs a set of MongoDB aggregation pipelines against your Platform database (using the same `mongo_uri` you already have configured) and produces a standalone HTML report with the results. The operational report covers things like:

- Top workflows by execution count
- Workflow runtime statistics (total time, average duration)
- Task execution patterns and frequency

The operational report is saved as `~/.atlas/sessions/<name>/04_operational.html` alongside the standard report. It does not replace or interfere with the configuration health report — it’s a completely separate output.

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

The operational report is a separate, optional report that analyzes live workflow and task data from your Platform’s MongoDB database. Unlike the standard health report (which validates configuration), the operational report shows actual usage metrics — things like which workflows run most frequently and how long they take.

Generate it after running the standard report:

```bash
platform-atlas session run report --operational
```

This requires a configured `mongo_uri` that points to the Platform database. The report is saved as `04_operational.html` in your session directory. You can add custom pipeline JSON files to `~/.atlas/pipelines/` to extend it with your own aggregations.

### Vault Integration

**Q: How do I switch from OS Keyring to Vault (or vice versa)?**

If you're using environments, create a new environment and select the desired backend during the wizard:

```bash
platform-atlas env create
```

For legacy setups without environments, re-run the setup wizard:

```bash
platform-atlas config init
```

During the credential storage step, select the backend you want. If switching to Vault, you'll need to provide the Vault URL and authentication credentials (token or AppRole). Your Atlas secrets must already exist in Vault at the configured path — Atlas only reads from Vault, it never writes.

**Q: My Vault token expired and now Atlas won't start.**

Run the credential update command:

```bash
platform-atlas config credentials
```

Atlas will detect the failed Vault connection and offer to update the connection settings. Enter your new token or AppRole credentials.

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
- `environments/` — One file per named deployment target (production.json, dev.json, etc.), each with its own org name, credentials backend, and topology
- `sessions/` — One folder per audit session containing capture data, validation results, reports, and a `session.json` with the session's bound environment, ruleset, and profile
- `atlas.log` — Application log (rotated at 5 MB)

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