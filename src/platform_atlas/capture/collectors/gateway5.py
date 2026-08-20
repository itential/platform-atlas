"""
Gateway5 Collector - Environment variable collection for Itential Gateway5

Three collection paths:

* ``collect_env()`` — reads ``GATEWAY_*`` vars via ``printenv`` over SSH
  (bare-metal / VM gateways).
* ``collect_from_file()`` — parses a local Docker Compose or Helm values file
  (containerized gateways) via :func:`parse_gateway5_yaml`. No SSH required.
* ``collect_from_ssh_conf()`` — reads the IAG5 SERVER config file
  (``gateway.conf``, INI-style) over SSH and maps ``[section] key`` settings to
  the audited variables. Only a server-mode file is accepted.

``collect_env``/``collect_from_file`` emit ``{variables, sources, summary}``;
``collect_from_ssh_conf`` emits ``{config_file: {section: {key: value}}}`` which
rules read via their ``alt_path``. Only variables in ``GATEWAY5_VARIABLES`` are
captured; anything else is dropped.

Example:
    >>> collector = Gateway5Collector(transport=ssh_transport)
    >>> data = collector.collect_env()
    >>> file_data = Gateway5Collector(source_path="~/iag5-compose.yml").collect_from_file()
"""

from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from platform_atlas.core.context import require_infra
from platform_atlas.core.preflight import CheckResult
from platform_atlas.core.transport import Transport

logger = logging.getLogger(__name__)

class GW5Category(str, Enum):
    """Logical grouping for Gateway5 variables"""
    STORAGE = "storage"
    LOGGING = "logging"
    TLS = "tls"
    CERTIFICATES = "certificates"
    CONNECT = "connect"
    FEATURES = "features"
    SERVER = "server"
    RUNNER = "runner"
    PRUNER = "pruner"

# IAG5 server config file (gateway.conf) is INI-style: the SAME setting appears
# as the env var GATEWAY_<SECTION>_<KEY> and, in the file, as ``[<section>]``
# with key ``<key>`` (all lowercase). The mapping is mechanical because every
# config section is a single word, so it can be derived from the env-var name.
# _FILE_LOCATION_OVERRIDES carries the rare spec exceptions where that mechanical
# split is wrong — e.g. GATEWAY_APPLICATION_MODE lives at ``[application]`` under
# ``mode`` (not ``application_mode``, despite the env var's name). It's not an
# audited variable, but it's the key used to detect a server config, so it's
# recorded here for reference.
_FILE_LOCATION_OVERRIDES: dict[str, tuple[str, str]] = {
    "GATEWAY_APPLICATION_MODE": ("application", "mode"),
}


def _derive_file_location(name: str) -> tuple[str, str]:
    """Map a ``GATEWAY_*`` env var name to its ``(section, key)`` in gateway.conf."""
    if name in _FILE_LOCATION_OVERRIDES:
        return _FILE_LOCATION_OVERRIDES[name]
    body = name.removeprefix("GATEWAY_")
    section, _, key = body.partition("_")
    return section.lower(), key.lower()


@dataclass(frozen=True, slots=True)
class GW5Variable:
    """Definition of a single Gateway5 environment variable"""
    name: str
    category: GW5Category
    label: str

    @property
    def file_section(self) -> str:
        """Section name in the IAG5 server config file (gateway.conf)."""
        return _derive_file_location(self.name)[0]

    @property
    def file_key(self) -> str:
        """Key within :attr:`file_section` in the server config file."""
        return _derive_file_location(self.name)[1]

