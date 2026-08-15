from __future__ import annotations

import asyncio
import copy
import inspect
import json
import sys
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeAlias, cast

from openai.types.responses import (
    Response,
    ResponseApplyPatchToolCall,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseInProgressEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseOutputTextAnnotationAddedEvent,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningSummaryPartDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseReasoningTextDoneEvent,
    ResponseRefusalDeltaEvent,
    ResponseRefusalDoneEvent,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
    ResponseUsage,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from openai.types.responses.response_reasoning_summary_part_added_event import (
    Part as AddedEventPart,
)
from openai.types.responses.response_reasoning_summary_part_done_event import Part as DoneEventPart
from openai.types.responses.response_text_delta_event import (
    Logprob as ResponseTextDeltaLogprob,
    LogprobTopLogprob as ResponseTextDeltaTopLogprob,
)
from openai.types.responses.response_text_done_event import (
    Logprob as ResponseTextDoneLogprob,
    LogprobTopLogprob as ResponseTextDoneTopLogprob,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from typing_extensions import TypedDict

from .._tool_invocation import tool_invocation_call_id
from ..agent_output import AgentOutputSchemaBase
from ..exceptions import ModelBehaviorError
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseOutputItem, TResponseStreamEvent
from ..model_settings import ModelSettings
from ..models.interface import Model, ModelTracing
from ..retry import ModelRetryAdvice, ModelRetryAdviceRequest
from ..tool import Tool
from ..tracing import SpanError, generation_span
from ..tracing.scope import Scope
from ..usage import (
    Usage,
    _attach_normalized_usage,
    _attach_raw_usage_snapshot,
    _raw_usage_snapshot,
)
from ..util._error_tracing import (
    REDACTED_TRACE_ERROR_MESSAGE,
    record_current_task_model_timeout_on_span,
)


class ModelScriptError(Exception):
    """Base exception for an invalid or incompletely consumed model script."""


ModelStepReason: TypeAlias = Literal[
    "invalid_input",
    "unsupported_field",
    "invalid_error",
    "invalid_responder",
    "invalid_stream_events",
    "conflicting_outcomes",
    "invalid_retry_advice",
]


class InvalidModelStep(ModelScriptError):
    """Raised when a model step is invalid before it enters the script queue."""

    def __init__(
        self,
        message: str,
        *,
        reason: ModelStepReason,
        input_index: int,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.input_index = input_index


class UnexpectedModelCall(ModelScriptError):
    """Raised when the model is called after all configured steps were consumed."""

    def __init__(self, message: str, *, call: ModelCall, call_index: int) -> None:
        super().__init__(message)
        self.call = call
        self.call_index = call_index


class UnconsumedModelSteps(ModelScriptError):
    """Raised when a test finishes before consuming every configured step."""

    def __init__(self, message: str, *, remaining_steps: int) -> None:
        super().__init__(message)
        self.remaining_steps = remaining_steps


@dataclass(frozen=True)
class ModelCall:
    """A recorded call at the provider-neutral ``Model`` boundary."""

    system_instructions: str | None
    input: Any
    model_settings: ModelSettings
    tools: list[Tool]
    output_schema: AgentOutputSchemaBase | None
    handoffs: list[Handoff]
    tracing: ModelTracing
    previous_response_id: str | None
    conversation_id: str | None
    prompt: ResponsePromptParam | None
    streamed: bool


def _snapshot_model_call(call: ModelCall) -> ModelCall:
    return ModelCall(
        system_instructions=call.system_instructions,
        input=copy.deepcopy(call.input),
        model_settings=copy.deepcopy(call.model_settings),
        tools=list(call.tools),
        output_schema=call.output_schema,
        handoffs=list(call.handoffs),
        tracing=call.tracing,
        previous_response_id=call.previous_response_id,
        conversation_id=call.conversation_id,
        prompt=copy.deepcopy(call.prompt),
        streamed=call.streamed,
    )


@dataclass
class ModelStep:
    """One deterministic model call result.

    ``output`` uses the normalized SDK output-item boundary. Set ``error`` to raise from the model
    call, ``responder`` to derive the result from the recorded call, or ``stream_events`` to supply
    an exact normalized event stream for advanced streaming tests. ``ScriptedModel`` also accepts
    the equivalent dictionary form described by ``ModelStepSpec``.
    """

    output: Sequence[TResponseOutputItem] = field(default_factory=tuple)
    usage: Usage = field(default_factory=Usage)
    response_id: str | None = "resp-789"
    request_id: str | None = None
    raw_usage: dict[str, Any] | None = None
    error: Exception | None = None
    responder: ModelResponder | None = None
    stream_events: Sequence[TResponseStreamEvent] | ModelStreamFactory | None = None
    retry_advice: ModelRetryAdvice | None = None

    @classmethod
    def raise_error(
        cls,
        error: Exception,
        *,
        retry_advice: ModelRetryAdvice | None = None,
    ) -> ModelStep:
        """Create a step that raises ``error`` with optional provider retry guidance."""
        return cls(error=error, retry_advice=retry_advice)

    @classmethod
    def respond(cls, responder: ModelResponder) -> ModelStep:
        """Create a step whose result is derived from the recorded call."""
        return cls(responder=responder)

    @classmethod
    def stream(
        cls,
        events: Sequence[TResponseStreamEvent] | ModelStreamFactory,
        *,
        output: Sequence[TResponseOutputItem] = (),
        usage: Usage | None = None,
        response_id: str | None = "resp-789",
    ) -> ModelStep:
        """Create a step with an exact normalized stream-event sequence or factory."""
        stream_events = events if callable(events) else tuple(events)
        return cls(
            output=output,
            usage=usage or Usage(),
            response_id=response_id,
            stream_events=stream_events,
        )


class ModelStepSpec(TypedDict, total=False):
    """Dictionary form of ``ModelStep`` accepted by ``ScriptedModel``."""

    output: Sequence[TResponseOutputItem]
    usage: Usage
    response_id: str | None
    request_id: str | None
    raw_usage: dict[str, Any] | None
    error: Exception | None
    responder: ModelResponder | None
    stream_events: Sequence[TResponseStreamEvent] | ModelStreamFactory | None
    retry_advice: ModelRetryAdvice | None


_MODEL_STEP_FIELDS = frozenset(ModelStepSpec.__annotations__)


ModelStepResult: TypeAlias = (
    ModelStep | ModelStepSpec | ModelResponse | Sequence[TResponseOutputItem] | Exception
)
ModelResponder: TypeAlias = Callable[[ModelCall], ModelStepResult | Awaitable[ModelStepResult]]
ModelStreamFactory: TypeAlias = Callable[[ModelCall], AsyncIterator[TResponseStreamEvent]]
ModelScriptItem: TypeAlias = ModelStepResult


class ScriptedModel(Model):
    """A deterministic provider-neutral model for testing agent workflows.

    Each step may be a ``ModelStep``, an equivalent ``ModelStepSpec`` dictionary, a
    ``ModelResponse``, a normalized output-item sequence, or an exception.
    """

    def __init__(
        self,
        steps: Iterable[ModelScriptItem] = (),
        *,
        emit_traces: bool = False,
        default_usage: Usage | None = None,
    ) -> None:
        self._steps = [
            self._coerce_step(step, input_index) for input_index, step in enumerate(steps)
        ]
        self._emit_traces = emit_traces
        self._default_usage = copy.deepcopy(default_usage)
        self._calls: list[ModelCall] = []
        self._retry_advice_by_error_id: dict[int, tuple[Exception, ModelRetryAdvice]] = {}

    @property
    def calls(self) -> tuple[ModelCall, ...]:
        """Return detached snapshots of recorded model calls."""
        return tuple(_snapshot_model_call(call) for call in self._calls)

    @property
    def remaining_steps(self) -> int:
        """Return the number of configured model calls that have not run yet."""
        return len(self._steps)

    @property
    def first_call(self) -> ModelCall | None:
        """Return the first recorded call, if any."""
        return _snapshot_model_call(self._calls[0]) if self._calls else None

    @property
    def last_call(self) -> ModelCall | None:
        """Return the most recent recorded call, if any."""
        return _snapshot_model_call(self._calls[-1]) if self._calls else None

    def enqueue(self, step: ModelScriptItem) -> None:
        """Append one model step."""
        self._steps.append(self._coerce_step(step, 0))

    def extend(self, steps: Iterable[ModelScriptItem]) -> None:
        """Append multiple model steps."""
        normalized = [
            self._coerce_step(step, input_index) for input_index, step in enumerate(steps)
        ]
        self._steps.extend(normalized)

    def set_default_usage(self, usage: Usage | None) -> None:
        """Set usage for scripted steps that do not provide their own usage."""
        self._default_usage = copy.deepcopy(usage)

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> ModelRetryAdvice | None:
        """Return retry advice attached to the exact scripted error that was raised."""
        configured = self._retry_advice_by_error_id.get(id(request.error))
        if configured is None or configured[0] is not request.error:
            return None
        return copy.deepcopy(configured[1])

    def assert_complete(self) -> None:
        """Raise when configured steps remain unconsumed."""
        if self._steps:
            raise UnconsumedModelSteps(
                f"{len(self._steps)} scripted model step(s) were not consumed.",
                remaining_steps=len(self._steps),
            )

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        call = self._record_call(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
            streamed=False,
        )
        with generation_span(disabled=not self._emit_traces) as span:
            retry_advice_synced = False
            try:
                step = await self._next_resolved_step(call)
                if step.error is not None:
                    self._remember_retry_advice(step)
                    retry_advice_synced = True
                    raise step.error
                return self._model_response(step, call.model_settings)
            except asyncio.CancelledError:
                record_current_task_model_timeout_on_span(
                    span,
                    message="Error",
                    trace_include_sensitive_data=call.tracing.include_data(),
                )
                raise
            except Exception as error:
                if not retry_advice_synced:
                    self._forget_retry_advice(error)
                self._set_span_error(span, error, call.tracing)
                raise

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        call = self._record_call(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
            streamed=True,
        )
        span = generation_span(disabled=not self._emit_traces)
        span.start(mark_as_current=False)
        retry_advice_synced = False
        try:
            with _mark_span_current(span):
                step = await self._next_resolved_step(call)
            if step.error is not None:
                self._remember_retry_advice(step)
                retry_advice_synced = True
                raise step.error
            if callable(step.stream_events):
                with _mark_span_current(span):
                    stream = step.stream_events(call)
                try:
                    while True:
                        try:
                            with _mark_span_current(span):
                                event = await anext(stream)
                        except StopAsyncIteration:
                            break
                        yield event
                finally:
                    aclose = getattr(stream, "aclose", None)
                    if callable(aclose):
                        active_error = sys.exc_info()[1]
                        try:
                            with _mark_span_current(span):
                                await aclose()
                        except BaseException:
                            if active_error is None:
                                raise
                return
            if step.stream_events is not None:
                for event in step.stream_events:
                    yield event
                return
            with _mark_span_current(span):
                events = _stream_events_for_step(
                    step,
                    preserve_raw_usage=call.model_settings.preserve_raw_usage is True,
                )
            for event in events:
                yield event
        except asyncio.CancelledError:
            record_current_task_model_timeout_on_span(
                span,
                message="Error",
                trace_include_sensitive_data=call.tracing.include_data(),
            )
            raise
        except Exception as error:
            if not retry_advice_synced:
                self._forget_retry_advice(error)
            self._set_span_error(span, error, call.tracing)
            raise
        finally:
            span.finish(reset_current=False)

    def _record_call(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
        streamed: bool,
    ) -> ModelCall:
        recorded_input = copy.deepcopy(input)
        call = ModelCall(
            system_instructions=system_instructions,
            input=recorded_input,
            model_settings=copy.deepcopy(model_settings),
            tools=list(tools),
            output_schema=output_schema,
            handoffs=list(handoffs),
            tracing=tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=copy.deepcopy(prompt),
            streamed=streamed,
        )
        execution_call = _snapshot_model_call(call)
        self._calls.append(call)
        return execution_call

    async def _next_resolved_step(self, call: ModelCall) -> ModelStep:
        if not self._steps:
            mode = "streaming" if call.streamed else "non-streaming"
            call_index = len(self._calls) - 1
            raise UnexpectedModelCall(
                f"Unexpected {mode} model call #{call_index + 1}: no scripted steps remain.",
                call=call,
                call_index=call_index,
            )
        step = self._steps.pop(0)
        while step.responder is not None:
            result = step.responder(call)
            if inspect.isawaitable(result):
                result = await result
            step = self._coerce_step(result, 0)
        if step.usage == Usage():
            usage = self._default_usage if self._default_usage is not None else Usage(requests=1)
        else:
            usage = step.usage
        return replace(step, usage=copy.deepcopy(usage))

    @staticmethod
    def _coerce_step(
        step: ModelScriptItem | ModelStepResult,
        input_index: int,
    ) -> ModelStep:
        if isinstance(step, ModelStep):
            normalized = step
        elif isinstance(step, ModelResponse):
            normalized = ModelStep(
                output=step.output,
                usage=step.usage,
                response_id=step.response_id,
                request_id=step.request_id,
                raw_usage=step.raw_usage,
            )
        elif isinstance(step, Exception):
            normalized = ModelStep.raise_error(step)
        elif isinstance(step, Mapping):
            unsupported = [field for field in step if field not in _MODEL_STEP_FIELDS]
            if unsupported:
                raise _invalid_model_step(
                    reason="unsupported_field",
                    input_index=input_index,
                    detail="contains unsupported fields",
                )
            normalized = ModelStep(**cast(ModelStepSpec, dict(step)))
        else:
            normalized = ModelStep(output=step)
        _validate_model_step(normalized, input_index=input_index)
        return _snapshot_model_step(normalized)

    def _remember_retry_advice(self, step: ModelStep) -> None:
        if step.error is None:
            return
        if step.retry_advice is None:
            self._forget_retry_advice(step.error)
            return
        self._retry_advice_by_error_id[id(step.error)] = (
            step.error,
            copy.deepcopy(step.retry_advice),
        )

    def _forget_retry_advice(self, error: Exception) -> None:
        self._retry_advice_by_error_id.pop(id(error), None)

    @staticmethod
    def _model_response(step: ModelStep, model_settings: ModelSettings) -> ModelResponse:
        return ModelResponse(
            output=_convert_output_items(step.output),
            usage=step.usage,
            response_id=step.response_id,
            request_id=step.request_id,
            raw_usage=(
                _raw_usage_snapshot(step.raw_usage)
                if model_settings.preserve_raw_usage is True
                else None
            ),
        )

    @staticmethod
    def _set_span_error(span: Any, error: Exception, tracing: ModelTracing) -> None:
        try:
            if tracing.include_data():
                try:
                    error_message = str(error)
                except BaseException:
                    error_message = f"Unrenderable {type(error).__name__}"
            else:
                error_message = REDACTED_TRACE_ERROR_MESSAGE
            span.set_error(
                SpanError(
                    message="Error",
                    data={"name": error.__class__.__name__, "message": error_message},
                )
            )
        except BaseException:
            pass


def assistant_message(text: str, *, item_id: str = "scripted-message") -> TResponseOutputItem:
    """Build one normalized assistant text output item."""
    return ResponseOutputMessage(
        id=item_id,
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                text=text,
                type="output_text",
                annotations=[],
                logprobs=[],
            )
        ],
    )


@contextmanager
def _mark_span_current(span: Any) -> Iterator[None]:
    token = Scope.set_current_span(span)
    try:
        yield
    finally:
        Scope.reset_current_span(token)


def function_call(
    name: str,
    arguments: str | Mapping[str, Any],
    *,
    call_id: str,
    item_id: str | None = None,
    namespace: str | None = None,
) -> TResponseOutputItem:
    """Build one normalized function-tool call output item."""
    serialized_arguments = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    )
    kwargs: dict[str, Any] = {
        "id": call_id if item_id is None else item_id,
        "call_id": call_id,
        "type": "function_call",
        "name": name,
        "arguments": serialized_arguments,
    }
    if namespace is not None:
        kwargs["namespace"] = namespace
    return ResponseFunctionToolCall(**kwargs)


