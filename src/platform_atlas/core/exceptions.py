"""
Custom exceptions for Atlas
"""
from enum import Enum

class AtlasError(Exception):
    """Base exception for all Atlas errors

    ``code`` is a stable, machine-readable identifier (override per class or pass
    ``code=`` at the raise site) so callers such as the WebUI can branch on it
    without matching message text. ``hint`` is the user-facing next step, taken
    from ``details["fix"]`` when the raise site supplied one.
    """

    code: str = "atlas_error"

    def __init__(self, message: str, details: dict | None = None, *, code: "str | Enum | None" = None):
        self.message = message
        self.details = details or {}
        if code is not None:
            # ``str(Enum)`` returns "Class.MEMBER" for str-mixin enums, so take the value.
            self.code = str(getattr(code, "value", code))
        super().__init__(self.message)

    def __str__(self):
        return self.message

    @property
    def hint(self) -> str:
        """User-facing next step: an explicitly set hint, else ``details["fix"]``, else ``""``."""
        explicit = self.__dict__.get("_hint")
        if explicit:
            return str(explicit)
        return str(self.details.get("fix", "")) if isinstance(self.details, dict) else ""

    @hint.setter
    def hint(self, value: str) -> None:
        # Subclasses such as TierViolationError assign ``self.hint`` directly.
        self.__dict__["_hint"] = value

    def to_dict(self) -> dict:
        """Stable, secret-free summary for APIs and UIs (no ``details`` payload)."""
        return {"code": self.code, "message": self.message, "hint": self.hint}

    def format_user_message(self) -> str:
        """Format a user-friendly error message"""
        msg = f"ERROR: {self.message}"
        if self.details:
            msg += "\n\nDetails:"
            for key, value in self.details.items():
                msg += f"\n  • {key}: {value}"
        return msg

class SecurityWarning(UserWarning):
    """Warning for security-related issues that don't prevent execution"""

class SecurityError(AtlasError):
    """Raised when a security violation is detected"""

class JSONSecurityError(AtlasError):
    """Raised when JSON parsing encounters security limits"""

class RulesetError(AtlasError):
    """Errors related to ruleset loading/parsing"""

class ValidationError(AtlasError):
    """Errors during validation execution"""

class CollectorError(AtlasError):
    """Errors during data collection"""

class ConfigError(AtlasError):
    """Configuration-related errors"""

class CollectorNotFoundError(CollectorError):
    """Collector class not found or not registered"""

class CollectorConfigError(CollectorError):
    """Invalid collector configuration"""

class CollectorConnectionError(CollectorError):
    """Cannot connect to data source"""

class CollectorDataError(CollectorError):
    """Data collection failed or returned invalid data"""

class EncryptedKeyError(CollectorConnectionError):
    """Raised when SSH key is encrypted and no phrase provided"""

class MongoCollectorError(CollectorError):
    """Base exception for MongoCollector operations"""

class MongoConnectionNotEstablishedError(MongoCollectorError):
    """Raised when an operation requires a connection that doesn't exist"""

class URIParseError(MongoCollectorError):
    """Raised when the MongoDB URI cannot be parsed"""

class AuthenticationError(MongoCollectorError):
    """Raised when MongoDB authentication fails"""

class QueryTimeoutError(MongoCollectorError):
    """Raised when a query exceeds the configured timeout"""

class InsufficientPermissionsError(MongoCollectorError):
    """Raised when the MongoDB user lacks required permissions"""

class RedisCollectorError(CollectorError):
    """Base exception for RedisCollector operations"""

class RedisConnectionNotEstablishedError(RedisCollectorError):
    """Raised when an operation requires a connection that doesn't exist"""

class SessionError(AtlasError):
    """Base exception for session operations"""

class SessionNotFoundError(SessionError):
    """Session does not exist"""

class SessionAlreadyExistsError(SessionError):
    """Session with this name already exists"""

class SessionInvalidStateError(SessionError):
    """Operation not allowed in current session state"""

class NoActiveSessionError(SessionError):
    """No active session set"""

class SessionLockedError(SessionError):
    """Another process holds the exclusive lock on this session."""

class CredentialErrorCode(str, Enum):
    """Stable codes for :class:`CredentialError` (``err.code`` holds the value)."""

    UNKNOWN = "credential_error"
    BACKEND_UNREACHABLE = "backend_unreachable"   # Vault down, DNS/TLS/connection failure
    AUTH_FAILED = "auth_failed"                   # reached the backend but it refused us (bad/expired token)
    NOT_CONFIGURED = "not_configured"             # backend settings missing from the credential store
    MISSING_KEY = "missing_key"                   # reachable, but the requested secret is not stored
    READ_ONLY = "read_only"                       # write/delete against a read-only backend (Vault)
    INSECURE_BACKEND = "insecure_backend"         # keyring substrate considered unsafe


class CredentialError(AtlasError):
    """Raised when a credential operation fails"""

    code = CredentialErrorCode.UNKNOWN.value

class InsecureBackendError(CredentialError):
    """Raised when the keyring backend is not secure"""

    code = CredentialErrorCode.INSECURE_BACKEND.value

class TierViolationError(AtlasError):
    """
    Raised when Extended-only code is invoked while the active tier is Standard.

    This is a programming error, not a user error — Standard-mode workflows
    must never reach Extended-only code paths. The hard mode boundary is
    enforced by registry pruning at AtlasContext init plus require_extended()
    guards inside Extended-only collectors and transports.
    """
    def __init__(self, component: str, *, hint: str = "") -> None:
        msg = f"'{component}' requires Extended Mode (current tier: standard)"
        if hint:
            msg += f". {hint}"
        super().__init__(msg)
        self.component = component
        self.hint = hint


class CaptureAborted(AtlasError):
    """Raised when capture is stopped by a user-requested cooperative shutdown."""
