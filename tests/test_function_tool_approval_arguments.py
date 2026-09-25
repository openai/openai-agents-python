"""Conditional approval observes the same invocation that can execute."""

import asyncio
import copy
import dataclasses
import json
import re
from typing import Annotated, Any, cast

import pytest
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from agents import Agent, RunConfig, RunHooks, Runner, RunState
from agents.decorators import tool
from agents.exceptions import ModelBehaviorError
from agents.items import ToolCallOutputItem
from agents.testing import ScriptedModel
from tests.realtime.session_test_support import RecordingRealtimeModel, _sent_tool_output_strings
from tests.test_responses import get_function_tool_call, get_text_message


async def run_agent(agent, value, streamed=False, *, hooks=None):
    config = RunConfig(tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(agent, value, run_config=config, hooks=hooks)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, value, run_config=config, hooks=hooks)


def scripted_agent(function_tool, arguments):
    model = ScriptedModel()
    model.extend(
        [
            [get_function_tool_call(function_tool.name, arguments, call_id="call_test")],
            [get_text_message("done")],
        ]
    )
    return Agent(name="test", model=model, tools=[function_tool])


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "arguments,expected_calls,interrupted",
    [
        ("{}", 0, True),
        ('{"endpoint":"protected"}', 1, True),
        ('{"endpoint":"safe"}', 1, False),
    ],
)
async def test_defaults_and_alias_keep_existing_policy(
    streamed, arguments, expected_calls, interrupted
):
    seen = []
    executed = []

    async def approve(_ctx, params, _id):
        seen.append(copy.deepcopy(params))
        return params.get("endpoint") == "protected"

    @tool(strict_mode=False, needs_approval=approve)
    async def operation(target: Annotated[str, Field(alias="endpoint")] = "protected") -> str:
        executed.append(target)
        return target

    result = await run_agent(scripted_agent(operation, arguments), "go", streamed)
    assert bool(result.interruptions) is interrupted
    assert len(seen) == expected_calls
    assert executed == ([] if interrupted else ["safe"])


@pytest.mark.parametrize("streamed", [False, True])
async def test_benign_omission_requires_approval_and_explicit_input_runs(streamed):
    seen = []
    executed = []

    async def approve(_ctx, params, _id):
        seen.append(params)
        return "environment" not in params

    @tool(strict_mode=False, needs_approval=approve)
    async def operation(environment: str = "safe") -> str:
        executed.append(environment)
        return environment

    omitted = await run_agent(scripted_agent(operation, "{}"), "go", streamed)
    assert omitted.interruptions and seen == [] and executed == []
    explicit = await run_agent(scripted_agent(operation, '{"environment":"safe"}'), "go", streamed)
    assert explicit.final_output == "done" and not explicit.interruptions
    assert seen == [{"environment": "safe"}] and executed == ["safe"]


@pytest.mark.parametrize("kind", ["case", "order", "in_place", "unchanged"])
async def test_nested_transformations_require_approval(kind):
    validations = []
    seen = []
    executed = []

    class Request(BaseModel):
        targets: dict[str, str]

        @field_validator("targets")
        @classmethod
        def transform(cls, targets):
            validations.append(1)
            if kind == "order":
                return dict(sorted(targets.items()))
            if kind == "case":
                return {key: value.lower() for key, value in targets.items()}
            return targets

        @model_validator(mode="before")
        @classmethod
        def mutate(cls, values):
            if kind == "in_place":
                values["targets"]["z"] = "prod"
            return values

        @field_serializer("targets")
        def serialize_targets(self, value):
            raise AssertionError("Approval must not run output serializers")

    async def approve(_ctx, params, _id):
        seen.append(copy.deepcopy(params))
        return next(iter(params["request"]["targets"].values())) in ("prod", "PROD")

    @tool(strict_mode=False, needs_approval=approve)
    async def operation(request: Request) -> str:
        target = next(iter(request.targets.values()))
        executed.append(target)
        return target

    raw = {"request": {"targets": {"z": "PROD" if kind == "case" else "test", "a": "prod"}}}
    agent = scripted_agent(operation, json.dumps(raw))
    result = await run_agent(agent, "go")
    assert validations == []
    assert result.interruptions and executed == [] and seen == []
    state = result.to_state()
    state.reject(state.get_interruptions()[0])
    await run_agent(agent, state)
    assert validations == [] and executed == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("serialize", [False, True])