GATEWAY5_VARIABLES: tuple[GW5Variable, ...] = (
    # Storage
    GW5Variable("GATEWAY_STORE_BACKEND",                  GW5Category.STORAGE,      "Store backend type"),
    # Logging
    GW5Variable("GATEWAY_LOG_CONSOLE_JSON",               GW5Category.LOGGING,      "Console JSON logging"),
    GW5Variable("GATEWAY_LOG_FILE_JSON",                  GW5Category.LOGGING,      "File JSON logging"),
    GW5Variable("GATEWAY_LOG_LEVEL",                      GW5Category.LOGGING,      "Log level"),
    # Client TLS
    GW5Variable("GATEWAY_CLIENT_USE_TLS",                 GW5Category.TLS,          "Client TLS enabled"),
    GW5Variable("GATEWAY_CLIENT_PRIVATE_KEY_FILE",        GW5Category.TLS,          "Client private key file"),
    # Certificates
    GW5Variable("GATEWAY_SERVER_CERTIFICATE_FILE",        GW5Category.CERTIFICATES, "Server certificate file"),
    GW5Variable("GATEWAY_CLIENT_CERTIFICATE_FILE",        GW5Category.CERTIFICATES, "Client certificate file"),
    GW5Variable("GATEWAY_CONNECT_CERTIFICATE_FILE",       GW5Category.CERTIFICATES, "Connect certificate file"),
    GW5Variable("GATEWAY_CONNECT_PRIVATE_KEY_FILE",       GW5Category.CERTIFICATES, "Connect private key file"),
    GW5Variable("GATEWAY_RUNNER_CERTIFICATE_FILE",        GW5Category.CERTIFICATES, "Runner certificate file"),
    # Connect / Gateway Manager
    GW5Variable("GATEWAY_CONNECT_ENABLED",                GW5Category.CONNECT,      "Gateway manager enabled"),
    GW5Variable("GATEWAY_CONNECT_INSECURE_TLS",           GW5Category.CONNECT,      "Connect insecure TLS"),
    GW5Variable("GATEWAY_CONNECT_SERVER_HA_ENABLED",      GW5Category.CONNECT,      "Connect HA enabled"),
    GW5Variable("GATEWAY_CONNECT_SERVER_HA_IS_PRIMARY",   GW5Category.CONNECT,      "Connect HA is primary"),
    # Features
    GW5Variable("GATEWAY_FEATURES_ANSIBLE_ENABLED",       GW5Category.FEATURES,     "Feature: Ansible"),
    GW5Variable("GATEWAY_FEATURES_HOSTKEYS_ENABLED",      GW5Category.FEATURES,     "Feature: Host Keys"),
    GW5Variable("GATEWAY_FEATURES_OPENTOFU_ENABLED",      GW5Category.FEATURES,     "Feature: OpenTofu"),
    GW5Variable("GATEWAY_FEATURES_PYTHON_ENABLED",        GW5Category.FEATURES,     "Feature: Python"),
    # Server
    GW5Variable("GATEWAY_SERVER_DISTRIBUTED_EXECUTION",   GW5Category.SERVER,       "Distributed execution"),
    GW5Variable("GATEWAY_SERVER_USE_TLS",                 GW5Category.SERVER,       "Server TLS enabled"),
    # Runner
    GW5Variable("GATEWAY_RUNNER_ANNOUNCEMENT_ADDRESS",    GW5Category.RUNNER,       "Runner announcement address"),
    GW5Variable("GATEWAY_RUNNER_USE_TLS",                 GW5Category.RUNNER,       "Runner TLS enabled"),
    # Venv Pruner
    GW5Variable("GATEWAY_APPLICATION_VENV_SWEEP_INTERVAL",   GW5Category.PRUNER,    "Venv pruner sweep interval"),
    GW5Variable("GATEWAY_APPLICATION_VENV_RETENTION_PERIOD", GW5Category.PRUNER,    "Venv pruner retention period"),
)

# Quick-access set for membership checks
_VAR_NAMES: frozenset[str] = frozenset(v.name for v in GATEWAY5_VARIABLES)

@dataclass
class _CollectedVars:
    """Internal accumulator for environment vars"""
    values: dict[str, str | None] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    # The Helm chart's declared image tag (e.g. "5.5.0-amd64"), when parsed
    # from a values.yaml. Not a GATEWAY_* variable — a separate fallback for
    # IAG-030's version check when no live iagctl data is available.
    image_tag: str | None = None

    def seed(self) -> None:
        """Pre-populate every known variable as None (unresolved)."""
        for var in GATEWAY5_VARIABLES:
            self.values.setdefault(var.name, None)

    def set_if_missing(self, name: str, value: str, source: str) -> None:
        """Set a variable only if it hasn't already been resolved."""
        if name not in _VAR_NAMES:
            return
        if self.values.get(name) is not None:
            return  # already resolved by a higher-priority tier
        self.values[name] = value
        self.sources[name] = source

    @property
    def resolved(self) -> dict[str, str]:
        return {k: v for k, v in self.values.items() if v is not None}

    @property
    def unresolved_names(self) -> list[str]:
        return [k for k, v in self.values.items() if v is None]

    def to_dict(self) -> dict[str, Any]:
        """Build the capture-ready dict for the capture engine"""
        result: dict[str, Any] = {
            "variables": dict(self.values),
            "sources": dict(self.sources),
            "summary": {
                "total": len(self.values),
                "resolved": len(self.resolved),
                "unresolved": len(self.unresolved_names),
                "unresolved_keys": self.unresolved_names,
            },
        }
        if self.image_tag is not None:
            result["image_tag"] = self.image_tag
        return result

