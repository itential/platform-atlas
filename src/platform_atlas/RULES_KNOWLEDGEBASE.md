# Platform Atlas — Rules Knowledge Base

> This document serves as the authoritative reference for all Platform Atlas
> validation rules.

---

# PLAT-001: Platform Default User

## Purpose

Validates that the default user is disabled in Platform 6 Production Environments.
For Production we recommend using more a more secure login system such as LDAP or SAML SSO.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate the `default_user_enabled` option.
3. Set it `false` or comment the line out.
4. Restart the platform for the change to take effect.

---

# PLAT-002: Platform Core Logging Level

## Purpose

Ensures that the core logging level is not set to DEBUG, which can potentially cause un-needed
performance overhead on Production systems, as well as logging un-needed data which could more
usage on the server.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add both options named `log_level` and `log_level_console`.
3. Set these both to either `info` or `warn`.
4. Restart the platform for the change to take effect.

---

# PLAT-003: Dead Process Check Enabled

## Purpose

When enabled, if a process in the Platform is considered dead it will be restarted.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `dead_process_check_enabled`.
3. Set the value to `true`.
4. Restart the platform for the change to take effect.

# PLAT-004: Dead Process Check Interval

## Purpose

The interval in seconds the Itential Platform will check the health of its services.
This check interval works in conjunction with `dead_process_max_period` to define when
Itential Platform considers the service lost.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `dead_process_check_interval`.
3. Set the value to `60` or higher.
4. Restart the platform for the change to take effect.

# PLAT-005: Dead Process Check Max Period

## Purpose

The threshold in seconds after which Itential Platform considers a service lost if it has
not received a health update. At each check interval, if the last received healthcheck
for this service is longer than the `dead_process_max_period`, then Itential Platform will
then consider the service lost.

**IMPORTANT**: The longest interval a service can be down before being caught is the sum
of `dead_process_max_period` and `dead_process_check_interval`.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `dead_process_max_period`.
3. Set the value to `60` or higher.
4. Restart the platform for the change to take effect.

# PLAT-006: Launch Timeout

## Purpose

The time in seconds the Itential Platform will wait for a service registration.
If a service has not registered itself to Itential Platform within this time frame,
it will consider it lost.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `service_launch_timeout`.
3. Set the value to `60` or higher.
4. Restart the platform for the change to take effect.

# PLAT-007: Launch Delay

## Purpose

The time in seconds the Itential Platform will wait between launching its services.
This can be used to manage the resource usage incurred by starting many services at the same time.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `service_launch_delay`.
3. Set the value to `5` or higher.
4. Restart the platform for the change to take effect.

# PLAT-008: Server ID

## Purpose

Specifies the name that the server uses to identify itself. Any valid string can be used,
but it is suggested that each name is unique to its environment. If a string is not provided,
serverName defaults to using a hashed value derived from the MAC address and Itential
Platform port values of the server.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `server_id`.
3. Set the value to any valid string that is unique to the environment.
4. Restart the platform for the change to take effect.

# PLAT-009: Broker Validation

## Purpose

If enabled, the platform will perform strict JSON Schema validation on messages into the
brokers. This can be disabled as it can decrease the performance of the Platform.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `broker_validation_enabled`.
3. Set the value to `false`.
4. Restart the platform for the change to take effect.

# PLAT-010: Platform Version

## Purpose

Validates the version of the Platform if it's within the latest version range.

## How to Fix

1. Please see online documentation for Upgrading Itential Platform.

# PLAT-011: Node Version

## Purpose

Validates if the NodeJS version matches the required version for the Platform

## How to Fix

1. Please see online documentation for Upgrading NodeJS version.

# PLAT-012: Device Count Polling Interval

## Purpose

The interval for how often Itential Platform polls for the number of devices,
measured in hours.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `device_count_polling_interval`.
3. Set the value to `24` or higher.
4. Restart the platform for the change to take effect.

# PLAT-013: External Request Timeout

## Purpose

The timeout for external API requests, measured in seconds.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `external_request_timeout`.
3. Set the value to `5` or higher.
4. Restart the platform for the change to take effect.

# PLAT-014: Log Max Files

## Purpose

The maximum number of log files maintained on the server. Once the maximum number
of files is reached, the oldest file will be deleted during log rotation.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `log_max_files`.
3. Set the value to `5` or higher.
4. Restart the platform for the change to take effect.

# PLAT-015: Service Crash Recovery Max Retries

## Purpose

Specifies the amount of times services will retry on crash before stopping.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `service_crash_recovery_max_retries`.
3. Set the value to `5` or higher.
4. Restart the platform for the change to take effect.

# PLAT-016: Log File Max Size

## Purpose

The maximum size of each Itential Platform log file in bytes. Once the maximum
file size is reached, the Itential Platform log will be rotated.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `log_max_file_size`.
3. Set the value to `1048576` or higher.
4. Restart the platform for the change to take effect.

# PLAT-017: Auth Session TTL

## Purpose

The time in minutes before a user session expires.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `auth_session_ttl`.
3. Set the value to `60` or higher.
4. Restart the platform for the change to take effect.

# PLAT-018: Webserver Cache Control Enabled

## Purpose

A toggle to instruct the webserver to include HTTP cache control headers on the response.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `webserver_cache_control_enabled`.
3. Set the value to `true`.
4. Restart the platform for the change to take effect.

# PLAT-019: Webserver HTTP Allowed Optional Verbs

## Purpose

The set of allowed HTTP verbs in addition to those defined in the standard HTTP/1.1 protocol.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `webserver_http_allowed_optional_verbs`.
3. If there are any extra verbs, are there any specific reasons why these values are needed?
4. Restart the platform for the change to take effect.

# PLAT-020: Webserver HTTPS Enabled

## Purpose

If true, allows the webserver to respond to secure HTTPS requests.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `webserver_https_enabled`.
3. Set the value to `true`.
4. Restart the platform for the change to take effect.

# PLAT-021: Webserver HTTP Enabled

## Purpose

If true, allows the webserver to respond to insecure HTTP requests.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `webserver_http_enabled`.
3. Set the value to `false`.
4. Restart the platform for the change to take effect.

# PLAT-022: Webserver Timeout

## Purpose

Timeout to use for incoming HTTP requests to the platform API, in milliseconds.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `webserver_timeout`.
3. Set the value to `300000` or higher.
4. Restart the platform for the change to take effect.

