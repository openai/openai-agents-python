"""Limits on logical model calls and observed tokens in a Runner run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ._redaction import current_redactor, protected_error
from .exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    UserError,
    _mark_error_to_drain_stream_events,
    _raise_data_redacted_error,
)
from .usage import Usage

if TYPE_CHECKING:
    from .agent import Agent
    from .memory import Session
    from .models.interface import Model
    from .result import RunResultStreaming
    from .run_config import RunConfig
    from .run_state import RunState
    from .tool import Tool


def _valid_limit(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**53 - 1


@dataclass(frozen=True, kw_only=True)
class RunBudget:
    """Limits for one Python Runner root, including handoffs and approval resumes.

    Pass the same limits and missing-usage policy on resume::

        from agents import RunBudget, RunConfig

        config = RunConfig(budget=RunBudget(max_model_calls=3, max_tokens=4000))

    Set one or both limits to integers from zero through 2**53 - 1. Zero
    blocks the first call. A response can exceed the token limit and still
    finish the run. Limits block the next call. Requested tools may still run.
    Since max_turns (default 10) wins a tie, pass max_turns=None to Runner
    when a finite max_model_calls should determine when the run stops.

    Each admitted model call counts, including follow-ups and corrections.
    Retries, approval replies, replay and session compaction add no calls.
    Tokens count completed root responses and compactions, excluding failed attempts.
    With a token limit, missing or negative totals block the next call.
    Set missing_usage="continue" to count only known totals. A provider
    that reports zero for missing usage makes it indistinguishable from zero.

    Agent tools and independent runs need their own budgets. A paused child
    requires the same unique, enabled tool and budget on resume. Child errors
    follow the tool's error policy. Check completed work before retrying.
    Native sampling through CodexProvider and hosted multi-agent orchestration
    are unsupported. These limits cannot bound physical sends or billing.
    Text redaction can replace RunBudgetError with a generic error.
    """

    max_model_calls: int | None = None
    max_tokens: int | None = None
    missing_usage: Literal["stop", "continue"] = "stop"

    def __post_init__(self) -> None:
        limits = (self.max_model_calls, self.max_tokens)
        if all(limit is None for limit in limits):
            raise ValueError("RunBudget requires at least one limit.")
        if any(limit is not None and not _valid_limit(limit) for limit in limits):
            raise ValueError("RunBudget limits must be integers from zero through 2**53 - 1.")
        if self.missing_usage not in ("stop", "continue"):
            raise ValueError("RunBudget.missing_usage must be 'stop' or 'continue'.")


class _BudgetCheckpointError(UserError):
    """A budget failure that must also abort an enclosing agent-tool checkpoint."""


class RunBudgetError(AgentsException):
    """A blocked model call. Check code for the reason and limit for the ceiling.

    For run_budget_exceeded, model_calls takes precedence over tokens.
    For run_budget_usage_unavailable, limit is None.
    """

    def __init__(
        self,
        code: Literal["run_budget_exceeded", "run_budget_usage_unavailable"],
        *,
        limit: Literal["model_calls", "tokens"] | None = None,
    ):
        self.limit = limit
        self.code = code
        if code == "run_budget_usage_unavailable":
            message = 'Unknown usage. Use missing_usage="continue" to count only known totals.'
        else:
            ceiling = "max_model_calls" if limit == "model_calls" else "max_tokens"
            message = (
                f"RunBudget.{ceiling} reached. Check completed work before starting a new run."
            )
        super().__init__(f"{code}: {message}")


@dataclass
class _BudgetState:
    limits: RunBudget
    requests: int = 0
    tokens: int = 0
    unavailable: bool = False

    def observe(self, usage: Usage | None) -> None:
        if self.limits.max_tokens is None:
            return
        if (
            usage is None
            or (usage._total_missing and usage.total_tokens == 0)
            or usage.total_tokens < 0
        ):
            self.unavailable = True
            return
        self.tokens += usage.total_tokens

    def check(self) -> None:
        limits = self.limits
        if limits.max_model_calls is not None and self.requests >= limits.max_model_calls:
            raise RunBudgetError("run_budget_exceeded", limit="model_calls")
        if limits.max_tokens is not None and self.tokens >= limits.max_tokens:
            raise RunBudgetError("run_budget_exceeded", limit="tokens")
        if self.unavailable and limits.missing_usage == "stop":
            raise RunBudgetError("run_budget_usage_unavailable")

    def checkpoint(self) -> dict[str, Any]:
        # Schema 1.25 stores logical model calls as requests.
        return {
            "max_requests": self.limits.max_model_calls,
            "max_tokens": self.limits.max_tokens,
            "missing_usage": self.limits.missing_usage,
            "requests": self.requests,
            "tokens": self.tokens,
            "unavailable": self.unavailable,
        }

    @classmethod
    def restore(cls, data: Any) -> _BudgetState:
        if not isinstance(data, dict) or data.keys() != {
            "max_requests",
            "max_tokens",
            "missing_usage",
            "requests",
            "tokens",
            "unavailable",
        }:
            raise _BudgetCheckpointError("Invalid run budget checkpoint.")
        if (
            any(type(data[k]) is not int or data[k] < 0 for k in ("requests", "tokens"))
            or type(data["unavailable"]) is not bool
        ):
            raise _BudgetCheckpointError("Invalid run budget consumption.")
        try:
            limits = RunBudget(
                max_model_calls=data["max_requests"],
                max_tokens=data["max_tokens"],
                missing_usage=data["missing_usage"],
            )
        except ValueError:
            raise _BudgetCheckpointError("Invalid run budget limits.") from None
        return cls(limits, data["requests"], data["tokens"], data["unavailable"])


def _check_model(model: object) -> None:
    from .extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel
    from .models.codex import CodexProvider, _CodexModel

    if isinstance(model, OpenAIHostedMultiAgentModel):
        raise UserError("RunBudget cannot isolate root usage in hosted multi-agent runs.")
    if isinstance(model, (CodexProvider, _CodexModel)):
        raise UserError("RunBudget cannot enforce native sampling limits with CodexProvider.")


def _tool_config(tool: Tool) -> RunConfig | None:
    invoker = getattr(tool, "on_invoke_tool", None)
    impl = getattr(invoker, "_invoke_tool_impl", None)
    return getattr(impl, "__agents_run_config__", None)


async def _admit(
    budget: _BudgetState | None,
    model: Model,
    session: Session | None,
    *,
    streamed_result: RunResultStreaming | None = None,
) -> None:
    if budget is None:
        return
    from .run_internal.resumable_session import ResumableRun, checkpoint_streamed_result

    try:
        _check_model(model)
        budget.check()
        budget.requests += 1
    except (RunBudgetError, UserError) as error:
        from .run_inbox import stop_inbox_intake

        # No further request can accept input, even while guardrails finish
        # or the session saves the completed tool results.
        stop_inbox_intake()
        if streamed_result is not None:
            from .run_internal.guardrails import input_guardrail_tripwire_triggered_for_stream

            # A parallel check can reject the completed turn while its tools run.
            await input_guardrail_tripwire_triggered_for_stream(
                streamed_result, ignore_cancelled=True
            )
            tripwire = streamed_result._triggered_input_guardrail_result
            if tripwire is not None:
                raise InputGuardrailTripwireTriggered(tripwire) from None
            checks = streamed_result._input_guardrails_task
            if checks is not None:
                if checks.cancelled():
                    raise  # No verdict to authorize publishing the queued events.
                checks.result()
            await checkpoint_streamed_result(streamed_result, interruptions=[])
            if current_redactor() is not None:
                safe = protected_error(error)
                _mark_error_to_drain_stream_events(safe)
                _raise_data_redacted_error(safe)
            _mark_error_to_drain_stream_events(error)
        elif isinstance(session, ResumableRun):
            await session.finish_budget_stop()
        raise


def _check_resumed_budget(state: RunState[Any], limits: RunBudget | None) -> bool:
    saved = state._context._budget if state._context is not None else None
    if (saved.limits if saved is not None else None) != limits:
        raise UserError("Resume requires the same RunBudget limits and missing_usage policy.")
    return saved is not None


def _prepare_budget(
    config: RunConfig, agent: Agent[Any], state: RunState[Any] | None
) -> _BudgetState | None:
    from .models.interface import Model

    limits = config.budget
    if limits is None:
        return None
    current = (
        state._current_agent if state is not None and state._current_agent is not None else agent
    )
    model = config.model if config.model is not None else current.model
    _check_model(model if isinstance(model, Model) else config.model_provider)
    if state is not None:
        assert state._context is not None
        return state._context._budget
    budget = _BudgetState(limits)
    budget.check()
    return budget
