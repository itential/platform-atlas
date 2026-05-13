# pylint: disable=line-too-long
"""
Initial Setup Script for First-Time users to Atlas

The setup flow is split into two phases:
  1. Global Init — organization name, preferences, and settings that apply
     across all environments. Written to ~/.atlas/config.json.
  2. Environment Creation — connection details, topology, and credentials
     for one deployment target. Written to ~/.atlas/environments/<name>.json.

On first run, both phases execute back-to-back. Subsequent environments
can be added with ``platform-atlas env create``.
"""

import re
import sys
import logging
from typing import Any
from pathlib import Path

import questionary
from questionary import Style
from rich import box
from rich.rule import Rule
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.console import Console, Group
from rich.align import Align

from platform_atlas.core.paths import ATLAS_HOME, ATLAS_CONFIG_FILE, ATLAS_ENVIRONMENTS_DIR
from platform_atlas.core.utils import atomic_write_json
from platform_atlas.core.topology import (
    DeploymentMode, NodeRole, TargetNode, DeploymentTopology,
)
from platform_atlas.core.credentials import (
    credential_store,
    scoped_service_name,
    CredentialKey,
    CredentialStore,
    CredentialBackendType,
    VaultAuthMethod,
    VaultBackend,
    VaultConfig,
    verify_keyring_backend,
)
from platform_atlas.core.environment import (
    Environment,
    EnvironmentManager,
    get_environment_manager,
    validate_env_name,
)
from platform_atlas.core.exceptions import CredentialError
from platform_atlas.core import ui
from platform_atlas.core._version import __version__

theme = ui.theme
console = Console()

logger = logging.getLogger(__name__)

QSTYLE = Style(
    [
        ("qmark", f"fg:{theme.accent} bold"),
        ("question", "fg:#ffffff bold"),
        ("answer", f"fg:{theme.success_glow} bold"),
        ("pointer", f"fg:{theme.accent} bold"),
        ("highlighted", f"fg:#000000 bg:{theme.primary} bold"),
        ("selected", f"fg:{theme.success_glow} bold"),
        ("instruction", f"fg:{theme.text_muted} italic"),
        ("text", "fg:#888888"),
        ("disabled", "fg:#555555 italic"),
    ]
)


# =================================================
# Shared helpers
# =================================================

def must(v: str, msg: str):
    """Used with the setup process"""
    return True if v.strip() else msg

def mask(s: str, keep: int = 4) -> str:
    """Mask function for redacting the client secret"""
    s = s.strip()
    if len(s) <= keep:
        return "•" * len(s)
    return ("•" * (len(s) - keep)) + s[-keep:]

# Strict HTTP/HTTPS URL guard used for Platform / Gateway4 / Vault URLs.
# The previous "generic URI" regex was permissive enough to accept ``htttp://``,
# ``https:///``, and other typos that only surfaced as a confusing connection
# error during capture. We anchor on http(s) AND require a real hostname.
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _validate_http_url(value: str) -> bool | str:
    """Validator returning True or an error string for an HTTP/HTTPS URL.

    - Accepts only ``http://`` or ``https://`` schemes (case-insensitive).
    - Confirms there is a real hostname after the scheme.
    - Rejects whitespace inside the URL.
    """
    s = (value or "").strip()
    if not s:
        return "Required"
    if any(ch.isspace() for ch in s):
        return "URL cannot contain whitespace"
    if not _HTTP_URL_RE.match(s):
        return "Must start with http:// or https://"
    try:
        from urllib.parse import urlparse
        parsed = urlparse(s)
    except Exception as exc:
        return f"URL is malformed: {exc}"
    if parsed.scheme.lower() not in ("http", "https"):
        return "Must start with http:// or https://"
    if not parsed.hostname:
        return "URL is missing a hostname (expected http(s)://host[:port][/path])"
    return True


def ask_text(label: str, instruction: str = "", uri: bool = False) -> str:
    """Used when asking user for text entry.

    Ctrl+C raises KeyboardInterrupt so the top-level handler can roll back
    cleanly; previously these helpers swallowed cancellation into an empty
    string, which silently produced broken environments.

    When ``uri=True`` the input is validated as an HTTP/HTTPS URL — schemes
    like ``ftp://`` or typo-schemes like ``htttp://`` are rejected.
    """
    def _v(v: str):
        if not v.strip():
            return "Required"
        if uri:
            return _validate_http_url(v)
        return True
    result = questionary.text(label, instruction=instruction, validate=_v,
                              style=QSTYLE).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()

def ask_text_optional(label: str, instruction: str = "") -> str:
    """Text prompt that allows empty input.

    Empty submission returns ""; Ctrl+C still raises so the caller can
    distinguish "user accepted blank" from "user cancelled".
    """
    result = questionary.text(label, instruction=instruction,
                              style=QSTYLE).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()

def ask_secret(label: str) -> str:
    """Prompt for a secret value with masking. Ctrl+C raises KeyboardInterrupt."""
    result = questionary.password(label, validate=lambda v: must(v, "Required"),
                                  style=QSTYLE).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()

def ask_uri_optional(label: str, instruction: str = "") -> str:
    """URI prompt that allows empty input, but validates format if something is entered."""
    def _v(v: str) -> bool | str:
        v = v.strip()
        if not v:
            return True  # Empty is fine — it's optional
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$", v):
            return "Doesn't look like a URI (expected 'scheme://...')"
        return True

    result = questionary.text(label, instruction=instruction, validate=_v,
                              style=QSTYLE).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()

# ---------------------------------------------------------------------------
# Foundation helpers used across the wizard
# ---------------------------------------------------------------------------

def ask_text_with_default(
    label: str,
    default: str,
    instruction: str = "",
    uri: bool = False,
) -> str:
    """Prompt for text with a *real* default — pressing Enter returns ``default``.

    Distinct from :func:`ask_text` (which rejects empty input) so prompts
    that advertise a "(default: foo)" hint actually honor it instead of
    re-asking. When ``uri=True`` the entered value is validated as an
    HTTP/HTTPS URL. Ctrl+C raises KeyboardInterrupt.
    """
    def _v(v: str) -> bool | str:
        s = (v or "").strip()
        if not s:
            return True  # blank means "use the default"
        if uri:
            return _validate_http_url(s)
        return True

    inst = instruction or f"(default: {default}) "
    result = questionary.text(label, instruction=inst, validate=_v,
                              style=QSTYLE).ask()
    if result is None:
        raise KeyboardInterrupt
    value = result.strip()
    return value if value else default


def ask_scheme_uri_optional(
    label: str,
    schemes: tuple[str, ...],
    instruction: str = "",
) -> str:
    """Optional URI prompt that validates the scheme.

    ``schemes`` is a tuple of allowed scheme prefixes (e.g. ``("mongodb://",
    "mongodb+srv://")``). Empty input returns "". Ctrl+C raises
    KeyboardInterrupt. The user gets a precise error if they paste a URI
    with the wrong scheme — much better than a generic "URI invalid" hours
    later during capture.
    """
    expected = " or ".join(s.rstrip("://") + "://" for s in schemes)

    def _v(v: str) -> bool | str:
        s = (v or "").strip()
        if not s:
            return True
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$", s):
            return "Doesn't look like a URI (expected 'scheme://...')"
        if not any(s.startswith(p) for p in schemes):
            return f"Expected a {expected} URI"
        return True

    result = questionary.text(label, instruction=instruction, validate=_v,
                              style=QSTYLE).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()


def _probe_ssh_agent() -> tuple[bool, str]:
    """Probe the current SSH agent.

    Returns ``(loaded, detail)``: ``loaded=True`` means at least one identity
    is loaded; ``detail`` is a human-readable summary suitable for a hint
    line. Used to surface "you picked ssh-agent but your agent is empty"
    BEFORE the user finishes the wizard and the failure is opaque later.
    """
    import os as _os
    import subprocess

    sock = _os.environ.get("SSH_AUTH_SOCK", "")
    if not sock:
        return False, "SSH_AUTH_SOCK is not set — no agent is running."
    try:
        # ssh-add -l: exit 0 = identities present, 1 = none loaded, 2 = no agent.
        proc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True, text=True, timeout=3,
            check=False,
        )
    except FileNotFoundError:
        return False, "ssh-add not on PATH — cannot verify agent state."
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"ssh-add probe failed: {exc}"

    if proc.returncode == 0:
        # Count loaded keys (one per line)
        count = len([l for l in proc.stdout.splitlines() if l.strip()])
        return True, f"{count} identity(ies) loaded in ssh-agent."
    if proc.returncode == 1:
        return False, "Agent is running but no identities are loaded (try ssh-add ~/.ssh/<key>)."
    return False, "ssh-add reported no agent is reachable."


def _default_cm_socket(host: str = "") -> str:
    """Choose a sensible ControlMaster socket path.

    Prefers ``~/.atlas/sockets/`` over ``/tmp`` so the socket lives in the
    user's home (private by default) instead of a world-writable directory.
    Falls back to /tmp only if the Atlas home can't be created.
    """
    name = f"atlas-{host}.sock" if host else "atlas-cm.sock"
    try:
        sockets_dir = ATLAS_HOME / "sockets"
        sockets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(sockets_dir / name)
    except OSError:
        return f"/tmp/{name}"


def _test_platform_oauth(
    platform_uri: str,
    client_id: str,
    client_secret: str,
    verify_ssl: bool = True,
    timeout: int = 10,
) -> tuple[bool, str]:
    """Probe an OAuth client-credentials handshake against the Platform.

    Returns ``(ok, message)``. ``ok=True`` means authentication succeeded
    and a token was returned; ``message`` is a human-readable detail
    suitable for a wizard panel. Failures classify common cases
    (401/403/connection-refused) into specific guidance.

    Used by the init-setup wizard (UX1) to surface invalid OAuth creds
    while the user is still entering them — not 45 seconds later during
    capture.
    """
    if not (platform_uri and client_id and client_secret):
        return False, "Missing one of platform_uri, client_id, client_secret"

    try:
        from urllib.parse import urlparse as _urlparse
        from ipsdk import platform_factory  # type: ignore[import-untyped]
    except Exception as exc:
        return False, f"ipsdk not importable: {exc}"

    parsed = _urlparse(platform_uri)
    if not parsed.scheme or not parsed.hostname:
        return False, "Platform URL is malformed (need scheme://host)"

    try:
        client = platform_factory(
            host=parsed.hostname,
            port=parsed.port or 0,
            use_tls=(parsed.scheme == "https"),
            verify=verify_ssl,
            client_id=client_id,
            client_secret=client_secret,
            timeout=timeout,
        )
        client.authenticate_oauth()
    except Exception as exc:  # ipsdk wraps many transport errors
        msg = str(exc) or type(exc).__name__
        lowered = msg.lower()
        if "401" in msg or "invalid_client" in lowered or "unauthorized" in lowered:
            return False, "OAuth 401 — the client ID or secret is wrong"
        if "403" in msg or "forbidden" in lowered:
            return False, "OAuth 403 — the client lacks permission on this server"
        if "ssl" in lowered or "certificate" in lowered:
            return False, f"TLS error reaching {platform_uri}: {msg}"
        if "refused" in lowered or "timed out" in lowered or "name or service" in lowered:
            return False, f"Cannot reach {platform_uri}: {msg}"
        return False, msg

    return True, "OAuth handshake succeeded"


def _collect_and_verify_platform_oauth(
    platform_uri: str,
    platform_client_id: str,
    url_label: str = "Platform (IAP) URL",
    url_instruction: str = "(e.g. https://iap.acme.com) ",
    id_label: str = "Platform OAuth Client ID",
    secret_label: str = "Platform OAuth Client Secret",
) -> tuple[str, str, str, str]:
    """Prompt for the OAuth secret, then probe the handshake (UX1).

    Loops until the user either gets a successful handshake, picks "skip
    the test", or cancels. Allows them to re-enter just the secret, or
    re-enter URL+ID+secret, between attempts. Returns
    ``(platform_uri, platform_client_id, platform_client_secret, status)``
    where ``status`` is "ok" (handshake succeeded), "skipped" (user opted
    to save without verifying), or a short error string from the last
    attempt.
    """
    # Read verify_ssl from the global config if it's been written — Phase 1
    # of start_setup_process writes it before this code runs.
    _verify_ssl = True
    try:
        if ATLAS_CONFIG_FILE.is_file():
            import json as _json
            with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as _f:
                _verify_ssl = bool(_json.load(_f).get("verify_ssl", True))
    except Exception:
        pass

    last_detail = "not tested"
    while True:
        platform_client_secret = ask_secret(secret_label)
        console.print(f"  [{theme.text_dim}]Testing Platform OAuth handshake...[/{theme.text_dim}]")
        ok, detail = _test_platform_oauth(
            platform_uri=platform_uri,
            client_id=platform_client_id,
            client_secret=platform_client_secret,
            verify_ssl=_verify_ssl,
        )
        last_detail = detail
        if ok:
            console.print(f"  [{theme.success}]✓ {detail}[/{theme.success}]")
            return platform_uri, platform_client_id, platform_client_secret, "ok"

        console.print(f"  [{theme.error}]✘ {detail}[/{theme.error}]")
        choice = questionary.select(
            "How would you like to proceed?",
            choices=[
                questionary.Choice("Re-enter the client secret", value="secret"),
                questionary.Choice("Re-enter URL / client ID / secret", value="all"),
                questionary.Choice("Skip the test and save anyway (advanced)", value="skip"),
                questionary.Choice("Cancel setup", value="cancel"),
            ],
            style=QSTYLE,
        ).ask()
        if choice is None or choice == "cancel":
            _bail()
        if choice == "skip":
            return platform_uri, platform_client_id, platform_client_secret, f"skipped ({last_detail})"
        if choice == "all":
            platform_uri = ask_text(url_label, url_instruction, uri=True)
            platform_client_id = ask_text(id_label)
        # "secret" — loop iterates and re-prompts the secret


def _render_post_init_checklist(
    *,
    env: Environment,
    backend_label: str,
    checks: list[tuple[str, bool | None, str, str]],
    next_command: str = "platform-atlas session create <session-name>",
    next_hint: str = "A session binds your environment, ruleset, and profile. After creating one, run `platform-atlas session run all` to capture, validate, and report.",
) -> None:
    """Render the end-of-setup checklist + next-step panel (UX4).

    ``checks`` is a list of ``(label, status, detail, suggestion)`` tuples.
    ``status`` is True (OK), False (failed), or None (skipped / not run).
    ``detail`` is the muted right-hand-side text. ``suggestion`` (optional)
    is rendered below the row when status is False.
    """
    lines: list[Text] = []
    for label, status, detail, suggestion in checks:
        if status is True:
            badge = f"[{theme.success}]✓[/{theme.success}]"
        elif status is False:
            badge = f"[{theme.error}]✘[/{theme.error}]"
        else:
            badge = f"[{theme.text_dim}]·[/{theme.text_dim}]"
        line = Text.from_markup(
            f"  {badge}  [bold]{label:<24}[/bold] [dim]{detail}[/dim]"
        )
        lines.append(line)
        if status is False and suggestion:
            lines.append(Text.from_markup(f"     [dim]→ {suggestion}[/dim]"))

    body: list[Any] = [
        Text.from_markup(
            f"[bold {theme.success_glow}]Setup complete[/bold {theme.success_glow}]  "
            f"[dim]— here's what just happened:[/dim]"
        ),
        Text(""),
        *lines,
        Text(""),
        Text.from_markup(f"[bold {theme.orange if hasattr(theme, 'orange') else theme.accent}]▶ Next step[/]"),
        Text.from_markup(f"  [dim]$[/dim] [bold]{next_command}[/bold]"),
    ]
    if next_hint:
        body.append(Text.from_markup(f"  [dim]{next_hint}[/dim]"))
    body.extend([
        Text(""),
        Text.from_markup("[dim]Useful commands:[/dim]"),
        Text.from_markup("  [dim]- platform-atlas env list        → see all environments[/dim]"),
        Text.from_markup("  [dim]- platform-atlas config doctor   → run a health check[/dim]"),
        Text.from_markup("  [dim]- platform-atlas guide           → open the user guide[/dim]"),
    ])

    console.print(Panel(
        Group(*body),
        title=f"Atlas — environment {env.name!r} ready",
        subtitle=f"[dim]{backend_label}[/dim]",
        box=box.ROUNDED,
        border_style=theme.success,
        expand=False,
    ))