@pytest.mark.parametrize("approve", [False, True])
async def test_manual_decision_roundtrip_does_not_rerun_predicate(streamed, serialize, approve):
    calls = []
    effects = []

    async def policy(_ctx, params, _id):
        calls.append(params)
        return False

    @tool(strict_mode=False, needs_approval=policy)
    async def operation(environment: str = "safe") -> str:
        effects.append(environment)
        return environment

    agent = scripted_agent(operation, "{}")
    paused = await run_agent(agent, "go", streamed)
    state = paused.to_state()
    if serialize:
        state = await RunState.from_string(agent, state.to_string())
    pending = state.get_interruptions()[0]
    if approve:
        state.approve(pending)
    else:
        state.reject(pending)
    result = await run_agent(agent, state, streamed)
    assert not result.interruptions and calls == []
    assert effects == (["safe"] if approve else [])


@pytest.mark.parametrize("failure", ["default", "custom", "raise"])
@pytest.mark.parametrize("mode", ["runner", "streamed", "realtime"])
@pytest.mark.parametrize("approve", [False, True])
async def test_invalid_typed_input_requires_approval_before_failure_policy(failure, mode, approve):
    from agents import ToolGuardrailFunctionOutput, ToolInputGuardrailData
    from agents.realtime import RealtimeAgent
    from agents.realtime.model_events import RealtimeModelToolCallEvent
    from agents.realtime.session import RealtimeSession
    from agents.tool_guardrails import tool_input_guardrail

    seen = []
    effects = []
    formatted = []
    callbacks = []

    async def policy(_ctx, params, _id):
        seen.append(params)
        return True

    @tool_input_guardrail
    def guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        callbacks.append("guardrail")
        return ToolGuardrailFunctionOutput.allow()

    class Hooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            callbacks.append("start")

    def formatter(_ctx, error):
        formatted.append(type(error))
        return "correct the number"

    options: dict[str, Any] = {}
    if failure != "default":
        options["failure_error_function"] = formatter if failure == "custom" else None

    @tool(needs_approval=policy, tool_input_guardrails=[guardrail], **options)
    async def operation(number: int) -> str:
        effects.append(number)
        return str(number)

    arguments = '{"number":"invalid"}'
    if mode == "realtime":
        model = RecordingRealtimeModel()
        async with RealtimeSession(
            model,
            RealtimeAgent(name="test", tools=[operation]),
            None,
            run_config={"async_tool_calls": False},
        ) as session:
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name=operation.name, call_id="invalid", arguments=arguments
                )
            )
            assert session._pending_tool_calls
            assert not seen and not formatted and not callbacks and not effects
            assert _sent_tool_output_strings(model) == []
            if not approve:
                await session.reject_tool_call("invalid")
                assert not formatted and not callbacks and not effects
            elif failure == "raise":
                with pytest.raises(ModelBehaviorError):
                    await session.approve_tool_call("invalid")
                assert _sent_tool_output_strings(model) == []
            else:
                await session.approve_tool_call("invalid")
                outputs = _sent_tool_output_strings(model)
                assert len(outputs) == 1
                if failure == "custom":
                    assert outputs == ["correct the number"]
            assert not session._pending_tool_calls
    else:
        agent = scripted_agent(operation, arguments)
        paused = await run_agent(agent, "go", mode == "streamed", hooks=Hooks())
        assert paused.interruptions
        assert not seen and not formatted and not callbacks and not effects
        state = await RunState.from_string(agent, paused.to_state().to_string())
        if not approve:
            state.reject(state.get_interruptions()[0])
            result = await run_agent(agent, state, mode == "streamed", hooks=Hooks())
            assert not result.interruptions and not formatted and not callbacks and not effects
        else:
            state.approve(state.get_interruptions()[0])
            if failure == "raise":
                with pytest.raises(ModelBehaviorError):
                    await run_agent(agent, state, mode == "streamed", hooks=Hooks())
            else:
                result = await run_agent(agent, state, mode == "streamed", hooks=Hooks())
                assert not result.interruptions
                outputs = [
                    item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
                ]
                assert len(outputs) == 1
                if failure == "custom":
                    assert outputs == ["correct the number"]
            assert "start" in callbacks
    assert not seen and not effects
    assert formatted == ([ModelBehaviorError] if approve and failure == "custom" else [])
    assert callbacks.count("guardrail") == int(approve)


