# SSH-primary rules reference—`p6-master-ruleset.json`

A rule is classified as **SSH-primary** when its primary `path` resolves to data that can
only be captured via SSH (not the Platform OAuth API, pymongo, redis-py, or ipsdk). This
excludes rules where SSH appears only as an `alt_path` fallback.

**32 of 122 rules are SSH-primary**, organized by collection mechanism below.

> **Kubernetes note:** None of these rules use `alt_path` to point at a Kubernetes path.
> Where a Kubernetes mechanism is listed, the `KubernetesCollector` populates the **same
> primary path** (e.g. `gateway5.variables.*`) from Helm `values.yaml` or `kubectl` instead
> of SSH. It is an alternate capture source for K8s deployments, not a ruleset fallback.

---

## Group 1—Gateway4: SSH filesystem/service checks

**Category:** `gateway4`
**SSH Mechanism:** `Gateway4Collector` reads the systemd unit file to check for `--sync-config`;
`FileSystemInfoCollector` runs `stat` over SSH for database file sizes.
**Kubernetes Mechanism:** None—Gateway4 is a bare-metal/VM service and has no Kubernetes collector equivalent.

| Rule # | Name | Description |
|---|---|---|
| IAG-008 | Gateway4 Sync Config | Validates if `--sync-config` is enabled in the Gateway4 service file |
| IAG-009 | Gateway4 Main Database Size | Validates the main SQLite database size |
| IAG-010 | Gateway4 Audit Database Size | Validates the audit SQLite database size |
| IAG-011 | Gateway4 Exec History Database Size | Validates the exec history SQLite database size |

---

## Group 2—Gateway5: Environment variables

**Category:** `gateway5`
**SSH Mechanism:** `Gateway5Collector.collect_env()` runs `printenv` over SSH and filters `GATEWAY_*`
environment variables. `printenv` is the primary source, but it is not the only one: these same
`gateway5.variables.*` values can also be parsed from a local Docker Compose / Helm file
(`parse_gateway5_yaml`, for containerized gateways with no SSH `printenv` reachable), and the server
`gateway.conf` (INI) can be read over SSH and mapped to the same settings—surfaced on each rule's
`alt_path` (`gateway5.config_file.*`).
**Kubernetes Mechanism:** `KubernetesCollector.collect_gateway5()` reads the Gateway 5 Helm `values.yaml`
(`applicationSettings`, `serverSettings`, `runnerSettings`) and maps them to the same
`gateway5.variables.*` paths. Inline `env:` overrides in the Helm values are also captured.

| Rule # | Name | Description |
|---|---|---|
| IAG-012 | Gateway Store Backend | Validate if this property is enabled |
| IAG-013 | Gateway Client TLS | Validate TLS is enabled on gateway |
| IAG-014 | Gateway Logging Levels | Validate Gateway5 logging level |
| IAG-015 | Gateway Connect Enabled | Validate if gateway manager is enabled |
| IAG-016 | Gateway Connect Insecure TLS | Validate TLS insecure flag for gateway |
| IAG-017 | Gateway Server TLS | Validate TLS is enabled on gateway server |
| IAG-018 | Gateway Feature: Ansible | Validate Ansible feature is enabled/configured |
| IAG-019 | Gateway Feature: Hostkeys | Validate Hostkeys feature is enabled/configured |
| IAG-020 | Gateway Feature: OpenTofu | Validate OpenTofu feature is enabled/configured |
| IAG-021 | Gateway Feature: Python | Validate Python feature is enabled/configured |
| IAG-022 | Gateway Runner TLS | Validate TLS is enabled on gateway runner |
| IAG-023 | Gateway Console Log: JSON | Validate gateway console log format |
| IAG-024 | Gateway File Log: JSON | Validate gateway file log format |
| IAG-025 | Gateway Connect Redundancy Check | Validate if redundancy is enabled for gateway manager |
| IAG-026 | Gateway Connect HA Primary Check | Validate Gateway Connect HA primary flag |
| IAG-027 | Gateway Client Certificate File | Validate if gateway client certificate file is set |
| IAG-028 | Gateway Connect Certificate File | Validate if gateway connect certificate file is set |
| IAG-029 | Gateway Runner Certificate File | Validate if gateway runner certificate file is set |
| IAG-032 | Gateway Runner Announcement Address | Validate gateway runner announcement address |
| IAG-033 | Gateway Server Distributed Execution | Validate gateway server distributed execution flag |
| IAG-034 | Gateway Server Certificate File | Validate if gateway server certificate file is set |
| IAG-035 | Gateway Venv Pruner Sweep Interval | Validate the Python venv pruner sweep interval |
| IAG-036 | Gateway Venv Pruner Retention Period | Validate the Python venv pruner retention period |

