"""Limits on model admissions and observed response usage in a Runner run."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

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
    from openai.types.responses.response_prompt_param import ResponsePromptParam

    from .agent import Agent
    from .memory import Session
    from .model_settings import ModelSettings
    from .models.interface import Model
    from .result import RunResultStreaming
    from .run_config import RunConfig
    from .run_context import RunContextWrapper
    from .run_state import RunState
    from .tool import Tool


def _valid_limit(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**53 - 1


@dataclass(frozen=True, kw_only=True)
class RunBudget:
    """Limits on logical calls, SDK outer requests, observed tokens and SDK tool starts.

    max_model_calls and max_tokens retain the root accounting of the upstream budget:
    completed root responses and compactions count, failed attempts do not. Retries,
    approval replies and replay add no model calls. max_turns wins a tie. Pass
    max_turns=None when a finite max_model_calls should determine the stopping point.
    Native sampling through CodexProvider and hosted orchestration remain unsupported.

    max_outer_requests explicitly bounds Python or Codex outer Runner requests. It
    counts the same SDK admissions as max_model_calls, never hidden native samples,
    physical sends or billing. Model-call limits take precedence over outer-request
    limits, then token limits. A response may exceed a token limit and finish the run.
    The next admission stops. Requested tools may still run.

    Set include_failed_attempt_usage=True to count known provider-reported failed
    attempts in token observations. It defaults to False. Unreported attempt usage
    stays unknown and adds no fabricated tokens. This flag adds no call admissions.
    missing_usage="stop" rejects the next admission when an observed total is missing
    or negative. Set missing_usage="continue" to count only known totals.
    Reported zero remains zero.

    Agent tools have independent budgets by default. share_with_children=True shares
    every configured dimension through SDK Agent.as_tool descendants. Explicit child
    budgets add local ceilings. Each scope's failed-attempt flag determines its own
    observations. An opted-in ancestor counts known child failures even if the child's
    local flag is False. Children without an explicit budget inherit the parent policy.
    max_child_runs, max_delegation_depth and allowed_tools also enable shared children.

    allowed_tools uses qualified SDK function names, including agent-tool wrapper
    names. Every ancestor must allow a tool. Restricted runs reject opaque prompt IDs,
    raw tool or prompt overrides, non-function and deferred/tool-search routes.
    max_tool_starts counts SDK FunctionTool starts, including Agent.as_tool wrappers.
    It rejects uncounted SDK custom, local shell, computer and apply-patch handlers.
    Provider-owned tools remain outside this counter. Local MCP tools adapted to
    FunctionTool use the normal start boundary.

    Slots reserve after approval and input guardrails, before hooks and invocation,
    without awaiting between checks and increments. Rejected approvals, blocked input
    guardrails and replay add no starts. Failures or cancellation after reservation
    do not refund slots. A paused child resumes without another wrapper reservation.
    max_child_runs counts admitted child wrappers. Depth zero forbids delegation.
    Shared native delegation guarantees remain unsupported even in SDK-only workspace
    mode, which does not prevent native child model requests.

    Configure the run and pass the same policy on approval resume::

        from agents import RunBudget, RunConfig

        config = RunConfig(budget=RunBudget(max_model_calls=3, max_tokens=4000))

    Portable checkpoints preserve consumption and shared parent binding.
    A paused child requires the same unique,
    enabled binding and policy. Child errors follow the tool's error policy. Check
    completed work before retrying. Text redaction can replace budget errors.
    """

    max_model_calls: int | None = None
    max_tokens: int | None = None
    missing_usage: Literal["stop", "continue"] = "stop"
    max_tool_starts: int | None = None
    share_with_children: bool = False
    max_child_runs: int | None = None
    max_delegation_depth: int | None = None
    allowed_tools: tuple[str, ...] | None = None
    max_outer_requests: int | None = None
    include_failed_attempt_usage: bool = False

    @property
    def _controlled_children(self) -> bool:
        return (
            self.share_with_children
            or self.max_child_runs is not None
            or self.max_delegation_depth is not None
            or self.allowed_tools is not None
        )

    def __post_init__(self) -> None:
        limits = (
            self.max_model_calls,
            self.max_tokens,
            self.max_tool_starts,
            self.max_child_runs,
            self.max_delegation_depth,
            self.max_outer_requests,
        )
        if all(limit is None for limit in limits) and self.allowed_tools is None:
            raise ValueError("RunBudget requires at least one limit.")
        if any(limit is not None and not _valid_limit(limit) for limit in limits):
            raise ValueError("RunBudget limits must be integers from zero through 2**53 - 1.")
        if self.missing_usage not in ("stop", "continue"):
            raise ValueError("RunBudget.missing_usage must be 'stop' or 'continue'.")
        if not isinstance(self.share_with_children, bool):
            raise TypeError("RunBudget.share_with_children must be a bool.")
        if not isinstance(self.include_failed_attempt_usage, bool):
            raise TypeError("RunBudget.include_failed_attempt_usage must be a bool.")
        if self.allowed_tools is not None:
            if not isinstance(self.allowed_tools, (tuple, list)) or any(
                not isinstance(name, str) or not name for name in self.allowed_tools
            ):
                raise ValueError("RunBudget.allowed_tools requires non-empty SDK tool names.")
            object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))


class _BudgetCheckpointError(UserError):
    """A budget failure that must also abort an enclosing agent-tool checkpoint."""


class RunBudgetError(AgentsException):
    """A blocked admission, with upstream model-call/token errors unchanged.

    For run_budget_exceeded, model_calls takes precedence over outer_requests,
    then tokens. For run_budget_usage_unavailable, limit is None.
    """

    def __init__(
        self,
        code: Literal["run_budget_exceeded", "run_budget_usage_unavailable"],
        *,
        limit: Literal[
            "model_calls",
            "tokens",
            "outer_requests",
            "tool_starts",
            "child_runs",
            "delegation_depth",
            "tools",
        ]
        | None = None,
    ):
        self.limit = limit
        self.code = code
        if code == "run_budget_usage_unavailable":
            message = 'Unknown usage. Use missing_usage="continue" to count only known totals.'
        else:
            ceilings = {
                "model_calls": "max_model_calls",
                "tokens": "max_tokens",
                "outer_requests": "max_outer_requests",
                "tool_starts": "max_tool_starts",
                "child_runs": "max_child_runs",
                "delegation_depth": "max_delegation_depth",
                "tools": "allowed_tools",
            }
            ceiling = ceilings.get(limit or "tokens", "max_tokens")
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
    tool_starts: int = 0
    child_runs: int = 0
    depth: int = 0
    identity: str = field(default_factory=lambda: uuid4().hex)
    parent: _BudgetState | None = field(default=None, repr=False)
    parent_identity: str | None = None

    @property
    def controlled_children(self) -> bool:
        # Portable checkpoints retain the binding before resume reattaches the live parent.
        return self.limits._controlled_children or self.parent_identity is not None

    def lineage(self) -> Iterable[_BudgetState]:
        current: _BudgetState | None = self
        while current is not None:
            yield current
            current = current.parent

    def reserve_request(self) -> None:
        for budget in self.lineage():
            budget.check()
        for budget in self.lineage():
            budget.requests += 1

    def reserve_tool(self, name: str, *, child: bool = False) -> None:
        # Check and reserve without awaiting so sibling tasks cannot oversubscribe.
        for budget in self.lineage():
            limits = budget.limits
            if limits.allowed_tools is not None and name not in limits.allowed_tools:
                raise RunBudgetError("run_budget_exceeded", limit="tools")
            if limits.max_tool_starts is not None and budget.tool_starts >= limits.max_tool_starts:
                raise RunBudgetError("run_budget_exceeded", limit="tool_starts")
            if child and limits._controlled_children:
                if limits.max_child_runs is not None and budget.child_runs >= limits.max_child_runs:
                    raise RunBudgetError("run_budget_exceeded", limit="child_runs")
                if (
                    limits.max_delegation_depth is not None
                    and self.depth + 1 - budget.depth > limits.max_delegation_depth
                ):
                    raise RunBudgetError("run_budget_exceeded", limit="delegation_depth")
        for budget in self.lineage():
            budget.tool_starts += 1
            if child:
                budget.child_runs += 1

    def observe(self, usage: Usage | None) -> None:
        for budget in self.lineage():
            budget._observe_local(usage)

    def observe_failed_attempt(self, usage: Usage | None) -> None:
        for budget in self.lineage():
            if budget.limits.include_failed_attempt_usage:
                budget._observe_local(usage)

    def _observe_local(self, usage: Usage | None) -> None:
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
        if limits.max_outer_requests is not None and self.requests >= limits.max_outer_requests:
            raise RunBudgetError("run_budget_exceeded", limit="outer_requests")
        if limits.max_tokens is not None and self.tokens >= limits.max_tokens:
            raise RunBudgetError("run_budget_exceeded", limit="tokens")
        if self.unavailable and limits.missing_usage == "stop":
            raise RunBudgetError("run_budget_usage_unavailable")

    def checkpoint(self) -> dict[str, Any]:
        # Preserve the exact schema 1.25 payload for budgets using only upstream fields.
        data: dict[str, Any] = {
            "max_requests": self.limits.max_model_calls,
            "max_tokens": self.limits.max_tokens,
            "missing_usage": self.limits.missing_usage,
            "requests": self.requests,
            "tokens": self.tokens,
            "unavailable": self.unavailable,
        }
        limits = self.limits
        if (
            limits.max_outer_requests is not None
            or limits.include_failed_attempt_usage
            or limits.max_tool_starts is not None
            or limits._controlled_children
            or self.parent is not None
            or self.parent_identity is not None
        ):
            data.update(
                {
                    "max_outer_requests": limits.max_outer_requests,
                    "include_failed_attempt_usage": limits.include_failed_attempt_usage,
                    "max_tool_starts": limits.max_tool_starts,
                    "share_with_children": limits.share_with_children,
                    "max_child_runs": limits.max_child_runs,
                    "max_delegation_depth": limits.max_delegation_depth,
                    "allowed_tools": list(limits.allowed_tools)
                    if limits.allowed_tools is not None
                    else None,
                    "tool_starts": self.tool_starts,
                    "child_runs": self.child_runs,
                    "depth": self.depth,
                    "identity": self.identity,
                    "parent_identity": self.parent.identity
                    if self.parent is not None
                    else self.parent_identity,
                }
            )
        return data

    @classmethod
    def restore(cls, data: Any) -> _BudgetState:
        required = {
            "max_requests",
            "max_tokens",
            "missing_usage",
            "requests",
            "tokens",
            "unavailable",
        }
        extension = {
            "max_outer_requests",
            "include_failed_attempt_usage",
            "max_tool_starts",
            "share_with_children",
            "max_child_runs",
            "max_delegation_depth",
            "allowed_tools",
            "tool_starts",
            "child_runs",
            "depth",
            "identity",
            "parent_identity",
        }
        if not isinstance(data, dict) or data.keys() not in (required, required | extension):
            raise _BudgetCheckpointError("Invalid run budget checkpoint.")
        counters = ("requests", "tokens") + (
            ("tool_starts", "child_runs", "depth") if "identity" in data else ()
        )
        if (
            any(type(data[key]) is not int or data[key] < 0 for key in counters)
            or type(data["unavailable"]) is not bool
        ):
            raise _BudgetCheckpointError("Invalid run budget consumption.")
        if "identity" in data and (
            not isinstance(data["identity"], str)
            or not data["identity"]
            or (
                data["parent_identity"] is not None and not isinstance(data["parent_identity"], str)
            )
        ):
            raise _BudgetCheckpointError("Invalid run budget identity.")
        try:
            limits = RunBudget(
                max_model_calls=data["max_requests"],
                max_tokens=data["max_tokens"],
                missing_usage=data["missing_usage"],
                **{
                    key: data[key]
                    for key in (
                        "max_outer_requests",
                        "include_failed_attempt_usage",
                        "max_tool_starts",
                        "share_with_children",
                        "max_child_runs",
                        "max_delegation_depth",
                        "allowed_tools",
                    )
                    if key in data
                },
            )
        except (TypeError, ValueError):
            raise _BudgetCheckpointError("Invalid run budget limits.") from None
        result = cls(limits, data["requests"], data["tokens"], data["unavailable"])
        for key in ("tool_starts", "child_runs", "depth", "identity", "parent_identity"):
            if key in data:
                setattr(result, key, data[key])
        return result


def _check_model(model: object, limits: RunBudget | None = None) -> None:
    from .extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel
    from .models.codex import CodexProvider, _CodexModel

    if isinstance(model, OpenAIHostedMultiAgentModel):
        raise UserError("RunBudget cannot isolate root usage in hosted multi-agent runs.")
    if isinstance(model, (CodexProvider, _CodexModel)) and limits is not None:
        if limits.max_model_calls is not None or limits.max_tokens is not None:
            raise UserError("RunBudget cannot enforce native sampling limits with CodexProvider.")
        if limits._controlled_children:
            raise UserError(
                "RunBudget cannot enforce controlled native delegation with CodexProvider."
            )


def _tool_budget(tool: Tool) -> RunBudget | None:
    invoker = getattr(tool, "on_invoke_tool", None)
    impl = getattr(invoker, "_invoke_tool_impl", None)
    config = getattr(impl, "__agents_run_config__", None)
    return config.budget if config is not None else None


def _validate_explicit_tool_authority(
    settings: ModelSettings,
    prompt: ResponsePromptParam | None,
    *,
    policy_name: str,
) -> None:
    """Reject provider-owned tool definitions hidden behind prompt or raw overrides."""
    if prompt is not None:
        raise UserError(f"{policy_name} requires explicit SDK tools, not prompt IDs.")
    for overrides in (settings.extra_args, settings.extra_body):
        if overrides is None:
            continue
        if not isinstance(overrides, Mapping) or any(
            key in overrides for key in ("tools", "prompt", "extra_body")
        ):
            raise UserError(f"{policy_name} cannot use raw tool or prompt overrides.")


def _check_restricted_tools(
    budget: _BudgetState | None,
    tools: Iterable[Tool],
    settings: ModelSettings | None = None,
    prompt: ResponsePromptParam | None = None,
) -> None:
    from .tool import (
        ApplyPatchTool,
        ComputerTool,
        CustomTool,
        FunctionTool,
        LocalShellTool,
        ShellTool,
    )

    if budget is None:
        return
    tools = list(tools)
    restricted = any(owner.limits.allowed_tools is not None for owner in budget.lineage())
    counted = any(owner.limits.max_tool_starts is not None for owner in budget.lineage())
    if restricted:
        if any(not isinstance(tool, FunctionTool) or tool.defer_loading for tool in tools):
            raise UserError(
                "RunBudget.allowed_tools supports only SDK FunctionTool handlers without "
                "deferred loading. Custom, shell, computer, apply-patch, hosted and tool-search "
                "routes cannot enforce this policy."
            )
        from .model_settings import ModelSettings

        _validate_explicit_tool_authority(
            settings if settings is not None else ModelSettings(),
            prompt,
            policy_name="RunBudget.allowed_tools",
        )
    if counted and any(
        isinstance(tool, (CustomTool, ComputerTool, ApplyPatchTool, LocalShellTool))
        or isinstance(tool, ShellTool)
        and tool.executor is not None
        for tool in tools
    ):
        raise UserError(
            "RunBudget.max_tool_starts supports SDK FunctionTool handlers only. "
            "wrap local custom, shell, computer or apply-patch execution in a FunctionTool. "
            "Provider-owned tool activity is outside this counter."
        )


async def _admit(
    budget: _BudgetState | None,
    model: Model,
    session: Session | None,
    tools: Iterable[Tool],
    *,
    streamed_result: RunResultStreaming | None = None,
    settings: ModelSettings | None = None,
    prompt: ResponsePromptParam | None = None,
) -> None:
    from .run_internal.resumable_session import ResumableRun, checkpoint_streamed_result

    try:
        if budget is not None:
            _check_restricted_tools(budget, tools, settings, prompt)
            for owner in budget.lineage():
                _check_model(model, owner.limits)
            budget.reserve_request()
    except (RunBudgetError, UserError) as error:
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


def _effective_limits(
    configured: RunBudget | None, parent: _BudgetState | None
) -> RunBudget | None:
    if configured is not None:
        return configured
    return parent.limits if parent is not None else None


async def _check_resume(
    state: RunState[Any],
    limits: RunBudget | None,
    context: RunContextWrapper[Any],
    parent: _BudgetState | None = None,
) -> bool:
    """Validate pending policies and report whether this subtree owns a budget."""
    from ._tool_identity import get_function_tool_lookup_key_for_call, get_function_tool_lookup_keys
    from .agent_tool_state import get_agent_tool_resume_state, peek_agent_tool_run_result
    from .tool import FunctionTool, _function_tool_enabled

    saved = state._context._budget if state._context is not None else None
    if (saved.limits if saved is not None else None) != _effective_limits(limits, parent):
        raise UserError("Resume requires the same RunBudget limits and missing_usage policy.")
    if saved is not None and (saved.parent_identity is not None or parent is not None):
        if (
            parent is None
            or saved.parent_identity != parent.identity
            or saved.depth != parent.depth + 1
        ):
            raise UserError("A shared child budget requires its original parent budget.")
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
        # the current qualified binding and grant only for required branches.
        key = get_function_tool_lookup_key_for_call(action.tool_call)
        tools = state._current_agent.tools if state._current_agent is not None else []
        bindings = [
            tool
            for tool in tools
            if isinstance(tool, FunctionTool) and key in get_function_tool_lookup_keys(tool)
        ]
        policies = (_tool_budget(tool) for tool in bindings)
        policy = next((value for value in policies if value is not None), None)
        child_parent = saved if saved is not None and saved.controlled_children else None
        if await _check_resume(child, policy, context, child_parent):
            owner = state._context
            if owner is not None and owner is not context:
                # Agent.as_tool keeps the nested wrapper but forwards application
                # context and usage. Leave the saved state untouched on refusal.
                owner = copy.copy(owner)
                owner.context = context.context
                owner.usage = context.usage
            if (
                len(bindings) != 1
                or bindings[0]._agent_instance is not action.function_tool._agent_instance
                or owner is None
                or state._current_agent is None
                or not await _function_tool_enabled(bindings[0], owner, state._current_agent)
            ):
                raise UserError("A pending RunBudget requires the same unique enabled agent tool.")
            budgeted = True
    if saved is not None and saved.parent_identity is not None:
        saved.parent = parent
    return budgeted


def _parent_budget(context: object) -> _BudgetState | None:
    """Only the invoking tool can supply the live parent, including on resume."""
    from .tool_context import ToolContext

    parent = context._budget if isinstance(context, ToolContext) else None
    return parent if parent is not None and parent.controlled_children else None


def _prepare_budget(
    config: RunConfig,
    agent: Agent[Any],
    state: RunState[Any] | None,
    inherited: _BudgetState | None,
) -> _BudgetState | None:
    from .models.interface import Model

    limits = _effective_limits(config.budget, inherited)
    current = (
        state._current_agent if state is not None and state._current_agent is not None else agent
    )
    if limits is None:
        return None
    model = config.model if config.model is not None else current.model
    _check_model(model if isinstance(model, Model) else config.model_provider, limits)
    if inherited is not None:
        for owner in inherited.lineage():
            _check_model(model if isinstance(model, Model) else config.model_provider, owner.limits)
    if state is not None:
        assert state._context is not None
        return state._context._budget
    budget = _BudgetState(limits)
    if inherited is not None:
        budget.parent = inherited
        budget.parent_identity = inherited.identity
        budget.depth = inherited.depth + 1
    _check_restricted_tools(budget, current.tools)
    budget.check()
    return budget
