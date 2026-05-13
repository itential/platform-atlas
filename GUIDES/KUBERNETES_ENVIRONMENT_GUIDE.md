# Platform Atlas — Kubernetes Environment Guide

Running Platform Atlas against a Kubernetes-hosted IAP deployment is a lot more approachable than it might sound at first. You do not need SSH access to any server. You do not need to log into the cluster as an admin. For most of the audit, Atlas talks directly to the APIs it has always used — Platform OAuth, MongoDB, and Redis — exactly the same way it does for a traditional deployment. Kubernetes just changes where a few supplemental pieces of data come from.

This guide explains what Atlas actually does during a Kubernetes capture, what each data source contributes, and how to configure things so you get the most complete picture possible.

---

## The Short Version

If you are in a hurry:

- **Platform OAuth** (client ID + secret) is required and covers the majority of the audit.
- **MongoDB URI** and **Redis URI** are required the same as always — Atlas connects directly via `pymongo` and `redis-py`.
- **`values.yaml`** is optional but strongly recommended — it fills in resource sizing, QoS class, and Kubernetes-specific metadata.
- **`kubectl`** is optional — it adds live pod status and resource consumption, and serves as a last-resort fallback for platform config if the API is unavailable.

You can run a meaningful Kubernetes audit with only OAuth + MongoDB URI + Redis URI. Everything else improves coverage.

---

## What Atlas Does NOT Need

It is worth saying this up front, because it tends to surprise people:

- Atlas does **not** need SSH access to your Kubernetes nodes or pods.
- Atlas does **not** need cluster admin credentials or a high-privilege kubeconfig.
- Atlas does **not** need to read files off the container filesystem directly.
- Atlas does **not** deploy anything into your cluster — no pods, no jobs, no sidecars.

The audit is almost entirely API-driven. `kubectl` is used only for a small number of things the APIs genuinely cannot tell you (live scheduling state and resource consumption), and even then it is optional.

---

## Required: Platform OAuth, MongoDB, and Redis

These three connections are the backbone of every Atlas audit — Kubernetes or not.

### Platform OAuth

Atlas authenticates to IAP using a client ID and client secret, exactly as it does for bare-metal deployments. During capture, Atlas calls a set of IAP REST endpoints in parallel:

| Endpoint | What it gives you |
|---|---|
| `GET /health/status` | Service health, component status |
| `GET /health/server` | Platform release version, Node.js version, uptime, memory |
| `GET /server/config` | Full runtime configuration — this is the primary source for nearly all platform settings |
| `GET /health/adapters` | Adapter running state, version, health |
| `GET /health/applications` | Application running state, version, health |
| `GET /adapters` | Adapter connection properties |
| `GET /applications` | Application properties |
| `GET /indexes/status` | MongoDB index coverage |

The Platform API is where Atlas gets the platform release version, Node.js version, installed adapter and application versions, and the full runtime configuration. None of that requires `kubectl exec` into a pod. It comes straight from the API.

### MongoDB

Atlas connects directly to MongoDB using the URI you configure in the environment — the same `mongodb://` connection string IAP itself uses. From there it collects server build info, the runtime config via `getCmdLineOpts`, and replica set status and configuration if you are running a replica set. This works identically whether MongoDB is running on bare metal or as a StatefulSet inside Kubernetes.

### Redis

Same story. Atlas connects via `redis-py` using a `redis://` URI and collects server info, memory stats, and the full config via `CONFIG GET`. The fact that Redis is running as a pod does not change anything here.

> **Where to find your URIs:** If you have access to IAP's running configuration, the MongoDB and Redis URIs are typically in the `ITENTIAL_MONGO_URL` and `ITENTIAL_REDIS_URL` environment variables on the IAP pod, or in the `env` block of your `values.yaml`. You can also check with your platform team — these are the same credentials IAP uses at runtime.

---

## Optional but Recommended: values.yaml

If you have the Helm values file used to deploy IAP, pointing Atlas at it unlocks a meaningful set of additional data that the OAuth API cannot provide — specifically, the declarative resource configuration and Kubernetes-specific deployment settings.

Atlas identifies a file as an IAP values file by checking for an `env` block containing at least one `ITENTIAL_*` key (as of 1.7.2, the file is also validated as a proper YAML mapping before being accepted — you will see a clear error if the path is wrong rather than a confusing failure later during capture).