def _convert_output_items(
    output: Sequence[TResponseOutputItem],
) -> list[TResponseOutputItem]:
    converted: list[TResponseOutputItem] = []
    for item in output:
        if isinstance(item, dict) and item.get("type") == "apply_patch_call":
            call_identity = tool_invocation_call_id(item)
            call_id = call_identity[1] if call_identity is not None else None
            if call_id is None:
                raise ModelBehaviorError(
                    "Tool invocations require a non-empty string call ID before execution."
                )
            if "id" in item:
                item_id = item["id"]
                if not isinstance(item_id, str) or not item_id:
                    raise ModelBehaviorError(
                        "Apply-patch tool calls require a non-empty string item ID when provided."
                    )
            else:
                item_id = call_id
            converted.append(
                cast(
                    TResponseOutputItem,
                    ResponseApplyPatchToolCall(
                        type="apply_patch_call",
                        id=item_id,
                        call_id=call_id,
                        status=item["status"] if "status" in item else "completed",
                        operation=item.get("operation"),
                        caller=item.get("caller"),
                    ),
                )
            )
        else:
            converted.append(item)
    return converted


def _snapshot_model_step(step: ModelStep) -> ModelStep:
    stream_events = step.stream_events
    if stream_events is not None and not callable(stream_events):
        stream_events = copy.deepcopy(stream_events)
    return ModelStep(
        output=copy.deepcopy(step.output),
        usage=copy.deepcopy(step.usage),
        response_id=step.response_id,
        request_id=step.request_id,
        raw_usage=copy.deepcopy(step.raw_usage),
        error=step.error,
        responder=step.responder,
        stream_events=stream_events,
        retry_advice=copy.deepcopy(step.retry_advice),
    )