@pytest.mark.parametrize("copy_tool", [copy.copy, dataclasses.replace])
async def test_preparation_is_preserved_on_copied_tools(copy_tool):
    seen = []
    effects = []

    async def policy(_ctx, params, _id):
        seen.append(copy.deepcopy(params))
        # A policy can mutate its raw input without changing prepared arguments.
        params["request"]["environment"] = "changed"
        return False

    @tool(needs_approval=policy, strict_mode=False)
    async def operation(request: dict[str, str]) -> str:
        effects.append(request["environment"])
        return request["environment"]

    copied = copy_tool(operation)
    result = await run_agent(scripted_agent(copied, '{"request":{"environment":"safe"}}'), "go")
    assert not result.interruptions
    assert effects == ["safe"] and seen == [{"request": {"environment": "safe"}}]


async def test_parallel_calls_keep_distinct_prepared_values():
    first_started = asyncio.Event()
    second_finished = asyncio.Event()
    executed = []

    async def policy(_ctx, params, _id):
        if params["request"]["value"] == 1:
            first_started.set()
            await second_finished.wait()
        else:
            await first_started.wait()
            second_finished.set()
        return False

    @tool(needs_approval=policy, strict_mode=False)
    async def operation(request: dict[str, int]) -> str:
        executed.append(request["value"])
        return str(request["value"])

    model = ScriptedModel()
    model.extend(
        [
            [
                get_function_tool_call(operation.name, '{"request":{"value":1}}', call_id="one"),
                get_function_tool_call(operation.name, '{"request":{"value":2}}', call_id="two"),
            ],
            [get_text_message("done")],
        ]
    )
    result = await run_agent(Agent(name="test", model=model, tools=[operation]), "go")
    assert result.final_output == "done" and executed == [2, 1]


@pytest.mark.parametrize(
    "arguments,pending,policy_count",
    [("{}", True, 0), ('{"value":"safe"}', False, 1), ('{"value":3}', True, 0)],
)
async def test_realtime_prepared_approval_and_errors(arguments, pending, policy_count):
    from agents.realtime import RealtimeAgent
    from agents.realtime.model_events import RealtimeModelToolCallEvent
    from agents.realtime.session import RealtimeSession

    seen = []
    effects = []

    async def policy(_ctx, params, _id):
        seen.append(params)
        return False

    @tool(strict_mode=False, needs_approval=policy)
    async def operation(value: str = "safe") -> str:
        effects.append(value)
        return value

    model = RecordingRealtimeModel()
    agent = RealtimeAgent(name="test", tools=[operation])
    async with RealtimeSession(
        model, agent, None, run_config={"async_tool_calls": False}
    ) as session:
        event = RealtimeModelToolCallEvent(name=operation.name, call_id="one", arguments=arguments)
        await session._handle_tool_call(event)
        assert bool(session._pending_tool_calls) is pending
        assert len(seen) == policy_count
        if pending:
            assert not effects
            await session.approve_tool_call("one")
            assert effects == ([] if arguments == '{"value":3}' else ["safe"])
            assert len(_sent_tool_output_strings(model)) == 1
        else:
            assert len(_sent_tool_output_strings(model)) == 1
            assert effects == (["safe"] if policy_count else [])


