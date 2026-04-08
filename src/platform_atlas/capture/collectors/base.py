"""Base collector class with shared connection management"""

from abc import ABC, abstractmethod
from typing import Any, Self, TypeVar, Generic

S = TypeVar("S") # Settings type

class BaseCollector(ABC, Generic[S]):
    """Abstract base class for all collectors"""

    _client: Any = None
    _settings: S

    def __init__(self, *, settings: S | None = None):
        self._settings = settings or self._default_settings()
        self._client = None

    @classmethod
    @abstractmethod
    def _default_settings(cls) -> S:
        """Return default settings for this collector"""
        ...

    @classmethod
    def from_config(cls, *, settings: S | None = None) -> Self:
        """Create collector from application config"""
        return cls(settings=settings)

    @property
    def settings(self) -> S:
        return self._settings

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @abstractmethod
    def connect(self) -> None:
        """Establish connection a data source"""
        ...

    @abstractmethod
    def collect(self) -> dict[str, Any]:
        """Gather data from the source"""
        ...

    def close(self) -> None:
        """Close connection and release resources. Override in sublasses"""
        self._client = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
