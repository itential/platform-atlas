# pylint: disable=unnecessary-ellipsis,missing-function-docstring,broad-exception-caught
"""
ATLAS // Transport Layer

Provides a unified interface for file reads and command execution,
whether the target is the local machine or a remote host over SSH.

Collectors receive a Transport instance and call its methods without
having to worry about where the operations are from.

Example (local):
    >>> with LocalTransport() as t:
    ...     content = t.read_file("/etc/mongod.conf")
    ...     result = t.run_command(["uname", "-r"])

# Example (remote)
    >>> creds = SSHCredentials(hostname="10.0.2.15", username="atlas")
    >>> with SSHTransport(creds) as t:
    ...     content = t.read_file("/etc/mongod.conf")
    ...     result = t.run_command(["uname", "-r"])
"""

from __future__ import annotations

import os
import stat
import logging
import subprocess # nosec B404 - used in LocalTransport with command allowlist
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self, Sequence, runtime_checkable
from urllib.parse import unquote

import paramiko

from platform_atlas.core.exceptions import (
    CollectorConnectionError,
    CollectorError,
    SecurityError,
    EncryptedKeyError,
)

__all__ = [
    "Transport",
    "LocalTransport",
    "SSHTransport",
    "SSHCredentials",
    "ControlMasterTransport",
    "ControlMasterConfig",
    "CommandResult",
]

logger = logging.getLogger(__name__)

# Suppress noisy Paramiko connection failure output.
# Paramiko's transport thread logs full tracebacks at WARNING level
# when SSH connections fail (banner timeout, refused, etc.).
# We catch and handle all SSH exceptions in SSHTransport.connect(),
# so the internal paramiko output is redundant noise.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

# SECURITY DEFITIONS
# =================================================
# Characters that should never appear in paths sent over the wire
_SHELL_META = frozenset("|&$`\n\r<>;(){}!")
# Block shell injections for additional file security
_INJECTION_PATTERNS = ("$(", "`", "${", ">>", "2>", "&>")
# Only allow the following paths to limit exposure with subprocess
ALLOWED_PREFIXES = ("/etc/", "/opt/", "/proc/meminfo", "/usr/bin/",
                    "/var/lib/automation-gateway/", "/data/iag/",
                    "/var/log/",
)
# Only allow a subset of linux commands to be run for better security
ALLOWED_COMMANDS = frozenset({
    "uname", "pip", "python", "cat", "systemctl",
    "echo", "stat", "realpath", "hostname", "nproc",
    "sqlite3", "printenv", "command", "iagctl", "find",
    "test", "tail", "grep"
})
# Allow reading a maximum file size of 10MB or less
MAX_READ_SIZE_10_MB = 10 * 1024 * 1024 # 10MB
# =================================================

@dataclass
class SSHRetryConfig:
    """Configuration for SSH connection retry behavior"""
    max_attempts: int = 3
    initial_delay: float = 1.0 # seconds
    max_delay: float = 30.0 # seconds
    backoff_factor: float = 2.0 # exponential multiplier

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for a given attempt number (0-indexed)"""
        delay = self.initial_delay * (self.backoff_factor ** attempt)
        return min(delay, self.max_delay)

@dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured result from a command execution"""

    stdout: str
    stderr: str
    return_code: int

    @property
    def ok(self) -> bool:
        """True when the. command exited successfully"""
        return self.return_code == 0

    def check(self) -> Self:
        """Raise if the command failed, similar to subprocess check=True"""
        if not self.ok:
            # Exit 127 = command not found (POSIX standard)
            if self.return_code == 127:
                # Try to extract the command name from stderr
                parts = self.stderr.strip().split(":")
                cmd_name = parts[2].strip() if len(parts) > 2 else parts[0].strip() or "command"
                raise CollectorError(
                    f"'{cmd_name}' not found on target system",
                    details={
                        "stderr": self.stderr[:500] if self.stderr else "",
                        "return_code": self.return_code,
                        "suggestion": f"Verify '{cmd_name}' is installed (e.g. dnf install {cmd_name})",
                    },
                )
            raise CollectorError(
                f"Command failed (exit {self.return_code})",
                details={
                    "stderr": self.stderr[:500] if self.stderr else "",
                    "return_code": self.return_code,
                },
            )
        return self

@dataclass(frozen=True, slots=True, repr=False)
class SSHCredentials:
    """Immutable SSH connection parameters.

    Supports key-based auth (preferred), SSH agent forwarding,
    and password fallback. At least one of ``key_path``,
    ``password``, or ``use_agent=True`` must be provided.

    Attributes:
        hostname:       Target host (IP or FQDN).
        username:       SSH login user.
        port:           SSH port (default 22).
        key_path:       Path to a private key file (PEM).
        key_passphrase: Optional passphrase for an encrypted key.
        password:       Password fallback (use keys when possible).
        use_agent:      Allow for the local SSH agent for auth.
        discover_keys:  When True, Paramiko searches ~/.ssh for keys
                        even when no explicit key_path is given.
                        Default is False - requires explicit opt-in.
        host_key_policy: How to handle unknown host keys.
                         "reject", "auto_add", or "warn" (default).
        timeout:        TCP connect timeout in seconds.
        banner_timeout: SSH banner exchange timeout in seconds.
    """

    hostname: str
    username: str
    port: int = 22
    key_path: str | None = None
    key_passphrase: str | None = None
    password: str | None = None
    use_agent: bool = True
    host_key_policy: str = "warn"
    discover_keys: bool = False
    timeout: float = 10.0
    banner_timeout: float = 15.0

    def __post_init__(self) -> None:
        if not self.hostname or not self.hostname.strip():
            raise ValueError("hostname is required")
        if not self.username or not self.username.strip():
            raise ValueError("username is required")
        if self.host_key_policy not in ("reject", "auto_add", "warn"):
            raise ValueError(
                f"Invalid host_key_policy: {self.host_key_policy!r}. "
                f"Must be 'reject', 'auto_add', or 'warn'"
            )

        # Ensure at least one auth method is viable
        has_key = bool(self.key_path)
        has_password = bool(self.password)
        has_agent = self.use_agent
        has_discovery = self.discover_keys

        if not any((has_key, has_password, has_agent, has_discovery)):
            raise ValueError(
                "No SSH authentication method configured. Provide at lease one of: "
                "key_path, password, use_agent=True, or discover_keys=True"
            )

    def __repr__(self) -> str:
        return (
            f"SSHCredentials(hostname={self.hostname!r}, username={self.username!r}, "
            f"port={self.port}, key_path={self.key_path!r}, "
            f"has_password={'yes' if self.password else 'no'}, "
            f"has_passphrase={'yes' if self.key_passphrase else 'no'})"
        )

