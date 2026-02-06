from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Generic, Literal, cast

from openai.types.responses.response_prompt_param import ResponsePromptParam
from pydantic import BaseModel, TypeAdapter, ValidationError
from typing_extensions import NotRequired, TypeAlias, TypedDict

from . import _debug
from .agent_output import AgentOutputSchemaBase
from .agent_tool_input import (
    AgentAsToolInput,
    StructuredToolInputBuilder,
    build_structured_input_schema_info,
    resolve_agent_tool_input,
)
from .agent_tool_state import (
    consume_agent_tool_run_result,
    peek_agent_tool_run_result,
    record_agent_tool_run_result,
)
from .exceptions import ModelBehaviorError, UserError
from .guardrail import InputGuardrail, OutputGuardrail
from .handoffs import Handoff
from .logger import logger
from .mcp import MCPUtil
from .model_settings import ModelSettings
from .models.default_models import (
    get_default_model_settings,
    gpt_5_reasoning_settings_required,
    is_gpt_5_default,
)
from .models.interface import Model
from .prompts import DynamicPromptFunction, Prompt, PromptUtil
from .run_context import RunContextWrapper, TContext
from .strict_schema import ensure_strict_json_schema
from .tool import (
    FunctionTool,
    FunctionToolResult,
    Tool,
    ToolErrorFunction,
    _extract_tool_argument_json_error,
    default_tool_error_function,
)
from .tool_context import ToolContext
from .tracing import SpanError
from .util import _error_tracing, _transforms
from .util._types import MaybeAwaitable

if TYPE_CHECKING:
    from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

    from .items import ToolApprovalItem
    from .lifecycle import AgentHooks, RunHooks
    from .mcp import MCPServer
    from .memory.session import Session
    from .result import RunResult, RunResultStreaming
    from .run import RunConfig
    from .run_state import RunState
    from .stream_events import StreamEvent


@dataclass
class ToolsToFinalOutputResult:
    is_final_output: bool
    """Whether this is the final output. If False, the LLM will run again and receive the tool call
    output.
    """

    final_output: Any | None = None
    """The final output. Can be None if `is_final_output` is False, otherwise must match the
    `output_type` of the agent.
    """


ToolsToFinalOutputFunction: TypeAlias = Callable[
    [RunContextWrapper[TContext], list[FunctionToolResult]],
    MaybeAwaitable[ToolsToFinalOutputResult],
]
"""A function that takes a run context and a list of tool results, and returns a
`ToolsToFinalOutputResult`.
"""


def _validate_codex_tool_name_collisions(tools: list[Tool]) -> None:
    codex_tool_names = {
        tool.name
        for tool in tools
        if isinstance(tool, FunctionTool) and bool(getattr(tool, "_is_codex_tool", False))
    }
    if not codex_tool_names:
        return

    name_counts: dict[str, int] = {}
    for tool in tools:
        tool_name = getattr(tool, "name", None)
        if isinstance(tool_name, str) and tool_name:
            name_counts[tool_name] = name_counts.get(tool_name, 0) + 1

    duplicate_codex_names = sorted(
        name for name in codex_tool_names if name_counts.get(name, 0) > 1
    )
    if duplicate_codex_names:
        raise UserError(
            "Duplicate Codex tool names found: "
            + ", ".join(duplicate_codex_names)
            + ". Provide a unique codex_tool(name=...) per tool instance."
        )


class AgentToolStreamEvent(TypedDict):
    """Streaming event emitted when an agent is invoked as a tool."""

    event: StreamEvent
    """The streaming event from the nested agent run."""

    agent: Agent[Any]
    """The nested agent emitting the event."""

    tool_call: ResponseFunctionToolCall | None
    """The originating tool call, if available."""


class StopAtTools(TypedDict):
    stop_at_tool_names: list[str]
    """A list of tool names, any of which will stop the agent from running further."""


class MCPConfig(TypedDict):
    """Configuration for MCP servers."""

    convert_schemas_to_strict: NotRequired[bool]
    """If True, we will attempt to convert the MCP schemas to strict-mode schemas. This is a
    best-effort conversion, so some schemas may not be convertible. Defaults to False.
    """

    failure_error_function: NotRequired[ToolErrorFunction | None]
    """Optional function to convert MCP tool failures into model-visible messages. If explicitly
    set to None, tool errors will be raised instead. If unset, defaults to
    default_tool_error_function.
    """


