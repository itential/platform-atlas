"""
Redis Collector - Lightweight, read-only data collection from Redis

This module provides a small Redis client wrapper optimized for metrics gathering.
Supports both standard Redis instances and Sentinel instances, auto-detecting
the mode after connection.

Example:
    >>> collector = RedisCollector.from_config()
    >>> with collector:
    ...     data = collector.collect()
    ...     print(data["mode"])  # "redis" or "sentinel"
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

import redis
from platform_atlas.capture.collectors.base import BaseCollector
from platform_atlas.core.exceptions import (
    CollectorConnectionError,
    RedisCollectorError,
    RedisConnectionNotEstablishedError,
)
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError

from platform_atlas.core.context import ContextNotInitializedError, ctx, require_extended
from platform_atlas.core.preflight import CheckResult
from platform_atlas.core.transport import ProtocolTunnelHandle, open_protocol_tunnel
from platform_atlas.core.uri_credentials import encode_uri_credentials

__all__ = [
    "RedisCollector", "RedisCollectorError", "RedisSettings", "RedisMode",
    "encode_redis_uri", "apply_redis_tls",
]

logger = logging.getLogger(__name__)


def encode_redis_uri(uri: str) -> str:
    """Properly URL-encode credentials in a Redis connection URI.

    Redis URIs are usually password-only (``redis://:pass@host:6379``, no
    username), which :func:`~platform_atlas.core.uri_credentials.encode_uri_credentials`
    handles alongside the ``user:pass`` form. Validates the scheme with a
    plain string check rather than ``urlparse`` — ``urlparse`` mis-splits
    the host/port whenever the still-unencoded password contains a raw ``#``
    (read as a fragment marker) or other reserved character.
    """
    if not uri:
        raise RedisCollectorError("Redis URI cannot be empty")

    scheme, sep, _ = uri.partition("://")
    if not sep or scheme not in ("redis", "rediss"):
        raise RedisCollectorError(
            f"Invalid scheme '{scheme}'. Expected 'redis' or 'rediss'"
        )

    return encode_uri_credentials(uri)


def apply_redis_tls(uri: str, enabled: bool) -> str:
    """Upgrade a ``redis://`` URI to ``rediss://`` when basic-level TLS is enabled.

    Basic-level means encryption-in-transit only — ``ssl_cert_reqs=none`` is
    appended (which redis-py also treats as disabling hostname checking, see
    ``SSLConnection.__init__``) since the audited deployments in the field
    overwhelmingly use self-signed certificates and this toggle intentionally
    has no CA-file/mTLS support to trust them properly. A no-op if the URI
    already uses ``rediss://`` or specifies an ``ssl_*`` query parameter —
    an explicit user choice always wins over the environment-level toggle.
    """
    if not enabled or uri is None:
        return uri
    if uri.startswith("rediss://"):
        return uri
    if not uri.startswith("redis://"):
        return uri
    rewritten = "rediss://" + uri[len("redis://"):]
    query = rewritten.partition("?")[2]
    if re.search(r"(?:^|&)ssl_\w+=", query, re.IGNORECASE):
        return rewritten
    separator = "&" if "?" in rewritten else "?"
    return f"{rewritten}{separator}ssl_cert_reqs=none"

# =================================================
# Constants
# =================================================

class RedisMode(StrEnum):
    """Detected Redis server mode"""
    REDIS = "redis"
    SENTINEL = "sentinel"

# CONFIG GET keys that mirror redis.conf settings validated by rules.
# Used as a protocol-based fallback when SSH config file collection
# is unavailable (alt_path on RDS-004 through RDS-013).
_RUNTIME_CONFIG_KEYS: tuple[str, ...] = (
    "bind",
    "tcp-keepalive",
    "repl-ping-replica-period",
    "repl-timeout",
    "repl-backlog-size",
    "min-replicas-max-lag",
    "no-appendfsync-on-rewrite",
    "client-output-buffer-limit",
)


def _parse_acl_list(acl_lines: list[str]) -> list[list[str]]:
    """Parse ``ACL LIST`` output into per-user token lists.

    Each line is formatted as ``"user <name> <on|off> ...directives"``.
    Strips the leading ``user`` keyword so the shape matches the SSH-parsed
    redis.conf ``user`` directive Atlas already normalizes to:
    ``[name, on|off, ...tokens]`` — the "list of lists" shape
    ``check_redis_acl`` / ``_parse_acl_entries`` expects.
    """
    entries: list[list[str]] = []
    for line in acl_lines:
        tokens = line.split()
        if len(tokens) >= 2 and tokens[0] == "user":
            entries.append(tokens[1:])
    return entries


def _parse_buffer_limit(raw: str) -> dict[str, list]:
    """Parse CONFIG GET client-output-buffer-limit into a nested dict.

    CONFIG GET returns a flat string like:
        "normal 0 0 0 replica 256mb 64mb 60 pubsub 32mb 8mb 60"

    Each class has exactly 4 tokens: class_name hard soft seconds.
    Returns: {"normal": ["0", "0", "0"], "replica": ["256mb", "64mb", "60"], ...}
    """
    tokens = raw.split()
    result: dict[str, list] = {}
    # Each buffer class is a group of 4 tokens: name, hard, soft, seconds
    for i in range(0, len(tokens) - 3, 4):
        class_name = tokens[i]
        result[class_name] = tokens[i + 1 : i + 4]
    return result

# =================================================
# Configuration
# =================================================

@dataclass(frozen=True, slots=True)
class RedisSettings:
    """Immutable Redis connection settings"""
    socket_connect_timeout: int = 5
    socket_timeout: int = 5
    health_check_interval: int = 30
    decode_responses: bool = True

    def __post_init__(self) -> None:
        if self.socket_connect_timeout < 1:
            raise ValueError(f"socket_connect_timeout must be >= 1, got {self.socket_connect_timeout}")
        if self.socket_timeout < 1:
            raise ValueError(f"socket_timeout must be >= 1, got {self.socket_timeout}")

# =================================================
# Collector
# =================================================

class RedisCollector(BaseCollector[RedisSettings]):
    """Small, read-only Redis collector with auto-detection for Sentinel mode"""

    def __init__(
            self,
            redis_uri: str | None,
            *,
            settings: RedisSettings | None = None,
            tls_enabled: bool = False,
            ) -> None:
        """Initialize the collector with a Redis URI.

        ``tls_enabled`` transparently upgrades the URI to ``rediss://``
        (basic-level TLS, no CA files or verification overrides) — see
        :func:`apply_redis_tls`. Sourced from ``Config.redis_tls_enabled``.
        """
        require_extended(
            "RedisCollector",
            hint="Redis collection requires Extended Mode.",
        )
        super().__init__(settings=settings)
        self.redis_uri = apply_redis_tls(redis_uri, tls_enabled)
        self._mode: RedisMode | None = None
        self._tunnel: ProtocolTunnelHandle | None = None

    @classmethod
    def _default_settings(cls) -> RedisSettings:
        return RedisSettings()

    @classmethod
    def from_config(cls, *, settings: RedisSettings | None = None) -> Self | None:
        """Create a collector using the application configuration"""
        require_extended(
            "RedisCollector.from_config",
            hint="Redis collection requires Extended Mode.",
        )
        config = ctx().config
        uri = config.redis_uri
        if not uri:
            return None
        # Apply the user-configured connection timeout (config.json / `config edit`).
        # Clamped to a safe range; an absent/invalid value keeps the historical 5s.
        if settings is None:
            timeout_s = getattr(config, "redis_timeout_s", 5) or 5
            timeout_s = max(1, min(int(timeout_s), 120))
            settings = RedisSettings(
                socket_connect_timeout=timeout_s,
                socket_timeout=timeout_s,
            )
        return cls(uri, settings=settings, tls_enabled=config.redis_tls_enabled)

    @property
    def settings(self) -> RedisSettings:
        return self._settings

    @property
    def mode(self) -> RedisMode | None:
        return self._mode

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    def _endpoint_label(self) -> str:
        """``host:port`` for the configured URI, with credentials stripped.

        Used only in connection-error messages so a skipped rule can report
        *where* Redis was unreachable without ever echoing the password.
        """
        try:
            from urllib.parse import urlparse
            parsed = urlparse(encode_uri_credentials(self.redis_uri or ""))
            host = parsed.hostname or "unknown-host"
            port = parsed.port or 6379
            return f"{host}:{port}"
        except Exception:  # best-effort label, never fatal
            return "the configured Redis endpoint"

    def connect(self) -> None:
        """Create the client and verify connectivity with a ping.

        A connection or auth failure is re-raised as
        ``RedisConnectionNotEstablishedError`` carrying the host:port (no
        credentials), so the capture engine records a clear reason in
        ``_atlas.metadata.failed_modules`` — which the report turns into a
        "skipped: couldn't reach Redis at host:port" callout.
        """
        if not self.redis_uri:
            return

        connect_uri = self.redis_uri

        # Extended-tier, opt-in: route this connection through an SSH jumphost.
        # See Config.jumphost_tunnel — already tier-gated and None unless an
        # environment explicitly configured one. Constructing a collector
        # directly (bypassing from_config(), e.g. in tests) may run before
        # AtlasContext exists — treat that the same as "no jumphost".
        try:
            jumphost = ctx().config.jumphost_tunnel
        except ContextNotInitializedError:
            jumphost = None
        if jumphost is not None and jumphost.tunnel_redis:
            try:
                connect_uri, self._tunnel = open_protocol_tunnel(jumphost, self.redis_uri)
            except (ValueError, CollectorConnectionError) as e:
                raise RedisConnectionNotEstablishedError(
                    f"Could not open jumphost tunnel for Redis at "
                    f"{self._endpoint_label()}: {e}"
                ) from e

        try:
            self._client = redis.from_url(
                encode_redis_uri(connect_uri),
                socket_connect_timeout=self._settings.socket_connect_timeout,
                socket_timeout=self._settings.socket_timeout,
                health_check_interval=self._settings.health_check_interval,
                decode_responses=self._settings.decode_responses,
            )
            # Verify we can actually reach Redis quickly
            self._client.ping()
        except (RedisError, RedisConnectionError, OSError) as exc:
            self._client = None
            self._close_tunnel()
            raise RedisConnectionNotEstablishedError(
                f"Could not connect to Redis at {self._endpoint_label()} — {exc}"
            ) from exc

    def close(self) -> None:
        """Close the connection pool, if any"""
        if self._client:
            try:
                self._client.close()
            except OSError: # nosec B110 - best-effort cleanup
                pass
            self._client = None
            self._mode = None
        self._close_tunnel()

    def _close_tunnel(self) -> None:
        """Cancel the jumphost forward for this connection, if one is open."""
        if self._tunnel is not None:
            try:
                self._tunnel.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # best-effort cleanup — the master socket itself stays open
            self._tunnel = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "connected" if self._client is not None else "disconnected"
        has_uri = bool(self.redis_uri)
        mode_str = f" mode={self._mode.value}" if self._mode else ""
        return f"<RedisCollector configured={has_uri} {state}{mode_str}>"

    def _detect_mode(self, info: dict[str, Any]) -> RedisMode:
        """Detect whether we're connected to a Redis instance or a Sentinel"""
        raw_mode = info.get("redis_mode", "standalone")
        if raw_mode == "sentinel":
            self._mode = RedisMode.SENTINEL
        else:
            self._mode = RedisMode.REDIS
        return self._mode

    def get_info(self) -> dict[str, Any]:
        """Fetch Redis INFO"""
        if not self.redis_uri:
            return {}

        try:
            if self._client is None:
                self.connect()
            if self._client is None:
                return {}

            return self._client.info()
        except (RedisError, RedisConnectionError) as exc:
            logger.debug("Redis INFO failed: %s", exc)
            return {}
        except Exception as exc:
            logger.debug("Unexpected Redis INFO failure: %s", exc)
            return {}

    # -------------------------------------------------
    # Mode-specific collection
    # -------------------------------------------------

    def _collect_runtime_config(self) -> tuple[dict[str, Any], str]:
        """Fetch runtime config values via CONFIG GET.

        Provides a protocol-based alternative to parsing redis.conf
        over SSH. Values are used as alt_path fallbacks for rules
        RDS-004 through RDS-013.

        Returns (config_dict, status) where status is one of:
          "ok"           — all or most keys returned successfully
          "acl_denied"   — NOPERM: user lacks +config|get permission
          "not_supported" — CONFIG command not available (managed service)
          "partial"      — some keys returned, some failed (managed service quirk)

        Managed services (ElastiCache, Redis Cloud) often expose CONFIG GET
        for some parameters but silently drop unsupported ones; others block
        CONFIG entirely. Callers can use the status to set expectations in
        the capture output rather than treating empty == "all failed".
        """
        config: dict[str, Any] = {}
        acl_denied = 0
        not_supported = 0
        succeeded = 0

        for key in _RUNTIME_CONFIG_KEYS:
            try:
                result = self._client.config_get(key)
                if key in result:
                    config[key] = result[key]
                    succeeded += 1
                else:
                    # Key accepted but returned no value — parameter not supported
                    not_supported += 1
                    logger.debug("CONFIG GET %s returned no value (unsupported parameter)", key)
            except (RedisError, RedisConnectionError) as exc:
                err = str(exc).upper()
                if "NOPERM" in err or "NO PERMISSION" in err or "ACL" in err:
                    acl_denied += 1
                    logger.debug("CONFIG GET %s ACL denied: %s", key, exc)
                elif "ERR UNKNOWN COMMAND" in err or "COMMAND NOT ALLOWED" in err:
                    not_supported += 1
                    logger.debug("CONFIG GET %s not supported (managed service?): %s", key, exc)
                else:
                    not_supported += 1
                    logger.debug("CONFIG GET %s failed: %s", key, exc)

        total = len(_RUNTIME_CONFIG_KEYS)
        if acl_denied == total:
            status = "acl_denied"
            logger.info(
                "All CONFIG GET calls denied — Redis user likely lacks "
                "+config|get permission. Add '+config|get' to the Redis ACL. "
                "Config file fallback (SSH) will be used if available."
            )
        elif not_supported == total:
            status = "not_supported"
            logger.info(
                "CONFIG GET is not available — this Redis instance is likely a "
                "managed service (ElastiCache, Redis Cloud, etc.) that does not "
                "expose the CONFIG command. Runtime config rules will use SSH "
                "fallback if available."
            )
        elif succeeded == 0:
            status = "acl_denied" if acl_denied > not_supported else "not_supported"
        elif succeeded < total:
            status = "partial"
            logger.debug(
                "CONFIG GET returned %d/%d keys — managed service may not support all parameters",
                succeeded, total,
            )
        else:
            status = "ok"

        # Parse client-output-buffer-limit into nested dict to match filesystem parser
        raw_buffer = config.get("client-output-buffer-limit")
        if raw_buffer and isinstance(raw_buffer, str):
            config["client-output-buffer-limit"] = _parse_buffer_limit(raw_buffer)

        return config, status

    @staticmethod
    def _extract_sentinel_config(masters: dict[str, dict]) -> dict[str, dict]:
        """Extract sentinel config fields from sentinel_masters() data.

        sentinel_masters() returns per-master dicts that include
        down-after-milliseconds, parallel-syncs, and failover-timeout.
        This reshapes them into the path structure used by rules
        RDS-014 through RDS-016 as alt_path fallbacks.

        Returns: {"itentialmaster": {"down-after-milliseconds": 5000, ...}, ...}
        """
        _SENTINEL_FIELDS = frozenset({
            "down-after-milliseconds",
            "parallel-syncs",
            "failover-timeout",
        })

        extracted: dict[str, dict] = {}
        for name, master_info in masters.items():
            master_data = master_info.get("master", {})
            fields = {
                k: v for k, v in master_data.items()
                if k in _SENTINEL_FIELDS
            }
            if fields:
                extracted[name] = fields
        return extracted

    def _collect_redis(self, info: dict[str, Any]) -> dict[str, Any]:
        """Collect standard Redis data (INFO, ACL, runtime config)"""
        step = "acl_users"
        try:
            acl_users = self._client.acl_users()
        except (RedisError, RedisConnectionError) as exc:
            logger.debug("Redis collect failed at step '%s': %s", step, exc)
            acl_users = []

        # ACL LIST — protocol-primary source for redis_acl rule detail.
        # SSH parsing of redis.conf's "user" directives is only a fallback
        # for this (see capture_engine.py's Redis ACL extraction), since a
        # live server's ACL rules can differ from what's on disk (e.g. ACL
        # SETUSER changes applied at runtime without a CONFIG REWRITE).
        step = "acl"
        try:
            acl = _parse_acl_list(self._client.acl_list())
        except (RedisError, RedisConnectionError) as exc:
            logger.debug("Redis collect failed at step '%s': %s", step, exc)
            acl = []

        # CONFIG GET fallback for redis.conf rules (alt_path)
        step = "runtime_config"
        config_get_status = "ok"
        try:
            runtime_config, config_get_status = self._collect_runtime_config()
        except (RedisError, RedisConnectionError) as exc:
            logger.debug("Redis collect failed at step '%s': %s", step, exc)
            runtime_config = {}
            config_get_status = "error"

        # Read the key count for the connected DB out of the INFO keyspace
        # section rather than issuing a separate DBSIZE call — some ACLs
        # (command allow-lists rather than category grants) permit INFO but
        # not DBSIZE, and INFO is already required above for mode detection.
        # Redis omits a db's "dbN" entry from the keyspace section entirely
        # when that db has zero keys, so a missing entry means 0, not unknown.
        try:
            db_index = self._client.connection_pool.connection_kwargs.get("db", 0)
        except AttributeError:
            db_index = 0
        db_keyspace = info.get(f"db{db_index}")
        key_count = db_keyspace.get("keys", 0) if isinstance(db_keyspace, dict) else 0

        payload: dict[str, Any] = {
            "info": info,
            "acl_users": acl_users,
            # Status lets validation and reporting distinguish "not configured"
            # from "managed service doesn't support CONFIG" from "ACL denied"
            "config_get_status": config_get_status,
            "key_count": key_count,
        }
        if runtime_config:
            payload["runtime_config"] = runtime_config
        if acl:
            payload["acl"] = acl
        return payload

    def _collect_sentinel(self, info: dict[str, Any]) -> dict[str, Any]:
        """Collect Sentinel-specific data (masters, replicas, topology)"""
        masters = {}
        step = "sentinel_masters"
        try:
            raw_masters = self._client.sentinel_masters()
            for name, master_data in raw_masters.items():
                step = f"sentinel_slaves({name})"
                try:
                    replicas = self._client.sentinel_slaves(name)
                except (RedisError, RedisConnectionError) as exc:
                    logger.debug("Redis collect failed at step '%s': %s", step, exc)
                    replicas = []

                step = f"sentinel_sentinels({name})"
                try:
                    sentinels = self._client.sentinel_sentinels(name)
                except (RedisError, RedisConnectionError) as exc:
                    logger.debug("Redis collect failed at step '%s': %s", step, exc)
                    sentinels = []

                masters[name] = {
                    "master": master_data,
                    "replicas": replicas,
                    "sentinels": sentinels,
                }
        except (RedisError, RedisConnectionError) as exc:
            logger.debug("Redis collect failed at step '%s': %s", step, exc)

        return {
            "info": info,
            "masters": masters,
            "sentinel_runtime": self._extract_sentinel_config(masters),
        }

    # -------------------------------------------------
    # Public API
    # -------------------------------------------------

    def collect(self) -> dict[str, Any]:
        """Collect a small, consistent payload for Platform Atlas.

        Auto-detects the server mode (Redis vs. Sentinel) and runs
        the appropriate collection commands for each.
        """
        empty = {"ok": False, "mode": None, "ping_ms": None}
        if not self.redis_uri or not self._client:
            return empty

        step = "ping"
        try:
            if self._client is None:
                self.connect()
            if self._client is None:
                return empty

            t0 = time.perf_counter()
            ok = bool(self._client.ping())
            ping_ms = (time.perf_counter() - t0) * 1000.0

            if not ok:
                raise RedisConnectionNotEstablishedError(
                    f"Redis at {self._endpoint_label()} did not respond to PING"
                )

            # INFO is common to both modes and used for detection
            step = "info"
            info = self._client.info()
            mode = self._detect_mode(info)
            logger.debug("Detected Redis mode: %s", mode.value)

            # Branch into mode-specific collection
            if mode == RedisMode.SENTINEL:
                payload = self._collect_sentinel(info)
            else:
                payload = self._collect_redis(info)

            return {
                "ok": True,
                "mode": mode.value,
                "ping_ms": ping_ms,
                **payload,
            }
        except RedisConnectionNotEstablishedError:
            raise
        except (RedisError, RedisConnectionError) as exc:
            logger.debug("Redis collect failed at step '%s': %s", step, exc)
            raise RedisConnectionNotEstablishedError(
                f"Redis became unreachable at {self._endpoint_label()} during '{step}' — {exc}"
            ) from exc

    @staticmethod
    def preflight() -> CheckResult:
        """Test Redis connectivity and report detected mode"""
        service_name = "Redis"
        try:
            config = ctx().config

            if not getattr(config, "redis_uri", None):
                return CheckResult.skip(service_name, "Not configured (redis_uri empty)")

            collector = RedisCollector.from_config()
            if collector is None:
                return CheckResult.skip(service_name, "Not configured (collector unavailable)")

            collector.connect()

            if collector._client is None:
                return CheckResult.skip(service_name, "Client not initialized")

            # Detect mode during preflight so the user knows what they're connected to
            info = collector._client.info()
            mode = collector._detect_mode(info)

            collector.close()
            return CheckResult.ok(service_name, f"Connected successfully ({mode.value} mode)")
        except Exception as e:
            return CheckResult.fail(service_name, "Connection failed", str(e))

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
