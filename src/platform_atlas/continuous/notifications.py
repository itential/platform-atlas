"""
Outbound notification channels for continuous-audit drift.

Two adapters: Slack incoming-webhooks and a generic JSON webhook with optional
HMAC-SHA256 signing. Channels are persisted per-environment on the env overlay
file under ``notification_channels`` so teams can mute staging without touching
the production routing.

Notification policy: only fire on alert-state *transitions* — newly-created
alerts and re-opened acked alerts. Drift on an already-unacked alert is already
on the dashboard; re-firing every hour would be spam.

Security:
    Payloads are *redacted by design* — they include rule_number, rule_name,
    severity, and alert_id only, never the captured ``previous``/``current``
    values or the rule ``path``. Audit data routinely contains MongoDB URIs,
    OAuth secrets, internal hostnames, etc., and a misconfigured Slack channel
    or webhook would otherwise exfiltrate them. Operators see "rule R-042
    drifted in prod, severity high — open Atlas WebUI for details" and have to
    log into the WebUI to inspect actual values.

    Webhook URLs are validated against an SSRF blocklist before persistence:
    private/loopback/link-local/cloud-metadata addresses are rejected unless
    ATLAS_ALLOW_PRIVATE_WEBHOOKS=1 is set.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import urlparse

import requests

from platform_atlas.core._version import __version__
from platform_atlas.continuous import runtime, storage

logger = logging.getLogger(__name__)


CHANNEL_TYPE_SLACK = "slack"
CHANNEL_TYPE_WEBHOOK = "webhook"
SUPPORTED_TYPES = (CHANNEL_TYPE_SLACK, CHANNEL_TYPE_WEBHOOK)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RETRIES = 1  # one retry on connection error; non-2xx is a hard fail

# Cap how many events ride along in a single payload. Slack rate-limits hard
# beyond ~10 attachments anyway, and a multi-hundred-event burst on a webhook
# is an attack vector against the receiver. Excess events are folded into a
# single overflow line that points at the dashboard.
MAX_EVENTS_PER_PAYLOAD = 25


@dataclass
class NotificationChannel:
    """One outbound channel for a single environment."""
    id: str
    type: str           # "slack" | "webhook"
    name: str           # human label
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    secret: str = ""    # optional HMAC-SHA256 signing secret (webhook only)
    enabled: bool = True
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NotificationChannel":
        if not isinstance(data, dict):
            raise ValueError(f"Channel entry must be a JSON object, got {type(data).__name__}")
        type_ = str(data.get("type", "")).lower()
        if type_ not in SUPPORTED_TYPES:
            raise ValueError(f"Unsupported channel type: {type_!r}")
        return cls(
            id=str(data["id"]),
            type=type_,
            name=str(data.get("name") or data.get("id")),
            url=str(data["url"]),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            secret=str(data.get("secret") or ""),
            enabled=bool(data.get("enabled", True)),
            created_at=str(data.get("created_at") or ""),
        )


# ── URL validation (SSRF guard) ───────────────────────────────────────

# Substrings that look like URLs / IPs in error messages — used by
# ``_scrub_error`` to keep destination details out of logs.
_URL_LIKE_RE = re.compile(r"https?://[^\s\"<>]+", re.IGNORECASE)
_IP_LIKE_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")


def _is_private_ip(addr: str) -> bool:
    """True for loopback, RFC1918, link-local, ULA, cloud-metadata addresses."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        # AWS / GCP / Azure metadata services
        or str(ip) in ("169.254.169.254", "169.254.170.2", "fd00:ec2::254")
    )


