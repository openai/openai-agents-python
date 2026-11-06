"""Public per-root output capture, native request evidence and settled recovery."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace

import pytest
from pydantic import BaseModel, Field, ValidationError, model_validator

from agents import (
    Agent,
    CheckpointStatus,
    FunctionTool,
    InteractionRequired,
    NativeOutput,
    NativeProvider,
    OutputSpec,
    OutputValidationError,
    PromptedOutput,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_review_ii_steering_tail import ProjectContinuation


class Report(BaseModel):
    label: str = Field(min_length=2)


class SessionPolicyAgent(Agent):
    def __init__(self, policy, **declaration):
        super().__init__(**declaration)
        self.policy = policy

    @asynccontextmanager
    async def session(self, **kwargs):
        restoring = kwargs.get("state") is not None
        self.policy["entries"].append((restoring, self.output_type))
        if restoring and not self.policy["allow_restore"]:
            raise PermissionError("Application policy denies restore")
        async with super().session(**kwargs) as session:
            yield session


def output_format(request):
    return request.get("text", {}).get("format")


def test_closed_output_public_imports_and_explicit_str_schema():
    assert OutputSpec is not None
    assert CheckpointStatus("ready", ()).status == "ready"
    marked = NativeOutput(str)
    with pytest.raises(FrozenInstanceError):
        marked.type_ = int
    assert Agent(instructions="Answer", output_type=marked).validate() is None
    with pytest.raises((TypeError, ValueError)):
        Agent(instructions="Answer", output_type=NativeOutput(dict[str, int])).validate()
    assert (
        Agent(instructions="Answer", output_type=PromptedOutput(dict[str, int])).validate() is None
    )


async def test_native_plain_prompted_overrides_restore_default_at_each_root(responses):
    responses.respond(assistant_output('{"label":"default"}'))
    responses.respond(assistant_output("ordinary text"))
    responses.respond(assistant_output('{"count":5}'))
    responses.respond(assistant_output('{"response":"explicit native text"}'))
    responses.respond(assistant_output('{"label":"again"}'))
    agent = Agent(instructions="Answer.", provider=provider(responses), output_type=Report)
    async with timeout(20), agent.session() as session:
        assert session.active_turn is None and session.restored_turn is None
        first = await session.submit("one")
        assert (await first.get_result()).final_output.label == "default"
        plain = await session.submit("two", output_type=str)
        assert (await plain.get_result()).final_output == "ordinary text"
        prompted = await session.submit("three", output_type=PromptedOutput(dict[str, int]))
        assert (await prompted.get_result()).final_output == {"count": 5}
        native_str = await session.submit("four", output_type=NativeOutput(str))
        assert (await native_str.get_result()).final_output == "explicit native text"
        last = await session.submit("five")
        assert (await last.get_result()).final_output.label == "again"
        assert session.active_turn is None and session.restored_turn is None
    a, b, c, d, e = responses.requests
    assert output_format(a)["type"] == "json_schema"
    assert output_format(b) is None
    assert output_format(c) is None
    assert "Return only JSON" in json.dumps(c["input"])
    assert "additionalProperties" in json.dumps(c["input"])
    assert output_format(d)["schema"]["properties"]["response"]["type"] == "string"
    assert output_format(e) == output_format(a)
    assert (await first.get_result()).final_output.label == "default"


@pytest.mark.parametrize("mode", [NativeOutput, PromptedOutput])
@pytest.mark.parametrize("validator_error", [ValueError, TypeError])
async def test_final_validation_is_once_per_exact_turn_without_effect_retry(
    responses, mode, validator_error
):
    validated: list[str] = []
    effects: list[str] = []

    class Counted(BaseModel):
        label: str

        @model_validator(mode="after")
        def remember(self):
            validated.append(self.label)
            if self.label == "synthetic-rejected-output":
                raise validator_error(f"synthetic-validator-detail: {self.label}")
            return self

    @function_tool
    async def record(label: str) -> str:
        effects.append(label)
        return label

    responses.respond(function_call("record", {"label": "effect-once"}, "only-call"))
    raw = '{"label":"synthetic-rejected-output"}'
    responses.respond(assistant_output(raw))
    responses.respond(assistant_output('{"label":"accepted"}'))
    agent = Agent(instructions="Answer.", tools=[record], provider=provider(responses))
    variant = agent.with_output_type(mode(Counted))
    assert variant.provider is agent.provider and variant.tools == agent.tools
    async with timeout(20), variant.session() as session:
        first = await session.submit("go")
        for _ in range(2):
            with pytest.raises(OutputValidationError) as failure:
                await first.get_result()
            assert failure.value.raw_output == raw
            assert "synthetic" not in str(failure.value)
            assert "synthetic" not in repr(failure.value)
            assert failure.value.__context__ is None and failure.value.__cause__ is None
        assert validated == ["synthetic-rejected-output"]
        assert effects == ["effect-once"]
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "only-call") == "effect-once"
        second = await session.submit("try a new turn")
        assert (await second.get_result()).final_output.label == "accepted"
        assert (await second.get_result()).final_output.label == "accepted"
        assert validated == ["synthetic-rejected-output", "accepted"]
    assert len(responses.requests) == 3
    assert all(
        (output_format(request) is not None) == (mode is NativeOutput)
        for request in responses.requests
    )


@pytest.mark.parametrize(
    "output,final",
    [
        (str, "plain final"),
        (PromptedOutput(Report), '{"label":"resumed"}'),
        (NativeOutput(Report), '{"label":"resumed"}'),
    ],
)
async def test_override_manual_seal_restores_fresh_authority_with_captured_contract(
    responses: ResponsesFixture, output, final
):
    tool = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Look up A.", tools=[tool], provider=provider(responses), output_type=int
    )
    responses.respond(function_call("lookup", {"key": "A"}, "lookup-A"))
    async with timeout(25):
        async with agent.session() as original:
            first = await original.submit("read", output_type=output)
            assert original.restored_turn is None
            request = await next_request(original.interactions(), tool)
            assert original.active_turn is first
            # A grant can arrive before Core settles its sample. Admission is only advisory.
            assert original._active is not None
            assert await original._wait_manual_boundary(original._active, require_request=True)
            assert (await original.checkpoint_status(kind="manual_wait")).status == "ready"
            saved = SessionState.from_json((await original.suspend()).to_json())
            assert saved.output_version == 1
            assert saved.native_continuation is not None
            if isinstance(output, NativeOutput):
                assert (
                    saved.native_continuation.output_schema
                    == output_format(responses.requests[0])["schema"]
                )
            else:
                assert saved.native_continuation.output_schema is None
            with pytest.raises((RuntimeError, ValueError)):
                await request.respond("stale")
            with pytest.raises(InteractionRequired):
                await first.get_result()
        current = agent.with_output_type(output)
        async with current.session(state=saved) as restored:
            new_turn = restored.restored_turn
            assert new_turn is not None and restored.active_turn is new_turn
            assert new_turn.turn_id == first.turn_id
            assert new_turn.submission_id != first.submission_id
            fresh = await next_request(restored.interactions(), tool)
            assert len(responses.requests) == 1
            assert (fresh.call_id, fresh.turn_id) == (request.call_id, request.turn_id)
            responses.respond(assistant_output(final))
            await fresh.respond("A")
            result = await new_turn.get_result()
            if output is str:
                assert result.final_output == "plain final"
            else:
                assert result.final_output.label == "resumed"
            assert restored.active_turn is None and restored.restored_turn is new_turn
            responses.respond(assistant_output("a later explicit turn"))
            later = await restored.submit("next", output_type=str)
            assert (await later.get_result()).final_output == "a later explicit turn"
            assert restored.restored_turn is new_turn
            assert (await new_turn.get_result()).final_output == result.final_output
            idle = await restored.save_state()
            assert idle.format_version == 4
        assert output_format(responses.requests[1]) == output_format(responses.requests[0])
        assert (output_format(responses.requests[0]) is not None) == isinstance(
            output, NativeOutput
        )
        assert output_format(responses.requests[2]) is None
        assert tool_output(responses.requests[1], "lookup-A") == "A"


async def test_output_variant_denied_manual_restore_precedes_provider_acquisition(responses):
    tool = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    connection = provider(responses)
    policy = {"allow_restore": True, "entries": []}
    agent = SessionPolicyAgent(policy, instructions="Read.", tools=[tool], provider=connection)
    mode = PromptedOutput(Report)
    responses.respond(function_call("lookup", {"key": "A"}, "call-a"))
    async with timeout(20):
        async with agent.session() as session:
            await session.submit("read", output_type=mode)
            await next_request(session.interactions(), tool)
            saved = SessionState.from_json((await session.suspend()).to_json())
        variant = agent.with_output_type(mode)
        policy["allow_restore"] = False
        # NativeProvider would reject acquisition; application denial must precede that boundary.
        await connection.aclose()
        with pytest.raises(PermissionError, match="Application policy denies restore"):
            async with variant.session(state=saved):
                pytest.fail("The variant bypassed the application restore policy")
        assert policy["entries"] == [(False, None), (True, mode)]
        assert len(responses.requests) == 1


async def test_output_variant_allows_manual_restore_and_dispatches_policy_for_runs(responses):
    tool = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    policy = {"allow_restore": True, "entries": []}
    agent = SessionPolicyAgent(
        policy, instructions="Read.", tools=[tool], provider=provider(responses)
    )
    mode = PromptedOutput(Report)
    responses.respond(function_call("lookup", {"key": "A"}, "call-a"))
    async with timeout(20):
        async with agent.session() as session:
            turn = await session.submit("read", output_type=mode)
            old = await next_request(session.interactions(), tool)
            saved = SessionState.from_json((await session.suspend()).to_json())
        variant = agent.with_output_type(mode)
        assert agent.output_type is None
        async with variant.session(state=saved) as restored:
            fresh_turn = restored.restored_turn
            assert fresh_turn is not None and fresh_turn.submission_id != turn.submission_id
            fresh = await next_request(restored.interactions(), tool)
            with pytest.raises((RuntimeError, ValueError)):
                await old.respond("stale")
            responses.respond(assistant_output('{"label":"allowed"}'))
            await fresh.respond("A")
            assert (await fresh_turn.get_result()).final_output == Report(label="allowed")
        assert policy["entries"] == [(False, None), (True, mode)]
        responses.respond(assistant_output('{"label":"ordinary variant run"}'))
        assert (
            await variant.run(
                "answer",
                on_interaction=lambda _: pytest.fail("Ordinary run unexpectedly needed lookup"),
            )
        ).final_output == Report(label="ordinary variant run")
        responses.respond(assistant_output("ordinary original run"))
        assert (
            await agent.run(
                "answer",
                on_interaction=lambda _: pytest.fail("Ordinary run unexpectedly needed lookup"),
            )
        ).final_output == "ordinary original run"
        assert policy["entries"] == [(False, None), (True, mode), (False, mode), (False, None)]
        assert tool_output(responses.requests[1], "call-a") == "A"
        assert all(output_format(r) is None for r in responses.requests)


async def test_output_fingerprint_and_version_fail_before_acquiring_provider_or_grants(responses):
    tool = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(instructions="Read.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "call-a"))
    async with timeout(20):
        async with agent.session() as session:
            await session.submit("read", output_type=PromptedOutput(Report))
            await next_request(session.interactions(), tool)
            saved = await session.suspend()
        unavailable = NativeProvider(base_url=responses.url, api_key="synthetic-closed")
        await unavailable.aclose()
        # Acquiring this provider would raise RuntimeError; neither rejection may acquire it.
        wrong = replace(agent, provider=unavailable)
        with pytest.raises(ValueError, match="output contract"):
            async with wrong.session(state=saved):
                pytest.fail("wrong validator restored")
        correct = wrong.with_output_type(PromptedOutput(Report))
        for state in (
            saved.model_copy(update={"output_version": 2}),
            saved.model_copy(update={"output_fingerprint": "wrong"}),
        ):
            with pytest.raises((ValidationError, ValueError)):
                async with correct.session(state=state):
                    pytest.fail("unsupported version or contract restored")
        raw = json.loads(saved.to_json())
        del raw["output_version"]
        with pytest.raises(ValidationError):
            SessionState.from_json(json.dumps(raw))
        assert len(responses.requests) == 1


async def test_advice_does_not_reserve_manual_boundary_or_future_idle_save(responses):
    tool = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(instructions="Read.", tools=[tool], provider=provider(responses))
    async with timeout(20), agent.session() as session:
        assert await session.checkpoint_status(kind="idle") == CheckpointStatus("ready", ())
        responses.respond(function_call("lookup", {"key": "A"}, "call-a"))
        turn = await session.submit("read")
        request = await next_request(session.interactions(), tool)
        assert (await session.checkpoint_status(kind="idle")).status == "not_ready"
        with pytest.raises(RuntimeError, match="idle"):
            await session.save_state()
        # Wait for setup to settle before testing whether advice reserves the checkpoint.
        assert session._active is not None
        assert await session._wait_manual_boundary(session._active, require_request=True)
        assert (await session.checkpoint_status(kind="manual_wait")).status == "ready"
        responses.respond(assistant_output("finished"))
        await request.respond("A")
        assert (await turn.get_result()).final_output == "finished"
        with pytest.raises(RuntimeError, match="pending manual"):
            await session.suspend()
        assert (await session.checkpoint_status(kind="manual_wait")).status == "not_ready"
        assert await session.checkpoint_status(kind="idle") == CheckpointStatus("ready", ())
        assert (await session.save_state()).format_version == 4
        assert len(responses.requests) == 2


async def test_checkpoint_advice_keeps_native_probe_failures_unknown(responses, monkeypatch):
    tool = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(instructions="Read.", tools=[tool], provider=provider(responses))
    unknown = CheckpointStatus("unknown", ("Native checkpoint readiness could not be verified",))
    async with timeout(20), agent.session() as session:
        native = session._native

        class ProbeFailure:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _export_checkpoint(self):
                raise RuntimeError("private idle native probe detail")

            async def _manual_ready(self):
                raise RuntimeError("private manual native probe detail")

        monkeypatch.setattr(session, "_native", ProbeFailure())
        assert await session.checkpoint_status(kind="idle") == unknown
        # Capture errors still belong to the caller; status never turns one into a save.
        with pytest.raises(RuntimeError, match="private idle native probe detail"):
            await session.save_state()
        assert (await session.checkpoint_status(kind="manual_wait")).status == "not_ready"
        monkeypatch.setattr(session, "_native", native)
        responses.respond(function_call("lookup", {"key": "A"}, "call-a"))
        turn = await session.submit("read")
        request = await next_request(session.interactions(), tool)
        assert session._active is not None
        assert await session._wait_manual_boundary(session._active, require_request=True)
        monkeypatch.setattr(session, "_native", ProbeFailure())
        # Local refusals remain definite even when native probes would fail.
        assert (await session.checkpoint_status(kind="idle")).status == "not_ready"
        assert await session.checkpoint_status(kind="manual_wait") == unknown
        with pytest.raises(RuntimeError, match="private manual native probe detail"):
            await session.suspend()
        monkeypatch.setattr(session, "_native", native)
        assert (await session.checkpoint_status(kind="manual_wait")).status == "ready"
        responses.respond(assistant_output("finished"))
        await request.respond("A")
        assert (await turn.get_result()).final_output == "finished"
        assert (await session.save_state()).format_version == 4


async def test_idle_save_uses_default_contract_after_typed_override(responses):
    agent = Agent(instructions="Answer.", provider=provider(responses))
    responses.respond(assistant_output('{"label":"typed override"}'))
    responses.respond(assistant_output("plain after idle restore"))
    async with timeout(20):
        async with agent.session() as session:
            turn = await session.submit("one", output_type=NativeOutput(Report))
            assert (await turn.get_result()).final_output.label == "typed override"
            saved = await session.save_state()
        async with agent.session(state=SessionState.from_json(saved.to_json())) as restored:
            assert restored.restored_turn is None and restored.active_turn is None
            assert (
                await (await restored.submit("two")).get_result()
            ).final_output == "plain after idle restore"
    assert output_format(responses.requests[0]) is not None
    assert output_format(responses.requests[1]) is None


@pytest.mark.parametrize("mode", [NativeOutput, PromptedOutput])
async def test_python_continuation_capture_keeps_mode_across_real_native_submissions(
    responses, monkeypatch, mode
):
    # Core's gap cannot be triggered deterministically by a Responses peer. This
    # existing projection changes only tail admission; the real native engine
    # still owns both requests, results and each output schema on the wire.
    responses.respond(assistant_output('{"label":"first"}'))
    responses.respond(assistant_output('{"label":"automatic"}'))
    responses.respond(assistant_output("later default"))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        monkeypatch.setattr(session, "_native", ProjectContinuation(session._native))
        first = await session.submit("original", output_type=mode(Report))
        auto = await first.get_result()
        assert auto.final_output.label == "automatic"
        assert auto.turn is first
        later = await session.submit("next")
        assert (await later.get_result()).final_output == "later default"
        assert (await first.get_result()).turn is auto.turn
    a, b, c = responses.requests
    assert (output_format(a) is not None) == (mode is NativeOutput)
    assert output_format(b) == output_format(a)
    assert output_format(c) is None


async def test_prompted_explicit_session_suspends_and_resumes_same_root(responses):
    tool = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(
        instructions="Read.",
        tools=[tool],
        provider=provider(responses),
        output_type=PromptedOutput(Report),
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-a"))
    async with timeout(20):
        async with agent.session() as source:
            turn = await source.submit("read")
            await next_request(source.interactions(), tool)
            saved = await source.suspend()
            with pytest.raises(InteractionRequired) as pending:
                await turn.get_result()
            assert pending.value.state.to_json() == saved.to_json()
        assert saved.native_continuation is not None
        responses.respond(assistant_output('{"label":"final"}'))
        async with agent.session(state=saved) as restored:
            request = await next_request(restored.interactions(), tool)
            await request.respond("A")
            result = await restored.restored_turn.get_result()
            assert result.final_output.label == "final"
            assert result.turn.turn_id == saved.native_continuation.turn_id
            assert (await restored.save_state()).format_version == 4
        assert len(responses.requests) == 2
