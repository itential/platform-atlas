# Platform Atlas — Prerequisites Checklist

Work through these items before installing Platform Atlas. Gather credentials, verify access,
and confirm your deployment topology before you begin.

**New in v1.7 — Two Tiers:** Platform Atlas now ships with two audit modes. Choose which one
applies to you before working through this checklist.

| Tier | What it audits | What you need |
|---|---|---|
| **Standard** | Platform application layer only (Platform OAuth + IAG4 API) | Sections 1, 2, and optionally 7 |
| **Extended** | Full infrastructure (all of Standard + SSH, MongoDB, Redis, Gateways) | All applicable sections |

If you are not sure which tier to use, start with **Standard**. You can upgrade to Extended
at any time with `platform-atlas tier upgrade`.

> Fresh installs default to Standard. Upgrades from 1.6.x default to Extended.

---

## 1. Workstation `Required`

- [ ] macOS 12+, Linux, or Windows 11 workstation available
  - Platform Atlas runs on the machine *you* work from — your laptop or a jump host. macOS,
    Linux, and Windows 11 are all supported. RHEL/Rocky 8 and 9 are validated for headless
    server installs.
- [ ] Python 3.11 or later installed
  - Verify with `python3 --version`. Must be **3.11.x or higher**. Python 3.10 and earlier are
    not supported.
- [ ] pip is available and up to date
  - Verify with `pip3 --version`. Update with `pip3 install --upgrade pip`.
- [ ] Platform Atlas `.whl` file(s) received from your Itential contact
  - The core wheel is named `platform_atlas-1.7.0-py3-none-any.whl`. If you want the optional
    browser interface, also request `platform_atlas_webui-1.7.0-py3-none-any.whl`. If you
    haven't received them yet, contact your Itential Customer Success representative.
- [ ] Credential storage backend is ready
  - Atlas never stores secrets in plain text. Choose one:
    - **macOS:** Keychain is built-in — nothing to do.
    - **Linux (desktop with GNOME):** gnome-keyring is typically built-in.
    - **Linux (headless/server):** Run `pip install keyrings.alt` for encrypted file-based
      storage, or use HashiCorp Vault.

---

## 2. Itential Automation Platform (IAP) `Required`

- [ ] IAP Platform URL is known
  - Example: `https://iap.yourcompany.com:3443`. This is the address Atlas uses for all
    Platform API calls.
- [ ] OAuth2 Client ID obtained
  - An OAuth2 client application must exist in IAP for Platform Atlas to authenticate. Your IAP
    administrator creates this in IAP's application management. The client needs read-only API
    access — it does not need admin permissions.
- [ ] OAuth2 Client Secret obtained
  - The secret that pairs with the Client ID above. Keep this secure — Atlas stores it in your
    OS keyring or Vault, never in a config file.
- [ ] Workstation can reach the IAP API port over the network
  - Test with: `curl -k https://<iap-host>:3443/health` from your workstation. If using a VPN
    or jump host, confirm it's connected before running Atlas.

---

## 3. MongoDB `Extended tier only`

- [ ] MongoDB connection URI is known
  - Standard: `mongodb://user:pass@host:27017/`
  - Replica set: `mongodb://user:pass@host1:27017,host2:27017,host3:27017/?replicaSet=rs0`
  - Your IAP administrator or DBA can provide this.
- [ ] MongoDB user has sufficient read permissions
  - Atlas runs `getCmdLineOpts`, `serverStatus`, `dbStats`, and reads the `admin`, `local`, and
    `config` databases. A user with the built-in `clusterMonitor` role satisfies this.
- [ ] Workstation can reach the MongoDB port over the network
  - Default port is `27017`. Test with:
    `mongosh "mongodb://<host>:27017" --eval "db.runCommand({ping:1})"`

> MongoDB auditing can be skipped if not needed — leave the URI blank during Atlas setup and
> MongoDB-related rules will show as **SKIP** in the report.

---

## 4. Redis `Extended tier only`

- [ ] Redis connection URI is known
  - Standard: `redis://host:6379` or `redis://user:pass@host:6379`
  - Sentinel: `redis://sentinel-host:26379?sentinel=mymaster`
  - Atlas auto-detects standalone vs. Sentinel once connected.
- [ ] The Redis `itential` user has the `+config|get` ACL permission
  - Atlas uses `CONFIG GET *` to read Redis configuration. Without this permission, the Redis
    capture will fail. Check current ACLs with: `redis-cli ACL GETUSER itential`
- [ ] Workstation can reach the Redis port over the network
  - Default port is `6379` (Sentinel: `26379`). Test with:
    `redis-cli -h <host> -p 6379 ping`

> Like MongoDB, Redis auditing can be skipped by leaving the URI blank during setup. Redis
> rules will appear as **SKIP** in the report.

