# Platform Atlas — SSH Setup Guide

This guide walks through creating a dedicated SSH user for Platform Atlas on your IAP deployment servers. By the end, you'll have a single service account with key-based authentication that Atlas can use to connect to every target server.

## Overview

Platform Atlas connects to your servers via SSH to collect configuration data. The recommended setup is:

- **One dedicated user** (e.g. `platformatlas`) created on every target server
- **One SSH key pair** generated on the workstation running Atlas
- **Key-based authentication** — no passwords
- **Optional passwordless sudo** for reading protected config files

This user is read-only. Atlas never writes to, modifies, or restarts anything on your servers.

## What You'll Need

- Root or sudo access on each target server to create the user
- A list of your target server hostnames or IPs (IAP, MongoDB, Redis, Gateway — whatever Atlas will audit)
- The workstation or laptop where Platform Atlas is installed

## Step 1: Create the User on Each Target Server

SSH into each server that Atlas will connect to and run:

```bash
sudo useradd -r -m -s /bin/bash platformatlas
```

This creates a system account (`-r`) with a home directory (`-m`) and a bash shell. Repeat on every server in your deployment — IAP nodes, MongoDB nodes, Redis nodes, and Gateway nodes.

> **Note:** The username can be anything you choose. `platformatlas` is just the recommended convention. Whatever you pick, use the same username on every server so Atlas can use one set of credentials for all nodes.

## Step 2: Generate an SSH Key Pair

On the **workstation where Atlas is installed** (not on the target servers), generate a dedicated key pair:

```bash
ssh-keygen -t ed25519 -C "platform-atlas" -f ~/.ssh/platform-atlas
```

When prompted for a passphrase, you can either set one (more secure — Atlas will ask for it during setup) or press Enter for no passphrase (more convenient for headless/automated usage).

This creates two files:

- `~/.ssh/platform-atlas` — the private key (stays on your workstation, never shared)
- `~/.ssh/platform-atlas.pub` — the public key (goes on every target server)

## Step 3: Distribute the Public Key

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

## Step 4: Verify Key-Based Login

Test the connection from your workstation:

```bash
ssh -i ~/.ssh/platform-atlas platformatlas@<hostname-or-ip>
```

You should log in without being prompted for a password (or only for the key passphrase if you set one). Test this for every server before configuring Atlas.

## Step 5: Configure Passwordless Sudo (Optional)

Some configuration files (e.g. `/etc/redis/redis.conf`, `/etc/redis/sentinel.conf`) are only readable by root. Atlas can use `sudo` as a fallback to read these files — but only if the user has passwordless sudo configured.

On each server where protected files need to be read, create a sudoers entry:

```bash
sudo visudo -f /etc/sudoers.d/platformatlas
```

Add the following line:

```
platformatlas ALL=(ALL) NOPASSWD: /usr/bin/test, /usr/bin/stat, /usr/bin/realpath, /usr/bin/cat
```

This gives the `platformatlas` user passwordless sudo access to **only** `test`, `stat`, `realpath`, and `cat` — the four commands Atlas uses for file access. No shell access, no writes, no service management.

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

These credentials are stored securely in your OS keyring, not in the config file.

## Quick Reference

| Item | Value |
|---|---|
| Username | `platformatlas` (or your choice — same on all servers) |
| Key type | Ed25519 (recommended) or RSA 4096 |
| Private key | `~/.ssh/platform-atlas` on your workstation |
| Public key | `~/.ssh/platform-atlas.pub` → copied to all targets |
| Sudo | Optional, passwordless, limited to `test`, `stat`, `realpath`, `cat` |
| Shell | `/bin/bash` |

## Troubleshooting

**"Permission denied (publickey)"** — The public key wasn't copied correctly. Verify the key exists on the target server:

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

**"Host key verification failed"** — First time connecting to this server. Either add the host key manually (`ssh-keyscan <host> >> ~/.ssh/known_hosts`) or connect once interactively and accept the fingerprint.

**Atlas says "Permission denied" for a config file** — The file is root-only and passwordless sudo isn't configured. Follow Step 5 above for that server.

**sudo works interactively but Atlas says "sudo not available"** — The sudo is likely password-protected. Atlas requires `NOPASSWD` sudo. Verify with:

```bash
sudo -n cat /etc/redis/redis.conf
```

If this prompts for a password, the sudoers entry needs `NOPASSWD` added.