### What values.yaml adds

**Resource and sizing data** that feeds into infrastructure sizing rules:

| values.yaml field | What Atlas captures |
|---|---|
| `resources.requests.cpu` | CPU core allocation (parsed from Kubernetes quantities like `1000m`) |
| `resources.requests.memory` / `resources.limits.memory` | Memory allocation in bytes |
| `replicaCount` | Number of IAP pod replicas |
| `resources` (full block) | Full requests and limits for rule evaluation |

**QoS class** — derived from the relationship between your requests and limits:

- `Guaranteed` — requests equal limits for all containers
- `Burstable` — at least one container has a lower request than its limit
- `BestEffort` — no requests or limits set at all

This matters because `BestEffort` pods are the first to be evicted under node memory pressure. Atlas flags this as non-compliant.

**Kubernetes deployment metadata:**

| values.yaml field | Captured as |
|---|---|
| `image` | Container image reference |
| `service` | Kubernetes Service spec |
| `ingress.enabled` | Whether an Ingress is configured |
| `useTLS` | TLS configuration |
| `useWebSockets` | WebSocket support |
| `certManager.enabled` | Whether cert-manager is managing TLS |
| `storageClass` | Persistent volume storage class |
| `persistentVolumeClaims` | PVC configuration |
| `livenessProbe.enabled` / `readinessProbe.enabled` | Probe configuration |
| `mountLogVolume` | Whether logs are persisted to a volume (false = logs lost on pod restart) |

**Platform config fallback** — if for any reason the Platform OAuth `/server/config` response does not contain configuration data, Atlas translates the `ITENTIAL_*` keys in your `values.yaml` env block into platform properties:

```
ITENTIAL_MONGO_URL   →  mongo_url
ITENTIAL_REDIS_URL   →  redis_url
ITENTIAL_LOG_LEVEL   →  log_level
```

This is a fallback path. In a healthy deployment where the API is responding normally, the values.yaml env block is not needed for platform config — but it is good to have configured so Atlas has something to fall back on.

The entire values.yaml is also stored verbatim in the capture data under `kubernetes.helm_values.iap` for reference and debugging.

---

## Optional: kubectl

When `use_kubectl` is enabled in your environment, Atlas uses `kubectl` for live cluster data that the APIs cannot provide. Think of it as the Kubernetes equivalent of SSH — used only for things that require direct cluster access, and only when those things are not available from a protocol API.

> **As of 1.7.2**, Atlas verifies the `kubectl` binary during environment configuration. If `kubectl` is not found in your PATH, the setup wizard will ask for the full binary path (with up to three retries) and validate it before continuing. If `kubectl` is unavailable and no `values.yaml` is configured, Atlas will warn you and suggest using a non-Kubernetes environment type instead.

### What kubectl collects

**Pod status and live resource usage** — these run unconditionally when `kubectl` is enabled:

| kubectl command | What Atlas captures |
|---|---|
| `kubectl get pods -o json` | Pod name, phase, restart count, node assignment, readiness — filtered to IAP pods |
| `kubectl top pods --no-headers` | Live CPU and memory consumption per pod |

This is particularly useful for spotting pods in crash-loop restart cycles, identifying which node a pod landed on, or confirming that resource consumption matches your declared requests.

**Platform config fallback via `kubectl exec printenv`** — this is only triggered if the Platform OAuth `/server/config` response does not contain configuration data, meaning the API is unhealthy or unreachable. Because the output of `printenv` contains credentials (MongoDB URI, Redis URI, client secret), Atlas will ask for your explicit confirmation before running this command. You are in control of whether that happens.

All `ITENTIAL_*` environment variables from the pod's live environment are translated the same way as the `values.yaml` env block and stored at `platform.config_file`. This catches runtime overrides that were injected at deploy time and are not reflected in the Helm values file.

If you need to share the capture data with someone, `session export --redact` will strip sensitive values before export.

### What kubectl does NOT do

To be clear about the boundary:

- Does **not** read `package.json` files inside pods — adapter and application versions come from the Platform API (`/health/adapters`, `/health/applications`).
- Does **not** run `node --version` inside pods — the Node.js version comes from `GET /health/server`.
- Does **not** read `release_metadata.json` for the platform version — that also comes from `GET /health/server`.
- Does **not** read `mongod.conf`, `redis.conf`, or `platform.properties` — those come from `pymongo getCmdLineOpts`, `redis-py CONFIG GET`, and the Platform OAuth API respectively.

