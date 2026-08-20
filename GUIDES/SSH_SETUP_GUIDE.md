# Platform Atlas—SSH setup guide

> **Extended and SaaS tiers**—SSH connectivity is required when running Platform Atlas in **Extended** mode (full infrastructure audit) and in **SaaS** mode (the single gateway, unless its config is read from a Docker Compose / Helm file). Only **Standard** tier never uses SSH—it audits over Platform OAuth and the Gateway 4 API only and does not connect to any servers via SSH. If you are unsure which tier you are on, run `platform-atlas tier show`. If you are on Standard, you can skip this guide entirely.

This guide walks through creating a dedicated SSH user for Platform Atlas on your Itential Platform deployment servers. By the end, you'll have a single service account with key-based authentication that Atlas can use to connect to every target server.

## Overview

Platform Atlas connects to your servers via SSH to collect configuration data. The recommended setup is:

- **One dedicated user** (e.g. `platformatlas`) created on every target server
- **One SSH key pair** generated on the workstation running Atlas
- **Key-based authentication**—no passwords
- **Optional passwordless sudo** for reading protected config files

This user is read-only. Atlas never writes to, modifies, or restarts anything on your servers.

## What you'll need

- Root or sudo access on each target server to create the user
- A list of your target server hostnames or IPs (IAP, MongoDB, Redis, Gateway—whatever Atlas will audit). The relevant node set depends on tier: Extended covers the full deployment, while SaaS is just the single gateway node.
- The workstation or laptop where Platform Atlas is installed

## Step 1: Create the user on each target server

SSH into each server that Atlas will connect to and run:

```bash
sudo useradd -r -m -s /bin/bash platformatlas
```

This creates a system account (`-r`) with a home directory (`-m`) and a bash shell. Repeat on every server in your deployment—IAP nodes, MongoDB nodes, Redis nodes, and Gateway nodes.

> **Note:** The username can be anything you choose. `platformatlas` is just the recommended convention. Whatever you pick, use the same username on every server so Atlas can use one set of credentials for all nodes.

## Step 2: Generate an SSH key pair

On the **workstation where Atlas is installed** (not on the target servers), generate a dedicated key pair:

```bash
ssh-keygen -t ed25519 -C "platform-atlas" -f ~/.ssh/platform-atlas
```

When prompted for a passphrase, you can either set one (more secure—Atlas will ask for it during setup) or press Enter for no passphrase (more convenient for headless/automated usage).

This creates two files:

- `~/.ssh/platform-atlas`—the private key (stays on your workstation, never shared)
- `~/.ssh/platform-atlas.pub`—the public key (goes on every target server)

## Step 3: Distribute the public key

Copy the public key to each target server. Run this from your workstation for every server:

```bash
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@<hostname-or-ip>
```

For example, in a typical HA2 deployment:

```bash
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@iap-01.acme.com
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@iap-02.acme.com
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@mongo-01.acme.com
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@mongo-02.acme.com
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@mongo-03.acme.com
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@redis-01.acme.com
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@redis-02.acme.com
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@redis-03.acme.com
ssh-copy-id -i ~/.ssh/platform-atlas.pub platformatlas@gw-01.acme.com
```

You'll be prompted for the `platformatlas` user's password during this step. If the user doesn't have a password yet, set a temporary one first:

```bash
# Run this on the target server
sudo passwd platformatlas
```

After the key is distributed, you can disable password authentication for this user (optional but recommended).

## Step 4: Verify key-based login

Test the connection from your workstation:

```bash
ssh -i ~/.ssh/platform-atlas platformatlas@<hostname-or-ip>
```

You should log in without being prompted for a password (or only for the key passphrase if you set one). Test this for every server before configuring Atlas.

## Step 5: Configure passwordless sudo (optional)

Some configuration files (e.g. `/etc/redis/redis.conf`, `/etc/redis/sentinel.conf`) are only readable by root. Atlas can use `sudo` as a fallback to read these files—but only if the user has passwordless sudo configured.

