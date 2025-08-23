"""Workflow orchestration engine for multi-agent systems."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, cast

from ..agent import Agent
from ..exceptions import UserError
from ..result import RunResult
from ..run_context import RunContextWrapper
from ..tracing import trace
from .connections import Connection

TContext = TypeVar("TContext", bound=Any)


@dataclass
class WorkflowResult(Generic[TContext]):
    """Result of a workflow execution."""

    final_result: RunResult
    """The final result from the last step in the workflow."""

    step_results: list[RunResult]
    """Results from each step in the workflow execution."""

    context: TContext
    """The final context state after workflow execution."""


@dataclass
class Workflow(Generic[TContext]):
    """A declarative workflow that orchestrates multiple agents through connections.

    !!! warning "Beta Feature"
        The Workflow system is currently in beta. The API may change in future releases.

    Workflows define a sequence of agent connections that are executed in order.
    Each connection specifies how agents interact (handoff, tool call, sequential, etc.).

    Example:
        ```python
        workflow = Workflow([
            HandoffConnection(triage_agent, billing_agent),
            ToolConnection(billing_agent, analysis_agent),
            SequentialConnection(analysis_agent, report_agent)
        ])

        result = await workflow.run("Customer billing inquiry")
        ```
    """

    connections: list[Connection[TContext]]
    """The sequence of connections that define the workflow."""

    name: str | None = None
    """Optional name for the workflow, used in tracing."""

    context: TContext | None = None
    """Optional shared context for all agents in the workflow."""

    max_steps: int = 100
    """Maximum number of steps to execute before stopping."""

    trace_workflow: bool = True
    """Whether to wrap the entire workflow execution in a trace."""

    step_results: list[RunResult] = field(default_factory=list, init=False)
    """Internal storage for step results during execution."""

    def __post_init__(self) -> None:
        """Validate workflow configuration."""
        if not self.connections:
            raise UserError("Workflow must have at least one connection")

        # For now, skip strict chain validation as it doesn't work well with
        # parallel connections and conditional routing
        # TODO: Implement more sophisticated validation that can handle
        # complex workflow patterns

    async def run(
        self,
        input_data: Any,
        context: TContext | None = None,
    ) -> WorkflowResult[TContext]:
        """Execute the workflow asynchronously.

        Args:
            input_data: Initial input data for the workflow
            context: Optional context override for this execution

        Returns:
            WorkflowResult containing the final result and execution details
        """
        execution_context = context or self.context
        # Create context wrapper - handle type issues with Any cast if needed
        context_wrapper = RunContextWrapper(execution_context)

        async def _execute_workflow() -> WorkflowResult[TContext]:
            self.step_results = []
            current_result: RunResult | None = None

            for i, connection in enumerate(self.connections):
                if i >= self.max_steps:
                    raise UserError(f"Workflow exceeded maximum steps ({self.max_steps})")

                try:
                    current_result = await connection.execute(
                        cast(Any, context_wrapper),
                        input_data
                        if i == 0
                        else (current_result.final_output if current_result else input_data),
                        current_result,
                    )
                    self.step_results.append(current_result)

                except Exception as e:
                    raise UserError(
                        f"Workflow failed at step {i} ({connection.__class__.__name__}): {e}"
                    ) from e

            if current_result is None:
                raise UserError("Workflow completed without producing any results")

            return WorkflowResult(
                final_result=current_result,
                step_results=self.step_results.copy(),
                context=cast(
                    TContext,
                    execution_context if execution_context is not None else context_wrapper.context,
                ),
            )

        if self.trace_workflow:
            workflow_name = self.name or "Workflow execution"
            with trace(workflow_name):
                return await _execute_workflow()
        else:
            return await _execute_workflow()

    def run_sync(
        self,
        input_data: Any,
        context: TContext | None = None,
    ) -> WorkflowResult[TContext]:
        """Execute the workflow synchronously.

        Args:
            input_data: Initial input data for the workflow
            context: Optional context override for this execution

        Returns:
            WorkflowResult containing the final result and execution details
        """

        try:
            # Try to use asyncio.run if no event loop is running
            return asyncio.run(self.run(input_data, context))
        except RuntimeError:
            # If we're already in an event loop, create a task and wait for it
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context, so we can't use run_until_complete
                # This is a bit of a hack, but it should work for testing
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(lambda: asyncio.run(self.run(input_data, context)))
                    return future.result()
            else:
                return loop.run_until_complete(self.run(input_data, context))

    def clone(self, **kwargs: Any) -> Workflow[TContext]:
        """Create a copy of the workflow with modified parameters.

        Args:
            **kwargs: Parameters to override in the cloned workflow

        Returns:
            A new Workflow instance with the specified modifications
        """
        import dataclasses

        return dataclasses.replace(self, **kwargs)

    def add_connection(self, connection: Connection[TContext]) -> Workflow[TContext]:
        """Add a connection to the workflow.

        Args:
            connection: The connection to add

        Returns:
            A new workflow with the connection added
        """
        new_connections = self.connections + [connection]
        return self.clone(connections=new_connections)

    def validate_chain(self) -> list[str]:
        """Validate the workflow connection chain.

        Returns:
            List of validation errors, empty if valid
        """
        errors = []

        if not self.connections:
            errors.append("Workflow must have at least one connection")
            return errors

        # For now, skip strict chain validation as it doesn't work well with
        # parallel connections and conditional routing
        # TODO: Implement more sophisticated validation that can handle
        # complex workflow patterns

        # Check for duplicate agent names (potential confusion)
        agent_names = set()
        for connection in self.connections:
            for agent in [connection.from_agent, connection.to_agent]:
                if agent.name in agent_names:
                    continue
                agent_names.add(agent.name)

        return errors

    @property
    def agent_count(self) -> int:
        """Get the total number of unique agents in the workflow."""
        agents = set()
        for connection in self.connections:
            agents.add(connection.from_agent.name)
            agents.add(connection.to_agent.name)
        return len(agents)

    @property
    def start_agent(self) -> Agent[TContext]:
        """Get the starting agent of the workflow."""
        if not self.connections:
            raise UserError("Cannot get start agent from empty workflow")
        return self.connections[0].from_agent

    @property
    def end_agent(self) -> Agent[TContext]:
        """Get the ending agent of the workflow."""
        if not self.connections:
            raise UserError("Cannot get end agent from empty workflow")
        return self.connections[-1].to_agent