def _validate_url_for_ssrf(url: str) -> None:
    """Reject URLs that point at private/loopback/link-local addresses.

    Skips the check when ATLAS_ALLOW_PRIVATE_WEBHOOKS=1 — for genuinely
    internal Slack-equivalent setups behind a firewall.

    Raises ``ValueError`` on rejection. Resolves the host to all A/AAAA
    answers and rejects if *any* answer is private; this defends against
    naïve DNS rebinding (a domain that happens to resolve to private).
    """
    if os.environ.get("ATLAS_ALLOW_PRIVATE_WEBHOOKS", "").strip() == "1":
        return

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got: {parsed.scheme!r}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("URL is missing a host component")

    # Reject literal IP forms first (faster, also catches IPv6 brackets).
    try:
        if _is_private_ip(host):
            raise ValueError(
                f"Refusing webhook to private/loopback address {host}. "
                "Set ATLAS_ALLOW_PRIVATE_WEBHOOKS=1 if this is intentional."
            )
    except ValueError:
        # _is_private_ip returns False for non-IP literals — fall through to DNS.
        pass

    # Skip DNS resolution when outbound connections are blocked — the send will
    # be suppressed by _send_channel, so the SSRF check provides no real value.
    try:
        from platform_atlas.core.config import is_network_restricted
        if is_network_restricted():
            logger.debug(
                "DNS SSRF check skipped due to network policy (host=%r)", host
            )
            return
    except Exception:
        pass

    # DNS resolve. Reject if any answer is private.
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve webhook host {host!r}: {exc}") from exc
    for info in infos:
        addr = info[4][0]
        # Strip IPv6 zone identifiers like fe80::1%eth0 before parsing.
        addr_clean = addr.split("%", 1)[0]
        if _is_private_ip(addr_clean):
            raise ValueError(
                f"Refusing webhook to {host} — resolves to private address {addr_clean}. "
                "Set ATLAS_ALLOW_PRIVATE_WEBHOOKS=1 if this is intentional."
            )


# ── Channel storage: metadata in env JSON, secrets in OS keyring ──────
#
# What's where:
#   env JSON  → id, type, name, enabled, created_at        (safe to share)
#   secret store → url, secret, headers                    (sensitive)
#
# The store service is scoped per-env to ``platform-atlas/<env>`` so each
# environment's notification secrets are fully isolated — the same scope the
# CredentialStore uses for Platform OAuth secrets, Mongo URIs, etc.
# Notifications go through the low-level secret store (active_secret_store)
# rather than the high-level CredentialStore facade because channels are
# dynamic (variable count, arbitrary IDs) and don't fit the fixed
# CredentialKey enum — but they share the same substrate, so they get the
# same encrypted local-file fallback when the OS keyring is unavailable.
#
# This holds even when ``credential_backend`` is Vault (Vault is read-only
# from Atlas, and the WebUI / CLI need to write channels at runtime): secrets
# go to the OS keyring, or to the encrypted local file when the keyring can't
# be used. Env JSON only ever holds non-sensitive metadata; secrets land there
# only as a last resort if the store write itself fails (rare — the file
# fallback almost always succeeds), with a one-time warning.

_METADATA_FIELDS: tuple[str, ...] = ("id", "type", "name", "enabled", "created_at")
_KEYRING_KEY_PREFIX = "notification_secret_"

# De-dupe per-session keyring-unavailable warnings so logs aren't spammed
# when a multi-channel env is rendered repeatedly.
_KEYRING_WARNED_KEYS: set[tuple[str, str]] = set()


def _scoped_service(env: str) -> str:
    """Match the platform-atlas/<env> scoping used by the credential store."""
    from platform_atlas.core.credentials import scoped_service_name
    return scoped_service_name(env)


def _channel_keyring_key(channel_id: str) -> str:
    return f"{_KEYRING_KEY_PREFIX}{channel_id}"


