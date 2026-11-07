"""Classic consumer views of the items accepted by one native run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Generic, TypeVar, cast

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseReasoningItem,
)
from openai.types.responses.response_input_item_param import FunctionCallOutput

from _agents_codex._lifecycle import _close_after_error, _error_contexts
from _agents_codex.agent import NativeProvider
from _agents_codex.session import Completed, Session

from ..classic.agent import Agent
from ..classic.exceptions import ModelBehaviorError, UserError
from ..classic.items import (
    ItemHelpers,
    MessageOutputItem,
    ModelResponse,
    ReasoningItem,
    RunItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from ..classic.result import ToInputListMode
from ..classic.run_context import RunContextWrapper
from ..classic.run_internal.items import run_items_to_input_items
from ..classic.stream_events import AgentUpdatedStreamEvent, RunItemStreamEvent, StreamEvent
from ..classic.tool import FunctionTool, ToolOrigin, get_function_tool_origin

ContextT = TypeVar("ContextT")


def get_cleanup(error: BaseException) -> Session[Any, Any] | NativeProvider | None:
    """Return the migration run's retained owner after failed cleanup, if any.

    Await the owner's ``aclose()`` to retry retirement. This also works after
    Python 3.10 Tasks replace cancellation exceptions: their contexts retain
    the original error and its cleanup owner. A retained Session closes its
    migration-owned provider too; do not close that provider separately.
    """
    for current in _error_contexts(error):
        owner = getattr(current, "cleanup", None)
        if isinstance(owner, (Session, NativeProvider)):
            return owner
    return None


@dataclass(frozen=True)
class SDKRunResult(Generic[ContextT]):
    """Settled migration result using Classic item, context and response types.

    The three-argument constructor leaves Classic observations empty and
    `context_wrapper` set to None. SDKRunner supplies its actual run context.
    """

    final_output: str
    last_agent: Agent[ContextT]
    native_result: Completed[str]
    input: str | list[TResponseInputItem] = field(default_factory=list, repr=False, kw_only=True)
    new_items: list[RunItem] = field(default_factory=list, repr=False, kw_only=True)
    raw_responses: list[ModelResponse] = field(default_factory=list, repr=False, kw_only=True)
    context_wrapper: RunContextWrapper[ContextT | None] | None = field(
        default=None, repr=False, kw_only=True
    )
    # Live approvals settle inside the invocation before it returns a result.
    interruptions: list[ToolApprovalItem] = field(default_factory=list, init=False)

    @property
    def last_response_id(self) -> str | None:
        """The last response's original Model ID, when supplied by the Model."""
        return self.raw_responses[-1].response_id if self.raw_responses else None

    def _new_input_items(self) -> list[TResponseInputItem]:
        return deepcopy(run_items_to_input_items(self.new_items))

    def to_input_list(self, *, mode: ToInputListMode = "preserve_all") -> list[TResponseInputItem]:
        """Export the effective input and current-run items as detached Responses items."""
        if mode not in ("preserve_all", "normalized"):
            raise UserError("SDKRunResult.to_input_list requires preserve_all or normalized mode")
        return deepcopy(ItemHelpers.input_to_new_input_list(self.input)) + self._new_input_items()

    async def replay_events(self) -> AsyncIterator[StreamEvent]:
        """Replay accepted semantic events. Raw Model frames and deltas are unavailable."""
        yield AgentUpdatedStreamEvent(new_agent=self.last_agent)
        for item in self.new_items:
            if isinstance(item, MessageOutputItem):
                yield RunItemStreamEvent(item=item, name="message_output_created")
            elif isinstance(item, ToolCallItem):
                yield RunItemStreamEvent(item=item, name="tool_called")
            elif isinstance(item, ToolCallOutputItem):
                yield RunItemStreamEvent(item=item, name="tool_output")
            elif isinstance(item, ReasoningItem):
                yield RunItemStreamEvent(item=item, name="reasoning_item_created")


