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
  - [Air-Gapped / Offline Install](#air-gapped--offline-install)
- [Initial Setup](#initial-setup)
- [Configuration](#configuration)
- [Environments](#environments)
- [Tiers](#tiers)
- [Kubernetes Deployments](#kubernetes-deployments)
- [ControlMaster Transport (CyberArk PSMP)](#controlmaster-transport-cyberark-psmp)
- [The Workflow](#the-workflow)
- [Command Reference](#command-reference)
- [Rulesets and Profiles](#rulesets-and-profiles)
- [Required Permissions](#required-permissions)
- [Security](#security)
- [Themes](#themes)
- [Troubleshooting](#troubleshooting)
- [Upgrading from Pre-1.5](#upgrading-from-pre-15)
- [Support](#support)
- [License](#license)

---

## Features

- **Three Audit Tiers** — **Standard** (Platform OAuth + IAG4 API only, ~55 rules, no SSH/MongoDB/Redis), **Extended** (full infrastructure audit via SSH, MongoDB, Redis, Kubernetes, and Gateways, ~122 rules), and **SaaS** (a single standalone Gateway — GW4 *or* GW5 — with no Platform/MongoDB/Redis at all). Switch Standard ⇄ Extended any time with `platform-atlas tier set`; SaaS is chosen per-environment at create time.
- **Automated Data Collection** — Connects via SSH, MongoDB, Redis, and Platform OAuth to capture configuration data from all components of an IAP deployment. If passwordless sudo is available, Atlas will automatically use it to read configuration files that the SSH user cannot access directly.
- **Optional WebUI** — Browser-based interface (`platform-atlas-webui` wheel) for managing sessions, running captures, streaming live job output, and browsing reports — no CLI knowledge required.
- **Multiple Rulesets** — Select from versioned, JSON schema-validated rulesets tailored to specific platform versions (e.g., Platform 6)
- **Ruleset Profiles** — Environment-specific overlays (standalone, HA2, dev, prod, gateway4, gateway5) that enable or disable rules from the master ruleset
- **~122 Validation Rules (Extended) / ~55 (Standard)** — Covering Platform, Gateway4, Gateway5, Redis, MongoDB, and Kubernetes across critical, warning, and info severity levels
- **Extended Validation** — Decorator-based checks that run outside the standard ruleset structure for health, adapter, and version analysis
- **Rule Chaining** — Rules can depend on other rules, so downstream checks are automatically skipped when a dependency fails
- **Dynamic Rules** — Limited computed values inside rules for proper comparison against runtime configuration
- **Professional HTML Reports** — Full HTML/CSS/JS reports with the Atlas Horizon design system, supporting dark and light themes with W3C-compliant markup
- **Session Diff Engine** — Compare two audit sessions side-by-side to track configuration drift, regressions, and fixes over time
- **Session Management** — Organize captures, validations, and reports into named sessions with metadata tracking. Each session binds an environment, ruleset, tier, and profile at creation — switching sessions restores the full context automatically
- **Guided Manual Collection** — Interactive fallback prompts for environments where automated capture cannot reach certain components. Supports batch directory import (`--import-dir`) for importing all pre-collected files at once without interactive prompts — re-runnable to incrementally add data.
- **Named Environments** — Define multiple deployment targets (dev, staging, production) as independent environment files, each with its own organization name, connection details, topology, and scoped credentials
- **Deployment Topology** — Models standalone, HA2, and custom deployment architectures with configurable capture scope
- **Secure Credential Storage** — Three explicit backends chosen at setup: the OS keyring (macOS Keychain, Windows Credential Locker, Linux Secret Service), an encrypted local file (AES-256-GCM, for headless servers), or Hashicorp Vault — scoped per environment, never in config files
- **Air-Gapped Support** — Operates entirely offline after installation; no internet access required for capture, validation, or reporting
- **Multiple Export Formats** — HTML, CSV, and JSON report output with session export and redaction support
- **Three-Report System** — `session run report` always generates all three HTML reports in a single pass: `03_report.html` (compliance), `04_operational.html` (logs + MongoDB pipelines), and `05_arch.html` (architecture & maintenance); all three share a header nav bar and the compliance report opens automatically in the browser
- **Unified Report (preview)** — opt-in `session run report --unified` also writes a single standalone `unified_report.html` folding Compliance, Operational, and Architecture into one tabbed file; purely additive, never replaces the classic reports
- **MongoDB Aggregation Pipelines** — After capture, Atlas prompts whether to run aggregation pipelines against the Platform's MongoDB database for the operational report; pipeline definitions are extensible via user-defined JSON files in `~/.atlas/pipelines/`

## Requirements

- Python 3.11 or higher
- Platform OAuth service account with read-only API access *(all tiers)*
- A credential store: OS keyring (macOS Keychain, GNOME Keyring, KWallet, Windows Credential Locker), an encrypted local file (for headless servers with no keyring), or HashiCorp Vault with KV v2 *(all tiers)*
- SSH access to target nodes (key-based authentication recommended) *(Extended tier only)*
- MongoDB user with `clusterMonitor` (admin) + `read` on the Platform database *(Extended tier only)*
- Read-only Redis user with limited ACL permissions *(Extended tier only)*

## Install and Setup

Wheel packages for every release are available on the [GitHub releases page](https://github.com/itential/platform-atlas/releases).

Platform Atlas is distributed as two Python wheel packages — the core CLI and an optional WebUI. Install inside a dedicated virtual environment on the workstation you use to access your IAP environment.

```bash
# Create and activate a virtual environment
python3 -m venv atlas-venv
source atlas-venv/bin/activate

# Install the CLI (required)
pip3 install platform_atlas-<version>-py3-none-any.whl

# Install the WebUI (optional — adds the browser interface)
pip3 install platform_atlas_webui-<version>-py3-none-any.whl
```

Verify the installation:

```bash
platform-atlas --version
```

### Air-Gapped / Offline Install

If the target server has no internet access, build a self-contained bundle on an internet-connected Mac or Windows workstation and transfer it to the server. The entire process takes just a few minutes.

**Step 1 — On the internet-connected machine**

Download the Platform Atlas wheel from the GitHub release page, then run a single `pip download` command to pull all dependencies as pre-built Linux binaries:

```bash
# macOS / Linux
mkdir atlas-bundle

pip download ./platform_atlas-<version>-py3-none-any.whl \
  --only-binary=:all: \
  --python-version 3.11 \
  --platform manylinux_2_28_x86_64 \
  --dest atlas-bundle/
```

> **Windows:** Open Command Prompt or PowerShell and run the same command on a single line (drop the backslash line continuations).

The `--platform manylinux_2_28_x86_64` flag is the key — it tells pip to download RHEL 8/9-compatible Linux wheels even though your workstation is a Mac or Windows machine. All transitive dependencies are resolved and downloaded automatically. The `--only-binary=:all:` flag ensures you get pre-built wheels rather than source packages that would require a compiler on the server.

If you also want the optional WebUI, run a second download command in the same folder:

```bash
pip download ./platform_atlas_webui-<version>-py3-none-any.whl \
  --only-binary=:all: \
  --python-version 3.11 \
  --platform manylinux_2_28_x86_64 \
  --dest atlas-bundle/
```

Zip the folder when done:

```bash
zip -r atlas-bundle.zip atlas-bundle/
```

> **Windows:** Right-click the `atlas-bundle` folder and choose **Send to → Compressed (zipped) folder**, or use 7-Zip.

**Step 2 — Transfer to the server**

Copy `atlas-bundle.zip` to the target server using USB, SCP through a jump host, or whatever your air-gap transfer method is. The bundle is typically 100–200 MB (300–400 MB with the WebUI).

**Step 3 — On the RHEL 9 server**

Python 3.11 must already be installed on the server (it is a prerequisite for Platform Atlas). Then:

```bash
unzip atlas-bundle.zip
cd atlas-bundle

# Create and activate a virtual environment
python3.11 -m venv ~/.atlas-env
source ~/.atlas-env/bin/activate

# Install Atlas and all dependencies from the local folder — no network access required
pip install --no-index --find-links . platform_atlas-*.whl

# If you also included the WebUI:
pip install --no-index --find-links . platform_atlas_webui-*.whl

# Verify
platform-atlas --version
```

The `--no-index` flag disables PyPI entirely and `--find-links .` tells pip to resolve everything from the current directory. No network access is made during this step.

### Upgrading

When a new version is available, use `--force-reinstall` and `--no-compile` instead of a plain `pip install -U`:

```bash
pip install --force-reinstall --no-compile platform_atlas-<version>-py3-none-any.whl

# If you also use the WebUI:
pip install --force-reinstall --no-compile platform_atlas_webui-<version>-py3-none-any.whl
```

**Why not just `pip install -U`?** A plain upgrade can leave stale `.pyc` bytecode files from the previous version in place — especially if a module was renamed or removed between releases. On some filesystems (NFS, RHEL with coarse timestamp resolution) these stale files are not detected as out of date and can cause confusing import errors or silently run old code. `--force-reinstall` does a full file replacement; `--no-compile` skips pre-generating bytecode so Python regenerates it fresh on the first run instead.

> **Note on first-run speed after upgrade:** Because `--no-compile` defers bytecode compilation, the very first run after upgrading will be slightly slower than usual while Python builds the cache. This is a one-time cost — every subsequent run is normal speed.

### Starting the WebUI

If you installed the WebUI wheel, launch it with:

```bash
platform-atlas-webui
```

This starts a local server and opens the interface in your browser. The WebUI shares the same `~/.atlas/` configuration directory as the CLI — no separate setup required.

### Credential Storage

Platform Atlas stores sensitive credentials (MongoDB, Redis, Platform OAuth, SSH passphrases, Gateway passwords) in one of three explicit backends you choose at setup — never in config files on disk. The **OS Keyring is the recommended default**; an **encrypted local file** and **HashiCorp Vault** are the other two. Atlas uses exactly the store you pick and never auto-switches between them.

With the OS keyring, each environment's secrets are scoped under `platform-atlas/<env-name>` so production credentials are completely isolated from dev/staging credentials.

| Platform | OS keyring backend used |
|---|---|
| macOS | Keychain (built-in) |
| Windows | Credential Locker (built-in) |
| Linux (desktop) | GNOME Keyring / KWallet via SecretService |
| Linux (headless) | No usable OS keyring — choose the encrypted local file store (see below) |

**Reconfigure or rotate credentials at any time:**

```bash
platform-atlas config credentials
```

This is the canonical command for adding, updating, or rotating any credential — Platform OAuth client secret, MongoDB/Redis URIs, SSH key passphrases, Gateway4 password, or Vault auth tokens. It writes to the active environment's keyring scope (or to your Vault backend, depending on `credential_backend`). Use it whenever:

- A credential has been rotated upstream and Atlas connections start failing
- You're switching backends (`keyring`, `file`, or `vault`)
- You set up a new SSH key with a passphrase
- You need to add a credential that wasn't collected during the original `env create` flow (e.g. retrofitting a Gateway4 password onto an existing environment)

You do **not** need to recreate the environment to update credentials.

### Encrypted Local File Backend

The encrypted local file is a first-class backend you choose explicitly at setup — not an automatic fallback. Pick it on a headless Linux server with no D-Bus session, or anywhere you'd rather keep secrets off the OS keyring. Atlas stores credentials in a machine-bound file at `~/.atlas/credentials.enc`. When you choose Vault, this is also where Vault's own *connection settings* can live if you'd rather not keep them in the keyring.

- **Encryption at rest:** AES-256-GCM with a key derived from this host and user plus a random per-install salt stored separately in `~/.atlas/.keysalt`. The file is non-portable (it won't decrypt on another host or user account) and a single leaked file is useless without the salt. Files are written `0o600`.
- **Honest reporting:** preflight, `config doctor`, the banner, and `config credentials` all show when the file store is active — it's reported as a warning, never claimed to be an encrypted keyring.
- **Atlas never auto-switches** — the store you choose at setup is the store it uses on every run. Switching later happens only when you run an explicit command (below); nothing migrates on its own.

**Switch an existing environment's backend (deliberately):**

```bash
platform-atlas config credentials --use-file-store   # switch this environment to the encrypted file
platform-atlas config credentials --use-keyring      # switch it back to the OS keyring
```

Switching re-enters the environment's secrets into the new store — nothing is silently copied between backends, so a credential is only ever where you put it.

### Credential Storage (Headless Servers)

Platform Atlas is generally used on a workstation PC, but it can be installed on a server. On headless Linux servers without a desktop environment, the OS keyring usually can't store secrets securely — so, as described above, you explicitly choose the **encrypted local file store** at setup (the wizard flags whether the keyring works on this host) and capture just works. No manual keyring configuration is required.

If you would instead prefer an encrypted **OS keyring** on the server (rather than the local file store or Vault), you can configure one by hand. Install the following packages:

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

**Alternative: per-shell override via environment variable**

If you see the error `Environment variable DBUS_SESSION_BUS_ADDRESS is unset`, the keyring is trying to reach a D-Bus session bus that does not exist on a headless server. Instead of writing `keyringrc.cfg`, you can select a file backend directly with the `PYTHON_KEYRING_BACKEND` environment variable. This takes effect for the current shell — add it to `~/.bashrc` to persist it:

```bash
# Encrypted file backend — prompts for a master password on first write (recommended)
export PYTHON_KEYRING_BACKEND=keyrings.alt.file.EncryptedKeyring

# Unencrypted file backend — no prompt, fastest way to unblock, but credentials
# are stored in cleartext. Lock the file down afterward:
#   chmod 600 ~/.local/share/python_keyring/keyring_pass.cfg
export PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
```

Keep the same `export` set in the shell that runs `platform-atlas config credentials` **and** `platform-atlas session run capture`, so credentials are written to and read from the same backend. For fully non-interactive runs (no TTY to enter a master password), prefer `PlaintextKeyring` with tightened file permissions, or use the Hashicorp Vault backend below.

### Credential Storage (Hashicorp Vault)

Platform Atlas can use Hashicorp Vault as a read-only credential backend instead of the OS keyring. In this mode, Atlas reads credentials from a KV v2 secrets engine at runetime but never writes to Vault - secrets are managed externally through the Vault UI, CLI, or API calls outside of Atlas.

Vault's own connection settings (URL, auth method, token or AppRole credentials, mount path) need a local home — Atlas asks where to keep them when you choose Vault, with the **OS keyring recommended** (stored under a `vault_` prefix, off disk). The encrypted local file is the alternative when the keyring isn't usable on the host.

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
    "tier": "standard",
    "verify_ssl": false,
    "dark_mode": true,
    "theme": "horizon-prism",
    "extended_validation_checks": true,
    "credential_backend": "keyring",
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

Atlas operates in one of three modes:

| | Standard | SaaS | Extended |
|---|---|---|---|
| **Audits** | Platform application layer | One standalone Gateway (GW4 *or* GW5) | Full Platform deployment |
| **Collectors** | Platform OAuth, IAG4 API | Gateway API + gateway SSH (optional for GW4), or a local Compose/Helm file for GW5 | All Standard + SSH, MongoDB, Redis, Kubernetes, Gateway5 |
| **Rules** | ~55 | Gateway categories only (~11 GW4 / ~27 GW5) | ~108 |
| **SSH required** | No | As needed (GW4 API-only works) | Yes |
| **Platform / MongoDB / Redis** | Platform only | **None** | Yes |
| **Best for** | Application-layer audits, quick checks, restricted environments | SaaS/cloud customers running a standalone gateway | Full infrastructure compliance audits |

Fresh installs default to **Standard**. Upgrades from 1.6.x default to **Extended** to preserve existing behavior. **SaaS** is chosen per-environment at create time (`platform-atlas env create`) — it is never a global default, and a SaaS environment's tier and gateway kind are fixed for its lifetime. A SaaS audit produces a **single report file**: the compliance report with the Architecture Overview merged in.

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

Sessions bind the active tier at creation time. The tier is displayed in session metadata and reports. Cross-tier diffs are flagged with a notice banner. `tier upgrade`/`downgrade` apply to Standard ↔ Extended only — SaaS environments are created as SaaS and stay SaaS (create a new environment to change direction).

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

## Gateway 5 Configuration Sources

When an environment includes an Automation Gateway 5, Atlas reads its `GATEWAY_*` settings from one of four sources, chosen during `env create` / `env edit`:

- **SSH `printenv`** — live environment variables from the running gateway host (default).
- **Docker Compose file** — parses the `environment:` block of a local compose file; no SSH needed for containerized gateways.
- **Helm values file** — parses `env` / `extraEnv` and the IAG5 chart's `serverSettings` / `applicationSettings` / `runnerSettings`.
- **Server `gateway.conf` over SSH** — reads the IAG5 server config file (INI); only a `server`-mode file is accepted.

All four feed the same `gateway5.*` rules, so results are identical regardless of source. File and server-config sources are validated during preflight.

---

## ControlMaster Transport (CyberArk PSMP)

Some environments route SSH through a Privileged Access Management (PAM) gateway such as CyberArk PSMP, where direct key-based SSH to the target server isn't possible — authentication requires MFA (YubiKey OTP, RADIUS, etc.) and goes through a jump host rather than direct to the server.

Atlas supports **ControlMaster transport** for these environments. Instead of opening a fresh SSH connection for every collector, Atlas piggybacks on a single pre-authenticated SSH session that you open manually before running a capture. Once the master session is established and MFA is satisfied, Atlas multiplexes all of its connections through it with no further authentication prompts.

ControlMaster can be set on **any node** in the topology — not just IAP. Each role (Platform/IAP, MongoDB, Redis, Gateway) has its own independent socket. In HA2, only the **primary node** of each role needs an open socket; non-primary nodes are never SSH-connected under the default `primary_only` capture scope.

### When to Use It

- Any IAP or gateway server is behind CyberArk PSMP or a similar PAM gateway
- SSH requires MFA (YubiKey, RADIUS, smart card) that Atlas cannot automate
- Direct key-based SSH to a node is not permitted by policy

### Step 1: Open the ControlMaster Sessions

Before running Atlas, open one master connection per role from your workstation. Each is the step that triggers MFA — complete it interactively, then Atlas takes over.

Atlas defaults to short role-based socket names stored under `~/.atlas/sockets/`:

```bash
# Platform / IAP node
ssh -M -S ~/.atlas/sockets/platform-01.sock \
    -o ControlPersist=10m \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -fN user@iap-host@psmp-gateway.example.com

# MongoDB primary (Extended tier only)
ssh -M -S ~/.atlas/sockets/mongo-01.sock \
    -o ControlPersist=10m \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -fN user@mongo-host@psmp-gateway.example.com

# Redis primary (Extended tier only)
ssh -M -S ~/.atlas/sockets/redis-01.sock \
    -o ControlPersist=10m \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -fN user@redis-host@psmp-gateway.example.com
```

| Flag | Purpose |
|---|---|
| `-M -S <socket>` | Create a ControlMaster socket at that path |
| `-o ControlPersist=10m` | Keep the socket alive for 10 minutes — enough for a full capture |
| `-fN` | Background the process (`-f`) and open no remote command (`-N`) |
| `user@target@psmp-gateway` | CyberArk PSMP format: `<user>@<target-ip-or-host>@<psmp-gateway-host>` |

You will be prompted for MFA during each step. Once complete, the master sessions run silently in the background.

**Check socket health at any time:**

```bash
platform-atlas env sockets <env-name>
```

This shows the status of every ControlMaster socket — open, stale (file exists but master not responding), missing, or unconfigured.

**Clean up stale sockets (master timed out or was closed):**

```bash
platform-atlas env sockets <env-name> --clean
```

This removes stale socket files so you can re-open fresh master connections without the "socket exists but is not responding" error.

**Let Atlas open the master connections for you:**

```bash
platform-atlas env sockets <env-name> --open
```

Atlas runs the `ssh -M` command for each missing or stale node sequentially — the terminal is handed over so you can complete MFA, approve a Duo push, or enter a password inline. After each node authenticates and SSH forks to the background, Atlas re-checks the socket and reports the result before moving to the next node.

### Step 2: Configure Atlas

**CLI — during environment setup:**

When the setup wizard asks how Atlas should connect to a node, select **ControlMaster**. You will be prompted for:

- **Socket path** — path to the `-S` socket file (default auto-generated, e.g. `~/.atlas/sockets/platform-01.sock`)
- **SSH destination** — the full destination string exactly as used in the `ssh -M` command (e.g. `user@target-host@psmp-gateway.example.com`)
- **Port** — the SSH port for the master connection (default 22)

You can leave the SSH destination blank to skip a node and configure it later — the wizard will warn but continue. Fill it in afterward with `env edit` → Deployment Topology → Edit a node.

**WebUI — in the Environment form:**

Under **Topology → [node] connection type**, select **ControlMaster — CyberArk PSMP / jump host**. Two additional fields appear for the socket path and SSH destination.

**Editing an individual node after setup:**

```bash
platform-atlas env edit <env-name>
# → Deployment Topology
# → Edit a node
# → pick the node, then change hostname / transport / socket / SSH destination
```

This avoids re-running the full topology wizard just to fix one node.

### Step 3: Run Atlas

With the master sessions open, run Atlas normally:

```bash
platform-atlas session run capture
```

If some sockets are closed or stale when capture starts, Atlas shows a status table and offers three choices: open them automatically (Atlas runs the SSH command and hands you the terminal for MFA), show the exact copy-paste commands, or proceed anyway. Nodes without open sockets fail individually and do not abort the entire capture; re-open them and re-run with `--resume` to pick up where it left off.

### Troubleshooting ControlMaster

| Symptom | Cause | Fix |
|---|---|---|
| `socket exists but is not responding` | Stale socket file — master timed out | `platform-atlas env sockets <name> --clean`, then re-open the master |
| `filename too long` / `bind error` | Socket path exceeds POSIX 104-byte limit | Use the default short paths under `~/.atlas/sockets/` |
| `SSH destination not configured for node X` | Node saved without an SSH destination | `env edit` → Deployment Topology → Edit a node → CM SSH destination |
| Socket status shows `missing` at capture time | Master was never opened for this session | Run the `ssh -M -S …` command shown by `env sockets` |

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

> **Unified report (preview):** add `--unified` to also write a single standalone `unified_report.html` that folds all three into one tabbed page (additive — the classic `03/04/05` files are still written). A **SaaS** audit instead produces a single merged `03_report.html` (compliance + Architecture Overview).

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
| `session run report --unified` | Also emit a single tabbed `unified_report.html` (preview; additive) |
| `session export [name]` | Package the full report set + JSON + metadata into a delivery archive (`ATLAS-<org>-<session>-<date>`); `--include-debug` adds capture/logs |
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
| `ruleset update` | Fetch and apply SHA-256-verified ruleset updates from the manifest |
| `ruleset skip-rule <n> --reason` | Suppress a rule in the active environment (shown as Suppressed; reason required) |
| `ruleset unskip-rule <n>` | Remove a per-environment rule suppression |
| `ruleset profile list` | List available profiles |
| `ruleset profile set <id>` | Set a profile overlay |
| `ruleset profile active` | Show active profile |
| `ruleset profile clear` | Clear active profile |

### Config Commands

| Command | Description |
|---|---|
| `config init` | Run the interactive setup wizard |
| `config show` | Display current configuration (redacted) |
| `config edit` | Tune individual settings (input mode, log retention, timeouts) without hand-editing config.json |
| `config credentials` | Add, rotate, or switch the backend for stored credentials |
| `config deployment` | Reconfigure deployment topology |
| `config theme` | Switch color theme |
| `config doctor` | Run a one-shot configuration health check |
| `config architecture` | Record/update infrastructure architecture info (alias of `env architecture`) |

### Environment Commands

| Command | Description |
|---|---|
| `env list` | List all environments with organization and active status (`⚠ incomplete` shown for partial setups) |
| `env create [name]` | Create a new environment (interactive wizard; saves a draft early so Ctrl-C doesn't lose your work) |
| `env create [name] --from <env>` | Copy from an existing environment |
| `env switch [name]` | Switch environment and offer to switch to a bound session |
| `env show [name]` | Show environment details |
| `env edit [name]` | Edit environment settings; Deployment Topology opens a sub-menu: edit a node, change capture scope, or replace topology |
| `env architecture [name]` | Record/update architecture info for the report (browser or CLI form) |
| `env sockets [name]` | Show ControlMaster socket health (open / stale / missing / unconfigured) |
| `env sockets [name] --clean` | Remove stale socket files so fresh master connections can be opened |
| `env sockets [name] --open` | Open master connections for all missing/stale nodes (Atlas runs SSH, you provide credentials inline) |
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
| `support-bundle` | Collect a diagnostic ZIP (health endpoints, redacted config, Extended logs) for a support ticket |
| `continuous-audit` | Schedule recurring audits with alerting and run history |
| `fleet` | Multi-environment health summary (pass rate, drift, continuous-audit state) |
| `guide` | View the built-in help guide |
| `--version` | Display version |
| `--debug` | Enable debug mode with verbose logging |
| `--env <name>` | Use a specific environment for this command |
| `--tier <tier>` | Override the active tier for this command only |

## Rulesets and Profiles

### Rulesets

A ruleset is a versioned JSON file containing an array of validation rules. Each rule defines a target path in the captured data, a validation type and operator, an expected value, and pass/fail messages.

Platform Atlas ships with the **Platform 6 Master Ruleset** (`p6-master-ruleset`) containing 122 rules across six categories:

| Category | Rules | Coverage |
|---|---|---|
| Platform | 49 | Application settings, adapters, services, properties |
| Gateway5 | 25 | IAG5 configuration, health, version checks |
| Redis | 16 | Server config, memory, persistence, replication, ACLs |
| Kubernetes | 15 | Probes, resource requests/limits, HPA, restart counts |
| Gateway4 | 11 | Venv packages, sync config, database settings |
| MongoDB | 6 | Server status, version, replication, connection settings |

Severity breakdown: 18 critical, 79 warning, 25 info.

There is also an included **IAP 2023.x Master Ruleset** (`20231-master-ruleset`) in the configuration file for IAP 2023.x Support for Atlas. Please see `3. Load a Ruleset and Profile` for more information on using this if needed.

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

#### MongoDB Log File Access

Atlas reads the MongoDB log file for operational analysis. MongoDB logs are typically owned by the `mongod` user with `0600` permissions, so the SSH user needs explicit read access. Two options:

**Option A — Filesystem ACL (recommended):** Grants access without touching the existing ownership or permissions. The default ACL on the directory ensures new files created by logrotate inherit it automatically.

```bash
setfacl -m u:<ssh_user>:rx /var/log/mongodb/
setfacl -d -m u:<ssh_user>:r /var/log/mongodb/
setfacl -m u:<ssh_user>:r /var/log/mongodb/mongod.log
```

**Option B — Logrotate + group membership:** Add the SSH user to the `mongod` group and configure logrotate to create files as group-readable so the permission survives rotation.

```bash
usermod -aG mongod <ssh_user>
chmod 640 /var/log/mongodb/mongod.log
```

Then in `/etc/logrotate.d/mongodb`, change `create 0600 mongod mongod` to `create 0640 mongod mongod`.

> If log access isn't granted, Atlas will still complete the capture — the log analysis section of the operational report will be empty.

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
└── pipelines/                      # Operational report pipeline definitions
    └── topworkflows.json
```

## Upgrading

### From 1.x → 2.0

- **Credentials are now an explicit choice** — OS keyring, encrypted local file, or Vault, selected at setup. Existing keyring and Vault installs keep working unchanged; nothing auto-switches.
- **New SaaS tier** for single-gateway (GW4 *or* GW5) audits with no Platform/MongoDB/Redis — chosen per environment at create time.
- **Multi-tenant mode and the `customer` command have been removed** — use named environments (one per customer) instead.

All existing sessions, environments, and captures continue to work.

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

**Version:** 2.0.0
**Author:** Cody Rester
**Last Updated:** June 2026
