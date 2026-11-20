from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any, ClassVar

from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
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
    ResponseUsage,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_reasoning_summary_part_added_event import Part as SummaryPart
from openai.types.responses.response_usage import OutputTokensDetails

from ..agent_output import AgentOutputSchemaBase
from ..exceptions import UserError
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..model_settings import ModelSettings
from ..retry import ModelRetryAdvice, ModelRetryAdviceRequest
from ..tool import ApplyPatchTool, ShellTool, Tool, WebSearchTool
from ..usage import Usage, _make_input_tokens_details, _response_usage_to_usage
from ._codex_app_server import CodexAppServer, resolve_codex_executable
from ._codex_inputs import prepare_codex_history, prepare_dynamic_tools, sdk_tool_call
from ._codex_tracing import CodexOperationTracing
from .interface import Model, ModelProvider, ModelTracing


class CodexProvider(ModelProvider):
    """Run Agents SDK requests through the stock Codex App Server.

    Codex owns workspace tools and authentication; the Runner owns SDK function tools,
    handoffs, approvals, and conversation history. Each request uses an ephemeral thread.
    """

    _supports_stored_response_id = False

    def __init__(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        codex_executable: str | os.PathLike[str] | None = None,
        config_overrides: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        api_key: str | None = None,
    ) -> None:
        self._cwd = str(Path(cwd).resolve() if cwd is not None else Path.cwd())
        self._codex_executable = (
            Path(codex_executable).absolute() if codex_executable is not None else None
        )
        self._config_overrides = tuple(config_overrides)
        self._env = dict(env) if env is not None else None
        self._api_key = api_key

    def get_model(self, model_name: str | None) -> Model:
        return _CodexModel(model_name, self)


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