class _ResultProjection(Generic[ContextT]):
    """Collect per-invocation model outputs and native-confirmed tool results."""

    def __init__(
        self,
        agent: Agent[ContextT],
        context_wrapper: RunContextWrapper[ContextT | None],
        effective_input: str | list[TResponseInputItem],
        *,
        tools: Sequence[FunctionTool],
    ) -> None:
        self._agent = agent
        self._context_wrapper = context_wrapper
        self._input = deepcopy(effective_input)
        self._tool_metadata = {
            tool.name: (tool.description, get_function_tool_origin(tool)) for tool in tools
        }
        self._items: list[RunItem] = []
        self._responses: list[ModelResponse] = []
        self._pending_calls: dict[str, ToolOrigin | None] = {}

    def accept_prepared_input(self, items: list[TResponseInputItem]) -> None:
        if not self._pending_calls:
            return
        receipts: dict[str, ToolCallOutputItem] = {}
        for item in items:
            if item.get("type") != "function_call_output":
                continue
            receipt = cast(FunctionCallOutput, item)
            call_id = receipt.get("call_id")
            if call_id is None or call_id not in self._pending_calls:
                continue
            if call_id in receipts:
                raise ModelBehaviorError("SDKRunner observed a duplicate function output receipt")
            match receipt["output"]:
                case str() as value:
                    pass
                case [{"type": "input_text", "text": str() as value}]:
                    pass
                case _:
                    raise ModelBehaviorError("SDKRunner requires a text function output receipt")
            raw_item = deepcopy(receipt)
            # Classic supplied the call ID and the callback's text. Core owns the receipt ID.
            raw_item.pop("id", None)
            receipts[call_id] = ToolCallOutputItem(
                agent=self._agent,
                raw_item=raw_item,
                output=value,
                tool_origin=self._pending_calls[call_id],
            )
        if receipts.keys() != self._pending_calls.keys():
            raise ModelBehaviorError("SDKRunner did not observe each accepted function output")
        self._items.extend(receipts.values())
        self._pending_calls.clear()

    def accept_response(self, response: ModelResponse) -> None:
        snapshot = deepcopy(response)
        self._responses.append(snapshot)
        for output in snapshot.output:
            if isinstance(output, ResponseOutputMessage):
                self._items.append(MessageOutputItem(agent=self._agent, raw_item=deepcopy(output)))
            elif isinstance(output, ResponseReasoningItem):
                self._items.append(ReasoningItem(agent=self._agent, raw_item=deepcopy(output)))
            elif isinstance(output, ResponseFunctionToolCall):
                description, origin = self._tool_metadata[output.name]
                self._pending_calls[output.call_id] = origin
                self._items.append(
                    ToolCallItem(
                        agent=self._agent,
                        raw_item=deepcopy(output),
                        description=description,
                        tool_origin=origin,
                    )
                )
            else:
                raise ModelBehaviorError("SDKRunner received an unsupported model output item")

    def finish(self, completed: Completed[str]) -> SDKRunResult[ContextT]:
        if self._pending_calls:
            raise ModelBehaviorError("SDKRunner completed with an unobserved function output")
        return SDKRunResult(
            completed.final_output,
            self._agent,
            completed,
            input=self._input,
            new_items=self._items,
            raw_responses=self._responses,
            context_wrapper=self._context_wrapper,
        )


class SDKRunHandle(Generic[ContextT]):
    """An in-flight SDKRunner run with buffered semantic events."""

    def __init__(self, run: Coroutine[Any, Any, SDKRunResult[ContextT]]) -> None:
        self._cancel_requested = False
        self._cancelled_error: asyncio.CancelledError | None = None
        self._task = asyncio.create_task(run)
        # A run can settle without an event reader. Retain the exception on the
        # task for get_result()/replay_events() without logging an orphan failure.
        self._task.add_done_callback(self._observe_failure)

    @staticmethod
    def _observe_failure(task: asyncio.Task[SDKRunResult[ContextT]]) -> None:
        if not task.cancelled():
            task.exception()

    @property
    def is_complete(self) -> bool:
        """Whether execution and all owned cleanup have settled."""
        return self._task.done()

    @property
    def final_output(self) -> str | None:
        """Return the final text on success, None while running, or raise the run's error."""
        return self._result().final_output if self._task.done() else None

    def cancel(self) -> None:
        """Request cancellation. Await get_result() to join cleanup."""
        if not self._task.done() and not self._cancel_requested:
            self._cancel_requested = True
            self._task.cancel()

    def _result(self) -> SDKRunResult[ContextT]:
        if self._task.cancelled():
            if self._cancelled_error is None:
                try:
                    self._task.result()
                except asyncio.CancelledError as error:
                    # Later task reads can manufacture a new cancellation and
                    # lose the original error's public cleanup capability.
                    self._cancelled_error = error
            assert self._cancelled_error is not None
            # Raising inside the shield's cancellation handler can replace this
            # error's context too, including an error cached by _settle().
            owner = get_cleanup(self._cancelled_error)
            if owner is not None:
                cast(Any, self._cancelled_error).cleanup = owner
            raise self._cancelled_error
        result = self._task.result()
        # The execution owner may finish an awaited operation that suppressed cancellation.
        if self._cancel_requested:
            raise asyncio.CancelledError
        return result

    async def _settle(self, primary: BaseException) -> None:
        try:
            await self._task
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                error = self._cancelled_error or error
                self._cancelled_error = error
            for attribute in ("cleanup", "provider"):
                owner = getattr(error, attribute, None)
                if owner is not None:
                    setattr(primary, attribute, owner)
            raise

    async def get_result(self) -> SDKRunResult[ContextT]:
        """Await settlement. Cancelling this wait stops and joins the run."""
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError as error:
            if self._task.cancelled():
                return self._result()
            self.cancel()
            await _close_after_error(error, partial(self._settle, error))
            raise
        return self._result()

    async def replay_events(self) -> AsyncIterator[StreamEvent]:
        """Yield buffered semantic events after success, or raise the run's error."""
        result = await self.get_result()
        async for event in result.replay_events():
            yield event