@pytest.mark.parametrize(
    "annotation,arguments,interrupted",
    [
        (int, '{"value":"1"}', True),
        (int, '{"value":true}', True),
        (float, '{"value":1}', True),
        (float, '{"value":1.0}', False),
        (tuple[int, ...], '{"value":[1]}', True),
        (int, '{"value":1,"extra":2}', True),
    ],
)
async def test_changed_or_uninspectable_argument_requires_manual_approval(
    annotation, arguments, interrupted
):
    seen = []
    effects = []

    def policy(_ctx, params, _id):
        seen.append(params)
        return False

    async def operation(value):
        effects.append(value)
        return "ok"

    operation.__annotations__ = {"value": annotation, "return": str}
    decorated = tool(operation, needs_approval=policy, strict_mode=False)
    result = await run_agent(scripted_agent(decorated, arguments), "go")
    assert bool(result.interruptions) is interrupted
    assert len(seen) == (0 if interrupted else 1)
    assert len(effects) == (0 if interrupted else 1)


async def test_root_parameter_order_does_not_change_approval():
    seen = []

    def policy(_ctx, params, _id):
        seen.append(list(params))
        return False

    @tool(needs_approval=policy)
    async def operation(first: str, second: str) -> str:
        return first + second

    result = await run_agent(scripted_agent(operation, '{"second":"b","first":"a"}'), "go")
    assert not result.interruptions and seen == [["second", "first"]]


async def test_invalid_keyword_reconstruction_uses_failure_policy():
    seen = []
    errors = []
    effects = []

    def policy(_ctx, params, _id):
        seen.append(params)
        return False

    def formatter(_ctx, error):
        errors.append(type(error))
        return "use unique keys"

    @tool(needs_approval=policy, strict_mode=False, failure_error_function=formatter)
    async def operation(value: str, **extra: str) -> str:
        effects.append(value)
        return value

    agent = scripted_agent(operation, '{"value":"safe","extra":{"value":"protected"}}')
    paused = await run_agent(agent, "go")
    assert paused.interruptions and not seen and not errors and not effects
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    result = await run_agent(agent, state)
    assert not result.interruptions and seen == [] and effects == []
    assert errors == [ModelBehaviorError]
    assert [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)] == [
        "use unique keys"
    ]


@pytest.mark.parametrize("invalid", [False, True])
async def test_resume_selection_reuses_preparation_or_requires_approval(invalid):
    # The resolver controls a queued call without a recorded approval. This isolates
    # the supported in-memory selection/execution handoff before any body is run.
    from agents import RunContextWrapper, RunHooks, Usage
    from agents.items import ModelResponse
    from agents.run_internal.agent_bindings import bind_public_agent
    from agents.run_internal.run_steps import (
        NextStepInterruption,
        ProcessedResponse,
        ToolRunFunction,
    )
    from agents.run_internal.turn_resolution import resolve_interrupted_turn

    policies = []
    effects = []
    errors = []

    def policy(_ctx, params, _id):
        policies.append(params)
        return False

    def formatter(_ctx, error):
        errors.append(type(error))
        return "correct input"

    @tool(needs_approval=policy, failure_error_function=formatter)
    async def operation(value: str) -> str:
        effects.append(value)
        return value

    arguments = '{"value":3}' if invalid else '{"value":"safe"}'
    call = get_function_tool_call(operation.name, arguments, call_id="resumed")
    agent = scripted_agent(operation, call.arguments)
    processed = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[ToolRunFunction(tool_call=call, function_tool=operation)],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )
    result = await resolve_interrupted_turn(
        bindings=bind_public_agent(agent),
        original_input="go",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[call], usage=Usage(), response_id="response"),
        processed_response=processed,
        hooks=RunHooks(),
        context_wrapper=RunContextWrapper(None),
        run_config=RunConfig(tracing_disabled=True),
        run_state=None,
    )
    assert effects == ([] if invalid else ["safe"])
    assert len(policies) == (0 if invalid else 1)
    assert errors == []
    assert isinstance(result.next_step, NextStepInterruption) is invalid
    outputs = [
        item.output for item in result.new_step_items if isinstance(item, ToolCallOutputItem)
    ]
    assert outputs == ([] if invalid else ["safe"])
    assert all(run._approval_evaluation is None for run in processed.functions)


