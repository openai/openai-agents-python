"""Passive observations of a native Session.

Sequences belong to one Session. Observations are retained in memory (a bounded
suffix by default), not a durable log. Use Turn.get_result and canonical history
for authoritative results.
"""

from __future__ import annotations

import asyncio as asyncio
import json as json
import weakref as weakref
from collections import deque as deque
from collections.abc import AsyncIterator as AsyncIterator, Callable as Callable
from copy import deepcopy as deepcopy
from dataclasses import dataclass as dataclass, field as field, fields as fields, replace as replace
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Literal as Literal,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
    cast as cast,
)

from typing_extensions import TypedDict as TypedDict

from agents.config import JsonValue as JsonValue

from ._events import (
    CodeInterpreterCallItem,
    ExecutionItem as ExecutionItem,
    FileSearchCallItem,
    FunctionCallStatus as FunctionCallStatus,
    HostedMcpCallItem,
    ImageGenerationCallItem,
    McpListToolsItem,
    MessageItem,
    ProgramCallItem,
    ProgramOutputItem,
    ReasoningItem,
    ShellCallItem,
    ShellCallOutputItem,
    WebSearchCallItem,
)
from ._mcp_errors import _protect_mcp_methods
from ._public import preserve_module_identity as _preserve_module_identity
from .rate_limits import ApiRateLimits as ApiRateLimits
from .response_facts import (
    RecordedResponseFact as RecordedResponseFact,
    RecordedResponseFactSummary as RecordedResponseFactSummary,
    _summarize_response_fact,
)
from .tools import ToolOutput as ToolOutput
from .usage import TokenUsageInfo as TokenUsageInfo

if TYPE_CHECKING:
    from .turn import Turn


