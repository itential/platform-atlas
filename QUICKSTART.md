# Platform Atlas—quick start

### Install

```bash
pip install platform_atlas-2.3.0-py3-none-any.whl
```

### Configure

```bash
platform-atlas
```

Running the CLI for the first time automatically launches the setup wizard—no `config init` needed. It asks for your theme and organization name, then writes global defaults and exits.

Next, create your first environment:

```bash
platform-atlas env create
```

This wizard walks you through your Platform URI, OAuth2 client ID and secret, and optionally your MongoDB and Redis URIs, plus where to store secrets. On a headless Linux server with no desktop keyring, choose the **Encrypted Local File** backend when prompted—no extra packages needed.

### Choose your tier

Platform Atlas 2.0 ships with three audit modes. Fresh installs default to **Standard**.

| Tier | What it audits | Requirements |
|------|---------------|--------------|
| **Standard** | Platform OAuth + optional Gateway 4 API (~56 rules) | Platform credentials only—no SSH, MongoDB, or Redis needed |
| **Extended** | Full infrastructure: SSH, MongoDB, Redis, Kubernetes, Gateways (~122 rules) | SSH access + MongoDB/Redis URIs |
| **SaaS** | A single Gateway 4 or Gateway 5 (gateway rules only) | Gateway API or SSH access—no Platform, MongoDB, or Redis |

```bash
platform-atlas tier show                  # see your current tier
platform-atlas tier set standard          # switch to Standard
platform-atlas tier set extended          # switch to Extended
platform-atlas tier upgrade               # interactive upgrade to Extended
platform-atlas tier downgrade             # interactive downgrade to Standard
```

**SaaS is picked when you create an environment** (the setup wizard offers it) and is fixed for that environment—`tier set saas` is intentionally blocked, and `tier upgrade`/`downgrade` only move between Standard and Extended. To audit a single gateway, create a new SaaS environment.

Sessions bind the tier at creation time—switching sessions restores the tier automatically. Use `--tier standard` or `--tier extended` as a one-off override on any command without changing the persisted setting.

### Verify connectivity

```bash
platform-atlas preflight
```

Fix any failures before proceeding. Common fixes: correct SSH key path, open firewall ports, update a credential with `platform-atlas config credentials`.

For a broader, one-shot health check that also covers credentials, ruleset, and URL reachability:

```bash
platform-atlas config doctor
```

Use `config doctor` right after setup, after editing an environment, or any time something feels off—it surfaces every config / credential / ruleset issue at once instead of letting them appear one by one during capture. Exits non-zero on warnings or errors, so it works in CI.

### Load a ruleset

```bash
platform-atlas ruleset setup                       # interactive—pick a ruleset and profile
```

This walks you through selecting a ruleset and profile in one step. The selection is saved and persists across sessions.

If you prefer explicit commands (useful for scripts or CI):

```bash
platform-atlas ruleset list                        # see what's available
platform-atlas ruleset load <ruleset-id>           # activate a ruleset
platform-atlas ruleset profile list                # see available profiles
platform-atlas ruleset profile set <profile-id>    # activate a profile
```

### Run an audit

```bash
platform-atlas session create prod-q1-2026         # create a session
platform-atlas session active prod-q1-2026          # set it as active
platform-atlas session run all                      # capture → validate → report
```

Your HTML report opens automatically. Find it at `~/.atlas/sessions/prod-q1-2026/report.html`.

### Run stages individually

```bash
platform-atlas session run capture                  # collect data from targets
platform-atlas session run validate                  # check against ruleset
platform-atlas session run report                    # generate HTML report
```

### Other useful commands

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

### Fleet dashboard

Get a compliance overview across all your environments from local cache—no captures triggered:

```bash
platform-atlas fleet status                          # overview of all environments
platform-atlas fleet status --json                   # machine-readable output
```

### Continuous audit

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

### Multiple environments

If you manage dev, staging, and production deployments:

```bash
platform-atlas env create                            # create a new environment
platform-atlas env create staging --from production  # copy and customize
platform-atlas env list                              # list all environments
platform-atlas env switch                            # switch active environment
platform-atlas --env dev preflight                   # one-off against a specific env
```

### Headless / scripted usage

```bash
platform-atlas session run all --headless --session prod-q1-2026
platform-atlas --env production session run all --headless --session prod-q1-2026  # explicit env
```

Skips all prompts. Suitable for cron jobs and CI pipelines. Use `--env` or the `ATLAS_ENV` environment variable to target a specific environment in scripts.

### Quick switching guide

```bash
platform-atlas env switch
platform-atlas ruleset switch
platform-atlas session switch
```

Quick review of the three main things you can switch between, `rules/profiles`, `environments`, and `sessions`.

### Where things live

| Path | Contents |
|------|----------|
| `~/.atlas/config.json` | Global configuration (no secrets) |
| `~/.atlas/environments/` | Named environment files (one per deployment) |
| `~/.atlas/sessions/` | Audit sessions (capture, validation, reports) |
| `~/.atlas/continuous/` | Continuous audit runs, alerts, and event timeline |
| `~/.atlas/atlas.log` | Application log |

Credentials live in one of three backends you pick at setup—your **OS keyring** (scoped per environment), an **encrypted local file** (AES-256-GCM, for headless hosts), or **HashiCorp Vault**—never in plain text.

### Need help?

```bash
platform-atlas --help                               # all commands
platform-atlas session run --help                    # options for a specific command
platform-atlas guide                                 # built-in reference guide
```

See the full **Platform Atlas User Guide** for detailed setup instructions, credential backend options, and troubleshooting FAQ.