# IAG5 Helm chart structured keys → GATEWAY_* env var names. Used when the
# provided values.yaml uses the official chart's camelCase settings blocks
# rather than a raw env list. (Single source of truth — the Kubernetes
# collector imports this rather than keeping its own copy.)
_IAG5_TO_GATEWAY_ENV: dict[str, str] = {
    # applicationSettings
    "logLevel": "GATEWAY_LOG_LEVEL",
    "storeBackend": "GATEWAY_STORE_BACKEND",
    # serverSettings
    "connectEnabled": "GATEWAY_CONNECT_ENABLED",
    "connectInsecureEnabled": "GATEWAY_CONNECT_INSECURE_TLS",
}


def _gw5_stringify(value: Any) -> str:
    """Normalize a YAML/env value to the string form Atlas rules expect.

    YAML booleans must become lowercase ``"true"``/``"false"`` (matching how the
    variable actually appears in a container's environment) rather than Python's
    ``"True"``/``"False"``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _harvest_env_mapping(env: Any, collected: _CollectedVars, source: str) -> None:
    """Pull ``GATEWAY_*`` entries from a compose/helm env block into ``collected``.

    Accepts the three shapes seen in the wild:
        * mapping ............ ``{GATEWAY_X: value}``
        * ``KEY=value`` list . ``["GATEWAY_X=value"]``  (docker-compose / .env)
        * k8s env list ....... ``[{"name": "GATEWAY_X", "value": ...}]``

    ``set_if_missing`` already filters to ``GATEWAY5_VARIABLES`` and keeps the
    first value seen, so callers need not pre-check membership or precedence.
    """
    if isinstance(env, dict):
        for key, value in env.items():
            if isinstance(key, str) and value is not None:
                collected.set_if_missing(key, _gw5_stringify(value), source)
    elif isinstance(env, list):
        for entry in env:
            if isinstance(entry, dict) and "name" in entry:
                name = entry.get("name")
                value = entry.get("value")
                if isinstance(name, str) and value is not None:
                    collected.set_if_missing(name, _gw5_stringify(value), source)
                continue
            entry_str = str(entry)
            if "=" in entry_str:
                key, _, value = entry_str.partition("=")
                collected.set_if_missing(key.strip(), value.strip(), source)


def _harvest_structured_iag5(data: dict[str, Any], collected: _CollectedVars) -> None:
    """Map an official IAG5 Helm chart's structured settings to ``GATEWAY_*`` vars."""
    app_settings = data.get("applicationSettings") or {}
    server_settings = data.get("serverSettings") or {}
    runner_settings = data.get("runnerSettings") or {}

    if isinstance(app_settings, dict):
        for yaml_key, env_name in _IAG5_TO_GATEWAY_ENV.items():
            val = app_settings.get(yaml_key)
            if val is not None:
                collected.set_if_missing(env_name, _gw5_stringify(val), "helm_values")

    if isinstance(server_settings, dict):
        for yaml_key in ("connectEnabled", "connectInsecureEnabled"):
            val = server_settings.get(yaml_key)
            if val is not None:
                collected.set_if_missing(
                    _IAG5_TO_GATEWAY_ENV[yaml_key], _gw5_stringify(val), "helm_values"
                )

    if isinstance(runner_settings, dict) and (runner_settings.get("replicaCount") or 0) > 0:
        # More than one runner replica implies distributed execution is on.
        collected.set_if_missing(
            "GATEWAY_SERVER_DISTRIBUTED_EXECUTION", "true", "helm_values"
        )

    if isinstance(server_settings, dict) and (server_settings.get("replicaCount") or 0) > 1:
        # More than one server replica implies Gateway Connect HA is on
        # (mirrors the chart's own $haEnabled template logic).
        collected.set_if_missing(
            "GATEWAY_CONNECT_SERVER_HA_ENABLED", "true", "helm_values"
        )

    if data.get("useTLS") is not None:
        tls_val = _gw5_stringify(data["useTLS"])
        for env_name in (
            "GATEWAY_SERVER_USE_TLS",
            "GATEWAY_CLIENT_USE_TLS",
            "GATEWAY_RUNNER_USE_TLS",
        ):
            collected.set_if_missing(env_name, tls_val, "helm_values")

    # Inline GATEWAY_* env overrides nested inside the settings blocks.
    for block in (server_settings, runner_settings, app_settings):
        if isinstance(block, dict) and block.get("env") is not None:
            _harvest_env_mapping(block["env"], collected, "helm_env_override")


