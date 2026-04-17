# Platform Atlas — Review Compliance Report for Actionable Insights

This guide explains how to read the HTML compliance report that Platform Atlas generates, what every section means, and how to act on the results. It is written for customers and operations teams who receive a report and need to understand what to fix, in what order, and how.

If you haven't run an audit yet, see the companion guide: [User Guide: Installation and Usage](./USER-GUIDE-INSTALLATION-AND-USAGE.md).

---

## What the Report Is

The Platform Atlas compliance report is a self-contained HTML file — no internet connection, no special software. Open it in any modern browser. It is saved at:

```
~/.atlas/sessions/<session-name>/03_report.html
```

The report captures a point-in-time snapshot of your IAP deployment's configuration health. It does not make any changes to your environment — it only reports on what was found. Running a new audit and generating a new report is always safe.

---

## Report Layout at a Glance

The report opens with a header showing the organization name, environment, audit date, and the ruleset used. Below that, everything is organized into four areas:

1. **Compliance Score** — the overall health percentage and a breakdown by category
2. **Results Table** — every rule that was evaluated, with its status and message
3. **Extended Validation** — pattern-based checks that go beyond single-value rules
4. **Log Analysis** — a breakdown of platform log error groups and frequencies

Read the report top-to-bottom on first review. The score gives you the headline. The results table tells you exactly what passed and what didn't. Extended validation surfaces patterns that individual rules can't catch. Log analysis is the operational pulse.

---

## The Compliance Score

The score at the top of the report is the percentage of *evaluated* rules that passed:

```
Score = (rules that PASSED) / (rules that PASSED + rules that FAILED) × 100
```

**Skipped rules are not counted against the score.** If data couldn't be collected for a section of your deployment (for example, if SSH to a server was unavailable), those rules are marked SKIP and excluded from the denominator. This means a 90% score on 80 evaluated rules is comparable to a 90% score on 60 evaluated rules — the score reflects what was reachable.

The score is broken down by category below the headline number:

| Category | What it covers |
|---|---|
| Platform | IAP core configuration — logging levels, adapter settings, user accounts, healthcheck intervals |
| MongoDB | Database configuration, replica set health, security settings, performance parameters |
| Redis | Cache configuration, persistence, ACL security, sentinel topology |
| Gateway4 | Automation Gateway 4 configuration, logging, thread settings, database sizes |
| Gateway5 | Automation Gateway 5 environment variables, TLS settings, manager integration |

A healthy production deployment should target 90%+ on all categories. Scores below 80% in any single category warrant immediate review.

---

## Rule Statuses

Every row in the results table has one of four statuses:

### PASS

The rule evaluated successfully and the configuration meets the expected value. No action needed.

### FAIL

The rule evaluated successfully but the configuration does not meet the expected value. This requires attention. Each FAIL row shows:

- The rule ID and name (e.g., `RDS-002 — Redis MaxMemory Policy`)
- The severity of the failure (`critical`, `warning`, or `info`)
- A message explaining what was found and why it matters

Start with `critical` FAILs. Then work through `warning` FAILs. `info` FAILs are informational — worth knowing but not urgent.

### SKIP

The rule could not be evaluated because the data wasn't available. This happens when a collector failed during capture — for example, when Atlas couldn't connect to a service or couldn't read a config file.

A SKIP is not a PASS. It means "we couldn't check this." If you see many SKIPs in a category, investigate why data collection failed for that section. Common causes: the service was down during capture, SSH authentication failed, or a config file was in a non-standard location.

### ERROR

The rule encountered a problem during evaluation itself — for example, a data type mismatch or a malformed path. ERRORs are rare and usually indicate either a version mismatch between the ruleset and the captured data, or a data collection issue that produced unexpected output. Report ERRORs to your Itential contact along with the session export.

---

## Understanding Severity

Each rule has a severity level that indicates how urgently a FAIL should be addressed:

**Critical** — Directly impacts data integrity, availability, or security. Fix these before the next business cycle. Examples: MongoDB replica set unhealthy, Redis eviction policy set incorrectly, unsupported software version.

**Warning** — Configuration deviates from best practice in a way that could cause problems under load or over time. Fix these within the next planned maintenance window. Examples: logging level too verbose, network binding too broad, thread counts misconfigured.

**Info** — A configuration observation that is worth knowing but carries low operational risk on its own. Review these as part of ongoing hygiene. Examples: default accounts still enabled, audit retention at default value, optional features not yet configured.

---

## Extended Validation

Below the main results table, the Extended Validation section contains findings that require analyzing patterns across the full dataset rather than checking a single value. These checks run after the primary rules and are not included in the compliance score.