@dataclass(frozen=True, slots=True)
class ControlMasterConfig:
    """Parameters for a ControlMaster-backed SSH session.

    Requires the user to open the master connection before running Atlas:

        ssh -M -S <socket_path> -o ControlPersist=10m -fN <ssh_target>

    Attributes:
        socket_path: Path to the Unix control socket (e.g. /tmp/atlas-cm.sock).
        ssh_target:  Full SSH destination string, exactly as typed after ``ssh -M -S …``.
                     Simple key-based: ``user@target-host``
                     CyberArk PSMP:    ``user@target-host@psmp-gateway.example.com``
        port:        SSH port for the *master* connection only (default 22).
                     Multiplexed commands use the socket — no TCP, so port is irrelevant
                     once the master is open. Include in ``ssh -M`` guidance only.
    """
    socket_path: str
    ssh_target: str
    port: int = 22

    def __post_init__(self) -> None:
        if not self.socket_path or not self.socket_path.strip():
            raise ValueError("socket_path is required")
        if not self.ssh_target or not self.ssh_target.strip():
            raise ValueError("ssh_target is required")
        # Reject targets that look like SSH options — modern OpenSSH rejects
        # them anyway with "hostname contains invalid characters", but failing
        # here points at the config field instead of an opaque ssh exit 255.
        if self.ssh_target.lstrip().startswith("-"):
            raise ValueError(
                f"ssh_target must not start with '-' (looks like an ssh option): "
                f"{self.ssh_target!r}"
            )

    def _port_flag(self) -> str:
        """Return '-p <port> ' when port is non-standard, else empty string."""
        return f"-p {self.port} " if self.port != 22 else ""

# =================================================
# Transport Protocol
# =================================================

@runtime_checkable
class Transport(Protocol):
    """Interface that every transport backend must satisfy.

    Collectors depend on this protocol - not on a concrete class -
    so local and remote execution are fully interchangeable.
    """

    # -- lifecycle --

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None: ...

    # -- queries --

    @property
    def is_connected(self) -> bool: ...

    @property
    def label(self) -> str:
        """Human-readable identifier (e.g. 'local' or 'user@host')"""
        ...

    # -- operations --

    def read_file(self, path: str, *, encoding: str = "utf-8") -> str:
        """Return the full text content of a remote/local file"""
        ...

    def is_exists(self, path: str) -> bool:
        """Checks if the file path or directory exists"""
        ...

    def is_readable(self, path: str) -> bool:
        """Checks if file path or directory path is readable"""
        ...

    def file_size(self, path: str) -> int:
        """Returns the filesize of a given remote path"""
        ...

    def run_command(
            self,
            command: str,
            *,
            timeout: int = 60,
    ) -> CommandResult:
        """Execute *command* and return structured output"""
        ...

def _is_under_allowed(resolved: Path, allowed: tuple[str, ...]) -> bool:
    """Check if resolved path is strictly within an allowed directory tree"""
    resolved_str = str(resolved)
    for prefix in allowed:
        # Ensure prefix is a proper directory boundary
        if not prefix.endswith("/"):
            prefix = prefix + "/"
        if resolved_str.startswith(prefix) or resolved_str == prefix.rstrip("/"):
            return True
    return False

def _validate_path(path: str, *, allowed: tuple = ALLOWED_PREFIXES) -> None:
    """Reject paths with shell metacharacters or traversal tricks"""

    # Decode URL-encoded characters before validation
    decoded = unquote(path)

    if any(c in decoded for c in _SHELL_META):
        raise SecurityError(
            "Path contains forbidden characters",
            details={"path": path},
        )

    raw = Path(decoded)

    # Check raw input FIRST for traversal attempts
    if ".." in raw.parts:
        raise SecurityError("Path traversal detected", details={"path": path})

    # Then resolve to catch symlink-based escapes
    resolved = raw.resolve()

    # Validate resolved path is still within allowed directories
    if allowed and not _is_under_allowed(resolved, allowed):
        raise SecurityError(f"Path outside allowed directories: {str(resolved)}")

# =================================================
# Local Transport
# =================================================