---

## 5. SSH Access to Servers `Extended tier only`

Atlas supports three transport options for connecting to each node. Most deployments use
**SSH** for everything — the items below cover that path. If direct SSH to the Platform server
is not possible (CyberArk PSMP, etc.) see Section 5b *ControlMaster*. If Atlas is installed
**on** the Platform server itself, see Section 5c *Local transport*.

- [ ] Full list of server hostnames or IP addresses is available
  - Every server Atlas will connect to: IAP nodes, MongoDB nodes, Redis nodes, and any Gateway
    nodes. For HA deployments this is typically 8–10 hosts.
- [ ] A dedicated SSH user exists on every target server
  - Recommended: create a `platformatlas` service account on each server. See the SSH Setup
    Guide included with Platform Atlas for step-by-step instructions. The user needs read access
    to config files in `/etc/` and `/opt/` — root access is **not** required.
- [ ] An SSH key pair is generated on your workstation
  - Generate a dedicated key:
    `ssh-keygen -t ed25519 -C "platform-atlas" -f ~/.ssh/platform-atlas`
  - The private key stays on your workstation. The public key goes to every target server.
- [ ] Public key deployed to each target server
  - Deploy with: `ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@<host>`
  - Verify key-based login works before configuring Atlas:
    `ssh -i ~/.ssh/platform-atlas platformatlas@<host>`
- [ ] SSH port (22) is reachable from your workstation to each server
  - Test with: `ssh -o ConnectTimeout=5 platformatlas@<host> echo ok`
  - If using a non-standard port, note it — you'll enter it during topology setup in Atlas.
- [ ] *(Optional)* Passwordless sudo configured for reading protected config files
  - Some files (e.g. `/etc/redis/redis.conf`, `/etc/redis/sentinel.conf`) are root-only.
    Limited passwordless sudo lets Atlas read them automatically. Add to
    `/etc/sudoers.d/platformatlas`:
    ```
    platformatlas ALL=(ALL) NOPASSWD: /usr/bin/test, /usr/bin/stat, /usr/bin/realpath, /usr/bin/cat
    ```

> Atlas is **strictly read-only** over SSH. It uses `cat`, `stat`, `uname`, and similar
> informational commands. It never modifies files, restarts services, or writes anything to
> your servers.

---

## 5b. ControlMaster Transport `Optional — only if direct SSH is blocked`

*Skip this section if you are using normal SSH (Section 5).*

Use ControlMaster when direct key-based SSH to the **Platform (IAP)** node is not possible —
typically because all privileged SSH is routed through CyberArk PSMP or another PAM gateway
that requires interactive MFA. You open one master session manually (which performs the MFA
tap) and Atlas multiplexes through that socket without ever holding the underlying credentials.

- [ ] OpenSSH client 7.x or newer is installed on the workstation
  - Verify with: `ssh -V`
- [ ] You can complete an interactive SSH login through the PAM gateway by hand
  - Example (CyberArk PSMP): `ssh <user>@<target>@<psmp-gateway>`
  - If this fails interactively, ControlMaster will not work — Atlas piggybacks on whatever
    authentication you can perform manually.
- [ ] You have a writable directory for the control socket
  - Atlas defaults to `/tmp/atlas-cm.sock`. Any path the running user can write to works.
- [ ] You know the full SSH destination string for each node you'll use ControlMaster on
  - PSMP destination format: `<user>@<target-ip-or-hostname>@<psmp-gateway>`. The CLI wizard and
    the WebUI environment form both prompt for it during topology setup.

> **Scope:** ControlMaster is selected per-node during topology setup. The CLI wizard applies
> your choice to every node when you select it for IAP; the WebUI form applies it to the IAP
> node only and preserves whatever the CLI set for Mongo/Redis. See the ControlMaster section
> in `SSH_SETUP_GUIDE.md` for end-to-end setup steps.

---

## 5c. Local Transport `Optional — only if Atlas runs on the Platform server`

*Skip this section if you are running Atlas from a separate workstation.*

When Atlas itself is installed on the IAP server, the IAP node can be configured with **Local**
transport — Atlas reads config files and runs system commands through the local filesystem
instead of SSH. MongoDB, Redis, and Gateway nodes still use SSH (Section 5).

- [ ] The user running Atlas on the IAP server has read access to `/etc/itential/`, `/opt/itential/`, and other paths the active ruleset references
- [ ] *(Optional)* Passwordless sudo for `cat`, `stat`, `realpath`, `test` configured for the running user — same setup as Section 5

---

## 6. Deployment Topology `Extended tier only`

