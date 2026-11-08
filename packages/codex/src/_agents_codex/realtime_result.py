"""Trusted function-result controls, separate from application content."""

from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar

from pydantic import ConfigDict

from ._public import preserve_module_identity as _preserve_module_identity

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class BackgroundResult(Generic[_ResultT]):
    """A validated tool result that does not request another Realtime response.

    Annotate the callback's return as ``BackgroundResult[T]`` or a union with
    its ordinary result type. Result policies receive the ordinary ToolOutput.
    Returning that same output preserves this control; replacing it clears it.
    A policy can return ``background_result(output)`` to set it explicitly.
    """

    value: _ResultT = field(repr=False)
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(revalidate_instances="always")


def background_result(value: _ResultT) -> BackgroundResult[_ResultT]:
    """Return content without automatically starting a Realtime response."""
    return BackgroundResult(value)


_preserve_module_identity(globals(), "agents.realtime")