---

## Group 3—Gateway5: `iagctl` commands over SSH

**Category:** `gateway5`
**SSH Mechanism:** `FileSystemInfoCollector.get_iagctl_checks()` runs `iagctl version` and
`iagctl get registries --raw` over SSH.
**Kubernetes Mechanism:** None—`iagctl` is a bare-metal CLI tool; no `kubectl exec` equivalent
is currently implemented for these checks.

| Rule # | Name | Description |
|---|---|---|
| IAG-030 | Gateway Version Check | Validate Gateway5 version via `iagctl version` (critical; requires version ≥ 5.4) |
| IAG-031 | Gateway Custom Registries | Validate gateway custom registries via `iagctl get registries` |

---

## Group 4—Platform: Filesystem and binary checks over SSH

**Category:** `platform`
**SSH Mechanism:** `FileSystemInfoCollector` over SSH—reads `platform.properties`, checks
file sizes with `stat`, and runs `python3 --version`.

| Rule # | Name | Description | Kubernetes Mechanism |
|---|---|---|---|
| PLAT-027 | Mongo URL | Validate mongo URL for any additional properties (read from `platform.properties`) | `KubernetesCollector.collect_platform_conf()` reads `ITENTIAL_MONGO_URL` from the IAP Helm `values.yaml` `env:` block and maps it to the same `platform.config_file.*` path |
| PLAT-038 | AGManager Pronghorn JSON Size | Validates the size of the AGManager `pronghorn.json` file | None |
| PLAT-040 | Platform Python Version Check | Validate if the installed Python version is supported | None |

---

## Summary

| Group | Category | Rules | SSH Mechanism | Kubernetes Mechanism |
|---|---|---|---|---|
| Gateway4 service/DB checks | `gateway4` | IAG-008 to IAG-011 | `stat` commands, systemd unit file parsing | None |
| Gateway5 environment variables | `gateway5` | IAG-012 to IAG-029, IAG-032 to IAG-036 | `printenv` over SSH (also Docker Compose / Helm file parse, or server `gateway.conf` via `alt_path`) | IAG5 Helm `values.yaml` via `KubernetesCollector.collect_gateway5()` |
| Gateway5 `iagctl` checks | `gateway5` | IAG-030, IAG-031 | `iagctl version` / `iagctl get registries` over SSH | None |
| Platform filesystem checks | `platform` | PLAT-027, PLAT-038, PLAT-040 | `platform.properties` parse, `stat`, `python3 --version` | PLAT-027 only—IAP Helm `values.yaml` `env:` block |
| **Total** | | **32 rules** | | |

---

> **What about the other 90 rules?** They obtain their primary data via the Platform OAuth API
> (`platform.*`), pymongo (`mongo.*`), redis-py (`redis.*`), or the ipsdk Gateway4 API
> (`gateway4.runtime_config.*`, `gateway4.api_status.*`). SSH may still appear as an
> `alt_path` fallback for some of those rules, but it is not the primary collection method.
> The fifteen KBS rules (`KBS-001` to `KBS-015`)—the full `kubernetes` category—use the
> Kubernetes collector (Helm `values.yaml` / `kubectl`) as their primary source and are also
> not SSH-based.