---

## Combining Sources: What You Get

| What you configure | What Atlas can audit |
|---|---|
| OAuth + MongoDB URI + Redis URI only | Platform API, MongoDB, Redis. System module produces basic stubs — no resource specs, no pod status. No platform config fallback. |
| OAuth + MongoDB URI + Redis URI + values.yaml | Everything above, plus resource sizing, QoS class, and all Kubernetes deployment metadata. Platform config fallback from env block. Raw values stored for debugging. |
| OAuth + MongoDB URI + Redis URI + kubectl | Everything in "OAuth only", plus live pod status and resource usage. Platform config fallback via `kubectl exec printenv` if needed (with your explicit approval). |
| OAuth + MongoDB URI + Redis URI + values.yaml + kubectl | Full picture: values.yaml for static declarative configuration, kubectl for live scheduling state and consumption, OAuth API for runtime truth. |

The most complete audit uses all four. But if you can only configure OAuth + URIs, you will still get coverage on the majority of compliance rules.

---

## Setting Up a Kubernetes Environment

If you have not yet created an environment for your Kubernetes deployment, run:

```bash
platform-atlas config init
```

Or to add an environment to an existing config:

```bash
platform-atlas env add
```

During setup, select **Kubernetes** as your deployment mode. You will be prompted for:

- **Platform OAuth credentials** (client ID and secret) — tested immediately after entry
- **MongoDB URI** (`mongodb://...`) — scheme is validated in the wizard
- **Redis URI** (`redis://...`) — scheme is validated in the wizard
- **values.yaml path** (optional) — the path to your IAP Helm values file on this machine
- **kubectl context** (optional) — the kubeconfig context to use; defaults to current context
- **kubectl namespace** (optional) — the Kubernetes namespace where IAP pods are running

Once configured, run a health check to confirm everything is reachable:

```bash
platform-atlas config doctor
```

This will verify your Platform and Gateway URLs are reachable, your credential backend is working, and your active ruleset is loaded — all in a single pass.

---

## Running the Audit

With your environment configured, the standard session workflow applies:

```bash
# Create a new session
platform-atlas session create <session-name>

# Run capture
platform-atlas session run capture

# Validate against the ruleset
platform-atlas session run validate

# Generate the report
platform-atlas session run report
```

Or run all three in one step:

```bash
platform-atlas session run all
```

During capture, you will see progress for each data source as it completes. If `kubectl exec printenv` is triggered as a fallback, Atlas will pause and ask for your confirmation before proceeding.

---

## Tier Considerations

Kubernetes environments can run in either **Standard** or **Extended** tier:

- **Standard** — Platform OAuth + optional IAG4 API. No MongoDB, no Redis, no kubectl. Covers ~55 rules focused on application-layer settings. If your team only has API-level access and no database credentials, this is your path.
- **Extended** — Full audit including MongoDB, Redis, kubectl, and values.yaml. Covers ~108 rules. This is the right choice for a complete Kubernetes deployment audit.

To check your current tier:

```bash
platform-atlas tier show
```

To switch:

```bash
platform-atlas tier set extended
```

---

## Troubleshooting

**"kubectl binary not found"** — During environment setup, Atlas will prompt for the full path to the `kubectl` binary. If you do not have kubectl installed or configured for this cluster, you can skip it and rely on values.yaml alone.

**"values.yaml is not a valid YAML mapping"** — Atlas validates the file before accepting it. Make sure you are pointing to the actual `values.yaml` file, not a rendered template or a directory.

**"MongoDB URI scheme not valid"** — The URI must start with `mongodb://` or `mongodb+srv://`. Passing an HTTPS URL or a bare hostname will be caught in the wizard.

**"Platform config not in API response"** — If Atlas cannot find configuration data in `GET /server/config`, it will attempt to fall back to values.yaml and then offer to run `kubectl exec printenv`. This is a sign that the Platform API may be partially unhealthy — worth investigating after the audit.

**Redacting credentials from exports** — If you need to share a capture file, run `session export --redact` to strip sensitive values including any URIs or secrets that were captured via `kubectl exec printenv`.
