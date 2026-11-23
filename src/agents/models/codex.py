from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from dataclasses import fields
from functools import partial
from importlib import import_module
from importlib.resources import files
from pathlib import Path
from typing import Any, ClassVar

from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseCustomToolCall,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseTextDeltaEvent,
    ResponseToolSearchCall,
    ResponseToolSearchOutputItem,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_reasoning_summary_part_added_event import Part as SummaryPart

from ..agent_output import AgentOutputSchemaBase
from ..exceptions import UserError
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..memory.session import await_session_mutation as _join_close
from ..model_settings import ModelSettings
from ..retry import ModelRetryAdvice, ModelRetryAdviceRequest
from ..run_config import ToolCallHookContext
from ..run_internal.tool_hooks import _await_hook, hook_payload_diagnostics
from ..tool import ApplyPatchTool, NativeTool, ShellTool, Tool, WebSearchTool
from ..usage import (
    Usage,
    _attach_raw_usage_snapshot,
    _extract_raw_usage_snapshot,
    _response_usage_to_usage,
)
from ._codex_inputs import prepare_codex_history, prepare_dynamic_tools, sdk_tool_call
from ._codex_items import (
    CodexCustomToolCallOutput,
    CodexFunctionToolCallOutput,
    CodexPendingCustomCall,
    CodexPendingFunctionCall,
)
from ._codex_tool_hooks import CodexToolHooks
from ._codex_tracing import CodexOperationTracing
from ._codex_usage import _add_sample_usage, _usage
from ._run_context import ModelToolHooks, get_model_tool_hooks
from .interface import Model, ModelProvider, ModelTracing


class CodexProvider(ModelProvider):
    """Run opt-in Agents SDK requests through embedded Codex Core.

    Codex handles workspace tools and authentication. Each request uses a fresh
    Codex thread. Pass a ResumableSession to the Runner to save a pending local
    command or edit for approval. The SDK session ID stays the same across workers.
    Supply the same SDK tool hooks on both workers. The user reviews the prepared
    input. Output preparation runs after execution.
    """

    _supports_stored_response_id = False

    def __init__(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        config_overrides: Sequence[str] = (),
        api_key: str | None = None,
    ) -> None:
        self._cwd = str(Path(cwd).resolve() if cwd is not None else Path.cwd())
        self._config_overrides = tuple(config_overrides)
        self._api_key = api_key
        self._manager: Any = None
        self._closed = False

    def _native_manager(self) -> Any:
        if self._closed:
            raise RuntimeError("CodexProvider is closed.")
        if self._manager is None:
            try:
                native = import_module("agents.models._codex_native")
            except ModuleNotFoundError as error:
                if error.name != "agents.models._codex_native":
                    raise
                raise UserError(
                    "Install a prebuilt SDK wheel with Codex support to use CodexProvider."
                ) from None
            executable = (
                files("agents.models")
                / "_codex_bin"
                / ("codex-sdk-runtime.exe" if os.name == "nt" else "codex-sdk-runtime")
            )
            self._manager = native.Manager(
                self._cwd, str(executable), list(self._config_overrides), self._api_key
            )
        return self._manager

    async def aclose(self) -> None:
        """Join native attempts and release this provider's shared services."""
        self._closed = True
        if self._manager is not None:
            await _join_close(self._manager.close())

    def get_model(self, model_name: str | None) -> Model:
        return _CodexModel(model_name, self)

    async def _run_approved_native_call(
        self, checkpoint: dict[str, Any], tracing: ModelTracing, hooks: CodexToolHooks | None
    ) -> str:
        """Run only the saved invocation, without a model turn or its old Codex home."""
        if checkpoint.get("cwd") != self._cwd:
            raise UserError("This Codex approval belongs to a different workspace or SDK version.")

        settings: dict[str, Any] = {"model": checkpoint["model"]}
        observations = CodexOperationTracing(set(), expect_model=False)
        resources = AsyncExitStack()
        watcher: asyncio.Task[None] | None = None
        failure: BaseException | None = None
        try:
            if hooks is not None:
                hooks.restore(checkpoint["call"])
                settings["sdk_tool_hooks"] = await hooks.configure(resources)
            thread = await self._native_manager().start_thread("[]", None, json.dumps(settings))
            resources.push_async_callback(thread.close)
            if hooks is not None:
                hooks.verify(thread)
            observations.bind(thread, _approval_tracing(tracing, hooks))
            watcher = asyncio.create_task(observations.watch())
            output = str(
                await thread.resolve_approval(
                    json.dumps(checkpoint["call"]),
                    json.dumps(checkpoint["request"]),
                    json.dumps(checkpoint["preconditions"]),
                )
            )
            return output if hooks is None else hooks.restored_output(output)
        except BaseException as error:
            failure = error
            raise
        finally:

            async def close() -> None:
                try:
                    await resources.aclose()
                finally:
                    if watcher is not None:
                        watcher.cancel()
                        await asyncio.gather(watcher, return_exceptions=True)
                    observations.close()

            try:
                await _join_close(close())
            except asyncio.CancelledError:
                if isinstance(failure, asyncio.CancelledError):
                    raise failure from None
                raise
            except Exception:
                if failure is None:
                    raise


