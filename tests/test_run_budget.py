"""Root budget contracts through the public Runner and approval boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import httpx2
import pytest
from openai import APIConnectionError, AsyncOpenAI

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelProvider,
    ModelRetrySettings,
    ModelSettings,
    RunBudget,
    RunBudgetError,
    RunConfig,
    Runner,
    RunState,
    Usage,
    UserError,
    input_guardrail,
    retry_policies,
    tool_namespace,
)
from agents.decorators import tool
from agents.memory.openai_responses_compaction_session import OpenAIResponsesCompactionSession
from agents.memory.resumable_session import ResumableSession, ResumableSessionError
from agents.models.codex import CodexProvider
from agents.models.openai_responses import OpenAIResponsesModel
from agents.testing import ModelStep, ScriptedModel
from tests.memory.test_openai_responses_compaction_session import SimpleListSession
from tests.memory.test_resumable_session import MemoryStore
from tests.model_test_helpers import get_response_obj
from tests.test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message


async def run(agent, budget, streamed=False, input="begin", **kwargs):
    config = RunConfig(
        budget=budget, tracing_disabled=True, tool_not_found_behavior="return_error_to_model"
    )
    if not streamed:
        return await Runner.run(agent, input, run_config=config, **kwargs)
    result = Runner.run_streamed(agent, input, run_config=config, **kwargs)
    async for _ in result.stream_events():
        pass
    return result


def step(item, total=None):
    usage = Usage(requests=1) if total is None else Usage(requests=1, total_tokens=total)
    return ModelStep(output=[item], usage=usage)


@pytest.mark.parametrize("value", [None, -1, True, 1.5, 2**53])
def test_invalid_limits(value):
    with pytest.raises(ValueError, match="RunBudget"):
        RunConfig(budget={"max_tokens": value})


@pytest.mark.parametrize("policy", ["stop", "continue"])
def test_cost_limit_and_estimator_must_be_paired(policy):
    with pytest.raises(ValueError, match="requires estimate_cost_usd"):
        RunBudget(max_estimated_cost_usd=1, missing_usage=policy)
    with pytest.raises(ValueError, match="requires max_estimated_cost_usd"):
        RunBudget(max_requests=2, estimate_cost_usd=lambda _model, _usage: 1, missing_usage=policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_missing_estimator_rejects_dict_before_dispatch(streamed):
    model = ScriptedModel()
    agent = Agent(name="root", model=model)
    config = {"budget": {"max_estimated_cost_usd": 1, "missing_usage": "continue"}}
    with pytest.raises(ValueError, match="requires estimate_cost_usd"):
        if streamed:
            Runner.run_streamed(agent, "begin", run_config=config)
        else:
            await Runner.run(agent, "begin", run_config=config)
    assert not model.calls


@pytest.mark.parametrize(
    "limits,blocked",
    [
        ({"max_requests": 0}, "requests"),
        ({"max_tokens": 0}, "tokens"),
        ({"max_estimated_cost_usd": 0}, "estimated_cost"),
        ({"max_requests": 0, "max_tokens": 0}, "requests"),
    ],
)
def test_zero_blocks_sync_dispatch(limits, blocked):
    model = ScriptedModel()
    with pytest.raises(RunBudgetError, match="run_budget_exceeded") as error:
        Runner.run_sync(Agent(name="root", model=model), "begin", run_config={"budget": limits})
    assert error.value.limit == blocked
    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_retry_counted_once_and_overshoot_can_finish(streamed):
    model = ScriptedModel(
        [
            APIConnectionError(request=httpx.Request("POST", "https://example.invalid")),
            step(get_function_tool_call("unknown"), 2),
            step(get_text_message("done"), 20),
        ]
    )
    retry = ModelRetrySettings(
        max_retries=1, policy=retry_policies.network_error(), backoff={"initial_delay": 0}
    )
    agent = Agent(name="root", model=model, model_settings=ModelSettings(retry=retry))
    result = await run(agent, RunBudget(max_requests=2, max_tokens=3), streamed, max_turns=2)
    assert result.final_output == "done"
    assert result.context_wrapper.usage.requests == len(model.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "total,policy,code",
    [
        (0, "stop", None),
        (None, "stop", "run_budget_usage_unavailable"),
        (-1, "stop", "run_budget_usage_unavailable"),
        (None, "continue", None),
        (10, "stop", "run_budget_exceeded"),
    ],
)
async def test_correction_uses_observed_totals(streamed, total, policy, code):
    model = ScriptedModel(
        [step(get_function_tool_call("unknown"), total), [get_text_message("done")]]
    )
    agent = Agent(name="root", model=model)
    budget = RunBudget(max_tokens=5, missing_usage=policy)
    if code:
        with pytest.raises(RunBudgetError, match=code) as error:
            await run(agent, budget, streamed)
        assert error.value.limit == ("tokens" if code == "run_budget_exceeded" else None)
        assert len(model.calls) == 1
    else:
        assert (await run(agent, budget, streamed)).final_output == "done"


def failing_estimate(model, usage):
    raise RuntimeError("private estimator detail")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "estimator,code",
    [
        (lambda _model, u: None, "run_budget_usage_unavailable"),
        (lambda _model, u: u.total_tokens * 2, "run_budget_exceeded"),
        (lambda _model, u: -0.01, "run_budget_invalid_estimate"),
        (lambda _model, u: float("inf"), "run_budget_invalid_estimate"),
        (lambda _model, u: "0.01", "run_budget_invalid_estimate"),
        (lambda _model, u: True, "run_budget_invalid_estimate"),
        (lambda _model, u: float("nan"), "run_budget_invalid_estimate"),
        (failing_estimate, "run_budget_invalid_estimate"),
    ],
)
@pytest.mark.parametrize("streamed", [False, True])
async def test_estimates_fail_distinctly_without_callback_details(estimator, code, streamed):
    model = ScriptedModel([step(get_function_tool_call("unknown"), 3)])
    budget = RunBudget(max_estimated_cost_usd=6, estimate_cost_usd=estimator)
    with pytest.raises(RunBudgetError) as error:
        await run(Agent(name="root", model=model), budget, streamed)
    assert error.value.limit == ("estimated_cost" if code == "run_budget_exceeded" else None)
    assert str(error.value) == error.value.code == code
    assert error.value.__context__ is None
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_max_turns_still_limits_corrections():
    model = ScriptedModel([[get_function_tool_call("unknown")]])
    with pytest.raises(MaxTurnsExceeded):
        await run(Agent(name="root", model=model), RunBudget(max_requests=2), max_turns=1)
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("portable", [False, True])
async def test_resume_after_handoff_checks_policy_before_approval(streamed, portable):
    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append(1)
        return "saved"

    target_model = ScriptedModel([step(get_function_tool_call("record"), 7)])
    target = Agent(name="target", model=target_model, tools=[record])
    model = ScriptedModel([step(get_handoff_tool_call(target), 2)])
    root = Agent(name="root", model=model, handoffs=[target])
    budget = RunBudget(max_requests=4, max_tokens=5)
    result = await run(root, budget, streamed)
    state = result.to_state()
    if portable:
        state = await RunState.from_string(root, state.to_string())
    assert state.to_json()["context"]["budget"]["tokens"] == 9
    state.approve(state.get_interruptions()[0])
    for changed in (
        None,
        replace(budget, max_tokens=10),
        replace(budget, missing_usage="continue"),
    ):
        with pytest.raises(UserError, match="same RunBudget"):
            await run(root, changed, not streamed, state)
    assert not effects
    with pytest.raises(RunBudgetError, match="exceeded"):
        await run(root, budget, not streamed, state)
    assert effects == [1]
    assert len(model.calls) == len(target_model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_durable_approval_stop_preserves_effect_and_idle_budget(streamed):
    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append(1)
        return "saved"

    store = MemoryStore()
    session = await ResumableSession.create(store)
    model = ScriptedModel([[get_function_tool_call("record")], [get_text_message("fresh")]])
    root = Agent(name="root", model=model, tools=[record])
    budget = RunBudget(max_requests=1)
    for invalid, error in [
        (RunBudget(max_requests=0), RunBudgetError),
        (
            RunBudget(max_estimated_cost_usd=1, estimate_cost_usd=lambda _model, _usage: 1),
            UserError,
        ),
    ]:
        with pytest.raises(error):
            await run(root, invalid, streamed, session=session)
    await run(root, budget, streamed, session=session)
    restored = await ResumableSession.resume(store, session.session_id)
    state = await restored.load_state(root)
    state.approve(state.get_interruptions()[0])
    with pytest.raises(UserError, match="same RunBudget"):
        await run(root, replace(budget, missing_usage="continue"), streamed, state)
    assert restored.is_paused and not effects
    with pytest.raises(RunBudgetError, match="exceeded"):
        await run(root, budget, streamed, state)
    assert effects == [1] and len(model.calls) == 1
    assert store.checkpoint(session.session_id)["status"] == "ready"
    assert (await run(root, budget, streamed, session=restored)).final_output == "fresh"
    assert "saved" in str(model.calls[-1].input)
    assert effects == [1]


@pytest.mark.asyncio
async def test_live_cost_approval_rejects_portable_capture():
    @tool(needs_approval=True)
    def record() -> str:
        return "saved"

    budget = RunBudget(
        max_estimated_cost_usd=1,
        estimate_cost_usd=lambda _model, _usage: None,
        missing_usage="continue",
    )
    model = ScriptedModel([step(get_function_tool_call("record"), 1), [get_text_message("done")]])
    root = Agent(name="root", model=model, tools=[record])
    state = (await run(root, budget)).to_state()
    with pytest.raises(UserError, match="estimated-cost"):
        state.to_string()
    state.approve(state.get_interruptions()[0])
    assert (await run(root, budget, input=state)).final_output == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_nested_tool_has_independent_budget_and_usage(streamed):
    child_model = ScriptedModel(
        [step(get_function_tool_call("unknown"), 50), step(get_text_message("child"), 50)]
    )
    child = Agent(name="child", model=child_model)
    nested = child.as_tool(
        "ask",
        "Ask child",
        on_stream=(lambda event: None) if streamed else None,
        failure_error_function=None,
    )
    model = ScriptedModel(
        [
            step(get_function_tool_call("ask", '{"input":"ask"}'), 1),
            step(get_text_message("done"), 1),
        ]
    )
    result = await run(
        Agent(name="root", model=model, tools=[nested]),
        RunBudget(max_requests=2, max_tokens=2),
        streamed,
    )
    assert result.final_output == "done"
    assert len(model.calls) == len(child_model.calls) == 2
    assert result.context_wrapper.usage.total_tokens == 102


@pytest.mark.asyncio
async def test_compaction_charges_tokens_without_an_admission():
    # ScriptedModel does not own the separate session compaction endpoint.
    client = MagicMock()
    client.responses.compact = AsyncMock(
        return_value=SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "synthetic"}],
            usage=Usage(total_tokens=7),
        )
    )
    session = OpenAIResponsesCompactionSession(
        session_id="example",
        underlying_session=SimpleListSession(),
        client=client,
        should_trigger_compaction=lambda ctx: True,
    )
    model = ScriptedModel([step(get_text_message("done"), 2)])
    result = await run(
        Agent(name="root", model=model), RunBudget(max_requests=1, max_tokens=3), session=session
    )
    checkpoint = result.to_state().to_json()["context"]["budget"]
    assert checkpoint["tokens"] == 9 and checkpoint["requests"] == 1


@pytest.mark.asyncio
async def test_native_sampling_rejected_without_dispatch():
    with pytest.raises(UserError, match="native sampling"):
        await Runner.run(
            Agent(name="root"),
            "begin",
            run_config=RunConfig(model_provider=CodexProvider(), budget=RunBudget(max_requests=1)),
        )


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("total", [None, 0])
async def test_responses_missing_total_before_normalization(total):
    # The Responses adapter normalizes null totals before returning to Runner.
    calls = []

    async def respond(request):
        calls.append(request)
        item = get_function_tool_call("unknown") if len(calls) == 1 else get_text_message("done")
        payload = get_response_obj([item]).model_dump()
        payload["usage"] = {
            "total_tokens": total,
            "input_tokens": 0,
            "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        return httpx2.Response(200, json=payload)

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
    ) as client:
        root = Agent(
            name="root", model=OpenAIResponsesModel(model="synthetic", openai_client=client)
        )
        budget = RunBudget(max_tokens=1)
        if total is None:
            with pytest.raises(RunBudgetError, match="unavailable"):
                await run(root, budget)
            assert len(calls) == 1
        else:
            assert (await run(root, budget)).final_output == "done"
            assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [False, True])
async def test_explicit_child_budget_survives_nested_approval(cost):
    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append(1)
        return "saved"

    child_model = ScriptedModel(
        [
            step(get_function_tool_call("record", call_id="inner"), 8),
            step(get_text_message("child"), 8),
        ]
    )
    child = Agent(name="child", model=child_model, tools=[record])
    child_budget = (
        RunBudget(max_estimated_cost_usd=9, estimate_cost_usd=lambda _model, u: u.total_tokens)
        if cost
        else RunBudget(max_requests=2)
    )
    nested = child.as_tool(
        "ask",
        "Ask child",
        run_config=RunConfig(budget=child_budget, tracing_disabled=True),
        failure_error_function=None,
    )
    model = ScriptedModel(
        [
            step(get_function_tool_call("ask", '{"input":"ask"}'), 1),
            step(get_text_message("done"), 2),
        ]
    )
    root = Agent(name="root", model=model, tools=[nested])
    budget = RunBudget(max_requests=2, max_tokens=2)
    state = (await run(root, budget)).to_state()
    if cost:
        with pytest.raises(UserError, match="estimated-cost"):
            state.to_string()
    else:
        state = await RunState.from_string(root, state.to_string())
    state.approve(state.get_interruptions()[0])
    assert (await run(root, budget, True, state)).final_output == "done"
    assert len(model.calls) == len(child_model.calls) == 2
    assert effects == [1]


@pytest.mark.asyncio
async def test_child_budget_requires_supported_schema_on_child_and_parent():
    @tool(needs_approval=True)
    def record() -> str:
        pytest.fail("Restoring an approval must not execute the tool.")

    child = Agent(
        name="child",
        model=ScriptedModel([step(get_function_tool_call("record", call_id="inner"), 7)]),
        tools=[record],
    )
    nested = child.as_tool(
        "ask", "Ask", run_config=RunConfig(budget=RunBudget(max_tokens=7), tracing_disabled=True)
    )
    parent = Agent(
        name="parent",
        model=ScriptedModel([step(get_function_tool_call("ask", '{"input":"check"}'), 1)]),
        tools=[nested],
    )
    state = (await run(parent, None)).to_state()
    for downgrade_parent in (True, False):
        data = state.to_json()
        inner = data["last_processed_response"]["functions"][0]["agent_run_state"]
        assert "budget" not in data["context"] and inner["context"]["budget"]["tokens"] == 7
        (data if downgrade_parent else inner)["$schemaVersion"] = "1.24"
        with pytest.raises(UserError, match="RunBudget"):
            await RunState.from_json(parent, data)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_unavailable_budgeted_grandchild_refuses_restore_before_sibling(approved):
    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append("record")
        return "saved"

    def model(*calls):
        return ScriptedModel([ModelStep(output=list(calls)), step(get_text_message("done"), 1)])

    leaf_model = model(get_function_tool_call("record", call_id="leaf"))
    leaf = Agent(name="leaf", tools=[record], model=leaf_model)
    config = RunConfig(budget=RunBudget(max_requests=2), tracing_disabled=True)
    allowed = True

    async def grant(ctx, agent):
        assert agent.name == "middle"
        return allowed and ctx.context["allowed"]

    inner = leaf.as_tool("inner", "Delegate", run_config=config, is_enabled=grant)
    middle_model = model(get_function_tool_call("inner", '{"input":"go"}', "inner"))
    middle = Agent(name="middle", tools=[inner], model=middle_model)
    root_model = model(
        get_function_tool_call("record", call_id="sibling"),
        get_function_tool_call("ask", '{"input":"go"}', "outer"),
    )
    root = Agent(name="root", tools=[record, middle.as_tool("ask", "Ask")], model=root_model)
    store = MemoryStore()
    session = await ResumableSession.create(store)
    state = (await run(root, None, session=session, context={"allowed": True})).to_state()
    if approved:
        for item in state.get_interruptions():
            state.approve(item)
    saved = state.to_string()
    checks = []

    def disabled(ctx, agent):
        checks.append(agent.name)
        return False

    unavailable = leaf.as_tool("inner", "Delegate", run_config=config, is_enabled=disabled)
    replacement = Agent(name="other", tools=[record]).as_tool(
        "inner", "Delegate", run_config=config
    )
    for tools in (
        [],
        [unavailable],
        [inner, inner],
        [replacement],
        [replace(record, name="inner")],
    ):
        middle.tools = list(tools)
        with pytest.raises(UserError):
            await RunState.from_string(root, saved)
    assert checks == ["middle"]
    middle.tools = [replace(inner, is_enabled=False)]
    with pytest.raises(UserError, match="required pending agent tool"):
        await RunState.from_string(root, saved, strict_context=True)
    assert not effects
    assert len(root_model.calls) == len(middle_model.calls) == len(leaf_model.calls) == 1
    middle.tools = [inner]
    pending = await session.load_state(root, context_override={"allowed": False})
    for item in pending.get_interruptions():
        pending.approve(item)
    for enabled, context in [(True, None), (False, {"allowed": True}), (True, {"allowed": False})]:
        allowed = enabled
        with pytest.raises(UserError, match="RunBudget"):
            await run(root, None, approved, pending, context=context)
        assert store.checkpoint(session.session_id)["status"] == "paused"
        assert not effects
        assert len(root_model.calls) == len(middle_model.calls) == len(leaf_model.calls) == 1
    result = await run(root, None, approved, pending, context={"allowed": True})
    assert result.final_output == "done" and effects == ["record", "record"]
    assert len(root_model.calls) == len(middle_model.calls) == len(leaf_model.calls) == 2


@pytest.mark.asyncio
async def test_missing_observation_survives_checkpoint():
    @tool(needs_approval=True)
    def record() -> str:
        return "saved"

    model = ScriptedModel([[get_function_tool_call("record")]])
    root = Agent(name="root", model=model, tools=[record])
    budget = RunBudget(max_tokens=5)
    paused = await run(root, budget)
    data = paused.to_state().to_json()
    for version in ("1.22", "1.23", "1.24"):
        with pytest.raises(UserError, match="RunBudget"):
            await RunState.from_json(root, {**data, "$schemaVersion": version})
    state = await RunState.from_json(root, data)
    state.approve(state.get_interruptions()[0])
    with pytest.raises(RunBudgetError, match="unavailable"):
        await run(root, budget, input=state)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_hosted_child_usage_cannot_be_isolated():
    from agents.extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel

    root = Agent(name="root", model=OpenAIHostedMultiAgentModel(model="synthetic"))
    with pytest.raises(UserError, match="hosted multi-agent"):
        await run(root, RunBudget(max_tokens=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_durable_child_policy_checked_before_claim(streamed):
    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append(1)
        return "saved"

    store = MemoryStore()
    session = await ResumableSession.create(store)
    inner = ScriptedModel(
        [
            step(get_function_tool_call("record", call_id="inner"), 4),
            step(get_text_message("child"), 4),
        ]
    )
    child = Agent(name="child", model=inner, tools=[record])
    config = RunConfig(
        budget=RunBudget(max_estimated_cost_usd=8, estimate_cost_usd=lambda _model, _usage: 1),
        tracing_disabled=True,
    )
    nested = child.as_tool("ask", "Ask", run_config=config, failure_error_function=None)
    outer = ScriptedModel(
        [
            step(get_function_tool_call("ask", '{"input":"check"}'), 1),
            step(get_text_message("done"), 1),
        ]
    )
    root = Agent(name="root", model=outer, tools=[nested])
    with pytest.raises(UserError, match="estimated-cost"):
        await run(root, None, streamed, session=session)
    assert not outer.calls and not inner.calls
    assert store.checkpoint(session.session_id)["status"] == "ready"
    budget = RunBudget(max_requests=2, max_tokens=5)
    config.budget = budget
    await run(root, budget, streamed, session=session)
    restored = await ResumableSession.resume(store, session.session_id)
    state = await restored.load_state(root)
    state.approve(state.get_interruptions()[0])
    for changed in (None, replace(budget, missing_usage="continue"), RunBudget(max_requests=3)):
        config.budget = changed
        with pytest.raises(UserError, match="same RunBudget"):
            await run(root, budget, streamed, state)
        assert store.checkpoint(session.session_id)["status"] == "paused"
        assert not effects
    config.budget = budget
    changed_tool = child.as_tool(
        "ask",
        "Ask",
        run_config=RunConfig(budget=RunBudget(max_requests=3), tracing_disabled=True),
        failure_error_function=None,
    )
    disabled = child.as_tool("ask", "Ask", run_config=config, is_enabled=False)
    for replacement in ([changed_tool], [disabled], [nested, changed_tool], []):
        root.tools = replacement
        with pytest.raises(UserError, match="RunBudget"):
            await run(root, budget, streamed, state)
        assert store.checkpoint(session.session_id)["status"] == "paused"
        assert len(outer.calls) == len(inner.calls) == 1
        assert not effects
    other = tool_namespace(name="other", description="Other tools", tools=[changed_tool])
    root.tools = [nested, *other]
    assert (await run(root, budget, streamed, state)).final_output == "done"
    assert len(outer.calls) == len(inner.calls) == 2
    assert effects == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_failed_resume_keeps_debit_and_tool_receipt(streamed):
    effects = []

    @tool(needs_approval=True)
    def save() -> str:
        effects.append(1)
        return "saved"

    model = ScriptedModel([[get_function_tool_call("save")], RuntimeError("offline")])
    root = Agent(name="root", model=model, tools=[save])
    budget = RunBudget(max_requests=2)
    state = (await run(root, budget, streamed)).to_state()
    state.approve(state.get_interruptions()[0])
    with pytest.raises(RuntimeError, match="offline"):
        await run(root, budget, streamed, state)
    restored = await RunState.from_string(root, state.to_string())
    with pytest.raises(RunBudgetError, match="exceeded"):
        await run(root, budget, not streamed, restored)
    assert len(model.calls) == 2
    assert effects == [1]


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    ("total", "policy"), [("absent", "stop"), (None, "stop"), (0, "stop"), (None, "continue")]
)
async def test_chat_total_presence_survives_normalization(streamed, total, policy):
    # Exercise Chat JSON and SSE conversion before the Runner sees normalized usage.
    import json

    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

    calls = []

    async def respond(request):
        calls.append(request)
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        if total != "absent":
            usage["total_tokens"] = total
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "unknown", "arguments": "{}"},
        }
        if streamed:
            call["index"] = 0
        first = len(calls) == 1
        message = {"tool_calls": [call]} if first else {"content": "done"}
        payload = {
            "id": "message",
            "created": 1,
            "model": "synthetic",
            "object": "chat.completion.chunk" if streamed else "chat.completion",
            "usage": usage,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if first else "stop",
                    "delta" if streamed else "message": {"role": "assistant", **message},
                }
            ],
        }
        if streamed:
            return httpx2.Response(
                200,
                content=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json=payload)

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
    ) as client:
        root = Agent(
            name="root", model=OpenAIChatCompletionsModel(model="synthetic", openai_client=client)
        )
        budget = RunBudget(max_tokens=1, missing_usage=policy)
        if total != 0 and policy == "stop":
            with pytest.raises(RunBudgetError, match="unavailable"):
                await run(root, budget, streamed)
            assert len(calls) == 1
        else:
            assert (await run(root, budget, streamed)).final_output == "done"
            assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_child_budget_stop_uses_existing_tool_error_policy(streamed):
    @tool(needs_approval=True)
    def record() -> str:
        return "saved"

    store = MemoryStore()
    session = await ResumableSession.create(store)
    inner = ScriptedModel([[get_function_tool_call("record", call_id="inner")]])
    child = Agent(name="child", model=inner, tools=[record])
    nested = child.as_tool(
        "ask", "Ask", run_config=RunConfig(budget=RunBudget(max_requests=1), tracing_disabled=True)
    )
    outer = ScriptedModel(
        [
            [get_function_tool_call("ask", '{"input":"check"}')],
            [get_text_message("done")],
        ]
    )
    root = Agent(name="root", model=outer, tools=[nested])
    budget = RunBudget(max_requests=2)
    await run(root, budget, streamed, session=session)
    state = await session.load_state(root)
    state.approve(state.get_interruptions()[0])
    assert (await run(root, budget, streamed, state)).final_output == "done"
    assert len(inner.calls) == 1
    assert "An error occurred while running the tool" in str(outer.calls[-1].input)
    assert store.checkpoint(session.session_id)["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_mode", ["none", "ordinary", "durable", "failed_save"])
@pytest.mark.parametrize("protected", [False, True])
async def test_stream_budget_stop_publishes_only_committed_work(session_mode, protected):
    effects = []

    @tool
    def record() -> str:
        effects.append(1)
        return "saved"

    store = MemoryStore()
    fail = session_mode == "failed_save"
    if session_mode in ("durable", "failed_save"):
        session = await ResumableSession.create(store)
        store.block_checkpoint = True
        store.fail_checkpoint = fail
    else:
        session = SimpleListSession() if session_mode == "ordinary" else None
    model = ScriptedModel([[get_text_message("working"), get_function_tool_call("record")]])
    agent = Agent(name="root", model=model, tools=[record])
    result = Runner.run_streamed(
        agent,
        "begin",
        session=session,
        run_config=RunConfig(
            budget=RunBudget(max_requests=1),
            tracing_disabled=True,
            redact_text=(lambda s: s.replace("working", "public")) if protected else None,
        ),
    )
    events = []

    async def consume():
        async for event in result.stream_events():
            if protected:
                assert event.type != "raw_response_event"
                assert "working" not in repr(event)
            if event.type == "run_item_stream_event":
                events.append(event.name)

    if not isinstance(session, ResumableSession):
        # Let the producer finish before the reader checks for errors.
        assert result.run_loop_task is not None
        _, pending = await asyncio.wait([result.run_loop_task], timeout=5)
        assert not pending
    consumer = asyncio.create_task(consume())
    try:
        if isinstance(session, ResumableSession):
            await asyncio.wait_for(store.saving.wait(), timeout=5)
            assert not events
    finally:
        store.release.set()
        expected = (
            ResumableSessionError if fail else (RuntimeError if protected else RunBudgetError)
        )
        with pytest.raises(expected) as error:
            await asyncio.wait_for(consumer, timeout=5)
    if protected and not fail:
        assert str(error.value) == "Error details are redacted."
        assert error.value.__context__ is None
    assert len(model.calls) == 1 and effects == [1]
    if isinstance(session, ResumableSession):
        assert store.checkpoint(session.session_id)["status"] == ("active" if fail else "ready")
    assert result.final_output is None
    assert events == ([] if fail else ["message_output_created", "tool_called", "tool_output"])


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("verdict", ["accept", "reject", "cancel"])
async def test_budget_stop_waits_for_parallel_input_verdict(durable, verdict):
    ran = asyncio.Event()

    @tool
    async def record() -> str:
        ran.set()
        return "saved"

    @input_guardrail
    async def check(ctx, agent, input):
        await ran.wait()
        if verdict == "cancel":
            raise asyncio.CancelledError
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=verdict == "reject")

    store = MemoryStore()
    session = await ResumableSession.create(store) if durable else None
    model = ScriptedModel([[get_text_message("working"), get_function_tool_call("record")]])
    result = Runner.run_streamed(
        Agent(name="root", model=model, tools=[record], input_guardrails=[check]),
        "begin",
        session=session,
        run_config=RunConfig(budget=RunBudget(max_requests=1), tracing_disabled=True),
    )
    assert result.run_loop_task is not None
    _, pending = await asyncio.wait([result.run_loop_task], timeout=5)
    assert not pending
    events = []
    with pytest.raises(InputGuardrailTripwireTriggered if verdict == "reject" else RunBudgetError):
        async for event in result.stream_events():
            events.append(event)
    assert ran.is_set() and len(model.calls) == 1 and result.final_output is None
    if verdict != "accept":
        assert not events
    else:
        assert [e.name for e in events if e.type == "run_item_stream_event"] == [
            "message_output_created",
            "tool_called",
            "tool_output",
        ]
    if session is not None:
        assert store.checkpoint(session.session_id)["status"] == (
            "ready" if verdict == "accept" else "active"
        )


@pytest.mark.parametrize("value", [True, -0.01, float("nan"), float("inf"), "0.01"])
def test_invalid_dollar_limit(value):
    with pytest.raises(ValueError, match="max_estimated_cost_usd"):
        RunBudget(max_estimated_cost_usd=value, estimate_cost_usd=lambda m, u: 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("prices, ceiling", [((0.1, 0.7), 0.8), ((1e-8, 2e-8), 3e-8)])
async def test_handoff_and_resume_charge_each_model_once(streamed, prices, ceiling):
    @tool(needs_approval=True)
    def record() -> str:
        return "saved"

    class PricedModel(ScriptedModel):
        model = "first"

    target_model = PricedModel([step(get_function_tool_call("record"), 7)])
    target_model.model = "second"
    target = Agent(name="target", model=target_model, tools=[record])
    model = PricedModel([step(get_handoff_tool_call(target), 2)])
    root = Agent(name="root", model=model, handoffs=[target])
    charged = []

    def estimate(name, usage):
        charged.append((name, usage.total_tokens))
        return {"first": prices[0], "second": prices[1]}[name]

    budget = RunBudget(max_estimated_cost_usd=ceiling, estimate_cost_usd=estimate)
    state = (await run(root, budget, streamed)).to_state()
    assert charged == [("first", 2)]
    state.approve(state.get_interruptions()[0])
    target_model.model = "later"
    with pytest.raises(RunBudgetError) as error:
        await run(root, budget, not streamed, state)
    assert error.value.limit == "estimated_cost"
    assert charged == [("first", 2), ("second", 7)]
    assert len(model.calls) == len(target_model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("named", [False, True])
async def test_override_prices_selected_name_or_none(streamed, named):
    model = ScriptedModel([step(get_function_tool_call("unknown"), 1)])

    class Provider(ModelProvider):
        def get_model(self, model_name):
            return model

    provider = Provider()
    charged = []

    def estimate(name, usage):
        charged.append(name)
        return 0.01 if name else None

    config = RunConfig(
        model="override" if named else model,
        model_provider=provider,
        budget=RunBudget(max_estimated_cost_usd=0.01, estimate_cost_usd=estimate),
        tool_not_found_behavior="return_error_to_model",
        tracing_disabled=True,
    )
    agent = Agent(name="root", model="unused")
    with pytest.raises(RunBudgetError) as error:
        if streamed:
            result = Runner.run_streamed(agent, "go", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go", run_config=config)
    assert charged == ["override" if named else None]
    assert error.value.code == ("run_budget_exceeded" if named else "run_budget_usage_unavailable")
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_compaction_uses_its_own_price_before_resume(streamed):
    @tool(needs_approval=True)
    def record() -> str:
        return "saved"

    model = ScriptedModel([step(get_function_tool_call("record"), 1)])
    agent = Agent(name="root", model=model, tools=[record])
    charged = []

    def estimate(name, usage):
        charged.append((name, usage.total_tokens))
        return 0.01 if name is None else 0.02

    budget = RunBudget(max_estimated_cost_usd=0.03, estimate_cost_usd=estimate)
    result = await run(agent, budget, streamed)
    client = MagicMock()
    client.responses.compact = AsyncMock(
        return_value=SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "synthetic"}],
            usage=Usage(total_tokens=7),
        )
    )
    history = SimpleListSession()
    await history.add_items([{"role": "user", "content": "earlier"}])
    session = OpenAIResponsesCompactionSession(
        "example", history, client=client, should_trigger_compaction=lambda ctx: True
    )
    await session.run_compaction(wrapper=result.context_wrapper)
    state = result.to_state()
    state.approve(state.get_interruptions()[0])
    with pytest.raises(RunBudgetError) as error:
        await run(agent, budget, streamed, state)
    assert error.value.limit == "estimated_cost"
    assert charged == [(None, 1), (session.model, 7)]
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_redaction_rejects_nonportable_budget_before_dispatch(streamed):
    model = ScriptedModel()
    root = Agent(name="root", model=model)
    config = RunConfig(
        redact_text=lambda text: text,
        budget=RunBudget(max_estimated_cost_usd=1, estimate_cost_usd=lambda m, u: 0),
    )
    with pytest.raises(RuntimeError, match="redacted"):
        if streamed:
            Runner.run_streamed(root, "begin", run_config=config)
        else:
            await Runner.run(root, "begin", run_config=config)
    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("prompt_owned", [False, True])
async def test_price_uses_openai_request_body_override_or_prompt_response(streamed, prompt_owned):
    import json

    sent = []
    priced = []

    def estimate(name, usage):
        priced.append(name)
        return 0.01 if name == "selected" else 0

    def respond(request):
        sent.append(json.loads(request.content).get("model"))
        item = get_function_tool_call("unknown") if len(sent) == 1 else get_text_message("done")
        payload = get_response_obj([item]).model_dump()
        payload["model"] = "selected" if prompt_owned else "reported"
        payload["usage"] = {
            "total_tokens": 1,
            "input_tokens": 1,
            "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        if streamed:
            event = {"type": "response.completed", "sequence_number": 1, "response": payload}
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
            )
        return httpx2.Response(200, json=payload)

    async with AsyncOpenAI(
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
    ) as client:
        agent = Agent(
            name="root",
            model=OpenAIResponsesModel(
                model="base", openai_client=client, model_is_explicit=not prompt_owned
            ),
            prompt={"id": "fixture"} if prompt_owned else None,
            model_settings=ModelSettings(
                extra_body=None if prompt_owned else {"model": "selected"}
            ),
        )
        with pytest.raises(RunBudgetError) as error:
            await run(
                agent, RunBudget(max_estimated_cost_usd=0.01, estimate_cost_usd=estimate), streamed
            )
        assert error.value.limit == "estimated_cost"
        assert sent == [None if prompt_owned else "selected"]
        assert priced == ["selected"]