def _save_channel_secrets(env: str, channel_id: str, payload: dict[str, Any]) -> bool:
    """Persist URL/secret/headers to the active secret store (OS keyring, or the
    encrypted local-file fallback) under the env-scoped service.

    Returns True on success, False on any store write failure. Empty payloads
    (no url, secret, or headers) are treated as success so callers that don't
    have anything sensitive to store don't get false negatives.
    """
    if not env or not channel_id:
        return False
    has_anything = bool(
        payload.get("url") or payload.get("secret") or payload.get("headers")
    )
    if not has_anything:
        return True
    try:
        from platform_atlas.core.credentials import active_secret_store
        active_secret_store().set(
            _scoped_service(env),
            _channel_keyring_key(channel_id),
            json.dumps(payload, ensure_ascii=False),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — keyring error, file write error, etc.
        logger.debug("Secret-store write failed for channel=%s/%s: %s", env, channel_id, exc)
        return False


def _load_channel_secrets(env: str, channel_id: str) -> dict[str, Any]:
    """Read URL/secret/headers from the active secret store. Empty dict on any failure."""
    if not env or not channel_id:
        return {}
    try:
        from platform_atlas.core.credentials import active_secret_store
        raw = active_secret_store().get(_scoped_service(env), _channel_keyring_key(channel_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load channel secrets for %s/%s: %s", env, channel_id, exc)
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Corrupt keyring blob for channel %s/%s: %s", env, channel_id, exc)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _delete_channel_secrets(env: str, channel_id: str) -> None:
    """Best-effort delete of the secret-store entry; never raises."""
    if not env or not channel_id:
        return
    try:
        from platform_atlas.core.credentials import active_secret_store
        active_secret_store().delete(_scoped_service(env), _channel_keyring_key(channel_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not delete channel secrets for %s/%s: %s", env, channel_id, exc)


def _warn_secret_store_unavailable(env: str, channel_id: str) -> None:
    key = (env, channel_id)
    if key in _KEYRING_WARNED_KEYS:
        return
    _KEYRING_WARNED_KEYS.add(key)
    logger.warning(
        "Could not store notification secrets for channel %s/%s in the credential store "
        "(OS keyring or encrypted local file) — they remain in the environment JSON for now.",
        env, channel_id,
    )


def _metadata_only(channel_dict: dict[str, Any]) -> dict[str, Any]:
    """Return ``channel_dict`` with secret-bearing fields stripped."""
    return {k: channel_dict[k] for k in _METADATA_FIELDS if k in channel_dict}


def _read_channels_raw(environment: str) -> list[dict[str, Any]]:
    raw = runtime._read_raw(environment)  # pylint: disable=protected-access
    if not isinstance(raw, dict):
        return []
    section = raw.get("notification_channels")
    if not isinstance(section, list):
        return []
    return section


def _write_channels_raw(environment: str, channels: list[dict[str, Any]]) -> None:
    raw = runtime._read_raw(environment)  # pylint: disable=protected-access
    if not isinstance(raw, dict):
        raise ValueError(f"Environment file for '{environment}' is not a JSON object")
    raw["notification_channels"] = channels
    runtime._write_raw(environment, raw)  # pylint: disable=protected-access


def list_channels(environment: str) -> list[NotificationChannel]:
    """Return all channels for ``environment``, hydrating secrets from keyring.

    On the first read after upgrading from the legacy plaintext-JSON layout,
    any channel that still carries ``url``/``secret``/``headers`` in env JSON
    is migrated transparently:
      1. Try to write the secret blob into the OS keyring.
      2. If that succeeded, rewrite the env JSON with metadata only.
      3. If the keyring is unusable, fall back to the legacy values for
         this read and log a one-time warning per channel.

    The migration is opportunistic — it never raises, and partial migration
    (some channels keyring, some legacy) is fine.
    """
    raw = _read_channels_raw(environment)
    if not raw:
        return []

    out: list[NotificationChannel] = []
    cleaned_raw: list[dict[str, Any]] = []
    migrated_any = False

    for entry in raw:
        if not isinstance(entry, dict):
            cleaned_raw.append(entry)
            continue

        channel_id = str(entry.get("id") or "")
        secrets_payload = _load_channel_secrets(environment, channel_id) if channel_id else {}

        # Detect legacy: secrets that still live in env JSON.
        legacy_url = str(entry.get("url") or "")
        legacy_secret = str(entry.get("secret") or "")
        legacy_headers_raw = entry.get("headers") or {}
        legacy_headers = (
            {str(k): str(v) for k, v in legacy_headers_raw.items()}
            if isinstance(legacy_headers_raw, dict) else {}
        )
        has_legacy = bool(legacy_url or legacy_secret or legacy_headers)

        cleaned_entry = _metadata_only(entry)

        if not secrets_payload and has_legacy and channel_id:
            payload = {
                "url": legacy_url,
                "secret": legacy_secret,
                "headers": legacy_headers,
            }
            if _save_channel_secrets(environment, channel_id, payload):
                secrets_payload = payload
                migrated_any = True
                logger.info(
                    "Migrated notification channel %s/%s secrets: env JSON → credential store",
                    environment, channel_id,
                )
            else:
                # Store write failed — keep legacy values inline so the channel
                # still works this session. We re-attempt migration on the next
                # list_channels call.
                _warn_secret_store_unavailable(environment, channel_id)
                secrets_payload = payload
                cleaned_entry = dict(entry)  # preserve secrets in JSON until the store works

        merged = {
            **cleaned_entry,
            "url": str(secrets_payload.get("url") or legacy_url),
            "secret": str(secrets_payload.get("secret") or legacy_secret),
            "headers": secrets_payload.get("headers") or legacy_headers,
        }
        try:
            out.append(NotificationChannel.from_dict(merged))
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed notification channel: %s", exc)

        cleaned_raw.append(cleaned_entry)

    # Persist the cleaned env JSON only if at least one channel was migrated
    # AND its cleaned form drops secret-bearing fields. Otherwise we'd
    # uselessly rewrite the file every read.
    if migrated_any:
        try:
            _write_channels_raw(environment, cleaned_raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not rewrite env JSON after migration: %s", exc)

    return out


def get_channel(environment: str, channel_id: str) -> NotificationChannel | None:
    for ch in list_channels(environment):
        if ch.id == channel_id:
            return ch
    return None


def add_channel(environment: str, channel: NotificationChannel) -> None:
    """Append (or replace by id) ``channel`` — secrets to the credential store, metadata to env JSON.

    Validates the URL against the SSRF blocklist before any persistence so
    the CLI and WebUI share one validator. Pass ATLAS_ALLOW_PRIVATE_WEBHOOKS=1
    to bypass the blocklist for legitimate internal targets.

    Secret persistence:
      • Secrets go to the active secret store — the OS keyring, or the encrypted
        local-file fallback (~/.atlas/credentials.enc) when the keyring can't be
        used. On success, env JSON holds only metadata (id, type, name, enabled,
        created_at).
      • If the store write fails (a genuine error — the file fallback is almost
        always writable), raise rather than silently downgrading secrets into
        plaintext env JSON.
    """
    if not environment:
        raise ValueError("Cannot manage notification channels without an environment")
    if channel.type not in SUPPORTED_TYPES:
        raise ValueError(f"Unsupported channel type: {channel.type!r}")
    if not channel.url:
        raise ValueError("Channel URL is required")
    # Centralized SSRF + scheme check — both the CLI and WebUI route hit this.
    _validate_url_for_ssrf(channel.url)
    if not channel.created_at:
        channel.created_at = storage.now_iso()

    secrets_payload = {
        "url": channel.url,
        "secret": channel.secret,
        "headers": dict(channel.headers or {}),
    }
    if not _save_channel_secrets(environment, channel.id, secrets_payload):
        # The active secret store (OS keyring, or the encrypted local-file
        # fallback) could not store the secret — a genuine write error, not a
        # "no secure store on this host" case (the file fallback always is one).
        # Raise rather than silently downgrade secrets into plaintext env JSON.
        raise ValueError(
            f"Could not save channel '{channel.id}' secrets to the credential store. "
            "Verify the OS keyring is reachable, or that ~/.atlas is writable for the "
            "encrypted local file store."
        )

    metadata = _metadata_only(channel.to_dict())

    existing = _read_channels_raw(environment)
    replaced = False
    for i, entry in enumerate(existing):
        if isinstance(entry, dict) and str(entry.get("id")) == channel.id:
            existing[i] = metadata
            replaced = True
            break
    if not replaced:
        existing.append(metadata)
    _write_channels_raw(environment, existing)

    logger.info(
        "Notification channel %s (env=%s, type=%s)",
        "updated" if replaced else "added",
        environment, channel.type,
    )


def remove_channel(environment: str, channel_id: str) -> bool:
    """Remove a channel by id. Cleans up both env JSON and keyring secrets."""
    existing = _read_channels_raw(environment)
    before = len(existing)
    kept = [e for e in existing if not (isinstance(e, dict) and str(e.get("id")) == channel_id)]
    if len(kept) == before:
        return False
    _write_channels_raw(environment, kept)
    _delete_channel_secrets(environment, channel_id)
    logger.info("Notification channel removed (env=%s, id=%s)", environment, channel_id)
    return True


def make_channel_id(prefix: str = "ch") -> str:
    """Short, URL-safe channel id."""
    return f"{prefix}-{secrets.token_hex(4)}"


# ── Outbound dispatch ─────────────────────────────────────────────────

_SEVERITY_COLORS: dict[str, str] = {
    "critical": "#DC2626",
    "high":     "#EA580C",
    "warning":  "#F59E0B",
    "medium":   "#F59E0B",
    "info":     "#3B82F6",
    "low":      "#3B82F6",
}


def _severity_color(severity: str) -> str:
    return _SEVERITY_COLORS.get((severity or "").lower(), "#6B7280")


def _truncate(text: str, limit: int = 240) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _scrub_error(text: str) -> str:
    """Strip URLs and bare IPs from receiver-reported error text.

    Slack incoming-webhook URLs are bearer credentials — when a Slack receiver
    returns "no_service: invalid url https://hooks.slack.com/services/T…/B…/…"
    that response body would otherwise land in our logs and the WebUI flash
    message. Same for self-hosted webhooks where the URL might leak internal
    hostnames.
    """
    if not text:
        return text
    cleaned = _URL_LIKE_RE.sub("[url-redacted]", text)
    cleaned = _IP_LIKE_RE.sub("[ip-redacted]", cleaned)
    return _truncate(cleaned, 200)


def _alert_id_from_event(event: dict[str, Any]) -> str:
    """Re-derive the alert_id from a drift event so receivers can deep-link.

    Mirrors ``alerts._alert_id`` exactly — kept as a duplicate to avoid an
    import cycle (alerts → storage → ... → notifications).
    """
    rule = str(event.get("rule_number", ""))
    path = str(event.get("path", ""))
    raw = f"{rule}|{path}".encode("utf-8")
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]


def _redacted_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a notification-safe view of a drift event.

    Includes only identifying metadata — never the captured value, the rule
    path (which may itself reveal sensitive structure), or run IDs (which can
    be used to enumerate run reports). Receivers see "rule X, severity Y" and
    have to log into the WebUI for the actual diff.
    """
    return {
        "rule_number": str(event.get("rule_number", "")),
        "rule_name": str(event.get("rule_name", "")),
        "severity": str(event.get("severity", "")),
        "alert_id": _alert_id_from_event(event),
    }


def _build_payload(environment: str, drift_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Generic webhook payload (also the basis for the test payload).

    Payload is intentionally minimal — see module docstring. Receivers get
    enough to route/page/correlate but nothing that could leak captured
    secrets if the webhook URL is misconfigured.
    """
    shown = drift_events[:MAX_EVENTS_PER_PAYLOAD]
    overflow = max(0, len(drift_events) - len(shown))
    return {
        "type": "drift_detected",
        "atlas_version": __version__,
        "environment": environment or "_default",
        "detected_at": storage.now_iso(),
        "drift_count": len(drift_events),
        "events": [_redacted_event(e) for e in shown],
        "overflow_count": overflow,
        "details_hint": (
            "Open the Atlas WebUI → Alerts to inspect the drifted values. "
            "Notification payloads are intentionally redacted to prevent "
            "secret exfiltration if a channel is misconfigured."
        ),
    }


def _build_slack_payload(environment: str, drift_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Slack incoming-webhook attachments format — works without app scopes.

    Same redaction policy as the generic webhook: rule_number, rule_name,
    severity only. No path, no previous/current values.
    """
    env_label = environment or "_default"
    summary = (
        f"*{len(drift_events)} drift event{'s' if len(drift_events) != 1 else ''}* "
        f"in `{env_label}` — open the Atlas WebUI for details."
    )
    attachments: list[dict[str, Any]] = [
        {"color": "#101625", "text": summary, "mrkdwn_in": ["text"]}
    ]
    shown = drift_events[:10]  # Slack soft-caps attachments — fewer than the JSON cap.
    for event in shown:
        rule = str(event.get("rule_number", "")) or "?"
        name = str(event.get("rule_name", "")) or "(no name)"
        sev = str(event.get("severity", "")) or "—"
        attachments.append({
            "color": _severity_color(sev),
            "fallback": f"{rule} {name} drifted (severity={sev})",
            "title": f"{rule} · {name}",
            "fields": [
                {"title": "Severity", "value": sev, "short": True},
                {"title": "Alert ID", "value": _alert_id_from_event(event), "short": True},
            ],
            "mrkdwn_in": ["fields"],
        })
    overflow = len(drift_events) - len(shown)
    if overflow > 0:
        attachments.append({
            "color": "#6B7280",
            "fallback": f"+{overflow} more drift events",
            "text": f"+{overflow} more drift event{'s' if overflow != 1 else ''} not shown — see WebUI.",
        })
    return {
        "text": f"Platform Atlas: drift detected in {env_label}",
        "attachments": attachments,
    }


def _sign_body(secret: str, body: bytes) -> str:
    """Compute the X-Atlas-Signature header value (sha256=<hex>)."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post_with_retry(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> tuple[bool, int, str]:
    """POST with one retry on connection error and Retry-After honoring on 429.

    Behavior:
      - 2xx        → success
      - 429        → respect Retry-After (capped at 30s) and retry once within
                     the retry budget; otherwise give up so the engine doesn't
                     block on a slow receiver.
      - 4xx / 5xx  → hard fail (no retry — broken endpoints don't get
                     hammered).
      - connection → retry up to ``retries`` times with 0.5s spacing.
    """
    last_err = ""
    last_status = 0
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            resp = requests.post(url, data=body, headers=headers, timeout=timeout)
            last_status = resp.status_code
            if 200 <= resp.status_code < 300:
                return True, resp.status_code, ""
            if resp.status_code == 429 and attempt < attempts - 1:
                # Honor Retry-After if the receiver provided one — bounded so a
                # malicious or pathological receiver can't stall us forever.
                retry_after = resp.headers.get("Retry-After", "").strip()
                wait = 1.0
                try:
                    wait = max(0.5, min(30.0, float(retry_after))) if retry_after else 1.0
                except ValueError:
                    wait = 1.0
                time.sleep(wait)
                continue
            return False, resp.status_code, _scrub_error(resp.text or resp.reason or "non-2xx")
        except requests.exceptions.RequestException as exc:
            last_err = str(exc)
            if attempt < attempts - 1:
                time.sleep(0.5)
                continue
    return False, last_status, _scrub_error(last_err or "connection error")


def _send_channel(
    channel: NotificationChannel,
    *,
    environment: str,
    drift_events: list[dict[str, Any]],
    is_test: bool,
) -> dict[str, Any]:
    """Send one payload to one channel and return a delivery record."""
    started = time.time()
    record: dict[str, Any] = {
        "channel_id": channel.id,
        "channel_type": channel.type,
        "channel_name": channel.name,
        "environment": environment,
        "started_at": storage.now_iso(),
        "is_test": is_test,
        "ok": False,
        "status_code": 0,
        "duration_ms": 0,
        "error": "",
    }

    try:
        from platform_atlas.core.config import is_network_restricted
        if is_network_restricted():
            logger.debug(
                "Outbound connection to %s blocked due to network policy"
                " (channel=%s, env=%s)",
                channel.url or "?", channel.id, environment,
            )
            record["error"] = "blocked:network_policy=disallow"
            record["duration_ms"] = int((time.time() - started) * 1000)
            return record
    except Exception:
        pass

    if channel.type == CHANNEL_TYPE_SLACK:
        if is_test:
            payload: dict[str, Any] = {
                "text": (
                    f"Platform Atlas test notification · "
                    f"channel `{channel.name}` · env `{environment or '_default'}`"
                ),
            }
        else:
            payload = _build_slack_payload(environment, drift_events)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"platform-atlas/{__version__}",
        }
    elif channel.type == CHANNEL_TYPE_WEBHOOK:
        payload = _build_payload(environment, drift_events)
        if is_test:
            payload["type"] = "test"
            payload["events"] = []
            payload["drift_count"] = 0
            payload["overflow_count"] = 0
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"platform-atlas/{__version__}",
            "X-Atlas-Source": f"platform-atlas/{__version__}",
            "X-Atlas-Timestamp": payload["detected_at"],
            "X-Atlas-Environment": environment or "_default",
        }
        if channel.secret:
            headers["X-Atlas-Signature"] = _sign_body(channel.secret, body)
        # Custom headers come last but cannot overwrite Atlas-controlled headers.
        for k, v in channel.headers.items():
            if k.lower().startswith("x-atlas-") or k.lower() in {"content-type"}:
                continue
            headers[k] = v
    else:
        record["error"] = f"Unsupported channel type: {channel.type!r}"
        record["duration_ms"] = int((time.time() - started) * 1000)
        return record

    ok, status_code, err = _post_with_retry(channel.url, body, headers)
    record["ok"] = ok
    record["status_code"] = status_code
    record["error"] = err
    record["duration_ms"] = int((time.time() - started) * 1000)
    return record


def fire_drift_alerts(environment: str, drift_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send drift events to every enabled channel for ``environment``.

    Returns one delivery record per channel attempted. Never raises — failures
    are logged and reflected in the returned records so callers (the engine)
    can keep running.
    """
    if not drift_events:
        return []
    channels = [c for c in list_channels(environment) if c.enabled]
    if not channels:
        return []
    records: list[dict[str, Any]] = []
    for channel in channels:
        try:
            record = _send_channel(channel, environment=environment, drift_events=drift_events, is_test=False)
        except Exception as exc:  # noqa: BLE001 — adapter must never fail the run
            logger.exception("Notification adapter crashed for channel=%s", channel.id)
            record = {
                "channel_id": channel.id,
                "channel_type": channel.type,
                "channel_name": channel.name,
                "environment": environment,
                "started_at": storage.now_iso(),
                "is_test": False,
                "ok": False,
                "status_code": 0,
                "duration_ms": 0,
                "error": _scrub_error(f"{type(exc).__name__}: {exc}"),
            }
        records.append(record)
        if record["ok"]:
            logger.info("Notification delivered to %s (channel=%s, env=%s, status=%d)",
                        channel.type, channel.id, environment, record["status_code"])
        else:
            logger.warning("Notification failed for channel=%s (env=%s): %s",
                           channel.id, environment, record["error"])
    return records


def test_channel(environment: str, channel_id: str) -> dict[str, Any]:
    """Send a synthetic test payload to a single channel."""
    channel = get_channel(environment, channel_id)
    if channel is None:
        return {
            "channel_id": channel_id,
            "channel_type": "",
            "channel_name": "",
            "environment": environment,
            "ok": False,
            "status_code": 0,
            "duration_ms": 0,
            "error": "channel not found",
            "is_test": True,
        }
    try:
        return _send_channel(channel, environment=environment, drift_events=[], is_test=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Test send crashed for channel=%s", channel_id)
        return {
            "channel_id": channel.id,
            "channel_type": channel.type,
            "channel_name": channel.name,
            "environment": environment,
            "ok": False,
            "status_code": 0,
            "duration_ms": 0,
            "error": _scrub_error(f"{type(exc).__name__}: {exc}"),
            "is_test": True,
        }