Extended validation covers things like:

- Adapter version consistency across Platform nodes
- ACL permission coverage for the `itential` Redis user
- Log error rate trends (errors per hour over the capture window)
- Replica set member lag in HA deployments

Each finding shows a description and a recommended action. Extended validation findings don't have PASS/FAIL statuses — they surface observations and let you decide how to act.

---

## Log Analysis

The final section of the report shows a breakdown of errors and warnings found in the Platform application log during the capture window. Entries are grouped by error type and sorted by frequency.

This section helps you identify recurring error patterns that may not surface in configuration rules — for example, an adapter that is configured correctly but failing at runtime, or a workflow error that is happening frequently enough to indicate a systemic issue.

A handful of logged warnings is normal in any running system. Dozens or hundreds of the same error per hour is not.

---

## Acting on Results: Remediation Examples

The following examples walk through real FAIL scenarios — what the report shows, what it means operationally, and how to fix it.

---

### Example 1 — RDS-002: Redis MaxMemory Policy (Critical)

**What the report shows:**

> Redis maxmemory-policy is not set to 'noeviction' — with other policies, Redis may silently evict important data when memory pressure increases.

**What it means:**

Redis has a configurable `maxmemory-policy` that controls what happens when Redis runs out of memory. IAP requires this to be set to `noeviction`, which means Redis will refuse new writes rather than silently discard existing data. If it's set to any other policy (such as `allkeys-lru` or `volatile-lru`), Redis will delete data without warning when memory is tight — which can cause IAP workflows and jobs to lose state in ways that are very hard to diagnose.

**How to fix it:**

1. Open `/etc/redis/redis.conf` on the Redis server.
2. Find the `maxmemory-policy` setting. If it doesn't exist, add it.
3. Set it to:
   ```
   maxmemory-policy noeviction
   ```
4. Reload the Redis configuration without restarting the service:
   ```bash
   redis-cli -a <password> CONFIG SET maxmemory-policy noeviction
   ```
5. Verify the change took effect:
   ```bash
   redis-cli -a <password> CONFIG GET maxmemory-policy
   ```
   You should see `noeviction` in the output.
6. Make sure the change is persisted in `redis.conf` so it survives a restart.

Re-run a capture and validate to confirm the rule passes.

---

### Example 2 — MDB-002: MongoDB Bind IP (Warning)

**What the report shows:**

> MongoDB is bound to 0.0.0.0 (all interfaces) — this exposes the database to all network traffic and is a security risk in production.

**What it means:**

The `net.bindIp` setting in `mongod.conf` controls which network interfaces MongoDB listens on. A value of `0.0.0.0` means MongoDB accepts connections on every interface on the server — including interfaces that face external networks or DMZs. MongoDB should only listen on the interfaces that IAP and your monitoring systems actually use.

**How to fix it:**

1. Identify the internal IP address(es) that IAP uses to connect to MongoDB. For a typical standalone deployment, this is the loopback address (`127.0.0.1`) and the server's private LAN address. For replica sets, include the addresses of all replica set members.
2. Open `/etc/mongod.conf` on each MongoDB server.
3. Update the `net` section:
   ```yaml
   net:
     bindIp: 127.0.0.1,<private-lan-ip>
   ```
   For replica sets, include all member IPs:
   ```yaml
   net:
     bindIp: 127.0.0.1,10.0.1.10,10.0.1.11,10.0.1.12
   ```
4. Restart MongoDB:
   ```bash
   sudo systemctl restart mongod
   ```
5. Confirm that MongoDB is still reachable from all IAP nodes before closing the maintenance window.

**Note:** Restarting MongoDB on a replica set primary triggers an election. Do this during a maintenance window and ensure all replica members are healthy before starting.

---

### Example 3 — IAG-003: Gateway4 HTTP Server Threads (Critical)

**What the report shows:**

> HTTP server threads are below the recommended value of 3x the CPU core count — this can create a bottleneck under load and cause request timeouts.

**What it means:**

Automation Gateway 4 handles incoming HTTP requests using a fixed thread pool. The recommended value is 3 times the number of logical CPU cores on the server. A server with 8 cores should have 24 HTTP server threads. If this is lower, Gateway will queue requests under load, leading to timeouts — particularly for workflows that make heavy use of Gateway as a job runner.

**How to fix it:**

1. Check the CPU count of the Gateway server:
   ```bash
   nproc --all
   ```
