"""Captured Agent profiles own Decisions without opening a native runtime."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, field_validator

from _agents_codex._agent_profiles import _capture_agent_graph
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

CHOICES = ("root", "target", "opening", "per-call", "replacement")


@pytest.fixture(autouse=True)
def block_native_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "agents._native", None)


class Provider:
    def __init__(self, name):
        self.name = name
        self.requests = []

    async def get_decision(self, request):
        self.requests.append(request)
        return DecisionResponse(value={"response": self.name}, model=self.name)


def capture(root):
    opening = _capture_opening_agent(
        root,
        provider=None,
        decisions=root.decisions,
        model=None,
        model_settings=None,
        tool_selection=None,
    )
    return _capture_agent_graph(root, root_declaration=opening)


def callback_session(root, *, opening_override=None):
    profiles = capture(root)
    runtime, _ = _capture_profile_runtimes(profiles, _capture_tools(0, (), None), "Root", None)
    session = object.__new__(Session)
    session._profiles = runtime
    session._opening_agent = profiles[0].declaration
    session._decisions_provider = profiles[0].declaration.decisions
    session._decisions_override = opening_override
    session._profile_current = ("agent_0", 0)
    recorded = []

    async def begin(state, message):
        async def record(response):
            recorded.append((message["profile_id"], response.model))

        return record

    session._begin_decision_response = begin
    return session, recorded


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
    ("target_binding", "opening_override", "per_call", "expected"),
    [
        (False, False, False, ("root", "root")),
        (True, False, False, ("root", "target")),
        (True, True, False, ("opening", "opening")),
        (True, True, True, ("per-call", "per-call")),
    ],
)
async def test_callback_provider_precedence_follows_captured_source(
    target_binding, opening_override, per_call, expected
):
    providers = {name: Provider(name) for name in CHOICES}
    target = Agent(
        instructions="Target",
        name="Target",
        decisions=providers["target"] if target_binding else None,
    )
    root = Agent(instructions="Root", decisions=providers["root"], handoffs=[target])
    session, recorded = callback_session(
        root, opening_override=providers["opening"] if opening_override else None
    )
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
                object(), message(profile, epoch), check, lambda: True, completion("candidate")
            )
        )
    assert tuple(selected) == expected
    assert recorded == [("agent_0", expected[0]), ("agent_1", expected[1])]
    assert not providers["replacement"].requests


async def test_approval_context_cannot_borrow_a_later_profile_callback():
    root_provider, target_provider = Provider("root"), Provider("target")
    target = Agent(instructions="Target", name="Target", decisions=target_provider)
    root = Agent(instructions="Root", handoffs=[target], decisions=root_provider)
    session, recorded = callback_session(root)
    child = ChildInvocation("session", "child", "instance", "child-turn")
    earlier = ChildStepContext(context=None, child=child, step_index=0)
    later = ChildStepContext(context=None, child=child, step_index=1)

    async def first(ctx):
        return await ctx.decide("Approve?", input="evidence", choices=CHOICES)

    assert (
        await session._call_decision_callback(
            object(), message("agent_1", 1), first, lambda: True, earlier
        )
        == "target"
    )

    async def second(ctx):
        with pytest.raises(RuntimeError, match="exact live callback"):
            await earlier.decide("Expired?", input="evidence", choices=CHOICES)
        return await ctx.decide("Approve?", input="evidence", choices=CHOICES)

    assert (
        await session._call_decision_callback(
            object(), message("agent_0", 0), second, lambda: True, later
        )
        == "root"
    )
    with pytest.raises(RuntimeError, match="exact live callback"):
        await later.decide("Expired?", input="evidence", choices=CHOICES)
    assert recorded == [("agent_1", "target"), ("agent_0", "root")]


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
    session, recorded = callback_session(root)
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
        assert recorded == [("agent_1", "target")]
        assert not root_provider.requests
        return
    assert await work is None
    assert [ctx.text for ctx in candidates] == ["invalid JSON", '{"label":"accepted"}']
    assert {ctx.turn_id for ctx in candidates} == {"original-turn"}
    assert state.accepted_output == (Report(label="accepted"),)
    assert recorded == [("agent_1", "target"), ("agent_1", "target")]
    assert not root_provider.requests


async def test_unknown_profile_fails_before_dispatch_instead_of_using_current_profile():
    provider = Provider("root")
    session, recorded = callback_session(Agent(instructions="Root", decisions=provider))

    async def check(ctx):
        return await ctx.decide("Allowed?", input="evidence", choices=CHOICES)

    with pytest.raises(NativeError, match="unknown Agent profile"):
        await session._call_decision_callback(
            SimpleNamespace(), message("foreign", 0), check, lambda: True, completion("candidate")
        )
    assert not provider.requests
    assert not recorded


@pytest.mark.parametrize(
    ("target_binding", "opening_override", "expected"),
    [(True, True, "opening"), (True, False, "target"), (False, False, "root")],
)
async def test_deferred_repeated_restore_preserves_opening_override_and_target_precedence(
    monkeypatch, target_binding, opening_override, expected
):
    from _agents_codex import session as implementation
    from _agents_codex.experimental import deferred

    providers = {name: Provider(name) for name in CHOICES}
    target = Agent(
        instructions="Target",
        name="Target",
        decisions=providers["target"] if target_binding else None,
    )
    root = Agent(instructions="Root", decisions=providers["root"], handoffs=[target])
    override = providers["opening"] if opening_override else None
    opening = _capture_opening_agent(
        root,
        provider=None,
        decisions=override or providers["root"],
        model=None,
        model_settings=None,
        tool_selection=None,
    )
    session, _ = callback_session(opening, opening_override=override)
    request = SimpleNamespace(call_id="manual-call")
    jobs = deferred.DeferredTools(journal=object(), tenant="fixture", run_id="restore")

    def prepare_live(session):
        session._agent = root
        session._catalog = session._profiles["agent_0"].catalog
        session._bound_tools = session._catalog.tools
        session._instructions = session._opening_agent.instructions
        session._active = SimpleNamespace(output=_prepare_spec(str))
        session._model_api_key = None
        session._max_function_concurrency = None
        session._context = None
        session._manual_request = lambda *args, **kwargs: request

    async def prepare(*args, **kwargs):
        return SimpleNamespace(native_continuation=None)

    async def advance(*args, **kwargs):
        return "pending"

    class OpeningCaptured(Exception):
        def __init__(self, provider, override):
            self.provider = provider
            self.override = override

    capture_decisions = implementation._capture_decisions

    def stop_before_native(default, override):
        raise OpeningCaptured(capture_decisions(default, override), override)

    @asynccontextmanager
    async def restored(selection, context, timeout):
        # Exercise the real selected-opening branch, stopping at the existing
        # provider-capture seam before native resources or checkpoint parsing.
        selection.owner = asyncio.current_task()
        token = implementation._restore_declaration.set(selection)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(implementation, "_capture_decisions", stop_before_native)
                with pytest.raises(OpeningCaptured) as captured:
                    await Session._open(selection.agent, state=selection.state)
        finally:
            selection.owner = None
            implementation._restore_declaration.reset(token)
        reopened, _ = callback_session(
            selection.declaration, opening_override=captured.value.override
        )
        reopened._decisions_provider = captured.value.provider
        prepare_live(reopened)
        yield reopened, lambda: True

    monkeypatch.setattr(jobs, "prepare", prepare)
    monkeypatch.setattr(jobs, "advance", advance)
    monkeypatch.setattr(deferred, "_restored_session", restored)
    prepare_live(session)

    async def check(ctx):
        return await ctx.decide("Restored target?", input="evidence", choices=CHOICES)

    selected = []
    async with jobs.start(session, request) as (first, _):
        selected.append(
            await first._call_decision_callback(
                object(), message("agent_1", 1), check, lambda: True, completion("first restore")
            )
        )
        async with jobs.start(first, request) as (second, _):
            selected.append(
                await second._call_decision_callback(
                    object(),
                    message("agent_1", 2),
                    check,
                    lambda: True,
                    completion("second restore"),
                )
            )
    assert selected == [expected, expected]


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
    session, recorded = callback_session(root)
    session._id = "session"
    session._context = {"tenant": "fixture"}
    session._owned_provider = provider
    session._model_api_key = "synthetic-profile-key"
    session._codex_login_binding = None
    session._repair_session = None
    session._repair_turns = []
    session._repair_items = []
    session._repair_receipts_incomplete = False
    session._observations = SimpleNamespace()
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

    class Repair:
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

        async def _start(self, input, output, *, budget):
            correction_inputs.append(json.loads(input[0]["text"]))
            return Attempt()

        async def aclose(self):
            self._closed = True

    async def open_repair(cls, declaration, **kwargs):
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
    assert recorded == [("agent_1", "target"), ("agent_1", "target")]
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
