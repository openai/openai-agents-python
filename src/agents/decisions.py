"""Specialist handoffs using the caller's Decisions client."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeVar

from openai import AsyncOpenAI
from openai.types.responses.response_usage import ResponseUsage
from pydantic import BaseModel, ConfigDict, Field

from . import _debug
from ._public_agent import get_public_agent
from ._run_state_agent_identity import _DecisionHandoff
from .agent import Agent
from .exceptions import _data_redacted_error_boundary, _mark_error_data_redacted
from .handoffs import Handoff, HandoffInputFilter
from .run_context import RunContextWrapper
from .usage import Usage, _response_usage_to_usage

__all__ = ["DecisionError", "decision_handoff"]

_T = TypeVar("_T")
_CHOICE_COUNTS = range(2, 256)


class DecisionError(ValueError):
    """A refused or invalid response. Only metadata is retained."""

    def __init__(
        self,
        reason: Literal["refusal", "invalid_response"],
        *,
        request_id: str | None = None,
        model: str | None = None,
        usage: Usage | None = None,
    ):
        super().__init__("Decision refused" if reason == "refusal" else "Invalid decision response")
        self.reason = reason
        self.request_id = request_id
        self.model = model
        self.usage = usage


def _read_answer(raw: Any, choices: list[str], ctx: RunContextWrapper[Any]) -> str | DecisionError:
    metadata: dict[str, Any] = {"request_id": raw.headers.get("x-request-id")}
    try:
        data = raw.http_response.json()
        if isinstance(data.get("model"), str):
            metadata["model"] = data["model"]
        if data.get("usage") is not None:
            usage = ResponseUsage.model_validate(data["usage"], strict=True, extra="ignore")
            metadata["usage"] = _response_usage_to_usage(usage)
            ctx.usage.add(metadata["usage"])
        answers = data["answers"]
        if isinstance(answers, list) and len(answers) == 1 and answers[0]["name"] == "decision":
            answer = answers[0]
            if answer["type"] == "refusal":
                return DecisionError("refusal", **metadata)
            if answer["type"] == "choice":
                label = answer["choice"]
                if isinstance(label, str) and label in choices:
                    return label
    except (ValueError, KeyError, AttributeError, TypeError, RecursionError):
        pass
    return DecisionError("invalid_response", **metadata)


class _RoutingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(min_length=1, max_length=8192)


def _routing_input(arguments: str) -> str:
    try:
        value = _RoutingInput.model_validate_json(arguments, strict=True).input
        if value.strip() and len(value.encode()) <= 8192:
            return value
    except ValueError:
        pass
    raise ValueError("Expected nonblank routing input of at most 8192 UTF-8 bytes")


def decision_handoff(
    source: Agent[_T],
    *,
    client: AsyncOpenAI,
    model: str,
    question: str,
    targets: Mapping[str, Agent[_T]],
    tool_name: str = "select_specialist",
    tool_description: str = "Transfer to a specialist. Supply only evidence needed for routing.",
    is_available: Callable[[RunContextWrapper[_T], str], bool] | None = None,
    timeout: float = 30,
    input_filter: HandoffInputFilter | None = None,
) -> Handoff[_T, Agent[_T]]:
    """Build a handoff for source.handoffs. Decisions chooses a label on a model request.

    Labels bind to concrete agents at construction. Removing or replacing a target
    revokes it. A synchronous is_available callback can further restrict each run.
    The primary model supplies the routing evidence. The optional input_filter
    changes the selected specialist's history only, after the Decisions request.
    Supply fresh context_override on protected reload. Resuming rechecks the
    selected grant without another request. Use separate specialist instances
    for different labels or ordinary handoffs.
    """
    if not isinstance(client, AsyncOpenAI):
        raise TypeError("Pass a caller-owned AsyncOpenAI client.")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("A nonblank Decisions model is required.")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("A nonblank question is required.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    resource = getattr(client.with_options(max_retries=0), "decisions", None)
    if resource is None:
        raise ValueError("Decision handoffs require an OpenAI client with Decisions support.")
    if not tool_name.strip() or not targets:
        raise ValueError("Expected a handoff name and targets")
    if any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, Agent)
        for k, v in targets.items()
    ):
        raise ValueError("Expected nonblank labels mapped to agents")
    roster = {label: (agent, agent.name) for label, agent in targets.items()}

    if len(roster) < 2 or (len(roster) > 255 and is_available is None):
        raise ValueError(
            "Decisions requires 2 to 255 available labels. Use handoff(target) for one."
        )
    if any(agent is source or not name.strip() for agent, name in roster.values()) or len(
        {id(agent) for agent, _ in roster.values()}
    ) != len(roster):
        raise ValueError("Create a separate named specialist for each decision label.")

    def admitted(ctx: RunContextWrapper[_T] | None, label: str) -> bool:
        agent, name = roster[label]
        return (
            (is_available is None or (ctx is not None and is_available(ctx, label) is True))
            and targets.get(label) is agent
            and agent.name == name
        )

    def available_choices(ctx: RunContextWrapper[_T]) -> list[str]:
        labels = [label for label in roster if admitted(ctx, label)]
        if len(labels) not in _CHOICE_COUNTS:
            return []
        return labels

    def enabled(ctx: RunContextWrapper[_T], agent: Any) -> bool:
        return get_public_agent(agent) is source and bool(available_choices(ctx))

    def attached() -> bool:
        return (
            route.tool_name == tool_name
            and route.on_invoke_handoff is invoke
            and route.is_enabled is enabled
            and any(h is route for h in source.handoffs)
        )

    def authorized(ctx: RunContextWrapper[_T] | None, label: str) -> bool:
        return admitted(ctx, label) and attached()

    @_data_redacted_error_boundary
    async def invoke(ctx: RunContextWrapper[_T], arguments: str) -> Agent[_T]:
        try:
            return await select(ctx, arguments)
        except (Exception, asyncio.CancelledError) as error:
            if _debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA:
                _mark_error_data_redacted(error)
            raise

    async def select(ctx: RunContextWrapper[_T], arguments: str) -> Agent[_T]:
        evidence = _routing_input(arguments)
        choices = available_choices(ctx)
        if not choices or not attached():
            raise ValueError("Decision handoff is unavailable")
        deadline = asyncio.get_running_loop().time() + timeout
        task = asyncio.create_task(
            resource.with_raw_response.create(
                model=model,
                input=evidence,
                questions=[
                    {
                        "type": "choice",
                        "name": "decision",
                        "instructions": question,
                        "choices": [{"value": label} for label in choices],
                    }
                ],
            )
        )
        try:
            await asyncio.wait((task,), timeout=timeout)
            if not task.done() or asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError("Decision timed out")
            if task.cancelled():
                raise RuntimeError("Decisions request was cancelled")
            answer = _read_answer(task.result(), choices, ctx)
        finally:
            if not task.done():
                task.cancel()
            interrupted = False
            while not task.done():
                try:
                    await asyncio.wait((task,))
                except asyncio.CancelledError:
                    interrupted = True
            if not task.cancelled():
                task.exception()
            del task
            if interrupted:
                raise asyncio.CancelledError
        if isinstance(answer, DecisionError):
            raise answer
        if not authorized(ctx, answer):
            raise ValueError("Selected specialist is unavailable")
        ctx._decision_binding = (route, answer)
        return roster[answer][0]

    route: _DecisionHandoff[_T] = _DecisionHandoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema=_RoutingInput.model_json_schema(),
        on_invoke_handoff=invoke,
        input_filter=input_filter,
        agent_name=tool_name,
        is_enabled=enabled,
    )
    route._source = source
    route._roster = roster
    route._can_restore = authorized
    return route