def _id(result: Any, field: str) -> str:
    value = result.get(field, {}).get("id") if isinstance(result, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Codex did not return a {field} ID.")
    return value


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
                    raw_usage=getattr(response, "codex_raw_usage", None),
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
        sdk_sandbox = any(
            getattr(tool, "_sdk_sandbox", False) or isinstance(tool, (ShellTool, ApplyPatchTool))
            for tool in tools
        )
        overrides = list(provider._config_overrides)
        if sdk_sandbox or require_sdk_call:
            overrides.extend(("features.shell_tool=false", "features.view_image=false"))
        if sdk_sandbox:
            system_instructions = "\n\n".join(
                filter(
                    None,
                    [
                        system_instructions,
                        "Use the SDK-provided tools for workspace commands, reads, and edits. "
                        "When agents_sdk tools are available, use that namespace for workspace "
                        "operations; "
                        "native Codex workspace tools are unavailable for this run.",
                    ],
                )
            )
        if sdk_sandbox:
            overrides.extend(
                (
                    "features.apps=false",
                    "features.plugins=false",
                    "features.multi_agent=false",
                    "features.multi_agent_v2=false",
                    "features.hooks=false",
                    "features.code_mode=false",
                    "notify=[]",
                    'web_search="disabled"',
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
        if deferred_namespaces:
            overrides.append("features.code_mode=false")
        web_tools = [tool for tool in tools if isinstance(tool, WebSearchTool)]
        if len(web_tools) > 1:
            raise UserError("CodexProvider supports one WebSearchTool per request.")
        if web_tools:
            if sdk_sandbox:
                raise UserError("Expose web search through SDK sandbox tools with CodexProvider.")
            web = web_tools[0]
            if web.image_settings is not None:
                raise UserError("CodexProvider does not support web search image settings.")
            overrides.append("features.standalone_web_search=false")
            overrides.append(
                'web_search="cached"' if web.external_web_access is False else 'web_search="live"'
            )
            overrides.append("tools.web_search.context_size=" + json.dumps(web.search_context_size))
            if web.filters is not None:
                if web.filters.allowed_domains is not None:
                    overrides.append(
                        "tools.web_search.allowed_domains="
                        + json.dumps(web.filters.allowed_domains)
                    )
            if web.user_location is not None:
                location = dict(web.user_location)
                if location.pop("type", "approximate") != "approximate":
                    raise UserError("CodexProvider supports approximate web search locations.")
                for key, value in location.items():
                    if value is not None:
                        overrides.append(
                            "tools.web_search.location." + key + "=" + json.dumps(value)
                        )
        if provider._api_key is not None:
            overrides.append('cli_auth_credentials_store="ephemeral"')
        server = await CodexAppServer.open_initialized(
            resolve_codex_executable(provider._codex_executable),
            provider._cwd,
            overrides,
            provider._env,
        )
        observations = CodexOperationTracing(tracing, input, self._name)
        output: dict[str, Any] = {}
        streamed: set[str] = set()
        indices: dict[str, int] = {}
        started: set[str] = set()
        finished: set[str] = set()
        sdk_call = None
        completed = False
        sequence = 0
        raw_usage = None
        reasoning_summaries: list[str] = []
        usage = None
        sdk_done_event = None
        try:
            if provider._api_key is not None:
                await server.request(
                    "account/login/start", {"type": "apiKey", "apiKey": provider._api_key}
                )
            thread_config: dict[str, Any] = {}
            if sdk_sandbox or deferred_namespaces:
                config = (
                    await server.request(
                        "config/read", {"cwd": provider._cwd, "includeLayers": False}
                    )
                ).get("config")
                if not isinstance(config, dict):
                    raise UserError("CodexProvider could not verify SDK tool configuration.")
                if sdk_sandbox and any(
                    entry.get("enabled") is not False
                    for entry in (config.get("mcp_servers") or {}).values()
                ):
                    raise UserError(
                        "CodexProvider with SandboxAgent requires Codex MCP servers "
                        "to be disabled; "
                        "expose tools through the SDK instead."
                    )
                code_mode = (config.get("features") or {}).get("code_mode") or {}
                configured_direct = (
                    code_mode.get("direct_only_tool_namespaces", [])
                    if isinstance(code_mode, dict)
                    else []
                )
                direct_namespaces = [
                    name for name in configured_direct if name not in deferred_namespaces
                ]
                if deferred_namespaces and "functions" not in direct_namespaces:
                    direct_namespaces.append("functions")
                if (
                    sdk_sandbox
                    and "agents_sdk" not in deferred_namespaces
                    and "agents_sdk" not in direct_namespaces
                ):
                    direct_namespaces.append("agents_sdk")
                thread_config["features.code_mode.direct_only_tool_namespaces"] = direct_namespaces
                if deferred_namespaces:
                    thread_config["features.code_mode_tool_search"] = True
            if web_tools:
                capabilities = await server.request("modelProvider/capabilities/read", {})
                if capabilities.get("webSearch") is not True:
                    raise UserError(
                        "The configured Codex model provider does not support hosted web search."
                    )
                config = (
                    await server.request(
                        "config/read", {"cwd": provider._cwd, "includeLayers": False}
                    )
                ).get("config")
                if not isinstance(config, dict) or config.get("model_catalog_json") is not None:
                    raise UserError(
                        "CodexProvider cannot verify hosted web search with a custom model catalog."
                    )
                requested_mode = "cached" if web_tools[0].external_web_access is False else "live"
                requirements = (await server.request("configRequirements/read", {})).get(
                    "requirements"
                ) or {}
                modes = requirements.get("allowedWebSearchModes")
                # Live access is permission to fetch. A managed host may narrow it to
                # cached search. A cache-only request must never permit a live fetch.
                if (
                    modes is not None
                    and requested_mode not in modes
                    and not (requested_mode == "live" and "cached" in modes)
                ):
                    raise UserError(
                        "Codex host policy does not allow the requested web search mode."
                    )
                if (
                    config.get("web_search") != requested_mode
                    or config.get("features", {}).get("standalone_web_search") is not False
                ):
                    raise UserError(
                        "Codex did not apply the requested hosted web search configuration."
                    )
                effective_web = (config.get("tools") or {}).get("web_search") or {}
                if effective_web.get("context_size") != web_tools[0].search_context_size:
                    raise UserError("Codex did not apply the requested web search context size.")
                if (
                    web_tools[0].filters is not None
                    and web_tools[0].filters.allowed_domains is not None
                    and effective_web.get("allowed_domains") != web_tools[0].filters.allowed_domains
                ):
                    raise UserError("Codex did not apply the requested web search domain filter.")
                if web_tools[0].user_location is not None and any(
                    (effective_web.get("location") or {}).get(key) != value
                    for key, value in web_tools[0].user_location.items()
                    if key != "type" and value is not None
                ):
                    raise UserError("Codex did not apply the requested web search location.")
            thread_params: dict[str, Any] = {
                "cwd": provider._cwd,
                "ephemeral": True,
                "experimentalRawEvents": True,
            }
            if self._name is not None:
                thread_params["model"] = self._name
            if system_instructions is not None:
                thread_params["developerInstructions"] = system_instructions
            if dynamic_tools:
                thread_params["dynamicTools"] = dynamic_tools
            if model_settings.verbosity is not None:
                thread_config["model_verbosity"] = model_settings.verbosity
            if thread_config:
                thread_params["config"] = thread_config
            if sdk_sandbox:
                thread_params["environments"] = []
            thread_result = await server.request("thread/start", thread_params)
            thread_id = _id(thread_result, "thread")
            if web_tools and (
                thread_result.get("model")
                or thread_result.get("thread", {}).get("model")
                or self._name
            ) not in {"gpt-5.5", "gpt-5.4", "gpt-5.4-mini"}:
                raise UserError(
                    "CodexProvider hosted web search requires a verified gpt-5.5, gpt-5.4, "
                    "or gpt-5.4-mini model; other stock models may omit the hosted tool."
                )
            if web_tools and web_tools[0].search_content_types is not None:
                effective_model = (
                    thread_result.get("model")
                    or thread_result.get("thread", {}).get("model")
                    or self._name
                )
                expected_types = {"text", "image"} if effective_model == "gpt-5.5" else {"text"}
                if set(web_tools[0].search_content_types) != expected_types:
                    raise UserError(
                        "CodexProvider supports only the selected stock model's default "
                        "web search content types."
                    )
            if (
                web_tools
                and web_tools[0].external_web_access is False
                and thread_result.get("sandbox", {}).get("type") == "dangerFullAccess"
            ):
                raise UserError(
                    "Codex upgrades cached web search in danger-full-access; use a restricted "
                    "sandbox for external_web_access=False."
                )
            has_audio = any(part.get("type") == "audio" for part in turn_input) or any(
                part.get("type") == "input_audio"
                for item in history
                for part in item.get("content", [])
                if isinstance(part, dict)
            )
            if has_audio:
                effective_model = (
                    thread_result.get("model")
                    or thread_result.get("thread", {}).get("model")
                    or self._name
                )
                cursor = None
                found = False
                while True:
                    page = await server.request(
                        "model/list", {"cursor": cursor, "includeHidden": True}
                    )
                    for candidate in page.get("data", []):
                        if effective_model in (candidate.get("id"), candidate.get("model")):
                            if "audio" not in candidate.get("inputModalities", []):
                                raise UserError(
                                    "The selected Codex model does not support audio input."
                                )
                            found = True
                            break
                    if found or not page.get("nextCursor"):
                        break
                    cursor = page["nextCursor"]
                if not found:
                    raise UserError(
                        "CodexProvider could not verify audio support for the selected model."
                    )
            if history:
                await server.request(
                    "thread/inject_items", {"threadId": thread_id, "items": history}
                )
            turn_params: dict[str, Any] = {"threadId": thread_id, "input": turn_input}
            if effort is not None:
                turn_params["effort"] = effort
            if summary is not None:
                turn_params["summary"] = summary
            if (
                output_schema is not None
                and not output_schema.is_plain_text()
                and output_schema.is_strict_json_schema()
            ):
                turn_params["outputSchema"] = output_schema.json_schema()
            if sdk_sandbox:
                turn_params["environments"] = []
            operation_mode = observations.request_mode()
            if operation_mode is not None:
                turn_params["operationTracing"] = operation_mode
            turn_result = await server.request("turn/start", turn_params)
            turn_id = _id(turn_result, "turn")
            observations.bind(thread_id, turn_id, turn_result.get("operationTracing"))
            response = Response(
                id=turn_id,
                created_at=time.time(),
                model=self._name or "codex",
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
            async for event in server.events():
                params = event.get("params") or {}
                if event.get("method") in (
                    "turn/operation/started",
                    "turn/operation/completed",
                ):
                    observations.observe(event["method"], params)
                    continue
                if "id" in event:
                    if (
                        event["method"] != "item/tool/call"
                        or params.get("threadId") != thread_id
                        or params.get("turnId") != turn_id
                    ):
                        raise RuntimeError(
                            "Codex requested an unsupported approval or client action."
                        )
                    call = sdk_tool_call(params, identities, tools)
                    if sdk_call is None:
                        sdk_call = call.model_copy(update={"id": params["callId"]})
                        call_id = params["callId"]
                        indices.setdefault(call_id, len(indices))
                        if call_id not in started:
                            started.add(call_id)
                            changes = {"status": "in_progress"}
                            if sdk_call.type == "function_call":
                                changes["arguments"] = ""
                            yield ResponseOutputItemAddedEvent(
                                item=sdk_call.model_copy(update=changes),
                                output_index=indices[call_id],
                                sequence_number=sequence,
                                type="response.output_item.added",
                            )
                            sequence += 1
                        if sdk_call.type == "function_call":
                            # Stock publishes one complete argument snapshot, not token deltas.
                            yield ResponseFunctionCallArgumentsDeltaEvent(
                                delta=sdk_call.arguments,
                                item_id=call_id,
                                output_index=indices[call_id],
                                sequence_number=sequence,
                                type="response.function_call_arguments.delta",
                            )
                            sequence += 1
                            yield ResponseFunctionCallArgumentsDoneEvent(
                                arguments=sdk_call.arguments,
                                item_id=call_id,
                                output_index=indices[call_id],
                                sequence_number=sequence,
                                type="response.function_call_arguments.done",
                            )
                            sequence += 1
                        await server.request(
                            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                        )
                    continue
                if params.get("threadId") != thread_id:
                    continue
                method = event["method"]
                if method == "turn/completed":
                    turn = params.get("turn") or {}
                    if turn.get("id") != turn_id:
                        continue
                    if turn.get("status") != ("interrupted" if sdk_call else "completed"):
                        raise RuntimeError("Codex did not finish the turn successfully.")
                    completed = True
                    break
                if method == "thread/tokenUsage/updated":
                    token_usage = params.get("tokenUsage") or {}
                    total = token_usage.get("total") or {}
                    usage = ResponseUsage(
                        input_tokens=total.get("inputTokens", 0),
                        output_tokens=total.get("outputTokens", 0),
                        total_tokens=total.get(
                            "totalTokens",
                            total.get("inputTokens", 0) + total.get("outputTokens", 0),
                        ),
                        input_tokens_details=_make_input_tokens_details(
                            cached_tokens=total.get("cachedInputTokens", 0),
                            cache_write_tokens=total.get("cacheWriteInputTokens"),
                        ),
                        output_tokens_details=OutputTokensDetails(
                            reasoning_tokens=total.get("reasoningOutputTokens", 0)
                        ),
                    )
                    if model_settings.preserve_raw_usage:
                        raw_usage = token_usage
                    continue
                if params.get("turnId") != turn_id:
                    continue
                if summary is not None and method in (
                    "item/reasoning/summaryPartAdded",
                    "item/reasoning/summaryTextDelta",
                ):
                    item_id = params["itemId"]
                    indices.setdefault(item_id, len(indices))
                    if method == "item/reasoning/summaryPartAdded":
                        yield ResponseReasoningSummaryPartAddedEvent(
                            item_id=item_id,
                            output_index=indices[item_id],
                            part=SummaryPart(type="summary_text", text=""),
                            summary_index=params["summaryIndex"],
                            sequence_number=sequence,
                            type="response.reasoning_summary_part.added",
                        )
                    else:
                        yield ResponseReasoningSummaryTextDeltaEvent(
                            item_id=item_id,
                            output_index=indices[item_id],
                            delta=params["delta"],
                            summary_index=params["summaryIndex"],
                            sequence_number=sequence,
                            type="response.reasoning_summary_text.delta",
                        )
                    sequence += 1
                    continue
                if method == "item/started":
                    item = params.get("item") or {}
                    item_id = item.get("id")
                    if not isinstance(item_id, str) or item_id in started:
                        continue
                    if item.get("type") == "agentMessage":
                        beginning: Any = ResponseOutputMessage(
                            id=item_id,
                            type="message",
                            role="assistant",
                            status="in_progress",
                            content=[],
                        )
                    else:
                        continue
                    indices.setdefault(item_id, len(indices))
                    started.add(item_id)
                    yield ResponseOutputItemAddedEvent(
                        item=beginning,
                        output_index=indices[item_id],
                        sequence_number=sequence,
                        type="response.output_item.added",
                    )
                    sequence += 1
                    continue
                if method == "rawResponseItem/completed":
                    raw = params.get("item") or {}
                    if raw.get("type") == "reasoning":
                        output[raw["id"]] = ResponseReasoningItem.model_validate(raw)
                    elif raw.get("type") == "web_search_call":
                        output[raw["id"]] = ResponseFunctionWebSearch.model_validate(raw)
                    elif raw.get("type") == "tool_search_call":
                        output[raw["id"]] = ResponseToolSearchCall.model_validate(
                            {"status": "completed", **raw}
                        )
                    elif raw.get("type") == "tool_search_output":
                        output[raw["id"]] = ResponseToolSearchOutputItem.model_validate(raw)
                    if raw.get("id") in output:
                        indices.setdefault(raw["id"], len(indices))
                    continue
                delta = None
                done_message = None
                item_id = params.get("itemId")
                if (
                    method == "item/agentMessage/delta"
                    and isinstance(item_id, str)
                    and isinstance(params.get("delta"), str)
                ):
                    indices.setdefault(item_id, len(indices))
                    if item_id not in started:
                        started.add(item_id)
                        beginning = ResponseOutputMessage(
                            id=item_id,
                            type="message",
                            role="assistant",
                            status="in_progress",
                            content=[],
                        )
                        yield ResponseOutputItemAddedEvent(
                            item=beginning,
                            output_index=indices[item_id],
                            sequence_number=sequence,
                            type="response.output_item.added",
                        )
                        sequence += 1
                    streamed.add(item_id)
                    delta = params["delta"]
                elif method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "reasoning" and summary is not None:
                        reasoning_summaries.extend(
                            part for part in item.get("summary", []) if isinstance(part, str)
                        )
                    if (
                        item.get("type") != "agentMessage"
                        or not isinstance(item.get("id"), str)
                        or not isinstance(item.get("text"), str)
                    ):
                        continue
                    item_id = item["id"]
                    if item_id in finished:
                        continue
                    indices.setdefault(item_id, len(indices))
                    if item_id not in streamed and item_id not in output:
                        delta = item["text"]
                    message = ResponseOutputMessage(
                        id=item_id,
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text", text=item["text"], annotations=[]
                            )
                        ],
                    )
                    if item.get("phase") in ("commentary", "final_answer"):
                        message.phase = item["phase"]
                    if item_id not in started:
                        started.add(item_id)
                        yield ResponseOutputItemAddedEvent(
                            item=message.model_copy(
                                update={"status": "in_progress", "content": []}
                            ),
                            output_index=indices[item_id],
                            sequence_number=sequence,
                            type="response.output_item.added",
                        )
                        sequence += 1
                    output[item_id] = message
                    done_message = message
                if delta is not None:
                    assert isinstance(item_id, str)
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
                    finished.add(done_message.id)
                    yield ResponseOutputItemDoneEvent(
                        item=done_message,
                        output_index=indices[done_message.id],
                        sequence_number=sequence,
                        type="response.output_item.done",
                    )
                    sequence += 1
            if not completed:
                raise RuntimeError("Codex finished without a completed response.")
            if require_sdk_call and sdk_call is None:
                raise UserError(
                    "CodexProvider tool_choice='required' did not return "
                    "an SDK tool or handoff call."
                )
            if sdk_call is None and not output:
                raise RuntimeError("Codex finished without a completed response.")
            items = list(output.values()) + ([sdk_call] if sdk_call is not None else [])
            if reasoning_summaries:
                items[-1].codex_reasoning_summary = reasoning_summaries
            if sdk_call is not None:
                assert isinstance(sdk_call.id, str)
                sdk_done_event = ResponseOutputItemDoneEvent(
                    item=sdk_call,
                    output_index=indices[sdk_call.id],
                    sequence_number=sequence,
                    type="response.output_item.done",
                )
                sequence += 1
            items.sort(key=lambda item: indices[item.id])
            response = response.model_copy(
                update={"output": items, "status": "completed", "usage": usage}
            )
            if raw_usage is not None:
                response.__dict__["codex_raw_usage"] = raw_usage
            observations.finish_response(response)
        finally:
            observations.close(interrupted=True)
            await server.close()
        if sdk_done_event is not None:
            yield sdk_done_event
        yield ResponseCompletedEvent(
            response=response, sequence_number=sequence, type="response.completed"
        )
