"""
SSH connection diagnostics.

Turns an opaque SSH failure ("Authentication failed", "Network error") into a
specific, plain-English cause the user can act on: the hostname can't be
resolved, the port is refused, the key is encrypted, the server only wants a
password, and so on.

This module is deliberately leaf-level: it imports only the stdlib and
``paramiko`` so both the live transport (``core/transport.py``) and the
preflight checks (``core/preflight.py``) can share one classifier and speak the
same language. Every probe here is read-only and best-effort — a probe that
itself fails never masks the original error, it just means we fall back to a
less specific (but still honest) message.
"""
from __future__ import annotations

import logging
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

import paramiko

logger = logging.getLogger(__name__)

# Short TCP/banner probe timeout. Diagnosis runs *after* a failure, so this is
# pure overhead on an already-broken connection — keep it snappy.
_PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class SSHDiagnosis:
    """A classified SSH failure.

    Attributes:
        category: Stable machine slug (``dns``, ``port_refused``, ``auth_key``…)
                  for grouping/testing — never shown verbatim to users.
        summary:  Short headline, e.g. "Hostname cannot be resolved".
        detail:   One actionable line for the "↳" sub-line / error details.
    """
    category: str
    summary: str
    detail: str


def _expand(key_path: str | None) -> Path | None:
    if not key_path:
        return None
    try:
        return Path(key_path).expanduser()
    except Exception:  # pylint: disable=broad-except
        return None


def resolve_host(host: str) -> tuple[bool, str]:
    """Return (resolved, first_ip_or_error). Pure DNS check, no connection."""
    try:
        infos = socket.getaddrinfo(host, None)
        ip = infos[0][4][0] if infos else ""
        return True, ip
    except socket.gaierror as e:
        return False, str(e)
    except Exception as e:  # pylint: disable=broad-except
        return False, str(e)