@dataclass(frozen=True, slots=True, kw_only=True)
class _Event:
    session_id: str
    sequence: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class _SubmissionEvent(_Event):
    """An actual submission; parent_submission_id links only derived repair work."""

    submission_id: str
    turn_id: str
    parent_submission_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelRequestDispatching(_SubmissionEvent):
    """Resolved root sampling fields before entering a transport client API.

    This may precede encoding failure or zero socket writes. A record does not
    prove provider receipt, success or billing. None means the wire field is
    omitted, with server defaults unknown. Transport retries share request_id;
    later client invocations have new IDs. Metadata is bounded and best effort.
    Separately managed correction roots keep their own thread/submission IDs and
    the existing parent_submission_id link. Native children, warmup and compaction
    and background/reviewer requests are outside this event's coverage. Restored
    manual roots emit fresh events for resumed work; prior events are not replayed.
    """

    request_id: str
    thread_id: str
    model: str
    reasoning_effort: str | int | None
    reasoning_summary: str | None
    service_tier: str | None
    wire: Literal["responses", "responses_lite"]
    transport: Literal["http_sse", "http_json", "websocket"]
    type: Literal["model_request_dispatching"] = field(
        default="model_request_dispatching", init=False
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelResponseLinked(_SubmissionEvent):
    """Best-effort link to one retained response occurrence, independent of provider ID.

    Cancellation or retirement can suppress this link after history retention.
    Its absence does not prove that no terminal was retained. Links are transient.
    """

    request_id: str
    thread_id: str
    response_observation_id: str
    type: Literal["model_response_linked"] = field(default="model_response_linked", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnStarted(_SubmissionEvent):
    """Native accepted this exact submission."""

    type: Literal["turn_started"] = field(default="turn_started", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnFinished(_SubmissionEvent):
    """The submission and its owned callback cleanup have settled."""

    status: Literal["completed", "failed", "cancelled"]
    error_type: str | None = None
    type: Literal["turn_finished"] = field(default="turn_finished", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputAccepted(_SubmissionEvent):
    """One successful logical root result after typed validation and owned cleanup.

    submission_id identifies the original root; turn_id identifies its final native
    step. Content remains on Turn.get_result(). Application approval and delivery
    stay separate. Passive retention can report gaps; this is not a durable receipt.
    """

    type: Literal["output_accepted"] = field(default="output_accepted", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputTextDelta(_SubmissionEvent):
    """A native output-text delta with its original native item identity."""

    item_id: str
    delta: str
    type: Literal["output_text_delta"] = field(default="output_text_delta", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponseFactReceived(_SubmissionEvent):
    """Private received facts; durable history remains available after failure.

    A probability delta refers to provider text, not a rendered output segment.
    """

    record: RecordedResponseFact | RecordedResponseFactSummary = field(repr=False)
    type: Literal["response_fact"] = field(default="response_fact", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class RawProviderEvent(_SubmissionEvent):
    """Provider JSON on the exact native turn observation stream."""

    source: Literal["responses", "chat_completions"]
    transport: Literal["sse", "websocket", "json"]
    payload: dict[str, JsonValue] = field(repr=False)
    type: Literal["raw_provider_event"] = field(default="raw_provider_event", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class HostedToolCallSummary:
    """Identity and status of a hosted call, without provider payloads."""

    type: Literal[
        "file_search_call",
        "code_interpreter_call",
        "image_generation_call",
        "shell_call",
        "shell_call_output",
        "mcp_list_tools",
        "mcp_call",
        "program",
        "program_output",
    ]
    id: str
    turn_id: str
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelOutputItemCompleted(_SubmissionEvent):
    """A completed assistant message, provider reasoning summary, or hosted call.

    Open the Session with include_model_output_items=True to record ordinary
    assistant messages and provider reasoning summaries.

    FileSearch, CodeInterpreter, ImageGeneration, and program items default to
    HostedToolCallSummary. Read with
    include_execution_details=True to receive their complete typed items.
    Citation offsets refer to the individual text part. Use Session.items()
    to recover recorded items if the event buffer drops an observation.
    """

    item: (
        MessageItem
        | ReasoningItem
        | WebSearchCallItem
        | McpListToolsItem
        | HostedMcpCallItem
        | ProgramCallItem
        | ProgramOutputItem
        | FileSearchCallItem
        | CodeInterpreterCallItem
        | ShellCallItem
        | ShellCallOutputItem
        | ImageGenerationCallItem
        | HostedToolCallSummary
    )
    type: Literal["model_output_item_completed"] = field(
        default="model_output_item_completed", init=False
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class HostedToolCallProgress(_SubmissionEvent):
    """A provider lifecycle, code, or partial-image event.

    By default, event contains only the provider event type. Read with
    include_execution_details=True to receive its complete provider payload.
    This passive stream can report gaps. Partial images are previews; read the
    completed image item from Session.items() for the authoritative result.
    """

    item_id: str
    event: dict[str, JsonValue]
    type: Literal["hosted_tool_call_progress"] = field(
        default="hosted_tool_call_progress", init=False
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageUpdated(_SubmissionEvent):
    """A native token-count snapshot emitted while this submission was active.

    Usage may repeat when native rate limits update. None means native usage
    is not available; it does not mean zero. Do not add these snapshots.
    """

    usage: TokenUsageInfo | None
    type: Literal["usage_updated"] = field(default="usage_updated", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiRateLimitsUpdated(_SubmissionEvent):
    """Rate-limit fields reported on a provider response for this turn."""

    snapshot: ApiRateLimits
    type: Literal["api_rate_limits_updated"] = field(default="api_rate_limits_updated", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompacted(_SubmissionEvent):
    """Core completed this native context-compaction item."""

    item_id: str
    type: Literal["context_compacted"] = field(default="context_compacted", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentProfileChanged(_Event):
    """A committed change of captured Agent identity, in native commit order.

    Initial/restored bindings and same-profile configuration refreshes are silent.
    Idle updates have no submission or turn. Names come from the admitted graph,
    independently of later mutations to the original public Agent objects.
    The previous profile identifies the committed source, including unnamed Agents.
    Older native events can omit that identity.
    """

    profile_id: str
    profile_epoch: int
    agent_name: str | None
    submission_id: str | None = None
    turn_id: str | None = None
    previous_profile_id: str | None = None
    previous_agent_name: str | None = None
    type: Literal["agent_profile_changed"] = field(default="agent_profile_changed", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStarted(_SubmissionEvent):
    """A function was admitted, or native MCP handling began before any approval.

    MCP start does not prove that the server has executed the call.
    """

    call_id: str
    name: str
    namespace: str | None
    family: Literal["function", "mcp"] = "function"
    type: Literal["tool_started"] = field(default="tool_started", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFinished(_SubmissionEvent):
    """A function settled, or Core delivered an MCP end with its result status.

    MCP cancellation may have no end event. Status does not imply effect rollback.
    With include_execution_details=True, output is the acknowledged post-hook
    function callback response. It is absent for other completions and cancelled
    delivery. The recorded conversation remains authoritative for model input.
    """

    call_id: str
    name: str
    namespace: str | None
    status: Literal["completed", "failed", "cancelled"]
    family: Literal["function", "mcp"] = "function"
    output: ToolOutput | None = None
    type: Literal["tool_finished"] = field(default="tool_finished", init=False)


class _ExecutionItemReference(TypedDict):
    type: Literal["command_execution", "patch_execution", "mcp_call"]
    id: str
    call_id: str
    thread_id: str | None
    turn_id: str
    status: FunctionCallStatus
    native_status: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionItemCompleted(_SubmissionEvent):
    """Core reported the end of a command, patch, or MCP call.

    By default, the item contains only identity and status. Use
    include_execution_details=True on the reader to receive sensitive payloads.
    Match the item's thread, turn, and call IDs to its retained record in
    Session.items() after an EventGap or reopen. Pass subagent_id for a child
    thread. Core bounds command output and MCP results before retention.
    Calls interrupted before Core reports a completion may have no item.
    """

    item: ExecutionItem | _ExecutionItemReference
    type: Literal["execution_item_completed"] = field(
        default="execution_item_completed", init=False
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentLifecycle(_Event):
    """A change in the native spawn graph, or a restored graph snapshot.

    Use kind="created" to distinguish a new spawn from existing descendants
    reported when a Session opens. status describes the graph edge, not whether
    the child currently has a loaded runtime.
    """

    subagent_id: str
    parent_agent_id: str
    status: str
    kind: Literal["created", "active", "closed", "snapshot"] = "snapshot"
    type: Literal["subagent_lifecycle"] = field(default="subagent_lifecycle", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionEnded(_Event):
    """No further observations will arrive; the Session context still owns cleanup."""

    reason: Literal["closed", "failed"]
    type: Literal["session_ended"] = field(default="session_ended", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class EventGap(_Event):
    """Global sequences first_sequence through sequence are no longer retained.

    The gap does not mean that native work or authoritative history was lost.
    """

    first_sequence: int
    type: Literal["gap"] = field(default="gap", init=False)


Observation: TypeAlias = (
    TurnStarted
    | AgentProfileChanged
    | ModelRequestDispatching
    | ModelResponseLinked
    | TurnFinished
    | OutputAccepted
    | SubagentLifecycle
    | OutputTextDelta
    | ResponseFactReceived
    | RawProviderEvent
    | ModelOutputItemCompleted
    | HostedToolCallProgress
    | UsageUpdated
    | ApiRateLimitsUpdated
    | ContextCompacted
    | ToolStarted
    | ToolFinished
    | ExecutionItemCompleted
    | SessionEnded
    | EventGap
)

_ObservationT = TypeVar("_ObservationT", bound=Observation)


def _observation_sizes(
    event: Observation, *, utf8: bool = False, include_raw_payload: bool = True
) -> tuple[int, int]:
    """Measure retained event metadata and payload without copying the event."""

    def measure(value: str) -> int:
        return len(value.encode("utf-8")) if utf8 else len(value)

    def json_size(value: Any) -> int:
        if not utf8:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
        return sum(measure(part) for part in encoder.iterencode(value))

    size = sum(
        measure(value)
        for definition in fields(event)
        if isinstance(value := getattr(event, definition.name), str)
    )
    payload_size = 0
    if (
        isinstance(event, ModelOutputItemCompleted)
        and not isinstance(event.item, HostedToolCallSummary)
        and event.item["type"] == "web_search_call"
        and "results" in event.item
    ):
        search_metadata = event.item.copy()
        results = search_metadata.pop("results")
        size += json_size(search_metadata)
        payload_size = json_size(results)
    elif isinstance(event, (ExecutionItemCompleted, ModelOutputItemCompleted)):
        size += json_size(event.item)
    if isinstance(event, ToolFinished) and event.output is not None:
        size += json_size(event.output.to_native())
        if event.output.application_data is not None:
            payload_size += json_size(event.output.application_data)
    if isinstance(event, ResponseFactReceived):
        size += json_size(event.record)
    elif isinstance(event, RawProviderEvent) and include_raw_payload:
        payload_size = json_size(event.payload)
    elif isinstance(event, HostedToolCallProgress):
        metadata = event.event
        partial = metadata.get("partial_image_b64")
        if metadata.get("type") == "response.image_generation_call.partial_image" and isinstance(
            partial, str
        ):
            payload_size = measure(partial)
            metadata = {key: value for key, value in metadata.items() if key != "partial_image_b64"}
        size += json_size(metadata)
    return size, payload_size


class _ObservationBuffer:
    """One retained event log; publishing never awaits or invokes application code."""

    def __init__(
        self,
        session_id: str,
        *,
        retention: Literal["bounded", "all"] = "bounded",
        include_model_output_items: bool = False,
    ) -> None:
        self.retention: Literal["bounded", "all"] = retention
        self.include_model_output_items = include_model_output_items
        self.session_id = session_id
        self.loop = asyncio.get_running_loop()
        self.records: (
            list[tuple[Observation, int, int, bool]] | deque[tuple[Observation, int, int, bool]]
        ) = [] if retention == "all" else deque()
        self.characters = 0
        self.payload_characters = 0
        self.sequence = 0
        self.closed = False
        self.changed = asyncio.Event()
        self._text_demand: weakref.WeakMethod[Callable[[], None]] | None = None
        self._callback_check: weakref.WeakMethod[Callable[[str], None]] | None = None
        self.repair_parent: tuple[_ObservationBuffer, str] | None = None
        self.on_record: weakref.WeakMethod[Callable[[Observation], None]] | None = None

    def _check_read(self) -> None:
        if self._callback_check is not None:
            check = self._callback_check()
            if check is not None:
                check("read events from its own Session")

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def turn_started(self, turn_id: str) -> TurnStarted | None:
        """Find the retained owner of a late native completion."""
        for event, _, _, _ in reversed(self.records):
            if isinstance(event, TurnStarted) and event.turn_id == turn_id:
                return event
        return None

    def _mark_gap(self, *, forward: bool = True) -> None:
        if self.closed:
            return
        self.sequence += 1
        if forward and self.repair_parent is not None:
            self.repair_parent[0]._mark_gap()
        if self.retention == "all":
            self.records.append(
                (
                    EventGap(
                        session_id=self.session_id,
                        sequence=self.sequence,
                        first_sequence=self.sequence,
                    ),
                    0,
                    0,
                    False,
                )
            )
            self.notify()
            return
        # Drop the prefix so the retained observations form a contiguous suffix.
        self.records.clear()
        self.characters = 0
        self.payload_characters = 0
        self.notify()

    def append(self, event: _ObservationT) -> _ObservationT | None:
        if self.closed:
            return None
        event = replace(event, sequence=self.sequence + 1)
        retained_event = event
        details_lost = False
        size, payload_size = _observation_sizes(
            event, include_raw_payload=self.retention == "bounded"
        )
        if (
            self.retention == "bounded"
            and payload_size > 16 * 1024 * 1024
            and isinstance(event, ModelOutputItemCompleted)
            and not isinstance(event.item, HostedToolCallSummary)
            and event.item["type"] == "web_search_call"
            and "results" in event.item
        ):
            search_metadata = event.item.copy()
            search_metadata.pop("results")
            retained_event = replace(event, item=search_metadata)
            payload_size = 0
            details_lost = True
        if self.retention == "bounded" and (size > 65_536 or payload_size > 16 * 1024 * 1024):
            # The parent receives the full event below; this eviction is local.
            self._mark_gap(forward=False)
        else:
            self.sequence += 1
            self.records.append((retained_event, size, payload_size, details_lost))
            self.characters += size
            self.payload_characters += payload_size
            if self.retention == "bounded" and self.payload_characters > 32 * 1024 * 1024:
                # Search details have been yielding before compact events or image previews.
                for index, (previous, metadata_size, detail_size, _) in enumerate(self.records):
                    if (
                        detail_size
                        and isinstance(previous, ModelOutputItemCompleted)
                        and not isinstance(previous.item, HostedToolCallSummary)
                        and previous.item["type"] == "web_search_call"
                    ):
                        search_item = previous.item.copy()
                        search_item.pop("results", None)
                        self.records[index] = (
                            replace(previous, item=search_item),
                            metadata_size,
                            0,
                            True,
                        )
                        self.payload_characters -= detail_size
                        if self.payload_characters <= 32 * 1024 * 1024:
                            break
            while self.retention == "bounded" and (
                len(self.records) > 256
                or self.characters > 65_536
                or self.payload_characters > 32 * 1024 * 1024
            ):
                assert isinstance(self.records, deque)
                _, removed, removed_payload, _ = self.records.popleft()
                self.characters -= removed
                self.payload_characters -= removed_payload
            self.notify()
        if (
            self.repair_parent is not None
            and isinstance(event, _SubmissionEvent)
            and not isinstance(event, OutputAccepted)
        ):
            buffer, parent_id = self.repair_parent
            buffer.append(cast(Observation, replace(event, parent_submission_id=parent_id)))
        if self.on_record is not None and (observer := self.on_record()) is not None:
            observer(event)
        return event

    def finish(self, reason: Literal["closed", "failed"]) -> None:
        if not self.closed:
            self.append(SessionEnded(session_id=self.session_id, reason=reason))
            self.closed = True
            self.notify()


class EventStream(AsyncIterator[Observation]):
    """An independent cursor over a Session, optionally restricted to one turn.

    Closing or cancelling this reader leaves native work running. Live requests
    are available through Session.interactions, Session.pending_interactions,
    or Turn.wait_for_request_or_result.
    """

    def __init__(
        self,
        buffer: _ObservationBuffer,
        *,
        after: int,
        _turn: Turn[Any] | None = None,
        _include_execution_details: bool = False,
    ) -> None:
        if after < 0 or after > buffer.sequence:
            raise ValueError("after must be between zero and the current event sequence")
        if buffer._text_demand is not None and (demand := buffer._text_demand()) is not None:
            demand()
        self._buffer = buffer
        self._after = after
        self._closed = False
        self._reading = False
        self._turn = _turn
        self._include_execution_details = _include_execution_details is True
        self._respect_admission_floor = False

    def __aiter__(self) -> EventStream:
        return self

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._buffer.loop:
            raise RuntimeError("EventStream must be used on its creating event loop")

    async def __anext__(self) -> Observation:
        self._check_loop()
        self._buffer._check_read()
        if self._reading:
            raise RuntimeError("EventStream already has a pending reader")
        self._reading = True
        try:
            while not self._closed:
                signal = self._buffer.changed
                if (
                    self._buffer.retention == "all" or self._respect_admission_floor
                ) and self._turn is not None:
                    # Queued roots have no observation window until dispatch. Read
                    # the live floor after every wake, including pre-opened readers.
                    floor = getattr(self._turn, "_observation_after", 0)
                    if floor < 0:
                        if self._buffer.closed or self._turn._root_result_ready():
                            break
                        await signal.wait()
                        continue
                    self._after = max(self._after, floor)
                records = self._buffer.records
                oldest = records[0][0].sequence if records else self._buffer.sequence + 1
                if self._after < oldest - 1:
                    first = self._after + 1
                    self._after = oldest - 1
                    return EventGap(
                        session_id=self._buffer.session_id,
                        sequence=self._after,
                        first_sequence=first,
                    )
                # Retained sequences are contiguous; start after this reader's cursor.
                for index in range(self._after - oldest + 1, len(records)):
                    event, _, _, details_lost = records[index]
                    if event.sequence > self._after:
                        self._after = event.sequence
                        if (
                            isinstance(event, RawProviderEvent)
                            and not self._include_execution_details
                        ):
                            continue
                        if self._turn is not None and not isinstance(event, EventGap):
                            owners = {step.submission_id for step in self._turn._steps()}
                            own_turn = getattr(event, "submission_id", None) in owners
                            if (
                                not own_turn
                                and getattr(event, "parent_submission_id", None) not in owners
                            ):
                                continue
                            if (
                                own_turn
                                and isinstance(event, TurnFinished)
                                and self._turn.child_id is not None
                            ):
                                self._closed = True
                        if isinstance(event, ResponseFactReceived):
                            event = replace(
                                event,
                                record=deepcopy(event.record)
                                if self._include_execution_details
                                else _summarize_response_fact(event.record),
                            )
                        elif isinstance(event, RawProviderEvent):
                            event = replace(event, payload=deepcopy(event.payload))
                        elif isinstance(event, ModelOutputItemCompleted):
                            model_item = event.item
                            if (
                                not self._include_execution_details
                                and not isinstance(model_item, HostedToolCallSummary)
                                and (
                                    model_item["type"] == "mcp_list_tools"
                                    or model_item["type"] == "mcp_call"
                                    or model_item["type"] == "file_search_call"
                                    or model_item["type"] == "code_interpreter_call"
                                    or model_item["type"] == "shell_call"
                                    or model_item["type"] == "shell_call_output"
                                    or model_item["type"] == "image_generation_call"
                                    or model_item["type"] == "program"
                                    or model_item["type"] == "program_output"
                                )
                            ):
                                model_item = HostedToolCallSummary(
                                    type=model_item["type"],
                                    id=model_item["id"],
                                    turn_id=model_item["turn_id"],
                                    status=model_item["status"],
                                )
                            elif (
                                not self._include_execution_details
                                and not isinstance(model_item, HostedToolCallSummary)
                                and model_item["type"] == "web_search_call"
                            ):
                                model_item = model_item.copy()
                                model_item.pop("results", None)
                                model_item = deepcopy(model_item)
                            else:
                                model_item = deepcopy(model_item)
                            event = replace(event, item=model_item)
                        elif isinstance(event, HostedToolCallProgress):
                            event_type = event.event.get("type")
                            event = replace(
                                event,
                                event=(
                                    deepcopy(event.event)
                                    if self._include_execution_details
                                    else {
                                        "type": event_type if isinstance(event_type, str) else None
                                    }
                                ),
                            )
                        elif isinstance(event, ToolFinished):
                            event = replace(
                                event,
                                output=deepcopy(event.output)
                                if self._include_execution_details
                                else None,
                            )
                        elif isinstance(event, ExecutionItemCompleted):
                            item = event.item
                            if self._include_execution_details:
                                item = deepcopy(item)
                            else:
                                item = {
                                    "type": item["type"],
                                    "id": item["id"],
                                    "call_id": item.get("call_id", item["id"]),
                                    "thread_id": item.get("thread_id"),
                                    "turn_id": item["turn_id"],
                                    "status": item["status"],
                                    "native_status": item.get("native_status"),
                                }
                            event = replace(event, item=item)
                        return (
                            EventGap(
                                session_id=event.session_id,
                                sequence=event.sequence,
                                first_sequence=event.sequence,
                            )
                            if details_lost and self._include_execution_details
                            else event
                        )
                if self._buffer.closed:
                    break
                if self._turn is not None:
                    if self._turn._root_result_ready():
                        break
                    children = self._turn._session._callbacks_for_child(self._turn)
                    if children and all(child.completion.done() for child in children):
                        break
                await signal.wait()
            self._closed = True
            raise StopAsyncIteration
        finally:
            self._reading = False

    async def aclose(self) -> None:
        """Wake and close only this reader."""
        self._check_loop()
        self._closed = True
        self._buffer.notify()


def _request_observation(
    value: Any, *, session_id: str, submission_id: str
) -> ModelRequestDispatching | ModelResponseLinked | None:
    """Decode only the safe tagged bridge; malformed passive metadata is harmless."""
    if not isinstance(value, dict):
        return None
    common = {"type", "request_id", "thread_id", "turn_id"}
    if not all(isinstance(value.get(key), str) and value[key] for key in common):
        return None
    if value["type"] == "model_response_linked":
        if (
            set(value) != common | {"response_observation_id"}
            or not isinstance(value.get("response_observation_id"), str)
            or not value["response_observation_id"]
        ):
            return None
        return ModelResponseLinked(
            session_id=session_id,
            submission_id=submission_id,
            turn_id=value["turn_id"],
            thread_id=value["thread_id"],
            request_id=value["request_id"],
            response_observation_id=value["response_observation_id"],
        )
    if value["type"] != "model_request_dispatching" or set(value) != common | {
        "model",
        "reasoning_effort",
        "reasoning_summary",
        "service_tier",
        "wire",
        "transport",
    }:
        return None
    if (
        not isinstance(value["model"], str)
        or value["wire"] not in ("responses", "responses_lite")
        or value["transport"] not in ("http_sse", "http_json", "websocket")
    ):
        return None
    if any(
        value[key] is not None and not isinstance(value[key], str)
        for key in ("reasoning_summary", "service_tier")
    ):
        return None
    raw = value["reasoning_effort"]
    effort: str | int | None = None
    if raw is not None:
        if not isinstance(raw, dict):
            return None
        if (
            raw.get("kind") == "text"
            and set(raw) == {"kind", "value"}
            and isinstance(raw.get("value"), str)
        ):
            effort = raw["value"]
        elif raw.get("kind") == "integer" and set(raw) == {"kind", "decimal"}:
            digits = raw["decimal"]
            if (
                not isinstance(digits, str)
                or not digits
                or len(digits) > 20
                or not digits.isascii()
                or not digits.isdigit()
                or (len(digits) > 1 and digits[0] == "0")
            ):
                return None
            effort = int(digits)
            if effort > 18446744073709551615:
                return None
        else:
            return None
    return ModelRequestDispatching(
        session_id=session_id,
        submission_id=submission_id,
        turn_id=value["turn_id"],
        thread_id=value["thread_id"],
        request_id=value["request_id"],
        model=value["model"],
        reasoning_effort=effort,
        reasoning_summary=value["reasoning_summary"],
        service_tier=value["service_tier"],
        wire=value["wire"],
        transport=value["transport"],
    )


def _event_stream_mcp(stream: Any) -> Any:
    if stream._turn is not None:
        agent = stream._turn._session._opening_agent
        return (agent, *agent.tools)
    reference = stream._buffer._callback_check
    callback = None if reference is None else reference()
    session = getattr(callback, "__self__", None)
    return () if session is None else (session._opening_agent, *session._opening_agent.tools)


_protect_mcp_methods(EventStream, _event_stream_mcp)
_preserve_module_identity(globals(), "agents.observation")
