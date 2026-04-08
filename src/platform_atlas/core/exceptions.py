"""
Custom exceptions for Atlas
"""

class AtlasError(Exception):
    """Base exception for all Atlas errors"""
    def __init__(self, message: str, details: dict | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)

    def __str__(self):
        return self.message

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

class CredentialError(AtlasError):
    """Raised when a credential operation fails"""

class InsecureBackendError(CredentialError):
    """Raised when the keyring backend is not secure"""