@pytest.mark.parametrize("complex_alias", [False, True])
async def test_nested_input_aliases_keep_original_policy_keys(complex_alias):
    from pydantic import AliasChoices

    seen = []
    effects = []

    class Request(BaseModel):
        target: str = Field(
            validation_alias=AliasChoices("endpoint", "target") if complex_alias else "endpoint"
        )

    def policy(_ctx, params, _id):
        seen.append(params)
        return params["request"]["endpoint"] == "protected"

    @tool(strict_mode=False, needs_approval=policy)
    async def operation(request: Request) -> str:
        effects.append(request.target)
        return request.target

    result = await run_agent(
        scripted_agent(operation, '{"request":{"endpoint":"protected"}}'), "go"
    )
    assert result.interruptions and effects == []
    assert seen == []


async def test_cancelled_policy_does_not_reuse_preparation_for_a_new_run():
    started = asyncio.Event()
    policies = []
    effects = []

    async def policy(_ctx, params, _id):
        policies.append(params)
        if len(policies) == 1:
            started.set()
            await asyncio.Event().wait()
        return False

    @tool(needs_approval=policy, strict_mode=False)
    async def operation(request: dict[str, str]) -> str:
        effects.append(request["value"])
        return request["value"]

    task = asyncio.create_task(
        run_agent(scripted_agent(operation, '{"request":{"value":"first"}}'), "go")
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await run_agent(scripted_agent(operation, '{"request":{"value":"second"}}'), "go")
    assert not result.interruptions
    assert policies == [{"request": {"value": "first"}}, {"request": {"value": "second"}}]
    assert effects == ["second"]


@pytest.mark.parametrize("failure", ["default", "custom", "raise"])
@pytest.mark.parametrize("mode", ["runner", "streamed", "realtime"])
async def test_validator_exception_preserves_failure_policy(failure, mode):
    from agents.exceptions import UserError
    from agents.realtime import RealtimeAgent
    from agents.realtime.model_events import RealtimeModelToolCallEvent
    from agents.realtime.session import RealtimeSession

    validations = []
    policies = []
    effects = []
    errors = []

    class Request(BaseModel):
        value: str

        @field_validator("value", mode="before")
        @classmethod
        def normalize(cls, value):
            validations.append(value)
            return value.lower()

    def policy(_ctx, params, _id):
        policies.append(params)
        return False

    def formatter(_ctx, error):
        errors.append(type(error))
        return "correct value"

    options: dict[str, Any] = {}
    if failure != "default":
        options["failure_error_function"] = formatter if failure == "custom" else None

    @tool(needs_approval=cast(Any, policy), **options)
    def operation(request: Request) -> str:
        effects.append(request.value)
        return request.value

    arguments = '{"request":{"value":3}}'
    if mode == "realtime":
        model = RecordingRealtimeModel()
        async with RealtimeSession(
            model,
            RealtimeAgent(name="test", tools=[operation]),
            None,
            run_config={"async_tool_calls": False},
        ) as session:
            event = RealtimeModelToolCallEvent(
                name=operation.name, call_id="one", arguments=arguments
            )
            await session._handle_tool_call(event)
            assert session._pending_tool_calls and validations == [] and policies == []
            if failure == "raise":
                with pytest.raises(AttributeError):
                    await session.approve_tool_call("one")
                assert _sent_tool_output_strings(model) == []
            else:
                await session.approve_tool_call("one")
                outputs = _sent_tool_output_strings(model)
                assert len(outputs) == 1
                if failure == "custom":
                    assert outputs == ["correct value"]
            assert not session._pending_tool_calls
    else:
        agent = scripted_agent(operation, arguments)
        paused = await run_agent(agent, "go", mode == "streamed")
        assert paused.interruptions and validations == [] and policies == []
        state = paused.to_state()
        state.approve(state.get_interruptions()[0])
        if failure == "raise":
            with pytest.raises(UserError, match="Error running tool"):
                await run_agent(agent, state, mode == "streamed")
        else:
            result = await run_agent(agent, state, mode == "streamed")
            assert not result.interruptions
            outputs = [
                item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
            ]
            assert len(outputs) == 1
            if failure == "custom":
                assert outputs == ["correct value"]
    assert validations == [3] and policies == [] and effects == []
    assert errors == ([AttributeError] if failure == "custom" else [])


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("kind", ["annotated", "nested", "factory", "post_init", "union"])
async def test_application_validation_waits_for_explicit_approval(kind, approve, streamed):
    events = []

    def validate(value):
        events.append("validate")
        return value.lower()

    class Request(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def normalize(cls, value):
            return validate(value)

    class InitializedRequest(BaseModel):
        value: str

        def model_post_init(self, context):
            events.append("validate")

    def factory():
        events.append("validate")
        return "safe"

    annotation: Any = list[Annotated[str, BeforeValidator(validate)]]
    arguments = '{"value":["SAFE"]}'
    if kind == "nested":
        annotation = list[Request]
        arguments = '{"value":[{"value":"SAFE"}]}'
    elif kind == "factory":
        annotation = Annotated[str, Field(default_factory=factory)]
        arguments = "{}"
    elif kind == "post_init":
        annotation = InitializedRequest
        arguments = '{"value":{"value":"safe"}}'
    elif kind == "union":
        annotation = int | Annotated[str, AfterValidator(validate)]
        arguments = '{"value":"SAFE"}'

    async def policy(_ctx, _params, _id):
        events.append("policy")
        return True

    async def operation(value):
        events.append("body")
        return "ok"

    operation.__annotations__ = {"value": annotation, "return": str}
    decorated = tool(operation, needs_approval=policy, strict_mode=False)
    agent = scripted_agent(decorated, arguments)
    paused = await run_agent(agent, "go", streamed)
    assert paused.interruptions and events == []
    state = await RunState.from_string(agent, paused.to_state().to_string())
    if approve:
        state.approve(state.get_interruptions()[0])
    else:
        state.reject(state.get_interruptions()[0])
    result = await run_agent(agent, state, streamed)
    assert not result.interruptions
    assert events == (["validate", "body"] if approve else [])


@pytest.fixture
def pydantic_plugin_events(monkeypatch):
    events = []

    class Handler:
        def on_enter(self, value, **kwargs):
            events.append("enter")

        def on_success(self, value):
            events.append("success")

        def on_error(self, error):
            events.append("error")

    class Plugin:
        def new_schema_validator(self, schema, schema_type, path, kind, config, settings):
            return (Handler(), None, None) if path.name == "operation_args" else (None, None, None)

    # Use Pydantic's real plugin wrapper around the generated tool model.
    monkeypatch.setattr("pydantic.plugin._loader.get_plugins", lambda: [Plugin()])
    return events


@pytest.mark.parametrize("mode", ["runner", "streamed", "realtime"])
@pytest.mark.parametrize("approve", [False, True])
async def test_plugin_validation_waits_for_approval(pydantic_plugin_events, mode, approve):
    events = pydantic_plugin_events

    async def policy(_ctx, _params, _id):
        events.append("policy")
        return False

    @tool(needs_approval=policy)
    async def operation(value: int) -> str:
        events.append("body")
        return str(value)

    if mode == "realtime":
        from agents.realtime import RealtimeAgent
        from agents.realtime.model_events import RealtimeModelToolCallEvent
        from agents.realtime.session import RealtimeSession

        model = RecordingRealtimeModel()
        async with RealtimeSession(
            model,
            RealtimeAgent(name="test", tools=[operation]),
            None,
            run_config={"async_tool_calls": False},
        ) as session:
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name=operation.name, call_id="one", arguments='{"value":1}'
                )
            )
            assert session._pending_tool_calls and events == []
            if approve:
                await session.approve_tool_call("one")
            else:
                await session.reject_tool_call("one")
            assert not session._pending_tool_calls
            outputs = _sent_tool_output_strings(model)
    else:
        agent = scripted_agent(operation, '{"value":1}')
        paused = await run_agent(agent, "go", mode == "streamed")
        assert paused.interruptions and events == []
        state = await RunState.from_string(agent, paused.to_state().to_string())
        if approve:
            state.approve(state.get_interruptions()[0])
        else:
            state.reject(state.get_interruptions()[0])
        result = await run_agent(agent, state, mode == "streamed")
        assert not result.interruptions
        outputs = [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert events == (["enter", "success", "body"] if approve else [])
    assert len(outputs) == 1
    if approve:
        assert outputs == ["1"]


@pytest.mark.parametrize("block", [False, True])
async def test_plugin_validation_follows_input_guardrails(pydantic_plugin_events, block):
    from agents import ToolGuardrailFunctionOutput, ToolInputGuardrailData
    from agents.tool_guardrails import tool_input_guardrail

    events = pydantic_plugin_events

    async def policy(_ctx, _params, _id):
        events.append("policy")
        return True

    @tool_input_guardrail
    def guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        events.append("guardrail")
        if block:
            return ToolGuardrailFunctionOutput.reject_content("blocked")
        return ToolGuardrailFunctionOutput.allow()

    def failure(_ctx, _error):
        events.append("formatter")
        return "invalid value"

    @tool(needs_approval=policy, tool_input_guardrails=[guardrail], failure_error_function=failure)
    async def operation(value: int) -> str:
        events.append("body")
        return str(value)

    agent = scripted_agent(operation, '{"value":"bad"}')
    paused = await run_agent(agent, "go")
    assert paused.interruptions and events == []
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    result = await run_agent(agent, state)
    assert not result.interruptions
    assert events == (["guardrail"] if block else ["guardrail", "enter", "error", "formatter"])
    assert [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)] == [
        "blocked" if block else "invalid value"
    ]