# PLAT-023: Redis Connect Timeout

## Purpose

The maximum time in milliseconds to wait for initial Redis connection before timing out.
If not set, defaults to 30000ms (30 seconds).

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `redis_connect_timeout`.
3. Set the value to `300000` or higher.
4. Restart the platform for the change to take effect.

# PLAT-024: Mongo Auth Enabled

## Purpose

Instructs the MongoDB driver to use the configured username/password when connecting to MongoDB.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `mongo_auth_enabled`.
3. Set the value to `true`.
4. Restart the platform for the change to take effect.

# PLAT-025: Mongo Max Idle Time

## Purpose

The maximum number of milliseconds that a connection can remain idle in the pool.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `mongo_max_idle_time_ms`.
3. Set the value to `1` or higher.
4. Restart the platform for the change to take effect.

# PLAT-026: MongoDB Bypass Version Check

## Purpose

If true, the server will not check if it is connecting to a compatible MongoDB version.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `mongo_bypass_version_check`.
3. Set the value to `false`.
4. Restart the platform for the change to take effect.

# PLAT-027: Mongo URL

## Purpose

The MongoDB connection string. For a replica set this will include all members of the
replica set. For Mongo Atlas this will be the SRV connection format. This checks if URL
contains properties like connectionTimeout or read or write concern values.
Remove any unwanted properties.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `mongo_url`.
3. Check for any additional properties after the `?` if they are needed.
4. Restart the platform for the change to take effect.

# PLAT-028: Mongo TLS Enabled

## Purpose

Instruct the MongoDB driver to use TLS protocols when connecting to the database.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `mongo_tls_enabled`.
3. Set the value to `true`.
4. Restart the platform for the change to take effect.

# PLAT-029: Max Retries Per Request

## Purpose

The maximum number of times to retry a request to Redis when the connection is lost.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `redis_max_retries_per_request`.
3. Set the value to `20` or higher.
4. Restart the platform for the change to take effect.

# PLAT-030: Redis Max Heartbeat Write Retries

## Purpose

The maximum number of times to retry writing a heartbeat message to Redis from a service.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `redis_max_heartbeat_write_retries`.
3. Set the value to `20`.
4. Restart the platform for the change to take effect.

# PLAT-031: Task Worker Enabled

## Purpose

If true, will start working tasks immediately after the server startup process is complete.
If false, the task worker must be enabled manually via the UI/API.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `task_worker_enabled`.
3. Set the value to `true`.
4. Restart the platform for the change to take effect.

# PLAT-032: Valut Read Only

## Purpose

If true, only reads secrets from Hashicorp Vault. Otherwise, the platform can write secrets
to Vault for storage.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `vault_read_only`.
3. Set the value to `true`.
4. Restart the platform for the change to take effect.

# PLAT-033: Profile Enabled

## Purpose

The name of the profile document to load from the MongoDB where legacy configuration properties
are stored. Not required for installations that are using environment variables or a properties file.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `profile_id`.
3. Comment out this variable or remove the name if not using legacy profiles.
4. Restart the platform for the change to take effect.

# PLAT-034: TLS Max Version

## Purpose

Maximum permitted TLS version

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `webserver_https_tls_max_version`.
3. Set the value to `1.3`.
4. Restart the platform for the change to take effect.

# PLAT-035: TLS Min Version

## Purpose

Minimum permitted TLS version

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `webserver_https_tls_min_version`.
3. Set the value to `1.2`.
4. Restart the platform for the change to take effect.

# PLAT-036: Redis DB Name

## Purpose

Checks to ensure the Redis DB Name does not contain any special characters or whitespaces.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `redis_name`.
3. Verify that the value for that option does not contain any special characters or whitespaces

# PLAT-037: Redis DB

## Purpose

Validates that the redis db is being used, as it is important that all the transactions are happening to same db.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `redis_db`.
3. Set the value to the redis database number you are using (default: 0)
4. Restart the platform for the change to take effect.

# PLAT-038: AGManager Pronghorn JSON Size

## Purpose

Checks the AGManager pronghorn.json file size to see if it's too large.

## How to Fix

1. Recommened to upgrade to the newest version of Platform 6 which uses Redis to store AGManager tasks.

However, if the platform cannot be upgraded:

1. If on a lower version of Platform 6, you can reduce this by removing unused collections such as
the path `/opt/automation-gateway/ansible/collections` and refreshing the collections module in IAG.
2. Double-check to ensure no tasks are being used by Automation Gateway from any removed paths first.
3. Finally, run Undiscover All and Discover All in IAP to refresh the AGManager pronghorn.json tasks.

# PLAT-039: NSO Netconf Frame Size

## Purpose

Validates the Frame Size parameter in the NSO adapter for optimal connectivity.

## How to Fix

1. Open the Adapter Properties for your NSO adapter
2. Locate the `frame_size` setting (or add it to `properties.properties.netconf`)
3. Ensure the value is set to `16376`, change this if set to a different value.

# PLAT-040: Platform Python Version Check

## Purpose

Validates that Python 3.11 is installed on the Platform server

## How to Fix

1. If Python 3.11 is not installed, please install it with `dnf install python3.11`

# PLAT-041: Gateway Manager Version Check

## Purpose

Validates the installed version of Gateway Manager in the Platform.

## How to Fix

1. If Gateway Manager is oudated, please install the latest version from Nexus Repo.

# PLAT-042: Service Healthcheck Unhealthy Threshold

## Purpose

Validates the threshold for unhealthy service healthchecks.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `service_health_check_unhealthy_threshold`.
3. Set the value to `3`
4. Restart the platform for the change to take effect.

# PLAT-043: Service Healthcheck Interval

## Purpose

Validates the interval for the service healthchecks

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `service_health_check_interval`.
3. Set this to a preferred value in the range of `5` to `30`
4. Restart the platform for the change to take effect.

# PLAT-044: Service Blacklist

## Purpose

Validates if there are any services blacklisted

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `service_blacklist`.
3. Review any services to ensure this is correct for your usage.

# PLAT-045: Service Shutdown Timeout

## Purpose

Validate if the Shutdown Timeout is greater than 0

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `platform_shutdown_timeout`.
3. Set the value to `3` or higher.
4. Restart the platform for the change to take effect.

# PLAT-046: Mongo Max Pool Size

## Purpose