def _invalid_model_step(
    *,
    reason: ModelStepReason,
    input_index: int,
    detail: str,
) -> InvalidModelStep:
    return InvalidModelStep(
        f"Scripted model step #{input_index + 1} {detail}.",
        reason=reason,
        input_index=input_index,
    )


def _validate_model_step(step: ModelStep, *, input_index: int) -> None:
    if not isinstance(step.output, Sequence) or isinstance(step.output, str | bytes):
        raise _invalid_model_step(
            reason="invalid_input",
            input_index=input_index,
            detail="must use a sequence for output",
        )
    if not isinstance(step.usage, Usage):
        raise _invalid_model_step(
            reason="invalid_input",
            input_index=input_index,
            detail="must use Usage for usage",
        )
    if step.response_id is not None and not isinstance(step.response_id, str):
        raise _invalid_model_step(
            reason="invalid_input",
            input_index=input_index,
            detail="must use a string or None for response_id",
        )
    if step.request_id is not None and not isinstance(step.request_id, str):
        raise _invalid_model_step(
            reason="invalid_input",
            input_index=input_index,
            detail="must use a string or None for request_id",
        )
    if step.raw_usage is not None and not isinstance(step.raw_usage, dict):
        raise _invalid_model_step(
            reason="invalid_input",
            input_index=input_index,
            detail="must use a dictionary or None for raw_usage",
        )
    if step.error is not None and not isinstance(step.error, Exception):
        raise _invalid_model_step(
            reason="invalid_error",
            input_index=input_index,
            detail="must use an Exception for error",
        )
    if step.responder is not None and not callable(step.responder):
        raise _invalid_model_step(
            reason="invalid_responder",
            input_index=input_index,
            detail="must use a callable responder",
        )
    if (
        step.stream_events is not None
        and not callable(step.stream_events)
        and (
            not isinstance(step.stream_events, Sequence)
            or isinstance(step.stream_events, str | bytes)
        )
    ):
        raise _invalid_model_step(
            reason="invalid_stream_events",
            input_index=input_index,
            detail="must use a sequence or callable stream_events value",
        )
    selected = sum(value is not None for value in (step.error, step.responder, step.stream_events))
    if selected > 1:
        raise _invalid_model_step(
            reason="conflicting_outcomes",
            input_index=input_index,
            detail="cannot combine error, responder, and stream_events outcomes",
        )
    if step.retry_advice is not None:
        if not isinstance(step.retry_advice, ModelRetryAdvice) or step.error is None:
            raise _invalid_model_step(
                reason="invalid_retry_advice",
                input_index=input_index,
                detail="requires an error and a ModelRetryAdvice value",
            )