On each server where protected files need to be read, create a sudoers entry:

```bash
sudo visudo -f /etc/sudoers.d/platformatlas
```

Add the following line:

```
platformatlas ALL=(ALL) NOPASSWD: /usr/bin/test, /usr/bin/stat, /usr/bin/realpath, /usr/bin/cat
```

This gives the `platformatlas` user passwordless sudo access to **only** `test`, `stat`, `realpath`, and `cat`—the four commands Atlas uses for file access. No shell access, no writes, no service management.

> **Note:** If you skip this step, Atlas will still work. It will simply skip files it can't read and note them in the capture log. The corresponding validation rules will show as SKIP in the report.

## Step 6: Configure Platform Atlas

Run the Atlas setup wizard:

```bash
platform-atlas config init
```

When prompted for SSH settings, enter:

- **SSH username:** `platformatlas`
- **SSH key path:** `~/.ssh/platform-atlas`
- **SSH key passphrase:** (your passphrase, or leave blank if none)

These credentials are stored in your configured credential backend (OS Keyring, Encrypted Local File, or Vault), never in the config file.

## Quick reference

| Item | Value |
|---|---|
| Username | `platformatlas` (or your choice—same on all servers) |
| Key type | Ed25519 (recommended) or RSA 4096 |
| Private key | `~/.ssh/platform-atlas` on your workstation |
| Public key | `~/.ssh/platform-atlas.pub` → copied to all targets |
| Sudo | Optional, passwordless, limited to `test`, `stat`, `realpath`, `cat` |
| Shell | `/bin/bash` |

---

## Alternative: ControlMaster transport (CyberArk PSMP / jump hosts)

If direct SSH to your IAP server isn't possible—for example, because SSH goes through a CyberArk PSMP gateway that requires MFA (YubiKey OTP, RADIUS, smart card)—you can use **ControlMaster transport** instead.

In this mode, you open one authenticated SSH session manually (satisfying MFA once), and Atlas multiplexes all of its connections through that session with no further prompts.

> **Important:** ControlMaster applies to the **Platform (IAP) node only** (Extended tier). MongoDB and Redis nodes still need direct SSH access—set those up using Steps 1–6 above. Under the SaaS tier there is no Platform node, so the single gateway connects over direct SSH (Steps 1–6), not ControlMaster.

### When to use this

- The IAP node is behind CyberArk PSMP or a similar PAM/jump host
- SSH requires interactive MFA that Atlas cannot automate
- Direct key-based SSH to the IAP server is not allowed by policy

### 1. Open the ControlMaster session

Run this once from your workstation before starting Atlas. This is the step where you authenticate (password + MFA tap):

```bash
ssh -M -S /tmp/atlas-cm.sock \
    -o ControlPersist=10m \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -fN <user>@<target-host>@<psmp-gateway>
```

If your server listens on a non-standard port, add `-p <port>`:

```bash
ssh -M -S /tmp/atlas-cm.sock \
    -p 2222 \
    -o ControlPersist=10m \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -fN <user>@<target-host>@<psmp-gateway>
```

**CyberArk PSMP destination format:** `<username>@<target-ip-or-hostname>@<psmp-gateway-hostname>`

For example:
```bash
ssh -M -S /tmp/atlas-cm.sock \
    -o ControlPersist=10m \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -fN user@target-host@psmp-gateway.example.com
```

What the flags do:

| Flag | Purpose |
|---|---|
| `-M -S /tmp/atlas-cm.sock` | Create a ControlMaster socket at that path |
| `-o ControlPersist=10m` | Hold the socket open for 10 minutes after Atlas completes—enough for a full capture |
| `-fN` | Run in the background (`-f`) with no remote command (`-N`) |

### 2. Verify the socket

Confirm Atlas will be able to use the socket before running a capture:

```bash
ssh -S /tmp/atlas-cm.sock \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    <user>@<target-host>@<psmp-gateway> \
    "echo atlas-ok && whoami && hostname"
```