def probe_port(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> str:
    """Classify raw TCP reachability of host:port.

    Returns one of: ``open``, ``refused``, ``timeout``, ``dns``, ``unreachable``.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open"
    except socket.gaierror:
        return "dns"
    except ConnectionRefusedError:
        return "refused"
    except (socket.timeout, TimeoutError):
        return "timeout"
    except OSError as e:
        logger.debug("TCP probe to %s:%s failed: %s", host, port, e)
        return "unreachable"


def peek_banner(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> str | None:
    """Read the first line a listening port emits.

    An SSH daemon greets with ``SSH-2.0-...`` before any auth. If the port is
    open but the greeting isn't an SSH banner, the user almost certainly has the
    wrong port (pointed at a web server, proxy, etc.). Returns the decoded first
    line, or None if nothing could be read.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = sock.recv(256)
        if not data:
            return None
        return data.split(b"\r\n", 1)[0].decode("utf-8", "replace").strip()
    except Exception as e:  # pylint: disable=broad-except
        logger.debug("Banner peek on %s:%s failed: %s", host, port, e)
        return None


def inspect_private_key(
    key_path: str | None,
    passphrase: str | None,
) -> SSHDiagnosis | None:
    """Inspect a private key file on local disk.

    Returns a diagnosis when something is provably wrong with the key *file*
    (missing, unreadable, wrong permissions, not a key, encrypted without a
    usable passphrase), or None when the key looks loadable — in which case a
    rejection is the server's doing, not the file's.
    """
    path = _expand(key_path)
    if path is None:
        return None

    if not path.exists():
        return SSHDiagnosis(
            "key_missing",
            "SSH key file not found",
            f"No file at {path} — fix the key path for this node",
        )
    if not path.is_file():
        return SSHDiagnosis(
            "key_missing",
            "SSH key path is not a file",
            f"{path} is a directory — point at the private key file itself",
        )
    try:
        with path.open("rb") as fh:
            fh.read(1)
    except PermissionError:
        return SSHDiagnosis(
            "key_unreadable",
            "SSH key file is not readable",
            f"Can't read {path} — check file ownership and permissions",
        )
    except OSError as e:
        return SSHDiagnosis(
            "key_unreadable",
            "SSH key file could not be opened",
            f"{path}: {e}",
        )

    # World/group-readable private keys are a common, silent foot-gun: OpenSSH
    # refuses them outright. paramiko is more lenient, but flag it so the user
    # isn't left guessing when the far end rejects the key.
    bad_perms = _key_perms_too_open(path)

    # Try to actually load the key. We don't know the type up front, so try each.
    load_error: Exception | None = None
    for key_cls in (
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.RSAKey,
        getattr(paramiko, "DSSKey", None),
    ):
        if key_cls is None:
            continue
        try:
            key_cls.from_private_key_file(str(path), password=passphrase or None)
            # Loaded cleanly → the file is fine.
            if bad_perms:
                return bad_perms
            return None
        except paramiko.PasswordRequiredException as e:
            load_error = e
            if passphrase:
                return SSHDiagnosis(
                    "key_bad_passphrase",
                    "SSH key passphrase is incorrect",
                    f"The passphrase for {path} did not decrypt the key",
                )
            return SSHDiagnosis(
                "key_encrypted",
                "SSH key is encrypted — passphrase required",
                f"Add the passphrase for {path} to this node's credentials",
            )
        except paramiko.SSHException as e:
            # Wrong type for this class, or a bad passphrase surfacing as a
            # generic SSHException — remember it and keep trying other types.
            load_error = e
            continue
        except Exception as e:  # pylint: disable=broad-except
            load_error = e
            continue

    # No key class could load it. If a passphrase was supplied, the most likely
    # cause is that it's wrong; otherwise the file isn't a key we understand.
    if passphrase and load_error is not None and "decrypt" in str(load_error).lower():
        return SSHDiagnosis(
            "key_bad_passphrase",
            "SSH key passphrase is incorrect",
            f"The passphrase for {path} did not decrypt the key",
        )
    return SSHDiagnosis(
        "key_unparseable",
        "SSH key file is not a valid private key",
        f"{path} isn't a recognizable OpenSSH/PEM private key "
        f"(is it a public key, or the wrong file?)",
    )


def _key_perms_too_open(path: Path) -> SSHDiagnosis | None:
    """Return a diagnosis if a private key is group/world accessible (POSIX)."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return SSHDiagnosis(
            "key_bad_perms",
            "SSH key permissions are too open",
            f"{path} is accessible by group/others — run 'chmod 600 {path}' "
            f"(SSH may refuse the key)",
        )
    return None


def query_auth_methods(
    host: str,
    port: int,
    username: str,
    timeout: float = _PROBE_TIMEOUT,
) -> list[str] | None:
    """Ask the server which auth methods it will accept for ``username``.

    Opens a short-lived transport and attempts ``auth_none``; a well-behaved
    server rejects it with the list of methods it actually wants. Returns that
    list (e.g. ``["publickey"]``, ``["password"]``), or None if the server
    couldn't be asked. Read-only: no key material or password is ever sent.
    """
    transport: paramiko.Transport | None = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=timeout)
        try:
            transport.auth_none(username)
        except paramiko.BadAuthenticationType as e:
            return list(e.allowed_types)
        except paramiko.AuthenticationException:
            # Server allowed 'none' probing but still rejected — can't enumerate.
            return None
        # auth_none unexpectedly succeeded (open server) — nothing to report.
        return None
    except Exception as e:  # pylint: disable=broad-except
        logger.debug("Auth-method query to %s@%s:%s failed: %s", username, host, port, e)
        return None
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:  # pylint: disable=broad-except
                pass


def _auth_detail(
    methods: list[str] | None,
    *,
    username: str,
    used_key: bool,
    used_password: bool,
    used_agent: bool,
) -> str:
    """Build the actionable detail line for an authentication rejection."""
    if methods:
        offered = ", ".join(methods)
        if "publickey" in methods and (used_key or used_agent):
            return (
                f"Server accepts: {offered}. The key was rejected — check that "
                f"the matching public key is in {username}'s authorized_keys, "
                f"that '{username}' is the right user, and that the server's "
                f"home/.ssh permissions aren't too open"
            )
        if "password" in methods and used_password:
            return (
                f"Server accepts: {offered}. The password was rejected — verify "
                f"the password and that '{username}' is the right user"
            )
        if methods == ["keyboard-interactive"] or (
            "keyboard-interactive" in methods and "password" not in methods
            and "publickey" not in methods
        ):
            return (
                f"Server only offers: {offered} (likely MFA) — Atlas can't answer "
                f"interactive prompts; use an SSH key or ControlMaster instead"
            )
        if "publickey" in methods and used_password:
            return (
                f"Server accepts: {offered}. It wants a key, but only a password "
                f"was configured for this node"
            )
        if "password" in methods and (used_key or used_agent):
            return (
                f"Server accepts: {offered}. It wants a password, but only key/agent "
                f"auth was configured for this node"
            )
        return f"Server accepts: {offered}. None of the configured methods were accepted"

    # Couldn't enumerate — give the best generic guidance based on what we tried.
    if used_key:
        return (
            f"The SSH key was rejected — confirm the key, the user '{username}', "
            f"and that the public key is in the server's authorized_keys"
        )
    if used_password:
        return f"The password was rejected — confirm the password and user '{username}'"
    if used_agent:
        return (
            "The SSH agent had no key the server accepted — confirm the right key "
            "is loaded (ssh-add -l) or configure an explicit key for this node"
        )
    return f"Authentication was rejected for user '{username}'"


def diagnose_connection_failure(
    exc: BaseException,
    *,
    host: str,
    port: int,
    username: str,
    key_path: str | None = None,
    key_passphrase: str | None = None,
    had_password: bool = False,
    had_agent: bool = False,
    timeout: float = _PROBE_TIMEOUT,
    probe: bool = True,
) -> SSHDiagnosis:
    """Classify an SSH connection failure into a specific, actionable diagnosis.

    ``exc`` is whatever the connection attempt raised (paramiko or OSError).
    When ``probe`` is True (the default) best-effort network probes refine the
    verdict; pass False to classify purely from the exception (used in tests and
    where a second connection is unwanted).
    """
    used_key = bool(key_path)
    used_agent = had_agent and not key_path
    hostport = f"{host}:{port}"

    # -- DNS: wrong hostname is the single most common "nothing works" cause. --
    if isinstance(exc, socket.gaierror) or (probe and not resolve_host(host)[0]):
        return SSHDiagnosis(
            "dns",
            "Hostname cannot be resolved",
            f"'{host}' did not resolve to an IP — check the hostname/DNS "
            f"(or use an IP address) for this node",
        )

    # -- Broken key file (knowable without the network). A bad key surfaces
    # inconsistently across paramiko versions — FileNotFoundError, SSHException,
    # or AuthenticationException — so check the file itself up front, whatever
    # was raised. Only short-circuit on *fatal* file problems; "perms too open"
    # is advisory and handled alongside the auth verdict below.
    _FATAL_KEY = {
        "key_missing", "key_unreadable", "key_unparseable",
        "key_encrypted", "key_bad_passphrase",
    }
    if key_path:
        key_diag = inspect_private_key(key_path, key_passphrase)
        if key_diag is not None and key_diag.category in _FATAL_KEY:
            return key_diag

    # -- Host key changed: possible MITM or a rebuilt/re-IP'd host. --
    if isinstance(exc, paramiko.BadHostKeyException):
        return SSHDiagnosis(
            "host_key_mismatch",
            "Host key has changed",
            f"The host key for {host} doesn't match known_hosts — if this host "
            f"was rebuilt, remove its old known_hosts entry; otherwise investigate",
        )

    # -- Authentication rejected by the server: ask what it actually wants. --
    if isinstance(exc, paramiko.AuthenticationException):
        # A group/world-readable key is a common silent cause of rejection.
        if key_path:
            perms = inspect_private_key(key_path, key_passphrase)
            if perms is not None and perms.category == "key_bad_perms":
                return perms
        methods = query_auth_methods(host, port, username, timeout) if probe else None
        return SSHDiagnosis(
            "auth",
            "Authentication failed",
            _auth_detail(
                methods,
                username=username,
                used_key=used_key,
                used_password=had_password,
                used_agent=used_agent,
            ),
        )

    # -- No route to a listening SSH port. --
    if isinstance(exc, (paramiko.ssh_exception.NoValidConnectionsError, ConnectionRefusedError)):
        return SSHDiagnosis(
            "port_refused",
            f"Connection refused on port {port}",
            f"Nothing is accepting SSH at {hostport} — the SSH daemon may be "
            f"down, or the port is wrong",
        )

    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SSHDiagnosis(
            "port_timeout",
            f"Timed out connecting to {hostport}",
            f"No response from {hostport} — host unreachable or a firewall is "
            f"dropping the port",
        )

    # -- Generic SSH protocol error: often a non-SSH port or an unparseable key. --
    if isinstance(exc, paramiko.SSHException):
        low = str(exc).lower()
        if "not a valid" in low or "private key" in low:
            key_diag = inspect_private_key(key_path, key_passphrase)
            if key_diag is not None:
                return key_diag
        if "banner" in low or "protocol" in low:
            banner = peek_banner(host, port, timeout) if probe else None
            if banner is not None and not banner.startswith("SSH-"):
                return SSHDiagnosis(
                    "not_ssh",
                    f"Port {port} is open but not speaking SSH",
                    f"{hostport} answered with '{banner[:40]}' — this looks like "
                    f"the wrong port for SSH",
                )
            return SSHDiagnosis(
                "ssh_banner",
                "No SSH banner received",
                f"{hostport} accepted the connection but didn't present an SSH "
                f"banner — wrong port, or an upstream proxy is interfering",
            )
        return SSHDiagnosis(
            "ssh_protocol",
            "SSH protocol error",
            f"{hostport}: {exc}",
        )

    # -- OSError / everything else: probe the port to say something useful. --
    if probe:
        status = probe_port(host, port, timeout)
        if status == "dns":
            return SSHDiagnosis(
                "dns",
                "Hostname cannot be resolved",
                f"'{host}' did not resolve to an IP — check the hostname/DNS",
            )
        if status == "refused":
            return SSHDiagnosis(
                "port_refused",
                f"Connection refused on port {port}",
                f"Nothing is accepting SSH at {hostport} — daemon down or wrong port",
            )
        if status == "timeout":
            return SSHDiagnosis(
                "port_timeout",
                f"Timed out connecting to {hostport}",
                f"No response from {hostport} — host unreachable or firewalled",
            )
        if status == "unreachable":
            return SSHDiagnosis(
                "unreachable",
                f"Cannot reach {hostport}",
                f"Network error reaching {hostport}: {exc}",
            )

    return SSHDiagnosis(
        "network",
        f"Cannot reach {hostport}",
        f"{type(exc).__name__}: {exc}",
    )
