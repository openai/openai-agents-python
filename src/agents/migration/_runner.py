"""Run a bounded Classic agent with the native model and tool loop.

Requires the ``classic`` extra. SDKRunner returns a migration result rather
than a Classic RunResult; it does not convert or resume Classic RunState.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from copy import copy, deepcopy
from dataclasses import replace
from typing import Any, Literal, TypeVar, cast
from uuid import uuid4

from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallParam,
    ResponseOutputItemDoneEvent,
)
from pydantic import RootModel, TypeAdapter

from _agents_codex._compat import cancel_once
from _agents_codex._function_tools import FunctionTool, ToolContext
from _agents_codex._lifecycle import _error_contexts, _join, _retain_cleanup_error
from _agents_codex.agent import Agent as NativeAgent, NativeProvider
from _agents_codex.canonical_storage import CustomStorage
from _agents_codex.native_interception import Allow, Deny
from _agents_codex.prepared_request import PreparedRequest
from _agents_codex.recovery import ReconciledCall
from _agents_codex.session import Session, _close_after_error, _validate_storage_opening
from _agents_codex.state import LocalStorage, SessionState
from _agents_codex.tools import _CapturedFunction
from _agents_codex.turn import Turn

from .. import classic
from ..classic.exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError
from ..classic.items import (
    ItemHelpers,
    ModelResponse,
    TResponseInputItem,
    TResponseOutputItem,
    TResponseStreamEvent,
)
from ..classic.model_settings import ModelSettings, _coerce_model_settings
from ..classic.models._run_context import joined_model_stream_cleanup
from ..classic.models.interface import Model, ModelProvider, ModelTracing
from ..classic.run_config import RunConfig
from ..classic.run_context import RunContextWrapper
from ..classic.run_internal.turn_preparation import _model_settings_for_resolved_name
from ..classic.tool import FunctionTool as ClassicFunctionTool, ToolOriginType
from ..classic.tool_context import ToolContext as ClassicToolContext
from ..classic.usage import (
    Usage,
    _extract_raw_usage_snapshot,
    _requests_for_response_without_usage,
    _response_usage_to_usage,
    model_usage_to_span_usage,
)
from ._result import SDKRunResult, SDKRunResultStreaming, _ResultProjection
from ._session_history import _append_completed_history, _load_completed_history
from ._transport import _CARRIER_MODEL, _ResponseCarrier

__all__ = ("SDKRunner",)

ContextT = TypeVar("ContextT")


def _unsupported(feature: str) -> UserError:
    return UserError(f"SDKRunner does not support {feature}; use agents.classic.Runner instead")


def _validate_model_settings(settings: ModelSettings) -> None:
    for name in (
        "retry",
        "timeout",
        "context_management",
        "extra_query",
        "extra_body",
        "extra_headers",
        "extra_args",
    ):
        if getattr(settings, name) is not None:
            raise _unsupported(f"model_settings.{name}")
    if settings.tool_choice not in (None, "auto"):
        raise _unsupported("model_settings.tool_choice")


def _check_loopback_route(environment: Mapping[str, str], platform: str, os_name: str) -> None:
    proxy_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    requires_bypass = (
        platform == "darwin"
        or os_name == "nt"
        or any(environment.get(name) for name in proxy_names)
    )
    bypasses = [environment[name] for name in ("NO_PROXY", "no_proxy") if name in environment]
    if requires_bypass and (
        not bypasses
        or any("127.0.0.1" not in {part.strip() for part in value.split(",")} for value in bypasses)
    ):
        raise UserError(
            "SDKRunner's private transport requires the explicit 127.0.0.1 token in every present "
            "NO_PROXY/no_proxy value when HTTP_PROXY, HTTPS_PROXY, ALL_PROXY or their lowercase "
            "variants are configured, and on macOS/Windows. Configure the bypass before running; "
            "SDKRunner does not change proxy settings."
        )


def _capture_agent(
    agent: classic.Agent[Any],
    input: str,
    max_turns: int,
    provider: ModelProvider | None,
    *,
    has_approval_callback: bool,
    model_override: Model | str | None,
    settings_override: ModelSettings | dict[str, Any] | None,
) -> tuple[str, str | None, Model | str, ModelSettings, list[ClassicFunctionTool]]:
    if type(agent) is not classic.Agent:
        raise _unsupported("agent subclasses")
    if not isinstance(input, str):
        raise _unsupported("non-text input or Classic RunState")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise UserError("SDKRunner max_turns must be a positive integer")
    if agent.instructions is not None and not isinstance(agent.instructions, str):
        raise _unsupported("dynamic instructions")
    for name in ("handoffs", "mcp_servers", "input_guardrails", "output_guardrails"):
        if getattr(agent, name):
            raise _unsupported(name)
    for name in ("hooks", "prompt"):
        if getattr(agent, name) is not None:
            raise _unsupported(name)
    if agent.output_type not in (None, str):
        raise _unsupported("structured output")
    if agent.tool_use_behavior != "run_llm_again" or not agent.reset_tool_choice:
        raise _unsupported("custom tool-use behavior")
    model = agent.model if model_override is None else model_override
    if not isinstance(model, (str, Model)) or (isinstance(model, str) and not model):
        raise UserError(
            "SDKRunner requires an explicit Classic Model or model name and model_provider"
        )
    if isinstance(model, str) and not isinstance(provider, ModelProvider):
        raise UserError("SDKRunner requires model_provider for a model name")
    if isinstance(model, Model) and provider is not None:
        raise UserError("SDKRunner model_provider applies only to a model name")
    if settings_override is not None:
        settings_override = _coerce_model_settings(
            settings_override, parameter_name="SDKRunner model_settings"
        )
    # Validate caller settings before lookup. Implicit defaults depend on the
    # selected/resolved Model and are filled in after lookup, as in Classic.
    base_settings = (
        ModelSettings() if agent._uses_implicit_model_settings() else agent.model_settings
    )
    settings = deepcopy(base_settings.resolve(settings_override))
    _validate_model_settings(settings)
    tools: list[ClassicFunctionTool] = []
    names: set[str] = set()
    for tool in agent.tools:
        if type(tool) is not ClassicFunctionTool:
            raise _unsupported("non-function tools")
        if tool.is_enabled is not True:
            raise _unsupported("tool enablement")
        if tool.needs_approval is not False and not (
            tool.needs_approval is True and has_approval_callback
        ):
            raise _unsupported("tool approvals")
        for name in ("tool_input_guardrails", "tool_output_guardrails"):
            if getattr(tool, name):
                raise _unsupported(f"function tool {name}")
        for name in (
            "timeout_seconds",
            "custom_data_extractor",
            "allowed_callers",
            "output_json_schema",
            "_tool_namespace",
            "_mcp_tool_binding",
        ):
            if getattr(tool, name) is not None:
                raise _unsupported(f"function tool {name}")
        for name in ("defer_loading", "_is_agent_tool", "_is_codex_tool"):
            if getattr(tool, name) is not False:
                raise _unsupported(f"function tool {name}")
        if tool._tool_origin is not None and tool._tool_origin.type != ToolOriginType.FUNCTION:
            raise _unsupported("non-function tool origins")
        if not tool.name or tool.name in names:
            raise UserError("SDKRunner requires distinct nonempty function tool names")
        names.add(tool.name)
        captured = copy(tool)
        captured.params_json_schema = deepcopy(tool.params_json_schema)
        tools.append(captured)
    return agent.name, agent.instructions, model, settings, tools


def _native_tool(
    tool: ClassicFunctionTool,
    agent: classic.Agent[Any],
    run_context: RunContextWrapper[Any],
    calls: dict[str, ResponseFunctionToolCall],
    on_approval: Callable[[ClassicToolContext[Any]], Awaitable[Allow | Deny]] | None,
) -> FunctionTool[Any, Any, Any]:
    # RootModel carries the original JSON object; the Classic invoker owns its
    # schema validation, defaults, context injection and failure formatting.
    arguments = RootModel[dict[str, Any]]
    invoke = tool.on_invoke_tool
    spec = json.dumps(
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.params_json_schema,
            "deferLoading": False,
        },
        allow_nan=False,
    )

    def tool_context(native: ToolContext[Any]) -> ClassicToolContext[Any]:
        original = deepcopy(calls[native.call_id])
        return ClassicToolContext(
            run_context.context,
            run_context.usage,
            original.name,
            native.call_id,
            original.arguments,
            tool_call=original,
            agent=agent,
            turn_input=deepcopy(run_context.turn_input),
        )

    async def approve(native: ToolContext[Any]) -> Allow | Deny:
        assert on_approval is not None
        decision = await on_approval(tool_context(native))
        if not isinstance(decision, (Allow, Deny)):
            raise TypeError("SDKRunner on_approval must return Allow or Deny")
        return decision

    async def call(native: ToolContext[Any], *, root: dict[str, Any]) -> str:
        wrapper = tool_context(native)
        # Classic synchronous decorators may own executor workers. Join the
        # complete invocation before propagating cancellation or retiring tools.
        value = await _join(asyncio.ensure_future(invoke(wrapper, wrapper.tool_arguments)))
        if not isinstance(value, str):
            raise _unsupported("non-text function tool results")
        return value

    def bind(_context: Any, _declaration: Any) -> _CapturedFunction:
        return _CapturedFunction(
            name=tool.name,
            spec_json=spec,
            model=arguments,
            field_names=("root",),
            output_adapter=TypeAdapter(str),
            callback=call,
            context_parameter=inspect.Parameter("native", inspect.Parameter.POSITIONAL_ONLY),
            callback_timeout=None,
            before=approve if tool.needs_approval is True else None,
            after=None,
            context=run_context.context,
        )

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        input_type=arguments,
        output_type=str,
        _bind=bind,
    )


def _input_items(items: list[dict[str, Any]]) -> list[TResponseInputItem]:
    result: list[dict[str, Any]] = []
    for item in items:
        kind = item["type"]
        if kind == "message":
            # The public input is one user string. System/developer records
            # here belong to Codex's host prompt, not to the Classic agent.
            if item.get("role") in {"system", "developer"}:
                continue
            message = {key: item[key] for key in ("type", "role", "content")}
            if "phase" in item:
                message["phase"] = item["phase"]
            result.append(message)
        elif kind == "function_call":
            result.append({key: item[key] for key in ("type", "name", "call_id", "arguments")})
        elif kind == "function_call_output":
            result.append({key: item[key] for key in ("type", "call_id", "output")})
        elif kind == "reasoning":
            result.append(
                {
                    key: item[key]
                    for key in ("type", "id", "summary", "content", "encrypted_content")
                    if key in item
                }
            )
        else:
            raise _unsupported(f"native input item {kind}")
    return cast(list[TResponseInputItem], result)


def _response_bytes(
    response: ModelResponse,
    names: set[str],
    seen_calls: set[str],
    historical_calls: set[str],
) -> bytes:
    if not isinstance(response, ModelResponse):
        raise TypeError("Classic Model.get_response must return ModelResponse")
    output = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
    calls: set[str] = set()
    if not output:
        raise ModelBehaviorError("SDKRunner requires text or function calls in the model response")
    for item in output:
        if "provider_data" in item:
            raise _unsupported("model response provider_data")
        if item["type"] == "message":
            if (
                item.get("status") != "completed"
                or item.get("role") != "assistant"
                or not item.get("content")
                or any(part.get("type") != "output_text" for part in item["content"])
            ):
                raise _unsupported("non-text model messages")
        elif item["type"] == "function_call":
            if item.get("status") not in (None, "completed"):
                raise ModelBehaviorError("SDKRunner requires complete function calls")
            call_id = item.get("call_id")
            if call_id in historical_calls:
                raise ModelBehaviorError(
                    "SDKRunner received a function call ID already recorded in the Classic "
                    "Session. Check the recorded outcome using MIG.8 and use fresh IDs for "
                    "new calls."
                )
            if (
                not call_id
                or call_id in calls
                or call_id in seen_calls
                or item.get("name") not in names
                or item.get("namespace") is not None
            ):
                raise ModelBehaviorError("SDKRunner received an invalid function call identity")
            calls.add(call_id)
            try:
                arguments = json.loads(item["arguments"])
                json.dumps(arguments, allow_nan=False)
            except (ValueError, TypeError):
                # Raise outside the handler so parser exceptions cannot retain
                # private arguments through the propagated exception context.
                arguments = None
            if not isinstance(arguments, dict):
                raise ModelBehaviorError("SDKRunner requires JSON object function arguments")
        elif item["type"] != "reasoning":
            raise _unsupported(f"model response item {item['type']}")
    if all(item["type"] == "reasoning" for item in output):
        raise ModelBehaviorError("SDKRunner requires text or function calls in the model response")
    response_id = response.response_id or f"resp_{uuid4().hex}"
    events = [
        {"type": "response.created", "response": {"id": response_id}},
        *[{"type": "response.output_item.done", "item": item} for item in output],
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "usage": model_usage_to_span_usage(response.usage),
            },
        },
    ]
    payload = b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event, allow_nan=False)}\n\n".encode()
        for event in events
    )
    if len(payload) > 4 * 1024 * 1024:
        raise UserError("SDKRunner's private response transport exceeds 4 MiB")
    seen_calls.update(calls)
    return payload


async def _stream_response(
    create_stream: Callable[[], AsyncIterator[TResponseStreamEvent]],
    carrier: _ResponseCarrier,
    observer: Callable[[TResponseStreamEvent], None] | None,
    settings: ModelSettings,
) -> ModelResponse:
    """Settle one borrowed Model stream before exposing any native effects."""
    settling = False

    async def consume() -> ModelResponse:
        nonlocal settling
        with joined_model_stream_cleanup():
            stream = create_stream()
            completed: ResponseCompletedEvent | None = None
            output_items: list[TResponseOutputItem] = []
            serialized_bytes = 0

            async def close() -> None:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()

            try:
                async for event in stream:
                    if not carrier.active:
                        raise asyncio.CancelledError
                    serialized_bytes += len(
                        json.dumps(event.model_dump(mode="json"), allow_nan=False).encode()
                    )
                    if serialized_bytes > 4 * 1024 * 1024:
                        raise UserError("SDKRunner's model stream exceeds 4 MiB")
                    if isinstance(event, ResponseOutputItemDoneEvent):
                        output_items.append(deepcopy(event.item))
                    if event.type == "response.completed":
                        if (
                            not isinstance(event, ResponseCompletedEvent)
                            or event.response.status not in (None, "completed")
                            or event.response.error is not None
                            or event.response.incomplete_details is not None
                        ):
                            raise ModelBehaviorError(
                                "SDKRunner requires a valid response.completed"
                            )
                        # Neither an observer nor a producer reusing event objects can
                        # edit the response accepted after iterator cleanup.
                        completed = deepcopy(event)
                        # Completion ends consumption. Only same-owner cleanup may
                        # await after this event; never request a remote tail/EOF.
                        settling = True
                    if observer is not None:
                        if not carrier.active:
                            raise asyncio.CancelledError
                        observed = cast(Callable[[TResponseStreamEvent], object], observer)(
                            deepcopy(event)
                        )
                        if inspect.isawaitable(observed):
                            if inspect.iscoroutine(observed):
                                observed.close()
                            raise TypeError("SDKRunner on_model_event must be synchronous")
                    if event.type in {
                        "response.failed",
                        "response.incomplete",
                        "error",
                        "response.error",
                    }:
                        raise ModelBehaviorError(
                            "SDKRunner requires a successful completed model stream"
                        )
                    if completed is not None:
                        break
                if completed is None:
                    raise ModelBehaviorError(
                        "SDKRunner model stream ended without response.completed"
                    )
            except BaseException as error:
                settling = True
                previous = _error_contexts(error.__context__)
                try:
                    await close()
                except BaseException as cleanup_error:
                    _retain_cleanup_error(error, cleanup_error, previous)
                raise
            else:
                settling = True
                await close()
            if not carrier.active:
                raise asyncio.CancelledError
            response = completed.response
            # Match Classic adapters that emit finished items but omit terminal output.
            if not response.output:
                response.output = output_items
            return ModelResponse(
                output=response.output,
                usage=(
                    _response_usage_to_usage(response.usage)
                    if response.usage is not None
                    else Usage(requests=_requests_for_response_without_usage(response))
                ),
                response_id=response.id,
                request_id=getattr(response, "_request_id", None),
                raw_usage=(
                    _extract_raw_usage_snapshot(response)
                    if settings.preserve_raw_usage is True
                    else None
                ),
            )

    # Generator finalizers may reset ContextVar tokens or exit task-bound
    # scopes. Keep acquisition, iteration and close in one task. Once a terminal
    # is accepted or closing starts, protect settlement through aclose.
    owner = asyncio.create_task(consume())
    try:
        return await asyncio.shield(owner)
    except asyncio.CancelledError as error:
        previous = _error_contexts(error.__context__)
        if not settling:
            cancel_once(owner)
        try:
            await _join(owner)
        except BaseException as owner_error:
            _retain_cleanup_error(error, owner_error, previous)
        raise


class SDKRunner:
    """Run one static Classic agent through a single native turn."""

    @staticmethod
    async def run(
        starting_agent: classic.Agent[ContextT],
        input: str,
        *,
        context: ContextT | None = None,
        max_turns: int = 10,
        model_provider: ModelProvider | None = None,
        state: SessionState | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        reconciled_calls: Sequence[ReconciledCall] = (),
        return_state: bool = False,
        on_approval: Callable[[ClassicToolContext[ContextT]], Awaitable[Allow | Deny]]
        | None = None,
        session: classic.Session | None = None,
        model: Model | str | None = None,
        model_settings: ModelSettings | dict[str, Any] | None = None,
        model_invocation: Literal["response", "stream"] = "response",
        on_model_event: Callable[[TResponseStreamEvent], None] | None = None,
    ) -> SDKRunResult[ContextT]:
        """Submit new text with current Classic bindings and join native cleanup.

        Restore idle portable state or reopen native storage by Session ID.
        Set return_state to capture an idle portable checkpoint without storage.
        A static tool approval can await a decision inside this invocation.

        Pass a Classic session to read settled, uncompacted history and append the completed turn.
        SDKRunner uses OpenAIResponsesCompactionSession for storage only. Use
        agents.classic.Runner for automatic compaction or to continue opaque compacted history.

        Run model/settings overrides take precedence over the agent. Models and
        providers remain caller-owned. Stream observers receive tentative,
        detached Model events; await the result to confirm native success.
        """
        if model_invocation not in ("response", "stream"):
            raise ValueError("SDKRunner model_invocation must be response or stream")
        if on_model_event is not None:
            if (
                not callable(on_model_event)
                or inspect.iscoroutinefunction(on_model_event)
                # Inspect async callable objects without invoking their method.
                or inspect.iscoroutinefunction(getattr(on_model_event, "__call__", None))  # noqa: B004
            ):
                raise TypeError("SDKRunner on_model_event must be a synchronous callable")
            if model_invocation != "stream":
                raise ValueError("SDKRunner on_model_event requires model_invocation='stream'")
        if session is not None and (
            state is not None
            or storage is not None
            or resume is not None
            or reconciled_calls
            or return_state is True
        ):
            raise ValueError(
                "SDKRunner session cannot be combined with state, storage, resume, "
                "reconciled_calls, or return_state=True"
            )
        reconciliation = _validate_storage_opening(storage, state, resume, reconciled_calls)
        if not isinstance(return_state, bool):
            raise TypeError("return_state must be a bool")
        if return_state and storage is not None:
            raise ValueError("SDKRunner return_state cannot be combined with storage")
        if on_approval is not None and not callable(on_approval):
            raise TypeError("on_approval must be callable")
        if state is not None:
            if not isinstance(state, SessionState):
                raise TypeError("state must be a SessionState")
            # Keep the idle admission check and native opening on the same
            # snapshot even if the caller edits nested state during startup.
            state = state.model_copy(deep=True)
            if (
                state.native_continuation is not None
                or state.pending_actions
                or state.interrupted_thread_ids
                or state.recovery is not None
            ):
                raise ValueError("SDKRunner requires idle native state without pending recovery")
            metadata = state.root_metadata
            if metadata is None or metadata.get("model") != _CARRIER_MODEL:
                raise ValueError("SDKRunner requires state from a compatible native model profile")

        name, instructions, selected, settings, tools = _capture_agent(
            starting_agent,
            input,
            max_turns,
            model_provider,
            has_approval_callback=on_approval is not None,
            model_override=model,
            settings_override=model_settings,
        )
        implicit_settings = starting_agent._uses_implicit_model_settings()
        _check_loopback_route(os.environ, sys.platform, os.name)
        usage = Usage()
        run_context = RunContextWrapper(context=context, usage=usage)
        history = (
            await _load_completed_history(session, run_context) if session is not None else None
        )
        fresh_items = ItemHelpers.input_to_new_input_list(input) if history is not None else []
        classic_input = history.items + fresh_items if history is not None else None
        historical_calls = history.call_ids if history is not None else set()
        selected_model = (
            model_provider.get_model(selected)
            if isinstance(selected, str) and model_provider is not None
            else selected
        )
        if not isinstance(selected_model, Model):
            raise TypeError("model_provider.get_model must return a Classic Model")
        if implicit_settings:
            settings = deepcopy(
                _model_settings_for_resolved_name(
                    starting_agent, RunConfig(model=selected), selected_model
                ).resolve(settings)
            )
        _validate_model_settings(settings)
        effective_input = input if classic_input is None else classic_input
        projection = _ResultProjection(starting_agent, run_context, effective_input, tools=tools)
        current_calls: dict[str, ResponseFunctionToolCall] = {}
        native_tools = [
            _native_tool(tool, starting_agent, run_context, current_calls, on_approval)
            for tool in tools
        ]
        carrier = _ResponseCarrier()
        native_session: Session[Any, str] | None = None
        provider: NativeProvider | None = None
        turns = 0
        seen_calls: set[str] = set()
        native_input_prefix: TResponseInputItem | None = None

        async def prepare(
            turn: Turn[Any],
            request: PreparedRequest,
            *,
            preparation_index: int,
            recovery_attempt: int,
        ) -> None:
            nonlocal turns, native_input_prefix
            if not carrier.active or carrier.pending is not None or recovery_attempt != 0:
                raise UserError("SDKRunner cannot retry or recover a model response")
            if turns >= max_turns:
                raise MaxTurnsExceeded(f"Max turns ({max_turns}) exceeded")
            turns += 1
            translated = _input_items(request["input"])
            # The projected request may also contain calls from the current
            # turn. Reserve those IDs in addition to the full root history.
            seen_calls.update(
                cast(ResponseFunctionToolCallParam, item)["call_id"]
                for item in translated
                if item.get("type") == "function_call"
            )
            projection.accept_prepared_input(translated)
            model_input = translated
            if classic_input is not None:
                # Core only receives the fresh turn. Keep its carrier message
                # as a witness, and give the Model the exact Classic input.
                if native_input_prefix is None:
                    if (
                        len(translated) != 1
                        or translated[0].get("type") != "message"
                        or translated[0].get("role") != "user"
                    ):
                        raise ModelBehaviorError("SDKRunner expected one fresh native user message")
                    native_input_prefix = deepcopy(translated[0])
                elif not translated or translated[0] != native_input_prefix:
                    raise ModelBehaviorError("SDKRunner's native input prefix changed")
                model_input = deepcopy(classic_input) + translated[1:]
            input_snapshot = deepcopy(model_input)
            arguments: dict[str, Any] = {
                "system_instructions": instructions,
                "input": model_input,
                "model_settings": deepcopy(settings),
                "tools": list(tools),
                "output_schema": None,
                "handoffs": [],
                "tracing": ModelTracing.DISABLED,
                "previous_response_id": None,
                "conversation_id": None,
                "prompt": None,
            }
            if model_invocation == "stream":
                response = await _stream_response(
                    lambda: selected_model.stream_response(**arguments),
                    carrier,
                    on_model_event,
                    settings,
                )
            else:
                response = await selected_model.get_response(**arguments)
            # Cancellation can retire the owner while a custom Model suppresses
            # CancelledError. Such a result must never reach the native loop.
            if not carrier.active:
                raise asyncio.CancelledError
            payload = _response_bytes(
                response, {tool.name for tool in tools}, seen_calls, historical_calls
            )
            projection.accept_response(response)
            # Core waits for every tool before its next sampling callback.
            # These data belong only to this validated response's invocations.
            current_calls.clear()
            current_calls.update(
                {
                    item.call_id: deepcopy(item)
                    for item in response.output
                    if isinstance(item, ResponseFunctionToolCall)
                }
            )
            run_context.turn_input = input_snapshot
            usage.add(response.usage)
            carrier.pending = payload

        async def close() -> None:
            async def release_provider() -> None:
                if provider is not None:
                    await provider.aclose()

            try:
                try:
                    if native_session is not None:
                        await native_session._exit_scope()
                except BaseException as error:
                    await _close_after_error(error, release_provider)
                    raise
                else:
                    await release_provider()
            finally:
                await carrier.close()

        try:
            await carrier.start()
            provider = NativeProvider(
                base_url=carrier.url, api_key=carrier.key, supports_websockets=False
            )
            native_agent = NativeAgent[Any, str](
                name=name,
                instructions=instructions or "",
                tools=native_tools,
                model=_CARRIER_MODEL,
                provider=provider,
            )
            native_session = await Session[ContextT | None, str]._open(
                native_agent,
                context=context,
                state=state,
                storage=storage,
                resume=resume,
                reconciled_calls=reconciliation,
                _migration_transport=True,
            )
            native_session._fail_unhandled_questions = True
            if state is not None or resume is not None:
                # Native root records can outlive the context in a Model
                # request. Never admit a recorded call ID as a new effect.
                async for item in native_session.items():
                    if item["type"] == "function_call":
                        seen_calls.add(item["call_id"])
            turn = await native_session.submit(input, prepare_request=prepare)
            result = await turn.get_result()
            if return_state:
                result = replace(result, state=await native_session.save_state())
        except BaseException as error:
            carrier.retire()
            await _close_after_error(error, close)
            raise
        else:
            carrier.retire()
            await close()
            # The single terminal-success boundary for result projection
            # and history consumers. The exact turn and cleanup have settled.
            projected = projection.finish(result)
            if session is not None:
                await _append_completed_history(
                    session, [*fresh_items, *projected._new_input_items()], run_context
                )
            return projected

    @staticmethod
    def run_streamed(
        starting_agent: classic.Agent[ContextT],
        input: str,
        *,
        context: ContextT | None = None,
        max_turns: int = 10,
        model_provider: ModelProvider | None = None,
        state: SessionState | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        reconciled_calls: Sequence[ReconciledCall] = (),
        return_state: bool = False,
        on_approval: Callable[[ClassicToolContext[ContextT]], Awaitable[Allow | Deny]]
        | None = None,
        session: classic.Session | None = None,
        model: Model | str | None = None,
        model_settings: ModelSettings | dict[str, Any] | None = None,
        model_invocation: Literal["response", "stream"] = "response",
        on_model_event: Callable[[TResponseStreamEvent], None] | None = None,
    ) -> SDKRunResultStreaming[ContextT]:
        """Start a run in the current event loop and buffer its semantic events.

        The ``session`` parameter follows the same storage and compaction rules as ``run``.
        """
        return SDKRunResultStreaming(
            SDKRunner.run(
                starting_agent,
                input,
                context=context,
                max_turns=max_turns,
                model_provider=model_provider,
                state=state,
                storage=storage,
                resume=resume,
                reconciled_calls=reconciled_calls,
                return_state=return_state,
                on_approval=on_approval,
                session=session,
                model=model,
                model_settings=model_settings,
                model_invocation=model_invocation,
                on_model_event=on_model_event,
            )
        )