def _stream_events_for_step(
    step: ModelStep,
    *,
    preserve_raw_usage: bool,
) -> list[TResponseStreamEvent]:
    output = _convert_output_items(step.output)
    unsupported_item = next(
        (
            item
            for item in output
            if not isinstance(
                item,
                ResponseApplyPatchToolCall
                | ResponseFunctionToolCall
                | ResponseOutputMessage
                | ResponseReasoningItem,
            )
        ),
        None,
    )
    if unsupported_item is not None:
        raise ModelBehaviorError(
            f"Automatic streaming does not support {type(unsupported_item).__name__}. "
            "Use ModelStep.stream(...) to provide exact normalized stream events."
        )
    response = _response_for_step(
        step,
        output,
    )
    in_progress_response = response.model_copy(
        update={"output": [], "status": "in_progress", "usage": None}
    )
    if preserve_raw_usage and step.raw_usage is not None:
        _attach_raw_usage_snapshot(response, step.raw_usage)
    events: list[TResponseStreamEvent] = []
    sequence_number = 0

    events.append(
        cast(
            TResponseStreamEvent,
            ResponseCreatedEvent(
                type="response.created",
                response=copy.deepcopy(in_progress_response),
                sequence_number=sequence_number,
            ),
        )
    )
    sequence_number += 1
    events.append(
        cast(
            TResponseStreamEvent,
            ResponseInProgressEvent(
                type="response.in_progress",
                response=copy.deepcopy(in_progress_response),
                sequence_number=sequence_number,
            ),
        )
    )
    sequence_number += 1

    for output_index, output_item in enumerate(output):
        events.append(
            cast(
                TResponseStreamEvent,
                ResponseOutputItemAddedEvent(
                    type="response.output_item.added",
                    item=copy.deepcopy(_in_progress_output_item(output_item)),
                    output_index=output_index,
                    sequence_number=sequence_number,
                ),
            )
        )
        sequence_number += 1

        if isinstance(output_item, ResponseReasoningItem):
            for summary_index, summary in enumerate(output_item.summary or []):
                events.extend(
                    [
                        cast(
                            TResponseStreamEvent,
                            ResponseReasoningSummaryPartAddedEvent(
                                type="response.reasoning_summary_part.added",
                                item_id=output_item.id,
                                output_index=output_index,
                                summary_index=summary_index,
                                part=AddedEventPart(text="", type=summary.type),
                                sequence_number=sequence_number,
                            ),
                        ),
                        cast(
                            TResponseStreamEvent,
                            ResponseReasoningSummaryTextDeltaEvent(
                                type="response.reasoning_summary_text.delta",
                                item_id=output_item.id,
                                output_index=output_index,
                                summary_index=summary_index,
                                delta=summary.text,
                                sequence_number=sequence_number + 1,
                            ),
                        ),
                        cast(
                            TResponseStreamEvent,
                            ResponseReasoningSummaryTextDoneEvent(
                                type="response.reasoning_summary_text.done",
                                item_id=output_item.id,
                                output_index=output_index,
                                summary_index=summary_index,
                                text=summary.text,
                                sequence_number=sequence_number + 2,
                            ),
                        ),
                        cast(
                            TResponseStreamEvent,
                            ResponseReasoningSummaryPartDoneEvent(
                                type="response.reasoning_summary_part.done",
                                item_id=output_item.id,
                                output_index=output_index,
                                summary_index=summary_index,
                                part=DoneEventPart(text=summary.text, type=summary.type),
                                sequence_number=sequence_number + 3,
                            ),
                        ),
                    ]
                )
                sequence_number += 4
            for content_index, content in enumerate(output_item.content or []):
                events.extend(
                    [
                        cast(
                            TResponseStreamEvent,
                            ResponseReasoningTextDeltaEvent(
                                type="response.reasoning_text.delta",
                                item_id=output_item.id,
                                output_index=output_index,
                                content_index=content_index,
                                delta=content.text,
                                sequence_number=sequence_number,
                            ),
                        ),
                        cast(
                            TResponseStreamEvent,
                            ResponseReasoningTextDoneEvent(
                                type="response.reasoning_text.done",
                                item_id=output_item.id,
                                output_index=output_index,
                                content_index=content_index,
                                text=content.text,
                                sequence_number=sequence_number + 1,
                            ),
                        ),
                    ]
                )
                sequence_number += 2
        elif isinstance(output_item, ResponseFunctionToolCall):
            item_id = output_item.call_id if output_item.id is None else output_item.id
            events.extend(
                [
                    cast(
                        TResponseStreamEvent,
                        ResponseFunctionCallArgumentsDeltaEvent(
                            type="response.function_call_arguments.delta",
                            item_id=item_id,
                            output_index=output_index,
                            delta=output_item.arguments,
                            sequence_number=sequence_number,
                        ),
                    ),
                    cast(
                        TResponseStreamEvent,
                        ResponseFunctionCallArgumentsDoneEvent(
                            type="response.function_call_arguments.done",
                            item_id=item_id,
                            output_index=output_index,
                            arguments=output_item.arguments,
                            name=output_item.name,
                            sequence_number=sequence_number + 1,
                        ),
                    ),
                ]
            )
            sequence_number += 2
        elif isinstance(output_item, ResponseOutputMessage):
            for content_index, content_part in enumerate(output_item.content or []):
                if isinstance(content_part, ResponseOutputText):
                    delta_logprobs = [
                        ResponseTextDeltaLogprob(
                            token=logprob.token,
                            logprob=logprob.logprob,
                            top_logprobs=[
                                ResponseTextDeltaTopLogprob(
                                    token=top_logprob.token,
                                    logprob=top_logprob.logprob,
                                )
                                for top_logprob in logprob.top_logprobs
                            ],
                        )
                        for logprob in content_part.logprobs or []
                    ]
                    done_logprobs = [
                        ResponseTextDoneLogprob(
                            token=logprob.token,
                            logprob=logprob.logprob,
                            top_logprobs=[
                                ResponseTextDoneTopLogprob(
                                    token=top_logprob.token,
                                    logprob=top_logprob.logprob,
                                )
                                for top_logprob in logprob.top_logprobs
                            ],
                        )
                        for logprob in content_part.logprobs or []
                    ]
                    events.extend(
                        [
                            cast(
                                TResponseStreamEvent,
                                ResponseContentPartAddedEvent(
                                    type="response.content_part.added",
                                    item_id=output_item.id,
                                    output_index=output_index,
                                    content_index=content_index,
                                    part=content_part.model_copy(
                                        deep=True,
                                        update={"annotations": [], "logprobs": [], "text": ""},
                                    ),
                                    sequence_number=sequence_number,
                                ),
                            ),
                            cast(
                                TResponseStreamEvent,
                                ResponseTextDeltaEvent(
                                    type="response.output_text.delta",
                                    item_id=output_item.id,
                                    output_index=output_index,
                                    content_index=content_index,
                                    delta=content_part.text,
                                    logprobs=delta_logprobs,
                                    sequence_number=sequence_number + 1,
                                ),
                            ),
                        ]
                    )
                    sequence_number += 2
                    for annotation_index, annotation in enumerate(content_part.annotations or []):
                        events.append(
                            cast(
                                TResponseStreamEvent,
                                ResponseOutputTextAnnotationAddedEvent.model_validate(
                                    {
                                        "type": "response.output_text.annotation.added",
                                        "item_id": output_item.id,
                                        "output_index": output_index,
                                        "content_index": content_index,
                                        "annotation_index": annotation_index,
                                        "annotation": annotation.model_dump(),
                                        "sequence_number": sequence_number,
                                    }
                                ),
                            )
                        )
                        sequence_number += 1
                    events.extend(
                        [
                            cast(
                                TResponseStreamEvent,
                                ResponseTextDoneEvent(
                                    type="response.output_text.done",
                                    item_id=output_item.id,
                                    output_index=output_index,
                                    content_index=content_index,
                                    text=content_part.text,
                                    logprobs=done_logprobs,
                                    sequence_number=sequence_number,
                                ),
                            ),
                            cast(
                                TResponseStreamEvent,
                                ResponseContentPartDoneEvent(
                                    type="response.content_part.done",
                                    item_id=output_item.id,
                                    output_index=output_index,
                                    content_index=content_index,
                                    part=copy.deepcopy(content_part),
                                    sequence_number=sequence_number + 1,
                                ),
                            ),
                        ]
                    )
                    sequence_number += 2
                elif isinstance(content_part, ResponseOutputRefusal):
                    events.extend(
                        [
                            cast(
                                TResponseStreamEvent,
                                ResponseContentPartAddedEvent(
                                    type="response.content_part.added",
                                    item_id=output_item.id,
                                    output_index=output_index,
                                    content_index=content_index,
                                    part=content_part.model_copy(
                                        deep=True,
                                        update={"refusal": ""},
                                    ),
                                    sequence_number=sequence_number,
                                ),
                            ),
                            cast(
                                TResponseStreamEvent,
                                ResponseRefusalDeltaEvent(
                                    type="response.refusal.delta",
                                    item_id=output_item.id,
                                    output_index=output_index,
                                    content_index=content_index,
                                    delta=content_part.refusal,
                                    sequence_number=sequence_number + 1,
                                ),
                            ),
                            cast(
                                TResponseStreamEvent,
                                ResponseRefusalDoneEvent(
                                    type="response.refusal.done",
                                    item_id=output_item.id,
                                    output_index=output_index,
                                    content_index=content_index,
                                    refusal=content_part.refusal,
                                    sequence_number=sequence_number + 2,
                                ),
                            ),
                            cast(
                                TResponseStreamEvent,
                                ResponseContentPartDoneEvent(
                                    type="response.content_part.done",
                                    item_id=output_item.id,
                                    output_index=output_index,
                                    content_index=content_index,
                                    part=copy.deepcopy(content_part),
                                    sequence_number=sequence_number + 3,
                                ),
                            ),
                        ]
                    )
                    sequence_number += 4

        events.append(
            cast(
                TResponseStreamEvent,
                ResponseOutputItemDoneEvent(
                    type="response.output_item.done",
                    item=copy.deepcopy(output_item),
                    output_index=output_index,
                    sequence_number=sequence_number,
                ),
            )
        )
        sequence_number += 1

    events.append(
        cast(
            TResponseStreamEvent,
            ResponseCompletedEvent(
                type="response.completed",
                response=response,
                sequence_number=sequence_number,
            ),
        )
    )
    return events


