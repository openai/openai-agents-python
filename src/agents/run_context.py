from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Generic

from typing_extensions import TypeVar

from .items import RunItem, TResponseInputItem
from .usage import Usage

TContext = TypeVar("TContext", default=Any)


@dataclass
class RunContextWrapper(Generic[TContext]):
    """This wraps the context object that you passed to `Runner.run()`. It also contains
    information about the usage of the agent run so far.

    NOTE: Contexts are not passed to the LLM. They're a way to pass dependencies and data to code
    you implement, like tool functions, callbacks, hooks, etc.
    """

    context: TContext
    """The context object (or None), passed by you to `Runner.run()`"""

    usage: Usage = field(default_factory=Usage)
    """The usage of the agent run so far. For streamed responses, the usage will be stale until the
    last chunk of the stream is processed.
    """

    _new_items: list[RunItem] = field(default_factory=list, repr=False)
    """The new items created during the agent run."""

    _input: str | list[TResponseInputItem] = field(default_factory=list, repr=False)
    """The original input that you passed to `Runner.run()`."""

    @property
    def input(self) -> str | list[TResponseInputItem]:
        """The original input that you passed to `Runner.run()`."""

        return copy.deepcopy(self._input)

    @property
    def new_items(self) -> list[RunItem]:
        """The new items created during the agent run."""

        return copy.deepcopy(self._new_items)