@dataclass
class AgentBase(Generic[TContext]):
    """Base class for `Agent` and `RealtimeAgent`."""

    name: str
    """The name of the agent."""

    handoff_description: str | None = None
    """A description of the agent. This is used when the agent is used as a handoff, so that an
    LLM knows what it does and when to invoke it.
    """

    tools: list[Tool] = field(default_factory=list)
    """A list of tools that the agent can use."""

    mcp_servers: list[MCPServer] = field(default_factory=list)
    """A list of [Model Context Protocol](https://modelcontextprotocol.io/) servers that
    the agent can use. Every time the agent runs, it will include tools from these servers in the
    list of available tools.

    NOTE: You are expected to manage the lifecycle of these servers. Specifically, you must call
    `server.connect()` before passing it to the agent, and `server.cleanup()` when the server is no
    longer needed. Consider using `MCPServerManager` from `agents.mcp` to keep connect/cleanup
    in the same task.
    """

    mcp_config: MCPConfig = field(default_factory=lambda: MCPConfig())
    """Configuration for MCP servers."""

    async def get_mcp_tools(self, run_context: RunContextWrapper[TContext]) -> list[Tool]:
        """Fetches the available tools from the MCP servers."""
        convert_schemas_to_strict = self.mcp_config.get("convert_schemas_to_strict", False)
        failure_error_function = self.mcp_config.get(
            "failure_error_function", default_tool_error_function
        )
        return await MCPUtil.get_all_function_tools(
            self.mcp_servers,
            convert_schemas_to_strict,
            run_context,
            self,
            failure_error_function=failure_error_function,
        )

    async def get_all_tools(self, run_context: RunContextWrapper[TContext]) -> list[Tool]:
        """All agent tools, including MCP tools and function tools."""
        mcp_tools = await self.get_mcp_tools(run_context)

        async def _check_tool_enabled(tool: Tool) -> bool:
            if not isinstance(tool, FunctionTool):
                return True

            attr = tool.is_enabled
            if isinstance(attr, bool):
                return attr
            res = attr(run_context, self)
            if inspect.isawaitable(res):
                return bool(await res)
            return bool(res)

        results = await asyncio.gather(*(_check_tool_enabled(t) for t in self.tools))
        enabled: list[Tool] = [t for t, ok in zip(self.tools, results) if ok]
        all_tools: list[Tool] = [*mcp_tools, *enabled]
        _validate_codex_tool_name_collisions(all_tools)
        return all_tools


