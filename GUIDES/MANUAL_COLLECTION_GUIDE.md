# Manual Data Collection Guide

When Platform Atlas cannot connect directly to your infrastructure, you can
collect the data manually and feed it to Atlas using **guided collection** mode:

```
platform-atlas session run capture --manual
```

Atlas will walk you through each file. This guide covers how to gather them
all ahead of time so the process goes quickly.

---

## Before You Start

You will need SSH access to each server in the deployment and the ability to
run the commands below. All commands are read-only — nothing is modified.

Create a working directory on the Platform host to keep everything organized:

```bash
mkdir ~/atlas-capture && cd ~/atlas-capture
```

---

## 1. Platform API Data

A helper script called **`collect_platform.sh`** automates the Platform
endpoint collection. It asks whether you're running Platform 6 or IAP 2023.x
and collects the correct endpoints accordingly:

```bash
bash collect_platform.sh <host>:<port> <TOKEN>
```

If you prefer to collect them by hand, see below. Replace `<HOST>` with your
Platform address (e.g. `localhost:3443`) and `<TOKEN>` with a valid API token.

### Common Endpoints (all versions)

These endpoints are the same for both Platform 6 and IAP 2023.x:

```bash
curl -sk "https://<HOST>/health/server?token=<TOKEN>"       > platform_health_server.json
curl -sk "https://<HOST>/health/status?token=<TOKEN>"       > platform_health_status.json
curl -sk "https://<HOST>/health/adapters?token=<TOKEN>"     > platform_adapter_status.json
curl -sk "https://<HOST>/health/applications?token=<TOKEN>" > platform_application_status.json
curl -sk "https://<HOST>/adapters?token=<TOKEN>"            > platform_adapter_props.json
curl -sk "https://<HOST>/applications?token=<TOKEN>"        > platform_application_props.json
```

`platform_health_server.json` is required. The rest are optional but recommended.

### Platform 6 Only

This endpoint is only available on Platform 6 — it does not exist on IAP 2023.x:

```bash
curl -sk "https://<HOST>/server/config?token=<TOKEN>"       > platform_config.json
```

### IAP 2023.x Only

This endpoint is only available on IAP 2023.x. Replace `<PROFILE_NAME>` with
your profile name (the same value as the `legacy_profile` setting in your
Atlas environment config):

```bash
curl -sk "https://<HOST>/profiles/<PROFILE_NAME>?token=<TOKEN>" > platform_profile.json
```

Atlas auto-detects whether you're running 2023.x or Platform 6 from your
environment config — you don't need to tell it. When using `--import-dir`,
it will remind you about this endpoint if applicable.

---

## 2. Platform Properties File (Platform 6 only)

This file does not exist on IAP 2023.x installations — skip this section
if you're running 2023.x.

```bash
cat /etc/itential/platform.properties > platform_conf.txt
```

---

## 3. Platform Supplemental Files (Platform 6 only)

These files are specific to Platform 6 installations. Skip this section
if you're running IAP 2023.x.

```bash
# AGManager pronghorn.json size (used by rule PLAT-038)
stat -c %s /opt/itential/platform/server/services/app-ag_manager/pronghorn.json > agmanager_size.txt

# Python version check (used by rule PLAT-040)
python3.11 --version > python_version.txt 2>&1
```

---

## 4. Platform Logs (Platform 6 only, optional)

Log analysis is optional and only applies to Platform 6 installations.
Skip this section if you're running IAP 2023.x.

```bash
# Platform application log (most recent 10,000 lines)
tail -n 10000 /var/log/itential/platform/itential-platform.log > platform_logs.txt

# Webserver access log (most recent 10,000 lines)
tail -n 10000 /var/log/itential/platform/webserver.log > webserver_logs.txt
```

---

## 5. MongoDB

Run these on the **primary** MongoDB server. If you have a replica set, you
only need data from the primary member — not the secondaries or arbiters.

```bash
# Server status (required)
mongosh --quiet --eval "JSON.stringify(db.adminCommand({serverStatus: 1}))" > mongo_server_status.json

# Database stats (optional)
mongosh --quiet --eval "JSON.stringify(db.adminCommand({dbStats: 1}))" > mongo_db_stats.json
```

If authentication is required, add your credentials:

```bash
mongosh "mongodb://user:password@localhost:27017/admin" --quiet --eval "..."
```

---

## 6. MongoDB Configuration

Collect from the same primary MongoDB server:

```bash
cat /etc/mongod.conf > mongo_conf.yml
```

---

## 7. MongoDB Replica Set (HA2 only)

If your deployment uses MongoDB in a replica set (HA2 mode), collect the
replica set status and configuration from the **primary** member. Skip this
section for standalone deployments.

Atlas auto-detects HA2 mode from your environment config and will only ask
for these files when applicable.

```bash
# Replica set status (rs.status())
mongosh --quiet --eval "JSON.stringify(db.adminCommand({replSetGetStatus: 1}))" > mongo_repl_status.json

# Replica set configuration (rs.conf())
mongosh --quiet --eval "JSON.stringify(db.adminCommand({replSetGetConfig: 1}))" > mongo_repl_config.json
```

If authentication is required, add your credentials as shown in section 5.

---

## 8. Redis

Run these on the **master** Redis server only. If you have a Redis Sentinel
cluster with multiple Redis instances, you only need data from the one
reporting `role:master`. Check which server is the master with:

```bash
redis-cli INFO replication | grep role
```

The server that returns `role:master` is the one to collect from. Skip the
replicas.

```bash
# Redis INFO (required)
redis-cli INFO ALL > redis_info.txt

# Redis ACL users (optional)
redis-cli ACL USERS > redis_acl.txt
```

If Redis requires authentication:

```bash
redis-cli -a '<password>' INFO ALL > redis_info.txt
redis-cli -a '<password>' ACL USERS > redis_acl.txt
```

---

## 9. Redis Configuration

Collect from the same master Redis server:

```bash
cat /etc/redis/redis.conf > redis_conf.txt
```

If your environment uses Sentinel, also collect the sentinel config from any
one of the Sentinel nodes:

```bash
cat /etc/redis/sentinel.conf > sentinel_conf.txt
```

---

## 10. Gateway4 (Automation Gateway)

If your deployment includes Gateway4, collect the following from the Gateway
server:

```bash
# Installed Python packages (required)
/opt/automation-gateway/venv/bin/pip list --format=json > gateway4_packages.json

# Properties file
cat /etc/automation-gateway/properties.yml > gateway4_conf.yml

# SQLite database file sizes (one line each: main, audit, exec_history)
stat -c %s /var/lib/automation-gateway/automation-gateway.db > gw4_db_sizes.txt
stat -c %s /var/lib/automation-gateway/automation-gateway_audit.db >> gw4_db_sizes.txt
stat -c %s /var/lib/automation-gateway/automation-gateway_exec_history.db >> gw4_db_sizes.txt

# Sync-config flag check
systemctl cat automation-gateway > gateway4_sync_config.txt
```

---

## 11. Gateway5

If your deployment uses Gateway5 (containerized), provide one of the following:

```bash
# Docker Compose
cp /path/to/docker-compose.yml ./gateway5_config.yml

# — or Helm values —
cp /path/to/values.yaml ./gateway5_config.yml
```

Atlas parses the environment variables from either format automatically.

---

## 12. System Information (Optional)

Atlas can derive basic system facts without this file. If you want to provide
it explicitly, create a JSON file with the following structure:

```json
{
  "cpu": {
    "cores_physical": 4,
    "cores_logical": 8
  },
  "memory": {
    "virtual": {
      "total": 16777216000
    }
  },
  "host": {
    "hostname": "platform-prod-01"
  },
  "os": {
    "system": "linux",
    "machine": "x86_64"
  }
}
```

Or generate it automatically:

```bash
python3 -c "
import json, os, platform
info = {
    'cpu': {'cores_physical': os.cpu_count(), 'cores_logical': os.cpu_count()},
    'memory': {'virtual': {'total': os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')}},
    'host': {'hostname': platform.node()},
    'os': {'system': platform.system().lower(), 'machine': platform.machine()}
}
print(json.dumps(info, indent=2))
" > system_info.json
```

---

## Feeding the Files to Atlas

Once all files are collected, transfer them to the machine running Atlas.

### Option A: Batch Import (Recommended)

Point Atlas at the directory and it imports everything automatically:

```
platform-atlas session create my-audit
platform-atlas session run capture --manual --import-dir ~/atlas-capture/
```

Atlas matches files by name — no prompts, no typing paths one at a time.
It shows you exactly what it found, what it imported, and what's still
missing.

If you realize you forgot some files, just add them to the directory and
run the same command again. Atlas updates what changed and adds what's new.

### Option B: Interactive Prompts

If you prefer to be walked through it step by step:

```
platform-atlas session create my-audit
platform-atlas session run capture --manual
```

Atlas will prompt for each file one at a time. Provide the path to each file
when asked. You can type **skip** to skip any optional item, and you can
**quit** at any point — progress is saved and you can resume later by running
the same command again.

### Mixing Both

You can batch-import a directory first, then run interactive mode to fill in
anything that's still missing. Progress is shared between both modes.

After the capture is complete:

```
platform-atlas session run validate
platform-atlas session run report
```

---

## Quick Reference — Expected Filenames

These are the filenames Atlas recognizes during batch import (`--import-dir`).
Use these exact names (extension can be `.json`, `.txt`, `.yml`, etc.):

Atlas auto-detects whether you're running Platform 6 or IAP 2023.x from
your environment's `legacy_profile` setting.

| Module               | Expected Filename(s)                     | Required | Notes |
|----------------------|------------------------------------------|----------|-------|
| Platform API (common)| `platform_health_server`, `platform_health_status`, `platform_adapter_status`, `platform_application_status`, `platform_adapter_props`, `platform_application_props` | First one yes | All versions |
| Platform Config      | `platform_config`                        | Yes      | P6 only |
| Platform Profile     | `platform_profile`                       | No       | 2023.x only |
| Platform Properties  | `platform_conf`                          | Yes      | P6 only |
| AGManager Size       | `agmanager_size`                         | Yes      | P6 only |
| Python Version       | `python_version`                         | Yes      | P6 only |
| Platform Logs        | `platform_logs`, `webserver_logs`        | No       | P6 only |
| MongoDB Status       | `mongo_server_status`                    | Yes      | All versions |
| MongoDB Stats        | `mongo_db_stats`                         | No       | All versions |
| MongoDB Config       | `mongo_conf`                             | Yes      | All versions |
| MongoDB Repl Status  | `mongo_repl_status` or `rs_status`       | No       | HA2 only |
| MongoDB Repl Config  | `mongo_repl_config` or `rs_conf`         | No       | HA2 only |
| Redis Info           | `redis_info`                             | Yes      | All versions |
| Redis ACL            | `redis_acl`                              | No       | All versions |
| Redis Config         | `redis_conf`                             | Yes      | All versions |
| Gateway4 Packages    | `gateway4_packages`                      | If deployed | |
| Gateway4 Config      | `gateway4_conf`                          | If deployed | |
| Gateway4 DB Sizes    | `gw4_db_sizes`                           | If deployed | |
| Gateway4 Sync Config | `gateway4_sync_config`                   | If deployed | |
| Gateway5             | `gateway5_config`                        | If deployed | |
| System Info          | `system_info`                            | No       | All versions |