def _harvest_image_tag(data: dict[str, Any], collected: _CollectedVars) -> None:
    """Record the chart's declared image tag, e.g. ``image.tag: "5.5.0-amd64"``.

    Not a ``GATEWAY_*`` variable — this is the version the customer chose to
    deploy, and a solid fallback for IAG-030's version check when no live
    ``iagctl`` data was collected (values.yaml-only capture, no pod exec).
    ``operators.parse_version`` already tolerates a trailing arch suffix.
    """
    image = data.get("image")
    if isinstance(image, dict) and image.get("tag"):
        collected.image_tag = str(image["tag"])


def parse_gateway5_yaml(data: dict[str, Any]) -> _CollectedVars:
    """Extract Gateway5 ``GATEWAY_*`` variables from a parsed Compose/Helm dict.

    Handles three real-world shapes, applied in priority order (first value
    seen wins, courtesy of ``_CollectedVars.set_if_missing``):

        1. docker-compose .... ``services.<svc>.environment`` (mapping or list)
        2. helm raw env ...... ``<gateway-key>.env`` / ``.extraEnv`` / ``.environment``
        3. structured IAG5 ... ``applicationSettings`` / ``serverSettings`` /
                                ``runnerSettings`` / top-level ``useTLS``

    Also records ``image.tag`` (if present) as ``.image_tag`` — see
    :func:`_harvest_image_tag`.

    Returns a seeded :class:`_CollectedVars` (every known variable present, with
    unresolved ones left ``None``). Callers inspect ``.resolved`` / ``.to_dict()``.
    Variables outside ``GATEWAY5_VARIABLES`` are dropped, matching the SSH path.
    """
    collected = _CollectedVars()
    collected.seed()
    if not isinstance(data, dict):
        return collected

    # 1. docker-compose: services.*.environment
    services = data.get("services")
    if isinstance(services, dict):
        for svc_name, svc_def in services.items():
            if isinstance(svc_def, dict) and svc_def.get("environment") is not None:
                _harvest_env_mapping(
                    svc_def["environment"], collected, f"docker-compose:{svc_name}"
                )

    # 2. helm raw env blocks under a gateway-ish top-level key
    for top_key in ("gateway", "gateway5", "itential-gateway", "automation-gateway"):
        section = data.get(top_key)
        if not isinstance(section, dict):
            continue
        for env_key in ("env", "extraEnv", "environment"):
            if section.get(env_key) is not None:
                _harvest_env_mapping(section[env_key], collected, "helm-values")

    # 3. structured IAG5 Helm chart settings blocks
    _harvest_structured_iag5(data, collected)
    _harvest_image_tag(data, collected)

    return collected


# Conservative charset for a remote config-file path. The path is operator-typed
# (env wizard) and read with ``cat <path>`` so a leading ``~`` expands remotely —
# restricting to these characters keeps that shell-injection-safe.
_SAFE_REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9._/~-]+$")