def _in_progress_output_item(output_item: TResponseOutputItem) -> TResponseOutputItem:
    if isinstance(output_item, ResponseApplyPatchToolCall):
        return output_item.model_copy(update={"status": "in_progress"})
    if isinstance(output_item, ResponseOutputMessage):
        return output_item.model_copy(update={"content": [], "status": "in_progress"})
    if isinstance(output_item, ResponseReasoningItem):
        return output_item.model_copy(
            update={
                "content": [] if output_item.content is not None else None,
                "encrypted_content": None,
                "status": "in_progress",
                "summary": [],
            }
        )
    if isinstance(output_item, ResponseFunctionToolCall):
        return output_item.model_copy(update={"arguments": "", "status": "in_progress"})
    return output_item


def _response_for_step(
    step: ModelStep,
    output: list[TResponseOutputItem],
) -> Response:
    usage = step.usage
    response_usage = _response_usage_for_usage(usage)
    _attach_normalized_usage(response_usage, usage)
    object.__setattr__(response_usage, "_agents_sdk_request_count", usage.requests)
    if usage.request_usage_entries:
        object.__setattr__(
            response_usage,
            "_agents_sdk_request_usages",
            [_response_usage_for_usage(entry) for entry in usage.request_usage_entries],
        )
    response = Response(
        id=step.response_id if step.response_id is not None else "scripted-response",
        created_at=0,
        model="scripted-model",
        object="response",
        output=output,
        tool_choice="none",
        tools=[],
        top_p=None,
        parallel_tool_calls=False,
        status="completed",
        usage=response_usage,
    )
    if step.response_id is None:
        # The normalized Model boundary permits an absent response ID, although Response does not.
        object.__setattr__(response, "id", None)
    if step.request_id is not None:
        response._request_id = step.request_id
    return response


def _response_usage_for_usage(usage: Any) -> ResponseUsage:
    return ResponseUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        input_tokens_details=InputTokensDetails.model_validate(
            {
                "cache_write_tokens": getattr(usage.input_tokens_details, "cache_write_tokens", 0),
                "cached_tokens": getattr(usage.input_tokens_details, "cached_tokens", 0),
            }
        ),
        output_tokens_details=OutputTokensDetails(
            reasoning_tokens=getattr(usage.output_tokens_details, "reasoning_tokens", 0)
        ),
    )


__all__ = [
    "InvalidModelStep",
    "ModelCall",
    "ModelScriptError",
    "ModelStep",
    "ModelStepSpec",
    "ScriptedModel",
    "UnconsumedModelSteps",
    "UnexpectedModelCall",
    "assistant_message",
    "function_call",
]
