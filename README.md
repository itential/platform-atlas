# Platform Atlas

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-%20%20GNU%20GPLv3%20-green?style=flat-square)

> Enterprise configuration auditing and compliance reporting for Itential Automation Platform

Platform Atlas is a comprehensive CLI tool that captures configuration data from Itential Automation Platform deployments and their dependencies, validates it against versioned rulesets, and generates professional compliance reports. It is designed for both Itential Customer Success teams conducting quarterly health assessments and customers performing self-service configuration validation.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Install and Setup](#install-and-setup)
- [Initial Setup](#initial-setup)
- [Configuration](#configuration)
- [Environments](#environments)
- [Kubernetes Deployments](#kubernetes-deployments)
- [ControlMaster Transport (CyberArk PSMP)](#controlmaster-transport-cyberark-psmp)
- [The Workflow](#the-workflow)
- [Command Reference](#command-reference)
- [Rulesets and Profiles](#rulesets-and-profiles)
- [Multi-Tenant Mode](#multi-tenant-mode)
- [Required Permissions](#required-permissions)
- [Security](#security)
- [Themes](#themes)
- [Troubleshooting](#troubleshooting)
- [Upgrading from Pre-1.5](#upgrading-from-pre-15)
- [Support](#support)
- [License](#license)

---

## Features

- **Two Audit Tiers** — Choose between Standard (Platform OAuth + IAG4 API only, ~55 rules, no SSH/MongoDB/Redis required) and Extended (full infrastructure audit via SSH, MongoDB, Redis, Kubernetes, and Gateways, ~108 rules). Switch at any time with `platform-atlas tier set`.
- **Automated Data Collection** — Connects via SSH, MongoDB, Redis, and Platform OAuth to capture configuration data from all components of an IAP deployment. If passwordless sudo is available, Atlas will automatically use it to read configuration files that the SSH user cannot access directly.
- **Optional WebUI** — Browser-based interface (`platform-atlas-webui` wheel) for managing sessions, running captures, streaming live job output, and browsing reports — no CLI knowledge required.
- **Multiple Rulesets** — Select from versioned, JSON schema-validated rulesets tailored to specific platform versions (e.g., Platform 6)
- **Ruleset Profiles** — Environment-specific overlays (standalone, HA2, dev, prod, gateway4, gateway5) that enable or disable rules from the master ruleset
- **~108 Validation Rules (Extended) / ~55 (Standard)** — Covering Redis, MongoDB, Platform, Gateway4, and Gateway5 across critical, warning, and info severity levels
- **Extended Validation** — Decorator-based checks that run outside the standard ruleset structure for health, adapter, and version analysis
- **Rule Chaining** — Rules can depend on other rules, so downstream checks are automatically skipped when a dependency fails
- **Dynamic Rules** — Limited computed values inside rules for proper comparison against runtime configuration
- **Professional HTML Reports** — Full HTML/CSS/JS reports with the Atlas Horizon design system, supporting dark and light themes with W3C-compliant markup
- **Session Diff Engine** — Compare two audit sessions side-by-side to track configuration drift, regressions, and fixes over time
- **Session Management** — Organize captures, validations, and reports into named sessions with metadata tracking. Each session binds an environment, ruleset, tier, and profile at creation — switching sessions restores the full context automatically
- **Guided Manual Collection** — Interactive fallback prompts for environments where automated capture cannot reach certain components. Supports batch directory import (`--import-dir`) for importing all pre-collected files at once without interactive prompts — re-runnable to incrementally add data.
- **Multi-Tenant Mode** — Itential staff can import and manage capture data from multiple customer organizations in a single installation
- **Named Environments** — Define multiple deployment targets (dev, staging, production) as independent environment files, each with its own organization name, connection details, topology, and scoped credentials
- **Deployment Topology** — Models standalone, HA2, and custom deployment architectures with configurable capture scope
- **Secure Credential Storage** — Sensitive credentials stored in the OS keyring (macOS Keychain, Windows Credential Locker, Linux Secret Service) or Hashicorp Vault, scoped per environment, never in config files
- **Air-Gapped Support** — Operates entirely offline after installation; no internet access required for capture, validation, or reporting
- **Multiple Export Formats** — HTML, CSV, and JSON report output with session export and redaction support
- **Three-Report System** — `session run report` always generates all three HTML reports in a single pass: `03_report.html` (compliance), `04_operational.html` (logs + MongoDB pipelines), and `05_arch.html` (architecture & maintenance); all three share a header nav bar and the compliance report opens automatically in the browser
- **MongoDB Aggregation Pipelines** — After capture, Atlas prompts whether to run aggregation pipelines against the Platform's MongoDB database for the operational report; pipeline definitions are extensible via user-defined JSON files in `~/.atlas/pipelines/`

## Requirements

- Python 3.11 or higher
- Platform OAuth service account with read-only API access *(all tiers)*
- OS keyring backend available (macOS Keychain, GNOME Keyring, KWallet, or Windows Credential Locker), or Hashicorp Vault with KV v2 secrets engine *(all tiers)*
- SSH access to target nodes (key-based authentication recommended) *(Extended tier only)*
- MongoDB user with `clusterMonitor` (admin) + `read` on the Platform database *(Extended tier only)*
- Read-only Redis user with limited ACL permissions *(Extended tier only)*

## Install and Setup

Platform Atlas is distributed as two Python wheel packages — the core CLI and an optional WebUI. Install inside a dedicated virtual environment on the workstation you use to access your IAP environment.

```bash
# Create and activate a virtual environment
python3 -m venv atlas-venv
source atlas-venv/bin/activate

# Install the CLI (required)
pip3 install -U platform_atlas-<version>-py3-none-any.whl

# Install the WebUI (optional — adds the browser interface)
pip3 install -U platform_atlas_webui-<version>-py3-none-any.whl
```

Verify the installation:

```bash
platform-atlas --version
```

### Starting the WebUI

If you installed the WebUI wheel, launch it with:

```bash
platform-atlas-webui
```

This starts a local server and opens the interface in your browser. The WebUI shares the same `~/.atlas/` configuration directory as the CLI — no separate setup required.

### Credential Storage (OS Keyring)

Platform Atlas stores sensitive credentials (MongoDB, Redis, Platform OAuth, SSH passphrases, Gateway passwords) in your operating system's secure credential store via the `keyring` library — never in config files on disk. Each environment's secrets are scoped under `platform-atlas/<env-name>` so production credentials are completely isolated from dev/staging credentials.

| Platform | Backend used |
|---|---|
| macOS | Keychain (built-in) |
| Windows | Credential Locker (built-in) |
| Linux (desktop) | GNOME Keyring / KWallet via SecretService |
| Linux (headless) | Encrypted file backend (configure manually — see below) |

**Reconfigure or rotate credentials at any time:**

```bash
platform-atlas config credentials
```

This is the canonical command for adding, updating, or rotating any credential — Platform OAuth client secret, MongoDB/Redis URIs, SSH key passphrases, Gateway4 password, or Vault auth tokens. It writes to the active environment's keyring scope (or to your Vault backend, depending on `credential_backend`). Use it whenever:

- A credential has been rotated upstream and Atlas connections start failing
- You're switching backends (`keyring` ↔ `vault`)
- You set up a new SSH key with a passphrase
- You need to add a credential that wasn't collected during the original `env create` flow (e.g. retrofitting a Gateway4 password onto an existing environment)

You do **not** need to recreate the environment to update credentials.

### Credential Storage (Headless Servers)

Platform Atlas is generally to be used on a Workstation PC, but if needed to install on a server this can be done. On headless Linux servers without a desktop environment, the default keyring backend has no encryption. Atlas will warn you about this during setup if it detects an insecure backend.

To set up encrypted credential storage, install the following packages:

```bash
pip3 install keyring keyrings.alt pycryptodome SecretStorage
```

If `SecretStorage` fails to build, you may also need:

```bash
dnf install libsecret-devel python3-dbus
```

Then configure keyring to use the encrypted file backend:

```bash
mkdir -p ~/.config/python_keyring
cat > ~/.config/python_keyring/keyringrc.cfg << EOF
[backend]
default-keyring=keyrings.alt.file.EncryptedKeyring
EOF
```

You can verify the backend is active with:

```bash
python3 -c "import keyring; print(keyring.get_keyring())"
```

This should output `EncryptedKeyring`. The first time Atlas stores a credential, you will be prompted to create a master password for the encrypted keyring file. After that, run `platform-atlas config credentials` to populate the encrypted store with your environment's secrets.

### Credential Storage (Hashicorp Vault)

Platform Atlas can use Hashicorp Vault as a read-only credential backend instead of the OS keyring. In this mode, Atlas reads credentials from a KV v2 secrets engine at runetime but never writes to Vault - secrets are managed externally through the Vault UI, CLI, or API calls outside of Atlas.

Vault connection settings (URL, authe methd, token or AppRole credentials, mount path) are stored in the OS keyring under a `vault_` prefix, keeping them off disk entirely.

To configure Vault as the credential backend, run the setup wizard and select "vault" when prompted:
```bash
platform-atlas config init
```

Or reconfigure credentials for an existing installation:
```bash
platform-atlas config credentials
```

Atlas supports five Vault authentication methods, grouped by use case:

**Standard — credentials stored in the OS keyring:**

| Method | What to provide | When to use |
|---|---|---|
| **Token** | A static Vault token | Simple setups; token is long-lived or manually rotated |
| **AppRole** | `role_id` + static `secret_id` | Machine-to-machine auth; secret_id does not rotate |

**Automated / rotating credentials — no long-lived secret stored in Atlas:**

| Method | What to provide | When to use |
|---|---|---|
| **AppRole (Wrapped)** | `role_id` + response-wrapped token | Pipeline or Vault admin generates a fresh wrapped secret_id on a schedule; token is consumed on first use |
| **Token (file)** | Path to a token sink file | Vault Agent runs on the same host, renews the token continuously, and writes it to a file; Atlas reads the file at runtime |
| **Token (env)** | *(none stored)* | Pipeline or orchestrator sets `VAULT_TOKEN` before running Atlas; Atlas reads it at runtime |

For the **Token (file)** method, your Vault admin configures Vault Agent as a system service (e.g. a systemd unit) once. After that, token rotation is entirely hands-off — Atlas always finds a valid token in the file regardless of when it runs.

For the **Token (env)** method, ensure `VAULT_TOKEN` is set in the environment before invoking Atlas. In systemd, this is typically an `EnvironmentFile=` directive; in CI pipelines, a secret injection step.

Secrets should be stored in Vault as key-value pairs at the configured path (default: `secret/data/platform-atlas`):

| Vault Key | Description |
| ---|---|
| `platform_client_secret` | Platform OAuth client secret (required) |
| `mongo_uri` | MongoDB connection URI |
| `redis_uri` | Redis connection URI |
| `ssh_key_passphrase` | SSH key passphrase (if applicable) |

You can verify the backend is active with:
```bash
platform-atlas config show
```

The credential backend type will be displayed in the configuration output. Preflight checks will verify Vault connectivity and confirm that required secrets are present.

## Initial Setup

To configure Platform Atlas for the first time, run it without any arguments:

```bash
platform-atlas
```

If no configuration file exists, this will launch an interactive setup wizard with two phases:

1. **Global Settings** — Organization name, theme, and preferences that apply across all environments. Saved to `~/.atlas/config.json`.
2. **First Environment** — Connection details, credential backend, and deployment topology for your first target deployment. Saved to `~/.atlas/environments/<name>.json`.

You can also run the setup wizard directly at any time:

```bash
platform-atlas config init
```

Global settings are stored at `~/.atlas/config.json`. Environment-specific configuration is stored in `~/.atlas/environments/`. Credentials are stored separately in your OS keyring (scoped per environment under `platform-atlas/<env-name>`) or read from Hashicorp Vault if configured as the credential backend.

## Configuration

### Global Configuration

Global settings that apply across all environments are stored in `~/.atlas/config.json`:

```json
{
    "organization_name": "Acme Corp",
    "active_environment": "production",
    "verify_ssl": false,
    "dark_mode": true,
    "theme": "horizon-prism",
    "extended_validation_checks": true,
    "multi_tenant_mode": false,
    "debug": false
}
```

### Environment Configuration

Each environment file (`~/.atlas/environments/<n>.json`) contains the connection and deployment details for one target:

```json
{
    "name": "production",
    "organization_name": "Acme Corp",
    "description": "Production IAP cluster - US East",
    "platform_uri": "https://iap.acme.com:3443",
    "platform_client_id": "6920cb7d61910148410489f9",
    "credential_backend": "keyring",
    "deployment": {
        "mode": "standalone",
        "capture_scope": "primary_only",
        "nodes": [
            {
                "role": "all",
                "host": "iap-01.acme.com",
                "ssh_user": "atlas",
                "ssh_port": 22
            }
        ]
    }
}
```

When an environment is active, its fields are merged on top of the global config at load time. Credentials in the OS keyring are scoped to `platform-atlas/<env-name>`, keeping each environment's secrets isolated.

### Configuration Commands

```bash
platform-atlas config show              # Display current config (redacted)
platform-atlas config show --full       # Display config including secrets
platform-atlas config credentials       # Manage stored credentials
platform-atlas config deployment        # Reconfigure deployment topology
platform-atlas config theme             # Switch color theme
platform-atlas config doctor            # Run a configuration health check
```

`config doctor` is a one-shot diagnostic that verifies the global config, active environment, credential backend, Platform/Gateway URL reachability, active ruleset, and SSH key path in a single pass. Use it after `config init`, after editing an environment, or any time a capture fails for an unclear reason — it surfaces every issue at once rather than letting them appear one-by-one across multiple capture runs. Exits non-zero on warnings or errors so it composes cleanly with shell scripts and CI.

## Environments

Environments let you define and switch between multiple IAP deployments (dev, staging, production) without re-running setup. Each environment is a JSON file under `~/.atlas/environments/` with its own Platform URI, credentials, and deployment topology.

### Managing Environments

```bash
# List all environments (shows which is active)
platform-atlas env list

# Create a new environment (interactive wizard)
platform-atlas env create

# Create by copying an existing environment
platform-atlas env create staging --from production

# Switch the active environment
platform-atlas env switch staging

# Show details of an environment
platform-atlas env show production

# Edit an environment file in $EDITOR
platform-atlas env edit staging

# Remove an environment
platform-atlas env remove dev
```

### Overriding for a Single Command

Use the `--env` flag to target a specific environment without switching the global active:

```bash
platform-atlas --env dev preflight
platform-atlas --env staging session run capture
```

### Environment Resolution

When Atlas starts, the active environment is resolved in this order:

1. `--env` CLI flag (highest priority)
2. `ATLAS_ENV` environment variable
3. `active_environment` field in `config.json`
4. No environment — legacy mode using `config.json` directly

### Backward Compatibility

If no environments exist (e.g., an existing installation that predates this feature), Atlas uses `config.json` as-is — the same behavior as before. The environment system only activates when environments are explicitly created.

### Deployment Modes

Platform Atlas supports three deployment architectures:

- **Standalone** — Single-instance IAP with co-located or split MongoDB, Redis, and optional Gateway. Uses an `all` role for all-in-one nodes, or individual `iap`, `mongo`, `redis`, `iag` roles for split configurations.
- **HA2** — Highly Available deployments with 2+ IAP nodes, 3-node MongoDB replica set, 3-node Redis Sentinel cluster, and optional Gateway nodes.
- **Custom** — Free-form node list with manually assigned collector modules per node.

### Capture Scope

The `capture_scope` setting controls how many nodes the capture engine connects to:

- **primary_only** (default) — One node per role. Minimal connections for standard audits.
- **all_nodes** — Every node in the topology. Used when you need full coverage across all replicas and cluster members.

## Tiers

Starting in v1.7, Atlas operates in one of two modes:

| | Standard | Extended |
|---|---|---|
| **Collectors** | Platform OAuth, IAG4 API | All Standard + SSH, MongoDB, Redis, Kubernetes, Gateway5 |
| **Rules** | ~55 | ~108 |
| **SSH required** | No | Yes |
| **MongoDB / Redis required** | No | Yes |
| **Best for** | Application-layer audits, quick checks, restricted environments | Full infrastructure compliance audits |

Fresh installs default to **Standard**. Upgrades from 1.6.x default to **Extended** to preserve existing behavior.

### Tier Commands

```bash
platform-atlas tier show              # Show active tier and what is enabled
platform-atlas tier set standard      # Switch to Standard (non-interactive)
platform-atlas tier set extended      # Switch to Extended (non-interactive)
platform-atlas tier upgrade           # Guided Standard → Extended flow
platform-atlas tier downgrade         # Guided Extended → Standard flow
```

Use the `--tier` flag to override the tier for a single command without changing the persisted setting:

```bash
platform-atlas --tier standard session run capture
```

Sessions bind the active tier at creation time. The tier is displayed in session metadata and reports. Cross-tier diffs are flagged with a notice banner.

---

## Kubernetes Deployments

Kubernetes deployments use the `KubernetesCollector` instead of SSH-based collectors. Data comes from two sources: Helm `values.yaml` files (declarative config) and live `kubectl` commands (runtime state). No SSH access is required.

### Configuring a Kubernetes Environment

When creating an environment, select **Kubernetes** as the deployment mode. The setup wizard will prompt for:

| Field | Purpose |
|---|---|
| `values_yaml_path` | Path to the IAP Helm `values.yaml` |
| `iag5_values_yaml_path` | Path to the IAG5 `values.yaml` (optional) |
| `use_kubectl` | Enable live `kubectl` collection |
| `kubectl_context` | kubectl context name (leave blank for current context) |
| `kubectl_namespace` | Kubernetes namespace (default: `default`) |

These fields are also editable after setup via `platform-atlas env edit` or the WebUI **Environments** form.

### Data Source Priority

For Kubernetes environments, each data type is collected in this order, using the first source that succeeds:

1. **Platform OAuth API** — health, version, application status, runtime configuration
2. **kubectl `printenv`** (if `use_kubectl: true`) — live environment variables from inside a running IAP pod
3. **values.yaml** — declarative Helm configuration as a static fallback

MongoDB and Redis data requires the protocol collectors (pymongo / redis-py) to be reachable. If MongoDB and Redis are external managed services (e.g., AWS Atlas, ElastiCache), the Mongo and Redis rule categories will show as **SKIP** in the report — this is expected and not an error.

### kubectl Rule Fallbacks

The following rules have an `alt_path` that Atlas uses when the primary data source (Platform OAuth API) is unavailable. The `alt_path` data comes from `kubectl exec <pod> -- printenv` — the live `ITENTIAL_*` environment variables baked into the running pod.

**Group 1 — Pod environment variables (`ITENTIAL_*` → `platform.config_file.*`)**

| Rule | Primary path | kubectl alt_path | Pod env var |
|---|---|---|---|
| Platform Default User | `platform.config.default_user_enabled.value` | `platform.config_file.default_user_enabled` | `ITENTIAL_DEFAULT_USER_ENABLED` |
| Platform Core Logging Level | `platform.config.log_level.value` | `platform.config_file.log_level` | `ITENTIAL_LOG_LEVEL` |
| Server ID | `platform.config.server_id.value` | `platform.config_file.server_id` | `ITENTIAL_SERVER_ID` |
| Mongo Auth Enabled | `platform.config.mongo_auth_enabled.value` | `platform.config_file.mongo_auth_enabled` | `ITENTIAL_MONGO_AUTH_ENABLED` |
| Mongo TLS Enabled | `platform.config.mongo_tls_enabled.value` | `platform.config_file.mongo_tls_enabled` | `ITENTIAL_MONGO_TLS_ENABLED` |
| Log Max Files | `platform.config.log_max_files.value` | `platform.config_file.log_max_files` | `ITENTIAL_LOG_MAX_FILES` |
| Log File Max Size | `platform.config.log_max_file_size.value` | `platform.config_file.log_max_file_size` | `ITENTIAL_LOG_MAX_FILE_SIZE` |
| Webserver HTTPS Enabled | `platform.config.webserver_https_enabled.value` | `platform.config_file.webserver_https_enabled` | `ITENTIAL_WEBSERVER_HTTPS_ENABLED` |
| Webserver HTTP Enabled | `platform.config.webserver_http_enabled.value` | `platform.config_file.webserver_http_enabled` | `ITENTIAL_WEBSERVER_HTTP_ENABLED` |
| Webserver Timeout | `platform.config.webserver_timeout.value` | `platform.config_file.webserver_timeout` | `ITENTIAL_WEBSERVER_TIMEOUT` |

**Group 2 — kubectl system data (`system.kubernetes.*`)**

These fallbacks come from files and commands read directly inside the running pod, rather than environment variables. They are collected automatically whenever `use_kubectl: true` and an IAP pod is found.

| Rule | Primary path | kubectl alt_path | Source inside pod |
|---|---|---|---|
| Platform Version | `platform.health_server.version` | `system.kubernetes.platform_release_version` | `cat /opt/itential/platform/server/release_metadata.json` |
| Node Version | `platform.health_server.versions.node` | `system.kubernetes.node_version` | `node --version` |
| Gateway Manager Version Check | `platform.application_status.results.GatewayManager.version` | `system.kubernetes.installed_services.app-ag_manager.version` | `cat .../services/app-ag_manager/package.json` |

> **Debug logging:** When `--debug` is enabled (or `"debug": true` in `config.json`), Atlas logs every `kubectl` command it runs, the exit code, elapsed time, and any stderr output to `~/.atlas/atlas.log`. This makes it straightforward to verify which commands fired and whether they succeeded.

---

## ControlMaster Transport (CyberArk PSMP)

Some environments route SSH through a Privileged Access Management (PAM) gateway such as CyberArk PSMP, where direct key-based SSH to the target server isn't possible — authentication requires MFA (YubiKey OTP, RADIUS, etc.) and goes through a jump host rather than direct to the server.

Atlas supports **ControlMaster transport** for these environments. Instead of opening a fresh SSH connection for every collector, Atlas piggybacks on a single pre-authenticated SSH session that you open manually before running a capture. Once the master session is established and MFA is satisfied, Atlas multiplexes all of its connections through it with no further authentication prompts.

> **Scope:** ControlMaster applies to the **Platform (IAP) node only**. MongoDB and Redis nodes still use regular SSH — configure a direct-access user for them as described in the [SSH Setup Guide](GUIDES/SSH_SETUP_GUIDE.md).

### When to Use It

- Your IAP server is behind CyberArk PSMP or a similar PAM gateway
- SSH to the IAP node requires MFA (YubiKey, RADIUS, smart card) that Atlas cannot automate
- Direct key-based SSH to the IAP node is not permitted by policy

### Step 1: Open the ControlMaster Session

Before running Atlas, open the master connection once from your workstation. This is the step that triggers MFA — complete it interactively, then Atlas takes over:

```bash
ssh -M -S /tmp/atlas-cm.sock \
    -o ControlPersist=10m \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -fN user@target-host@psmp-gateway.example.com
```

| Flag | Purpose |
|---|---|
| `-M -S /tmp/atlas-cm.sock` | Create a ControlMaster socket at that path |
| `-o ControlPersist=10m` | Keep the socket alive for 10 minutes after the connection — enough for a full capture |
| `-fN` | Background the process (`-f`) and open no remote command (`-N`) |
| `user@target-host@psmp-gateway` | CyberArk PSMP format: `<user>@<target-ip-or-host>@<psmp-gateway-host>` |

You will be prompted for MFA (password, YubiKey tap, etc.) during this step. Once complete, the master session runs silently in the background.

**Verify the socket is working:**

```bash
ssh -S /tmp/atlas-cm.sock \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    user@target-host@psmp-gateway.example.com \
    "echo atlas-ok && whoami && hostname"
```

If you see `atlas-ok` plus the username and hostname, Atlas will be able to connect.

### Step 2: Configure Atlas

**CLI — during environment setup:**

When the setup wizard asks how Atlas should connect to the Platform (IAP) server, select **ControlMaster**. You will be prompted for:

- **Socket path** — path to the `-S` socket file (e.g. `/tmp/atlas-cm.sock`)
- **SSH destination** — the full destination string exactly as used in the `ssh -M` command (e.g. `user@target-host@psmp-gateway.example.com`)

**WebUI — in the Environment form:**

Under **Topology → Platform (IAP) connection type**, select **ControlMaster — CyberArk PSMP / jump host**. Two additional fields appear:
- **ControlMaster socket path** — same as above
- **SSH destination** — same as above

### Step 3: Run Atlas

With the master session open, run Atlas normally:

```bash
platform-atlas session run capture
```

Atlas will connect through the socket without prompting for MFA. If the socket has expired (closed or timed out), Atlas will print the exact `ssh -M` command to re-open it.

---

## The Workflow

Platform Atlas follows a structured sequence: **Preflight → Capture → Validate → Report**.

### 1. Preflight

Before capturing anything, verify that all configured connections are reachable:

```bash
platform-atlas preflight
```

This checks SSH connectivity to all target nodes, MongoDB and Redis access, Platform API reachability, and required file permissions. If something fails, it tells you what and why.

### 2. Create a Session

Everything runs inside a session. A session is a directory that holds your capture data, validation results, and reports together under a single name. When you create a session, you select an environment, ruleset, and profile — these are bound to the session so that switching sessions later restores the full context.

```bash
platform-atlas session create prod-audit-q1
```

The interactive wizard prompts you to select an environment, ruleset, and profile. You can also specify them directly:

```bash
platform-atlas session create prod-audit-q1 --env production --ruleset p6-master-ruleset --profile p6-prod-standalone-gateway4
```

To switch between sessions (also restores the bound environment, ruleset, and profile):

```bash
platform-atlas session switch
platform-atlas session list
platform-atlas session show prod-audit-q1
```

To edit session bindings before capture begins:

```bash
platform-atlas session edit
```

> **Note:** In versions before 1.5, environments, rulesets, profiles, and sessions were managed independently. Starting in v1.5, sessions bind all of these together — one switch, full context restored. The `ruleset setup`, `ruleset load`, and `ruleset profile set` commands still work for ad-hoc use, but the session creation wizard is the recommended workflow.

### 3. Capture

The capture engine connects to your targets and collects configuration data from all enabled modules:

```bash
platform-atlas session run capture
```

You will see a live progress display as each collector runs. If one module fails, the rest continue — you will not lose your entire capture because a single system timed out. Failed modules will prompt guided fallback collection unless skipped.

The capture order is always: **Preflight → Automated Capture → Manual Prompts → JSON Integrity Check → Customer Summary**.

To capture only specific modules:

```bash
platform-atlas session run capture --modules system mongo redis
```

To use fully manual collection (for air-gapped or restricted environments):

```bash
platform-atlas session run capture --manual
```

If you have pre-collected data files in a directory, you can batch-import them all at once instead of going through the interactive prompts:

```bash
platform-atlas session run capture --manual --import-dir ~/atlas-capture/
```

Atlas matches files by name and loads them automatically. Any files it doesn't recognize are skipped. You can re-run the same command after adding more files to the directory — progress is cumulative. See `MANUAL-COLLECTION-GUIDE.md` for the expected filenames and the commands to collect each file.

### 4. Validate

Validation runs your captured data through the loaded ruleset. Each rule has a type, operator, target path, and expected value. Rules can depend on other rules — if a dependency fails, the downstream rule is automatically skipped.

```bash
platform-atlas session run validate
```

Results are stored as `validation.parquet` in the session directory using Apache Arrow for efficient storage and retrieval.

Severity levels are **critical**, **warning**, and **info**.

### 5. Report

Generate a professional HTML report from the validation results:

```bash
platform-atlas session run report
```

Reports include compliant/non-compliant breakdowns by category and severity, detailed results with expected vs. actual values, extended validation findings, and session metadata. The report opens automatically in your default browser.

To generate in other formats:

```bash
platform-atlas session run report --format csv
platform-atlas session run report --format json
```

### 6. Reports

`session run report` generates all three HTML reports in a single pass:

| File | Contents |
|---|---|
| `03_report.html` | Compliance: overall score, category breakdown, rule results, extended validation findings |
| `04_operational.html` | Operational: platform/webserver/MongoDB log analysis and aggregation pipeline results |
| `05_arch.html` | Architecture & Maintenance: adapter states, Redis ACL, index status, IAG paths, and architecture overview |

The compliance report opens automatically in your browser. All three reports share a header navigation bar linking to each other.

**MongoDB Aggregation Pipelines**

After capture completes, Atlas prompts whether to run MongoDB aggregation pipelines for the operational report. These query live workflow and task data from the Platform database to produce execution statistics, top workflows, and runtime metrics. If you decline, the operational report still generates — it will contain log analysis only with a notice in the pipeline section.

You can extend the pipeline output by adding your own pipeline JSON files to `~/.atlas/pipelines/` — they are discovered and executed automatically.

### Run Everything at Once

To execute the full capture → validate → report pipeline in one command:

```bash
platform-atlas session run all
```

### Session Diff

Compare two sessions to see what changed between audits:

```bash
platform-atlas session diff baseline-q4 latest-q1
```

The diff report classifies each rule as Fixed, Regressed, Unchanged, New, Removed, Changed, or Skipped.

## Command Reference

### Session Commands

| Command | Description |
|---|---|
| `session create <n>` | Create a new session (binds environment, ruleset, and profile) |
| `session create <n> --env --ruleset --profile` | Create with explicit bindings (skips prompts) |
| `session list` | List all sessions with environment, org, ruleset, and status |
| `session show [name]` | Show session details and bindings |
| `session active [name]` | Show or set the active session (restores full context) |
| `session switch [name]` | Switch sessions (alias for active) |
| `session edit [name]` | Edit session bindings (only before capture) |
| `session run <stage>` | Run a workflow stage (capture, validate, report, all) |
| `session run capture --manual` | Interactive guided collection for air-gapped environments |
| `session run capture --manual --import-dir <dir>` | Batch import capture files from a directory |
| `session run report` | Generate all three HTML reports (compliance, operational, architecture) |
| `session export [name]` | Package session for delivery (zip or tar.gz) |
| `session delete <n>` | Permanently remove a session |
| `session diff <baseline> <latest>` | Compare two sessions |
| `session repair [name]` | Backfill missing metadata on pre-1.5 sessions |

### Ruleset Commands

| Command | Description |
|---|---|
| `ruleset setup` | Interactive ruleset and profile selection (recommended) |
| `ruleset list` | List available rulesets |
| `ruleset load <id>` | Load and activate a ruleset |
| `ruleset info [id]` | Show ruleset details |
| `ruleset active` | Show active ruleset |
| `ruleset clear` | Deactivate current ruleset |
| `ruleset rules [id]` | Display all rules in a ruleset |
| `ruleset profile list` | List available profiles |
| `ruleset profile set <id>` | Set a profile overlay |
| `ruleset profile active` | Show active profile |
| `ruleset profile clear` | Clear active profile |

### Config Commands

| Command | Description |
|---|---|
| `config init` | Run the interactive setup wizard |
| `config show` | Display current configuration (redacted) |
| `config credentials` | Manage keyring credentials |
| `config deployment` | Reconfigure deployment topology |
| `config theme` | Switch color theme |

### Environment Commands

| Command | Description |
|---|---|
| `env list` | List all environments with organization and active status |
| `env create [name]` | Create a new environment (interactive wizard) |
| `env create [name] --from <env>` | Copy from an existing environment |
| `env switch [name]` | Switch environment and offer to switch to a bound session |
| `env show [name]` | Show environment details |
| `env edit [name]` | Edit environment settings (org name, URIs, topology, etc.) |
| `env remove <n>` | Delete an environment |

### Tier Commands

| Command | Description |
|---|---|
| `tier show` | Show the active tier and what is enabled |
| `tier set <tier>` | Set the global tier (`standard` or `extended`) |
| `tier upgrade` | Guided Standard → Extended upgrade flow |
| `tier downgrade` | Guided Extended → Standard downgrade flow |

### Other Commands

| Command | Description |
|---|---|
| `preflight` | Run connectivity checks against all configured services |
| `guide` | View the built-in help guide |
| `--version` | Display version |
| `--debug` | Enable debug mode with verbose logging |
| `--env <name>` | Use a specific environment for this command |
| `--tier <tier>` | Override the active tier for this command only |

## Rulesets and Profiles

### Rulesets

A ruleset is a versioned JSON file containing an array of validation rules. Each rule defines a target path in the captured data, a validation type and operator, an expected value, and pass/fail messages.

Platform Atlas ships with the **Platform 6 Master Ruleset** (`p6-master-ruleset`) containing 105 rules across five categories:

| Category | Rules | Coverage |
|---|---|---|
| Platform | 47 | Application settings, adapters, services, properties |
| Gateway5 | 23 | IAG5 configuration, health, version checks |
| Redis | 16 | Server config, memory, persistence, replication, ACLs |
| Gateway4 | 11 | Venv packages, sync config, database settings |
| MongoDB | 8 | Server status, version, replication, connection settings |

Severity breakdown: 15 critical, 61 warning, 29 info.

There is also an included **IAP 2023.x Master Ruleset** (`2023-master-ruleset`) in the configuration file for IAP 2023.x Support for Atlas. Please see `3. Load a Ruleset and Profile` for more information on using this if needed.

### Profiles

Profiles are lightweight overlays that enable or disable specific rules from the master ruleset. This avoids maintaining separate ruleset copies for each environment type.

Available profiles for Platform 6:

| Profile | Description |
|---|---|
| `p6-prod-standalone-gateway4` | Production standalone with Gateway4 |
| `p6-prod-standalone-gateway5` | Production standalone with Gateway5 |
| `p6-prod-standalone-no-gateway` | Production standalone without Gateway |
| `p6-prod-ha2-gateway4` | Production HA2 with Gateway4 |
| `p6-prod-ha2-gateway5` | Production HA2 with Gateway5 |
| `p6-prod-ha2-no-gateway` | Production HA2 without Gateway |
| `p6-dev-standalone-gateway4` | Development standalone with Gateway4 |
| `p6-dev-standalone-gateway5` | Development standalone with Gateway5 |
| `p6-dev-standalone-no-gateway` | Development standalone without Gateway |

## Multi-Tenant Mode

Multi-tenant mode is designed for Itential Customer Success staff who manage capture data from multiple customer organizations. Enable it by setting `"multi_tenant_mode": true` in the configuration file.

Customer data is organized under `~/.atlas/customer-data/<organization>/` with session-based directories for each capture.

```bash
# Import a customer's capture file
platform-atlas customer import capture.json --organization "Acme Corp"

# List all customer organizations
platform-atlas customer list

# List sessions for an organization
platform-atlas customer sessions "Acme Corp"

# Validate a customer session
platform-atlas customer validate "Acme Corp" 2026-q1

# Generate a report for a customer session
platform-atlas customer report "Acme Corp" 2026-q1
```

## Required Permissions

Platform Atlas is designed to operate with read-only access. No write permissions are required on target systems. Sudo is not required, but if the SSH user has passwordless sudo available, Atlas will automatically use it as a fallback to read configuration files that are not readable by the SSH user directly. (e.g. `/etc/redis/redis.conf`).

### SSH

A read-only user with key-based authentication. Needs to read configuration files under `/opt/` and `/etc/`, and to run a limited set of commands: `hostname`, `uname`, `nproc`, `stat`, `realpath`, `cat`, `systemctl` (read-only), `sqlite3` (read-only), `python`, `pip`, `command`, `iagctl`, `printenv`, and `echo`.

Please see the separate guide entitled `SSH_SETUP_GUIDE` for full details on how to setup SSH access for all servers.

### MongoDB

A dedicated user created in the `admin` database with `clusterMonitor` for server diagnostics and `read` on the Platform database. Create it from mongosh:

```javascript
db.getSiblingDB("admin").createUser({
    user: "platformatlas",
    pwd: "securepassword",
    roles: [
        { role: "clusterMonitor", db: "admin" },
        { role: "read", db: "itential" }
    ]
})
```

The `clusterMonitor` role grants read-only access to `serverStatus`, `replSetGetStatus`, `dbStats`, and other diagnostic commands. The `read` role grants read access to collections in the Platform database. Neither role allows any write or destructive operations.

Your MongoDB URI must include `authSource=admin` since the user is created in the `admin` database:

```
mongodb://platformatlas:securepassword@mongo-host:27017/itential?authSource=admin
```

> **Note:** If your environment cannot grant `clusterMonitor`, Atlas will still work — server status and replica set metrics will be skipped, and the corresponding validation rules will show as SKIP in the report. The `read` role alone is sufficient for `dbStats` and collection-level checks.

### Redis

A user with the minimum required ACL permissions:

```
user platformatlas on >securepassword allcommands -@all +info +acl +ping +role +command +config|get
```

### Platform OAuth

A read-only Platform Service Account created under **Admin Essentials → Authorization → Clients** with the following permissions:

```
apiread:Adapters
apiread:Applications
apiread:Health
apiread:Indexes
apiread:Server
```

## Security

Platform Atlas is built with a security-first approach for enterprise environments:

- **Read-Only Operation** — All data collection is strictly read-only. No modifications are made to any target system.
- **Credential Isolation** — Sensitive credentials (MongoDB URI, Redis URI, Platform client secret, SSH passphrase) are stored in the OS keyring or Hashicorp Vault, never in configuration files. Vault integration is read-only from Atlas; secrets are managed externally.
- **Command Allowlisting** — SSH and local command execution is restricted to a hardcoded allowlist of safe commands. Shell metacharacters and injection patterns are blocked.
- **Path Validation** — File reads are restricted to allowed directory prefixes with traversal detection and symlink resolution.
- **Transport Security** — SSH host key verification with configurable policies. File size limits prevent reading excessively large files.
- **Configuration Permissions** — Config file permissions are checked on load with warnings for overly permissive access.
- **Data Redaction** — Session exports support automatic redaction of sensitive values.

## Themes

Platform Atlas uses the **Atlas Horizon** design system with semantic color tokens for consistent visual hierarchy across both terminal UI and HTML reports.

Available themes:

| Theme | Description |
|---|---|
| `horizon-dark` | Cyan and purple on dark background |
| `horizon-prism` | Teal and rose on deep indigo |
| `horizon-core` | Warm coral and amber sunset tones (default) |
| `horizon-light` | Light mode with teal and purple accents |

Switch themes interactively:

```bash
platform-atlas config theme
```

Or set directly in `~/.atlas/config.json`:

```json
{
    "dark_mode": true,
    "theme": "horizon-prism"
}
```

## Troubleshooting

Platform Atlas writes to a log file at `~/.atlas/atlas.log`. For more verbose output, enable debug logging:

```bash
# Via command-line flag
platform-atlas --debug session run capture

# Or permanently in config.json
{
    "debug": true
}
```

### Raw Capture Export (Authoring New Rules)

When `session run capture` finishes, Atlas writes `01_capture.json` — a copy of the captured data **pruned** to only the dot-paths the active ruleset references (plus a few passthrough sections for the operational/architecture reports). That keeps the file small but means any path the ruleset doesn't already use is invisible — which is awkward when you're trying to author a *new* rule and need to see what's actually available under, say, `mongo.config_file.systemLog.*`.

Atlas can also write an **unfiltered** companion file, `01_raw_capture.json`, containing the full reshaped capture **before** ruleset filtering. The shape is identical to `01_capture.json` — same nested keys, same dot-paths — just without the prune step, so every value the collectors returned is visible. Use it to find the exact dot-notation path you want to target, then add the new rule to your ruleset and re-run validate.

Two ways to enable the raw export (whichever you set, or both):

**Per-environment (persistent):** Set `debug_export_raw_capture: true` on the environment. Use `platform-atlas env edit`, choose `Debug: Export Raw Capture`, and toggle it on. Every capture against that environment will write `01_raw_capture.json` until you turn it off.

```bash
platform-atlas env edit production
# → select "Debug: Export Raw Capture" → on
```

You can also edit the environment file directly under `~/.atlas/environments/<name>.json`:

```json
{
    "name": "production",
    ...
    "debug_export_raw_capture": true
}
```

**Per-run (one-off):** Pass `--debug-raw-capture` to a single capture without changing the environment.

```bash
platform-atlas session run capture --debug-raw-capture
```

The flag takes effect for that run only and OR-combines with the env-level setting — turning the flag on never disables the env toggle, and an env toggle stays on across runs without the flag.

Once enabled, the file appears next to the regular capture:

```
~/.atlas/sessions/<session>/
├── 01_capture.json         # Filtered to ruleset paths (always written)
└── 01_raw_capture.json     # Unfiltered reshape (debug only)
```

Both flows work identically in the WebUI — the env toggle is editable from the Environment form and the raw file is written into the session directory whenever the toggle is on. The WebUI does not have a per-run override (no CLI flag context); use the env toggle there.

> **Disk note:** The raw file can be several times larger than the filtered one on large deployments — it contains every nested section the collectors returned. Leave the toggle off for routine audits and only flip it on while authoring rules.

### Common Issues

**"Config file not found"** — Run `platform-atlas config init` to create the initial configuration.

**"No ruleset loaded"** — If you created the session with v1.5+, switch to it with `platform-atlas session switch` to restore its bound ruleset and profile. For older sessions or manual control, use `platform-atlas ruleset setup` to interactively select a ruleset and profile.

**"Connection refused" during preflight** — Verify the target host is reachable, the correct port is configured, and the SSH user has key-based access.

**"Permission denied" on config file** — Set proper permissions: `chmod 600 ~/.atlas/config.json`.

**"Insecure keyring backend"** — Install a supported keyring backend. On headless Linux, install `gnome-keyring` or `kwallet`, or set the `PYTHON_KEYRING_BACKEND` environment variable. After the backend is in place, run `platform-atlas config credentials` to populate it with your environment's secrets — Atlas will not auto-migrate credentials from a previous insecure backend.

**"Authentication failed" / "401 Unauthorized" against Platform, MongoDB, Redis, or Gateway4** — A stored credential is wrong, expired, or rotated upstream. Run `platform-atlas config credentials` to update it; this rewrites the keyring entry for the active environment without recreating the environment file. Re-run `platform-atlas preflight` to verify.

**"No credential found for `<key>`"** — A required secret was never stored in the keyring (common after restoring `~/.atlas/` from a backup, switching machines, or initializing the encrypted backend on a headless server). Run `platform-atlas config credentials` and supply the missing value. The keyring is intentionally not part of `~/.atlas/`, so credentials never travel with the config directory.

**"Vault unreachable" or "Credential Backend Failed"** - Vault is configured as the credential backend but Atlas cannot connect. Verify that Vault is running and the URL is correct. For **token** auth, check that the token hasn't expired. For **approle**, verify both `role_id` and `secret_id` are valid. For **approle_wrapped**, the wrapping token may have already been used or expired — obtain a new one and run `platform-atlas config credentials`. For **token_file**, verify that Vault Agent is running and has written a token to the configured sink path. For **token_env**, verify that `VAULT_TOKEN` is set in the environment before running Atlas. Run `platform-atlas preflight` to diagnose.

**"Missing credentials" with Vault backend** - The required secrets are not present at the configured Vault path. Add them at the path shown in the error message (default: `secret/data/platform-atlas`) using the Vault CLI or UI.

**Capture module fails but others succeed** — This is by design. Platform Atlas continues collecting from remaining modules. Use `--skip-guided` to suppress fallback prompts, or provide the missing data through guided collection. For bulk import of pre-collected files, use `--manual --import-dir <directory>`.

**"Environment not found"** — The environment specified by `--env`, `ATLAS_ENV`, or `active_environment` in config.json doesn't exist in `~/.atlas/environments/`. Run `platform-atlas env list` to see available environments, or `platform-atlas env create` to set one up.

## Directory Structure

```
~/.atlas/
├── config.json                     # Global configuration (no secrets)
├── settings.json                   # Active ruleset and profile pointers
├── atlas.log                       # Application log
├── environments/                   # Named deployment targets
│   ├── production.json
│   ├── staging.json
│   └── dev.json
├── sessions/                       # Audit sessions
│   └── prod-audit-q1/
│       ├── session.json            # Session metadata (bound env, ruleset, profile, org)
│       ├── 01_capture.json         # Captured configuration data (filtered to ruleset paths)
│       ├── 01_raw_capture.json     # Optional: unfiltered reshape — written only when
│       │                           #   debug_export_raw_capture is on (env or --debug-raw-capture)
│       ├── 02_validation.parquet   # Validation results
│       ├── 03_report.html          # Compliance report
│       ├── 04_operational.html     # Operational report (logs + pipeline metrics)
│       └── 05_arch.html            # Architecture & Maintenance report
├── pipelines/                      # Operational report pipeline definitions
│   └── topworkflows.json
└── customer-data/                  # Multi-tenant customer data
    └── acme-corp/
        └── 2026-q1/
            ├── 01_capture.json
            ├── 02_validation.parquet
            └── 03_report.html
```

## Upgrading

### From 1.6.x → 1.7

All existing sessions, environments, and captures continue to work without changes. Your installation is automatically assigned **Extended** tier on first run, which preserves the full infrastructure audit behavior from 1.6.x.

If you want to switch to Standard tier for application-only audits, run:

```bash
platform-atlas tier set standard
```

The optional WebUI can be installed independently — it does not affect CLI behavior.

### From Pre-1.5

Starting in v1.5, sessions bind an environment, ruleset, and profile at creation time. Switching sessions restores the full context automatically — no more separately managing `env switch`, `ruleset load`, and `ruleset profile set`.

Existing sessions and environments continue to work without changes. Sessions created before 1.5 won't have bound metadata, but you can backfill it from capture data with `platform-atlas session repair`. Environments can be updated with `platform-atlas env edit` to add an organization name.

## Support

For issues, questions, or feature requests, please refer to the official Itential support channels:

- **Itential Documentation — Get Support:** <https://docs.itential.com/itential-platform/resources/get-support>

## License

This project is licensed under the GNU General Public License v3.0. See the [LICENSE](LICENSE) file for details.

---

**Version:** 1.7.2
**Author:** Cody Rester
**Last Updated:** May 2026