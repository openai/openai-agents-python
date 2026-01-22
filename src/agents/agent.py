from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Generic, Literal, cast

from openai.types.responses.response_prompt_param import ResponsePromptParam
from typing_extensions import NotRequired, TypeAlias, TypedDict

from .agent_output import AgentOutputSchemaBase
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
from .tool import (
    FunctionTool,
    FunctionToolResult,
    Tool,
    ToolErrorFunction,
    default_tool_error_function,
    function_tool,
)
from .tool_context import ToolContext
from .util import _transforms
from .util._types import MaybeAwaitable

if TYPE_CHECKING:
    from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

    from .lifecycle import AgentHooks, RunHooks
    from .mcp import MCPServer
    from .memory.session import Session
    from .result import RunResult, RunResultStreaming
    from .run import RunConfig
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
        return await MCPUtil.get_all_function_tools(
            self.mcp_servers, convert_schemas_to_strict, run_context, self
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
        return [*mcp_tools, *enabled]


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
        """

        @function_tool(
            name_override=tool_name or _transforms.transform_string_function_style(self.name),
            description_override=tool_description or "",
            is_enabled=is_enabled,
            failure_error_function=failure_error_function,
        )
        async def run_agent(context: ToolContext, input: str) -> Any:
            from .run import DEFAULT_MAX_TURNS, Runner

            resolved_max_turns = max_turns if max_turns is not None else DEFAULT_MAX_TURNS
            run_result: RunResult | RunResultStreaming

            if on_stream is not None:
                run_result = Runner.run_streamed(
                    starting_agent=self,
                    input=input,
                    context=context.context,
                    run_config=run_config,
                    max_turns=resolved_max_turns,
                    hooks=hooks,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
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

                    current_agent = run_result.current_agent
                    async for event in run_result.stream_events():
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
            else:
                run_result = await Runner.run(
                    starting_agent=self,
                    input=input,
                    context=context.context,
                    run_config=run_config,
                    max_turns=resolved_max_turns,
                    hooks=hooks,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
                    session=session,
                )
            if custom_output_extractor:
                return await custom_output_extractor(run_result)

            return run_result.final_output

        return run_agent

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