Validates the max pool size for MongoDB.

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `mongo_max_pool_size`.
3. Set the value to `100` or greater.
4. Restart the platform for the change to take effect.

# PLAT-047: Redis TLS Enabled

## Purpose

Validates if TLS is enabled for Redis

## How to Fix

1. Open the platform's `platform.properties` file.
2. Locate or add the option named `redis_tls`.
3. Set the value to an empty object of `{}`.
4. Restart the platform for the change to take effect.

# IAG-001: Logging Level

## Purpose

Validates the logging level for Automation Gateway 4 is not too verbose.

## How to Fix

1. Open the Automation Gateway `properties.yml` file.
2. Set `logging_level` to INFO.
3. Save file and restart Automation Gateway.

# IAG-002: HTTP Logging Level

## Purpose

Validates the HTTP logging level for Automation Gateway 4 is not too verbose.

## How to Fix

1. Open the Automation Gateway `properties.yml` file.
2. Set `http_logging_level` to INFO.
3. Save file and restart Automation Gateway.

# IAG-003: HTTP Server Threads

## Purpose

Validates that the HTTP Server Threads for Automation Gateway is 3x the number of CPU cores.

## How to Fix

1. Open the Automation Gateway `properties.yml` file.
2. Calculate 3x the number of CPU cores on your server.
3. Set `http_server_threads` to the 3x CPU core value (ie: *16-cores x 3 = 48*)
4. Save file and restart Automation Gateway.

# IAG-004: Gateway4 Version

## Purpose

Validates the current Automation Gateway 4 version.

## How to Fix

1. Please see the online documentation for upgrading Automation Gateway 4.

# IAG-005: Gateway4 Audit Retention Days

## Purpose

Validates the Audit Retention Days in Automation Gateway for the Audit Log Database

## How to Fix

1. Open the Automation Gateway `properties.yml` file.
2. Set `audit_retention_days` to less than 30 days.
3. Save file and restart Automation Gateway.

# IAG-006: Ansible Debug

## Purpose

Validates that the Ansible Debug log setting is not too verbose.

## How to Fix

1. Open the Automation Gateway `properties.yml` file.
2. Set `ansible_debug` to `False`.
3. Save file and restart Automation Gateway.

# IAG-007: Gateway4 LDAP Enabled

## Purpose

Checks if LDAP is being used for Automation Gateway for better login security.

## How to Fix

1. Log into Automation Gateway.
2. Go to the `Configuration>LDAP` page and fill out your LDAP connection settings.
1. Open the Automation Gateway `properties.yml` file.
2. Set `ldap_secure_enabled` to `True`.
3. Save file and restart Automation Gateway.

# IAG-008: Gateway4 Sync Config

## Purpose

Checks if Sync Config is enabled in the systemd service file for Gateway 4. This flag will
automatically sync settings from `properties.yml` into Gateway4 on restart.

## How to Fix

1. Edit the Automation Gateway 4 systemd service file with `systemctl edit automation-gateway`
2. Add or remove the flag `--sync-config` from the ExecStart line
3. Reload systemd with the command `systemctl daemon-reload`
3. Restart Automation Gateway.

# IAG-009: Gateway4 Main Database Size

## Purpose

Validates that the Gateway4 Main Database size isn't too large

## How to Fix

1. If the main database is too large, this would usually indicate that there
may be too many devices and/or each device JSON is too large.
2. Please work with Itential Support to determine the exact cause and remediation steps.

# IAG-010: Gateway4 Audit Database Size

## Purpose

Validates that the Gateway4 Audit Database size isn't too large

## How to Fix

1. The Audit Database file can be deleted after stopping Gateway4, and will be re-created on restart.
2. To keep the size down, reduce the number of days to keep the logs.
3. Please see `IAG-005` for more information on updating this.

# IAG-011: Gateway4 Exec History Database Size

## Purpose

Validates that the Gateway4 Exec History Database size isn't too large

## How to Fix

1. The Exec History Database file can be deleted after stopping Gateway4, and will be re-created on restart.
2. To keep the size down, reduce the number of days to keep the logs.
3. Please see `IAG-005` for more information on updating this.

# IAG-012: Gateway Store Backend

## Purpose

Validates that the `GATEWAY_STORE_BACKEND` variable is explicitly set on the IAG 5 instance.
Without an explicit backend, the gateway defaults to the `local` (single-file) store, which
is not suitable for clustered deployments with runner nodes or multiple controller nodes.

## How to Fix

1. Decide on the appropriate store backend for your deployment:
   - `local` — Single-file on-disk store. Acceptable for standalone (single-node) deployments.
   - `etcd` — Distributed key-value store. **Required** for deployments with runner nodes or multiple controller nodes.
   - `dynamodb` — Amazon DynamoDB. Required for AWS-hosted distributed deployments.
   - `memory` — In-memory only, not persistent across restarts. Not recommended for production.
2. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [store]
   backend = etcd
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_STORE_BACKEND=etcd
   ```
3. If using `etcd` or `dynamodb`, configure the additional backend-specific variables
   (hosts, TLS, credentials) as described in the Itential IAG 5 documentation.

# IAG-013: Gateway Client TLS

## Purpose

Validates that `GATEWAY_CLIENT_USE_TLS` is set to `true`, ensuring that when this IAG 5
instance operates as a gateway client connecting to a gateway server, it communicates over
an encrypted TLS connection rather than plaintext.

## How to Fix

1. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [client]
   use_tls = true
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_CLIENT_USE_TLS=true
   ```
2. Ensure the related certificate variables are also configured, as they are required when
   TLS is enabled:
   - `GATEWAY_CLIENT_CERTIFICATE_FILE` — path to the client certificate (see IAG-027).
   - `GATEWAY_CLIENT_PRIVATE_KEY_FILE` — path to the client private key.

# IAG-014: Gateway Logging Levels

## Purpose

Validates that the `GATEWAY_LOG_LEVEL` for IAG 5 is not set to an overly verbose level
(`TRACE` or `DEBUG`) in a production environment. Excessively verbose logging generates
unnecessary I/O overhead and can fill disk space faster than expected.

## How to Fix

