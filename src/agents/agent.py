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
from .items import ItemHelpers
from .logger import logger
from .mcp import MCPUtil
from .model_settings import ModelSettings
from .models.interface import Model
from .prompts import DynamicPromptFunction, Prompt, PromptUtil
from .run_context import RunContextWrapper, TContext
from .tool import FunctionTool, FunctionToolResult, Tool, function_tool
from .util import _transforms
from .util._types import MaybeAwaitable

if TYPE_CHECKING:
    from .lifecycle import AgentHooks
    from .mcp import MCPServer
    from .result import RunResult


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
class Agent(Generic[TContext]):
    """An agent is an AI model configured with instructions, tools, guardrails, handoffs and more.

    We strongly recommend passing `instructions`, which is the "system prompt" for the agent. In
    addition, you can pass `handoff_description`, which is a human-readable description of the
    agent, used when the agent is used inside tools/handoffs.

    Agents are generic on the context type. The context is a (mutable) object you create. It is
    passed to tool functions, handoffs, guardrails, etc.
    """

    name: str
    """The name of the agent."""

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

    handoff_description: str | None = None
    """A description of the agent. This is used when the agent is used as a handoff, so that an
    LLM knows what it does and when to invoke it.
    """

    handoffs: list[Agent[Any] | Handoff[TContext]] = field(default_factory=list)
    """Handoffs are sub-agents that the agent can delegate to. You can provide a list of handoffs,
    and the agent can choose to delegate to them if relevant. Allows for separation of concerns and
    modularity.
    """

    model: str | Model | None = None
    """The model implementation to use when invoking the LLM.

    By default, if not set, the agent will use the default model configured in
    `openai_provider.DEFAULT_MODEL` (currently "gpt-4o").
    """

    model_settings: ModelSettings = field(default_factory=ModelSettings)
    """Configures model-specific tuning parameters (e.g. temperature, top_p).
    """

    tools: list[Tool] = field(default_factory=list)
    """A list of tools that the agent can use."""

    mcp_servers: list[MCPServer] = field(default_factory=list)
    """A list of [Model Context Protocol](https://modelcontextprotocol.io/) servers that
    the agent can use. Every time the agent runs, it will include tools from these servers in the
    list of available tools.

    NOTE: You are expected to manage the lifecycle of these servers. Specifically, you must call
    `server.connect()` before passing it to the agent, and `server.cleanup()` when the server is no
    longer needed.
    """

    mcp_config: MCPConfig = field(default_factory=lambda: MCPConfig())
    """Configuration for MCP servers."""

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
    """This lets you configure how tool use is handled.
    - "run_llm_again": The default behavior. Tools are run, and then the LLM receives the results
        and gets to respond.
    - "stop_on_first_tool": The output of the first tool call is used as the final output. This
        means that the LLM does not process the result of the tool call.
    - A list of tool names: The agent will stop running if any of the tools in the list are called.
        The final output will be the output of the first matching tool call. The LLM does not
        process the result of the tool call.
    - A function: If you pass a function, it will be called with the run context and the list of
      tool results. It must return a `ToolToFinalOutputResult`, which determines whether the tool
      calls result in a final output.

      NOTE: This configuration is specific to FunctionTools. Hosted tools, such as file search,
      web search, etc are always processed by the LLM.
    """

    reset_tool_choice: bool = True
    """Whether to reset the tool choice to the default value after a tool has been called. Defaults
    to True. This ensures that the agent doesn't enter an infinite loop of tool usage."""

    def clone(self, **kwargs: Any) -> Agent[TContext]:
        """Make a copy of the agent, with the given arguments changed. For example, you could do:
        ```
        new_agent = agent.clone(instructions="New instructions")
        ```
        """
        return dataclasses.replace(self, **kwargs)

    def as_tool(
        self,
        tool_name: str | None,
        tool_description: str | None,
        custom_output_extractor: Callable[[RunResult], Awaitable[str]] | None = None,
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
        """

        @function_tool(
            name_override=tool_name or _transforms.transform_string_function_style(self.name),
            description_override=tool_description or "",
        )
        async def run_agent(context: RunContextWrapper, input: str) -> str:
            from .run import Runner

            output = await Runner.run(
                starting_agent=self,
                input=input,
                context=context.context,
            )
            if custom_output_extractor:
                return await custom_output_extractor(output)

            return ItemHelpers.text_message_outputs(output.new_items)

        return run_agent

    async def get_system_prompt(self, run_context: RunContextWrapper[TContext]) -> str | None:
        """Get the system prompt for the agent."""
        if isinstance(self.instructions, str):
            return self.instructions
        elif callable(self.instructions):
            if inspect.iscoroutinefunction(self.instructions):
                return await cast(Awaitable[str], self.instructions(run_context, self))
            else:
                return cast(str, self.instructions(run_context, self))
        elif self.instructions is not None:
            logger.error(f"Instructions must be a string or a function, got {self.instructions}")

        return None

    async def get_prompt(
        self, run_context: RunContextWrapper[TContext]
    ) -> ResponsePromptParam | None:
        """Get the prompt for the agent."""
        return await PromptUtil.to_model_input(self.prompt, run_context, self)

    async def get_mcp_tools(self) -> list[Tool]:
        """Fetches the available tools from the MCP servers."""
        convert_schemas_to_strict = self.mcp_config.get("convert_schemas_to_strict", False)
        return await MCPUtil.get_all_function_tools(self.mcp_servers, convert_schemas_to_strict)

    async def get_all_tools(self, run_context: RunContextWrapper[Any]) -> list[Tool]:
        """All agent tools, including MCP tools and function tools."""
        mcp_tools = await self.get_mcp_tools()

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