def _strip_conf_quotes(value: str) -> str:
    """Strip a single layer of matching surrounding quotes from an INI value."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_gateway5_conf(text: str) -> dict[str, dict[str, str]]:
    """Parse an IAG5 server config file into ``{section: {key: value}}``.

    The file is INI-style (``[section]`` headers, ``key = value`` pairs). Full-line
    and whitespace-preceded inline comments (``#``/``;``) are ignored, and a single
    layer of surrounding quotes is stripped from values. Section names are kept
    verbatim, so a named client profile (``[client:production]``) stays distinct
    from the base ``[client]`` section. Raises ``ValueError`` on malformed input.
    """
    parser = configparser.ConfigParser(
        interpolation=None,                  # values may legitimately contain "%"
        strict=False,                        # tolerate duplicate sections/keys
        allow_no_value=True,
        inline_comment_prefixes=("#", ";"),  # strip trailing "key = val  # note"
    )
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise ValueError(f"not valid INI ({exc})") from exc

    result: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        result[section] = {
            key: _strip_conf_quotes(value)
            for key, value in parser.items(section)
            if value is not None
        }
    return result


class Gateway5Collector:
    """Collects Gateway5 environment variables over SSH or from a values file"""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        source_path: str = "",
        conf_path: str = "",
    ) -> None:
        require_infra(
            "Gateway5Collector",
            hint="Gateway5 collection is unavailable in Standard Mode.",
        )
        self._transport = transport
        self._source_path = source_path
        # Remote path to the IAG5 server config file (gateway.conf), read over
        # SSH. When set, collection uses collect_from_ssh_conf instead of printenv.
        self._conf_path = conf_path

    def __repr__(self) -> str:
        if self._source_path:
            return f"<Gateway5Collector source_path={self._source_path!r}>"
        if self._conf_path:
            return f"<Gateway5Collector conf_path={self._conf_path!r}>"
        transport = type(self._transport).__name__
        return f"<Gateway5Collector transport={transport}>"

    def collect_env(self) -> dict[str, Any]:
        """Run the full tiered collection and return capture-ready dict"""
        collected = _CollectedVars()
        collected.seed()

        self._collect_from_env(collected)

        if not collected.resolved:
            logger.debug("Gateway5: no env vars found - skipping")
            return {}

        logger.debug(
            "Gateway5: collection complete - %d/%d resolved",
            len(collected.resolved), len(collected.values),
        )
        return collected.to_dict()

    def collect_from_file(self) -> dict[str, Any]:
        """Collect Gateway5 env vars from a local Docker Compose / Helm values file.

        The file is read on the Atlas host (no SSH). A missing or empty path
        raises ``FileNotFoundError`` so capture surfaces the module as failed
        (and therefore recoverable via guided collection) rather than silently
        returning nothing — per the "file is authoritative" source model.
        """
        if not self._source_path:
            raise FileNotFoundError("Gateway5 file source has no path configured")

        path = Path(self._source_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Gateway5 source file not found: {path}")

        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"Gateway5 source file is not valid YAML: {exc}") from exc

        if not isinstance(data, dict):
            logger.debug("Gateway5: source file %s did not parse to a mapping", path)
            return {}

        collected = parse_gateway5_yaml(data)
        if not collected.resolved:
            logger.debug("Gateway5: no known variables found in %s", path)
            return {}

        logger.debug(
            "Gateway5: file collection complete (%s) — %d/%d resolved",
            path, len(collected.resolved), len(collected.values),
        )
        return collected.to_dict()

    def collect_from_ssh_conf(self) -> dict[str, Any]:
        """Read the IAG5 SERVER config file (``gateway.conf``) over SSH.

        Extracts the audited ``[section] key`` settings into
        ``{"config_file": {section: {key: value}}}`` — the shape gateway5 rules
        resolve via their ``alt_path``. ``GATEWAY_*`` env vars are NOT read in this
        mode (the file is the single source).

        BLOCKS (raises ``ValueError``) when the file's
        ``[application] mode`` is not ``server`` — only a server
        config is audited here. A missing/unreadable/invalid file raises so the
        gateway5 module surfaces as failed (recoverable) rather than empty.
        """
        if not self._conf_path:
            raise FileNotFoundError("Gateway5 config-file source has no path configured")

        text = self._read_remote_conf()
        try:
            parsed = parse_gateway5_conf(text)
        except ValueError as exc:
            raise ValueError(
                f"Gateway 5 config file at {self._conf_path} is {exc}"
            ) from exc

        mode = (parsed.get("application", {}).get("mode") or "").strip().lower()
        if mode != "server":
            raise ValueError(
                f"Gateway 5 config file at {self._conf_path} has mode="
                f"'{mode or 'unset'}', which is not a server config (expected "
                "'server'). Point Atlas at the server's gateway.conf."
            )

        config_file = self._select_audited_conf(parsed)
        logger.debug(
            "Gateway5: server config file parsed (%s) — %d audited setting(s)",
            self._conf_path, sum(len(v) for v in config_file.values()),
        )
        return {"config_file": config_file}

    def _read_remote_conf(self) -> str:
        """Read the configured remote config file over SSH; raise on failure."""
        path = self._conf_path
        if not _SAFE_REMOTE_PATH_RE.match(path):
            raise ValueError(
                f"Refusing to read Gateway 5 config file — unsafe path: {path!r}"
            )
        # Unquoted so the remote shell expands a leading "~"; the charset guard
        # above keeps this safe from shell injection.
        result = self._transport.run_command(f"cat {path}")
        if not result.ok or not result.stdout.strip():
            detail = (result.stderr or "").strip() or "file not found or empty"
            raise FileNotFoundError(
                f"Could not read Gateway 5 config file at {path}: {detail}"
            )
        return result.stdout

    def _select_audited_conf(
        self, parsed: dict[str, dict[str, str]]
    ) -> dict[str, dict[str, str]]:
        """Pull only the audited (whitelisted) settings from a parsed config file,
        preserving the file's ``section → key`` shape so rule ``alt_path`` values
        resolve. ``mode`` is always kept for provenance/reporting.
        """
        out: dict[str, dict[str, str]] = {}
        for var in GATEWAY5_VARIABLES:
            value = parsed.get(var.file_section, {}).get(var.file_key)
            if value is not None:
                out.setdefault(var.file_section, {})[var.file_key] = value
        app_mode = parsed.get("application", {}).get("mode")
        if app_mode is not None:
            out.setdefault("application", {})["mode"] = app_mode
        return out

    def preflight(self) -> CheckResult:
        """Verify the configured Gateway5 source is reachable/usable.

        File mode validates the local Compose/Helm file exists and parses to at
        least one known variable; SSH mode probes the remote host for env vars.
        """
        service_name = "Gateway5"

        if self._conf_path:
            try:
                text = self._read_remote_conf()
            except Exception as e:
                return CheckResult.fail(
                    service_name, "Cannot read Gateway5 server config file", str(e),
                )
            try:
                parsed = parse_gateway5_conf(text)
            except ValueError as e:
                return CheckResult.fail(
                    service_name, "Server config file is not valid INI", str(e),
                )
            mode = (parsed.get("application", {}).get("mode") or "").strip().lower()
            if mode != "server":
                return CheckResult.fail(
                    service_name,
                    f"Not a server config file (mode={mode or 'unset'})",
                    self._conf_path,
                )
            found = sum(
                1 for v in GATEWAY5_VARIABLES
                if parsed.get(v.file_section, {}).get(v.file_key) is not None
            )
            return CheckResult.ok(
                service_name,
                f"{found} Gateway5 setting(s) found in server config file",
            )

        if self._source_path:
            path = Path(self._source_path).expanduser()
            if not path.is_file():
                return CheckResult.fail(
                    service_name, "Gateway5 source file not found", str(path),
                )
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                return CheckResult.fail(
                    service_name, "Source file is not valid YAML", str(exc),
                )
            if not isinstance(data, dict):
                return CheckResult.fail(
                    service_name, "Source file is not a YAML mapping", str(path),
                )
            resolved = len(parse_gateway5_yaml(data).resolved)
            if resolved == 0:
                return CheckResult.skip(
                    service_name,
                    "No known Gateway5 variables found in source file",
                    str(path),
                )
            return CheckResult.ok(
                service_name, f"{resolved} Gateway5 variable(s) found in {path.name}",
            )

        try:
            result = self._transport.run_command("hostname")
            result.check()
            hostname = result.stdout.strip()

            # Check if any env vars are set
            test_result = self._transport.run_command(
                "printenv GATEWAY_LOG_LEVEL"
            )
            if test_result.ok and test_result.stdout.strip():
                return CheckResult.ok(
                    service_name,
                    f"Gateway5 env vars detected on {hostname}",
                )

            return CheckResult.skip(
                service_name,
                "No Gateway5 env vars detected",
                hostname,
            )
        except Exception as e:
            return CheckResult.fail(
                service_name,
                f"Preflight failed: {type(e).__name__}",
                str(e),
            )

    def _collect_from_env(self, collected: _CollectedVars) -> None:
        """Read all GATEWAY_* variables in a single SSH command"""
        try:
            result = self._transport.run_command("printenv")
            if not result.ok:
                logger.debug("Gateway5: printenv returned no results")
                return

            for line in result.stdout.splitlines():
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if not key.startswith("GATEWAY_"):
                    continue
                if key in _VAR_NAMES and value:
                    collected.set_if_missing(key, value, "environment")
        except Exception as exc:
            logger.debug("Gateway5: failed to read env - %s", exc)

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