def _pending_native_call(
    call: Any, event: dict[str, Any], cwd: str, model: str, hooks: CodexToolHooks | None = None
) -> CodexPendingFunctionCall | CodexPendingCustomCall:
    request = event["msg"]
    if hooks is not None:
        hooks.prepare_pending(call, event.get("input_hash"))
    wire = call.model_dump(exclude_none=True)
    if event.get("preconditions") is None:
        raise UserError("Codex could not confirm that this tool is still waiting to run.")
    checkpoint = {
        "version": 1,
        "cwd": cwd,
        "model": model,
        "call": wire,
        "request": request,
        "preconditions": event["preconditions"],
    }
    if hooks is not None:
        checkpoint.update(version=2, hooks=hooks.required)
    if getattr(call, "namespace", None) not in (None, "functions"):
        raise UserError("Only built-in Codex tools support native approval recovery.")
    if (
        isinstance(call, ResponseFunctionToolCall)
        and call.name == "exec_command"
        and request["type"] == "exec_approval_request"
        and request.get("kind", "command") == "command"
        and request.get("approval_id") is None
        and request.get("network_approval_context") is None
        and request.get("environmentId") in (None, "local")
    ):
        return CodexPendingFunctionCall(**wire, checkpoint=checkpoint)
    if (
        isinstance(call, ResponseCustomToolCall)
        and call.name == "apply_patch"
        and request["type"] == "apply_patch_approval_request"
    ):
        return CodexPendingCustomCall(**wire, checkpoint=checkpoint)
    raise UserError("This Codex approval cannot safely be resumed on another worker.")


def _uses_sdk_workspace(tools: list[Tool], bindings: ModelToolHooks | None) -> bool:
    settings = bindings.settings if bindings else None
    return bool(
        settings
        and settings.sdk_only_tool_hooks
        and (settings.prepare_tool_input or settings.prepare_tool_output)
    ) or any(
        getattr(tool, "_sdk_sandbox", False) or isinstance(tool, (ShellTool, ApplyPatchTool))
        for tool in tools
    )


def _settings(settings: ModelSettings) -> tuple[str | None, str | None]:
    supported = {
        "timeout",
        "retry",
        "preserve_raw_usage",
        "reasoning",
        "verbosity",
        "response_include",
    }
    for field in fields(settings):
        value = getattr(settings, field.name)
        if field.name in supported or value is None:
            continue
        if (field.name == "tool_choice" and value in ("auto", "required")) or (
            field.name == "store" and value is False
        ):
            continue
        raise UserError(f"CodexProvider does not support ModelSettings.{field.name}.")
    if settings.response_include is not None and any(
        value != "reasoning.encrypted_content" for value in settings.response_include
    ):
        raise UserError(
            "CodexProvider supports only response_include=['reasoning.encrypted_content']."
        )
    reasoning = settings.reasoning
    if reasoning is not None:
        unsupported = set(reasoning.model_dump(exclude_none=True)) - {"effort", "summary"}
        if unsupported:
            raise UserError(
                f"CodexProvider does not support ModelSettings.reasoning.{sorted(unsupported)[0]}."
            )
    return (reasoning.effort if reasoning else None, reasoning.summary if reasoning else None)


