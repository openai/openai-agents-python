from __future__ import annotations

import asyncio
import dataclasses
import inspect
import weakref
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Generic, Literal, cast, List, Optional, Tuple, Dict

from openai.types.responses.response_prompt_param import ResponsePromptParam
from typing_extensions import NotRequired, TypeAlias, TypedDict

from agent_output import AgentOutputSchemaBase
from guardrail import InputGuardrail, OutputGuardrail
from handoffs import Handoff
from items import ItemHelpers
from logger import logger
from .mcp import MCPUtil, MCPServer
from .model_settings import ModelSettings
from .models.interface import Model
from .prompts import DynamicPromptFunction, Prompt, PromptUtil
from .run_context import RunContextWrapper, TContext
from .tool import FunctionTool, FunctionToolResult, Tool, function_tool
from .util import _transforms
from .util._types import MaybeAwaitable
from .exceptions import AgentError, TimeoutError
from .run import AgentContext

if TYPE_CHECKING:
    from .lifecycle import AgentHooks
    from .mcp import MCPServer
    from .result import RunResult
    from .registry import AgentRegistry

class AgentNotFoundError(AgentError):
    """Raised when requested agent is not in registry."""
    pass

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

    parent_agent: Agent[Any] | None = None
    """Reference to parent agent for bidirectional handoffs. This allows sub-agents to return
    control to their parent agent, enabling orchestrator-like workflows where a parent agent can
    coordinate multiple sub-agents.
    """

    return_to_parent_enabled: bool = True
    """Whether this agent can return control to its parent agent. When True, the agent will have
    access to a 'return_to_parent' tool that allows it to hand control back to its parent agent.
    This enables bidirectional handoff workflows.
    """

    model: str | Model | None = None
    """The model implementation to use when invoking the LLM.

    By default, if not set, the agent will use the default model configured in
    `openai_provider.DEFAULT_MODEL` (currently "gpt-4o").
    """

    model_settings: ModelSettings = field(default_factory=ModelSettings)
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
    - "stop_on_first_tool": The output of the first tool call is used as the final output. This
        means that the LLM does not process the result of the tool call.
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

    # New fields for bidirectional handoff
    registry: Optional['AgentRegistry'] = None
    """Registry for managing agent instances."""
    return_results: bool = True
    """Whether to return results to parent agent."""
    results: List[Any] = field(default_factory=list)
    """List of results collected during execution."""
    _parent_ref: Optional[weakref.ReferenceType['Agent']] = None
    """Weak reference to parent agent."""
    _children: weakref.WeakSet = field(default_factory=weakref.WeakSet)
    """Set of child agents."""

    @property
    def parent(self) -> Optional['Agent']:
        """Get the parent agent."""
        return self._parent_ref() if self._parent_ref else None

    @parent.setter
    def parent(self, value: Optional['Agent']):
        """Set the parent agent."""
        self._parent_ref = weakref.ref(value) if value else None

    async def execute(
        self, 
        task: str, 
        context: Optional[AgentContext] = None,
        timeout: int = 30
    ) -> Any:
        """Execute a task with timeout handling and context propagation.
        
        Args:
            task: Task description to execute.
            context: SDK AgentContext for message history.
            timeout: Maximum execution time in seconds.
        
        Returns:
            Result of task execution.
        
        Raises:
            TimeoutError: If execution exceeds timeout.
            AgentError: For general execution failures.
        """
        try:
            async with asyncio.timeout(timeout):
                if context:
                    context.add_message({"role": "system", "content": await self.get_system_prompt(RunContextWrapper(context))})
                    context.add_message({"role": "user", "content": task})
                
                # Simulate LLM call (replace with actual SDK LLM call)
                result = f"{self.name} processed: {task}"
                
                self.results.append(result)
                if self.return_results and self.parent:
                    self.parent.handle_child_result(result, source=self.name)
                
                if context:
                    context.add_message({"role": "assistant", "content": result})
                
                return result
                
        except asyncio.TimeoutError:
            error = f"{self.name} timed out on: {task}"
            self.handle_error(error)
            raise TimeoutError(error)
        except Exception as e:
            self.handle_error(str(e))
            raise AgentError(f"{self.name} failed: {str(e)}")

    async def handoff(
        self,
        sub_agent_name: str,
        task: str,
        context: Optional[AgentContext] = None,
        return_results: Optional[bool] = None,
        timeout: int = 30
    ) -> Any:
        """Delegate task to a sub-agent with context propagation.
        
        Args:
            sub_agent_name: Name of sub-agent in registry.
            task: Task description to execute.
            context: SDK AgentContext for message history.
            return_results: Override default result return behavior.
            timeout: Maximum execution time in seconds.
        
        Returns:
            Result of sub-agent's task execution.
        
        Raises:
            AgentNotFoundError: If sub-agent is not registered.
            AgentError: For general handoff failures.
        """
        try:
            if not self.registry:
                raise AgentError("AgentRegistry not provided")
            sub_agent = self.registry.get_agent(sub_agent_name, parent=self)
            self._children.add(sub_agent)
            
            if return_results is not None:
                sub_agent.return_results = return_results
                
            async with asyncio.timeout(timeout):
                return await sub_agent.execute(task, context, timeout)
                
        except AgentNotFoundError as e:
            self.handle_error(str(e))
            raise
        except Exception as e:
            self.handle_error(str(e))
            raise AgentError(f"Handoff to {sub_agent_name} failed: {str(e)}")

    async def handoff_parallel(
        self,
        agent_tasks: List[Tuple[str, str, Optional[bool]]],
        context: Optional[AgentContext] = None,
        timeout: int = 30,
        max_concurrent: int = 5
    ) -> List[Any]:
        """Execute multiple handoffs concurrently with concurrency limit.
        
        Args:
            agent_tasks: List of (agent_name, task, return_results) tuples.
            context: SDK AgentContext for message history.
            timeout: Maximum execution time per task in seconds.
            max_concurrent: Maximum concurrent tasks.
        
        Returns:
            List of results from sub-agents.
        """
        tasks = [
            self.handoff(name, task, context, return_results, timeout)
            for name, task, return_results in agent_tasks[:max_concurrent]
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def select_sub_agent(self, task: str) -> Optional[str]:
        """Dynamically select sub-agent based on task content.
        
        Args:
            task: Task description to analyze.
        
        Returns:
            Name of selected sub-agent or None.
        """
        try:
            from .models import call_llm
            prompt = f"Given task '{task}', select an agent: {list(self.registry.agent_configs.keys())}"
            response = await call_llm(prompt, model="gpt-4")
            if response in self.registry.agent_configs:
                return response
        except (ImportError, AgentError):
            # Fallback to regex-based selection
            task_lower = task.lower()
            patterns = {
                "FinancialAgent": r"financial|earnings|stock|market",
                "DocsAgent": r"save|document|report|write",
                "AnalysisAgent": r"analyze|metrics|calculate"
            }
            for agent_name, pattern in patterns.items():
                if re.search(pattern, task_lower):
                    return agent_name
            return None

    def handle_child_result(self, result: Any, source: str) -> None:
        """Receive and process results from child agents.
        
        Args:
            result: Result from sub-agent.
            source: Name of the sub-agent.
        """
        formatted_result = f"[From {source}]: {result}"
        self.results.append(formatted_result)
        if self.return_results and self.parent:
            self.parent.handle_child_result(formatted_result, source=self.name)

    def handle_error(self, error: str) -> None:
        """Centralized error handling.
        
        Args:
            error: Error message to log and propagate.
        """
        error_msg = f"[Error in {self.name}]: {error}"
        self.results.append(error_msg)
        if self.parent:
            self.parent.handle_child_result(error_msg, source=self.name)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable agent state."""
        return {
            'name': self.name,
            'instructions': self.instructions if isinstance(self.instructions, str) else None,
            'results': [str(r) for r in self.results],
            'parent': self.parent.name if self.parent else None,
            'children': [c.name for c in self._children]
        }

    def clone(self, **kwargs: Any) -> Agent[TContext]:
        """Make a copy of the agent, with the given arguments changed."""
        return dataclasses.replace(self, **kwargs)

    def set_parent(self, parent: Agent[Any]) -> None:
        """Set the parent agent for bidirectional handoffs.
        
        Args:
            parent: The parent agent that this agent can return control to.
        """
        self.parent_agent = parent

    def can_return_to_parent(self) -> bool:
        """Check if this agent can return control to its parent.
        
        Returns:
            True if the agent has a parent and return_to_parent is enabled.
        """
        return self.return_to_parent_enabled and self.parent_agent is not None

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