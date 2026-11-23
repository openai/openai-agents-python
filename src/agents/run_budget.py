"""Limits on model admissions and observed response usage in a Runner run."""

from __future__ import annotations

import copy
import inspect
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Literal

from ._redaction import current_redactor, protected_error
from .exceptions import (
    AgentsException,
    UserError,
    _mark_error_to_drain_stream_events,
    _raise_data_redacted_error,
)
from .usage import Usage

if TYPE_CHECKING:
    from openai.types.responses.response_prompt_param import ResponsePromptParam

    from .agent import Agent
    from .memory import Session
    from .model_settings import ModelSettings
    from .models.interface import Model
    from .result import RunResultStreaming
    from .run_config import RunConfig
    from .run_state import RunState
    from .tool import Tool


def _valid_limit(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**53 - 1


def _usd(value: float) -> Fraction:
    # Sum decimal amounts exactly, without rounding each response to a currency unit.
    if type(value) is int and value >= 0:
        return Fraction(value)
    if type(value) is float and math.isfinite(value) and value >= 0:
        return Fraction(str(value))
    raise ValueError("RunBudget.max_estimated_cost_usd must be a finite nonnegative int or float.")


@dataclass(frozen=True, kw_only=True)
class RunBudget:
    """Limits for one Python Runner root, including handoffs and approval resumes.

    Set at least one limit. Request and token limits accept integers from zero
    through 2**53 - 1. Zero blocks the first
    admission. A response may finish above a limit. The next admission then fails.

    Each new regular model invocation counts once, including follow-ups and corrections.
    Runner and transport retries, compaction, approval replies and replay add no admissions.
    Agent tools and other nested runs have independent budgets. Native sampling through
    CodexProvider and hosted multi-agent orchestration are unsupported. Independent
    Runner calls, guardrails, sandbox memory, and tool-owned clients are outside the
    root. These limits cannot bound physical sends or billing.

    Tokens and estimated cost use completed root responses and reported session compactions.
    Failed attempts, child responses and cumulative snapshots are excluded. Only usage
    visible to the SDK counts. Adapters that replace missing totals with explicit zeros
    make those totals indistinguishable from measured zero. Before the next
    admission, the synchronous estimator receives the model name and Usage
    for each completion. The name is the resolved adapter's string ``model``,
    the selected model string if unavailable, or None. Compactions use their
    own request model. OpenAI request body overrides take precedence. When
    a stored prompt selects the model, use the reported model if present. These identifiers
    describe Runner selections, with no visibility into provider routing.

    A positive cost limit requires an estimator. An estimator requires a cost
    limit. Both amounts use finite, nonnegative int or float values in USD.
    The Runner sums their decimal representations without rounding to cents.
    Missing or negative totals and unknown estimates (None) block the next
    admission by default. Set missing_usage="continue" to use only known values.

    For a $0.01 limit, with a flat rate per million tokens for each known model::

        prices_per_million = {"example": 2.0}

        def estimate(model, usage):
            rate = prices_per_million.get(model)
            return None if rate is None else usage.total_tokens * rate / 1_000_000

        budget = RunBudget(max_estimated_cost_usd=0.01, estimate_cost_usd=estimate)

    Pass the budget to RunConfig(budget=...) on each call, including resume. RunBudgetError
    identifies a blocked admission. Estimated-cost budgets support in-memory
    continuation only. ResumableSession and redact_text require portable checkpoints
    and reject cost limits anywhere in the agent graph. Protected runs can replace
    budget errors with generic exceptions.
    A paused child budget requires a unique, enabled binding in its parent's tools.
    Keep the tool graph and policies stable during each invocation.
    Child budget errors follow the agent tool's error policy. An unhandled tool failure
    leaves a durable session active for reconciliation. A new run starts with fresh counters.
    """

    max_requests: int | None = None
    max_tokens: int | None = None
    max_estimated_cost_usd: float | None = None
    estimate_cost_usd: Callable[[str | None, Usage], float | None] | None = None
    missing_usage: Literal["stop", "continue"] = "stop"

    def __post_init__(self) -> None:
        limits = (self.max_requests, self.max_tokens)
        if self.max_estimated_cost_usd is not None:
            _usd(self.max_estimated_cost_usd)
        elif all(limit is None for limit in limits):
            raise ValueError("RunBudget requires at least one limit.")
        if any(limit is not None and not _valid_limit(limit) for limit in limits):
            raise ValueError(
                "RunBudget request and token limits must be integers from zero through 2**53 - 1."
            )
        if self.missing_usage not in ("stop", "continue"):
            raise ValueError("RunBudget.missing_usage must be 'stop' or 'continue'.")
        if self.max_estimated_cost_usd is None and self.estimate_cost_usd is not None:
            raise ValueError("RunBudget.estimate_cost_usd requires max_estimated_cost_usd.")
        if (
            self.max_estimated_cost_usd is not None
            and self.max_estimated_cost_usd > 0
            and self.estimate_cost_usd is None
        ):
            raise ValueError("A positive max_estimated_cost_usd requires estimate_cost_usd.")
        if self.estimate_cost_usd is not None and (
            not callable(self.estimate_cost_usd)
            or inspect.iscoroutinefunction(self.estimate_cost_usd)
        ):
            raise TypeError("RunBudget.estimate_cost_usd must be a synchronous callable.")


class _BudgetCheckpointError(UserError):
    """A budget failure that must also abort an enclosing agent-tool checkpoint."""


class RunBudgetError(AgentsException):
    """A blocked admission. The code identifies the reason.

    For run_budget_exceeded, limit identifies the first reached ceiling.
    The Runner checks "requests", then "tokens", then "estimated_cost".
    For other codes, limit is None.
    """

    def __init__(
        self,
        code: Literal[
            "run_budget_exceeded", "run_budget_usage_unavailable", "run_budget_invalid_estimate"
        ],
        *,
        limit: Literal["requests", "tokens", "estimated_cost"] | None = None,
    ):
        self.limit = limit
        self.code = code
        super().__init__(code)


def _pricing_model(
    model: Model,
    agent: Agent[Any],
    config: RunConfig,
    settings: ModelSettings,
    prompt: ResponsePromptParam | None,
) -> str | None:
    if config.budget is None or config.budget.estimate_cost_usd is None:
        return None
    from .models.openai_chatcompletions import OpenAIChatCompletionsModel
    from .models.openai_responses import OpenAIResponsesModel

    if isinstance(model, (OpenAIResponsesModel, OpenAIChatCompletionsModel)):
        body = settings.extra_body
        if isinstance(body, Mapping) and "model" in body:
            name = body["model"]
            return name if isinstance(name, str) else None

    if isinstance(model, OpenAIResponsesModel) and model._prompt_owns_model(prompt):
        # A stored prompt can replace the adapter's implicit default.
        override = (settings.extra_args or {}).get("model")
        return override if isinstance(override, str) else None
    name = getattr(model, "model", None)
    if isinstance(name, str):
        return name
    selected = config.model if config.model is not None else agent.model
    return selected if isinstance(selected, str) else None


def _estimate(
    estimator: Callable[[str | None, Usage], float | None], model: str | None, usage: Usage
) -> Fraction | None:
    try:
        value = estimator(model, usage)
        if inspect.iscoroutine(value):
            value.close()
        return None if value is None else _usd(value)
    except Exception:
        pass
    raise RunBudgetError("run_budget_invalid_estimate")


@dataclass
class _BudgetState:
    limits: RunBudget
    requests: int = 0
    tokens: int = 0
    unavailable: bool = False
    cost: Fraction = Fraction(0)
    pending: list[tuple[str | None, Usage]] = field(default_factory=list)

    def observe(self, model: str | None, usage: Usage | None) -> None:
        limits = self.limits
        if limits.max_tokens is None and limits.max_estimated_cost_usd is None:
            return
        if (
            usage is None
            or (usage._total_missing and usage.total_tokens == 0)
            or usage.total_tokens < 0
        ):
            self.unavailable = True
            return
        self.tokens += usage.total_tokens
        if limits.estimate_cost_usd is not None:
            if model is None and isinstance(usage._reported_model, str):
                model = usage._reported_model
            self.pending.append((model, copy.deepcopy(usage)))

    def check(self) -> None:
        limits = self.limits
        if limits.max_requests is not None and self.requests >= limits.max_requests:
            raise RunBudgetError("run_budget_exceeded", limit="requests")
        if limits.max_tokens is not None and self.tokens >= limits.max_tokens:
            raise RunBudgetError("run_budget_exceeded", limit="tokens")
        ceiling = (
            _usd(limits.max_estimated_cost_usd)
            if limits.max_estimated_cost_usd is not None
            else None
        )
        if ceiling is not None and self.cost >= ceiling:
            raise RunBudgetError("run_budget_exceeded", limit="estimated_cost")
        if self.unavailable and limits.missing_usage == "stop":
            raise RunBudgetError("run_budget_usage_unavailable")
        while self.pending:
            assert limits.estimate_cost_usd is not None
            assert ceiling is not None
            estimate = _estimate(limits.estimate_cost_usd, *self.pending[0])
            self.pending.pop(0)
            if estimate is None:
                self.unavailable = True
                if limits.missing_usage == "stop":
                    raise RunBudgetError("run_budget_usage_unavailable")
            else:
                self.cost += estimate
                if self.cost >= ceiling:
                    raise RunBudgetError("run_budget_exceeded", limit="estimated_cost")

    def checkpoint(self) -> dict[str, Any]:
        if self.limits.max_estimated_cost_usd is not None:
            raise _BudgetCheckpointError("Cannot serialize an estimated-cost budget.")
        return {
            "max_requests": self.limits.max_requests,
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
                max_requests=data["max_requests"],
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


def _tool_budget(tool: Tool) -> RunBudget | None:
    invoker = getattr(tool, "on_invoke_tool", None)
    impl = getattr(invoker, "_invoke_tool_impl", None)
    config = getattr(impl, "__agents_run_config__", None)
    return config.budget if config is not None else None


def _check_portable(limits: RunBudget | None) -> None:
    if limits is not None and limits.max_estimated_cost_usd is not None:
        raise UserError(
            "ResumableSession and redact_text cannot checkpoint an estimated-cost budget."
        )


def _check_tools(tools: Iterable[Tool]) -> None:
    for tool in tools:
        _check_portable(_tool_budget(tool))


async def _admit(
    budget: _BudgetState | None,
    model: Model,
    session: Session | None,
    tools: Iterable[Tool],
    *,
    streamed_result: RunResultStreaming | None = None,
) -> None:
    from .run_internal.resumable_session import ResumableRun, checkpoint_streamed_result

    try:
        if isinstance(session, ResumableRun) or current_redactor() is not None:
            # Include tools resolved dynamically after the initial graph check.
            _check_tools(tools)
        if budget is not None:
            _check_model(model)
            budget.check()
            budget.requests += 1
    except (RunBudgetError, UserError) as error:
        if isinstance(session, ResumableRun):
            if streamed_result is None:
                await session.finish_budget_stop()
            else:
                await checkpoint_streamed_result(streamed_result, interruptions=[])
                if current_redactor() is not None:
                    safe = protected_error(error)
                    _mark_error_to_drain_stream_events(safe)
                    _raise_data_redacted_error(safe)
                _mark_error_to_drain_stream_events(error)
        raise


def _check_resume(state: RunState[Any], limits: RunBudget | None) -> bool:
    """Validate pending policies and report whether this subtree owns a budget."""
    from ._tool_identity import get_function_tool_lookup_key_for_call, get_function_tool_lookup_keys
    from .agent_tool_state import get_agent_tool_resume_state, peek_agent_tool_run_result
    from .tool import FunctionTool

    saved = state._context._budget if state._context is not None else None
    if (saved.limits if saved is not None else None) != limits:
        raise UserError("Resume requires the same RunBudget limits and missing_usage policy.")
    budgeted = saved is not None
    for action in getattr(state._last_processed_response, "functions", ()):
        if not action.function_tool._is_agent_tool:
            continue
        result = peek_agent_tool_run_result(
            action.tool_call, scope_id=state._agent_tool_state_scope_id
        )
        child = get_agent_tool_resume_state(result)
        if child is None and result is not None and result.interruptions:
            child = result.to_state()
        if child is None:
            continue
        # Turn resolution can rebind a queued call after load_state(). Check
        # the current qualified binding without running enablement callbacks.
        key = get_function_tool_lookup_key_for_call(action.tool_call)
        tools = state._current_agent.tools if state._current_agent is not None else []
        bindings = [
            tool
            for tool in tools
            if isinstance(tool, FunctionTool) and key in get_function_tool_lookup_keys(tool)
        ]
        policies = (_tool_budget(tool) for tool in bindings)
        policy = next((value for value in policies if value is not None), None)
        if _check_resume(child, policy):
            if (
                len(bindings) != 1
                or bindings[0].is_enabled is False
                or bindings[0]._agent_instance is not action.function_tool._agent_instance
            ):
                raise UserError("A pending RunBudget requires the same unique agent tool.")
            budgeted = True
    return budgeted


def _prepare_budget(
    config: RunConfig, agent: Agent[Any], state: RunState[Any] | None, session: Session | None
) -> _BudgetState | None:
    from .models.interface import Model
    from .run_internal.resumable_session import ResumableRun

    limits = config.budget
    if state is not None:
        _check_resume(state, limits)
    current = (
        state._current_agent if state is not None and state._current_agent is not None else agent
    )
    if isinstance(session, ResumableRun) or current_redactor() is not None:
        from ._run_state_agent_identity import _iter_agent_graph

        _check_portable(limits)
        for reachable in _iter_agent_graph(current):
            _check_tools(reachable.tools)
    if limits is None:
        return None
    model = config.model if config.model is not None else current.model
    _check_model(model if isinstance(model, Model) else config.model_provider)
    if state is not None:
        assert state._context is not None
        return state._context._budget
    budget = _BudgetState(limits)
    budget.check()
    return budget