class LocalTransport:
    """
    Execute operations on the machine Atlas is running on.

    This is the zero-config default: no SSH, no credentials,
    just direct syscalls via ``open()`` and ``subprocess.run()``
    """

    def __init__(self) -> None:
        self._connected = False
        self._sudo_available: bool | None = None

    # -- lifecycle --

    def connect(self) -> None:
        self._connected = True
        logger.debug("LocalTransport ready")

    def __repr__(self) -> str:
        state = "ready" if self._connected else "idle"
        return f"<LocalTransport {state}>"

    def close(self) -> None:
        self._connected = False

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # -- queries --

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def label(self) -> str:
        return "local"

    # -- operations --

    def _validate_local_path(
            self,
            path: str,
            *,
            allowed: tuple = ALLOWED_PREFIXES,
    ) -> Path:
        """Validate and resolve a local path"""

        # Check for shell metacharacters
        if any(c in path for c in _SHELL_META):
            raise SecurityError("Path contains forbidden characters")

        raw = Path(path)

        # Check if file exists before resolving
        if not raw.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        # Resolve FIRST, then check the resolved path for traversal
        resolved = raw.resolve(strict=True)

        # Check for path traversal in input
        if ".." in resolved.parts:
            raise SecurityError("Path traversal detected")

        # Validate resolved path is within allowed directories
        if allowed and not _is_under_allowed(resolved, allowed):
            raise SecurityError(
                f"Path outside allowed directories: {resolved}",
                details={
                    "requested": path,
                    "resolved": str(resolved),
                    "allowed": allowed
                }
            )

        return resolved

    # -- sudo escalation (file reads only) --

    def has_passwordless_sudo(self) -> bool:
        """Check if the local user can run sudo without a password.

        Result is cached for the lifetime of this transport instance.
        Only called when a file read hits PermissionError — never
        on the happy path.
        """
        if self._sudo_available is not None:
            return self._sudo_available

        try:
            proc = subprocess.run(  # nosec B603 B607 - fixed command, no user input
                ["sudo", "-n", "true"],
                capture_output=True,
                timeout=5,
            )
            self._sudo_available = proc.returncode == 0
        except Exception:
            self._sudo_available = False

        logger.debug(
            "Passwordless sudo (local): %s",
            "available" if self._sudo_available else "not available",
        )
        return self._sudo_available

    def _read_file_sudo(self, resolved: Path, encoding: str = "utf-8") -> str:
        """Read a local file via sudo cat. Only called as a fallback
        when the normal read hits PermissionError.

        The path must already be validated by _validate_local_path()
        before reaching this method.
        """
        logger.debug("sudo fallback read (local): %s", resolved)
        proc = subprocess.run(  # nosec B603 B607 - 'sudo cat' with validated path
            ["sudo", "-n", "cat", str(resolved)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            raise PermissionError(
                f"sudo cat failed for {resolved}: {proc.stderr.strip()}"
            )
        return proc.stdout

    # -- file operations --

    def read_file(
            self,
            path: str,
            *,
            encoding: str = "utf-8",
            max_size: int = MAX_READ_SIZE_10_MB
    ) -> str:
        """Read a local file with security checks"""

        # Check if the RAW path is a symlink BEFORE validation resolves it
        raw = Path(path)
        if raw.is_symlink():
            raise SecurityError(
                "Refusing to read symlink",
                details={"path": path, "target": str(raw.resolve())}
            )

        # Resolve after checking for symlink
        resolved = self._validate_local_path(path)

        # Check file size
        if resolved.stat().st_size > max_size:
            raise CollectorError(
                f"File too large: {resolved.stat().st_size:,} bytes"
            )

        try:
            return resolved.read_text(encoding=encoding)
        except PermissionError:
            if not self.has_passwordless_sudo():
                raise
            logger.debug("Permission denied for %s, retrying with sudo", path)
            return self._read_file_sudo(resolved, encoding)

    def is_exists(self, path: str) -> bool:
        try:
            self._validate_local_path(path)
            return True
        except (FileNotFoundError, SecurityError):
            return False

    def is_readable(self, path: str) -> bool:
        try:
            resolved = self._validate_local_path(path)
            return os.access(str(resolved), os.R_OK)
        except (SecurityError, FileNotFoundError):
            return False

    def file_size(self, path: str) -> int:
        """Return the size in bytes of a local file."""
        resolved = self._validate_local_path(path)
        return resolved.stat().st_size

    def _validate_command(self, cmd: str) -> None:
        """Validate a command against the allowlist"""
        parts = shlex.split(cmd)
        if not parts:
            raise SecurityError("Empty command")

        cmd_name = str(Path(parts[0]).name)
        cmd_parent = str(Path(parts[0]).parent)
        if cmd_name not in ALLOWED_COMMANDS and cmd_parent not in ALLOWED_PREFIXES:
            raise SecurityError(f"Command not in allowlist: {parts[0]}")

        for arg in parts[1:]:
            if any(c in arg for c in _SHELL_META):
                raise SecurityError(f"Argument contains forbidden characters: {arg}")
            # Block command substitution and redirection
            if any(pat in arg for pat in _INJECTION_PATTERNS):
                raise SecurityError(
                    f"Argument contains injection pattern: {arg}"
                )

    def run_command(self, command: str, *, timeout: int = 60) -> CommandResult:
        """Run a secure command against the local system"""
        # Validate the security of the command FIRST
        self._validate_command(command)

        cmd = shlex.split(command)
        logger.debug("LocalTransport running: %s", cmd)

        try:
            proc = subprocess.run( # nosec B603 - cmd validated by _validate_command(), no shell=True
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False
            )
            return CommandResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                return_code=proc.returncode,
            )
        except FileNotFoundError as e:
            raise CollectorError(
                f"Command not found: {cmd[0]}",
                details={"command": " ".join(cmd), "timeout": timeout},
            ) from e
        except subprocess.TimeoutExpired as e:
            raise CollectorError(
                f"Command timed out after {timeout}s",
                details={"command": " ".join(cmd), "timeout": timeout},
            ) from e

# =================================================
# SSH Transport
# =================================================

class SSHTransport:
    """
    Execute operations on a remote host over SSH.

    Uses Paramiko under the hood. Supports key-based auth,
    agent forwarding, and password fallback.

    The SFTP subsystem is used for file reads; ``exec_command``
    is used for running commands.

    Example:
        >>> creds = SSHCredentials(
                hostname="10.0.2.15",
                username="atlas",
                key_path="~/.ssh/id_ed25519",
            )
        >>> with SSHTransport(creds) as t:
            print(t.read_file("/etc/mongo.conf"))
    """

    __slots__ = ("_creds", "_client", "_sftp", "_path_cache", "_sudo_available", "_retry")

    def __init__(
        self,
        credentials: SSHCredentials,
        *,
        retry: SSHRetryConfig | None = None,
    ) -> None:
        self._creds = credentials
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._path_cache: dict[str, str] = {}
        self._sudo_available: bool | None = None
        # Retry only on transient network errors; auth failures fail fast.
        # ``None`` means a single attempt (existing behavior preserved).
        self._retry = retry

    def __repr__(self) -> str:
        target = f"{self._creds.username}@{self._creds.hostname}:{self._creds.port}"
        state = "connected" if self._client is not None else "disconnected"
        return f"<SSHTransport {target} {state}>"

    # -- lifecycle --

    def connect(self) -> None:
        if self._client is not None and self.is_connected:
            return # already healthy

        self._close_existing()
        target = f"{self._creds.username}@{self._creds.hostname}:{self._creds.port}"
        logger.info("SSH connecting to %s", target)

        client = paramiko.SSHClient()

        # Host key policy
        policy = self._creds.host_key_policy
        if policy == "auto_add":
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy()) # nosec B507
        elif policy == "warn":
            client.set_missing_host_key_policy(paramiko.WarningPolicy()) # nosec B507
        else:
            # Default: reject unknown keys (secure default)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        logger.debug("Host key policy: %s", policy)

        # Load system host keys when available
        try:
            client.load_system_host_keys()
            logger.debug("System host keys loaded")
        except Exception:
            logger.debug("Could not load system host keys")

        # Build auth kwargs
        connect_kwargs: dict[str, Any] = {
            "hostname": self._creds.hostname,
            "port": self._creds.port,
            "username": self._creds.username,
            "timeout": self._creds.timeout,
            "banner_timeout": self._creds.banner_timeout,
            "allow_agent": self._creds.use_agent
        }

        # Never auto-discover ~/.ssh/ keys unless explicitly opted in.
        # This prevents connecting with unintended keys.
        connect_kwargs["look_for_keys"] = self._creds.discover_keys

        # Add explicit credentials if provided
        if self._creds.password:
            connect_kwargs["password"] = self._creds.password

        if self._creds.key_path:
            # -- Explicit Key mode --
            # Use ONLY the specified key file. No agent, no discovery.
            key_path = Path(self._creds.key_path).expanduser().resolve()
            logger.debug("Auth mode: explicit key (%s)", key_path)

            if not key_path.is_file():
                raise CollectorConnectionError(
                    f"SSH key not found: {key_path}",
                    details={"key_path": str(key_path)},
                )
            connect_kwargs["key_filename"] = str(key_path)
            connect_kwargs["allow_agent"] = False
            connect_kwargs["look_for_keys"] = False
            if self._creds.key_passphrase:
                connect_kwargs["passphrase"] = self._creds.key_passphrase
                logger.debug("Key passphrase: provided")
            else:
                logger.debug("Key passphrase: not set (assuming unencrypted key)")

        else:
            # -- Agent mode --
            # No explicit key - let the SSH agent handle authentication.
            # Optionally allow discovery of keys in ~/.ssh/ if opted in.
            connect_kwargs["allow_agent"] = self._creds.use_agent
            connect_kwargs["look_for_keys"] = self._creds.discover_keys
            logger.debug(
                "Auth mode: agent (allow_agent=%s, discover_keys=%s)",
                self._creds.use_agent,
                self._creds.discover_keys,
            )

        if self._creds.password:
            logger.debug("Password auth: provided as fallback")

        # Connect with optional retry on *transient* network errors only.
        # Auth failures, host-key rejections, and protocol errors fail
        # fast — retrying them just amplifies broken configs.
        retry = self._retry
        max_attempts = retry.max_attempts if retry else 1
        last_transient_error: OSError | None = None

        for attempt in range(max_attempts):
            try:
                client.connect(**connect_kwargs)
                break
            except paramiko.AuthenticationException as e:
                error_msg = str(e).lower()

                # Detect encrypted key
                if "encrypted" in error_msg or "passphrase" in error_msg:
                    logger.debug("Encrypted key detected: %s", self._creds.key_path)
                    raise EncryptedKeyError(
                        f"SSH key is encrypted: {self._creds.key_path}",
                        details={
                            "suggestion": (
                                "Add 'ssh_key_passphrase' to this node's config, "
                                "or remove 'ssh_key' to use the SSH agent instead"
                            ),
                            "error": str(e)
                        }
                    ) from e

                logger.debug(
                    "Authentication failed for %s - key_path=%s, has_passphrase=%s, "
                    "allow_agent=%s, look_for_keys=%s, has_password=%s",
                    target,
                    self._creds.key_path or "(none)",
                    bool(self._creds.key_passphrase),
                    connect_kwargs.get("allow_agent"),
                    connect_kwargs.get("look_for_keys"),
                    bool(self._creds.password),
                )
                raise CollectorConnectionError(
                    f"SSH authentication failed for {self._creds.username}@{self._creds.hostname}",
                    details={"error": str(e)},
                ) from e
            except paramiko.SSHException as e:
                logger.debug("SSH protocol error connecting to %s: %s", target, e)
                raise CollectorConnectionError(
                    f"SSH connection error to {self._creds.hostname}",
                    details={"error": str(e)}
                ) from e
            except OSError as e:
                last_transient_error = e
                if attempt + 1 >= max_attempts:
                    logger.debug("Network error reaching %s: %s", target, e)
                    raise CollectorConnectionError(
                        f"Cannot reach {self._creds.hostname}:{self._creds.port}"
                        + (f" (after {max_attempts} attempts)" if max_attempts > 1 else ""),
                        details={"error": str(e)}
                    ) from e
                delay = retry.get_delay(attempt) if retry else 0.0
                logger.debug(
                    "SSH connect attempt %d/%d failed (%s) — retrying in %.1fs",
                    attempt + 1, max_attempts, e, delay,
                )
                import time as _time
                _time.sleep(delay)

        self._client = client
        logger.info("SSH connected to %s", self.label)

    def close(self) -> None:
        self._close_existing()
        logger.debug("SSHTransport closed")

    def _close_existing(self) -> None:
        """Shut down SFTP and SSH client gracefully"""
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # -- queries --

    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    @property
    def label(self) -> str:
        return f"{self._creds.username}@{self._creds.hostname}:{self._creds.port}"

    # -- internal helpers --

    def _validate_command(self, cmd: str) -> None:
        parts = shlex.split(cmd)

        if not parts:
            raise SecurityError("Empty command")

        cmd_name = str(Path(parts[0]).name)
        cmd_parent = str(Path(parts[0]).parent)

        if cmd_name not in ALLOWED_COMMANDS and cmd_parent not in ALLOWED_PREFIXES:
            raise SecurityError(f"Command not in allowlist: {parts[0]}")

        # Validate arguments contain no shell metacharacters
        for arg in parts[1:]:
            if any(c in arg for c in _SHELL_META):
                raise SecurityError(
                    f"Argument contains forbidden characters: {arg}"
                )
            # Block command substitution and redirection
            if any(pat in arg for pat in _INJECTION_PATTERNS):
                raise SecurityError(
                    f"Argument contains injection pattern: {arg}"
                )


    def _require_client(self) -> paramiko.SSHClient:
        """Return the SSH client, raising if disconnected"""
        if self._client is None or not self.is_connected:
            raise CollectorConnectionError(
                "SSH not connected. Use 'with SSHTransport(creds):' or call connect() first",
            )
        return self._client

    def _get_sftp(self) -> paramiko.SFTPClient:
        """Lazy-open the SFTP channel, reusing if still alive"""
        if self._sftp is not None:
            try:
                self._sftp.stat(".") # quick health check
                return self._sftp
            except Exception:
                # stale channel, reopen
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None

        client = self._require_client()
        self._sftp = client.open_sftp()
        return self._sftp

    def _resolve_remote_path(self, path: str) -> str:
        """Resolve a path on the remote host"""
        if path in self._path_cache:
            logger.debug("Path found in cache: %s", path)
            return self._path_cache[path]

        # Use realpath on the remote host to resolve symlinks and ..
        result = self.run_command(f"realpath {shlex.quote(path)}")

        if result.return_code != 0:
            raise FileNotFoundError(f"Path does not exist on remote: {path}")

        resolved = result.stdout.strip()
        self._path_cache[path] = resolved
        return resolved

    def _validate_remote_path(
            self,
            path: str,
            *,
            allowed: tuple = ALLOWED_PREFIXES
    ) -> str:
        """Validate and resolve a remote path"""
        # Check for shell metacharacters BEFORE sending to remote
        if any(c in path for c in _SHELL_META):
            raise SecurityError("Path contains forbidden characters")

        # Check for obvious path traversal in the INPUT
        if ".." in Path(path).parts:
            raise SecurityError("Path traversal detected in input")

        # Resolve on the REMOTE host
        try:
            resolved = self._resolve_remote_path(path)
        except FileNotFoundError as e:
            raise SecurityError(f"Path does not exist on remote: {path}") from e

        # Validate the RESOLVED path against allowed prefixes
        if allowed and not _is_under_allowed(resolved, allowed):
            raise SecurityError(
                f"Path outside allowed directories: {resolved}",
                details={
                    "requested": path,
                    "resolved": resolved,
                    "allowed": allowed
                }
            )

        return resolved

    # -- sudo escalation (file reads only) --

    def has_passwordless_sudo(self) -> bool:
        """Check if the SSH user can run sudo without a password.

        Result is cached for the lifetime of this transport session.
        Only called when a file read hits PermissionError — never
        on the happy path.
        """
        if self._sudo_available is not None:
            return self._sudo_available

        client = self._require_client()
        try:
            _, stdout, stderr = client.exec_command(
                "sudo -n true 2>/dev/null",
                timeout=5,
            )
            rc = stdout.channel.recv_exit_status()
            self._sudo_available = rc == 0
        except Exception:
            self._sudo_available = False

        logger.debug(
            "Passwordless sudo on %s: %s",
            self.label,
            "available" if self._sudo_available else "not available",
        )
        return self._sudo_available

    def _sudo_command(self, cmd: str, timeout: int = 15) -> CommandResult:
        """Run a command under sudo -n.

        Bypasses _validate_command() because 'sudo' is intentionally
        NOT in ALLOWED_COMMANDS — only this internal method may invoke it.
        The caller is responsible for ensuring the wrapped command is safe
        (validated path, no user-controlled arguments).
        """
        client = self._require_client()
        full_cmd = f"sudo -n {cmd}"

        logger.debug("sudo command on %s: %s", self.label, full_cmd)
        try:
            _, stdout, stderr = client.exec_command(full_cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()
        except Exception as e:
            return CommandResult(stdout="", stderr=str(e), return_code=1)

        return CommandResult(stdout=out, stderr=err, return_code=rc)

    def _read_file_sudo(
            self,
            path: str,
            *,
            encoding: str = "utf-8",
            max_size: int = MAX_READ_SIZE_10_MB,
    ) -> str:
        """Read a file via sudo — fully self-contained fallback.

        Every step (resolve, symlink check, size check, read) is
        performed through sudo shell commands instead of SFTP or
        unprivileged exec, because the SSH user may lack permission
        to even traverse the parent directory.

        The resolved path is still validated against ALLOWED_PREFIXES.
        """
        # Basic input sanitization
        if any(c in path for c in _SHELL_META):
            raise SecurityError("Path contains forbidden characters")
        if ".." in Path(path).parts:
            raise SecurityError("Path traversal detected in input")

        # Resolve via sudo (normal user may not be able to traverse parent)
        result = self._sudo_command(
            f"realpath {shlex.quote(path)}", timeout=5
        )
        if result.return_code != 0:
            raise FileNotFoundError(
                f"Path does not exist on remote: {path}"
            )
        resolved_path = result.stdout.strip()

        # Validate resolved path against allowed prefixes
        if not _is_under_allowed(resolved_path, ALLOWED_PREFIXES):
            raise SecurityError(
                f"Path outside allowed directories: {resolved_path}",
                details={"requested": path, "resolved": resolved_path}
            )

        # Symlink check via sudo (test -L exits 0 if path is a symlink)
        result = self._sudo_command(
            f"test -L {shlex.quote(resolved_path)}", timeout=5
        )
        if result.return_code == 0:
            raise SecurityError(
                "Refusing to read symlink",
                details={"path": resolved_path}
            )

        # Size check via sudo
        result = self._sudo_command(
            f"stat -c %s {shlex.quote(resolved_path)}", timeout=5
        )
        if result.return_code != 0:
            raise FileNotFoundError(
                f"Cannot stat remote file via sudo: {resolved_path}"
            )
        file_size = int(result.stdout.strip())
        if file_size > max_size:
            raise CollectorError(
                f"File too large: {file_size:,} bytes (max {max_size:,})",
                details={"path": resolved_path}
            )

        # Read via sudo cat
        result = self._sudo_command(
            f"cat {shlex.quote(resolved_path)}", timeout=30
        )
        if result.return_code != 0:
            raise PermissionError(
                f"sudo cat failed for {resolved_path}: {result.stderr.strip()}"
            )
        return result.stdout

    # -- operations --

    def read_file(
            self,
            path: str,
            *,
            encoding: str = "utf-8",
            max_size: int = MAX_READ_SIZE_10_MB
    ) -> str:
        """Read a file from the remote host with security checks.

        Attempts a normal SFTP read first. If any step hits a
        PermissionError, falls back to sudo cat (if the SSH user
        has passwordless sudo). The sudo path performs its own
        symlink and size checks via shell commands.
        """
        try:
            return self._read_file_sftp(path, encoding=encoding, max_size=max_size)
        except PermissionError:
            if not self.has_passwordless_sudo():
                raise
            logger.debug("Permission denied for %s, retrying with sudo", path)
            return self._read_file_sudo(
                path, encoding=encoding, max_size=max_size
            )

    def _read_file_sftp(
            self,
            path: str,
            *,
            encoding: str = "utf-8",
            max_size: int = MAX_READ_SIZE_10_MB
    ) -> str:
        """Read via SFTP — the normal unprivileged path.

        Uses one SFTP ``lstat`` for the symlink check and reuses its
        ``st_size`` for the size check, avoiding a separate ``stat -c %s``
        exec channel (saves ~50–150 ms per read on a high-RTT link).
        """
        sftp = self._get_sftp()

        # Check the ORIGINAL path for symlinks BEFORE resolving
        try:
            stat_info = sftp.lstat(path)
            if stat.S_ISLNK(stat_info.st_mode):
                raise SecurityError(
                    "Refusing to read symlink",
                    details={"path": path, "target": "use 'realpath' to inspect"}
                )
        except IOError as e:
            # PermissionError is a subclass of OSError — let it propagate
            # so the sudo fallback in read_file() can catch it
            if isinstance(e, PermissionError):
                raise
            pass # File doesn't exist, _validate_remote_path() will catch this

        # Validate and resolve the path on the remote host
        resolved_path = self._validate_remote_path(path)

        # Re-check symlink + size on the resolved path. Reuse the lstat result
        # for the size check instead of running a separate ``stat -c %s``.
        try:
            stat_info = sftp.lstat(resolved_path)
        except PermissionError:
            raise
        except IOError as e:
            raise FileNotFoundError(
                f"Cannot stat remote file: {resolved_path} ({e})"
            ) from e

        if stat.S_ISLNK(stat_info.st_mode):
            raise SecurityError(
                "Refusing to read symlink",
                details={"path": resolved_path}
            )

        file_size = int(getattr(stat_info, "st_size", 0) or 0)
        if file_size > max_size:
            raise CollectorError(
                f"File too large: {file_size:,} bytes (max {max_size:,})",
                details={"path": resolved_path}
            )

        # Read file contents from remote and decode
        with sftp.file(resolved_path, 'r') as f:
            content = f.read().decode(encoding)

        return content

    def is_exists(self, path: str) -> bool:
        """Check if a path exists on the remote host.

        Uses shell ``test -e`` instead of SFTP stat, with a sudo
        fallback for paths where the SSH user lacks permission to
        traverse the parent directory.
        """
        if any(c in path for c in _SHELL_META):
            return False
        if ".." in Path(path).parts:
            return False

        result = self.run_command(
            f"test -e {shlex.quote(path)}", timeout=5
        )
        if result.return_code == 0:
            return True

        # Normal user can't traverse the parent dir — try sudo
        if self.has_passwordless_sudo():
            result = self._sudo_command(
                f"test -e {shlex.quote(path)}", timeout=5
            )
            return result.return_code == 0

        return False

    def is_readable(self, path: str) -> bool:
        """Check if a path is readable on the remote host.

        Uses shell ``test -r`` instead of SFTP open, with a sudo
        fallback for paths where the SSH user lacks permission.
        """
        if any(c in path for c in _SHELL_META):
            return False
        if ".." in Path(path).parts:
            return False

        result = self.run_command(
            f"test -r {shlex.quote(path)}", timeout=5
        )
        if result.return_code == 0:
            return True

        if self.has_passwordless_sudo():
            result = self._sudo_command(
                f"test -r {shlex.quote(path)}", timeout=5
            )
            return result.return_code == 0

        return False

    def file_size(self, path: str) -> int:
        """Get file size in bytes for a remote path"""
        resolved = self._validate_remote_path(path)
        result = self.run_command(f"stat -c %s {shlex.quote(resolved)}")
        result.check()
        return int(result.stdout.strip())

    def run_command(
            self,
            command: str | Sequence[str],
            *,
            timeout: int = 60,
    ) -> CommandResult:
        client = self._require_client()

        # Build a shell-safe command string from the argument list
        # Normalize to a command string for SSH exec_command
        if isinstance(command, str):
            cmd_str = command # Already a shell string, send as-is
        else:
            # We quote each argument to avoid injection
            cmd_str = " ".join(shlex.quote(str(c)) for c in command)

        # Validate command security before anything else
        self._validate_command(cmd_str)

        logger.debug("SSHTransport running on %s: %s", self.label, cmd_str)

        try:
            _, stdout, stderr = client.exec_command( # nosec B601 - cmd_str validated by _validate_command()
                cmd_str,
                timeout=timeout,
            )
            # Read all output
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()

            return CommandResult(stdout=out, stderr=err, return_code=rc)
        except paramiko.SSHException as e:
            raise CollectorError(
                f"SSH command execution failed on {self.label}",
                details={"command": cmd_str, "error": str(e)}
            ) from e
        except OSError as e:
            raise CollectorConnectionError(
                f"Connection lost to {self.label} during command execution",
                details={"command": cmd_str, "error": str(e)}
            ) from e

# =================================================
# ControlMaster Transport
# =================================================

class ControlMasterTransport:
    """SSH transport that multiplexes through an existing OpenSSH ControlMaster socket.

    The user opens a master connection once (handling any MFA / PAM gate):

        ssh -M -S /tmp/atlas-cm.sock -o ControlPersist=10m -fN <ssh_target>

    Atlas then multiplexes its own connections through that authenticated
    session with no separate credentials — no keys, no passwords, no MFA
    interaction. Works transparently with CyberArk PSMP and other
    privileged-access gateways where Atlas cannot hold or use target
    credentials directly.

    ``ControlPersist`` keeps the master socket alive after the foreground process
    exits (-fN backgrounds it). 10 minutes is enough for a typical capture; use
    a longer value or ``ControlPersist=yes`` if captures are slow.

    File reads use ``cat`` over the socket channel. Path validation mirrors
    SSHTransport: remote ``realpath``, allowlist check, symlink rejection, and
    a sudo fallback when the session user lacks read permission.
    """

    __slots__ = ("_config", "_connected", "_sudo_available")

    def __init__(self, config: ControlMasterConfig) -> None:
        import sys as _sys
        if _sys.platform == "win32":
            raise CollectorConnectionError(
                "SSH ControlMaster is not supported on Windows — OpenSSH for Windows does not implement the -M/-S multiplexing protocol.",
                details={"suggestion": "Use the standard SSH transport instead."},
            )
        self._config = config
        self._connected = False
        self._sudo_available: bool | None = None

    def __repr__(self) -> str:
        state = "connected" if self._connected else "disconnected"
        return f"<ControlMasterTransport {self.label} {state}>"

    # -- lifecycle --

    def connect(self) -> None:
        if self._connected:
            return

        socket_path = Path(self._config.socket_path)
        _pf = self._config._port_flag()
        if not socket_path.exists():
            raise CollectorConnectionError(
                f"ControlMaster socket not found: {self._config.socket_path}",
                details={
                    "suggestion": (
                        f"Open the master connection before running Atlas:\n"
                        f"  ssh -M -S {self._config.socket_path} {_pf}-o ControlPersist=10m "
                        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                        f"-fN {self._config.ssh_target}"
                    )
                },
            )
        if os.name == "posix" and not stat.S_ISSOCK(socket_path.stat().st_mode):
            raise CollectorConnectionError(
                f"ControlMaster path is not a Unix socket: {self._config.socket_path}",
                details={
                    "suggestion": (
                        "A regular file or directory exists at the socket path. "
                        "Remove it and re-open the master connection:\n"
                        f"  ssh -M -S {self._config.socket_path} {_pf}-o ControlPersist=10m "
                        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                        f"-fN {self._config.ssh_target}"
                    )
                },
            )

        result = self._run_raw("echo atlas-cm-ping", timeout=10)
        if result.return_code != 0 or "atlas-cm-ping" not in result.stdout:
            raise CollectorConnectionError(
                f"ControlMaster socket exists but is not responding: {self._config.socket_path}",
                details={
                    "stderr": result.stderr[:300] if result.stderr else "",
                    "suggestion": (
                        "The socket file is stale — the master connection timed out or was closed.\n"
                        "  1. Clean up stale sockets:  platform-atlas env sockets <env-name> --clean\n"
                        "  2. Re-open the master connection:\n"
                        f"     ssh -M -S {self._config.socket_path} {_pf}-o ControlPersist=10m "
                        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                        f"-fN {self._config.ssh_target}"
                    ),
                },
            )

        self._connected = True
        logger.info(
            "ControlMaster connected via %s → %s",
            self._config.socket_path,
            self._config.ssh_target,
        )

    def close(self) -> None:
        self._connected = False
        logger.debug("ControlMasterTransport closed (master socket left open)")

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # -- queries --

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def label(self) -> str:
        return f"cm:{self._config.ssh_target}"

    # -- internal helpers --

    def _run_raw(self, command: str, *, timeout: int = 60) -> CommandResult:
        """Run a command through the control socket, bypassing the allowlist.

        For internal use only — connect ping, sudo probe, and the sudo read
        path. All public-facing calls go through ``run_command()``.
        """
        try:
            proc = subprocess.run(  # nosec B603 B607 - fixed arg list; command validated by callers
                [
                    "ssh",
                    "-S", self._config.socket_path,
                    "-o", "ControlMaster=no",
                    "-o", "BatchMode=yes",
                    self._config.ssh_target,
                    "--",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return CommandResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                return_code=proc.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise CollectorError(
                f"Command timed out after {timeout}s",
                details={"command": command, "timeout": timeout},
            ) from e
        except FileNotFoundError as e:
            raise CollectorConnectionError(
                "ssh binary not found — ensure OpenSSH is installed and on PATH",
                details={"error": str(e)},
            ) from e

    def _validate_command(self, cmd: str) -> None:
        """Validate command against the allowlist (mirrors SSHTransport logic)."""
        parts = shlex.split(cmd)
        if not parts:
            raise SecurityError("Empty command")

        cmd_name = str(Path(parts[0]).name)
        cmd_parent = str(Path(parts[0]).parent)
        if cmd_name not in ALLOWED_COMMANDS and cmd_parent not in ALLOWED_PREFIXES:
            raise SecurityError(f"Command not in allowlist: {parts[0]}")

        for arg in parts[1:]:
            if any(c in arg for c in _SHELL_META):
                raise SecurityError(f"Argument contains forbidden characters: {arg}")
            if any(pat in arg for pat in _INJECTION_PATTERNS):
                raise SecurityError(f"Argument contains injection pattern: {arg}")

    def _run_sudo(self, cmd: str, *, timeout: int = 15) -> CommandResult:
        """Run cmd under sudo -n through the socket.

        Bypasses _validate_command() — callers must ensure cmd is safe
        (validated path, no user-controlled arguments).
        """
        return self._run_raw(f"sudo -n {cmd}", timeout=timeout)

    def run_command(
        self,
        command: str | Sequence[str],
        *,
        timeout: int = 60,
    ) -> CommandResult:
        if isinstance(command, str):
            cmd_str = command
        else:
            cmd_str = " ".join(shlex.quote(str(c)) for c in command)

        self._validate_command(cmd_str)
        logger.debug("ControlMasterTransport running on %s: %s", self.label, cmd_str)
        return self._run_raw(cmd_str, timeout=timeout)

    # -- sudo escalation --

    def has_passwordless_sudo(self) -> bool:
        if self._sudo_available is not None:
            return self._sudo_available

        result = self._run_raw("sudo -n true 2>/dev/null", timeout=5)
        self._sudo_available = result.return_code == 0
        logger.debug(
            "Passwordless sudo via %s: %s",
            self.label,
            "available" if self._sudo_available else "not available",
        )
        return self._sudo_available

    # -- file operations --

    def read_file(self, path: str, *, encoding: str = "utf-8") -> str:
        try:
            return self._read_direct(path, encoding=encoding)
        except PermissionError:
            if not self.has_passwordless_sudo():
                raise
            logger.debug("Permission denied for %s, retrying with sudo", path)
            return self._read_sudo(path, encoding=encoding)

    def _validate_remote_path(
        self,
        path: str,
        *,
        allowed: tuple = ALLOWED_PREFIXES,
        use_sudo: bool = False,
    ) -> str:
        """Validate and resolve a remote path. Mirrors SSHTransport._validate_remote_path
        so file_size, read_file, and the sudo fallback all enforce the same allowed-prefix
        rule. Symlink rejection is left to the caller (parity with SSHTransport)."""
        if any(c in path for c in _SHELL_META):
            raise SecurityError("Path contains forbidden characters")
        if ".." in Path(path).parts:
            raise SecurityError("Path traversal detected in input")

        runner = self._run_sudo if use_sudo else self.run_command
        result = runner(f"realpath {shlex.quote(path)}", timeout=5)
        if result.return_code != 0:
            raise FileNotFoundError(f"Path does not exist on remote: {path}")
        resolved = result.stdout.strip()

        if allowed and not _is_under_allowed(resolved, allowed):
            raise SecurityError(
                f"Path outside allowed directories: {resolved}",
                details={"requested": path, "resolved": resolved, "allowed": allowed},
            )
        return resolved

    def _read_direct(self, path: str, *, encoding: str = "utf-8") -> str:
        """Read a file through the ControlMaster socket with security checks."""
        # Reject when the INPUT path is a symlink — SSHTransport rejects on input
        # via SFTP lstat; matching that here closes the parity gap.
        if any(c in path for c in _SHELL_META):
            raise SecurityError("Path contains forbidden characters")
        if ".." in Path(path).parts:
            raise SecurityError("Path traversal detected in input")
        lcheck = self.run_command(f"test -L {shlex.quote(path)}", timeout=5)
        if lcheck.return_code == 0:
            raise SecurityError("Refusing to read symlink", details={"path": path})

        resolved = self._validate_remote_path(path)

        scheck = self.run_command(f"stat -c %s {shlex.quote(resolved)}", timeout=5)
        if scheck.return_code != 0:
            raise CollectorError(
                f"Cannot stat remote file: {resolved}",
                details={"stderr": scheck.stderr[:300]},
            )
        try:
            file_size = int(scheck.stdout.strip())
        except ValueError:
            raise CollectorError(f"Unexpected stat output for {resolved}: {scheck.stdout.strip()!r}")
        if file_size > MAX_READ_SIZE_10_MB:
            raise CollectorError(
                f"File too large: {file_size:,} bytes (max {MAX_READ_SIZE_10_MB:,})"
            )

        read_result = self.run_command(f"cat {shlex.quote(resolved)}", timeout=30)
        if read_result.return_code != 0:
            if "permission denied" in read_result.stderr.lower():
                raise PermissionError(f"Permission denied reading {resolved}")
            raise CollectorError(
                f"Cannot read file: {resolved}",
                details={"stderr": read_result.stderr[:300]},
            )
        return read_result.stdout

    def _read_sudo(self, path: str, *, encoding: str = "utf-8") -> str:
        """Read a file via sudo cat through the ControlMaster socket."""
        if any(c in path for c in _SHELL_META):
            raise SecurityError("Path contains forbidden characters")
        if ".." in Path(path).parts:
            raise SecurityError("Path traversal detected in input")
        # Use sudo for the symlink check too — the SSH user may not be able to
        # traverse the parent directory without elevation.
        lcheck = self._run_sudo(f"test -L {shlex.quote(path)}", timeout=5)
        if lcheck.return_code == 0:
            raise SecurityError("Refusing to read symlink", details={"path": path})

        resolved = self._validate_remote_path(path, use_sudo=True)

        scheck = self._run_sudo(f"stat -c %s {shlex.quote(resolved)}", timeout=5)
        if scheck.return_code != 0:
            raise CollectorError(
                f"Cannot stat remote file: {resolved}",
                details={"stderr": scheck.stderr[:300]},
            )
        try:
            file_size = int(scheck.stdout.strip())
        except ValueError:
            raise CollectorError(f"Unexpected stat output for {resolved}: {scheck.stdout.strip()!r}")
        if file_size > MAX_READ_SIZE_10_MB:
            raise CollectorError(f"File too large: {file_size:,} bytes")

        read_result = self._run_sudo(f"cat {shlex.quote(resolved)}", timeout=30)
        if read_result.return_code != 0:
            raise PermissionError(
                f"sudo cat failed for {resolved}: {read_result.stderr.strip()}"
            )
        return read_result.stdout

    def is_exists(self, path: str) -> bool:
        if any(c in path for c in _SHELL_META) or ".." in Path(path).parts:
            return False
        result = self.run_command(f"test -e {shlex.quote(path)}", timeout=5)
        if result.return_code == 0:
            return True
        if self.has_passwordless_sudo():
            result = self._run_sudo(f"test -e {shlex.quote(path)}", timeout=5)
            return result.return_code == 0
        return False

    def is_readable(self, path: str) -> bool:
        if any(c in path for c in _SHELL_META) or ".." in Path(path).parts:
            return False
        result = self.run_command(f"test -r {shlex.quote(path)}", timeout=5)
        if result.return_code == 0:
            return True
        if self.has_passwordless_sudo():
            result = self._run_sudo(f"test -r {shlex.quote(path)}", timeout=5)
            return result.return_code == 0
        return False

    def file_size(self, path: str) -> int:
        resolved = self._validate_remote_path(path)
        result = self.run_command(f"stat -c %s {shlex.quote(resolved)}")
        result.check()
        return int(result.stdout.strip())

# =================================================
# Ping Utility
# =================================================

def ping_ssh(credentials: SSHCredentials) -> tuple[bool, str]:
    """Quick connectivity check without keeping a connection open"""
    try:
        with SSHTransport(credentials) as t:
            result = t.run_command("echo atlas-ping")
            if result.ok and "atlas-ping" in result.stdout:
                return True, f"Connected to {t.label}"
            return False, f"Unexpected response from {t.label}"
    except CollectorConnectionError as e:
        return False, e.message
    except Exception as e:
        return False, f"Connection failed: {type(e).__name__}: {e}"

def transport_from_config(target: dict) -> Transport:
    """Build a Transport instance from a target config dict.

    SSH transports need infrastructure access — Standard tier deployments
    must never construct an SSH connection (Extended and SaaS both may).
    ``LocalTransport`` is permitted in every tier (used by preflight on the
    user's own machine).
    """
    from platform_atlas.core.context import require_infra
    from platform_atlas.core.exceptions import ConfigError

    kind = target.get("transport", "local").lower()

    if kind == "local":
        return LocalTransport()

    if kind == "ssh":
        # Hard mode boundary: SSH must never run in Standard.
        require_infra(
            "transport_from_config(ssh)",
            hint="SSH transport is unavailable in Standard Mode.",
        )
        # User-configurable connect timeout (config.json / `config edit`); clamped
        # to a safe range. Absent/invalid keeps the historical 10s. ctx() is already
        # initialized here — require_infra() above reads it.
        from platform_atlas.core.context import ctx
        _connect_timeout = getattr(ctx().config, "ssh_connect_timeout_s", 10) or 10
        _connect_timeout = float(max(1, min(int(_connect_timeout), 120)))
        creds = SSHCredentials(
            hostname=target["host"],
            username=target.get("username", "atlas"),
            key_path=target.get("key_path"),
            key_passphrase=target.get("key_passphrase"),
            password=target.get("password"),
            port=target.get("port", 22),
            use_agent=target.get("use_agent", True),
            discover_keys=target.get("discover_keys", False),
            host_key_policy=target.get("host_key_policy", "warn"),
            timeout=_connect_timeout,
        )
        transport = SSHTransport(creds)
        transport.connect()
        return transport

    if kind == "control_master":
        require_infra(
            "transport_from_config(control_master)",
            hint="ControlMaster transport is unavailable in Standard Mode.",
        )
        _ssh_target = target.get("ssh_target", "")
        if not _ssh_target:
            node_label = target.get("name", target.get("host", "unknown"))
            raise CollectorConnectionError(
                f"SSH destination not configured for node '{node_label}'",
                details={
                    "suggestion": (
                        "This ControlMaster node has no SSH destination set. "
                        "Edit the environment topology to add it:\n"
                        "  platform-atlas env edit <env-name>"
                    )
                },
            )
        cm_config = ControlMasterConfig(
            socket_path=target.get("control_socket", ""),
            ssh_target=_ssh_target,
            port=target.get("port", 22),
        )
        transport = ControlMasterTransport(cm_config)
        transport.connect()
        return transport

    raise ConfigError(
        f"Unknown transport type: {kind}",
        details={"valid_options": "local, ssh, control_master"}
    )