@pytest.mark.parametrize("mode", ["runner", "streamed", "realtime"])
@pytest.mark.parametrize("approve", [False, True])
async def test_compiled_pattern_requires_explicit_approval(mode, approve):
    events = []

    async def policy(_ctx, _params, _id):
        events.append("policy")
        return False

    @tool(needs_approval=policy)
    async def operation(
        value: list[Annotated[str, Field(pattern=re.compile("^a+$", re.IGNORECASE))]],
    ) -> str:
        events.append("body")
        return value[0]

    # A short matching input exercises compiled regex validation without a
    # timing threshold or an expensive backtracking workload.
    arguments = '{"value":["AAA"]}'
    if mode == "realtime":
        from agents.realtime import RealtimeAgent
        from agents.realtime.model_events import RealtimeModelToolCallEvent
        from agents.realtime.session import RealtimeSession

        model = RecordingRealtimeModel()
        async with RealtimeSession(
            model,
            RealtimeAgent(name="test", tools=[operation]),
            None,
            run_config={"async_tool_calls": False},
        ) as session:
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(name=operation.name, call_id="one", arguments=arguments)
            )
            assert session._pending_tool_calls and events == []
            if approve:
                await session.approve_tool_call("one")
            else:
                await session.reject_tool_call("one")
            assert not session._pending_tool_calls
            outputs = _sent_tool_output_strings(model)
    else:
        agent = scripted_agent(operation, arguments)
        paused = await run_agent(agent, "go", mode == "streamed")
        assert paused.interruptions and events == []
        state = await RunState.from_string(agent, paused.to_state().to_string())
        if approve:
            state.approve(state.get_interruptions()[0])
        else:
            state.reject(state.get_interruptions()[0])
        result = await run_agent(agent, state, mode == "streamed")
        assert not result.interruptions
        outputs = [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert events == (["body"] if approve else [])
    assert len(outputs) == 1
    if approve:
        assert outputs == ["AAA"]


@pytest.mark.parametrize("compiled", [False, True])
@pytest.mark.parametrize("value", ["aaa", "a!"])
async def test_pattern_validation_runs_after_approval(compiled, value):
    events = []
    pattern = re.compile("^a+$") if compiled else "^a+$"

    async def policy(_ctx, _params, _id):
        events.append("policy")
        return False

    def failure(_ctx, _error):
        events.append("error")
        return "invalid value"

    @tool(needs_approval=policy, failure_error_function=failure)
    async def operation(value: list[Annotated[str, Field(pattern=pattern)]]) -> str:
        events.append("body")
        return value[0]

    agent = scripted_agent(operation, json.dumps({"value": [value]}))
    paused = await run_agent(agent, "go")
    assert paused.interruptions and events == []
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    result = await run_agent(agent, state)
    assert not result.interruptions
    assert events == (["body"] if value == "aaa" else ["error"])
    assert [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)] == [
        value if value == "aaa" else "invalid value"
    ]


