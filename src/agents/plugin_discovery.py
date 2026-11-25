"""Experimental discovery for trusted installed Python plugins. No implicit activation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from importlib import metadata
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from .exceptions import UserError
from .plugins import Plugin

__all__ = ["InstalledPlugin", "installed_plugins"]

_GROUP = "openai_agents.plugins"


@dataclass(frozen=True, kw_only=True, order=True)
class InstalledPlugin:
    """Exact installed metadata for a named entry point in openai_agents.plugins."""

    distribution: str
    version: str
    name: str
    value: str

    def load(self) -> Plugin[Any]:
        """Import a trusted plugin for use in Agent(plugins=[...]).

        Use an installed wheel exporting a Plugin at a top-level module:attribute.
        Trust the package, its dependencies and the Python environment. Metadata
        and path checks cannot authenticate code. Restart after package changes.
        """
        matches = [candidate for candidate in installed_plugins() if candidate.name == self.name]
        if len(matches) > 1:
            raise UserError("Ambiguous installed plugin name.")
        if matches != [self]:
            raise UserError("Missing or changed installed plugin.")
        distributions = list(metadata.distributions(name=self.distribution))
        if len(distributions) != 1:
            raise UserError("Missing or ambiguous plugin distribution.")
        (dist,) = distributions
        entries = [entry for candidate, entry in _entries(dist) if candidate == self]
        if len(entries) != 1:
            raise UserError("Missing or changed installed plugin.")
        (entry,) = entries
        module, _, attr = entry.value.partition(":")
        if not module.isidentifier() or not attr.isidentifier():
            raise UserError("Expected a top-level module:attribute plugin entry point.")
        spec = find_spec(module)
        if (
            not spec
            or not spec.origin
            or not any(
                Path(str(dist.locate_file(file))).resolve() == Path(spec.origin).resolve()
                for file in dist.files or ()
                if str(file) in (f"{module}.py", f"{module}/__init__.py")
            )
        ):
            raise UserError(
                "The plugin module must resolve to a file in the selected distribution."
            )
        plugin = entry.load()
        if not isinstance(plugin, Plugin) or plugin.name != self.name:
            raise UserError("Expected a Plugin with the entry point's name.")
        return plugin


def _entries(dist: metadata.Distribution) -> Iterator[tuple[InstalledPlugin, metadata.EntryPoint]]:
    try:
        entries = dist.entry_points.select(group=_GROUP)
        if not entries:
            return
        name, version = dist.name, dist.version
    except (KeyError, TypeError, ValueError):
        # Ignore distributions whose metadata cannot describe a selection.
        return
    if name and version:
        for entry in entries:
            yield (
                InstalledPlugin(
                    distribution=name, version=version, name=entry.name, value=entry.value
                ),
                entry,
            )


def installed_plugins() -> tuple[InstalledPlugin, ...]:
    """List metadata only. Keep duplicates visible and reject them on selection."""
    # metadata.entry_points() hides duplicate distribution installs.
    return tuple(sorted(item for dist in metadata.distributions() for item, _ in _entries(dist)))