def _validate_yaml_file(path_str: str) -> bool | str:
    """Validator for ``questionary.path`` that confirms the file parses as YAML.

    Used for IAP / IAG5 values.yaml selection in the K8s wizard. Returns
    ``True`` on success or an error string for the prompt validator API.
    """
    from pathlib import Path as _P
    s = (path_str or "").strip()
    if not s:
        return "File not found — enter the full path to your values.yaml"
    p = _P(s).expanduser()
    if not p.is_file():
        return f"File not found: {p}"
    try:
        import yaml  # PyYAML — dep of the WebUI side, but bundled
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except ImportError:
        # PyYAML not installed — skip content validation; existence is enough.
        return True
    except yaml.YAMLError as e:  # type: ignore[attr-defined]
        return f"YAML parse error: {e}"
    except OSError as e:
        return f"Cannot read file: {e}"
    if data is None:
        return "File is empty"
    if not isinstance(data, dict):
        return "values.yaml should be a YAML mapping at the top level"
    return True


def ask_vault_settings() -> VaultConfig:
    """Interactive wizard for HashiCorp Vault connection settings."""
    _section("Vault Configuration", "Connection details for your Vault server")

    url = ask_text("Vault URL", instruction="(e.g. https://vault.company.com:8200) ", uri=True)

    auth_method = questionary.select(
        "Authentication method",
        choices=[
            questionary.Separator("── Standard ─────────────────────────────────────────────"),
            questionary.Choice("Token                — Static token stored in keyring",               value="token"),
            questionary.Choice("AppRole              — role_id + static secret_id",                   value="approle"),
            questionary.Separator("── Automated / rotating credentials ─────────────────────"),
            questionary.Choice("AppRole (Wrapped)    — role_id + response-wrapped secret_id",         value="approle_wrapped"),
            questionary.Choice("Token (file)         — Token maintained by Vault Agent on this host", value="token_file"),
            questionary.Choice("Token (env)          — VAULT_TOKEN injected by pipeline/orchestrator", value="token_env"),
        ],
        style=QSTYLE,
    ).ask()
    if auth_method is None:
        _bail()

    token = role_id = secret_id = wrapping_token = token_file_path = None

    if auth_method == "token":
        token = ask_secret("Vault Token (hidden)")

    elif auth_method == "approle":
        role_id = ask_secret("AppRole Role ID (hidden)")
        secret_id = ask_secret("AppRole Secret ID (hidden)")

    elif auth_method == "approle_wrapped":
        _hint(
            "A wrapping token is a one-time-use token issued by your pipeline or Vault admin.\n"
            "  It unwraps to the AppRole secret_id and is consumed on first use.\n"
            "  You will need to update this token each time it expires or is used."
        )
        role_id = ask_secret("AppRole Role ID (hidden)")
        wrapping_token = ask_secret("Wrapping Token (hidden)")

    elif auth_method == "token_file":
        _hint(
            "Vault Agent runs as a service on this host and writes a continuously-renewed\n"
            "  token to a file. Atlas reads that file at runtime — no credentials to manage."
        )
        token_file_path = ask_text(
            "Token sink file path",
            instruction="(e.g. /run/vault-agent/atlas.token or ~/.atlas/vault-token) ",
        )

    else:  # token_env
        import os as _os
        console.print(
            f"\n  [{theme.text_dim}]Atlas reads VAULT_TOKEN from the environment at runtime and never stores it.\n"
            f"  Enter the token now so the connection test below can run.[/{theme.text_dim}]\n"
        )
        _token_for_test = ask_secret("Vault Token (hidden — for connection test only, not stored)")
        _os.environ["VAULT_TOKEN"] = _token_for_test
        console.print()
        console.print(Panel(
            f"[bold {theme.primary_glow}]VAULT_TOKEN set for this session[/bold {theme.primary_glow}]\n\n"
            f"[{theme.text_primary}]Atlas has temporarily set VAULT_TOKEN in the current process so the\n"
            f"connection test can run. It is [bold]never stored[/bold] anywhere by Atlas.\n\n"
            f"You must set it yourself before every Atlas run.[/{theme.text_primary}]\n\n"
            f"[{theme.text_dim}]Options:\n\n"
            f"  Shell profile [dim](persists across logins)[/dim]\n"
            f"    [{theme.accent}]export VAULT_TOKEN=<your-token>[/{theme.accent}]\n"
            f"    Add to ~/.bashrc or ~/.zshrc, then: source ~/.bashrc\n\n"
            f"  One-off run\n"
            f"    [{theme.accent}]VAULT_TOKEN=<your-token> platform-atlas session run all[/{theme.accent}]\n\n"
            f"  systemd service ([Service] block)\n"
            f"    [{theme.accent}]Environment=VAULT_TOKEN=<your-token>[/{theme.accent}][/{theme.text_dim}]",
            box=box.ROUNDED,
            border_style=theme.border_primary,
            expand=False,
        ))

    mount_point = ask_text_optional("KV v2 mount point", instruction="(default: secret) ") or "secret"
    secret_path = ask_text_optional("Secret path", instruction="(default: platform-atlas) ") or "platform-atlas"

    verify_ssl = questionary.confirm(
        "Verify Vault SSL certificate?",
        default=True,
        style=QSTYLE,
    ).ask()

    namespace = ask_text_optional("Vault namespace", instruction="(Enterprise only, leave blank if N/A) ")

    return VaultConfig(
        url=url,
        auth_method=VaultAuthMethod(auth_method),
        token=token,
        role_id=role_id,
        secret_id=secret_id,
        wrapping_token=wrapping_token,
        token_file_path=token_file_path,
        mount_point=mount_point,
        secret_path=secret_path,
        verify_ssl=bool(verify_ssl),
        namespace=namespace or None,
    )

# Sentinel marker for "default cancellation" — when _bail() is called with
# no custom message, we suppress the inline print so the top-level handler
# emits the single canonical "Setup interrupted. No changes saved." message
# (M1: consolidate cancellation output). Informational messages from
# specific call sites — e.g. "Cannot continue without a working Vault
# connection." — still print because they convey something the top-level
# handler can't know.
_DEFAULT_BAIL_MSG = "Canceled. No changes made."


def _bail(msg: str = _DEFAULT_BAIL_MSG) -> None:
    """Raise KeyboardInterrupt so the wrapping CLI handler can clean up.

    With ``msg`` set to the default, prints nothing — the top-level handler
    is the single source of truth for the cancellation message. Pass a
    custom message when the user benefits from a specific reason (e.g. a
    Vault connection failure that they need to fix before retrying).
    """
    if msg and msg != _DEFAULT_BAIL_MSG:
        console.print(f"\n[{theme.warning}]{msg}[/{theme.warning}]")
    raise KeyboardInterrupt

def _section(title: str, subtitle: str = "") -> None:
    """Print a styled section header"""
    header = f"[bold {theme.primary_glow}]{title}[/bold {theme.primary_glow}]"
    if subtitle:
        header += f"\n[{theme.text_dim}]{subtitle}[/{theme.text_dim}]"
    console.print()
    console.print(Panel(header, box=box.ROUNDED, border_style=theme.border_primary, expand=False))

def _hint(text: str) -> None:
    """Print a subtle hint line"""
    console.print(f"  [{theme.text_dim}]{text}[/{theme.text_dim}]")


# =================================================
# Deployment Topology Wizard
# =================================================

_MODE_CHOICES = [
    questionary.Choice(
        title="Standalone (All-in-One)  — IAP, Mongo, Redis on a single server",
        value="standalone_all",
    ),
    questionary.Choice(
        title="Standalone (Split)       — IAP, Mongo, Redis on separate servers",
        value="standalone_split",
    ),
    questionary.Choice(
        title="Highly Available (HA2)    — Redundant IAP, Mongo replica set, Redis sentinels",
        value="ha2",
    ),
    questionary.Choice(
        title="Custom                    — I'll define each node manually",
        value="custom",
    ),
]


def _ask_host(label: str, instruction: str = "") -> str:
    """Prompt for a hostname/IP with basic validation. Ctrl+C raises KeyboardInterrupt."""
    def _v(v: str):
        v = v.strip()
        if not v:
            return "Required — enter a hostname or IP address"
        if v.startswith("http"):
            return "Enter a hostname or IP, not a URL (e.g. 10.0.0.1 or iap-prod-01)"
        # Reject internal whitespace and path traversal tokens
        if any(c.isspace() for c in v):
            return "Hostnames cannot contain spaces"
        if ".." in v:
            return "Hostnames cannot contain '..'"
        return True

    inst = instruction or "(hostname or IP) "
    result = questionary.text(label, instruction=inst, validate=_v,
                              style=QSTYLE).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()


