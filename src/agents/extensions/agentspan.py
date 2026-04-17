"""Agentspan execution backend for the OpenAI Agents SDK.

`Agentspan <https://agentspan.ai>`_ is a durable agent execution platform that
adds **persistence, observability, human-in-the-loop (HITL), and horizontal
scaling** to any agent built with the OpenAI Agents SDK — without changing how
you define agents or write tools.

Migration
---------
Change one import line::

    # Before — runs directly against OpenAI
    from agents import Runner

    # After — runs on Agentspan (durable, observable, scalable)
    from agents.extensions.agentspan import AgentspanRunner as Runner

Everything else — ``Agent``, ``@function_tool``, ``result.final_output`` —
stays identical.

Quick start
-----------
::

    from agents import Agent, function_tool
    from agents.extensions.agentspan import AgentspanRunner

    @function_tool
    def get_weather(city: str) -> str:
        \"\"\"Return current weather for a city.\"\"\"
        return f"72°F and sunny in {city}"

    agent = Agent(
        name="weather_assistant",
        model="gpt-4o",
        instructions="You are a helpful assistant.",
        tools=[get_weather],
    )

    result = AgentspanRunner.run_sync(agent, "What's the weather in NYC?")
    print(result.final_output)

Requirements
------------
- Agentspan server running (default: ``http://localhost:6767/api``)
- ``AGENTSPAN_SERVER_URL`` env var (optional, defaults to localhost)
- ``AGENTSPAN_LLM_MODEL`` env var (optional, model override)
- Install: ``pip install openai-agents[agentspan]``
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

try:
    import agentspan.agents as _agentspan
    from agentspan.agents.agent import Agent as _AgentspanAgent
    from agentspan.agents.tool import ToolDef
except ImportError as e:
    raise ImportError(
        "AgentspanRunner requires the 'agentspan' package. "
        "Install it with: pip install openai-agents[agentspan]"
    ) from e

logger = logging.getLogger("agents.extensions.agentspan")

__all__ = ["AgentspanRunner", "AgentspanRunResult"]


# ── AgentspanRunResult ────────────────────────────────────────────────────


class AgentspanRunResult:
    """Return value of :meth:`AgentspanRunner.run_sync` and :meth:`AgentspanRunner.run`.

    Exposes the same ``final_output`` attribute as the built-in ``RunResult``
    so existing code that reads ``result.final_output`` works without change.

    Attributes:
        final_output: The agent's final text output.
        execution_id: The Agentspan execution ID (useful for debugging in
            the Agentspan UI).
    """

    def __init__(self, agent_result: Any) -> None:
        self._agent_result = agent_result

    @property
    def final_output(self) -> Any:
        """The agent's final output — same attribute as ``RunResult.final_output``."""
        output = self._agent_result.output
        if isinstance(output, dict):
            return output.get("result", output)
        return output

    @property
    def execution_id(self) -> str:
        """Agentspan execution ID for tracing in the Agentspan UI."""
        return self._agent_result.execution_id

    def __repr__(self) -> str:
        return f"AgentspanRunResult(final_output={self.final_output!r})"


# ── Internal helpers ──────────────────────────────────────────────────────


def _model_to_agentspan(model: str) -> str:
    """Add a provider prefix when the model string lacks one.

    ``"gpt-4o"``          → ``"openai/gpt-4o"``
    ``"claude-opus-4-6"`` → ``"anthropic/claude-opus-4-6"``
    ``"openai/gpt-4o"``   → ``"openai/gpt-4o"``  (already qualified)
    """
    if "/" in model:
        return model
    if model.startswith(("gpt", "o1", "o3", "o4")):
        return f"openai/{model}"
    if model.startswith("claude"):
        return f"anthropic/{model}"
    if model.startswith("gemini"):
        return f"google/{model}"
    return f"openai/{model}"


