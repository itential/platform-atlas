"""Command dispatch registry with decorator-based auto-registration"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

# Handle alias for handler signature
HandlerFn = Callable[["Namespace"], int]

@dataclass(frozen=True, slots=True)
class CommandDef:
    """Metadata for a registered CLI command"""
    path: tuple[str, ...]
    handler: HandlerFn
    description: str = ""
    hidden: bool = False

class CommandRegistry:
    """
    Stores and resolves CLI command handlers

    Usage:
        registry = CommandRegistry()

        @registry.register("session", "create", description="Create a session")
        def handle_session_create(args: Namespace) -> int:
            ...

        # Dispatch
        cmd = registry.resolve(("session", "create"))
        cmd.handler(args)
    """

    def __init__(self) -> None:
        self._commands: dict[tuple[str, ...], CommandDef] = {}

    def register(
        self,
        *path: str,
        description: str = "",
        hidden: bool = False
    ):
        """
        Decorator that registers a handler function for a command path.

        Args:
            *path: Command path segments, e.g. "session", "create"
            description: Human-readable description (used in help/docs)
            hidden: if True, command is omitted from the help listings

        Returns:
            The original function, unmodified

        Raises:
            ValueError: If the path is already registered
        """
        def decorator(fn: HandlerFn) -> HandlerFn:
            if path in self._commands:
                existing = self._commands[path]
                raise ValueError(
                    f"Command path {path!r} already registered "
                    f"to {existing.handler.__name__!r}"
                )
            self._commands[path] = CommandDef(
                path=path,
                handler=fn,
                description=description,
                hidden=hidden,
            )
            return fn

        return decorator


    # Lookup
    def resolve(self, path: tuple[str, ...]) -> CommandDef | None:
        """Look up a command by its full path tuple"""
        return self._commands.get(path)

    def all_commands(self) -> list[CommandDef]:
        """Return all registered commands sorted by path (for help screens)"""
        return sorted(
            (c for c in self._commands.values() if not c.hidden),
            key=lambda c: c.path,
        )

    def commands_under(self, prefix: str) -> list[CommandDef]:
        """Return all commands that start with a given top-level prefix"""
        return sorted(
            (c for c in self._commands.values()
             if c.path[0] == prefix and not c.hidden),
            key=lambda c: c.path,
        )

    def __len__(self) -> int:
        return len(self._commands)

    def __contains__(self, path: tuple[str, ...]) -> bool:
        return path in self._commands

    def __repr__(self) -> str:
        return f"CommandRegistry({len(self._commands)} commands)"

registry = CommandRegistry()