2. Multiply that number by 3. For an 8-core server, the value is `24`.
3. Open the Gateway configuration (typically `/etc/automation-gateway/properties.yml`):
   ```yaml
   http_server_threads: 24
   ```
4. Restart the Automation Gateway service:
   ```bash
   sudo systemctl restart automation-gateway
   ```
5. If Gateway is running behind a load balancer or is managed by Platform, confirm that the service registers back and Platform reports it as connected before re-enabling traffic.

---

### Example 4 — PLAT-002: Platform Core Logging Level (Warning)

**What the report shows:**

> Platform core logging is set to a verbose level (e.g., DEBUG or TRACE) — this generates excessive log output and should be set to INFO for production.

**What it means:**

IAP's core logging level is set below `info`. Debug or trace logging is useful during troubleshooting but generates very high log volume in production — filling disk faster, consuming I/O, and making it harder to find real errors when they occur.

**How to fix it:**

1. Log into the IAP UI as an administrator.
2. Navigate to **Admin** → **Settings** → **Logging**.
3. Set the core log level to `info`.
4. Save the change. The change takes effect immediately without a restart.

If you need to make this change via the Platform API:
```bash
curl -X PUT https://<platform-uri>/api/v2.0/settings/log_level \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"value": "info"}'
```

---

### Example 5 — MDB-006: Replica Set Healthy (Critical)

**What the report shows:**

> One or more members of the MongoDB replica set are unhealthy — investigate replica member status immediately to prevent data availability issues.

**What it means:**

At the time of capture, at least one member of the MongoDB replica set was not reporting a healthy status. This is one of the most urgent findings in any report — an unhealthy replica reduces your ability to tolerate node failures. If another member fails before this is resolved, you could lose write availability or (in worst case) data.

**How to fix it:**

1. Connect to the MongoDB primary and check the replica set status:
   ```bash
   mongosh --eval "rs.status()"
   ```
2. Look at the `stateStr` field for each member. Healthy members show `PRIMARY` or `SECONDARY`. Unhealthy members show `RECOVERING`, `DOWN`, `STARTUP`, `REMOVED`, or `UNKNOWN`.
3. For a member in `RECOVERING`: this is often normal after a restart or brief network interruption — give it a few minutes to catch up. Check `optimeDate` and `lastHeartbeatMessage` for details.
4. For a member in `DOWN` or `UNKNOWN`: SSH to that server and check whether the `mongod` service is running:
   ```bash
   sudo systemctl status mongod
   ```
   If it's down, check the MongoDB log (`/var/log/mongodb/mongod.log`) for the cause before restarting.
5. Once all members report `PRIMARY` or `SECONDARY`, re-run a capture and validate to confirm MDB-006 passes.

---

## Prioritizing What to Fix

If you have multiple FAILs and aren't sure where to start, use this order:

1. **Operational health first** — anything affecting availability right now: replica set issues, services down, unhealthy members (e.g., MDB-006). These can affect production within minutes of the next failure event.

2. **Security settings** — open network bindings, default accounts still active, unencrypted traffic (e.g., MDB-002, RDS-003, IAG-013). These are lower urgency day-to-day but are the first things an attacker targets.

3. **Configuration correctness** — values that differ from recommended but aren't immediately dangerous: thread counts, cache sizes, eviction policies (e.g., RDS-002, IAG-003). These tend to surface as performance problems under load.

4. **Logging and verbosity** — verbose logging doesn't break anything but degrades operations over time: disk fills up faster, real errors get buried (e.g., PLAT-002, IAG-001).

5. **Info-severity and audit hygiene** — default user accounts left enabled, audit retention at default values, optional features unconfigured (e.g., PLAT-001, IAG-005). Useful to address before a compliance review or external audit.

---

## Sharing the Report

The HTML report is self-contained — all styling and data is embedded. You can email it, put it in a shared drive, or include it in a customer-facing document package without any supporting files.

To package the report with session metadata for handoff:

```bash
platform-atlas session export <session-name>
```

This creates a ZIP file containing the report and a session summary. By default, raw capture data is excluded for security. If your Itential contact needs the raw data for deeper analysis, add `--no-redact`.

---

## Running a Follow-Up Audit

After making changes based on the report, run a new audit to confirm the fixes took effect. Create a new session so you have a clear before/after comparison:

```bash
platform-atlas session create <name>-follow-up --env <environment> --ruleset p6-master-ruleset --profile <profile>
platform-atlas session run all
```

Then compare the two sessions:

```bash
platform-atlas session diff <original-session> <follow-up-session>
```

The diff report highlights which rules improved, which regressed, and which stayed the same — giving you a clear record of progress.
