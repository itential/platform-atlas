# Platform Atlas — Prerequisites Checklist

Work through these items before installing Platform Atlas. Gather credentials, verify access,
and confirm your deployment topology before you begin. Gateway sections can be skipped if not
applicable.

---

## 1. Workstation `Required`

- [ ] macOS 12+ or Linux workstation available
  - Platform Atlas runs on the machine *you* work from — your laptop or a jump host. Windows is
    not officially supported. RHEL/Rocky 8 and 9 are validated for headless server installs.
- [ ] Python 3.11 or later installed
  - Verify with `python3 --version`. Must be **3.11.x or higher**. Python 3.10 and earlier are
    not supported.
- [ ] pip is available and up to date
  - Verify with `pip3 --version`. Update with `pip3 install --upgrade pip`.
- [ ] Platform Atlas `.whl` file received from your Itential contact
  - The wheel file will be named something like `platform_atlas-1.6-py3-none-any.whl`. If you
    haven't received it yet, contact your Itential Customer Success representative.
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

## 3. MongoDB `Required for full audit`

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

## 4. Redis `Required for full audit`

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

## 5. SSH Access to Servers `Required`

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

## 6. Deployment Topology `Required`

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

## 7. Automation Gateway 4 `Optional`

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

## 8. Automation Gateway 5 `Optional`

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

Once all applicable items are checked, run:

```bash
pip install platform_atlas-1.6-py3-none-any.whl
```

Then follow the Installation & Usage Guide to configure your first environment and run your
first audit.