@dataclass
class Agent(AgentBase, Generic[TContext]):
    """An agent is an AI model configured with instructions, tools, guardrails, handoffs and more.

    We strongly recommend passing `instructions`, which is the "system prompt" for the agent. In
    addition, you can pass `handoff_description`, which is a human-readable description of the
    agent, used when the agent is used inside tools/handoffs.

    Agents are generic on the context type. The context is a (mutable) object you create. It is
    passed to tool functions, handoffs, guardrails, etc.

    See `AgentBase` for base parameters that are shared with `RealtimeAgent`s.
    """

    instructions: (
        str
        | Callable[
            [RunContextWrapper[TContext], Agent[TContext]],
            MaybeAwaitable[str],
        ]
        | None
    ) = None
    """The instructions for the agent. Will be used as the "system prompt" when this agent is
    invoked. Describes what the agent should do, and how it responds.

    Can either be a string, or a function that dynamically generates instructions for the agent. If
    you provide a function, it will be called with the context and the agent instance. It must
    return a string.
    """

    prompt: Prompt | DynamicPromptFunction | None = None
    """A prompt object (or a function that returns a Prompt). Prompts allow you to dynamically
    configure the instructions, tools and other config for an agent outside of your code. Only
    usable with OpenAI models, using the Responses API.
    """

    handoffs: list[Agent[Any] | Handoff[TContext, Any]] = field(default_factory=list)
    """Handoffs are sub-agents that the agent can delegate to. You can provide a list of handoffs,
    and the agent can choose to delegate to them if relevant. Allows for separation of concerns and
    modularity.
    """

    model: str | Model | None = None
    """The model implementation to use when invoking the LLM.

    By default, if not set, the agent will use the default model configured in
    `agents.models.get_default_model()` (currently "gpt-4.1").
    """

    model_settings: ModelSettings = field(default_factory=get_default_model_settings)
    """Configures model-specific tuning parameters (e.g. temperature, top_p).
    """

    input_guardrails: list[InputGuardrail[TContext]] = field(default_factory=list)
    """A list of checks that run in parallel to the agent's execution, before generating a
    response. Runs only if the agent is the first agent in the chain.
    """

    output_guardrails: list[OutputGuardrail[TContext]] = field(default_factory=list)
    """A list of checks that run on the final output of the agent, after generating a response.
    Runs only if the agent produces a final output.
    """

    output_type: type[Any] | AgentOutputSchemaBase | None = None
    """The type of the output object. If not provided, the output will be `str`. In most cases,
    you should pass a regular Python type (e.g. a dataclass, Pydantic model, TypedDict, etc).
    You can customize this in two ways:
    1. If you want non-strict schemas, pass `AgentOutputSchema(MyClass, strict_json_schema=False)`.
    2. If you want to use a custom JSON schema (i.e. without using the SDK's automatic schema)
       creation, subclass and pass an `AgentOutputSchemaBase` subclass.
    """

    hooks: AgentHooks[TContext] | None = None
    """A class that receives callbacks on various lifecycle events for this agent.
    """

    tool_use_behavior: (
        Literal["run_llm_again", "stop_on_first_tool"] | StopAtTools | ToolsToFinalOutputFunction
    ) = "run_llm_again"
    """
    This lets you configure how tool use is handled.
    - "run_llm_again": The default behavior. Tools are run, and then the LLM receives the results
        and gets to respond.
    - "stop_on_first_tool": The output from the first tool call is treated as the final result.
        In other words, it isn’t sent back to the LLM for further processing but is used directly
        as the final output.
    - A StopAtTools object: The agent will stop running if any of the tools listed in
        `stop_at_tool_names` is called.
        The final output will be the output of the first matching tool call.
        The LLM does not process the result of the tool call.
    - A function: If you pass a function, it will be called with the run context and the list of
      tool results. It must return a `ToolsToFinalOutputResult`, which determines whether the tool
      calls result in a final output.

      NOTE: This configuration is specific to FunctionTools. Hosted tools, such as file search,
      web search, etc. are always processed by the LLM.
    """

    reset_tool_choice: bool = True
    """Whether to reset the tool choice to the default value after a tool has been called. Defaults
    to True. This ensures that the agent doesn't enter an infinite loop of tool usage."""

    def __post_init__(self):
        from typing import get_origin

        if not isinstance(self.name, str):
            raise TypeError(f"Agent name must be a string, got {type(self.name).__name__}")

        if self.handoff_description is not None and not isinstance(self.handoff_description, str):
            raise TypeError(
                f"Agent handoff_description must be a string or None, "
                f"got {type(self.handoff_description).__name__}"
            )

        if not isinstance(self.tools, list):
            raise TypeError(f"Agent tools must be a list, got {type(self.tools).__name__}")

        if not isinstance(self.mcp_servers, list):
            raise TypeError(
                f"Agent mcp_servers must be a list, got {type(self.mcp_servers).__name__}"
            )

        if not isinstance(self.mcp_config, dict):
            raise TypeError(
                f"Agent mcp_config must be a dict, got {type(self.mcp_config).__name__}"
            )

        if (
            self.instructions is not None
            and not isinstance(self.instructions, str)
            and not callable(self.instructions)
        ):
            raise TypeError(
                f"Agent instructions must be a string, callable, or None, "
                f"got {type(self.instructions).__name__}"
            )

        if (
            self.prompt is not None
            and not callable(self.prompt)
            and not hasattr(self.prompt, "get")
        ):
            raise TypeError(
                f"Agent prompt must be a Prompt, DynamicPromptFunction, or None, "
                f"got {type(self.prompt).__name__}"
            )

        if not isinstance(self.handoffs, list):
            raise TypeError(f"Agent handoffs must be a list, got {type(self.handoffs).__name__}")

        if self.model is not None and not isinstance(self.model, str):
            from .models.interface import Model

            if not isinstance(self.model, Model):
                raise TypeError(
                    f"Agent model must be a string, Model, or None, got {type(self.model).__name__}"
                )

        if not isinstance(self.model_settings, ModelSettings):
            raise TypeError(
                f"Agent model_settings must be a ModelSettings instance, "
                f"got {type(self.model_settings).__name__}"
            )

        if (
            # The user sets a non-default model
            self.model is not None
            and (
                # The default model is gpt-5
                is_gpt_5_default() is True
                # However, the specified model is not a gpt-5 model
                and (
                    isinstance(self.model, str) is False
                    or gpt_5_reasoning_settings_required(self.model) is False  # type: ignore
                )
                # The model settings are not customized for the specified model
                and self.model_settings == get_default_model_settings()
            )
        ):
            # In this scenario, we should use a generic model settings
            # because non-gpt-5 models are not compatible with the default gpt-5 model settings.
            # This is a best-effort attempt to make the agent work with non-gpt-5 models.
            self.model_settings = ModelSettings()

        if not isinstance(self.input_guardrails, list):
            raise TypeError(
                f"Agent input_guardrails must be a list, got {type(self.input_guardrails).__name__}"
            )

        if not isinstance(self.output_guardrails, list):
            raise TypeError(
                f"Agent output_guardrails must be a list, "
                f"got {type(self.output_guardrails).__name__}"
            )

        if self.output_type is not None:
            from .agent_output import AgentOutputSchemaBase

            if not (
                isinstance(self.output_type, (type, AgentOutputSchemaBase))
                or get_origin(self.output_type) is not None
            ):
                raise TypeError(
                    f"Agent output_type must be a type, AgentOutputSchemaBase, or None, "
                    f"got {type(self.output_type).__name__}"
                )

        if self.hooks is not None:
            from .lifecycle import AgentHooksBase

            if not isinstance(self.hooks, AgentHooksBase):
                raise TypeError(
                    f"Agent hooks must be an AgentHooks instance or None, "
                    f"got {type(self.hooks).__name__}"
                )

        if (
            not (
                isinstance(self.tool_use_behavior, str)
                and self.tool_use_behavior in ["run_llm_again", "stop_on_first_tool"]
            )
            and not isinstance(self.tool_use_behavior, dict)
            and not callable(self.tool_use_behavior)
        ):
            raise TypeError(
                f"Agent tool_use_behavior must be 'run_llm_again', 'stop_on_first_tool', "
                f"StopAtTools dict, or callable, got {type(self.tool_use_behavior).__name__}"
            )

        if not isinstance(self.reset_tool_choice, bool):
            raise TypeError(
                f"Agent reset_tool_choice must be a boolean, "
                f"got {type(self.reset_tool_choice).__name__}"
            )

    def clone(self, **kwargs: Any) -> Agent[TContext]:
        """Make a copy of the agent, with the given arguments changed.
        Notes:
            - Uses `dataclasses.replace`, which performs a **shallow copy**.
            - Mutable attributes like `tools` and `handoffs` are shallow-copied:
              new list objects are created only if overridden, but their contents
              (tool functions and handoff objects) are shared with the original.
            - To modify these independently, pass new lists when calling `clone()`.
        Example:
            ```python
            new_agent = agent.clone(instructions="New instructions")
            ```
        """
        return dataclasses.replace(self, **kwargs)

    def as_tool(
        self,
        tool_name: str | None,
        tool_description: str | None,
        custom_output_extractor: (
            Callable[[RunResult | RunResultStreaming], Awaitable[str]] | None
        ) = None,
        is_enabled: bool
        | Callable[[RunContextWrapper[Any], AgentBase[Any]], MaybeAwaitable[bool]] = True,
        on_stream: Callable[[AgentToolStreamEvent], MaybeAwaitable[None]] | None = None,
        run_config: RunConfig | None = None,
        max_turns: int | None = None,
        hooks: RunHooks[TContext] | None = None,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        session: Session | None = None,
        failure_error_function: ToolErrorFunction | None = default_tool_error_function,
        needs_approval: bool
        | Callable[[RunContextWrapper[Any], dict[str, Any], str], Awaitable[bool]] = False,
        parameters: type[Any] | None = None,
        input_builder: StructuredToolInputBuilder | None = None,
        include_input_schema: bool = False,
    ) -> Tool:
        """Transform this agent into a tool, callable by other agents.

        This is different from handoffs in two ways:
        1. In handoffs, the new agent receives the conversation history. In this tool, the new agent
           receives generated input.
        2. In handoffs, the new agent takes over the conversation. In this tool, the new agent is
           called as a tool, and the conversation is continued by the original agent.

        Args:
            tool_name: The name of the tool. If not provided, the agent's name will be used.
            tool_description: The description of the tool, which should indicate what it does and
                when to use it.
            custom_output_extractor: A function that extracts the output from the agent. If not
                provided, the last message from the agent will be used.
            is_enabled: Whether the tool is enabled. Can be a bool or a callable that takes the run
                context and agent and returns whether the tool is enabled. Disabled tools are hidden
                from the LLM at runtime.
            on_stream: Optional callback (sync or async) to receive streaming events from the nested
                agent run. The callback receives an `AgentToolStreamEvent` containing the nested
                agent, the originating tool call (when available), and each stream event. When
                provided, the nested agent is executed in streaming mode.
            failure_error_function: If provided, generate an error message when the tool (agent) run
                fails. The message is sent to the LLM. If None, the exception is raised instead.
            needs_approval: Bool or callable to decide if this agent tool should pause for approval.
            parameters: Structured input type for the tool arguments (dataclass or Pydantic model).
            input_builder: Optional function to build the nested agent input from structured data.
            include_input_schema: Whether to include the full JSON schema in structured input.
        """

        def _is_supported_parameters(value: Any) -> bool:
            if not isinstance(value, type):
                return False
            if dataclasses.is_dataclass(value):
                return True
            return issubclass(value, BaseModel)

        tool_name_resolved = tool_name or _transforms.transform_string_function_style(self.name)
        tool_description_resolved = tool_description or ""
        has_custom_parameters = parameters is not None
        include_schema = bool(include_input_schema and has_custom_parameters)
        should_capture_tool_input = bool(
            has_custom_parameters or include_schema or input_builder is not None
        )

        if parameters is None:
            params_adapter = TypeAdapter(AgentAsToolInput)
            params_schema = ensure_strict_json_schema(params_adapter.json_schema())
        else:
            if not _is_supported_parameters(parameters):
                raise TypeError("Agent tool parameters must be a dataclass or Pydantic model type.")
            params_adapter = TypeAdapter(parameters)
            params_schema = ensure_strict_json_schema(params_adapter.json_schema())

        schema_info = build_structured_input_schema_info(
            params_schema,
            include_json_schema=include_schema,
        )

        def _normalize_tool_input(parsed: Any) -> Any:
            # Prefer JSON mode so structured params (datetime/UUID/Decimal, etc.) serialize cleanly.
            try:
                return params_adapter.dump_python(parsed, mode="json")
            except Exception as exc:
                raise ModelBehaviorError(
                    f"Failed to serialize structured tool input for {tool_name_resolved}: {exc}"
                ) from exc

        async def _run_agent_impl(context: ToolContext, input_json: str) -> Any:
            from .run import DEFAULT_MAX_TURNS, Runner
            from .tool_context import ToolContext

            try:
                json_data = json.loads(input_json) if input_json else {}
            except Exception as exc:
                if _debug.DONT_LOG_TOOL_DATA:
                    logger.debug(f"Invalid JSON input for tool {tool_name_resolved}")
                else:
                    logger.debug(f"Invalid JSON input for tool {tool_name_resolved}: {input_json}")
                raise ModelBehaviorError(
                    f"Invalid JSON input for tool {tool_name_resolved}: {input_json}"
                ) from exc

            if _debug.DONT_LOG_TOOL_DATA:
                logger.debug(f"Invoking tool {tool_name_resolved}")
            else:
                logger.debug(f"Invoking tool {tool_name_resolved} with input {input_json}")

            try:
                parsed_params = params_adapter.validate_python(json_data)
            except ValidationError as exc:
                raise ModelBehaviorError(
                    f"Invalid JSON input for tool {tool_name_resolved}: {exc}"
                ) from exc

            params_data = _normalize_tool_input(parsed_params)
            resolved_input = await resolve_agent_tool_input(
                params=params_data,
                schema_info=schema_info if should_capture_tool_input else None,
                input_builder=input_builder,
            )
            if not isinstance(resolved_input, str) and not isinstance(resolved_input, list):
                raise ModelBehaviorError("Agent tool called with invalid input")

            resolved_max_turns = max_turns if max_turns is not None else DEFAULT_MAX_TURNS
            if isinstance(context, ToolContext):
                # Use a fresh ToolContext to avoid sharing approval state with parent runs.
                nested_context = ToolContext(
                    context=context.context,
                    usage=context.usage,
                    tool_name=context.tool_name,
                    tool_call_id=context.tool_call_id,
                    tool_arguments=context.tool_arguments,
                    tool_call=context.tool_call,
                )
                if should_capture_tool_input:
                    nested_context.tool_input = params_data
            elif isinstance(context, RunContextWrapper):
                if should_capture_tool_input:
                    nested_context = RunContextWrapper(context=context.context)
                    nested_context.tool_input = params_data
                else:
                    nested_context = context.context
            else:
                if should_capture_tool_input:
                    nested_context = RunContextWrapper(context=context)
                    nested_context.tool_input = params_data
                else:
                    nested_context = context
            run_result: RunResult | RunResultStreaming | None = None
            resume_state: RunState | None = None
            should_record_run_result = True

            def _nested_approvals_status(
                interruptions: list[ToolApprovalItem],
            ) -> Literal["approved", "pending", "rejected"]:
                has_pending = False
                has_decision = False
                for interruption in interruptions:
                    call_id = interruption.call_id
                    if not call_id:
                        has_pending = True
                        continue
                    status = context.get_approval_status(
                        interruption.tool_name or "", call_id, existing_pending=interruption
                    )
                    if status is False:
                        return "rejected"
                    if status is True:
                        has_decision = True
                    if status is None:
                        has_pending = True
                if has_decision:
                    return "approved"
                if has_pending:
                    return "pending"
                return "approved"

            def _apply_nested_approvals(
                nested_context: RunContextWrapper[Any],
                parent_context: RunContextWrapper[Any],
                interruptions: list[ToolApprovalItem],
            ) -> None:
                for interruption in interruptions:
                    call_id = interruption.call_id
                    if not call_id:
                        continue
                    tool_name = RunContextWrapper._resolve_tool_name(interruption)
                    status = parent_context.get_approval_status(
                        tool_name, call_id, existing_pending=interruption
                    )
                    if status is None:
                        continue
                    approval_record = parent_context._approvals.get(tool_name)
                    if status is True:
                        always_approve = bool(approval_record and approval_record.approved is True)
                        nested_context.approve_tool(
                            interruption,
                            always_approve=always_approve,
                        )
                    else:
                        always_reject = bool(approval_record and approval_record.rejected is True)
                        nested_context.reject_tool(
                            interruption,
                            always_reject=always_reject,
                        )

            if isinstance(context, ToolContext) and context.tool_call is not None:
                pending_run_result = peek_agent_tool_run_result(context.tool_call)
                if pending_run_result and getattr(pending_run_result, "interruptions", None):
                    status = _nested_approvals_status(pending_run_result.interruptions)
                    if status == "pending":
                        run_result = pending_run_result
                        should_record_run_result = False
                    elif status in ("approved", "rejected"):
                        resume_state = pending_run_result.to_state()
                        if resume_state._context is not None:
                            # Apply only explicit parent approvals to the nested resumed run.
                            _apply_nested_approvals(
                                resume_state._context,
                                context,
                                pending_run_result.interruptions,
                            )
                        consume_agent_tool_run_result(context.tool_call)

            if run_result is None:
                if on_stream is not None:
                    run_result_streaming = Runner.run_streamed(
                        starting_agent=cast(Agent[Any], self),
                        input=resume_state or resolved_input,
                        context=None if resume_state is not None else cast(Any, nested_context),
                        run_config=run_config,
                        max_turns=resolved_max_turns,
                        hooks=hooks,
                        previous_response_id=None
                        if resume_state is not None
                        else previous_response_id,
                        conversation_id=None if resume_state is not None else conversation_id,
                        session=session,
                    )
                    # Dispatch callbacks in the background so slow handlers do not block
                    # event consumption.
                    event_queue: asyncio.Queue[AgentToolStreamEvent | None] = asyncio.Queue()

                    async def _run_handler(payload: AgentToolStreamEvent) -> None:
                        """Execute the user callback while capturing exceptions."""
                        try:
                            maybe_result = on_stream(payload)
                            if inspect.isawaitable(maybe_result):
                                await maybe_result
                        except Exception:
                            logger.exception(
                                "Error while handling on_stream event for agent tool %s.",
                                self.name,
                            )

                    async def dispatch_stream_events() -> None:
                        while True:
                            payload = await event_queue.get()
                            is_sentinel = payload is None  # None marks the end of the stream.
                            try:
                                if payload is not None:
                                    await _run_handler(payload)
                            finally:
                                event_queue.task_done()

                            if is_sentinel:
                                break

                    dispatch_task = asyncio.create_task(dispatch_stream_events())

                    try:
                        from .stream_events import AgentUpdatedStreamEvent

                        current_agent = run_result_streaming.current_agent
                        async for event in run_result_streaming.stream_events():
                            if isinstance(event, AgentUpdatedStreamEvent):
                                current_agent = event.new_agent

                            payload: AgentToolStreamEvent = {
                                "event": event,
                                "agent": current_agent,
                                "tool_call": context.tool_call,
                            }
                            await event_queue.put(payload)
                    finally:
                        await event_queue.put(None)
                        await event_queue.join()
                        await dispatch_task
                    run_result = run_result_streaming
                else:
                    run_result = await Runner.run(
                        starting_agent=cast(Agent[Any], self),
                        input=resume_state or resolved_input,
                        context=None if resume_state is not None else cast(Any, nested_context),
                        run_config=run_config,
                        max_turns=resolved_max_turns,
                        hooks=hooks,
                        previous_response_id=None
                        if resume_state is not None
                        else previous_response_id,
                        conversation_id=None if resume_state is not None else conversation_id,
                        session=session,
                    )
            assert run_result is not None

            # Store the run result by tool call identity so nested interruptions can be read later.
            interruptions = getattr(run_result, "interruptions", None)
            if isinstance(context, ToolContext) and context.tool_call is not None and interruptions:
                if should_record_run_result:
                    record_agent_tool_run_result(context.tool_call, run_result)

            if custom_output_extractor:
                return await custom_output_extractor(run_result)

            return run_result.final_output

        async def _run_agent_tool(context: ToolContext, input_json: str) -> Any:
            try:
                return await _run_agent_impl(context, input_json)
            except Exception as exc:
                if failure_error_function is None:
                    raise

                result = failure_error_function(context, exc)
                if inspect.isawaitable(result):
                    result = await result

                json_decode_error = _extract_tool_argument_json_error(exc)
                if json_decode_error is not None:
                    span_error_message = "Error running tool"
                    span_error_detail = str(json_decode_error)
                else:
                    span_error_message = "Error running tool (non-fatal)"
                    span_error_detail = str(exc)

                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message=span_error_message,
                        data={
                            "tool_name": tool_name_resolved,
                            "error": span_error_detail,
                        },
                    )
                )
                if _debug.DONT_LOG_TOOL_DATA:
                    logger.debug(f"Tool {tool_name_resolved} failed")
                else:
                    logger.error(
                        f"Tool {tool_name_resolved} failed: {input_json} {exc}",
                        exc_info=exc,
                    )
                return result

        run_agent_tool = FunctionTool(
            name=tool_name_resolved,
            description=tool_description_resolved,
            params_json_schema=params_schema,
            on_invoke_tool=_run_agent_tool,
            strict_json_schema=True,
            is_enabled=is_enabled,
            needs_approval=needs_approval,
        )
        run_agent_tool._is_agent_tool = True
        run_agent_tool._agent_instance = self

        return run_agent_tool

    async def get_system_prompt(self, run_context: RunContextWrapper[TContext]) -> str | None:
        if isinstance(self.instructions, str):
            return self.instructions
        elif callable(self.instructions):
            # Inspect the signature of the instructions function
            sig = inspect.signature(self.instructions)
            params = list(sig.parameters.values())

            # Enforce exactly 2 parameters
            if len(params) != 2:
                raise TypeError(
                    f"'instructions' callable must accept exactly 2 arguments (context, agent), "
                    f"but got {len(params)}: {[p.name for p in params]}"
                )

            # Call the instructions function properly
            if inspect.iscoroutinefunction(self.instructions):
                return await cast(Awaitable[str], self.instructions(run_context, self))
            else:
                return cast(str, self.instructions(run_context, self))

        elif self.instructions is not None:
            logger.error(
                f"Instructions must be a string or a callable function, "
                f"got {type(self.instructions).__name__}"
            )

        return None

    async def get_prompt(
        self, run_context: RunContextWrapper[TContext]
    ) -> ResponsePromptParam | None:
        """Get the prompt for the agent."""
        return await PromptUtil.to_model_input(self.prompt, run_context, self)