- [ ] Deployment mode is identified: Standalone, HA2, or Custom
  - **Standalone** — Single IAP server, one MongoDB instance, one Redis instance.
  - **HA2** — Multiple IAP nodes, MongoDB replica set (typically 3), Redis Sentinel
    (typically 3).
  - **Custom** — Any other layout; you manually assign roles to each node.
- [ ] IAP node hostname(s) or IP address(es) are documented
  - Standalone: 1 host. HA2: typically 2 IAP app nodes (e.g. `iap-01`, `iap-02`).
- [ ] MongoDB node hostname(s) or IP address(es) are documented
  - Standalone: 1 host. HA2: typically 3 replica set members (e.g. `mongo-01`, `mongo-02`,
    `mongo-03`).
- [ ] Redis node hostname(s) or IP address(es) are documented
  - Standalone: 1 host. HA2 with Sentinel: typically 3 members (e.g. `redis-01`, `redis-02`,
    `redis-03`).

---

## 7. Automation Gateway 4 `Optional — Standard and Extended`

*Skip this section if Gateway 4 is not part of your IAP deployment.*

- [ ] Gateway 4 node hostname(s) or IP address(es) are documented
  - Note all hostnames where the `automation-gateway` service is running.
- [ ] Gateway 4 REST API is reachable from the workstation
  - Atlas queries `GET /config` and `GET /status` on the Gateway 4 API (default port `8443`)
    for runtime configuration. Verify network access and that the Gateway service is running.
- [ ] Gateway 4 API credentials obtained (if authentication is enabled)
  - If your Gateway 4 deployment requires authentication, obtain the API token or credentials
    from your Gateway administrator before running Atlas setup.

> Gateway 4 uses a REST API as its primary data source. Atlas reads `automation-gateway.db`
> via `GET /config` — the `properties.yml` file on disk may be stale after first boot and is
> only used as a fallback if the API is unreachable.

---

## 8. Automation Gateway 5 `Optional — Extended tier only`

*Skip this section if Gateway 5 is not part of your IAP deployment.*

- [ ] Gateway 5 node hostname(s) or IP address(es) are documented
  - Note all hostnames where the Gateway 5 virtual environment is installed. These must already
    be covered by your SSH setup (Section 5).
- [ ] Gateway 5 virtual environment path is known
  - Atlas locates Gateway 5 by inspecting the virtual environment (typically under `/opt/`) and
    reading the installed `automation-gateway` package version via `pip list` over SSH. Confirm
    the venv path with your Gateway administrator.
- [ ] Gateway 5 environment variables are configured on the host
  - Gateway 5 is configured via environment variables rather than a config file. Atlas reads
    these over SSH from the process environment or systemd unit file. Confirm that the Gateway 5
    service is running and its environment is set on the target host.
- [ ] SSH user has read access to the Gateway 5 venv and service files
  - The `platformatlas` SSH user needs read access to the Gateway 5 virtual environment
    directory and the systemd unit or init script where environment variables are defined. Test
    with: `ssh platformatlas@<gw5-host> "ls /opt/automation-gateway/"`

> Gateway 5 is entirely SSH-based — there is no REST API to query. Atlas reads package version
> information and environment variables over the same SSH connection used for the rest of the
> audit. No additional network ports need to be opened beyond SSH.

---

## 9. WebUI `Optional — Standard and Extended`

*Skip this section if you only intend to use the CLI.*

The WebUI ships as a separate `platform_atlas_webui-1.7.0-py3-none-any.whl` and runs on the
same machine as the CLI. It is local-only — there is no remote / multi-tenant deployment mode.

- [ ] You'll run the WebUI on the same machine where Platform Atlas is installed
  - Both share `~/.atlas/`. The WebUI cannot manage a remote Atlas install.
- [ ] A modern browser is available on that machine
  - Chrome / Edge / Firefox / Safari current. The WebUI uses self-signed TLS, so the browser
    will warn the first time — accept it for `localhost`.
- [ ] One of the following high TCP ports is free on `localhost`
  - The WebUI binds to `127.0.0.1:8765` by default and falls back to the next free port.
- [ ] *(Optional, daemon mode)* `--daemon` is supported on Linux and macOS only
  - Windows users run the foreground command in a terminal that stays open.

> The WebUI authenticates the OS user that started it (via a token file at `~/.atlas/.webui-token`).
> No additional credentials are required beyond what the CLI already has — environments,
> credentials, sessions, and tier are all read from the shared `~/.atlas/` directory.

---

Once all applicable items are checked, install:

```bash
# Core CLI (required)
pip install platform_atlas-1.7.0-py3-none-any.whl

# Optional WebUI — browser-based interface
pip install platform_atlas_webui-1.7.0-py3-none-any.whl
```

Then follow the Installation & Usage Guide to configure your first environment and run your
first audit. During setup you will be asked to choose a tier — refer to the table at the top
of this checklist to confirm which sections you completed.
