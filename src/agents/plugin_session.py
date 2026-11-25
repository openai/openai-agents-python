"""Local commands for trusted application input."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from .agent import Agent

T = TypeVar("T")


@dataclass(frozen=True)
class Command(Generic[T]):
    """Optional help, suggested arguments and shortcut for a local command."""

    run: Callable[[T, str], str | Awaitable[str]]
    description: str = ""
    shortcut: str | None = None
    complete: Callable[[T, str], Sequence[str]] | None = None

    def __call__(self, context: T, arguments: str) -> str | Awaitable[str]:
        return self.run(context, arguments)


def command_catalog(agent: Agent[T]) -> dict[str, Command[T]]:
    """Return qualified commands. The application decides which keys to bind."""
    return {
        f"/{plugin.name}:{key}": callback if isinstance(callback, Command) else Command(callback)
        for plugin in agent.plugins
        for key, callback in plugin.commands.items()
    }


async def run_command(agent: Agent[T], line: str, *, context: T) -> str:
    """Run /name or /plugin:name locally. Pass only explicit user commands here."""
    parts = line.split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        raise ValueError("Expected a slash command")
    matches = [
        command
        for path, command in command_catalog(agent).items()
        if parts[0] == path or parts[0][1:] == path.partition(":")[2]
    ]
    if not matches:
        raise ValueError("Unknown command")
    if len(matches) > 1:
        raise ValueError("Ambiguous command. Use /plugin:name")
    result = matches[0](context, parts[1] if len(parts) == 2 else "")
    return await result if inspect.isawaitable(result) else result