@pytest.mark.parametrize("approve", [False, True])
async def test_realtime_application_validation_waits_for_approval(approve):
    from agents.realtime import RealtimeAgent
    from agents.realtime.model_events import RealtimeModelToolCallEvent
    from agents.realtime.session import RealtimeSession

    events = []

    def validate(value):
        events.append("validate")
        return value.lower()

    async def policy(_ctx, _params, _id):
        events.append("policy")
        return True

    @tool(needs_approval=policy)
    async def operation(value: list[Annotated[str, BeforeValidator(validate)]]) -> str:
        events.append("body")
        return value[0]

    model = RecordingRealtimeModel()
    async with RealtimeSession(
        model,
        RealtimeAgent(name="test", tools=[operation]),
        None,
        run_config={"async_tool_calls": False},
    ) as session:
        await session._handle_tool_call(
            RealtimeModelToolCallEvent(
                name=operation.name, call_id="one", arguments='{"value":["SAFE"]}'
            )
        )
        assert session._pending_tool_calls and events == []
        if approve:
            await session.approve_tool_call("one")
        else:
            await session.reject_tool_call("one")
        assert not session._pending_tool_calls
        assert events == (["validate", "body"] if approve else [])


@pytest.mark.parametrize("approved", [False, True])
async def test_input_guardrail_blocks_application_validation(approved):
    from agents import ToolExecutionConfig, ToolGuardrailFunctionOutput, ToolInputGuardrailData
    from agents.tool_guardrails import tool_input_guardrail

    events = []
    block = not approved

    def validate(value):
        events.append("validate")
        return value

    async def policy(_ctx, _params, _id):
        events.append("policy")
        return False

    @tool_input_guardrail
    def guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        events.append("guardrail")
        if block:
            return ToolGuardrailFunctionOutput.reject_content("blocked")
        return ToolGuardrailFunctionOutput.allow()

    @tool(needs_approval=policy, tool_input_guardrails=[guardrail])
    async def operation(value: list[Annotated[str, BeforeValidator(validate)]]) -> str:
        events.append("body")
        return value[0]

    agent = scripted_agent(operation, '{"value":["safe"]}')
    config = RunConfig(
        tracing_disabled=True,
        tool_execution=ToolExecutionConfig(pre_approval_tool_input_guardrails=True),
    )
    result = await Runner.run(agent, "go", run_config=config)
    if approved:
        assert result.interruptions and events == ["guardrail"]
        state = result.to_state()
        state.approve(state.get_interruptions()[0])
        block = True
        result = await Runner.run(agent, state, run_config=config)
        assert events == ["guardrail", "guardrail"]
    else:
        assert events == ["guardrail"]
    assert not result.interruptions
    assert [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)] == [
        "blocked"
    ]
