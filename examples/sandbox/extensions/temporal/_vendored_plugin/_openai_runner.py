# vendored pre-release code; type errors are misreported due to patching
# mypy: ignore-errors
import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any, Unpack

from temporalio import workflow
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._temporal_model_stub import _TemporalModelStub
from temporalio.contrib.openai_agents.sandbox._temporal_sandbox_client import (
    TemporalSandboxClient,
)
from temporalio.contrib.openai_agents.workflow import AgentsWorkflowError

from agents import (
    Agent,
    AgentsException,
    Handoff,
    RunConfig,
    RunContextWrapper,
    RunResult,
    RunResultStreaming,
    RunState,
    SQLiteSession,
    TContext,
    TResponseInputItem,
)
from agents.run import DEFAULT_AGENT_RUNNER, DEFAULT_MAX_TURNS, AgentRunner, RunOptions
from agents.sandbox import SandboxAgent


# Recursively replace models in all agents
def _convert_agent(
    model_params: ModelActivityParameters,
    agent: Agent[Any],
    seen: dict[int, Agent] | None,
) -> Agent[Any]:
    if seen is None:
        seen = {}

    # Short circuit if this model was already seen to prevent looping from circular handoffs
    if id(agent) in seen:
        return seen[id(agent)]

    # This agent has already been processed in some other run
    if isinstance(agent.model, _TemporalModelStub):
        return agent

    # Save the new version of the agent so that we can replace loops
    new_agent = dataclasses.replace(agent)
    seen[id(agent)] = new_agent

    name = _model_name(agent)

    new_handoffs: list[Agent | Handoff] = []
    for handoff in agent.handoffs:
        if isinstance(handoff, Agent):
            new_handoffs.append(_convert_agent(model_params, handoff, seen))
        elif isinstance(handoff, Handoff):
            original_invoke = handoff.on_invoke_handoff

            # Use default parameter to capture original_invoke by value, not reference
            async def on_invoke(
                context: RunContextWrapper[Any],
                args: str,
                invoke_func: Callable[
                    [RunContextWrapper[Any], str], Awaitable[Any]
                ] = original_invoke,
            ) -> Agent:
                handoff_agent = await invoke_func(context, args)
                return _convert_agent(model_params, handoff_agent, seen)

            new_handoffs.append(dataclasses.replace(handoff, on_invoke_handoff=on_invoke))
        else:
            raise TypeError(f"Unknown handoff type: {type(handoff)}")

    new_agent.model = _TemporalModelStub(
        model_name=name,
        model_params=model_params,
        agent=agent,
    )
    new_agent.handoffs = new_handoffs
    return new_agent


def _has_sandbox_agent(agent: Agent[Any], seen: set[int] | None = None) -> bool:
    """Check if any agent in the graph (following direct Agent handoffs) is a SandboxAgent."""
    if seen is None:
        seen = set()
    if id(agent) in seen:
        return False
    seen.add(id(agent))
    if isinstance(agent, SandboxAgent):
        return True
    for handoff in agent.handoffs:
        if isinstance(handoff, Agent) and _has_sandbox_agent(handoff, seen):
            return True
    return False


class TemporalOpenAIRunner(AgentRunner):
    """Temporal Runner for OpenAI agents.

    Forwards model calls to a Temporal activity.

    """

    def __init__(
        self,
        model_params: ModelActivityParameters,
    ) -> None:
        """Initialize the Temporal OpenAI Runner."""
        self._runner = DEFAULT_AGENT_RUNNER or AgentRunner()
        self.model_params = model_params

    async def run(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResult:
        """Run the agent in a Temporal workflow."""
        if not workflow.in_workflow():
            return await self._runner.run(
                starting_agent,
                input,
                **kwargs,
            )

        for t in starting_agent.tools:
            if callable(t):
                raise ValueError(
                    "Provided tool is not a tool type. If using an activity, make sure to wrap it with openai_agents.workflow.activity_as_tool."
                )

        if starting_agent.mcp_servers:
            from temporalio.contrib.openai_agents._mcp import (
                _StatefulMCPServerReference,
                _StatelessMCPServerReference,
            )

            for s in starting_agent.mcp_servers:
                if not isinstance(
                    s,
                    _StatelessMCPServerReference | _StatefulMCPServerReference,
                ):
                    raise ValueError(f"Unknown mcp_server type {type(s)} may not work durably.")

        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = kwargs.get("hooks")
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")
        session = kwargs.get("session")

        if isinstance(session, SQLiteSession):
            raise ValueError("Temporal workflows don't support SQLite sessions.")

        if run_config is None:
            run_config = RunConfig()

        if run_config.model and not isinstance(run_config.model, _TemporalModelStub):
            if not isinstance(run_config.model, str):
                raise ValueError(
                    "Temporal workflows require a model name to be a string in the run config."
                )
            run_config = dataclasses.replace(
                run_config,
                model=_TemporalModelStub(
                    run_config.model, model_params=self.model_params, agent=None
                ),
            )
        # run_config.sandbox is global for the entire run — configure it if any agent needs it.
        if _has_sandbox_agent(starting_agent) or run_config.sandbox:
            if run_config.sandbox is None:
                raise ValueError(
                    "A SandboxAgent was provided but run_config.sandbox is not configured. "
                    "You must set run_config.sandbox to a SandboxRunConfig. "
                    "For example:\n"
                    "  from temporalio.contrib.openai_agents.workflow import temporal_sandbox_client\n"
                    "  run_config = RunConfig(sandbox=SandboxRunConfig(client=temporal_sandbox_client('my-backend')))"
                )
            elif run_config.sandbox.client is None:
                raise ValueError(
                    "run_config.sandbox.client must be set to a temporal sandbox client. "
                    "Use temporalio.contrib.openai_agents.workflow.temporal_sandbox_client(name) "
                    "to create one, where name matches a SandboxClientProvider registered on the plugin."
                )
            elif not isinstance(run_config.sandbox.client, TemporalSandboxClient):
                raise ValueError(
                    "run_config.sandbox.client must be created via "
                    "temporalio.contrib.openai_agents.workflow.temporal_sandbox_client(name). "
                    "Do not pass a raw sandbox client directly."
                )

        try:
            return await self._runner.run(
                starting_agent=_convert_agent(self.model_params, starting_agent, None),
                input=input,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
                previous_response_id=previous_response_id,
                session=session,
            )
        except AgentsException as e:
            # In order for workflow failures to properly fail the workflow, we need to rewrap them in
            # a Temporal error
            if e.__cause__ and workflow.is_failure_exception(e.__cause__):
                reraise = AgentsWorkflowError(
                    f"Workflow failure exception in Agents Framework: {e}"
                )
                reraise.__traceback__ = e.__traceback__
                raise reraise from e.__cause__
            else:
                raise e

    def run_sync(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Any,
    ) -> RunResult:
        """Run the agent synchronously (not supported in Temporal workflows)."""
        if not workflow.in_workflow():
            return self._runner.run_sync(
                starting_agent,
                input,
                **kwargs,
            )
        raise RuntimeError("Temporal workflows do not support synchronous model calls.")

    def run_streamed(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Any,
    ) -> RunResultStreaming:
        """Run the agent with streaming responses (not supported in Temporal workflows)."""
        if not workflow.in_workflow():
            return self._runner.run_streamed(
                starting_agent,
                input,
                **kwargs,
            )
        raise RuntimeError("Temporal workflows do not support streaming.")


def _model_name(agent: Agent[Any]) -> str | None:
    name = agent.model
    if name is not None and not isinstance(name, str):
        raise ValueError("Temporal workflows require a model name to be a string in the agent.")
    return name
