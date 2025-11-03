from __future__ import annotations

from dataclasses import dataclass, replace
from textwrap import dedent
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .agent import Agent
    from .guardrail import InputGuardrailResult, OutputGuardrailResult
    from .items import ModelResponse, RunItem, TResponseInputItem
    from .run_context import RunContextWrapper
    from .run import RunConfig
    from .result import RunResult
    from .tool_guardrails import (
        ToolGuardrailFunctionOutput,
        ToolInputGuardrail,
        ToolOutputGuardrail,
    )

from .util._pretty_print import pretty_print_run_error_details


@dataclass
class RunErrorDetails:
    """Data collected from an agent run when an exception occurs."""

    input: str | list[TResponseInputItem]
    new_items: list[RunItem]
    raw_responses: list[ModelResponse]
    last_agent: Agent[Any]
    context_wrapper: RunContextWrapper[Any]
    input_guardrail_results: list[InputGuardrailResult]
    output_guardrail_results: list[OutputGuardrailResult]
    run_config: RunConfig

    def __str__(self) -> str:
        return pretty_print_run_error_details(self)


class AgentsException(Exception):
    """Base class for all exceptions in the Agents SDK."""

    run_data: RunErrorDetails | None

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.run_data = None


class MaxTurnsExceeded(AgentsException):
    """Exception raised when the maximum number of turns is exceeded."""

    message: str

    _DEFAULT_RESUME_PROMPT = """
    You reached the maximum number of turns.
    Return a final answer to the query using ONLY the information already gathered in the conversation so far.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def resume(self, prompt: Optional[str] = _DEFAULT_RESUME_PROMPT) -> RunResult:
        """Resume the failed run synchronously with a final, tool-free turn.

        Args:
            prompt: Optional user instruction to append before rerunning the final turn.
                Pass ``None`` to skip injecting an extra message; defaults to a reminder
                to produce a final answer from existing context.
        """
        run_data = self._require_run_data()
        inputs, run_config = self._prepare_resume_arguments(run_data, prompt)

        from .run import Runner

        return Runner.run_sync(
            starting_agent=run_data.last_agent,
            input=inputs,
            context=run_data.context_wrapper.context,
            max_turns=1,
            run_config=run_config,
        )

    async def resume_async(self, prompt: Optional[str] = _DEFAULT_RESUME_PROMPT) -> RunResult:
        """Resume the failed run asynchronously with a final, tool-free turn.

        Args:
            prompt: Optional user instruction to append before rerunning the final turn.
                Pass ``None`` to skip injecting an extra message; defaults to a reminder
                to produce a final answer from existing context.
        """
        run_data = self._require_run_data()
        inputs, run_config = self._prepare_resume_arguments(run_data, prompt)

        from .run import Runner

        return await Runner.run(
            starting_agent=run_data.last_agent,
            input=inputs,
            context=run_data.context_wrapper.context,
            max_turns=1,
            run_config=run_config,
        )

    def _prepare_resume_arguments(
        self,
        run_data: RunErrorDetails,
        prompt: Optional[str] = None,
    ) -> tuple[list[TResponseInputItem], RunConfig]:
        from .items import ItemHelpers
        from .model_settings import ModelSettings

        history: list[TResponseInputItem] = ItemHelpers.input_to_new_input_list(run_data.input)
        for item in run_data.new_items:
            history.append(item.to_input_item())

        normalized_prompt = self._normalize_resume_prompt(prompt)
        if normalized_prompt is not None:
            history.append({"content": normalized_prompt, "role": "user"})

        run_config = replace(run_data.run_config)
        if run_config.model_settings is None:
            run_config.model_settings = ModelSettings(tool_choice="none")
        else:
            run_config.model_settings = run_config.model_settings.resolve(
                ModelSettings(tool_choice="none")
            )

        return (
            history,
            run_config,
        )

    def _normalize_resume_prompt(self, prompt: Optional[str]) -> Optional[str]:
        if prompt is None:
            return None
        normalized = dedent(prompt).strip()
        return normalized or None

    def _require_run_data(self) -> RunErrorDetails:
        if self.run_data is None:
            raise RuntimeError(
                "Run data is not available; resume() can only be called on exceptions raised by Runner."
            )
        return self.run_data


class ModelBehaviorError(AgentsException):
    """Exception raised when the model does something unexpected, e.g. calling a tool that doesn't
    exist, or providing malformed JSON.
    """

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class UserError(AgentsException):
    """Exception raised when the user makes an error using the SDK."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class InputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a guardrail tripwire is triggered."""

    guardrail_result: InputGuardrailResult
    """The result data of the guardrail that was triggered."""

    def __init__(self, guardrail_result: InputGuardrailResult):
        self.guardrail_result = guardrail_result
        super().__init__(
            f"Guardrail {guardrail_result.guardrail.__class__.__name__} triggered tripwire"
        )


class OutputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a guardrail tripwire is triggered."""

    guardrail_result: OutputGuardrailResult
    """The result data of the guardrail that was triggered."""

    def __init__(self, guardrail_result: OutputGuardrailResult):
        self.guardrail_result = guardrail_result
        super().__init__(
            f"Guardrail {guardrail_result.guardrail.__class__.__name__} triggered tripwire"
        )


class ToolInputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a tool input guardrail tripwire is triggered."""

    guardrail: ToolInputGuardrail[Any]
    """The guardrail that was triggered."""

    output: ToolGuardrailFunctionOutput
    """The output from the guardrail function."""

    def __init__(self, guardrail: ToolInputGuardrail[Any], output: ToolGuardrailFunctionOutput):
        self.guardrail = guardrail
        self.output = output
        super().__init__(f"Tool input guardrail {guardrail.__class__.__name__} triggered tripwire")


class ToolOutputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a tool output guardrail tripwire is triggered."""

    guardrail: ToolOutputGuardrail[Any]
    """The guardrail that was triggered."""

    output: ToolGuardrailFunctionOutput
    """The output from the guardrail function."""

    def __init__(self, guardrail: ToolOutputGuardrail[Any], output: ToolGuardrailFunctionOutput):
        self.guardrail = guardrail
        self.output = output
        super().__init__(f"Tool output guardrail {guardrail.__class__.__name__} triggered tripwire")