1. Set the log level to `INFO` (recommended for production) in the gateway configuration
   file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [log]
   level = INFO
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_LOG_LEVEL=INFO
   ```
2. Valid levels in order of verbosity are: `TRACE`, `DEBUG`, `INFO`, `WARN`, `ERROR`,
   `FATAL`, `DISABLED`. Use `WARN` or higher to reduce log volume further if needed.

# IAG-015: Gateway Connect Enabled

## Purpose

Validates that `GATEWAY_CONNECT_ENABLED` is set to `true`, confirming that this IAG 5
instance is connected to Itential Platform's Gateway Manager. Without this enabled, the
gateway will not register with Platform and cannot receive automation execution requests.

## How to Fix

1. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [connect]
   enabled = true
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_CONNECT_ENABLED=true
   ```
2. Ensure `GATEWAY_CONNECT_HOSTS` is also configured with the Gateway Manager host and port:
   ```ini
   [connect]
   hosts = <gateway-manager-host>:8080
   ```

# IAG-016: Gateway Connect Insecure TLS

## Purpose

Validates that `GATEWAY_CONNECT_INSECURE_TLS` is set to `false`, ensuring that the gateway
fully verifies TLS certificates when connecting to Itential Platform's Gateway Manager.
When set to `true`, certificate verification is skipped, which exposes the connection to
potential man-in-the-middle attacks.

## How to Fix

1. Set the variable to `false` in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [connect]
   insecure_tls = false
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_CONNECT_INSECURE_TLS=false
   ```
2. If TLS verification was disabled due to certificate issues, ensure the correct CA
   certificate and connect certificate files are configured instead of bypassing verification.
   See `GATEWAY_CONNECT_CERTIFICATE_FILE` (IAG-028) for details.

# IAG-017: Gateway Server TLS

## Purpose

Validates that `GATEWAY_SERVER_USE_TLS` is set to `true`, ensuring that the IAG 5 server
requires TLS when accepting connections from gateway clients and runner nodes. Disabling
this exposes all gRPC communication between gateway components to interception.

## How to Fix

1. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [server]
   use_tls = true
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_SERVER_USE_TLS=true
   ```
2. When TLS is enabled, `GATEWAY_SERVER_CERTIFICATE_FILE` and `GATEWAY_SERVER_PRIVATE_KEY_FILE`
   must also be set. See IAG-026 for the certificate file check.

# IAG-018: Gateway Feature: Ansible

## Purpose

An informational check that reports whether the Ansible feature is enabled or disabled on
this IAG 5 instance via `GATEWAY_FEATURES_ANSIBLE_ENABLED`. This rule does not enforce a
pass/fail outcome — it simply documents the current state of Ansible support for the audit
report.

## How to Fix

