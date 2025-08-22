"""Connection types for workflow orchestration."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar, cast

from ..agent import Agent
from ..exceptions import UserError
from ..handoffs import HandoffInputData, handoff
from ..result import RunResult
from ..run_context import RunContextWrapper
from ..util._types import MaybeAwaitable

TContext = TypeVar("TContext", default=Any)


@dataclass
class Connection(abc.ABC, Generic[TContext]):
    """Abstract base class for agent connections in workflows."""

    from_agent: Agent[TContext]
    to_agent: Agent[TContext]

    @abc.abstractmethod
    async def execute(
        self,
        context: RunContextWrapper[TContext],
        input_data: Any,
        previous_result: RunResult | None = None,
    ) -> RunResult:
        """Execute the connection between agents.

        Args:
            context: The run context wrapper
            input_data: Input data for the connection
            previous_result: Result from the previous step in the workflow

        Returns:
            RunResult from executing the connection
        """
        pass

    @abc.abstractmethod
    def prepare_agent(self, context: RunContextWrapper[TContext]) -> Agent[TContext]:
        """Prepare the target agent for execution.

        Args:
            context: The run context wrapper

        Returns:
            The prepared agent ready for execution
        """
        pass


@dataclass
class HandoffConnection(Connection[TContext]):
    """Connection that transfers control from one agent to another via handoff.

    The target agent takes over the conversation and sees the full conversation history.
    This is useful for routing tasks to specialized agents.
    """

    tool_name_override: str | None = None
    tool_description_override: str | None = None
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True

    def prepare_agent(self, context: RunContextWrapper[TContext]) -> Agent[TContext]:
        """Prepare the source agent with handoff to the target agent."""
        handoff_config = handoff(
            agent=self.to_agent,
            tool_name_override=self.tool_name_override,
            tool_description_override=self.tool_description_override,
            input_filter=self.input_filter,
            is_enabled=self.is_enabled,
        )

        # Clone the from_agent and add the handoff
        return self.from_agent.clone(handoffs=[*self.from_agent.handoffs, handoff_config])

    async def execute(
        self,
        context: RunContextWrapper[TContext],
        input_data: Any,
        previous_result: RunResult | None = None,
    ) -> RunResult:
        """Execute the handoff connection."""
        from ..run import Runner

        prepared_agent = self.prepare_agent(context)

        if previous_result is not None:
            # Use the conversation history from the previous result
            return await Runner.run(
                starting_agent=prepared_agent,
                input=previous_result.to_input_list(),
                context=context.context,
            )
        else:
            # First step in workflow
            return await Runner.run(
                starting_agent=prepared_agent,
                input=input_data,
                context=context.context,
            )


@dataclass
class ToolConnection(Connection[TContext]):
    """Connection that uses the target agent as a tool for the source agent.

    The source agent calls the target agent as a tool and continues with the result.
    This is useful for modular functionality and parallel processing.
    """

    tool_name: str | None = None
    tool_description: str | None = None
    custom_output_extractor: Callable[[RunResult], Any] | None = None
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True

    def prepare_agent(self, context: RunContextWrapper[TContext]) -> Agent[TContext]:
        """Prepare the source agent with the target agent as a tool."""

        async def _async_extractor(result: RunResult) -> str:
            if self.custom_output_extractor:
                extracted = self.custom_output_extractor(result)
                if asyncio.iscoroutine(extracted):
                    result_val = await extracted
                    return str(result_val) if result_val is not None else ""
                return str(extracted) if extracted is not None else ""
            return str(result.final_output) if result.final_output is not None else ""

        tool = self.to_agent.as_tool(
            tool_name=self.tool_name,
            tool_description=self.tool_description,
            custom_output_extractor=_async_extractor if self.custom_output_extractor else None,
            is_enabled=cast(Any, self.is_enabled),
        )

        # Clone the from_agent and add the tool
        return self.from_agent.clone(tools=[*self.from_agent.tools, tool])

    async def execute(
        self,
        context: RunContextWrapper[TContext],
        input_data: Any,
        previous_result: RunResult | None = None,
    ) -> RunResult:
        """Execute the tool connection."""
        from ..run import Runner

        prepared_agent = self.prepare_agent(context)

        if previous_result is not None:
            # Continue from previous result
            return await Runner.run(
                starting_agent=prepared_agent,
                input=previous_result.to_input_list(),
                context=context.context,
            )
        else:
            # First step in workflow
            return await Runner.run(
                starting_agent=prepared_agent,
                input=input_data,
                context=context.context,
            )


@dataclass
class SequentialConnection(Connection[TContext]):
    """Connection that passes the output of one agent as input to another agent.

    This creates a pipeline where each agent processes the result of the previous one.
    Useful for multi-step transformations and processing chains.
    """

    output_transformer: Callable[[RunResult], Any] | None = None

    def prepare_agent(self, context: RunContextWrapper[TContext]) -> Agent[TContext]:
        """Return the target agent as-is for sequential execution."""
        return self.to_agent

    async def execute(
        self,
        context: RunContextWrapper[TContext],
        input_data: Any,
        previous_result: RunResult | None = None,
    ) -> RunResult:
        """Execute the sequential connection."""
        from ..run import Runner

        if previous_result is not None:
            # Transform the output if a transformer is provided
            if self.output_transformer:
                transformed_input = self.output_transformer(previous_result)
            else:
                transformed_input = previous_result.final_output
        else:
            transformed_input = input_data

        return await Runner.run(
            starting_agent=self.to_agent,
            input=transformed_input,
            context=context.context,
        )


@dataclass
class ConditionalConnection(Connection[TContext]):
    """Connection that conditionally routes to different agents based on a predicate.

    Evaluates a condition and routes to either the primary agent or an alternative agent.
    Useful for dynamic routing based on context or previous results.
    """

    condition: Callable[[RunContextWrapper[TContext], RunResult | None], MaybeAwaitable[bool]]
    alternative_agent: Agent[TContext] | None = None

    def prepare_agent(self, context: RunContextWrapper[TContext]) -> Agent[TContext]:
        """Return the from_agent as-is since routing is determined at execution time."""
        return self.from_agent

    async def execute(
        self,
        context: RunContextWrapper[TContext],
        input_data: Any,
        previous_result: RunResult | None = None,
    ) -> RunResult:
        """Execute the conditional connection."""
        from ..run import Runner

        # Evaluate condition
        condition_result = self.condition(context, previous_result)
        if asyncio.iscoroutine(condition_result):
            should_use_primary = await condition_result
        else:
            should_use_primary = condition_result

        # Choose target agent
        target_agent = self.to_agent if should_use_primary else self.alternative_agent
        if target_agent is None:
            raise UserError("Conditional connection failed: no alternative agent provided")

        # Execute with chosen agent
        if previous_result is not None:
            return await Runner.run(
                starting_agent=target_agent,
                input=previous_result.to_input_list(),
                context=context.context,
            )
        else:
            return await Runner.run(
                starting_agent=target_agent,
                input=input_data,
                context=context.context,
            )


@dataclass
class ParallelConnection(Connection[TContext]):
    """Connection that runs multiple agents in parallel and synthesizes results.

    Executes several agents concurrently on the same input and optionally
    synthesizes their outputs using a coordinator agent.
    """

    parallel_agents: list[Agent[TContext]]
    synthesizer_agent: Agent[TContext] | None = None
    synthesis_template: str = "Synthesize the following results:\n\n{results}"

    def prepare_agent(self, context: RunContextWrapper[TContext]) -> Agent[TContext]:
        """Return the from_agent as-is since parallel execution is handled specially."""
        return self.from_agent

    async def execute(
        self,
        context: RunContextWrapper[TContext],
        input_data: Any,
        previous_result: RunResult | None = None,
    ) -> RunResult:
        """Execute the parallel connection."""
        from ..run import Runner

        # Determine input for parallel agents
        if previous_result is not None:
            parallel_input = previous_result.to_input_list()
        else:
            parallel_input = input_data

        # Run all parallel agents concurrently
        parallel_tasks = [
            Runner.run(
                starting_agent=agent,
                input=parallel_input,
                context=context.context,
            )
            for agent in self.parallel_agents
        ]

        parallel_results = await asyncio.gather(*parallel_tasks)

        # If no synthesizer, return the first result
        if self.synthesizer_agent is None:
            return parallel_results[0]

        # Synthesize results
        results_text = "\n\n".join(
            [
                f"Agent '{result.last_agent.name}': {result.final_output}"
                for result in parallel_results
            ]
        )

        synthesis_input = self.synthesis_template.format(results=results_text)

        return await Runner.run(
            starting_agent=self.synthesizer_agent,
            input=synthesis_input,
            context=context.context,
        )