def _run_async_safely(coro: Any) -> Any:
    """Run a coroutine synchronously regardless of the current event loop state."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return asyncio.run(coro)

    if loop.is_running():
        # We're inside a running loop (e.g. a Jupyter cell or async test).
        # Escape to a fresh thread so we can call asyncio.run() safely.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    return loop.run_until_complete(coro)


def _convert_function_tool(ft: Any) -> ToolDef:
    """Convert an openai-agents ``FunctionTool`` to an Agentspan ``ToolDef``.

    Args:
        ft: A ``FunctionTool`` with ``.name``, ``.description``,
            ``.params_json_schema``, and ``.on_invoke_tool(ctx, json_str)``
            attributes.
    """
    tool_name: str = ft.name
    tool_desc: str = getattr(ft, "description", "") or ""
    schema: dict = getattr(ft, "params_json_schema", {})
    on_invoke = ft.on_invoke_tool

    def _sync_wrapper(**kwargs: Any) -> Any:
        result = on_invoke(None, json.dumps(kwargs))
        if asyncio.iscoroutine(result):
            return _run_async_safely(result)
        return result

    _sync_wrapper.__name__ = tool_name
    _sync_wrapper.__doc__ = tool_desc

    return ToolDef(
        name=tool_name,
        description=tool_desc,
        input_schema=schema,
        func=_sync_wrapper,
        tool_type="worker",
    )


def _to_agentspan_agent(agent: Any) -> _AgentspanAgent:
    """Convert an openai-agents ``Agent`` to an Agentspan ``Agent``.

    If *agent* is already an Agentspan ``Agent`` it is returned unchanged.
    Accepts any duck-typed object with ``name``, ``instructions``, ``model``,
    and ``tools`` attributes.
    """
    if isinstance(agent, _AgentspanAgent):
        return agent

    name: str = getattr(agent, "name", "openai_agent")

    instructions: Any = getattr(agent, "instructions", "")
    if callable(instructions):
        try:
            result = instructions()
            instructions = asyncio.run(result) if asyncio.iscoroutine(result) else result
        except Exception:
            instructions = ""
    instructions = str(instructions) if instructions else ""

    model: str = _model_to_agentspan(getattr(agent, "model", "openai/gpt-4o"))

    agentspan_tools = []
    for t in (getattr(agent, "tools", []) or []):
        if hasattr(t, "on_invoke_tool"):
            agentspan_tools.append(_convert_function_tool(t))
        elif hasattr(t, "_tool_def"):
            agentspan_tools.append(t)
        else:
            logger.warning(
                "Skipping tool '%s' — type '%s' is not recognised. "
                "Wrap it with Agentspan's @tool decorator to include it.",
                getattr(t, "name", "?"),
                type(t).__name__,
            )

    return _AgentspanAgent(
        name=name,
        instructions=instructions,
        model=model,
        tools=agentspan_tools,
    )


# ── AgentspanRunner ───────────────────────────────────────────────────────


class AgentspanRunner:
    """Agentspan execution backend — drop-in replacement for ``Runner``.

    Identical call signatures to the built-in :class:`agents.Runner` so the
    only required change is the import::

        # Before
        from agents import Runner

        # After
        from agents.extensions.agentspan import AgentspanRunner as Runner

    Agentspan executes each agent run as a **durable workflow** backed by
    Conductor, giving you:

    - **Persistence** — runs survive process restarts and server reboots.
    - **Observability** — every tool call, LLM response, and handoff is
      recorded and visible in the Agentspan UI.
    - **Human-in-the-loop** — pause any run waiting for human input, then
      resume it from any process.
    - **Horizontal scaling** — distribute tool workers across any number of
      machines.

    Configuration
    -------------
    Set env vars before running::

        AGENTSPAN_SERVER_URL=http://localhost:6767/api  # default
        AGENTSPAN_LLM_MODEL=openai/gpt-4o              # optional override
    """

    @classmethod
    def run_sync(
        cls,
        starting_agent: Any,
        input: str,
        *,
        context: Optional[Any] = None,
        max_turns: int = 10,
        **kwargs: Any,
    ) -> AgentspanRunResult:
        """Run an agent synchronously on Agentspan.

        Drop-in for ``Runner.run_sync(agent, input)``.

        Args:
            starting_agent: An openai-agents ``Agent`` or Agentspan ``Agent``.
            input: The user's input message.
            context: Ignored — present for ``Runner`` API compatibility.
            max_turns: Maximum agent loop iterations (default 10).
            **kwargs: Extra keyword arguments (ignored for forward compatibility).

        Returns:
            An :class:`AgentspanRunResult` with a ``final_output`` attribute.
        """
        agent = _to_agentspan_agent(starting_agent)
        if max_turns != 10:
            agent.max_turns = max_turns

        result = _agentspan.run(agent, input)
        return AgentspanRunResult(result)

    @classmethod
    async def run(
        cls,
        starting_agent: Any,
        input: str,
        *,
        context: Optional[Any] = None,
        max_turns: int = 10,
        **kwargs: Any,
    ) -> AgentspanRunResult:
        """Run an agent asynchronously on Agentspan.

        Drop-in for ``await Runner.run(agent, input)``.

        Args:
            starting_agent: An openai-agents ``Agent`` or Agentspan ``Agent``.
            input: The user's input message.
            context: Ignored — present for ``Runner`` API compatibility.
            max_turns: Maximum agent loop iterations (default 10).
            **kwargs: Extra keyword arguments (ignored for forward compatibility).

        Returns:
            An :class:`AgentspanRunResult` with a ``final_output`` attribute.
        """
        agent = _to_agentspan_agent(starting_agent)
        if max_turns != 10:
            agent.max_turns = max_turns

        result = await _agentspan.run_async(agent, input)
        return AgentspanRunResult(result)

    @classmethod
    async def run_streamed(
        cls,
        starting_agent: Any,
        input: str,
        *,
        context: Optional[Any] = None,
        max_turns: int = 10,
        **kwargs: Any,
    ) -> Any:
        """Run an agent with live event streaming on Agentspan.

        Drop-in for ``Runner.run_streamed(agent, input)``.

        Returns an Agentspan :class:`~agentspan.agents.result.AsyncAgentStream`
        which supports ``async for event in stream`` iteration and
        ``await stream.get_result()`` to obtain the final result.

        Args:
            starting_agent: An openai-agents ``Agent`` or Agentspan ``Agent``.
            input: The user's input message.
            context: Ignored — present for ``Runner`` API compatibility.
            max_turns: Maximum agent loop iterations (default 10).
            **kwargs: Extra keyword arguments (ignored for forward compatibility).

        Returns:
            An Agentspan ``AsyncAgentStream``.
        """
        agent = _to_agentspan_agent(starting_agent)
        if max_turns != 10:
            agent.max_turns = max_turns

        return await _agentspan.stream_async(agent, input)