1. If Ansible should be enabled, set the variable in the gateway configuration file
   (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [features]
   ansible_enabled = true
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_FEATURES_ANSIBLE_ENABLED=true
   ```
2. If Ansible is intentionally disabled (e.g., this node does not need to run playbooks),
   no action is required — this is an informational rule only.

# IAG-019: Gateway Feature: Hostkeys

## Purpose

An informational check that reports whether the SSH hostkeys management feature is enabled
or disabled on this IAG 5 instance via `GATEWAY_FEATURES_HOSTKEYS_ENABLED`. This rule does
not enforce a pass/fail — it documents the current state for the audit report.

## How to Fix

1. If hostkeys management should be enabled, set the variable in the gateway configuration
   file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [features]
   hostkeys_enabled = true
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_FEATURES_HOSTKEYS_ENABLED=true
   ```
2. If hostkeys is intentionally disabled, no action is required.

# IAG-020: Gateway Feature: OpenTofu

## Purpose

An informational check that reports whether the OpenTofu (infrastructure-as-code) feature
is enabled or disabled on this IAG 5 instance via `GATEWAY_FEATURES_OPENTOFU_ENABLED`. This
rule does not enforce a pass/fail — it documents the current state for the audit report.

## How to Fix

1. If OpenTofu should be enabled, set the variable in the gateway configuration file
   (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [features]
   opentofu_enabled = true
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_FEATURES_OPENTOFU_ENABLED=true
   ```
2. If OpenTofu is intentionally disabled, no action is required.

# IAG-021: Gateway Feature: Python

## Purpose

An informational check that reports whether the Python script execution feature is enabled
or disabled on this IAG 5 instance via `GATEWAY_FEATURES_PYTHON_ENABLED`. This rule does
not enforce a pass/fail — it documents the current state for the audit report.

## How to Fix

1. If Python execution should be enabled, set the variable in the gateway configuration file
   (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [features]
   python_enabled = true
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_FEATURES_PYTHON_ENABLED=true
   ```
2. If Python is intentionally disabled, no action is required.

# IAG-022: Gateway Runner TLS

## Purpose

Validates that `GATEWAY_RUNNER_USE_TLS` is set to `true`, ensuring that the IAG 5 runner
node requires TLS when accepting service execution requests from a gateway server. Disabling
this exposes the gRPC runner communication channel to potential interception.

## How to Fix

1. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [runner]
   use_tls = true
   ```
   Or set it as an environment variable:
   ```bash
   export GATEWAY_RUNNER_USE_TLS=true
   ```
2. When TLS is enabled, `GATEWAY_RUNNER_CERTIFICATE_FILE` and `GATEWAY_RUNNER_PRIVATE_KEY_FILE`
   must also be configured. See IAG-029 for the certificate file check.

# IAG-023: Gateway Console Log: JSON

## Purpose

An informational check that reports whether IAG 5 console logs are formatted as JSON via
`GATEWAY_LOG_CONSOLE_JSON`. JSON-formatted logs integrate with log aggregation tools (e.g.,
Splunk, ELK), but require matching configuration on the file log side (see IAG-024).
This rule flags the current state for awareness rather than enforcing a specific value.

## How to Fix

1. To enable JSON-formatted console logging, set the variable in the gateway configuration
   file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [log]
   console_json = true
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_LOG_CONSOLE_JSON=true
   ```
2. If enabling JSON console logs, it is recommended to also enable JSON file logs
   (`GATEWAY_LOG_FILE_JSON = true`) for consistency. See IAG-024.

# IAG-024: Gateway File Log: JSON

## Purpose

An informational check that reports whether IAG 5 file logs are written in JSON format via
`GATEWAY_LOG_FILE_JSON`. JSON file logs are useful for log aggregation pipelines but should
be configured consistently alongside the console log format (see IAG-023). This rule flags
the current state for awareness rather than enforcing a specific value.

## How to Fix

1. To enable JSON-formatted file logging, set the variable in the gateway configuration
   file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [log]
   file_json = true
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_LOG_FILE_JSON=true
   ```
2. If enabling JSON file logs, it is recommended to also enable JSON console logs
   (`GATEWAY_LOG_CONSOLE_JSON = true`) for consistency. See IAG-023.
3. Log files are written to the directory specified by `GATEWAY_LOG_SERVER_DIR`
   (default: `/var/log/gateway`).

# IAG-025: Gateway Connect Redundancy Check

## Purpose

An informational check that reports whether IAG 5 High Availability (HA) mode is enabled
for the Gateway Manager connection via `GATEWAY_CONNECT_SERVER_HA_ENABLED`. When enabled,
multiple gateway nodes form an active/standby cluster so that if the active node loses its
connection to Gateway Manager, a standby node automatically takes over.

## How to Fix

1. To enable Gateway Connect HA, set the variable in the gateway configuration file
   (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [connect]
   server_ha_enabled = true
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_CONNECT_SERVER_HA_ENABLED=true
   ```
2. All nodes in the HA cluster must share the same `GATEWAY_APPLICATION_CLUSTER_ID`.
3. Designate exactly one node as the primary using `GATEWAY_CONNECT_SERVER_HA_IS_PRIMARY = true`.
   See IAG-026 for the primary check.
4. Ensure the store backend is set to `etcd` or `dynamodb` — HA requires a distributed store.

# IAG-026: Gateway Connect HA Primary Check

## Purpose

When Gateway Connect HA is enabled (IAG-025), this rule validates that
`GATEWAY_CONNECT_SERVER_HA_IS_PRIMARY` is set to `true` on exactly one node in the cluster.
The primary node takes precedence in connecting to Gateway Manager when all nodes are online.
This rule only runs when IAG-025 reports that HA is active.

## How to Fix

1. On the node you want to designate as the HA primary, set the variable in the gateway
   configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [connect]
   server_ha_is_primary = true
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_CONNECT_SERVER_HA_IS_PRIMARY=true
   ```
2. Ensure all other nodes in the cluster have this set to `false` (the default). Only one
   node in the cluster should have `server_ha_is_primary = true`.

# IAG-027: Gateway Client Certificate File

## Purpose

When `GATEWAY_CLIENT_USE_TLS` is enabled (IAG-013), this rule validates that
`GATEWAY_CLIENT_CERTIFICATE_FILE` is set to a non-empty value. Without a certificate file,
the gateway client cannot establish a TLS connection to a gateway server. This rule only
runs when IAG-013 passes.

## How to Fix

1. Obtain or generate the TLS certificate for the gateway client. In most deployments this
   certificate is provisioned by your organization's PKI or the Itential installation process.
2. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [client]
   certificate_file = /etc/gateway/certificates/client.pem
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_CLIENT_CERTIFICATE_FILE=/etc/gateway/certificates/client.pem
   ```
3. Ensure the corresponding private key is also set via `GATEWAY_CLIENT_PRIVATE_KEY_FILE`.
4. Verify the certificate file is readable by the gateway process user.

# IAG-028: Gateway Connect Certificate File

## Purpose

When `GATEWAY_CONNECT_INSECURE_TLS` is `false`, this rule validates that
`GATEWAY_CONNECT_CERTIFICATE_FILE` is set to a non-empty value. This certificate is used
by the gateway when establishing its secure connection to Gateway Manager. This rule only
runs when IAG-016 passes (i.e., insecure TLS is disabled).

## How to Fix

1. Ensure the Gateway Manager certificate file (PEM format) is present on the server. The
   default expected path is `/etc/gateway/certificates/gw-manager.pem`.
2. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [connect]
   certificate_file = /etc/gateway/certificates/gw-manager.pem
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_CONNECT_CERTIFICATE_FILE=/etc/gateway/certificates/gw-manager.pem
   ```
3. Ensure the corresponding private key is also set via `GATEWAY_CONNECT_PRIVATE_KEY_FILE`
   (default: `/etc/gateway/certificates/gw-manager-key.pem`).
4. Verify that both files are readable by the gateway process user.

# IAG-029: Gateway Runner Certificate File

## Purpose

When `GATEWAY_RUNNER_USE_TLS` is enabled (IAG-022), this rule validates that
`GATEWAY_RUNNER_CERTIFICATE_FILE` is set to a non-empty value. Without a certificate file
configured, the runner cannot complete a TLS handshake with the gateway server. This rule
only runs when IAG-022 passes.

## How to Fix

1. Obtain or generate the TLS certificate for the gateway runner. In most deployments this
   is provisioned by your organization's PKI or the Itential installation process.
2. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [runner]
   certificate_file = /etc/gateway/certificates/runner.pem
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_RUNNER_CERTIFICATE_FILE=/etc/gateway/certificates/runner.pem
   ```
3. Ensure the corresponding private key is also set via `GATEWAY_RUNNER_PRIVATE_KEY_FILE`.
4. Verify that both files are readable by the gateway process user.

# IAG-030: Gateway Version Check

## Purpose

Validates that the installed IAG 5 version (`iagctl`) is at least `5.2`. Older versions
may contain unpatched security vulnerabilities or lack features required for compatibility
with the currently deployed Itential Platform version.

## How to Fix

1. Check the current `iagctl` version with:
   ```bash
   iagctl version
   ```
2. If outdated, download the latest IAG 5 release package from the Itential Nexus repository
   or from your organization's internal package source.
3. Install the updated package. On RHEL 9:
   ```bash
   sudo dnf upgrade iagctl
   ```
4. Verify the new version with: `iagctl version`

# IAG-031: Gateway Custom Registries

## Purpose

An informational check that validates whether at least one custom dependency registry
(PyPI or Ansible Galaxy) has been configured in IAG 5. Custom registries are used to
serve Python packages or Ansible collections from an internal mirror, which is important
in air-gapped or restricted enterprise environments that cannot reach the public internet.

## How to Fix

1. If this environment requires internal registry mirrors (e.g., for air-gapped deployments),
   add a custom registry using `iagctl`:
   ```bash
   # Add a custom PyPI registry
   iagctl registry create --type pypi --name internal-pypi --url https://<internal-pypi-host>/simple/ --default

   # Add a custom Ansible Galaxy registry
   iagctl registry create --type galaxy --name internal-galaxy --url https://<internal-galaxy-host>/ --default
   ```
2. Verify registered registries with:
   ```bash
   iagctl registry list
   ```
3. If this is an internet-connected deployment that intentionally uses the public PyPI and
   Ansible Galaxy registries, no action is required — this is an informational rule only.

# IAG-032: Gateway Runner Anouncement Address

## Purpose

An informational check that validates whether `GATEWAY_RUNNER_ANNOUNCEMENT_ADDRESS` is
explicitly set on this IAG 5 runner node. This is the address the runner registers with
its cluster so the gateway server knows where to send execution requests. Without it set
explicitly, the runner will attempt to auto-detect its own IP, which can resolve incorrectly
in multi-homed or NATted environments.

## How to Fix

1. Determine the IP address or hostname that the gateway server should use to reach this
   runner node on the network.
2. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [runner]
   announcement_address = <runner-host-or-ip>
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_RUNNER_ANNOUNCEMENT_ADDRESS=<runner-host-or-ip>
   ```
3. This should be the address reachable by the gateway server, not `127.0.0.1`, unless
   the server and runner are co-located on the same host.

# IAG-033: Gateway Server Distributed Execution

## Purpose

An informational check that validates whether `GATEWAY_SERVER_DISTRIBUTED_EXECUTION` is
set to `true`. When enabled, the gateway server delegates service execution to separate
runner nodes rather than running services on the server node itself. This is the recommended
architecture for production deployments to separate the control plane from the execution plane.

## How to Fix

1. If this deployment uses dedicated runner nodes (separate from the gateway server), set
   the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [server]
   distributed_execution = true
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_SERVER_DISTRIBUTED_EXECUTION=true
   ```
2. When enabling distributed execution, ensure at least one runner node is deployed and
   connected to this server with `GATEWAY_APPLICATION_MODE` set to `runner` and the same
   `GATEWAY_APPLICATION_CLUSTER_ID`.
3. If this is a standalone "all-in-one" deployment where the server also executes services
   locally, this setting should remain `false` — this is an informational rule only.

# IAG-034: Gateway Server Certificate File

## Purpose

When `GATEWAY_SERVER_USE_TLS` is enabled (IAG-017), this rule validates that
`GATEWAY_SERVER_CERTIFICATE_FILE` is set to a non-empty value. Without a certificate file,
the gateway server cannot complete TLS handshakes with connecting clients and runner nodes.
This rule only runs when IAG-017 passes.

## How to Fix

1. Obtain or generate the TLS certificate for the gateway server. In most deployments this
   is provisioned by your organization's PKI or the Itential installation process.
2. Set the variable in the gateway configuration file (e.g., `/etc/gateway/gateway.conf`):
   ```ini
   [server]
   certificate_file = /etc/gateway/certificates/server.pem
   ```
   Or as an environment variable:
   ```bash
   export GATEWAY_SERVER_CERTIFICATE_FILE=/etc/gateway/certificates/server.pem
   ```
3. Ensure the corresponding private key is also configured via `GATEWAY_SERVER_PRIVATE_KEY_FILE`.
4. Verify that both files are readable by the gateway process user.

# RDS-001: Redis Configuration File

## Purpose

Validates that Redis is running with a configuration file loaded from the standard path
(`/etc/redis/redis.conf`). Without a configuration file, Redis runs with compiled-in defaults
which may not be suitable for production environments.

## How to Fix

1. Verify that `/etc/redis/redis.conf` exists on the server.
2. If the file is missing, install the Redis package which provides the default config:
   ```bash
   sudo dnf install redis
   ```
3. Ensure the Redis systemd service is referencing the config file. Check the service file with:
   ```bash
   systemctl cat redis
   ```
4. The `ExecStart` line should include `redis-server /etc/redis/redis.conf`.
5. Restart Redis to apply: `sudo systemctl restart redis`.

# RDS-002: Redis MaxMemory Policy

## Purpose

Validates that the Redis `maxmemory-policy` is set to `noeviction`. This ensures Redis never
silently discards data when memory limits are reached — instead, it returns an error to the
client, which is critical for Itential Platform's use of Redis as a message broker and session store.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf`.
2. Locate or add the `maxmemory-policy` directive.
3. Set it to `noeviction`:
   ```
   maxmemory-policy noeviction
   ```
4. Restart Redis to apply: `sudo systemctl restart redis`.
5. You can verify the running value with: `redis-cli CONFIG GET maxmemory-policy`.

# RDS-003: Redis Default User

## Purpose

Validates that the Redis built-in `default` user has been removed or disabled from the ACL
user list. The default user has full permissions with no password by default, which is a
significant security risk in production environments.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf`.
2. Disable the default user by adding or updating the ACL entry:
   ```
   user default off nopass nocommands nokeys
   ```
3. Create a dedicated named user with a strong password and only the required permissions:
   ```
   user itential on >StrongPassword ~* +@all
   ```
4. Update the Itential Platform `platform.properties` file to use the new named user credentials
   in the Redis connection string.
5. Restart Redis to apply: `sudo systemctl restart redis`.

# RDS-004: Redis Network Binding

## Purpose

Validates that Redis is not bound to `0.0.0.0`, which would expose the Redis port on all
network interfaces. Redis should be bound only to the specific IP addresses required for
communication with Itential Platform nodes to reduce the attack surface.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf`.
2. Locate the `bind` directive and replace the wildcard address with specific interface IPs:
   ```
   bind 127.0.0.1 <platform-server-ip>
   ```
3. Only include IP addresses for interfaces that Itential Platform nodes need to connect on.
4. Restart Redis to apply: `sudo systemctl restart redis`.
5. Verify the binding with: `ss -tlnp | grep 6379`.

# RDS-005: Redis Version

## Purpose

Validates that the running Redis version is at least `7.4.0`. Older versions may contain
unpatched security vulnerabilities or lack features required by Itential Platform.

## How to Fix

1. Check the current Redis version with: `redis-server --version`
2. If outdated, upgrade Redis using the appropriate package manager. On RHEL 9:
   ```bash
   sudo dnf upgrade redis
   ```
3. If the required version is not available in the default repositories, enable the Remi
   repository or install from a trusted RPM source.
4. Restart Redis after upgrading: `sudo systemctl restart redis`.

# RDS-006: Redis TCP Keepalive

## Purpose

Validates that the Redis `tcp-keepalive` setting is within the recommended range of 60 to
120 seconds. This setting controls how frequently Redis sends TCP ACK packets to detect
and close dead client connections. A value that is too low increases overhead; too high
risks holding stale connections open unnecessarily.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf`.
2. Locate or add the `tcp-keepalive` directive and set it to a value between 60 and 120:
   ```
   tcp-keepalive 60
   ```
3. Restart Redis to apply: `sudo systemctl restart redis`.
4. Verify the running value with: `redis-cli CONFIG GET tcp-keepalive`.

# RDS-007: Redis Replica Check

## Purpose

Validates that Redis has at least one connected replica (formerly called a slave). Running
Redis in standalone mode without replication means there is no high-availability failover,
and a Redis outage will directly impact Itential Platform's availability.

## How to Fix

1. On the intended replica server, open `/etc/redis/redis.conf`.
2. Add the `replicaof` directive pointing to the primary Redis instance:
   ```
   replicaof <primary-ip> 6379
   ```
3. Restart Redis on the replica: `sudo systemctl restart redis`.
4. Confirm replication status on the primary with:
   ```bash
   redis-cli INFO replication
   ```
   The output should show `connected_slaves:1` or greater.

**Note:** Rules RDS-008 through RDS-016 only apply when this rule passes (replication is active).

# RDS-008: Replica Ping Period

## Purpose

Validates that the `repl-ping-replica-period` is set between 30 and 180 seconds. This is
the interval at which the Redis primary sends a PING to its replicas to confirm they are
alive. A value outside this range may lead to premature failover detection or delayed
identification of a lost replica.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf` on the **primary** server.
2. Locate or add the `repl-ping-replica-period` directive and set it within the valid range:
   ```
   repl-ping-replica-period 60
   ```
3. Restart Redis to apply: `sudo systemctl restart redis`.
4. Verify with: `redis-cli CONFIG GET repl-ping-replica-period`.

# RDS-009: Replica Timeout

## Purpose

Validates that the `repl-timeout` is set between 30 and 180 seconds. This value defines
how long the replica will wait for a response from the primary (or vice versa) before
considering the replication connection as timed out and triggering a reconnect.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf`.
2. Locate or add the `repl-timeout` directive and set it to a value between 30 and 180:
   ```
   repl-timeout 60
   ```
   **Note:** This value must always be greater than `repl-ping-replica-period`.
3. Restart Redis to apply: `sudo systemctl restart redis`.
4. Verify with: `redis-cli CONFIG GET repl-timeout`.

# RDS-010: Replica Backlog Size

## Purpose

Validates that the replication backlog (`repl-backlog-size`) is at least 512MB. The backlog
is a buffer that stores recent write commands so that a replica that briefly disconnects can
resync without requiring a full data transfer. An undersized backlog increases the likelihood
of expensive full re-syncs.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf` on the **primary** server.
2. Locate or add the `repl-backlog-size` directive and set it to 512MB or higher:
   ```
   repl-backlog-size 512mb
   ```
3. Restart Redis to apply: `sudo systemctl restart redis`.
4. Verify with: `redis-cli CONFIG GET repl-backlog-size`.

# RDS-011: Replica Max Lag

## Purpose

Validates that `min-replicas-max-lag` is set between 10 and 60 seconds. This setting defines
the maximum number of seconds a replica can lag behind the primary before the primary stops
accepting writes, protecting data consistency in replicated deployments.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf` on the **primary** server.
2. Locate or add the `min-replicas-max-lag` directive and set it within the valid range:
   ```
   min-replicas-max-lag 10
   ```
3. This setting is typically used alongside `min-replicas-to-write`. Ensure that setting
   is also configured appropriately (commonly `1`).
4. Restart Redis to apply: `sudo systemctl restart redis`.
5. Verify with: `redis-cli CONFIG GET min-replicas-max-lag`.

# RDS-012: No appendfsync on rewrite

## Purpose

Validates that the `no-appendfsync-on-rewrite` option is enabled (`yes`). When Redis rewrites
the AOF file in the background, having this disabled causes `fsync()` to be called during the
rewrite, which can cause significant latency. Enabling this option defers fsync during rewrites
for better write performance.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf`.
2. Locate or add the `no-appendfsync-on-rewrite` directive and enable it:
   ```
   no-appendfsync-on-rewrite yes
   ```
3. Restart Redis to apply: `sudo systemctl restart redis`.
4. Verify with: `redis-cli CONFIG GET no-appendfsync-on-rewrite`.

# RDS-013: Client Output Buffer Limit Replica

## Purpose

Validates that the replica client output buffer limit is set to the recommended values of
`512mb 128mb 60`. This controls how much data Redis will buffer for replica clients before
disconnecting them. Undersized buffers in high-throughput environments can cause replicas to
be repeatedly dropped and force full re-syncs.

## How to Fix

1. Open the Redis configuration file at `/etc/redis/redis.conf` on the **primary** server.
2. Locate or add the `client-output-buffer-limit` directive for the `replica` class:
   ```
   client-output-buffer-limit replica 512mb 128mb 60
   ```
   - `512mb` — hard limit: disconnects the replica if the buffer exceeds this size.
   - `128mb` — soft limit: triggers a soft-limit timer if the buffer exceeds this size.
   - `60` — soft limit seconds: disconnects the replica if the soft limit persists for 60 seconds.
3. Restart Redis to apply: `sudo systemctl restart redis`.
4. Verify with: `redis-cli CONFIG GET client-output-buffer-limit`.

# RDS-014: Sentinel Down After Milliseconds

## Purpose

Validates that the Sentinel `down-after-milliseconds` for the `itentialmaster` group is
set to `5000` ms (5 seconds). This is the time Sentinel waits without a response before
marking a Redis instance as subjectively down and initiating quorum checks for failover.

## How to Fix

1. Open the Sentinel configuration file (typically `/etc/redis/sentinel.conf`).
2. Locate or add the `sentinel down-after-milliseconds` directive for your master group:
   ```
   sentinel down-after-milliseconds itentialmaster 5000
   ```
3. Restart Redis Sentinel to apply:
   ```bash
   sudo systemctl restart redis-sentinel
   ```
4. Verify the running configuration with:
   ```bash
   redis-cli -p 26379 SENTINEL MASTERS
   ```

# RDS-015: Sentinel Parallel Syncs

## Purpose

Validates that `sentinel parallel-syncs` for the `itentialmaster` group is set to `1`.
This controls how many replicas can resync from the new primary simultaneously after a
failover. Setting this to `1` ensures replicas resync one at a time, preventing all replicas
from being unavailable simultaneously during a failover event.

## How to Fix

1. Open the Sentinel configuration file (typically `/etc/redis/sentinel.conf`).
2. Locate or add the `sentinel parallel-syncs` directive for your master group:
   ```
   sentinel parallel-syncs itentialmaster 1
   ```
3. Restart Redis Sentinel to apply:
   ```bash
   sudo systemctl restart redis-sentinel
   ```

# RDS-016: Sentinel Failover Timeout

## Purpose

Validates that the Sentinel `failover-timeout` for the `itentialmaster` group is set to
`60000` ms (60 seconds). This value controls several timeout behaviors during a failover,
including how long Sentinel will wait for a replica to be promoted, and how long before a
failed failover attempt can be retried.

## How to Fix

1. Open the Sentinel configuration file (typically `/etc/redis/sentinel.conf`).
2. Locate or add the `sentinel failover-timeout` directive for your master group:
   ```
   sentinel failover-timeout itentialmaster 60000
   ```
3. Restart Redis Sentinel to apply:
   ```bash
   sudo systemctl restart redis-sentinel
   ```
4. Verify with:
   ```bash
   redis-cli -p 26379 SENTINEL MASTERS
   ```

# MDB-001: Mongo Version

## Purpose

Validates that the running MongoDB version is at least `7.0.0`. Older versions may contain
unpatched security vulnerabilities or lack features and driver compatibility required by
Itential Platform.

## How to Fix

1. Check the current MongoDB version with:
   ```bash
   mongod --version
   ```
2. If outdated, upgrade MongoDB following the official MongoDB upgrade path. On RHEL 9,
   configure the MongoDB 7.0 repository and upgrade:
   ```bash
   sudo dnf upgrade mongodb-org
   ```
3. **Important:** Always follow MongoDB's incremental upgrade path — do not skip major versions.
4. Restart MongoDB after upgrading: `sudo systemctl restart mongod`.
5. Confirm the new version with: `mongosh --eval "db.version()"`.

# MDB-002: MongoDB Bind IP

## Purpose

Validates that MongoDB is not bound to the wildcard address `0.0.0.0`, which would expose
the MongoDB port on all network interfaces. MongoDB should only listen on the specific IP
addresses needed to communicate with Itential Platform nodes.

## How to Fix

1. Open the MongoDB configuration file at `/etc/mongod.conf`.
2. Locate the `net.bindIp` setting under the `net:` section and replace the wildcard with
   specific IP addresses:
   ```yaml
   net:
     bindIp: 127.0.0.1,<platform-server-ip>
   ```
3. Only include addresses required for Platform connectivity and local administration.
4. Restart MongoDB to apply: `sudo systemctl restart mongod`.
5. Verify the binding with: `ss -tlnp | grep 27017`.

# MDB-003: Mongo Repl defaultReadConcern

## Purpose

Validates that the MongoDB replica set's default read concern level is set to `majority`.
This ensures reads reflect data that has been acknowledged by a majority of replica set
members, preventing stale reads from a lagging secondary in the event of a failover.

## How to Fix

1. Connect to the MongoDB primary using `mongosh`.
2. Set the default read concern to `majority` using the `setDefaultRWConcern` admin command:
   ```javascript
   db.adminCommand({
     setDefaultRWConcern: 1,
     defaultReadConcern: { level: "majority" }
   })
   ```
3. Verify the change was applied:
   ```javascript
   db.adminCommand({ getDefaultRWConcern: 1 })
   ```
   Confirm the `defaultReadConcern.level` field shows `majority`.

# MDB-004: Mongo Repl defaultWriteConcern

## Purpose

Validates that the MongoDB replica set's default write concern is set to `majority`. This
ensures that write operations are only acknowledged after they have been committed on a
majority of replica set members, preventing data loss in the event of a primary failover.

## How to Fix

1. Connect to the MongoDB primary using `mongosh`.
2. Set the default write concern to `majority` using the `setDefaultRWConcern` admin command:
   ```javascript
   db.adminCommand({
     setDefaultRWConcern: 1,
     defaultWriteConcern: { w: "majority" }
   })
   ```
3. Verify the change was applied:
   ```javascript
   db.adminCommand({ getDefaultRWConcern: 1 })
   ```
   Confirm the `defaultWriteConcern.w` field shows `majority`.

# MDB-005: Replica Member Vote

## Purpose

Validates that the total number of voting members in the MongoDB replica set is at least 3
and is an odd number. An odd number of voters (3, 5, 7...) prevents split-brain scenarios
by ensuring a clear majority can always be achieved during an election.

## How to Fix

1. Connect to the MongoDB primary using `mongosh`.
2. Check the current replica set configuration:
   ```javascript
   rs.conf()
   ```
3. Review the `members` array and verify each member's `votes` field (default is `1`).
4. To add a new voting member or arbiter to reach an odd quorum:
   ```javascript
   rs.add({ host: "<new-member-host>:27017", votes: 1, priority: 1 })
   // Or add an arbiter (votes but holds no data):
   rs.addArb("<arbiter-host>:27017")
   ```
5. Re-run `rs.conf()` to confirm the total vote count is odd and ≥ 3.


# MDB-006: Replica Set Healthy

## Purpose

Validates that all members of the MongoDB replica set are reporting a healthy state. An
unhealthy replica set (members in `STARTUP`, `RECOVERING`, `UNKNOWN`, or `DOWN` states) can
impact data availability and puts the deployment at risk if the primary fails.

## How to Fix

1. Connect to the MongoDB primary using `mongosh` and check the replica set status:
   ```javascript
   rs.status()
   ```
2. Review the `members` array. Each member should show `stateStr: "PRIMARY"` or
   `stateStr: "SECONDARY"` with `health: 1`.
3. For members showing unhealthy states, investigate the following common causes:
   - **Network connectivity** — Ensure the affected node is reachable on port 27017.
   - **Disk space** — A full disk will cause MongoDB to become unresponsive.
   - **mongod not running** — Check the service with `sudo systemctl status mongod`.
   - **Replication lag** — A heavily lagging secondary may enter `RECOVERING` state.
4. Check the MongoDB logs on the unhealthy node for specific errors:
   ```bash
   sudo journalctl -u mongod --since "1 hour ago"
   ```
5. Once the underlying issue is resolved, the replica set should self-heal. Contact
   Itential Support if the issue persists.