def _validate_native_restore(
    tools: list[Tool], bindings: ModelToolHooks | None, checkpoint: dict[str, Any]
) -> CodexToolHooks | None:
    hooks = CodexToolHooks.for_run(bindings, set())
    if (
        _uses_sdk_workspace(tools, bindings)
        or checkpoint.get("version") != (2 if hooks else 1)
        or checkpoint.get("hooks") != (hooks.required if hooks else None)
    ):
        raise UserError("Use the same native workspace and native tool hooks to restore this call.")
    return hooks


def _approval_tracing(tracing: ModelTracing, hooks: CodexToolHooks | None) -> ModelTracing:
    # Native model and tool events can contain payloads from before preparation.
    settings = hooks.settings if hooks is not None else None
    if (
        tracing.include_data()
        and settings
        and (settings.prepare_tool_input is not None or settings.prepare_tool_output is not None)
    ):
        return ModelTracing.ENABLED_WITHOUT_DATA
    return tracing


class _CodexModel(Model):
    _supports_stored_response_id = False
    _sandbox_model_capabilities: ClassVar[dict[str, Any]] = {
        "responses_compaction": False,
        "hosted_apply_patch": False,
        "tool_namespace": "agents_sdk",
        "use_provider_defaults": True,
    }

    def __init__(self, name: str | None, provider: CodexProvider) -> None:
        self._name = name
        self._provider = provider

    def get_default_model_settings(self) -> ModelSettings:
        return ModelSettings()

    async def _run_approved_native_call(
        self, checkpoint: dict[str, Any], *, tools: list[Tool], tracing: ModelTracing
    ) -> str:
        hooks = _validate_native_restore(tools, get_model_tool_hooks(), checkpoint)
        return await self._provider._run_approved_native_call(checkpoint, tracing, hooks)

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> ModelRetryAdvice:
        # Native Codex tools may have changed the workspace before a failed response.
        return ModelRetryAdvice(replay_safety="unsafe")

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
        async for event in self.stream_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        ):
            if isinstance(event, ResponseCompletedEvent):
                response = event.response
                return ModelResponse(
                    output=response.output,
                    usage=_response_usage_to_usage(response.usage)
                    if response.usage
                    else Usage(requests=1),
                    response_id=None,
                    raw_usage=_extract_raw_usage_snapshot(response),
                )
        raise RuntimeError("Codex finished without a completed response.")

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
        if previous_response_id is not None or conversation_id is not None or prompt is not None:
            raise UserError("CodexProvider does not support Responses API state or prompt IDs.")
        effort, summary = _settings(model_settings)
        history, turn_input = prepare_codex_history(input, tools)
        dynamic_tools, identities = prepare_dynamic_tools(tools, handoffs)
        require_sdk_call = model_settings.tool_choice == "required"
        if require_sdk_call:
            if not identities:
                raise UserError(
                    "CodexProvider tool_choice='required' needs an exposed SDK tool or handoff."
                )
            system_instructions = "\n\n".join(
                filter(
                    None,
                    [
                        system_instructions,
                        "You must call an exposed SDK tool or handoff before answering. "
                        "Native Codex tools and plain text do not satisfy this requirement.",
                    ],
                )
            )
        if (
            output_schema is not None
            and not output_schema.is_plain_text()
            and not output_schema.is_strict_json_schema()
        ):
            # Stock outputSchema always enforces strict JSON.
            # Non-strict schemas guide generation; the Runner still validates the final JSON.
            system_instructions = "\n\n".join(
                filter(
                    None,
                    [
                        system_instructions,
                        "When returning your final answer, return only a JSON value matching "
                        "this schema without Markdown fences. The schema is guidance, "
                        "not a server-enforced constraint:\n"
                        + json.dumps(output_schema.json_schema()),
                    ],
                )
            )
        provider = self._provider
        bindings = get_model_tool_hooks()
        sdk_sandbox = _uses_sdk_workspace(tools, bindings)
        tool_execution = bindings.settings if bindings is not None else None
        transforms = tool_execution._native_mcp_editors() if tool_execution else {}
        if transforms and (sdk_sandbox or (tool_execution and tool_execution.sdk_only_tool_hooks)):
            raise UserError("Native MCP result transforms require native Codex tools.")
        native_hooks = None if sdk_sandbox else CodexToolHooks.for_run(bindings, identities)
        if sdk_sandbox:
            system_instructions = "\n\n".join(
                filter(
                    None,
                    [
                        system_instructions,
                        "Use the SDK-provided tools for workspace commands, reads, and edits. "
                        "When agents_sdk tools are available, use that namespace for workspace "
                        "operations; native Codex workspace tools are unavailable for this run.",
                    ],
                )
            )
        deferred_namespaces = {
            tool["name"]
            for tool in dynamic_tools
            if tool.get("type") == "namespace"
            and any(member.get("deferLoading") for member in tool["tools"])
        }
        if "functions" in deferred_namespaces:
            raise UserError(
                "Codex native tool discovery reserves the functions namespace; "
                "use another namespace for deferred SDK tools."
            )
        web_tools = [tool for tool in tools if isinstance(tool, WebSearchTool)]
        if len(web_tools) > 1:
            raise UserError("CodexProvider supports one WebSearchTool per request.")
        web_request = None
        if web_tools:
            web = web_tools[0]
            if sdk_sandbox:
                raise UserError("Expose web search through SDK sandbox tools with CodexProvider.")
            if web.image_settings is not None:
                raise UserError("CodexProvider does not support web search image settings.")
            location = dict(web.user_location) if web.user_location is not None else None
            if location is not None and location.pop("type", "approximate") != "approximate":
                raise UserError("CodexProvider supports approximate web search locations.")
            web_request = {
                "external_web_access": web.external_web_access,
                "search_context_size": web.search_context_size,
                "allowed_domains": web.filters.allowed_domains if web.filters else None,
                "user_location": location,
                "search_content_types": web.search_content_types,
            }
        settings = {
            "model": self._name,
            "reasoning_effort": effort,
            "reasoning_summary": summary,
            "verbosity": model_settings.verbosity,
            "sdk_sandbox": sdk_sandbox,
            "require_sdk_call": require_sdk_call,
            "deferred_namespaces": sorted(deferred_namespaces),
            "web_search": web_request,
            "has_audio": any(part.get("type") == "audio" for part in turn_input)
            or any(
                part.get("type") == "input_audio"
                for item in history
                for part in (item.get("content") or item.get("output") or [])
                if isinstance(part, dict)
            ),
        }
        schema = (
            output_schema.json_schema()
            if output_schema is not None
            and not output_schema.is_plain_text()
            and output_schema.is_strict_json_schema()
            else None
        )
        observations = CodexOperationTracing(identities, input)
        completed: Response | None = None
        resources = AsyncExitStack()
        output: dict[str, Any] = {}
        streamed: set[str] = set()
        indices: dict[str, int] = {}
        started: set[str] = set()
        finished: set[str] = set()
        sdk_calls: dict[str, Any] = {}
        native_pending: dict[str, tuple[str, str]] = {}
        native_completed: set[str] = set()
        pending_approval: dict[str, Any] | None = None
        sampling_pending = False
        interrupt_requested = False
        sequence = 0
        raw_usage: dict[str, Any] | None = None
        reasoning_summaries: list[str] = []
        usage = None
        failure: BaseException | None = None

        async def interrupt_when_ready() -> None:
            nonlocal interrupt_requested
            if not interrupt_requested and sdk_calls and not sampling_pending:
                await thread.interrupt()
                interrupt_requested = True

        try:
            if native_hooks is not None:
                settings["sdk_tool_hooks"] = await native_hooks.configure(resources)
            thread = await provider._native_manager().start_thread(
                json.dumps(dynamic_tools),
                system_instructions,
                json.dumps(settings),
                **({"mcp_tools": list(transforms)} if transforms else {}),
            )
            resources.push_async_callback(thread.close)
            if native_hooks is not None:
                native_hooks.verify(thread)
            if history:
                await thread.inject_items(json.dumps(history))
            recover = bool(
                bindings and bindings.resumable and not sdk_sandbox and not require_sdk_call
            )
            observations.bind(
                thread, _approval_tracing(tracing, native_hooks) if recover else tracing
            )
            turn_id = await thread.start_turn(json.dumps(turn_input), json.dumps(schema), recover)
            response = Response(
                id=turn_id,
                created_at=time.time(),
                model=thread.model,
                object="response",
                output=[],
                parallel_tool_calls=False,
                status="in_progress",
                tool_choice="required" if require_sdk_call else "auto",
                tools=[],
            )
            yield ResponseCreatedEvent(
                response=response, sequence_number=sequence, type="response.created"
            )
            sequence += 1
            while True:
                if pending_approval is not None and not sampling_pending:
                    # All calls from this sampling response are now known. A sibling
                    # still in flight has an unknown outcome and cannot be dropped.
                    call_id = pending_approval["msg"]["call_id"]
                    if set(native_pending) != {call_id} or sdk_calls:
                        raise UserError("Codex can only pause one outstanding native call.")
                    item_id, _ = native_pending.pop(call_id)
                    output[item_id] = _pending_native_call(
                        output[item_id], pending_approval, provider._cwd, thread.model, native_hooks
                    )
                    # Close the waiting thread before handing the pause to the Runner.
                    break
                encoded = await thread.next_event()
                observations.drain()
                if encoded is None:
                    raise RuntimeError("Codex finished without a completed response.")
                if transforms and bindings is not None and not isinstance(encoded, str):
                    try:
                        request = json.loads(encoded.request)
                        if request["turn_id"] != turn_id:
                            raise RuntimeError("Unexpected native MCP result turn.")
                        server, name = request["server"], request["name"]
                        mcp_call = ToolCallHookContext(
                            bindings.context,
                            bindings.agent,
                            NativeTool(name),
                            request["call_id"],
                            server,
                            request["arguments"],
                        )
                        if native_hooks is not None:
                            native_hooks.confirm_mcp_result(mcp_call.call_id, mcp_call.arguments)
                        with hook_payload_diagnostics(
                            "Native MCP result transform failed or returned an invalid replacement "
                            "(limit 1 MiB)."
                        ):
                            replacement = await _await_hook(
                                partial(transforms[server, name], mcp_call, request["result"]),
                                "native_mcp_result_transforms",
                                name,
                            )
                            encoded.respond(
                                json.dumps(
                                    replacement,
                                    allow_nan=False,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            )
                    finally:
                        encoded.discard()
                    continue
                event = json.loads(encoded)
                message = event["msg"]
                kind = message["type"]
                if kind == "hook_completed" and native_hooks is not None:
                    native_hooks.check_completion(message["run"])
                    continue
                if kind == "context_compacted" or (
                    kind in ("item_started", "item_completed")
                    and message["item"]["type"] == "ContextCompaction"
                ):
                    raise UserError(
                        "Codex native context compaction cannot be replayed by this provider. "
                        "Use SDK-managed input compaction before starting another model call."
                    )
                if event.get("id") not in ("", turn_id):
                    continue
                if kind in ("exec_approval_request", "apply_patch_approval_request"):
                    if (
                        message.get("turn_id") != turn_id
                        or pending_approval is not None
                        or (native_hooks is not None and not recover)
                        or sdk_sandbox
                        or require_sdk_call
                    ):
                        raise UserError(
                            "Codex can only pause a single built-in tool call before execution, "
                            "with native workspace tools enabled."
                        )
                    pending_approval = event
                    continue
                if kind == "dynamic_tool_call_request":
                    if message.get("turnId") != turn_id:
                        continue
                    call = sdk_tool_call(message, identities, tools)
                    call_id = message["callId"]
                    if call_id in native_completed:
                        raise RuntimeError("Codex reused a completed native tool call ID.")
                    pending = native_pending.pop(call_id, None)
                    if pending is not None:
                        output.pop(pending[0], None)
                        indices[call_id] = indices.pop(pending[0])
                    if call_id in sdk_calls:
                        continue
                    call = call.model_copy(update={"id": call_id})
                    sdk_calls[call_id] = call
                    indices.setdefault(call_id, len(indices))
                    changes: dict[str, Any] = {"status": "in_progress"}
                    if call.type == "function_call":
                        changes["arguments"] = ""
                    yield ResponseOutputItemAddedEvent(
                        item=call.model_copy(update=changes),
                        output_index=indices[call_id],
                        sequence_number=sequence,
                        type="response.output_item.added",
                    )
                    sequence += 1
                    if call.type == "function_call":
                        yield ResponseFunctionCallArgumentsDeltaEvent(
                            delta=call.arguments,
                            item_id=call_id,
                            output_index=indices[call_id],
                            sequence_number=sequence,
                            type="response.function_call_arguments.delta",
                        )
                        sequence += 1
                        yield ResponseFunctionCallArgumentsDoneEvent(
                            arguments=call.arguments,
                            item_id=call_id,
                            output_index=indices[call_id],
                            sequence_number=sequence,
                            type="response.function_call_arguments.done",
                        )
                        sequence += 1
                    # A tool may start while its model response is still streaming.
                    # Drain that sampling response before handing actual admissions
                    # to the Runner; other raw calls may never pass native tool hooks.
                    await interrupt_when_ready()
                    continue
                if kind in ("task_complete", "turn_aborted"):
                    if kind != ("turn_aborted" if interrupt_requested else "task_complete") or (
                        sdk_calls and not interrupt_requested
                    ):
                        raise RuntimeError("Codex did not finish the turn successfully.")
                    if native_hooks is not None and native_hooks.error is not None:
                        raise native_hooks.error
                    break
                if kind == "error":
                    raise RuntimeError("Codex model turn failed.")
                if kind == "token_count":
                    info = message.get("info")
                    total = (info or {}).get("total_token_usage")
                    if total is not None:
                        usage = _usage(total)
                    if model_settings.preserve_raw_usage and info is not None:
                        if raw_usage is None:
                            raw_usage = {"responses": []}
                        raw_usage["token_count"] = info
                    continue
                if kind == "raw_response_completed":
                    # An SDK tool can interrupt before the next cumulative token count.
                    if message.get("token_usage") is not None:
                        usage = _add_sample_usage(usage, message["token_usage"])
                    if (
                        model_settings.preserve_raw_usage
                        and message.get("usage_metadata") is not None
                    ):
                        if raw_usage is None:
                            raw_usage = {"responses": []}
                        raw_usage["responses"].append(message["usage_metadata"])
                    sampling_pending = False
                    await interrupt_when_ready()
                    continue
                if kind == "model_reroute":
                    response = response.model_copy(update={"model": message["to_model"]})
                    continue
                if summary is not None and kind in (
                    "agent_reasoning_section_break",
                    "reasoning_content_delta",
                ):
                    item_id = message["item_id"]
                    indices.setdefault(item_id, len(indices))
                    if kind == "agent_reasoning_section_break":
                        yield ResponseReasoningSummaryPartAddedEvent(
                            item_id=item_id,
                            output_index=indices[item_id],
                            part=SummaryPart(type="summary_text", text=""),
                            summary_index=message["summary_index"],
                            sequence_number=sequence,
                            type="response.reasoning_summary_part.added",
                        )
                    else:
                        yield ResponseReasoningSummaryTextDeltaEvent(
                            item_id=item_id,
                            output_index=indices[item_id],
                            delta=message["delta"],
                            summary_index=message["summary_index"],
                            sequence_number=sequence,
                            type="response.reasoning_summary_text.delta",
                        )
                    sequence += 1
                    continue
                if kind == "raw_response_item":
                    raw = message["item"]
                    item_type = raw.get("type")
                    if item_type in ("function_call", "custom_tool_call"):
                        # Core records the outer model call before dispatching it,
                        # including calls that later invoke SDK tools from code mode.
                        sampling_pending = True
                    parsed: Any = None
                    if item_type in ("function_call", "custom_tool_call"):
                        call_id = raw.get("call_id")
                        if call_id in sdk_calls:
                            continue
                        if (
                            not isinstance(call_id, str)
                            or not call_id
                            or call_id in native_pending
                            or call_id in native_completed
                        ):
                            raise RuntimeError("Codex returned an invalid native tool call.")
                        item_id = raw.get("id") or call_id
                        call_type = (
                            ResponseFunctionToolCall
                            if item_type == "function_call"
                            else ResponseCustomToolCall
                        )
                        call_keys = (
                            "type",
                            "call_id",
                            "name",
                            "namespace",
                            "arguments",
                            "input",
                            "status",
                            "encrypted_content",
                        )
                        parsed = call_type.model_validate(
                            {"id": item_id, **{key: raw[key] for key in call_keys if key in raw}}
                        )
                        native_pending[call_id] = (item_id, item_type)
                    elif item_type in ("function_call_output", "custom_tool_call_output"):
                        call_id = raw.get("call_id")
                        # The Runner owns SDK results, including native cancellation results.
                        if call_id in sdk_calls:
                            continue
                        pending = native_pending.pop(call_id, None)
                        if pending is None or item_type != pending[1] + "_output":
                            raise RuntimeError("Codex returned an unmatched native tool output.")
                        output_type = (
                            CodexFunctionToolCallOutput
                            if item_type == "function_call_output"
                            else CodexCustomToolCallOutput
                        )
                        native_output = raw["output"]
                        if isinstance(native_output, list):
                            # Core omits default image detail; Responses output models require it.
                            native_output = [
                                {**part, "detail": "auto"}
                                if part.get("type") == "input_image" and part.get("detail") is None
                                else part
                                for part in native_output
                            ]
                        output_keys = ("type", "call_id", "name", "namespace")
                        parsed = output_type.model_validate(
                            {
                                "id": raw.get("id") or call_id + "_output",
                                "status": "completed",
                                "output": native_output,
                                **{key: raw[key] for key in output_keys if key in raw},
                            }
                        )
                        native_completed.add(call_id)
                    elif item_type == "reasoning":
                        parsed = ResponseReasoningItem.model_validate(raw)
                    elif item_type == "web_search_call":
                        parsed = ResponseFunctionWebSearch.model_validate(raw)
                    elif item_type == "tool_search_call":
                        parsed = ResponseToolSearchCall.model_validate(
                            {"status": "completed", **raw}
                        )
                    elif item_type == "tool_search_output":
                        parsed = ResponseToolSearchOutputItem.model_validate(raw)
                    if parsed is not None:
                        output[parsed.id] = parsed
                        indices.setdefault(parsed.id, len(indices))
                    continue
                delta = None
                done_message = None
                item_id = message.get("item_id")
                if kind == "agent_message_content_delta":
                    delta = message["delta"]
                    indices.setdefault(item_id, len(indices))
                    streamed.add(item_id)
                elif kind in ("item_started", "item_completed"):
                    item = message["item"]
                    if (
                        item["type"] == "Reasoning"
                        and kind == "item_completed"
                        and summary is not None
                    ):
                        reasoning_summaries.extend(item["summary_text"])
                    if item["type"] != "AgentMessage":
                        continue
                    item_id = item["id"]
                    indices.setdefault(item_id, len(indices))
                    if kind == "item_completed":
                        if item_id in finished:
                            continue
                        text = "".join(
                            part["text"] for part in item["content"] if part["type"] == "Text"
                        )
                        if item_id not in streamed:
                            delta = text
                        done_message = ResponseOutputMessage(
                            id=item_id,
                            type="message",
                            role="assistant",
                            status="completed",
                            content=[
                                ResponseOutputText(type="output_text", text=text, annotations=[])
                            ],
                        )
                        if item.get("phase") in ("commentary", "final_answer"):
                            done_message.phase = item["phase"]
                        output[item_id] = done_message
                else:
                    continue
                assert isinstance(item_id, str)
                if item_id not in started:
                    started.add(item_id)
                    yield ResponseOutputItemAddedEvent(
                        item=ResponseOutputMessage(
                            id=item_id,
                            type="message",
                            role="assistant",
                            status="in_progress",
                            content=[],
                        ),
                        output_index=indices[item_id],
                        sequence_number=sequence,
                        type="response.output_item.added",
                    )
                    sequence += 1
                if delta is not None:
                    yield ResponseTextDeltaEvent(
                        content_index=0,
                        delta=delta,
                        item_id=item_id,
                        logprobs=[],
                        output_index=indices[item_id],
                        sequence_number=sequence,
                        type="response.output_text.delta",
                    )
                    sequence += 1
                if done_message is not None:
                    finished.add(item_id)
                    yield ResponseOutputItemDoneEvent(
                        item=done_message,
                        output_index=indices[item_id],
                        sequence_number=sequence,
                        type="response.output_item.done",
                    )
                    sequence += 1
            if native_pending:
                if not interrupt_requested:
                    raise RuntimeError("Codex finished with an incomplete native tool call.")
                # Interrupted code-mode parents have no completed result to replay.
                for item_id, _ in native_pending.values():
                    output.pop(item_id, None)
            if native_hooks is not None:
                for item in output.values():
                    native_hooks.prepare_history_item(item)
            if require_sdk_call and not sdk_calls:
                raise UserError(
                    "CodexProvider tool_choice='required' did not return an SDK tool "
                    "or handoff call."
                )
            if not sdk_calls and not output:
                raise RuntimeError("Codex finished without a completed response.")
            items = list(output.values()) + list(sdk_calls.values())
            if reasoning_summaries:
                items[-1].codex_reasoning_summary = reasoning_summaries
            items.sort(key=lambda item: indices[item.id])
            completed = response.model_copy(
                update={"output": items, "status": "completed", "usage": usage}
            )
            if raw_usage is not None:
                _attach_raw_usage_snapshot(completed, raw_usage)
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                await _join_close(resources.aclose())
            except Exception:
                # Keep the attempt failure primary when joined cleanup also fails.
                if failure is None:
                    raise
            finally:
                # Pending items carry a private recovery checkpoint.
                observations.close(completed if pending_approval is None else None)
        for native_item in output.values():
            if native_item.type not in (
                "function_call",
                "custom_tool_call",
                "function_call_output",
                "custom_tool_call_output",
            ):
                continue
            yield ResponseOutputItemDoneEvent(
                item=native_item,
                output_index=indices[native_item.id],
                sequence_number=sequence,
                type="response.output_item.done",
            )
            sequence += 1
        for call_id, call in sdk_calls.items():
            yield ResponseOutputItemDoneEvent(
                item=call,
                output_index=indices[call_id],
                sequence_number=sequence,
                type="response.output_item.done",
            )
            sequence += 1
        yield ResponseCompletedEvent(
            response=completed, sequence_number=sequence, type="response.completed"
        )