Expected output:
```
atlas-ok
<your-username>
<server-hostname>
```

### 3. Configure Atlas to use the socket

**CLI setup wizard:**
When creating or editing an environment, select **ControlMaster** when asked how Atlas should connect to the Platform (IAP) server. Enter:
- **Socket path:** the `-S` path from your `ssh -M` command (e.g. `/tmp/atlas-cm.sock`)
- **SSH destination:** the full destination string (e.g. `user@target-host@psmp-gateway.example.com`)

**WebUI Environment form:**
Under **Topology → Platform (IAP) connection type**, choose **ControlMaster—CyberArk PSMP / jump host** and fill in the same two fields.

### 4. Run Atlas

With the master session open, run captures as normal:

```bash
platform-atlas session run capture
```

Atlas connects silently through the socket. If the socket has expired, Atlas prints the exact `ssh -M` command to re-open it.

> **Tip:** Use a longer `ControlPersist` value if your captures are slow (e.g. `-o ControlPersist=30m`). The socket closes automatically when it expires, and you can always re-open it with the same command.

---

## Alternative: Local transport (Atlas installed on the Platform server)

When the Atlas CLI is installed on the IAP server itself—typically because policy forbids
inbound SSH from a workstation—Atlas can collect IAP-side data through the **local
filesystem** instead. The IAP node uses Local transport; MongoDB, Redis, and Gateway nodes
still use SSH.

### When to use this

- The Atlas operator can install the wheel directly on the Platform server, and prefers that to opening up SSH.
- A hardened build of the IAP server forbids inbound SSH but allows shell access for the operator.
- You're auditing a single all-in-one (IAP + MongoDB + Redis) host that Atlas already lives on.

### Setup

1. Install Atlas on the IAP server (`pip install platform_atlas-2.0.0-py3-none-any.whl`).
2. Run `platform-atlas config init` (or `platform-atlas env create`).
3. When the wizard asks how Atlas should connect to the Platform (IAP) server, choose **Local**.
4. Configure SSH for MongoDB, Redis, and Gateway nodes as usual (Steps 1–6 above) if those services are on separate hosts.

The Atlas CLI runs as the operator who started it, so the user must have read access to
`/etc/itential/`, `/opt/itential/`, and any other paths the rules reference. Passwordless
sudo is used as a fallback for files Atlas can't read directly, exactly the same as the SSH
transport.

> **Tip:** Local transport is intentionally not the default. SSH is recommended whenever
> it's possible—running Atlas on a separate workstation keeps the audit tooling completely
> off the system being audited.

---

## Troubleshooting

**"Permission denied (publickey)"**—The public key wasn't copied correctly. Verify the key exists on the target server:

```bash
ssh -i ~/.ssh/platform-atlas platformatlas@<host>
# If this fails:
cat ~/.ssh/platform-atlas.pub  # copy the output
# Then on the target server:
sudo mkdir -p /home/platformatlas/.ssh
sudo sh -c 'echo "<paste-public-key>" >> /home/platformatlas/.ssh/authorized_keys'
sudo chmod 700 /home/platformatlas/.ssh
sudo chmod 600 /home/platformatlas/.ssh/authorized_keys
sudo chown -R platformatlas:platformatlas /home/platformatlas/.ssh
```

**"Host key verification failed"**—First time connecting to this server. Either add the host key manually (`ssh-keyscan <host> >> ~/.ssh/known_hosts`) or connect once interactively and accept the fingerprint.

**Atlas says "Permission denied" for a config file**—The file is root-only and passwordless sudo isn't configured. Follow Step 5 above for that server.

**sudo works interactively but Atlas says "sudo not available"**—The sudo is likely password-protected. Atlas requires `NOPASSWD` sudo. Verify with:

```bash
sudo -n cat /etc/redis/redis.conf
```

If this prompts for a password, the sudoers entry needs `NOPASSWD` added.
