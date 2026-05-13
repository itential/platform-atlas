# Platform Atlas — Quick Start

### Install

```bash
pip install platform_atlas-1.7.0-py3-none-any.whl
```

### Configure

```bash
platform-atlas config init
```

Follow the interactive wizard. You'll set global preferences first, then create your first named environment with your Platform URI, OAuth2 client ID and secret, and optionally your MongoDB and Redis URIs. For Linux servers without a desktop, install `keyrings.alt` first (`pip install keyrings.alt`).

### Choose Your Tier

Platform Atlas 1.7+ ships with two audit modes. Fresh installs default to **Standard**; upgrades from 1.6.x default to **Extended**.

| Tier | What it audits | Requirements |
|------|---------------|--------------|
| **Standard** | Platform OAuth + optional IAG4 API (~54 rules) | Platform credentials only — no SSH, MongoDB, or Redis needed |
| **Extended** | Full infrastructure: SSH, MongoDB, Redis, Kubernetes, Gateways (~107 rules) | SSH access + MongoDB/Redis URIs |

```bash
platform-atlas tier show                  # see your current tier
platform-atlas tier set standard          # switch to Standard
platform-atlas tier set extended          # switch to Extended
platform-atlas tier upgrade               # interactive upgrade to Extended
platform-atlas tier downgrade             # interactive downgrade to Standard
```

Sessions bind the tier at creation time — switching sessions restores the tier automatically. Use `--tier standard` or `--tier extended` as a one-off override on any command without changing the persisted setting.

### Verify Connectivity

```bash
platform-atlas preflight
```

Fix any failures before proceeding. Common fixes: correct SSH key path, open firewall ports, update a credential with `platform-atlas config credentials`.

For a broader, one-shot health check that also covers credentials, ruleset, and URL reachability:

```bash
platform-atlas config doctor
```

Use `config doctor` right after setup, after editing an environment, or any time something feels off — it surfaces every config / credential / ruleset issue at once instead of letting them appear one by one during capture. Exits non-zero on warnings or errors, so it works in CI.

### Load a Ruleset

```bash
platform-atlas ruleset setup                       # interactive — pick a ruleset and profile
```

This walks you through selecting a ruleset and profile in one step. The selection is saved and persists across sessions.

If you prefer explicit commands (useful for scripts or CI):

```bash
platform-atlas ruleset list                        # see what's available
platform-atlas ruleset load <ruleset-id>           # activate a ruleset
platform-atlas ruleset profile list                # see available profiles
platform-atlas ruleset profile set <profile-id>    # activate a profile
```

### Run an Audit

```bash
platform-atlas session create prod-q1-2026         # create a session
platform-atlas session active prod-q1-2026          # set it as active
platform-atlas session run all                      # capture → validate → report
```

Your HTML report opens automatically. Find it at `~/.atlas/sessions/prod-q1-2026/03_report.html`.

### Run Stages Individually

```bash
platform-atlas session run capture                  # collect data from targets
platform-atlas session run validate                  # check against ruleset
platform-atlas session run report                    # generate HTML report
```

### Other Useful Commands

```bash
platform-atlas                                      # show dashboard
platform-atlas session list                          # list all sessions
platform-atlas session show                          # details of active session
platform-atlas session switch                        # interactive session switch
platform-atlas session diff session-a session-b      # compare two sessions
platform-atlas session export prod-q1-2026           # package as ZIP for sharing
platform-atlas config credentials                    # update stored credentials
platform-atlas config deployment                     # change server topology
platform-atlas config doctor                         # run a configuration health check
platform-atlas --debug session run capture           # verbose output for troubleshooting
```

### Fleet Dashboard (1.7+)

Get a compliance overview across all your environments from local cache — no captures triggered:

```bash
platform-atlas fleet status                          # overview of all environments
platform-atlas fleet status --json                   # machine-readable output
```

### Continuous Audit (1.7+)

Schedule automatic drift monitoring that re-runs a Platform OAuth capture against your active ruleset and surfaces changes as alerts:

```bash
platform-atlas continuous-audit run-once             # test run before enabling
platform-atlas continuous-audit enable               # install OS-level schedule and start monitoring
platform-atlas continuous-audit status               # check if enabled, last run time, alert count
platform-atlas continuous-audit alerts               # view current unacknowledged alerts
platform-atlas continuous-audit ack <alert-id>       # acknowledge an alert
platform-atlas continuous-audit ack-all              # acknowledge all alerts
platform-atlas continuous-audit disable              # stop monitoring
platform-atlas continuous-audit notify add           # add a Slack or webhook notification channel
```

### Multiple Environments

If you manage dev, staging, and production deployments:

```bash
platform-atlas env create                            # create a new environment
platform-atlas env create staging --from production  # copy and customize
platform-atlas env list                              # list all environments
platform-atlas env switch                            # switch active environment
platform-atlas --env dev preflight                   # one-off against a specific env
```

### Headless / Scripted Usage

```bash
platform-atlas session run all --headless --session prod-q1-2026
platform-atlas --env production session run all --headless --session prod-q1-2026  # explicit env
```

Skips all prompts. Suitable for cron jobs and CI pipelines. Use `--env` or the `ATLAS_ENV` environment variable to target a specific environment in scripts.

### Quick Switching Guide

```bash
platform-atlas env switch
platform-atlas ruleset switch
platform-atlas session switch
```

Quick review of the 3 main things you can switch between, `rules/profiles`, `environments`, and `sessions`.

### Where Things Live

| Path | Contents |
|------|----------|
| `~/.atlas/config.json` | Global configuration (no secrets) |
| `~/.atlas/environments/` | Named environment files (one per deployment) |
| `~/.atlas/sessions/` | Audit sessions (capture, validation, reports) |
| `~/.atlas/continuous/` | Continuous audit runs, alerts, and event timeline (1.7+) |
| `~/.atlas/atlas.log` | Application log |

Credentials are stored in your OS keyring (scoped per environment) or HashiCorp Vault — never on disk.

### Need Help?

```bash
platform-atlas --help                               # all commands
platform-atlas session run --help                    # options for a specific command
platform-atlas guide                                 # built-in reference guide
```

See the full **Platform Atlas User Guide** for detailed setup instructions, credential backend options, and troubleshooting FAQ.