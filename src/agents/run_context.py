from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, Mapping, Optional

from typing_extensions import TypeVar

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

    # Internal emitter for streaming custom tool events; set by the Runner in streaming mode.
    _emit_fn: Optional[Callable[[Any], Awaitable[None]]] = field(default=None, repr=False)
    # Current agent reference for constructing RunItem wrappers; set by the Runner.
    _current_agent: Any = field(default=None, repr=False)

    async def emit_event(self, event: Mapping[str, Any]) -> None:
        """
        Emit a developer-defined event dict via the run's main stream.
        The dict should include at least a 'type' key. The event will be forwarded
        as a RunItemStreamEvent(name='tool_event', item.raw_item=event).

        No-op if not in streaming mode.
        """
        if not self._emit_fn or not isinstance(event, Mapping) or not event.get("type"):
            return
        # Lazy import to avoid circular dependencies at module import time
        from .items import ToolCallItem
        from .stream_events import RunItemStreamEvent

        item = ToolCallItem(raw_item=dict(event), agent=self._current_agent)
        await self._emit_fn(RunItemStreamEvent(name="tool_event", item=item))