def _ask_ssh_user(default: str = "atlas") -> str:
    """Prompt for SSH username with a default. Ctrl+C raises KeyboardInterrupt."""
    result = questionary.text(
        "SSH username for these servers",
        instruction=f"(default: {default}) ",
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip() or default

def _discover_ssh_keys(search_dir: Path | None = None) -> list[Path]:
    """Scan ~/.ssh/ for likely SSH key files"""
    search_dir = search_dir or Path.home() / ".ssh"
    if not search_dir.is_dir():
        return []

    skip_prefixes = ("known_", "authorized_", "config")
    skip_suffixes = (".pub", ".old", ".bak")

    return [
        f for f in sorted(search_dir.iterdir())
        if f.is_file()
        and not f.name.startswith(skip_prefixes)
        and not f.suffix in skip_suffixes
    ]

def _ask_ssh_key() -> str:
    """Prompt for an SSH key - offers discovered keys if available.

    Manual path entry is validated: the file must exist, be readable, and
    not end in ``.pub`` (a common mistake — users pick the public key
    instead of the private key and only see cryptic SSH errors later).
    Returns "" for "use ssh-agent". Ctrl+C raises KeyboardInterrupt.
    """
    def _validate_key_path(v: str) -> bool | str:
        s = (v or "").strip()
        if not s:
            return "Required — enter a path or Ctrl+C to cancel"
        path = Path(s).expanduser()
        if not path.exists():
            return f"File not found: {path}"
        if not path.is_file():
            return "Path exists but is not a regular file"
        if path.suffix == ".pub":
            return "That's a public key (.pub). Pick the matching private key file."
        try:
            with open(path, "rb"):
                pass
        except PermissionError:
            return "Permission denied — Atlas cannot read this file"
        except OSError as e:
            return f"Cannot read file: {e}"
        return True

    keys = _discover_ssh_keys()

    # M12: if the default ~/.ssh is empty, offer to scan a user-chosen
    # directory before falling back to manual entry. Enterprise users often
    # keep keys in /etc/atlas/keys, ~/keys, or a mounted secret directory.
    if not keys:
        ask_alt = questionary.confirm(
            "No SSH keys found in ~/.ssh/ — scan a different directory?",
            default=False,
            style=QSTYLE,
        ).ask()
        if ask_alt is None:
            raise KeyboardInterrupt
        if ask_alt:
            alt = questionary.path(
                "Directory to scan",
                only_directories=True,
                validate=lambda v: (
                    True if v and Path(v).expanduser().is_dir()
                    else "Not a directory"
                ),
                style=QSTYLE,
            ).ask()
            if alt is None:
                raise KeyboardInterrupt
            keys = _discover_ssh_keys(Path(alt.strip()).expanduser())
            if not keys:
                _hint(f"Nothing key-shaped found under {alt} — falling back to manual entry.")

    if keys:
        choices = [
            questionary.Choice(
                title=f"{k.name:24s}    ({k})",
                value=str(k),
            )
            for k in keys
        ]
        choices.append(questionary.Choice(
            title="Enter a path manually...",
            value="__manual__",
        ))
        choices.append(questionary.Choice(
            title="Skip - use ssh-agent instead",
            value="",
        ))

        result = questionary.select(
            "SSH private key",
            choices=choices,
            style=QSTYLE,
        ).ask()
        if result is None:
            raise KeyboardInterrupt
        if result == "":
            # User picked "Skip - use ssh-agent". Probe the agent so we
            # don't accept a configuration that will silently fail at
            # capture time with cryptic paramiko errors (L1 / UX6).
            loaded, detail = _probe_ssh_agent()
            if loaded:
                _hint(f"ssh-agent OK — {detail}")
                return ""
            console.print(
                f"  [{theme.warning}]⚠ ssh-agent unusable: {detail}[/{theme.warning}]"
            )
            choice = questionary.select(
                "What would you like to do?",
                choices=[
                    questionary.Choice(
                        "Pick a key file instead",
                        value="pick_file",
                    ),
                    questionary.Choice(
                        "Continue anyway (capture will fail until the agent has a key)",
                        value="continue",
                    ),
                ],
                default="pick_file",
                style=QSTYLE,
            ).ask()
            if choice is None:
                raise KeyboardInterrupt
            if choice == "continue":
                return ""
            # fall through to manual path entry
        elif result != "__manual__":
            return result

    # Fallback: tab-completing path prompt with file validation
    result = questionary.path(
        "SSH private key path",
        default=str(Path.home() / ".ssh") + "/",
        only_directories=False,
        validate=_validate_key_path,
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()


def _ask_ssh_key_passphrase(ssh_key: str) -> str:
    """Prompt for the SSH key passphrase when an explicit key is set.

    Empty input means "key is unencrypted" — that's a valid response.
    Ctrl+C raises KeyboardInterrupt so cancellation isn't confused with
    "no passphrase".
    """
    if not ssh_key:
        return ""

    result = questionary.password(
        "SSH key passphrase",
        instruction="(leave blank if key is not encrypted) ",
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()

def _ask_ssh_port(default: int = 22) -> int:
    """Prompt for SSH port with a default. Ctrl+C raises KeyboardInterrupt."""
    result = questionary.text(
        "SSH port",
        instruction=f"(default: {default}) ",
        validate=lambda v: True if not v.strip() else (
            "Enter a valid port number (1-65535)"
            if not v.strip().isdigit() or not 1 <= int(v.strip()) <= 65535
            else True
        ),
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return int(result.strip()) if result.strip() else default

def _ask_ssh_discover_keys(ssh_key: str) -> bool:
    """Ask whether to auto-discover keys from ~/.ssh/ when no explicit key is set."""
    if ssh_key:
        return False

    result = questionary.confirm(
        "Search ~/.ssh/ for keys automatically?",
        instruction="(if no, only the ssh-agent will be used) ",
        default=False,
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return bool(result)

def _ask_ssh_host_key_policy() -> str:
    """Ask how to handle unknown SSH host keys. Ctrl+C raises KeyboardInterrupt."""
    result = questionary.select(
        "Unknown SSH host key handling",
        choices=[
            questionary.Choice(
                "Auto-add       - Trust on first connect (recommended)",
                value="auto_add",
            ),
            questionary.Choice(
                "Warn           - Connect but log a warning",
                value="warn",
            ),
            questionary.Choice(
                "Reject         - Fail if host not in known_hosts",
                value="reject",
            ),
        ],
        default="auto_add",
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _ask_node_transport(target_label: str = "the Platform (IAP) server") -> str:
    """Ask how Atlas should connect to a server.

    Returns "ssh", "control_master", or "local".
    SSH is the default and recommended option for most deployments. The
    function is generic — it works for any role (IAP, MongoDB, Redis,
    IAG) — but defaults to the Platform label for the standalone-all and
    HA2 wizards that historically used it under the name
    ``_ask_iap_transport``.
    """
    result = questionary.select(
        f"How should Atlas connect to {target_label}?",
        choices=[
            questionary.Choice(
                "SSH (recommended)  — Key or agent-based direct SSH",
                value="ssh",
            ),
            questionary.Choice(
                "ControlMaster      — Multiplex through a pre-opened SSH session "
                "(use for CyberArk PSMP or jump hosts without direct key access)",
                value="control_master",
            ),
            questionary.Choice(
                "Local              — Atlas CLI is installed on the server itself",
                value="local",
            ),
        ],
        default="ssh",
        style=QSTYLE,
    ).ask()
    if result is None:
        _bail()
    if result == "local":
        _hint(
            "Local transport selected. Atlas will collect system and config data\n"
            "  directly from this machine's filesystem instead of over SSH.\n"
            "  Ensure Atlas is run on the Platform server for this environment."
        )
    return result or "ssh"


# Backward-compatible alias — some external callers / tests may still
# reference the old name; remove in a future release.
_ask_iap_transport = _ask_node_transport


def _ask_control_master_settings(host: str = "") -> tuple[str, str, int]:
    """Collect ControlMaster socket path, SSH destination, and port for one node.

    Returns (socket_path, ssh_target, port).
    """
    default_socket = _default_cm_socket(host)
    target_example = (
        f"user@{host}@psmp-host.example.com"
        if host
        else "user@target-ip@psmp-host.example.com"
    )

    # Render the multi-line ssh command as a Panel so the help block stays
    # legible on narrow terminals instead of wrapping mid-flag (M3).
    cm_example = (
        f"ssh -M -S {default_socket} \\\n"
        f"    -o ControlPersist=10m \\\n"
        f"    -o StrictHostKeyChecking=no \\\n"
        f"    -o UserKnownHostsFile=/dev/null \\\n"
        f"    -fN {target_example}"
    )
    console.print(Panel(
        Group(
            Text("Before running Atlas, open the ControlMaster session for this node:",
                 style=theme.text_dim),
            Text(""),
            Text(cm_example, style=f"{theme.text_primary}"),
        ),
        title="ControlMaster session setup",
        border_style=theme.border_primary,
        box=box.ROUNDED,
        expand=False,
    ))

    socket_path = questionary.text(
        "Socket path",
        instruction=f"(default: {default_socket}) ",
        style=QSTYLE,
    ).ask()
    if socket_path is None:
        _bail()
    socket_path = socket_path.strip() or default_socket

    ssh_target = questionary.text(
        "SSH destination",
        instruction=f"(e.g. {target_example}) ",
        style=QSTYLE,
    ).ask()
    if ssh_target is None:
        _bail()
    ssh_target = ssh_target.strip()
    if not ssh_target:
        _bail("SSH destination is required for ControlMaster transport.")

    ssh_port = _ask_ssh_port()

    return socket_path, ssh_target, ssh_port

def _ask_node_count(label: str, minimum: int, default: int) -> int:
    """Ask how many nodes of a type, with a minimum.

    Ctrl+C raises KeyboardInterrupt. An upper bound of 100 catches typos
    like 99999 that would otherwise spawn thousands of prompts.
    """
    _MAX = 100

    def _v(v: str):
        v = v.strip()
        if not v:
            return True  # will use default
        try:
            n = int(v)
        except ValueError:
            return "Enter a number"
        if n < minimum:
            return f"Minimum is {minimum}"
        if n > _MAX:
            return f"Maximum is {_MAX} (got {n} — typo?)"
        return True

    result = questionary.text(
        label,
        instruction=f"(min: {minimum}, default: {default}) ",
        validate=_v,
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return int(result.strip()) if result.strip() else default


def _ask_mongo_count(label: str, minimum: int = 3, default: int = 3) -> int:
    """Ask for the Mongo replica-set size, warning once if even.

    Behaviour:
      1. Prompt for the count.
      2. If even, warn that odd is preferred for election health and ask
         "continue with even?".
      3. If "no", re-prompt; on the second pass we silently accept
         whatever the user enters (they've been warned).

    M7: the previous logic kept warning + re-prompting in a way that gave
    no feedback if the user entered even a second time. This helper makes
    the contract explicit — one warning, then trust the user.
    """
    count = _ask_node_count(label, minimum=minimum, default=default)
    if count % 2 != 0:
        return count

    console.print(
        f"  [{theme.warning}]⚠ Even number of Mongo nodes ({count}) — odd is "
        f"recommended for healthy elections[/{theme.warning}]"
    )
    keep_even = questionary.confirm(
        "  Continue with even count?",
        default=False,
        style=QSTYLE,
    ).ask()
    if keep_even is None:
        raise KeyboardInterrupt
    if keep_even:
        return count

    _hint("(re-prompting; the next value is accepted as-is)")
    return _ask_node_count(label, minimum=minimum, default=default)


def _ask_hosts_for_role(role_label: str, count: int) -> list[str]:
    """Collect hostnames for N nodes of a given role"""
    hosts: list[str] = []
    for i in range(1, count + 1):
        host = _ask_host(f"  {role_label} #{i}")
        hosts.append(host)
    return hosts


def _ask_host_with_reuse(
    label: str,
    reuse_options: list[tuple[str, str]],
) -> str:
    """Prompt for a hostname offering "same as" shortcuts (UX7).

    ``reuse_options`` is a list of ``(role_label, hostname)`` pairs to
    surface as quick-select choices. Choosing one returns that hostname
    verbatim; choosing "Enter a different host" falls back to the standard
    ``_ask_host`` prompt. Ctrl+C raises KeyboardInterrupt.
    """
    if not reuse_options:
        return _ask_host(label)

    choices: list[questionary.Choice] = []
    for role_label, hostname in reuse_options:
        choices.append(questionary.Choice(
            title=f"Same as {role_label} ({hostname})",
            value=hostname,
        ))
    choices.append(questionary.Choice(
        title="Enter a different host...",
        value="__different__",
    ))

    result = questionary.select(
        label,
        choices=choices,
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    if result != "__different__":
        return result
    return _ask_host(label)

def _ask_gateway_version() -> str | None:
    """Ask which Automation Gateway version is deployed"""
    add_gw = questionary.confirm(
        "Do you have any Automation Gateway (IAG) servers?",
        default=False,
        style=QSTYLE,
    ).ask()
    if not add_gw:
        return None

    version = questionary.select(
        "Which gateway version?",
        choices=[
            questionary.Choice("Gateway 4 (Python / venv-based)", value="gateway4"),
            questionary.Choice("Gateway 5 (Container / env-var-based)", value="gateway5"),
        ],
        style=QSTYLE,
    ).ask()
    return version

def _ask_gateway_nodes(common_ssh: dict) -> list[TargetNode]:
    """Ask about gateway servers and return configured TargetNodes"""
    gw_version = _ask_gateway_version()
    if gw_version is None:
        return []

    count = _ask_node_count("How many gateway servers", minimum=1, default=1)
    hosts = _ask_hosts_for_role("Gateway", count)

    gw_modules = ["system", gw_version, "filesystem"]
    nodes: list[TargetNode] = []
    for i, host in enumerate(hosts, 1):
        nodes.append(TargetNode(
            role=NodeRole.IAG,
            host=host,
            label=f"iag-{i:02d}",
            modules=gw_modules,
            **common_ssh,
        ))

    return nodes

def _ask_capture_scope() -> str:
    """Ask user to select capture scope for HA/multi-node deployments.

    Ctrl+C raises KeyboardInterrupt — silently returning the default
    would treat cancellation as "yes, primary_only is fine", which
    isn't what the user meant by hitting Ctrl+C.
    """
    result = questionary.select(
        "Capture scope",
        choices=[
            questionary.Choice(
                "Primary only  — Connect to 1 node per role (recommended)",
                value="primary_only",
            ),
            questionary.Choice(
                "All nodes     — Connect to every node in the topology",
                value="all_nodes",
            ),
        ],
        default="primary_only",
        style=QSTYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _build_ssh_defaults(topology: DeploymentTopology) -> dict[str, Any]:
    """
    Extract shared SSH settings from the first SSH node to store as
    ssh_defaults in the config. Local and Kubernetes nodes are skipped.
    Nodes that differ keep their own values via per-node overrides.
    """
    if not topology.nodes:
        return {}

    first = next((n for n in topology.nodes if n.transport == "ssh"), None)
    if first is None:
        return {}

    defaults: dict[str, Any] = {"username": first.ssh_user}

    if first.ssh_key:
        defaults["key_path"] = first.ssh_key
    if first.ssh_key_passphrase:
        defaults["key_passphrase"] = first.ssh_key_passphrase
    if first.ssh_port != 22:
        defaults["port"] = first.ssh_port
    if first.ssh_discover_keys:
        defaults["discover_keys"] = True
    if first.ssh_host_key_policy != "auto_add":
        defaults["host_key_policy"] = first.ssh_host_key_policy

    return defaults


def _display_topology_review(
    topology: DeploymentTopology,
    capture_scope: str = "primary_only",
) -> None:
    """Display the configured topology.

    Renders the main role/host/transport table, and — when at least one node
    uses ControlMaster — a second table showing the per-node socket path and
    SSH destination so the user can verify exactly what they typed before
    saving (M4 / UX10).
    """
    table = Table(
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("Node", style=f"bold {theme.text_primary}", min_width=16)
    table.add_column("Role", style=theme.accent, min_width=8)
    table.add_column("Host", style=theme.secondary, min_width=16)
    table.add_column("Transport", min_width=8)
    table.add_column("Primary", justify="center", min_width=8)
    table.add_column("Modules", style=theme.text_dim, min_width=24)

    has_cm_node = False
    for node in topology.nodes:
        is_captured = (
            capture_scope == "all_nodes"
            or node.primary
        )
        primary_badge = (
            f"[{theme.success}]✓[/{theme.success}]"
            if node.primary
            else f"[{theme.text_dim}]·[/{theme.text_dim}]"
        )
        modules_str = (
            ", ".join(node.effective_modules)
            if is_captured
            else f"[{theme.text_dim}]— (topology only)[/{theme.text_dim}]"
        )
        transport_label = node.transport or "ssh"
        if transport_label == "local":
            transport_badge = f"[bold {theme.warning}]LOCAL[/bold {theme.warning}]"
        elif transport_label == "kubernetes":
            transport_badge = f"[{theme.accent}]K8S[/{theme.accent}]"
        elif transport_label == "control_master":
            transport_badge = f"[{theme.info}]CM[/{theme.info}]"
            has_cm_node = True
        else:
            transport_badge = f"[{theme.text_dim}]SSH[/{theme.text_dim}]"
        table.add_row(
            node.label,
            node.role.value.upper(),
            node.host,
            transport_badge,
            primary_badge,
            modules_str,
        )

    scope_label = (
        f"[{theme.info}]primary_only[/{theme.info}] — "
        "connecting to 1 node per role"
        if capture_scope == "primary_only"
        else f"[{theme.warning}]all_nodes[/{theme.warning}] — "
        "connecting to every node"
    )

    # Build the optional ControlMaster sub-table — same nodes, but with the
    # socket / SSH destination the user needs to verify. We render it as a
    # SECOND table inside the same panel so the main view stays scannable.
    cm_section: list[Any] = []
    if has_cm_node:
        cm_table = Table(
            box=box.SIMPLE,
            show_lines=False,
            pad_edge=True,
            title="ControlMaster sessions",
            title_style=f"bold {theme.info}",
        )
        cm_table.add_column("Node", style=f"bold {theme.text_primary}", min_width=14)
        cm_table.add_column("Port", justify="right", min_width=4)
        cm_table.add_column("Socket", style=theme.text_dim, min_width=24, overflow="fold")
        cm_table.add_column("SSH destination", style=theme.secondary, min_width=24, overflow="fold")
        for node in topology.nodes:
            if (node.transport or "") != "control_master":
                continue
            cm_table.add_row(
                node.label,
                str(node.ssh_port),
                node.ssh_control_socket or f"[{theme.warning}](unset)[/{theme.warning}]",
                node.ssh_control_target or f"[{theme.warning}](unset)[/{theme.warning}]",
            )
        cm_section = [Text(""), cm_table, Text(
            "Open each ControlMaster session before running Atlas:",
            style=theme.text_dim,
        ), Text(
            "  ssh -M -S <socket> -o ControlPersist=10m -fN <destination>",
            style=theme.text_muted,
        )]

    console.print(Panel(
        Group(
            Text(f" {topology.summary}\n", style=f"bold {theme.primary_glow}"),
            Text.from_markup(f" Capture scope: {scope_label}\n"),
            table,
            *cm_section,
        ),
        title="Deployment Topology",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False,
    ))


def _wizard_standalone_all() -> DeploymentTopology:
    """Guide: single server with everything"""
    _hint("All services (IAP, MongoDB, Redis) on one server.")
    _hint("pymongo/redis-py/OAuth handle service-specific data collection.\n")

    host = _ask_host("Server hostname")
    iap_transport = _ask_node_transport()

    gw_version = _ask_gateway_version()
    base_modules = ["system", "filesystem", "mongo", "redis", "platform"]
    if gw_version:
        base_modules.append(gw_version)

    if iap_transport == "local":
        nodes = [TargetNode(
            role=NodeRole.ALL, host=host,
            transport="local", modules=base_modules,
        )]
    elif iap_transport == "control_master":
        cm_socket, cm_target, cm_port = _ask_control_master_settings(host)
        nodes = [TargetNode(
            role=NodeRole.ALL, host=host,
            transport="control_master",
            ssh_port=cm_port,
            ssh_control_socket=cm_socket,
            ssh_control_target=cm_target,
            modules=base_modules,
        )]
    else:
        ssh_user = _ask_ssh_user()
        ssh_key = _ask_ssh_key()
        ssh_key_passphrase = _ask_ssh_key_passphrase(ssh_key)
        ssh_port = _ask_ssh_port()
        ssh_discover_keys = _ask_ssh_discover_keys(ssh_key)
        ssh_host_key_policy = _ask_ssh_host_key_policy()
        common = {
            "ssh_user": ssh_user, "ssh_key": ssh_key,
            "ssh_key_passphrase": ssh_key_passphrase,
            "ssh_port": ssh_port, "ssh_discover_keys": ssh_discover_keys,
            "ssh_host_key_policy": ssh_host_key_policy,
        }
        nodes = [TargetNode(
            role=NodeRole.ALL, host=host,
            modules=base_modules, **common,
        )]

    return DeploymentTopology(mode=DeploymentMode.STANDALONE, nodes=nodes)


def _wizard_standalone_split() -> DeploymentTopology:
    """Guide: separate servers for IAP, Mongo, Redis"""
    _hint("IAP, MongoDB, and Redis each on their own server.")
    _hint("Enter the hostname or IP for each.\n")

    iap_transport = _ask_node_transport()
    console.print()

    if iap_transport == "control_master":
        _hint(
            "Each server requires its own pre-opened ControlMaster session.\n"
            "  Open them before running Atlas — one per target node."
        )
        iap_host = _ask_host("IAP server")
        mongo_host = _ask_host_with_reuse(
            "MongoDB server",
            reuse_options=[("IAP", iap_host)],
        )
        redis_host = _ask_host_with_reuse(
            "Redis server",
            reuse_options=[
                ("IAP", iap_host),
                *([("MongoDB", mongo_host)] if mongo_host != iap_host else []),
            ],
        )

        console.print(f"\n  [{theme.primary_glow}]── IAP ({iap_host}) ──[/{theme.primary_glow}]")
        iap_sock, iap_tgt, iap_cm_port = _ask_control_master_settings(iap_host)

        console.print(f"\n  [{theme.primary_glow}]── MongoDB ({mongo_host}) ──[/{theme.primary_glow}]")
        mongo_sock, mongo_tgt, mongo_cm_port = _ask_control_master_settings(mongo_host)

        console.print(f"\n  [{theme.primary_glow}]── Redis ({redis_host}) ──[/{theme.primary_glow}]")
        redis_sock, redis_tgt, redis_cm_port = _ask_control_master_settings(redis_host)

        nodes: list[TargetNode] = [
            TargetNode(role=NodeRole.IAP, host=iap_host,
                       transport="control_master",
                       ssh_port=iap_cm_port,
                       ssh_control_socket=iap_sock, ssh_control_target=iap_tgt),
            TargetNode(role=NodeRole.MONGO, host=mongo_host,
                       transport="control_master",
                       ssh_port=mongo_cm_port,
                       ssh_control_socket=mongo_sock, ssh_control_target=mongo_tgt),
            TargetNode(role=NodeRole.REDIS, host=redis_host,
                       transport="control_master",
                       ssh_port=redis_cm_port,
                       ssh_control_socket=redis_sock, ssh_control_target=redis_tgt),
        ]

        gw_version = _ask_gateway_version()
        if gw_version:
            gw_count = _ask_node_count("How many gateway servers", minimum=1, default=1)
            gw_hosts = _ask_hosts_for_role("Gateway", gw_count)
            gw_modules = ["system", gw_version, "filesystem"]
            for i, gw_host in enumerate(gw_hosts, 1):
                console.print(
                    f"\n  [{theme.primary_glow}]── Gateway ({gw_host}) ──[/{theme.primary_glow}]"
                )
                gw_sock, gw_tgt, gw_cm_port = _ask_control_master_settings(gw_host)
                nodes.append(TargetNode(
                    role=NodeRole.IAG, host=gw_host, label=f"iag-{i:02d}",
                    transport="control_master", modules=gw_modules,
                    ssh_port=gw_cm_port,
                    ssh_control_socket=gw_sock, ssh_control_target=gw_tgt,
                ))

        return DeploymentTopology(mode=DeploymentMode.STANDALONE, nodes=nodes)

    if iap_transport == "local":
        _hint("SSH credentials below apply to MongoDB and Redis servers.")
    _hint("SSH credentials will be shared across all SSH-connected servers.\n")

    ssh_user = _ask_ssh_user()
    ssh_key = _ask_ssh_key()
    ssh_key_passphrase = _ask_ssh_key_passphrase(ssh_key)
    ssh_port = _ask_ssh_port()
    ssh_discover_keys = _ask_ssh_discover_keys(ssh_key)
    ssh_host_key_policy = _ask_ssh_host_key_policy()
    console.print()

    common = {"ssh_user": ssh_user, "ssh_key": ssh_key,
              "ssh_key_passphrase": ssh_key_passphrase,
              "ssh_port": ssh_port,
              "ssh_discover_keys": ssh_discover_keys,
              "ssh_host_key_policy": ssh_host_key_policy}

    iap_host = _ask_host("IAP server")
    mongo_host = _ask_host_with_reuse(
        "MongoDB server",
        reuse_options=[("IAP", iap_host)],
    )
    redis_host = _ask_host_with_reuse(
        "Redis server",
        reuse_options=[
            ("IAP", iap_host),
            *([("MongoDB", mongo_host)] if mongo_host != iap_host else []),
        ],
    )

    if iap_transport == "local":
        iap_node = TargetNode(role=NodeRole.IAP, host=iap_host, transport="local")
    else:
        iap_node = TargetNode(role=NodeRole.IAP, host=iap_host, **common)

    nodes = [
        iap_node,
        TargetNode(role=NodeRole.MONGO, host=mongo_host, **common),
        TargetNode(role=NodeRole.REDIS, host=redis_host, **common),
    ]

    gw_nodes = _ask_gateway_nodes(common)
    nodes.extend(gw_nodes)

    return DeploymentTopology(mode=DeploymentMode.STANDALONE, nodes=nodes)


def _wizard_ha2() -> DeploymentTopology:
    """Guide: HA2 multi-node deployment"""
    _hint("Highly Available architecture with redundant components.")
    _hint("Minimum: 2 IAP, 3 MongoDB (replica set), 3 Redis (sentinels).")
    _hint("MongoDB node count should be odd for healthy elections.")
    _hint("")
    _hint("Atlas connects to the PRIMARY node of each role for capture.")
    _hint("Non-primary nodes are recorded for topology validation.\n")

    iap_transport = _ask_node_transport()
    console.print()

    if iap_transport == "control_master":
        _hint(
            "ControlMaster: each node requires its own pre-opened SSH session.\n"
            "  Atlas will collect sockets for primary nodes now.\n"
            "  Non-primary nodes use the same socket pattern — open them before capture.\n"
            "  Primary-only capture scope is strongly recommended."
        )
        console.print()

        # -- IAP nodes -------------------------------------------------------
        console.print(f"  [{theme.primary_glow}]── IAP Servers ──[/{theme.primary_glow}]")
        _hint("First host listed is the primary (SSH + OAuth target)")
        iap_count = _ask_node_count("  How many IAP servers?", minimum=2, default=2)
        iap_hosts = _ask_hosts_for_role("IAP", iap_count)
        console.print()

        # -- MongoDB nodes ---------------------------------------------------
        console.print(f"  [{theme.primary_glow}]── MongoDB Replica Set ──[/{theme.primary_glow}]")
        _hint("First host listed is the primary (SSH + pymongo target)")
        mongo_count = _ask_mongo_count("  How many MongoDB servers?", minimum=3, default=3)
        mongo_hosts = _ask_hosts_for_role("MongoDB", mongo_count)
        console.print()

        # -- Redis nodes -----------------------------------------------------
        console.print(f"  [{theme.primary_glow}]── Redis Sentinels ──[/{theme.primary_glow}]")
        _hint("First host listed is the primary (SSH + redis-py target)")
        redis_count = _ask_node_count("  How many Redis servers?", minimum=3, default=3)
        redis_hosts = _ask_hosts_for_role("Redis", redis_count)
        console.print()

        # -- Collect per-node ControlMaster settings -------------------------
        _hint("Now enter ControlMaster settings for each node.")
        ha2_cm_nodes: list[TargetNode] = []

        for i, host in enumerate(iap_hosts, 1):
            console.print(f"\n  [{theme.primary_glow}]── IAP #{i} ({host}) ──[/{theme.primary_glow}]")
            sock, tgt, cm_port = _ask_control_master_settings(host)
            ha2_cm_nodes.append(TargetNode(
                role=NodeRole.IAP, host=host, label=f"iap-{i:02d}",
                transport="control_master",
                ssh_port=cm_port,
                ssh_control_socket=sock, ssh_control_target=tgt,
            ))

        for i, host in enumerate(mongo_hosts, 1):
            console.print(f"\n  [{theme.primary_glow}]── MongoDB #{i} ({host}) ──[/{theme.primary_glow}]")
            sock, tgt, cm_port = _ask_control_master_settings(host)
            ha2_cm_nodes.append(TargetNode(
                role=NodeRole.MONGO, host=host, label=f"mongo-{i:02d}",
                transport="control_master",
                ssh_port=cm_port,
                ssh_control_socket=sock, ssh_control_target=tgt,
            ))

        for i, host in enumerate(redis_hosts, 1):
            console.print(f"\n  [{theme.primary_glow}]── Redis #{i} ({host}) ──[/{theme.primary_glow}]")
            sock, tgt, cm_port = _ask_control_master_settings(host)
            ha2_cm_nodes.append(TargetNode(
                role=NodeRole.REDIS, host=host, label=f"redis-{i:02d}",
                transport="control_master",
                ssh_port=cm_port,
                ssh_control_socket=sock, ssh_control_target=tgt,
            ))

        gw_version = _ask_gateway_version()
        if gw_version:
            gw_count = _ask_node_count("How many gateway servers", minimum=1, default=1)
            gw_hosts = _ask_hosts_for_role("Gateway", gw_count)
            gw_modules = ["system", gw_version, "filesystem"]
            for i, gw_host in enumerate(gw_hosts, 1):
                console.print(
                    f"\n  [{theme.primary_glow}]── Gateway #{i} ({gw_host}) ──[/{theme.primary_glow}]"
                )
                sock, tgt, cm_port = _ask_control_master_settings(gw_host)
                ha2_cm_nodes.append(TargetNode(
                    role=NodeRole.IAG, host=gw_host, label=f"iag-{i:02d}",
                    transport="control_master", modules=gw_modules,
                    ssh_port=cm_port,
                    ssh_control_socket=sock, ssh_control_target=tgt,
                ))

        return DeploymentTopology(mode=DeploymentMode.HA2, nodes=ha2_cm_nodes)

    # -- Shared SSH credentials (stored as ssh_defaults in config) -----------
    if iap_transport == "local":
        _hint("SSH credentials apply to MongoDB, Redis, and non-primary IAP nodes.")
    _hint("Enter shared SSH credentials used across all servers.\n")

    ssh_user = _ask_ssh_user()
    ssh_key = _ask_ssh_key()
    ssh_key_passphrase = _ask_ssh_key_passphrase(ssh_key)
    ssh_port = _ask_ssh_port()
    ssh_discover_keys = _ask_ssh_discover_keys(ssh_key)
    ssh_host_key_policy = _ask_ssh_host_key_policy()
    console.print()

    common = {
        "ssh_user": ssh_user,
        "ssh_key": ssh_key,
        "ssh_key_passphrase": ssh_key_passphrase,
        "ssh_port": ssh_port,
        "ssh_discover_keys": ssh_discover_keys,
        "ssh_host_key_policy": ssh_host_key_policy,
    }

    # -- IAP nodes -----------------------------------------------------------
    console.print(f"  [{theme.primary_glow}]── IAP Servers ──[/{theme.primary_glow}]")
    if iap_transport == "local":
        _hint("First host is the primary (local transport). Remaining nodes use SSH.")
    else:
        _hint("First host listed is the primary (SSH + OAuth target)")
    iap_count = _ask_node_count("  How many IAP servers?", minimum=2, default=2)
    iap_hosts = _ask_hosts_for_role("IAP", iap_count)
    console.print()

    # -- MongoDB nodes -------------------------------------------------------
    console.print(f"  [{theme.primary_glow}]── MongoDB Replica Set ──[/{theme.primary_glow}]")
    _hint("First host listed is the primary (SSH + pymongo target)")
    mongo_count = _ask_mongo_count("  How many MongoDB servers?", minimum=3, default=3)
    mongo_hosts = _ask_hosts_for_role("MongoDB", mongo_count)
    console.print()

    # -- Redis nodes ---------------------------------------------------------
    console.print(f"  [{theme.primary_glow}]── Redis Sentinels ──[/{theme.primary_glow}]")
    _hint("First host listed is the primary (SSH + redis-py target)")
    redis_count = _ask_node_count("  How many Redis servers?", minimum=3, default=3)
    redis_hosts = _ask_hosts_for_role("Redis", redis_count)
    console.print()

    # -- Build topology nodes ------------------------------------------------
    nodes: list[TargetNode] = []

    for i, host in enumerate(iap_hosts, 1):
        # Primary IAP node uses local transport when the user opted in.
        # Non-primary nodes still connect over SSH (they may be visited in
        # ALL_NODES scope, and Atlas won't be installed on all of them).
        use_local = iap_transport == "local" and i == 1
        if use_local:
            nodes.append(TargetNode(
                role=NodeRole.IAP, host=host,
                label=f"iap-{i:02d}", transport="local",
            ))
        else:
            nodes.append(TargetNode(
                role=NodeRole.IAP, host=host,
                label=f"iap-{i:02d}", **common,
            ))
    for i, host in enumerate(mongo_hosts, 1):
        nodes.append(TargetNode(
            role=NodeRole.MONGO, host=host,
            label=f"mongo-{i:02d}", **common,
        ))
    for i, host in enumerate(redis_hosts, 1):
        nodes.append(TargetNode(
            role=NodeRole.REDIS, host=host,
            label=f"redis-{i:02d}", **common,
        ))

    # -- Optional IAG --------------------------------------------------------
    gw_nodes = _ask_gateway_nodes(common)
    nodes.extend(gw_nodes)

    return DeploymentTopology(mode=DeploymentMode.HA2, nodes=nodes)


# -- Custom node roles available in the wizard ------------------------------

_CUSTOM_ROLE_CHOICES = [
    questionary.Choice("IAP          — Itential Automation Platform",  value="iap"),
    questionary.Choice("MongoDB      — Database server",               value="mongo"),
    questionary.Choice("Redis        — Cache / message broker",        value="redis"),
    questionary.Choice("IAG          — Itential Automation Gateway",   value="iag"),
    questionary.Choice("All-in-One   — IAP + Mongo + Redis on one box", value="all"),
    questionary.Choice("Custom       — I'll pick the modules myself",  value="custom"),
]

_ALL_MODULES = [
    questionary.Choice("system       — CPU, memory, disk, network",        value="system"),
    questionary.Choice("mongo        — MongoDB status via pymongo",        value="mongo"),
    questionary.Choice("redis        — Redis INFO via redis-py",           value="redis"),
    questionary.Choice("platform     — Platform API health via OAuth",     value="platform"),
    questionary.Choice("filesystem   — Config file collection via SSH",    value="filesystem"),
    questionary.Choice("gateway4     — Gateway4 packages via SSH",      value="gateway4"),
    questionary.Choice("gateway5     - Gateway5 env vars via SSH",         value="gateway5"),
]


def _wizard_custom() -> DeploymentTopology:
    """Guide: free-form node definition"""
    _hint("Define each node individually with its role and modules.")
    _hint("Add as many nodes as you need.\n")

    ssh_user = _ask_ssh_user()
    ssh_key = _ask_ssh_key()
    ssh_key_passphrase = _ask_ssh_key_passphrase(ssh_key)
    ssh_port = _ask_ssh_port()
    ssh_discover_keys = _ask_ssh_discover_keys(ssh_key)
    ssh_host_key_policy = _ask_ssh_host_key_policy()
    console.print()

    common = {"ssh_user": ssh_user, "ssh_key": ssh_key,
              "ssh_key_passphrase": ssh_key_passphrase,
              "ssh_port": ssh_port,
              "ssh_discover_keys": ssh_discover_keys,
              "ssh_host_key_policy": ssh_host_key_policy}
    nodes: list[TargetNode] = []

    while True:
        node_num = len(nodes) + 1
        console.print(f"  [{theme.primary_glow}]── Node #{node_num} ──[/{theme.primary_glow}]")

        host = _ask_host(f"  Hostname")

        role_val = questionary.select(
            f"  Role for {host}",
            choices=_CUSTOM_ROLE_CHOICES,
            style=QSTYLE,
        ).ask()
        if role_val is None:
            _bail()

        role = NodeRole(role_val)

        node_transport = _ask_node_transport(target_label=f"this {role.value.upper()} server")

        # For custom role, let them pick modules
        modules = None
        if role == NodeRole.CUSTOM:
            selected = questionary.checkbox(
                "  Select modules to run on this node",
                choices=_ALL_MODULES,
                style=QSTYLE,
            ).ask()
            if selected is None:
                _bail()
            modules = selected

        label = ask_text_optional(f"  Label", instruction=f"(default: {role.value}-{host}) ")

        if node_transport == "local":
            nodes.append(TargetNode(
                role=role, host=host, label=label,
                transport="local", modules=modules,
            ))
        elif node_transport == "control_master":
            cm_sock, cm_tgt, cm_port = _ask_control_master_settings(host)
            nodes.append(TargetNode(
                role=role, host=host, label=label,
                transport="control_master", modules=modules,
                ssh_port=cm_port,
                ssh_control_socket=cm_sock, ssh_control_target=cm_tgt,
            ))
        else:
            nodes.append(TargetNode(
                role=role, host=host, label=label,
                modules=modules, **common,
            ))

        console.print()
        add_more = questionary.confirm(
            "Add another node?",
            default=True if node_num < 3 else False,
            style=QSTYLE,
        ).ask()
        if not add_more:
            break

    if not nodes:
        _bail("No nodes defined.")

    return DeploymentTopology(mode=DeploymentMode.CUSTOM, nodes=nodes)


def _wizard_kubernetes() -> tuple[DeploymentTopology, dict[str, Any]]:
    """
    Guide: Kubernetes deployment.

    Asks for Helm values.yaml path and optional kubectl configuration.
    Returns both the topology and K8s-specific metadata (values paths,
    kubectl settings) that get stored in the environment file.
    """
    _hint("Kubernetes deployment — no SSH required.")
    _hint("Atlas will collect system/config data from your Helm values file(s)")
    _hint("and use protocol collectors (OAuth, pymongo, redis-py) for live data.\n")

    k8s_meta: dict[str, Any] = {}

    # ── Data source selection ─────────────────────────────────────
    source_choice = questionary.select(
        "How should Atlas collect configuration data?",
        choices=[
            questionary.Choice(
                "Values file    — Provide a Helm values.yaml file",
                value="values",
            ),
            questionary.Choice(
                "kubectl        — Read from the live cluster (requires kubectl access)",
                value="kubectl",
            ),
            questionary.Choice(
                "Both           — kubectl primary, values.yaml as reference",
                value="both",
            ),
        ],
        style=QSTYLE,
    ).ask()
    if source_choice is None:
        _bail()

    # ── Values file path ──────────────────────────────────────────
    if source_choice in ("values", "both"):
        console.print()
        _hint("Provide the path to your IAP Helm chart values.yaml")
        _hint("This is the file used with 'helm install -f values.yaml'\n")

        values_path = questionary.path(
            "IAP values.yaml path",
            only_directories=False,
            validate=_validate_yaml_file,
            style=QSTYLE,
        ).ask()
        if values_path is None:
            _bail()
        k8s_meta["values_yaml_path"] = str(Path(values_path.strip()).expanduser().resolve())
    else:
        k8s_meta["values_yaml_path"] = ""

    # ── kubectl configuration ─────────────────────────────────────
    if source_choice in ("kubectl", "both"):
        console.print()
        _hint("Configure kubectl access to the cluster")

        # Locate the kubectl binary before asking for context/namespace.
        import shutil as _shutil
        import os as _os
        kubectl_binary = ""
        _found = _shutil.which("kubectl")
        if _found:
            console.print(f"  [green]✓[/green]  kubectl found at [bold]{_found}[/bold]")
        else:
            console.print(
                "\n  [bold yellow]⚠[/bold yellow]  [yellow]kubectl not found in PATH.[/yellow]"
            )
            for _attempt in range(3):
                custom = questionary.text(
                    "Path to kubectl binary (press Enter to skip kubectl):",
                    style=QSTYLE,
                ).ask()
                if custom is None:
                    _bail()
                custom = custom.strip()
                if not custom:
                    # User chose to skip kubectl
                    kubectl_binary = None  # sentinel: binary not available
                    break
                p = Path(custom).expanduser()
                if p.is_file() and _os.access(p, _os.X_OK):
                    kubectl_binary = str(p)
                    console.print(f"  [green]✓[/green]  kubectl found at [bold]{p}[/bold]")
                    break
                console.print(
                    f"  [red]✗[/red]  [red]'{p}' is not a valid executable. Try again.[/red]"
                )
            else:
                # 3 failed attempts
                kubectl_binary = None

        if kubectl_binary is None:
            # kubectl unavailable — handle based on what data we have
            k8s_meta["use_kubectl"] = False
            k8s_meta["kubectl_binary_path"] = ""
            k8s_meta["kubectl_context"] = ""
            k8s_meta["kubectl_namespace"] = ""

            if source_choice == "kubectl":
                # Chose kubectl-only but binary is unavailable — must decide now
                console.print()
                console.print(
                    "  [yellow]kubectl is not available. "
                    "Without it you'll need a values.yaml file to proceed.[/yellow]"
                )
                want_values = questionary.confirm(
                    "Would you like to provide a values.yaml file instead?",
                    default=True,
                    style=QSTYLE,
                ).ask()
                if want_values is None:
                    _bail()
                if want_values:
                    _hint("Provide the path to your IAP Helm chart values.yaml")
                    values_path = questionary.path(
                        "IAP values.yaml path",
                        only_directories=False,
                        validate=_validate_yaml_file,
                        style=QSTYLE,
                    ).ask()
                    if values_path is None:
                        _bail()
                    k8s_meta["values_yaml_path"] = str(
                        Path(values_path.strip()).expanduser().resolve()
                    )
                else:
                    # Neither kubectl nor values.yaml — no K8s data source at all
                    console.print()
                    console.print(
                        "  [bold red]⚠  No data source available.[/bold red]\n"
                        "  Without kubectl or a values.yaml file, Atlas cannot collect "
                        "configuration data from a Kubernetes deployment.\n\n"
                        "  [dim]Consider setting this environment up as a Standard or "
                        "Extended (non-Kubernetes) environment instead — those modes use "
                        "the Platform OAuth API, pymongo, and redis-py directly and do not "
                        "require kubectl or Helm values files.[/dim]"
                    )
                    if questionary.confirm(
                        "Return to the start of deployment setup?",
                        default=True,
                        style=QSTYLE,
                    ).ask():
                        return _wizard_kubernetes()
                    _bail()
        else:
            kubectl_context = ask_text_optional(
                "kubectl context",
                "(leave blank for current context) ",
            )
            kubectl_namespace = ask_text_optional(
                "Kubernetes namespace",
                "(e.g. itential, default) ",
            )
            k8s_meta["use_kubectl"] = True
            k8s_meta["kubectl_binary_path"] = kubectl_binary  # "" = use PATH
            k8s_meta["kubectl_context"] = kubectl_context
            k8s_meta["kubectl_namespace"] = kubectl_namespace
    else:
        k8s_meta["use_kubectl"] = False
        k8s_meta["kubectl_binary_path"] = ""
        k8s_meta["kubectl_context"] = ""
        k8s_meta["kubectl_namespace"] = ""

    # ── Sanity check: at least one data source must be reachable ─────────
    if not k8s_meta.get("use_kubectl") and not k8s_meta.get("values_yaml_path"):
        console.print()
        console.print(
            "  [bold red]⚠  No Kubernetes data source configured.[/bold red]\n"
            "  Atlas needs either a values.yaml file or kubectl access to audit "
            "a Kubernetes deployment.\n\n"
            "  [dim]If you don't have either, this may not be a Kubernetes environment "
            "— consider setting it up as a Standard or Extended environment instead.[/dim]"
        )
        if questionary.confirm(
            "Return to the start of deployment setup?",
            default=True,
            style=QSTYLE,
        ).ask():
            return _wizard_kubernetes()
        _bail()

    # ── Gateway5 (IAG5) support ───────────────────────────────────
    console.print()
    has_gw5 = questionary.confirm(
        "Do you have IAG5 (Automation Gateway 5) in this deployment?",
        default=False,
        style=QSTYLE,
    ).ask()
    if has_gw5 is None:
        _bail()

    if has_gw5 and source_choice in ("values", "both"):
        iag5_same_file = questionary.confirm(
            "Is IAG5 configured in the same values.yaml file?",
            default=False,
            style=QSTYLE,
        ).ask()

        if not iag5_same_file:
            iag5_path = questionary.path(
                "IAG5 values.yaml path",
                only_directories=False,
                validate=_validate_yaml_file,
                style=QSTYLE,
            ).ask()
            if iag5_path is None:
                _bail()
            k8s_meta["iag5_values_yaml_path"] = str(
                Path(iag5_path.strip()).expanduser().resolve()
            )
        else:
            k8s_meta["iag5_values_yaml_path"] = ""
    else:
        k8s_meta["iag5_values_yaml_path"] = ""

    # Build K8s topology
    topology = DeploymentTopology.kubernetes(has_gateway5=bool(has_gw5))

    return topology, k8s_meta


def _display_kubernetes_review(
    topology: DeploymentTopology,
    k8s_meta: dict[str, Any],
) -> None:
    """Display a summary of the Kubernetes deployment configuration."""
    table = Table(
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("Setting", style=f"bold {theme.text_primary}", min_width=24)
    table.add_column("Value", style=theme.text_secondary, min_width=40)

    table.add_row("Deployment mode", f"[bold {theme.accent}]KUBERNETES[/bold {theme.accent}]")

    if k8s_meta.get("values_yaml_path"):
        table.add_row("IAP values.yaml", k8s_meta["values_yaml_path"])
    if k8s_meta.get("iag5_values_yaml_path"):
        table.add_row("IAG5 values.yaml", k8s_meta["iag5_values_yaml_path"])

    kubectl_status = (
        f"Enabled (context: {k8s_meta.get('kubectl_context') or 'current'}, "
        f"namespace: {k8s_meta.get('kubectl_namespace') or 'default'})"
        if k8s_meta.get("use_kubectl")
        else f"[{theme.text_dim}]Disabled[/{theme.text_dim}]"
    )
    table.add_row("kubectl", kubectl_status)

    # Show which modules will run
    all_modules: list[str] = []
    for node in topology.nodes:
        all_modules.extend(node.effective_modules)
    modules_str = ", ".join(sorted(set(all_modules)))
    table.add_row("Collectors", modules_str)

    console.print(Panel(
        Group(
            Text(f" {topology.summary}\n", style=f"bold {theme.primary_glow}"),
            table,
        ),
        title="Kubernetes Deployment",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False,
    ))


def ask_deployment() -> tuple[dict, dict[str, Any]]:
    """
    Run the deployment topology wizard and return a serialized dict
    ready to embed in the config file.

    Returns:
        (deployment_dict, k8s_meta) — k8s_meta is empty for non-K8s deployments.
    """
    _section(
        "Deployment Topology",
        "How is your Itential environment set up?",
    )

    # ── Kubernetes check first ────────────────────────────────────
    is_k8s = questionary.confirm(
        "Is this environment running in Kubernetes?",
        default=False,
        style=QSTYLE,
    ).ask()
    if is_k8s is None:
        _bail()

    if is_k8s:
        console.print()
        topology, k8s_meta = _wizard_kubernetes()

        console.print()
        _display_kubernetes_review(topology, k8s_meta)

        if not questionary.confirm("Does this look right?", default=True, style=QSTYLE).ask():
            retry = questionary.confirm("Start deployment setup over?", default=True, style=QSTYLE).ask()
            if retry:
                return ask_deployment()
            _bail()

        result = topology.to_dict()
        result["capture_scope"] = "primary_only"
        return result, k8s_meta

    # ── Standard (non-K8s) deployment ─────────────────────────────
    mode = questionary.select(
        "Select your deployment architecture",
        choices=_MODE_CHOICES,
        style=QSTYLE,
    ).ask()
    if mode is None:
        _bail()

    console.print()

    wizards = {
        "standalone_all":   _wizard_standalone_all,
        "standalone_split": _wizard_standalone_split,
        "ha2":              _wizard_ha2,
        "custom":           _wizard_custom,
    }
    topology = wizards[mode]()

    # Ask about capture scope for multi-node deployments
    if topology.mode in (DeploymentMode.HA2, DeploymentMode.CUSTOM):
        console.print()
        capture_scope = _ask_capture_scope()
    else:
        capture_scope = "primary_only"

    # Review → optional edit → accept loop (H9). Previously the only
    # "doesn't look right" option was a full restart that wiped every
    # entered field. Now the user can mutate individual node hostnames
    # or change capture scope without redoing the whole wizard.
    while True:
        console.print()
        _display_topology_review(topology, capture_scope=capture_scope)

        if questionary.confirm("Does this look right?", default=True, style=QSTYLE).ask():
            break

        action = questionary.select(
            "What would you like to change?",
            choices=[
                questionary.Choice(
                    "Edit a node's hostname",
                    value="edit_host",
                ),
                questionary.Choice(
                    "Change capture scope",
                    value="scope",
                    disabled="(only relevant for HA2 / custom)"
                    if topology.mode not in (DeploymentMode.HA2, DeploymentMode.CUSTOM)
                    else None,
                ),
                questionary.Choice(
                    "Start deployment setup over",
                    value="restart",
                ),
                questionary.Choice(
                    "Cancel setup",
                    value="cancel",
                ),
            ],
            style=QSTYLE,
        ).ask()
        if action is None or action == "cancel":
            _bail()
        if action == "restart":
            return ask_deployment()
        if action == "scope":
            capture_scope = _ask_capture_scope()
            continue
        if action == "edit_host":
            # Pick a node by label, then prompt for a new hostname.
            if not topology.nodes:
                _hint("No nodes to edit.")
                continue
            node_choices = [
                questionary.Choice(
                    title=f"{n.label}  ({n.role.value.upper()}, {n.host})",
                    value=i,
                )
                for i, n in enumerate(topology.nodes)
            ]
            sel = questionary.select(
                "Pick the node to edit",
                choices=node_choices,
                style=QSTYLE,
            ).ask()
            if sel is None:
                continue
            new_host = _ask_host(
                f"  New hostname for {topology.nodes[sel].label}",
            )
            topology.nodes[sel].host = new_host
            # Auto-regenerated labels follow the host name — refresh if so.
            old_label = topology.nodes[sel].label
            default_label = f"{topology.nodes[sel].role.value}-{new_host}"
            if old_label.startswith(f"{topology.nodes[sel].role.value}-"):
                topology.nodes[sel].label = default_label
            continue

    # Build the deployment dict with the new structure
    result = topology.to_dict()
    result["capture_scope"] = capture_scope
    result["ssh_defaults"] = _build_ssh_defaults(topology)

    return result, {}


# =================================================
# Environment Name Prompt
# =================================================

def _ask_env_name(default: str = "") -> str:
    """Prompt for an environment name with validation."""
    def _v(v: str) -> bool | str:
        v = v.strip()
        if not v:
            return "Required"
        if not validate_env_name(v):
            return "Alphanumeric, hyphens, underscores only (1-64 chars, can't start/end with hyphen)"
        mgr = get_environment_manager()
        if mgr.exists(v):
            return f"Environment '{v}' already exists"
        return True

    result = questionary.text(
        "Environment name",
        instruction="(e.g. production, staging, dev) ",
        default=default,
        validate=_v,
        style=QSTYLE,
    ).ask()
    if result is None:
        _bail()
    return result.strip()


# =================================================
# Environment Creation Wizard
# =================================================

def _ask_tier_choice(default: str = "standard") -> str:
    """
    Prompt the user to pick a tier for a new environment.

    Returns "standard" or "extended". Cancelling raises KeyboardInterrupt
    so the caller can roll back cleanly.
    """
    standard_label = (
        "Standard — Platform OAuth (+ optional IAG4 API). "
        "5-minute setup, no SSH/Mongo/Redis."
    )
    extended_label = (
        "Extended — Full audit with SSH, MongoDB, Redis, Kubernetes. "
        "Requires infrastructure-team coordination."
    )

    choice = questionary.select(
        "What kind of environment is this?",
        choices=[standard_label, extended_label],
        default=standard_label if default == "standard" else extended_label,
        style=QSTYLE,
    ).ask()
    if choice is None:
        raise KeyboardInterrupt
    return "standard" if choice.startswith("Standard") else "extended"


def _create_standard_environment_wizard(
    env_name: str | None = None,
    from_env: str | None = None,
    default_organization_name: str | None = None,
    _keyring_verified: bool = False,
) -> Environment | None:
    """
    Standard-tier environment wizard — the "5-minute setup" flow.

    Collects only what's needed for a Platform OAuth audit (and optional
    IAG4 API). Never prompts for Mongo, Redis, SSH, deployment topology,
    or Kubernetes — those concepts belong to Extended Mode.

    Sequence:
        1. Environment name
        2. Organization name
        3. Platform URI
        4. Platform OAuth client ID + secret
        5. Optional Gateway4 (URI + username + password)
        6. Save environment + scoped credentials, set active.
    """
    _section(
        "Create Standard Environment",
        "Platform OAuth + optional IAG4 — no SSH or DB credentials needed",
    )

    mgr = get_environment_manager()

    if from_env:
        if not mgr.exists(from_env):
            console.print(f"  [{theme.error}]Source environment '{from_env}' not found[/{theme.error}]")
            return None
        source = mgr.load(from_env)
        new_env = Environment.from_dict(source.to_dict())
        new_env.name = env_name or _ask_env_name()
        new_env.tier = "standard"
        # Strip Extended-only fields when copying from an Extended source.
        new_env.deployment = None
        new_env.values_yaml_path = ""
        new_env.iag5_values_yaml_path = ""
        new_env.kubectl_context = ""
        new_env.kubectl_namespace = ""
        new_env.use_kubectl = False
        mgr.save(new_env)
        mgr.set_active(new_env.name)
        console.print(
            f"\n  [{theme.success}]✓ Standard environment '{new_env.name}' "
            f"created (copied from {from_env})[/{theme.success}]"
        )
        return new_env

    # -- Verify keyring backend (skipped when start_setup_process already checked) --
    if not _keyring_verified:
        is_secure, is_functional, backend = verify_keyring_backend()
        if not is_functional:
            console.print(Panel(
                f"[bold {theme.error}]No usable keyring backend found: {backend}[/bold {theme.error}]\n\n"
                f"[{theme.text_primary}]Platform Atlas cannot store credentials on this system.\n"
                f"  • macOS: Keychain (built-in)\n"
                f"  • Windows: Credential Locker (built-in)\n"
                f"  • Linux: install gnome-keyring, or use Vault as the credential backend[/{theme.text_primary}]",
                border_style=theme.error,
                box=box.ROUNDED,
                expand=False,
            ))
            return None
        if not is_secure:
            console.print(Panel(
                f"[bold {theme.warning}]Unencrypted keyring backend active: {backend}[/bold {theme.warning}]\n\n"
                f"[{theme.text_primary}]Credentials will be stored without encryption. Atlas will continue,\n"
                f"but consider switching to a secure backend for production use:\n"
                f"  • Linux: install gnome-keyring for encrypted Secret Service storage\n"
                f"  • Any platform: configure HashiCorp Vault as the credential backend[/{theme.text_primary}]",
                border_style=theme.warning,
                box=box.ROUNDED,
                expand=False,
            ))

    if env_name is None:
        env_name = _ask_env_name()
    elif mgr.exists(env_name):
        console.print(f"  [{theme.error}]Environment '{env_name}' already exists[/{theme.error}]")
        return None

    # Silently inherit the org name from any of:
    #   1. The caller (start_setup_process) — collected at the top of setup.
    #   2. The global config.json — written at the end of initial setup.
    # Users who genuinely need a different org per environment can change it
    # later via `platform-atlas env edit`. Asking again every time is the
    # actual bug — initial setup already collected this once.
    default_org = default_organization_name or ""
    if not default_org:
        try:
            if ATLAS_CONFIG_FILE.is_file():
                import json as _json
                with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as _f:
                    _cfg = _json.load(_f)
                default_org = _cfg.get("organization_name", "")
        except Exception:
            pass

    if default_org:
        org_name = default_org
    else:
        org_name = ask_text(
            "Organization Name",
            "(e.g. 'Acme Corp') ",
        )

    platform_uri = ask_text(
        "Platform (IAP) URL",
        "(e.g. https://iap.acme.com) ",
        uri=True,
    )
    platform_client_id = ask_text("Platform OAuth Client ID")

    # -- Credential Backend Selection -----------------------------------------
    use_vault = questionary.confirm(
        "Use HashiCorp Vault instead of OS Keyring?",
        default=False,
        style=QSTYLE,
    ).ask()
    if use_vault is None:
        _bail()
    backend_choice = "vault" if use_vault else "keyring"

    # Platform Client Secret — skip prompt when using Vault (read at runtime).
    # When we have a local secret we run a quick OAuth probe (UX1) so the
    # user sees credential failures inside the wizard, not during capture.
    platform_client_secret: str | None = None
    oauth_status = "skipped (Vault)" if backend_choice == "vault" else "not tested"
    if backend_choice != "vault":
        platform_uri, platform_client_id, platform_client_secret, oauth_status = (
            _collect_and_verify_platform_oauth(
                platform_uri=platform_uri,
                platform_client_id=platform_client_id,
            )
        )

    # -- Vault-specific setup -------------------------------------------------
    # vault_config is staged in memory and persisted to the OS keyring only
    # AFTER the env file is successfully saved. Saving early created orphan
    # keyring entries when the wizard was cancelled or crashed before the
    # env file got written.
    vault_config: VaultConfig | None = None
    test_backend: VaultBackend | None = None
    scoped = scoped_service_name(env_name)

    if backend_choice == "vault":
        while True:
            vault_config = ask_vault_settings()
            console.print(f"  [{theme.text_dim}]Testing Vault connection...[/{theme.text_dim}]")
            try:
                test_backend = VaultBackend(vault_config, service=scoped)
                console.print(f"  [{theme.success}]✓ Connected to Vault at {vault_config.url}[/{theme.success}]")
                if test_backend.token_ttl > 0:
                    _hint(f"Token TTL: {test_backend.token_ttl // 60}m {test_backend.token_ttl % 60}s"
                          + (" (renewable)" if test_backend.token_renewable else " (not renewable)"))
                break  # connection good — exit retry loop
            except CredentialError as e:
                console.print(f"  [{theme.error}]✘ Vault connection failed: {e}[/{theme.error}]")
                retry = questionary.confirm("Retry Vault configuration?", default=True, style=QSTYLE).ask()
                if retry is None or not retry:
                    _bail("Cannot continue without a working Vault connection.")

        console.print()
        console.print(Panel(
            f"[bold {theme.primary_glow}]Expected Vault Secret Layout[/bold {theme.primary_glow}]\n\n"
            f"[{theme.text_primary}]Atlas expects the following keys at "
            f"[bold]{vault_config.mount_point}/{vault_config.secret_path}[/bold]:[/{theme.text_primary}]\n\n"
            f"  [{theme.accent}]platform_client_secret[/{theme.accent}]"
            f"  [{theme.text_dim}]— Platform OAuth client secret[/{theme.text_dim}]\n"
            f"  [{theme.accent}]gateway4_password[/{theme.accent}]"
            f"          [{theme.text_dim}]— Gateway4 password (optional)[/{theme.text_dim}]\n\n"
            f"[{theme.text_dim}]Example:[/{theme.text_dim}]\n"
            f"  [{theme.text_muted}]vault kv put {vault_config.mount_point}/{vault_config.secret_path} \\\n"
            f"    platform_client_secret=\"...\" \\\n"
            f"    gateway4_password=\"...\"[/{theme.text_muted}]",
            box=box.ROUNDED,
            border_style=theme.border_primary,
            expand=False,
        ))

        while True:
            console.print(f"\n  [{theme.text_dim}]Checking Vault for required secrets...[/{theme.text_dim}]")
            found = test_backend.exists(CredentialKey.PLATFORM_SECRET.value)
            if found:
                console.print(
                    f"  [{theme.success}]✓ {CredentialKey.PLATFORM_SECRET.display_name} found in Vault[/{theme.success}]"
                )
                break

            console.print(
                f"  [{theme.error}]✘ {CredentialKey.PLATFORM_SECRET.display_name} not found in Vault[/{theme.error}]"
            )
            action = questionary.select(
                "How would you like to proceed?",
                choices=[
                    questionary.Choice("Retry       — Check Vault again (after adding secret)", value="retry"),
                    questionary.Choice("Continue    — Finish setup, add secret to Vault later", value="continue"),
                    questionary.Choice("Cancel      — Abort setup", value="cancel"),
                ],
                style=QSTYLE,
            ).ask()
            if action is None or action == "cancel":
                _bail()
            elif action == "continue":
                console.print(
                    f"  [{theme.text_dim}]Continuing — add missing secret to Vault before running a capture.[/{theme.text_dim}]"
                )
                break
            # else "retry" — loops back

    # -- Optional Gateway4 (IAG4) --------------------------------------------
    gateway4_uri = ""
    gateway4_username = ""
    gateway4_password = ""
    has_iag4 = questionary.confirm(
        "Do you use Itential Automation Gateway 4 (IAG4)?",
        default=False,
        style=QSTYLE,
    ).ask()
    if has_iag4 is None:
        raise KeyboardInterrupt
    if has_iag4:
        gateway4_uri = ask_text(
            "Gateway4 API URL",
            "(e.g. https://iag.acme.com) ",
            uri=True,
        )
        gateway4_username = ask_text_with_default(
            "Gateway4 Username",
            default="admin@itential",
        )
        if backend_choice == "keyring":
            gateway4_password = ask_secret("Gateway4 Password")
        else:
            # M2: verify gateway4_password is actually in Vault. Previously
            # we only checked PLATFORM_SECRET; users with IAG4 + Vault
            # didn't find out the password was missing until capture failed.
            _hint("Gateway4 password must be stored in Vault as 'gateway4_password'")
            if test_backend is not None:
                console.print(
                    f"\n  [{theme.text_dim}]Checking Vault for gateway4_password...[/{theme.text_dim}]"
                )
                if test_backend.exists(CredentialKey.GATEWAY4_PASSWORD.value):
                    console.print(
                        f"  [{theme.success}]✓ {CredentialKey.GATEWAY4_PASSWORD.display_name} "
                        f"found in Vault[/{theme.success}]"
                    )
                else:
                    console.print(
                        f"  [{theme.warning}]⚠ {CredentialKey.GATEWAY4_PASSWORD.display_name} "
                        f"not found in Vault — capture against Gateway4 will fail until you "
                        f"add it.[/{theme.warning}]"
                    )
                    _hint(
                        f"vault kv put {vault_config.mount_point}/{vault_config.secret_path} "
                        f"gateway4_password=\"...\""
                    )

    # -- Persist credentials in keyring scoped to this env -------------------
    if backend_choice == "keyring":
        # Sanity-check: if Ctrl+C ever made it past ask_secret historically,
        # this caught a None platform secret. With the helper fix it's a
        # belt-and-suspenders guard.
        if not platform_client_secret:
            console.print(
                f"\n  [{theme.error}]Platform Client Secret is required for keyring backend. "
                f"Re-run setup.[/{theme.error}]"
            )
            return None
        scoped_store = CredentialStore(
            backend_type=CredentialBackendType.KEYRING,
            env_name=env_name,
        )
        scoped_store.set(CredentialKey.PLATFORM_SECRET, platform_client_secret)
        if gateway4_password:
            scoped_store.set(CredentialKey.GATEWAY4_PASSWORD, gateway4_password)

    # -- Build & save the Environment file -----------------------------------
    env = Environment(
        name=env_name,
        description="",
        organization_name=org_name,
        platform_uri=platform_uri,
        platform_client_id=platform_client_id,
        credential_backend=backend_choice,
        deployment=None,
        legacy_profile="",
        gateway4_uri=gateway4_uri,
        gateway4_username=gateway4_username,
        tier="standard",
    )
    mgr.save(env)

    # -- Persist Vault connection settings (only after env file is saved) ----
    # Saving earlier would orphan keyring entries on cancellation. This is
    # the last point of no return — env file is written, credentials below.
    if backend_choice == "vault" and vault_config is not None:
        try:
            VaultBackend.save_config_to_keyring(vault_config, service=scoped)
            console.print(
                f"  [{theme.success}]✓ Vault connection settings saved to OS keyring[/{theme.success}]"
            )
        except CredentialError as exc:
            console.print(
                f"\n  [{theme.warning}]⚠ Vault connection saved on disk but keyring write failed: "
                f"{exc}[/{theme.warning}]"
            )
            console.print(
                f"  [{theme.text_dim}]Re-run 'platform-atlas config credentials' to retry.[/{theme.text_dim}]"
            )

    mgr.set_active(env_name)

    # Ensure tier=standard is recorded — but only when a global config
    # already exists. Writing a tier-only config.json from this path would
    # leave the user with a half-set-up Atlas (no organization_name, no
    # theme, etc.) that masquerades as a complete config. Initial setup is
    # responsible for writing the full config.json.
    if ATLAS_CONFIG_FILE.is_file():
        try:
            import json as _json
            with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as _f:
                global_cfg: dict[str, Any] = _json.load(_f)
            if "tier" not in global_cfg:
                global_cfg["tier"] = "standard"
                atomic_write_json(ATLAS_CONFIG_FILE, global_cfg)
        except Exception as exc:
            logger.debug("Could not persist tier default: %s", exc)

    # ── Post-init checklist (UX4) ─────────────────────────────────
    _is_secure, _is_functional, _kr_name = verify_keyring_backend()
    backend_summary = (
        f"OS Keyring ({_kr_name})" if backend_choice == "keyring"
        else f"HashiCorp Vault ({vault_config.url if vault_config else 'connection cached'})"
    )
    checks: list[tuple[str, bool | None, str, str]] = [
        ("Global config", True, str(ATLAS_CONFIG_FILE), ""),
        (f"Environment '{env_name}'", True, str(env.file_path), ""),
        (
            "Credential backend",
            _is_functional,
            backend_summary + ("" if _is_secure else " — unencrypted!"),
            "Switch to a secure backend or HashiCorp Vault for production use.",
        ),
        (
            "Platform OAuth",
            True if oauth_status == "ok" else (None if oauth_status.startswith("skipped") else False),
            "Token fetched OK" if oauth_status == "ok" else oauth_status,
            "Re-run `platform-atlas config doctor` after fixing the credentials.",
        ),
        ("Tier", True, "Standard (Platform OAuth only)", ""),
    ]
    if gateway4_uri:
        checks.append((
            "Gateway4 API",
            None,
            f"{gateway4_uri} (not auto-tested in wizard)",
            "",
        ))
    _render_post_init_checklist(
        env=env,
        backend_label=backend_summary,
        checks=checks,
    )

    return env


def create_environment_wizard(
    env_name: str | None = None,
    from_env: str | None = None,
    tier: str | None = None,
    default_organization_name: str | None = None,
    _keyring_verified: bool = False,
) -> Environment | None:
    """
    Interactive wizard to create a new environment.

    Branches on tier:
        - Standard: short 5-question flow (Platform OAuth + optional IAG4)
        - Extended: full topology + SSH + Mongo/Redis + Kubernetes flow

    If ``tier`` is not specified, prompts the user. ``--from`` (copy)
    inherits the source environment's tier when ``tier`` is None.

    Returns the created Environment, or None if canceled.
    """
    mgr = get_environment_manager()

    # If copying, inherit the source tier when caller didn't specify one.
    if tier is None and from_env and mgr.exists(from_env):
        try:
            src = mgr.load(from_env)
            tier = getattr(src, "tier", None) or "extended"
        except Exception:
            tier = None

    if tier is None:
        tier = _ask_tier_choice()

    if tier == "standard":
        return _create_standard_environment_wizard(
            env_name=env_name,
            from_env=from_env,
            default_organization_name=default_organization_name,
            _keyring_verified=_keyring_verified,
        )

    # ── Extended-tier flow (existing wizard) ──────────────────────────────
    _section(
        "Create Environment",
        "Configure a new deployment target",
    )

    # -- Copy from existing environment if --from was specified ----------------
    if from_env:
        if not mgr.exists(from_env):
            console.print(f"  [{theme.error}]Source environment '{from_env}' not found[/{theme.error}]")
            return None
        source = mgr.load(from_env)
        console.print(f"  [{theme.text_dim}]Copying from: {from_env}[/{theme.text_dim}]")
    else:
        source = None

    # -- Environment name -----------------------------------------------------
    if env_name is None:
        env_name = _ask_env_name()
    elif mgr.exists(env_name):
        console.print(f"  [{theme.error}]Environment '{env_name}' already exists[/{theme.error}]")
        return None

    description = ask_text_optional("Description", "(optional, e.g. 'Production US East') ")

    # -- Organization name ---------------------------------------------------
    # Silently inherit the org name from the caller (start_setup_process) or
    # from config.json. Per-env overrides happen via `env edit`, so prompting
    # again here is redundant and produced the "asked twice" complaint.
    default_org = default_organization_name or ""
    if not default_org:
        try:
            if ATLAS_CONFIG_FILE.is_file():
                import json as _json
                with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as _f:
                    _cfg = _json.load(_f)
                default_org = _cfg.get("organization_name", "")
        except Exception:
            pass

    if default_org:
        org_name = default_org
    else:
        org_name = ask_text(
            "Organization Name",
            "(e.g. 'Acme Corp') ",
        )

    # If copying, just save with new name and let user tweak later
    if source:
        new_env = Environment.from_dict(source.to_dict())
        new_env.name = env_name
        new_env.description = description or source.description
        new_env.organization_name = org_name or source.organization_name
        mgr.save(new_env)
        console.print(f"\n  [{theme.success}]✓ Environment '{env_name}' created (copied from {from_env})[/{theme.success}]")
        return new_env

    # -- Legacy profile (IAP 2023.x) ------------------------------------------
    is_legacy = questionary.confirm(
        "Is this a 2023.x environment?",
        default=False,
        style=QSTYLE,
    ).ask()
    if is_legacy is None:
        raise KeyboardInterrupt

    legacy_profile: str | None = None
    if is_legacy:
        legacy_profile = questionary.text(
            "What is the profile name that you're using in IAP 2023.x?",
            validate=lambda v: bool(v.strip()) or "Profile name cannot be empty",
            style=QSTYLE,
        ).ask()
        if legacy_profile is None:
            raise KeyboardInterrupt
        legacy_profile = legacy_profile.strip()

    # -- Verify keyring backend -----------------------------------------------
    is_secure, is_functional, backend = verify_keyring_backend()
    if not is_functional:
        console.print(Panel(
            f"[bold {theme.error}]No usable keyring backend found: {backend}[/bold {theme.error}]\n\n"
            f"[{theme.text_primary}]Platform Atlas cannot store credentials on this system.\n"
            f"  • macOS: Keychain (built-in)\n"
            f"  • Windows: Credential Locker (built-in)\n"
            f"  • Linux: install gnome-keyring, or use Vault as the credential backend[/{theme.text_primary}]",
            border_style=theme.error,
            box=box.ROUNDED,
            expand=False,
        ))
        raise SystemExit(1)
    if not is_secure:
        console.print(Panel(
            f"[bold {theme.warning}]Unencrypted keyring backend active: {backend}[/bold {theme.warning}]\n\n"
            f"[{theme.text_primary}]Credentials will be stored without encryption. Atlas will continue,\n"
            f"but consider switching to a secure backend for production use:\n"
            f"  • Linux: install gnome-keyring for encrypted Secret Service storage\n"
            f"  • Any platform: configure HashiCorp Vault as the credential backend[/{theme.text_primary}]",
            border_style=theme.warning,
            box=box.ROUNDED,
            expand=False,
        ))

    console.print(f"  [{theme.success}]✓ Credential store: {backend}[/{theme.success}]")
    console.print()

    # -- Credential Backend Selection -----------------------------------------
    _section("Credential Backend", "Where should Atlas retrieve secrets for this environment?")

    backend_choice = questionary.select(
        "Select credential backend",
        choices=[
            questionary.Choice(
                "OS Keyring  — Store credentials locally (macOS/Windows/Linux)",
                value="keyring",
            ),
            questionary.Choice(
                "Vault       — Read credentials from HashiCorp Vault (read-only)",
                value="vault",
            ),
        ],
        style=QSTYLE,
    ).ask()
    if backend_choice is None:
        _bail()

    # -- Connection Details ---------------------------------------------------
    _section("Connection Details", "Service URIs and credentials")

    platform_uri = ask_text("Platform URI", "(Example: https://localhost:3443) ", uri=True)
    platform_client_id = ask_text("Platform Client ID")

    # Ask for the Client Secret immediately after the Client ID so the OAuth
    # pair is captured together. On the Vault backend the secret is read from
    # Vault at runtime, so we skip the prompt and leave platform_client_secret
    # as None — Vault path will still source it correctly via vault_config.
    # When we have a local secret we run a quick OAuth probe (UX1).
    platform_client_secret: str | None = None
    oauth_status = "skipped (Vault)" if backend_choice == "vault" else "not tested"
    if backend_choice != "vault":
        platform_uri, platform_client_id, platform_client_secret, oauth_status = (
            _collect_and_verify_platform_oauth(
                platform_uri=platform_uri,
                platform_client_id=platform_client_id,
                url_label="Platform URI",
                url_instruction="(Example: https://localhost:3443) ",
                id_label="Platform Client ID",
                secret_label="Platform Client Secret (hidden)",
            )
        )

    # -- Vault-specific setup -------------------------------------------------
    # vault_config is staged in memory and persisted to the OS keyring only
    # AFTER the env file is successfully saved (see end of this wizard).
    # Saving early created orphan keyring entries when the wizard was
    # cancelled or crashed before the env file got written.
    vault_config: VaultConfig | None = None
    test_backend: VaultBackend | None = None
    mongo_uri = redis_uri = None

    # Scoped keyring service for this environment's credentials
    scoped = scoped_service_name(env_name)

    if backend_choice == "vault":
        while True:
            vault_config = ask_vault_settings()
            console.print(f"  [{theme.text_dim}]Testing Vault connection...[/{theme.text_dim}]")
            try:
                test_backend = VaultBackend(vault_config, service=scoped)
                console.print(f"  [{theme.success}]✓ Connected to Vault at {vault_config.url}[/{theme.success}]")
                if test_backend.token_ttl > 0:
                    _hint(f"Token TTL: {test_backend.token_ttl // 60}m {test_backend.token_ttl % 60}s"
                          + (" (renewable)" if test_backend.token_renewable else " (not renewable)"))
                break  # connection good — exit retry loop
            except CredentialError as e:
                console.print(f"  [{theme.error}]✘ Vault connection failed: {e}[/{theme.error}]")
                retry = questionary.confirm("Retry Vault configuration?", default=True, style=QSTYLE).ask()
                if retry is None or not retry:
                    _bail("Cannot continue without a working Vault connection.")

        # Show expected Vault keys and verify
        console.print()
        console.print(Panel(
            f"[bold {theme.primary_glow}]Expected Vault Secret Layout[/bold {theme.primary_glow}]\n\n"
            f"[{theme.text_primary}]Atlas expects the following keys at "
            f"[bold]{vault_config.mount_point}/{vault_config.secret_path}[/bold]:[/{theme.text_primary}]\n\n"
            f"  [{theme.accent}]platform_client_secret[/{theme.accent}]"
            f"  [{theme.text_dim}]— Platform OAuth client secret[/{theme.text_dim}]\n"
            f"  [{theme.accent}]mongo_uri[/{theme.accent}]"
            f"              [{theme.text_dim}]— Full MongoDB connection URI[/{theme.text_dim}]\n"
            f"  [{theme.accent}]redis_uri[/{theme.accent}]"
            f"              [{theme.text_dim}]— Full Redis connection URI[/{theme.text_dim}]\n"
            f"  [{theme.accent}]ssh_key_passphrase[/{theme.accent}]"
            f"     [{theme.text_dim}]— SSH key passphrase (optional)[/{theme.text_dim}]\n\n"
            f"[{theme.text_dim}]Example:[/{theme.text_dim}]\n"
            f"  [{theme.text_muted}]vault kv put {vault_config.mount_point}/{vault_config.secret_path} \\\n"
            f"    platform_client_secret=\"...\" \\\n"
            f"    mongo_uri=\"mongodb://user:pass@host:27017\" \\\n"
            f"    redis_uri=\"redis://user:pass@host:6379\" \\\n"
            f"    ssh_key_passphrase=\"...\"[/{theme.text_muted}]",
            box=box.ROUNDED,
            border_style=theme.border_primary,
            expand=False,
        ))

        # Retry loop for secret verification
        while True:
            console.print(f"\n  [{theme.text_dim}]Checking Vault for required secrets...[/{theme.text_dim}]")

            status_lines: list[str] = []
            missing_keys: list[CredentialKey] = []

            for key in CredentialKey:
                found = test_backend.exists(key.value)
                if found:
                    status_lines.append(
                        f"    [{theme.success}]✓[/{theme.success}] {key.display_name} ({key.value})"
                    )
                else:
                    missing_keys.append(key)
                    status_lines.append(
                        f"    [{theme.error}]✘[/{theme.error}] {key.display_name} ({key.value})"
                    )

            for line in status_lines:
                console.print(line)

            if not missing_keys:
                console.print(f"\n  [{theme.success}]✓ All required secrets found in Vault[/{theme.success}]")
                break

            console.print(
                f"\n  [{theme.warning}]⚠ {len(missing_keys)} credential(s) not found in Vault[/{theme.warning}]"
            )

            action = questionary.select(
                "How would you like to proceed?",
                choices=[
                    questionary.Choice(
                        "Retry          — Check Vault again (after adding secrets)",
                        value="retry",
                    ),
                    questionary.Choice(
                        "Continue       — Finish setup now, add secrets to Vault later",
                        value="continue",
                    ),
                    questionary.Choice(
                        "Reconfigure    — Re-enter Vault connection settings",
                        value="reconfigure",
                    ),
                    questionary.Choice(
                        "Cancel         — Abort setup",
                        value="cancel",
                    ),
                ],
                style=QSTYLE,
            ).ask()

            if action is None or action == "cancel":
                _bail()
            elif action == "continue":
                console.print(
                    f"  [{theme.text_dim}]Continuing — add missing secrets to Vault "
                    f"before running a capture.[/{theme.text_dim}]"
                )
                break
            elif action == "reconfigure":
                # Stage the new config in memory; keyring save still happens
                # at the end of the wizard, after the env file is written.
                vault_config = ask_vault_settings()
                try:
                    test_backend = VaultBackend(vault_config, service=scoped)
                    console.print(f"  [{theme.success}]✓ Connected to Vault at {vault_config.url}[/{theme.success}]")
                    if test_backend.token_ttl > 0:
                        _hint(f"Token TTL: {test_backend.token_ttl // 60}m {test_backend.token_ttl % 60}s"
                              + (" (renewable)" if test_backend.token_renewable else " (not renewable)"))
                except CredentialError as e:
                    console.print(f"  [{theme.error}]✘ Connection failed: {e}[/{theme.error}]")
                    continue
            # else "retry" — just loops back to the top

        # No local secret prompts — Mongo/Redis URIs and the Platform secret
        # are all read from Vault at runtime. platform_client_secret was
        # already set to None up top.
        mongo_uri = redis_uri = None

    # -- Keyring path: prompt for each secret locally -------------------------
    else:
        # Platform Client Secret was already collected right after the Client
        # ID above; only Mongo and Redis URIs are left to ask about here.
        _hint("MongoDB and Redis URIs are optional — skip if not needed for your deployment")
        mongo_uri = ask_scheme_uri_optional(
            "MongoDB URI",
            schemes=("mongodb://", "mongodb+srv://"),
            instruction="(leave blank to skip) ",
        )
        redis_uri = ask_scheme_uri_optional(
            "Redis URI",
            schemes=("redis://", "rediss://"),
            instruction="(leave blank to skip) ",
        )

    # -- Deployment Topology --------------------------------------------------
    deployment, k8s_meta = ask_deployment()

    # -- Gateway4 API Credentials (if gateway4 is in the topology) ----------
    # Gateway4 is not supported in Kubernetes mode
    gateway4_uri = ""
    gateway4_username = ""
    gateway4_password = ""
    _is_kubernetes = deployment.get("mode") == "kubernetes"

    if not _is_kubernetes:
        _has_gateway4 = any(
            "gateway4" in node.get("modules", [])
            for node in deployment.get("nodes", [])
        )
        if _has_gateway4:
            _section("Gateway4 API", "Direct API connection for config collection (primary source)")
            _hint("Atlas connects to Gateway4's REST API to collect runtime configuration.")
            _hint("This is the primary method — SSH config file collection is the fallback.\n")

            gateway4_uri = ask_text(
                "Gateway4 API URI",
                "(Example: http://gateway-host:8083) ",
                uri=True,
            )
            gateway4_username = ask_text_with_default(
                "Gateway4 Username",
                default="admin@itential",
            )

            if backend_choice == "keyring":
                gateway4_password = ask_secret("Gateway4 Password (hidden)")
            else:
                _hint("Gateway4 password must be stored in Vault as 'gateway4_password'")

    # -- Review ---------------------------------------------------------------
    _section("Review", "Everything we've collected for this environment")

    creds_table = Table(show_header=False, box=box.SIMPLE_HEAVY, pad_edge=True)
    creds_table.add_column("Field", style=f"bold {theme.text_primary}", min_width=24)
    creds_table.add_column("Value", style=theme.text_secondary)
    creds_table.add_row("environment", env_name)
    creds_table.add_row("organization", org_name or f"[{theme.text_dim}]—[/{theme.text_dim}]")
    creds_table.add_row("description", description or f"[{theme.text_dim}]—[/{theme.text_dim}]")
    creds_table.add_row("credential_backend", backend_choice)

    if backend_choice == "vault":
        creds_table.add_row("vault_url", vault_config.url)
        creds_table.add_row("vault_auth", vault_config.auth_method.value)
        creds_table.add_row("vault_path", f"{vault_config.mount_point}/{vault_config.secret_path}")
    else:
        creds_table.add_row("mongo_uri", mask(mongo_uri, keep=20) if mongo_uri else f"[{theme.text_dim}]— skipped[/{theme.text_dim}]")
        creds_table.add_row("redis_uri", mask(redis_uri, keep=20) if redis_uri else f"[{theme.text_dim}]— skipped[/{theme.text_dim}]")
        creds_table.add_row("platform_client_secret", mask(platform_client_secret))

    creds_table.add_row("platform_uri", platform_uri)
    creds_table.add_row("platform_client_id", platform_client_id)
    if legacy_profile:
        creds_table.add_row("legacy_profile", legacy_profile)
    if gateway4_uri:
        creds_table.add_row("gateway4_uri", gateway4_uri)
        creds_table.add_row("gateway4_username", gateway4_username)
    if k8s_meta.get("values_yaml_path"):
        creds_table.add_row("values_yaml", k8s_meta["values_yaml_path"])
    if k8s_meta.get("iag5_values_yaml_path"):
        creds_table.add_row("iag5_values_yaml", k8s_meta["iag5_values_yaml_path"])
    if k8s_meta.get("use_kubectl"):
        ctx_label = k8s_meta.get("kubectl_context") or "current"
        ns_label = k8s_meta.get("kubectl_namespace") or "default"
        creds_table.add_row("kubectl", f"{ctx_label} / {ns_label}")

    console.print(Panel(
        creds_table,
        title="Connection Details",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False,
    ))

    # Re-display topology summary
    topology = DeploymentTopology.from_dict(deployment)
    if _is_kubernetes:
        _display_kubernetes_review(topology, k8s_meta)
    else:
        scope = deployment.get("capture_scope", "primary_only")
        _display_topology_review(topology, capture_scope=scope)

    env_path = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
    if not questionary.confirm(f"Save environment to {env_path}?", default=True, style=QSTYLE).ask():
        _bail("Canceled. Nothing was written.")

    # ─────────────────────────────────────────────────────────────────────────
    # Persist order: env file → credentials → vault config → set active.
    # Any cancellation/crash BEFORE the env file is saved leaves no state.
    # A crash after env-file save leaves a recoverable, repairable env that
    # the user can complete via `platform-atlas config credentials`.
    # ─────────────────────────────────────────────────────────────────────────

    # Strip passphrases from individual node dicts before saving (sensitive
    # data should live in the credential store, not the env file).
    for node in deployment.get("nodes", []):
        node.pop("ssh_key_passphrase", None)

    # Extract the SSH passphrase from defaults before serializing the env;
    # we'll move it into the credential store below.
    ssh_passphrase = ""
    if not _is_kubernetes:
        ssh_defaults = deployment.get("ssh_defaults", {})
        ssh_passphrase = ssh_defaults.pop("key_passphrase", "")

    # Sanity check for keyring backend — must have a Platform secret
    if backend_choice == "keyring" and not platform_client_secret:
        console.print(
            f"\n  [{theme.error}]Platform Client Secret is required for keyring backend. "
            f"Re-run setup.[/{theme.error}]"
        )
        return None

    # -- Build and save the Environment file (point of no return) -------------
    env = Environment(
        name=env_name,
        description=description,
        organization_name=org_name,
        platform_uri=platform_uri,
        platform_client_id=platform_client_id,
        credential_backend=backend_choice,
        deployment=deployment,
        legacy_profile=legacy_profile,
        gateway4_uri=gateway4_uri,
        gateway4_username=gateway4_username,
        tier="extended",
        values_yaml_path=k8s_meta.get("values_yaml_path", ""),
        iag5_values_yaml_path=k8s_meta.get("iag5_values_yaml_path", ""),
        kubectl_context=k8s_meta.get("kubectl_context", ""),
        kubectl_namespace=k8s_meta.get("kubectl_namespace", ""),
        use_kubectl=k8s_meta.get("use_kubectl", False),
    )

    mgr.save(env)

    # -- Store credentials (scoped to this environment) -----------------------
    if backend_choice == "keyring":
        service = scoped_service_name(env_name)
        scoped_store = CredentialStore(
            service=service,
            backend_type=CredentialBackendType.KEYRING,
            env_name=env_name,
        )

        scoped_store.set(CredentialKey.PLATFORM_SECRET, platform_client_secret)
        if mongo_uri:
            scoped_store.set(CredentialKey.MONGO_URI, mongo_uri)
        if redis_uri:
            scoped_store.set(CredentialKey.REDIS_URI, redis_uri)
        if gateway4_password:
            scoped_store.set(CredentialKey.GATEWAY4_PASSWORD, gateway4_password)
        if ssh_passphrase:
            scoped_store.set(CredentialKey.SSH_PASSPHRASE, ssh_passphrase)

    else:
        # Vault mode: persist the staged connection settings now that the env
        # file is saved. Saving earlier would orphan keyring entries if the
        # wizard was cancelled before the env file got written.
        if vault_config is not None:
            try:
                VaultBackend.save_config_to_keyring(vault_config, service=scoped)
                console.print(
                    f"  [{theme.success}]✓ Vault connection settings saved to OS keyring[/{theme.success}]"
                )
            except CredentialError as exc:
                console.print(
                    f"\n  [{theme.warning}]⚠ Env saved but keyring write for Vault settings failed: "
                    f"{exc}[/{theme.warning}]"
                )
                console.print(
                    f"  [{theme.text_dim}]Re-run 'platform-atlas config credentials' to retry.[/{theme.text_dim}]"
                )

        if not _is_kubernetes:
            if ssh_passphrase:
                console.print(
                    f"\n  [{theme.warning}]⚠ SSH key passphrase was provided but cannot be stored — "
                    f"Vault backend is read-only.[/{theme.warning}]"
                )
                console.print(
                    f"  [{theme.text_dim}]Add '{CredentialKey.SSH_PASSPHRASE.value}' to your "
                    f"Vault secret manually.[/{theme.text_dim}]"
                )
            _has_gateway4 = any(
                "gateway4" in node.get("modules", [])
                for node in deployment.get("nodes", [])
            )
            if _has_gateway4 and not gateway4_password:
                console.print(
                    f"\n  [{theme.warning}]⚠ Gateway4 password must be added to Vault manually.[/{theme.warning}]"
                )
                console.print(
                    f"  [{theme.text_dim}]Add '{CredentialKey.GATEWAY4_PASSWORD.value}' to your "
                    f"Vault secret.[/{theme.text_dim}]"
                )

    # -- Set as active environment --------------------------------------------
    mgr.set_active(env_name)

    # -- Post-init checklist (UX4) ---------------------------------------------
    backend_label = (
        f"HashiCorp Vault ({vault_config.url})"
        if backend_choice == "vault"
        else f"OS keyring ({backend})"
    )
    checks: list[tuple[str, bool | None, str, str]] = [
        ("Global config", True, str(ATLAS_CONFIG_FILE), ""),
        (f"Environment '{env_name}'", True, str(env_path), ""),
        ("Credential backend", True, backend_label, ""),
        (
            "Platform OAuth",
            True if oauth_status == "ok" else (None if oauth_status.startswith("skipped") else False),
            "Token fetched OK" if oauth_status == "ok" else oauth_status,
            "Re-run `platform-atlas config doctor` after fixing the credentials.",
        ),
        ("Tier", True, "Extended (full infrastructure audit)", ""),
        (
            "Deployment topology",
            True,
            f"{deployment.get('mode', 'custom')} · {len(deployment.get('nodes', []))} node(s)",
            "",
        ),
    ]
    if gateway4_uri:
        checks.append((
            "Gateway4 API",
            None,
            f"{gateway4_uri} (not auto-tested in wizard)",
            "",
        ))
    _render_post_init_checklist(
        env=env,
        backend_label=backend_label,
        checks=checks,
    )

    return env

def start_setup_process() -> None:
    """
    Full setup process: global config + first environment.

    This is called on first run or via 'platform-atlas config init'.
    """
    console.print(Panel(
        f"[bold {theme.success_glow}]Atlas Setup[/bold {theme.success_glow}]\n"
        f"[{theme.text_dim}]First-time configuration[/{theme.text_dim}]",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False
    ))

    # -- Verify keyring backend before collecting any secrets ----------------
    is_secure, is_functional, backend = verify_keyring_backend()
    if not is_functional:
        console.print(Panel(
            f"[bold {theme.error}]No usable keyring backend found: {backend}[/bold {theme.error}]\n\n"
            f"[{theme.text_primary}]Platform Atlas cannot store credentials on this system.\n"
            f"  • macOS: Keychain (built-in)\n"
            f"  • Windows: Credential Locker (built-in)\n"
            f"  • Linux: install gnome-keyring, or use Vault as the credential backend[/{theme.text_primary}]",
            border_style=theme.error,
            box=box.ROUNDED,
            expand=False,
        ))
        raise SystemExit(1)
    if not is_secure:
        console.print(Panel(
            f"[bold {theme.warning}]Unencrypted keyring backend active: {backend}[/bold {theme.warning}]\n\n"
            f"[{theme.text_primary}]Credentials will be stored without encryption. Atlas will continue,\n"
            f"but consider switching to a secure backend for production use:\n"
            f"  • Linux: install gnome-keyring for encrypted Secret Service storage\n"
            f"  • Any platform: configure HashiCorp Vault as the credential backend[/{theme.text_primary}]",
            border_style=theme.warning,
            box=box.ROUNDED,
            expand=False,
        ))

    console.print(f"  [{theme.success}]✓ Credential store: {backend}[/{theme.success}]")
    console.print()

    if ATLAS_CONFIG_FILE.exists():
        ok = questionary.confirm(f"{ATLAS_CONFIG_FILE} already exists. Overwrite?", default=False, style=QSTYLE).ask()
        if not ok:
            _bail()

    # ================================================================
    # Phase 1: Global Settings
    # ================================================================
    _section("Global Settings", "Settings that apply across all environments")

    org_name = ask_text("Organization Name", "(Example: Acme Org) ")

    verify_ssl = questionary.confirm(
        "Verify SSL on Platform connections?",
        default=True,
        style=QSTYLE,
    ).ask()
    if verify_ssl is None:
        raise KeyboardInterrupt

    # Tier choice — Standard is the default for new users, but they can opt
    # into Extended right away if they know they need it.
    tier_default = _ask_tier_choice(default="standard")

    # -- Write global config --------------------------------------------------
    global_data: dict[str, Any] = {
        "organization_name": org_name,
        "verify_ssl": bool(verify_ssl),
        "dark_mode": True,
        "theme": "horizon-dark",
        "extended_validation_checks": True,
        "debug": False,
        "tier": tier_default,
        "compatibility_mode": "--plain" in sys.argv,
    }

    atomic_write_json(ATLAS_CONFIG_FILE, global_data)

    console.print(Panel(
        f"[{theme.success_glow} bold]Global config saved[/{theme.success_glow} bold] to "
        f"[bold]{ATLAS_CONFIG_FILE}[/bold]",
        box=box.ROUNDED,
        border_style=theme.success,
        expand=False,
    ))

    # ================================================================
    # Phase 2: First Environment
    # ================================================================
    console.print()
    _hint("Now let's configure your first deployment environment.")
    _hint("Each environment represents one IAP deployment (dev, staging, production, etc.)\n")

    # Ensure environments directory exists
    ATLAS_ENVIRONMENTS_DIR.mkdir(mode=0o700, exist_ok=True)

    create_environment_wizard(tier=tier_default, default_organization_name=org_name, _keyring_verified=True)

    # -- Offer to create additional environments ------------------------------
    while True:
        console.print()
        add_more = questionary.confirm(
            "Create another environment?",
            default=False,
            style=QSTYLE,
        ).ask()
        if add_more is None:
            raise KeyboardInterrupt
        if not add_more:
            break
        create_environment_wizard()


def _probe_system_quick() -> list[tuple[str, str, str, str | None]]:
    """Lightweight system probe used by the first-run welcome screen.

    Returns a list of ``(label, value, note, status)`` tuples where status
    is one of ``"ok" | "warn" | "fail" | None``. Every check must complete
    in well under a second so the welcome screen never feels slow. Notes
    are kept short so they render at typical terminal widths.
    """
    import shutil as _shutil

    rows: list[tuple[str, str, str, str | None]] = []

    py = sys.version_info
    py_ver = f"{py.major}.{py.minor}.{py.micro}"
    py_ok = py >= (3, 11)
    rows.append(("Python", py_ver,
                 "supported" if py_ok else "below 3.11",
                 "ok" if py_ok else "fail"))

    try:
        is_secure, is_functional, name = verify_keyring_backend()
        kr_label = (name or "—").replace("Backend", "").replace("Keyring", "") or (name or "—")
        if is_secure:
            rows.append(("OS keyring", kr_label, "encrypted", "ok"))
        elif is_functional:
            rows.append(("OS keyring", kr_label, "fallback", "warn"))
        else:
            rows.append(("OS keyring", kr_label, "use Vault", "fail"))
    except Exception:
        rows.append(("OS keyring", "—", "unavailable", "warn"))

    try:
        free = _shutil.disk_usage(str(Path.home())).free
        gb = free / (1024 ** 3)
        val = f"{gb / 1024:.1f} TB" if gb >= 1000 else f"{gb:.1f} GB"
        if gb >= 5:
            rows.append(("Free disk", val, "plenty", "ok"))
        elif gb >= 0.5:
            rows.append(("Free disk", val, "tight", "warn"))
        else:
            rows.append(("Free disk", val, "low", "fail"))
    except Exception:
        rows.append(("Free disk", "—", "unknown", None))

    return rows


def welcome_screen() -> None:
    """First-run welcome screen — punchy minimal hero.

    Centered wordmark, a personal greeting line in place of the tagline,
    three value-prop bullets, and the system probe collapsed into a
    single dot-status line. Reads fast and stays out of the way.
    """
    import os as _os
    import getpass as _getpass
    import time as _time

    _plain_active = _os.environ.get("NO_COLOR") or "--plain" in sys.argv
    if not _plain_active:
        try:
            import json as _json
            if ATLAS_CONFIG_FILE.is_file():
                with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as _f:
                    _plain_active = bool(_json.load(_f).get("compatibility_mode", False))
        except Exception:
            pass

    hour = _time.localtime().tm_hour
    if hour < 12:
        greet = "Good morning"
    elif hour < 18:
        greet = "Good afternoon"
    else:
        greet = "Good evening"

    try:
        user = _getpass.getuser()
    except Exception:
        user = ""

    arrow = ">" if _plain_active else "▶"
    sep = "·"
    mark_ok = "OK" if _plain_active else "✓"
    dot = "*" if _plain_active else "●"

    # ── Hero (centered) ────────────────────────────────────────────
    wordmark = Text("P L A T F O R M   A T L A S",
                    style=f"bold {theme.primary_glow}", justify="center")

    greeting = Text(justify="center")
    if user:
        greeting.append(f"{greet}, ", style=f"italic {theme.text_dim}")
        greeting.append(user, style=f"bold {theme.text_primary}")
        greeting.append(" — let's get you set up.", style=f"italic {theme.text_dim}")
    else:
        greeting.append(f"{greet} — let's get you set up.", style=f"italic {theme.text_dim}")

    version_line = Text(f"v{__version__}", style=theme.text_muted, justify="center")

    # ── Heading + value-prop bullets ───────────────────────────────
    heading = Text("First-time setup — about 5 minutes.",
                   style=f"bold {theme.text_primary}", justify="center")

    bullets = Text()
    for line in (
        "Captures your Itential Platform configuration",
        "Validates 100+ rules across Platform, Mongo, Redis, Gateway",
        "Generates a professional HTML compliance report",
    ):
        bullets.append(f"  {mark_ok}  ", style=f"bold {theme.success_glow}")
        bullets.append(f"{line}\n", style=theme.text_secondary)
    bullets.rstrip()

    # ── Inline status line ─────────────────────────────────────────
    # Collapse _probe_system_quick() into one centered line:
    #   "System ready: ● Python 3.14  ● Keyring encrypted  ● 1.3 TB free"
    # Dot color reflects status (green ok / amber warn / red fail / dim info).
    status_line = Text(justify="center")
    status_line.append("System ready:  ", style=theme.text_dim)
    rows = _probe_system_quick()
    for i, (label, value, note, status) in enumerate(rows):
        if status == "ok":
            dot_style = f"bold {theme.success_glow}"
        elif status == "warn":
            dot_style = f"bold {theme.warning_glow}"
        elif status == "fail":
            dot_style = f"bold {theme.error_glow}"
        else:
            dot_style = theme.text_dim
        if label == "Python":
            caption = f"Python {value}"
        elif label == "OS keyring":
            if status == "ok":
                caption = "Keyring encrypted"
            elif status == "warn":
                caption = "Keyring fallback"
            elif status == "fail":
                caption = "Keyring broken"
            else:
                caption = "Keyring unavailable"
        elif label == "Free disk":
            caption = f"{value} free"
        else:
            caption = f"{label} {value}"
        status_line.append(f"{dot} ", style=dot_style)
        status_line.append(caption, style=theme.text_secondary)
        if i < len(rows) - 1:
            status_line.append("   ", style=theme.text_dim)

    # ── CTA (centered) ─────────────────────────────────────────────
    cta = Text(justify="center")
    cta.append(f"{arrow} Press ", style=f"bold {theme.primary_glow}")
    cta.append("Enter", style=f"bold {theme.text_primary}")
    cta.append(" to start", style=f"bold {theme.primary_glow}")
    cta.append(f"   {sep}   ", style=theme.text_dim)
    cta.append("Ctrl+C", style=f"bold {theme.text_primary}")
    cta.append(" any time to cancel", style=theme.text_dim)

    # ── Compose ────────────────────────────────────────────────────
    content = Group(
        wordmark,
        greeting,
        version_line,
        Text(""),
        Rule(style=theme.border_secondary),
        Text(""),
        heading,
        Text(""),
        bullets,
        Text(""),
        status_line,
        Text(""),
        cta,
    )
    console.clear()
    console.print(Align.center(content))
    console.input(f"[{theme.text_dim}]Press Enter to continue...[/{theme.text_dim}]")
