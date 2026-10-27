from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agents import Agent, RunContextWrapper, Runner, RunState, function_tool
from agents.exceptions import UserError
from agents.testing import ScriptedModel, assistant_message, function_call


class Preferences(BaseModel):
    channel: str = "sms"


class SupportContext(BaseModel):
    customer_id: str
    actions: list[str] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    idempotency_key: str = Field(default_factory=lambda: uuid4().hex)


@pytest.mark.asyncio
@pytest.mark.parametrize("string_snapshot", [False, True], ids=["json", "string"])
async def test_pydantic_context_defaults_survive_approval_resume(string_snapshot: bool) -> None:
    @function_tool
    def record_preference(ctx: RunContextWrapper[SupportContext], channel: str) -> str:
        ctx.context.actions.append("verified identity")
        ctx.context.preferences.channel = channel
        return "recorded"

    @function_tool(needs_approval=True)
    def issue_refund(ctx: RunContextWrapper[SupportContext]) -> str:
        context = ctx.context
        return f"{context.preferences.channel}|{context.actions}|{context.idempotency_key}"

    agent = Agent(
        name="support",
        model=ScriptedModel(
            [
                [function_call("record_preference", {"channel": "email"}, call_id="record")],
                [function_call("issue_refund", {}, call_id="refund")],
                [assistant_message("done")],
            ]
        ),
        tools=[record_preference, issue_refund],
    )
    context = SupportContext(customer_id="c-1")
    result = await Runner.run(agent, "refund me", context=context)
    assert len(result.interruptions) == 1
    state = result.to_state()
    payload = json.loads(state.to_string()) if string_snapshot else state.to_json()
    assert payload["context"]["context"] == {
        "customer_id": "c-1",
        "actions": ["verified identity"],
        "preferences": {"channel": "email"},
        "idempotency_key": context.idempotency_key,
    }
    meta = payload["context"]["context_meta"]
    assert meta["requires_deserializer"] is True
    assert meta["omitted"] is False
    if string_snapshot:
        restored = await RunState.from_string(
            agent, json.dumps(payload), context_deserializer=SupportContext.model_validate
        )
    else:
        restored = await RunState.from_json(
            agent, payload, context_deserializer=SupportContext.model_validate
        )
    for interruption in restored.get_interruptions():
        restored.approve(interruption)
    resumed = await Runner.run(agent, restored)
    outputs = [item.output for item in resumed.new_items if item.type == "tool_call_output_item"]
    assert outputs[-1] == f"email|['verified identity']|{context.idempotency_key}"
    assert resumed.final_output == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_defaults", [False, True], ids=["assigned", "defaults"])
async def test_pydantic_context_uuid_and_datetime_round_trip(use_defaults: bool) -> None:
    class AuditContext(BaseModel):
        request_id: UUID = Field(default_factory=uuid4)
        started_at: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)

    agent: Agent[Any] = Agent(name="audit")
    context = (
        AuditContext()
        if use_defaults
        else AuditContext(
            request_id=UUID(int=1), started_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
    )
    state = RunState(RunContextWrapper(context), "input", agent)
    payload = state.to_json()
    assert payload["context"]["context"] == {
        "request_id": str(context.request_id),
        "started_at": "2026-01-01T00:00:00Z",
    }
    json.dumps(payload)
    restored = await RunState.from_string(
        agent, state.to_string(), context_deserializer=AuditContext.model_validate
    )
    assert restored._context is not None
    assert restored._context.context == context


@pytest.mark.asyncio
@pytest.mark.parametrize("string_snapshot", [False, True], ids=["json", "string"])
@pytest.mark.parametrize("resource", [object(), b"\xff"], ids=["client", "binary"])
async def test_pydantic_context_unsupported_default_requires_serializer(
    string_snapshot: bool,
    resource: Any,
) -> None:
    class ResourceContext(SupportContext):
        model_config = ConfigDict(arbitrary_types_allowed=True)

        resource: Any = Field(default_factory=lambda: resource)

    context = ResourceContext(customer_id="c-1")
    context.actions.append("verified identity")
    context.preferences.channel = "email"
    agent: Agent[Any] = Agent(name="client")
    state = RunState(RunContextWrapper(context), "input", agent)
    with pytest.raises(UserError, match="context_serializer"):
        if string_snapshot:
            state.to_string()
        else:
            state.to_json()

    saved = state.to_string(
        context_serializer=lambda ctx: ctx.model_dump(mode="json", exclude={"resource"})
    )
    payload = json.loads(saved)
    assert payload["context"]["context_meta"]["serialized_via"] == "context_serializer"
    restored = await RunState.from_string(
        agent, saved, context_deserializer=ResourceContext.model_validate
    )
    assert restored._context is not None
    rebuilt = restored._context.context
    assert isinstance(rebuilt, ResourceContext)
    assert rebuilt.actions == ["verified identity"]
    assert rebuilt.preferences.channel == "email"
    assert rebuilt.idempotency_key == context.idempotency_key
