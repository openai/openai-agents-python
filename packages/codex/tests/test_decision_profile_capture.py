"""Captured Agent profiles own Decisions without opening a native runtime."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, field_validator

from _agents_codex._agent_profiles import _capture_agent_graph
from _agents_codex.observation import _ObservationBuffer
from _agents_codex.schema import _prepare_spec
from _agents_codex.session import (
    Session,
    _capture_opening_agent,
    _capture_profile_runtimes,
    _capture_tools,
    _Submission,
)
from agents import (
    Agent,
    ChildInvocation,
    ChildStepContext,
    CompletionContext,
    DecisionResponse,
    NativeError,
    NativeProvider,
    PromptedOutput,
)

CHOICES = ("root", "target", "per-call", "replacement")


@pytest.fixture(autouse=True)
def block_native_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "agents._native", None)


class Provider:
    def __init__(self, name):
        self.name = name
        self.requests = []

    async def get_decision(self, request):
        self.requests.append(request)
        return DecisionResponse(value=self.name, model=self.name)


def capture(root):
    opening = _capture_opening_agent(
        root,
        provider=None,
        model=None,
        model_settings=None,
        tool_selection=None,
    )
    return _capture_agent_graph(root, root_declaration=opening)


def callback_session(root):
    profiles = capture(root)
    runtime, _ = _capture_profile_runtimes(profiles, _capture_tools(0, (), None), "Root", None)
    session = object.__new__(Session)
    session._profiles = runtime
    session._closing = False
    session._output_guardrails = ()
    session._uses_siwc = False
    session._raw_usage_started = False
    session._include_raw_provider_events = False
    session._opening_agent = profiles[0].declaration
    session._decisions_provider = profiles[0].declaration.decisions
    session._profile_current = ("agent_0", 0)
    session._repair_transport_settings = None
    return session


def message(profile, epoch):
    return {"profile_id": profile, "profile_epoch": epoch, "step_id": 7}


def completion(text, *, model="generation"):
    return CompletionContext(
        context={"tenant": "fixture"},
        text=text,
        session_id="session",
        turn_id="original-turn",
        subagent_id=None,
        model=model,
        continuation_count=0,
    )


async def test_graph_copies_keep_each_provider_after_original_agents_change():
    root_provider, target_provider, replacement = [
        Provider(name) for name in ("root", "target", "replacement")
    ]
    target = Agent(instructions="Target", name="Target", decisions=target_provider)
    root = Agent(instructions="Root", handoffs=[target], decisions=root_provider)
    profiles = capture(root)
    # Deliberately change frozen declarations after capture, as other capture
    # regressions do, to detect accidental reads from the original graph.
    object.__setattr__(root, "decisions", replacement)
    object.__setattr__(target, "decisions", replacement)
    assert [profile.agent for profile in profiles] == [root, target]
    assert await root.decide("Current declaration", input="evidence", choices=CHOICES) == (
        "replacement"
    )
    assert [
        await profile.declaration.decide("Captured declaration", input="evidence", choices=CHOICES)
        for profile in profiles
    ] == ["root", "target"]


@pytest.mark.parametrize(
    ("target_binding", "per_call", "expected"),
    [
        (False, False, ("root", "root")),
        (True, False, ("root", "target")),
        (True, True, ("per-call", "per-call")),
    ],
)
async def test_callback_provider_precedence_follows_captured_source(
    target_binding, per_call, expected
):
    providers = {name: Provider(name) for name in CHOICES}
    target = Agent(
        instructions="Target",
        name="Target",
        decisions=providers["target"] if target_binding else None,
    )
    root = Agent(instructions="Root", decisions=providers["root"], handoffs=[target])
    session = callback_session(root)
    object.__setattr__(root, "decisions", providers["replacement"])
    object.__setattr__(target, "decisions", providers["replacement"])
    selected = []

    async def check(ctx):
        return await ctx.decide(
            "Which provider owns this callback?",
            input="evidence",
            choices=CHOICES,
            provider=providers["per-call"] if per_call else None,
        )

    for profile, epoch in (("agent_0", 0), ("agent_1", 1)):
        # Current state deliberately names the opposite profile. The admitted
        # source receipt must continue to own this callback.
        session._profile_current = ("agent_1" if profile == "agent_0" else "agent_0", 2)
        selected.append(
            await session._call_decision_callback(
                SimpleNamespace(stopping=False, failure=asyncio.get_running_loop().create_future()),
                message(profile, epoch),
                check,
                lambda: True,
                completion("candidate"),
            )
        )
    assert tuple(selected) == expected
    assert not providers["replacement"].requests


async def test_approval_context_cannot_borrow_a_later_profile_callback():
    root_provider, target_provider = Provider("root"), Provider("target")
    target = Agent(instructions="Target", name="Target", decisions=target_provider)
    root = Agent(instructions="Root", handoffs=[target], decisions=root_provider)
    session = callback_session(root)
    child = ChildInvocation("session", "child", "instance", "child-turn")
    earlier = ChildStepContext(context=None, child=child, step_index=0)
    later = ChildStepContext(context=None, child=child, step_index=1)

    async def first(ctx):
        return await ctx.decide("Approve?", input="evidence", choices=CHOICES)

    assert (
        await session._call_decision_callback(
            SimpleNamespace(stopping=False, failure=asyncio.get_running_loop().create_future()),
            message("agent_1", 1),
            first,
            lambda: True,
            earlier,
        )
        == "target"
    )

    async def second(ctx):
        with pytest.raises(RuntimeError, match="exact live callback"):
            await earlier.decide("Expired?", input="evidence", choices=CHOICES)
        return await ctx.decide("Approve?", input="evidence", choices=CHOICES)

    assert (
        await session._call_decision_callback(
            SimpleNamespace(stopping=False, failure=asyncio.get_running_loop().create_future()),
            message("agent_0", 0),
            second,
            lambda: True,
            later,
        )
        == "root"
    )
    with pytest.raises(RuntimeError, match="exact live callback"):
        await later.decide("Expired?", input="evidence", choices=CHOICES)


@pytest.mark.parametrize("revoke", [False, True])
async def test_completion_repair_rechecks_original_profile_and_expires_previous_context(revoke):
    class Report(BaseModel):
        label: str

    root_provider, target_provider = Provider("root"), Provider("target")
    target = Agent(
        instructions="Target",
        name="Target",
        decisions=target_provider,
        output_type=PromptedOutput(Report, max_retries=1),
    )
    root = Agent(
        instructions="Root",
        handoffs=[target],
        decisions=root_provider,
        output_type=PromptedOutput(Report, max_retries=1),
    )
    session = callback_session(root)
    state = _Submission(_prepare_spec(target.output_type))
    candidates = []
    live = True

    async def check(ctx):
        if candidates:
            with pytest.raises(RuntimeError, match="exact live callback"):
                await candidates[0].decide("Expired?", input="evidence", choices=CHOICES)
        candidates.append(ctx)
        assert ctx.text is not None
        assert await ctx.decide("Accept candidate?", input=ctx.text, choices=CHOICES) == "target"
        return None

    async def correct(state, text, **kwargs):
        nonlocal live
        # Replace correction I/O only. The real completion-policy loop must
        # inspect both candidates and keep its original captured source.
        session._profile_current = ("agent_0", 2)
        object.__setattr__(target, "decisions", root_provider)
        state.repair_attempt_count += 1
        live = not revoke
        return '{"label":"accepted"}'

    session._repair_output_once = correct
    work = session._call_completion_callback(
        state, message("agent_1", 1), check, lambda: live, completion("invalid JSON")
    )
    if revoke:
        with pytest.raises(asyncio.CancelledError):
            await work
        assert len(candidates) == 1
        assert state.accepted_output is None
        assert not root_provider.requests
        return
    assert await work is None
    assert [ctx.text for ctx in candidates] == ["invalid JSON", '{"label":"accepted"}']
    assert {ctx.turn_id for ctx in candidates} == {"original-turn"}
    assert state.accepted_output == (Report(label="accepted"),)
    assert not root_provider.requests


async def test_unknown_profile_fails_before_dispatch_instead_of_using_current_profile():
    provider = Provider("root")
    session = callback_session(Agent(instructions="Root", decisions=provider))

    async def check(ctx):
        return await ctx.decide("Allowed?", input="evidence", choices=CHOICES)

    with pytest.raises(NativeError, match="unknown Agent profile"):
        await session._call_decision_callback(
            SimpleNamespace(), message("foreign", 0), check, lambda: True, completion("candidate")
        )
    assert not provider.requests


@pytest.mark.parametrize("successor_profile", ["agent_0", "agent_1"])
async def test_real_repair_keeps_original_epoch_settings_hooks_and_output_contract(
    monkeypatch, successor_profile
):
    class RootReport(BaseModel):
        model_config = ConfigDict(title="ProfileReport")
        label: str

        @field_validator("label")
        @classmethod
        def root_label(cls, value):
            if value != "root-only":
                raise ValueError("Root policy requires root-only")
            return value

    class TargetReport(BaseModel):
        model_config = ConfigDict(title="ProfileReport")
        label: str

        @field_validator("label")
        @classmethod
        def target_label(cls, value):
            if value != "target-only":
                raise ValueError("Target policy requires target-only")
            return value

    async def root_transform(ctx):
        return None

    async def target_transform(ctx):
        return None

    async def root_compact(ctx):
        return None

    async def target_compact(ctx):
        return None

    provider = NativeProvider(api_key="synthetic-profile-key")
    root_provider, target_provider = Provider("root"), Provider("target")
    target = Agent(
        instructions="Target",
        name="Target",
        provider=provider,
        decisions=target_provider,
        output_type=PromptedOutput(TargetReport, max_retries=2),
        transform_context=target_transform,
        before_compact=target_compact,
    )
    root = Agent(
        instructions="Root",
        handoffs=[target],
        provider=provider,
        decisions=root_provider,
        output_type=PromptedOutput(RootReport, max_retries=0),
        transform_context=root_transform,
        before_compact=root_compact,
    )
    session = callback_session(root)
    session._id = "session"
    session._context = {"tenant": "fixture"}
    session._owned_provider = provider
    session._model_api_key = "synthetic-profile-key"
    session._codex_login_binding = None
    session._repair_session = None
    session._repair_turns = []
    session._repair_items = []
    session._repair_receipts_incomplete = False
    session._include_raw_provider_events = True
    session._observations = _ObservationBuffer("session", include_model_output_items=True)
    state = _Submission(_prepare_spec(root.output_type))
    state.handle = SimpleNamespace(turn_id="original-turn", submission_id="submission")
    settings = {
        "model": "accepted-original",
        "reasoning_effort": "high",
        "reasoning_summary": "concise",
        "service_tier": "priority",
    }

    def history_row(profile, epoch, model):
        return {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_id": "session",
                "thread_settings": model,
            },
            "metadata": {
                "sdk_agent_profile": {"profile_id": profile, "epoch": epoch, "model": model}
            },
        }

    async def history(_):
        await asyncio.sleep(0)
        session._profile_current = (successor_profile, 2)
        # A later source may update the shared submission's output view while
        # repair waits. The original completion must keep its captured parser.
        state.output = _prepare_spec(root.output_type)
        return json.dumps(
            [
                history_row("agent_1", 1, settings),
                history_row(
                    successor_profile,
                    2,
                    {
                        "model": "accepted-successor",
                        "reasoning_effort": "low",
                        "reasoning_summary": "detailed",
                        "service_tier": "default",
                    },
                ),
            ]
        )

    session._native = SimpleNamespace(read_thread_history=history)
    opened = []
    correction_inputs = []

    class Attempt:
        async def get_result(self):
            return SimpleNamespace(final_output='{"label":"target-only"}')

        async def cancel(self):
            return None

    class Repair(Session):
        id = "repair-session"
        _broken = False
        _closed = False
        _environment_lease = None

        def __init__(self):
            self._observations = SimpleNamespace()
            self._native = SimpleNamespace(read_thread_history=self.history)

        async def history(self, _):
            return "[]"

        def _check_open(self):
            return None

        async def _start(
            self,
            input,
            output,
            *,
            budget,
            run_usage,
            model_override=None,
            model_settings_override=None,
            retry_policy_override=None,
            raise_on_unknown_tool=False,
        ):
            assert model_override is None
            assert model_settings_override is None
            assert retry_policy_override is state.retry_policy_override
            assert raise_on_unknown_tool is state.raise_on_unknown_tool
            assert run_usage is state.run_usage
            correction_inputs.append(json.loads(input[0]["text"]))
            return Attempt()

        async def aclose(self):
            self._closed = True

    async def open_repair(cls, declaration, **kwargs):
        assert kwargs["include_model_output_items"] is True
        assert kwargs["include_raw_provider_events"] is True
        opened.append(declaration)
        return Repair()

    monkeypatch.setattr(Session, "_open", classmethod(open_repair))
    candidates = []

    async def check(ctx):
        candidates.append(ctx.text)
        assert ctx.model == "physical-original"
        assert await ctx.decide("Accept?", input=ctx.text, choices=CHOICES) == "target"

    receipt = {**message("agent_1", 1), "model": "physical-original"}
    assert (
        await session._call_completion_callback(
            state,
            receipt,
            check,
            lambda: True,
            completion("invalid JSON", model="physical-original"),
        )
        is None
    )
    assert candidates == ["invalid JSON", '{"label":"target-only"}']
    assert state.accepted_output == (TargetReport(label="target-only"),)
    assert state.repair_attempt_count == 1
    assert not root_provider.requests
    (declaration,) = opened
    assert declaration.model == "physical-original"
    assert dict(declaration.model_settings) == {k: v for k, v in settings.items() if k != "model"}
    assert declaration.provider is provider
    assert declaration.transform_context is target_transform
    assert declaration.before_compact is target_compact
    assert declaration.before_complete is None and declaration.decisions is None
    assert correction_inputs == [
        {"schema": _prepare_spec(target.output_type).schema, "invalid_output": "invalid JSON"}
    ]
    assert session._repair_session is None
