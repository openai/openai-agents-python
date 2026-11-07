"""Receipted failures, exact stopped work and expired native interaction authority."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic import BaseModel, model_validator

from agents import (
    ActionRequested,
    Agent,
    ApprovalRequest,
    CapabilityError,
    ElicitationRequest,
    FunctionTool,
    HttpMCP,
    InteractionExpiredError,
    NativeError,
    OutputValidationError,
    PromptedOutput,
    SessionBusyError,
    StaleTurnError,
    TurnStoppedError,
    function_tool,
)
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_form_contract import SCHEMA, VALID
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_scoped_policies import AskingPeer, selected


@pytest.mark.parametrize("route", ["manual", "approval"])
async def test_environment_interruption_expires_issued_reply_before_session_close(
    responses: ResponsesFixture, tmp_path, monkeypatch, route: str
) -> None:
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_environment_leases_checklist import remote, trust

    issued = asyncio.Queue()
    interrupted, finish_close = asyncio.Event(), asyncio.Event()
    owner = None
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        aclosing(provider(responses)) as borrowed,
    ):
        trust(peer, tmp_path, monkeypatch)
        target = peer.root / "must-not-execute"
        if route == "manual":
            responses.respond(function_call("lookup", {"key": "A"}, "held"))
        else:
            responses.respond(
                function_call(
                    "exec_command",
                    {
                        "cmd": f"printf proof > {target}",
                        "sandbox_permissions": "require_escalated",
                        "justification": "approval before lease ends",
                    },
                    "held",
                )
            )
        agent = Agent(instructions="Read", provider=borrowed, tools=[manual])

        @asynccontextmanager
        async def source():
            nonlocal owner
            owner = asyncio.current_task()
            environment = remote(peer)
            if route == "approval":
                # Strict writes refuse escalation; this route needs a live legacy prompt.
                environment = replace(
                    environment, tools=["exec_command"], write_policy="legacy", writable_roots=None
                )
            yield environment

        async def borrower():
            async with agent.session(environment=source()) as session:
                await session.submit("go")
                async with aclosing(session.interactions()) as reader:
                    request = await anext(reader)
                await issued.put((session, request))
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # The source has already withdrawn the lease. Hold the
                    # borrower's exit before normal Session.close takes over.
                    interrupted.set()
                    await finish_close.wait()
                    raise

        task = asyncio.create_task(borrower())
        try:
            session, request = await issued.get()
            assert owner is not None
            owner.cancel()
            await interrupted.wait()
            assert not session._closing and not session._closed
            assert isinstance(request, ActionRequested if route == "manual" else ApprovalRequest)
            with pytest.raises(InteractionExpiredError):
                await request.respond("private-late-reply" if route == "manual" else "deny")
            assert not target.exists() and len(responses.requests) == 1
        finally:
            finish_close.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_waiter_and_joined_stop_are_different_with_sibling_work(
    responses: ResponsesFixture,
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    exited = threading.Event()
    effects = []

    @function_tool
    def wait_for_release() -> str:
        effects.append("entered")
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(15), "test did not release worker"
            effects.append("returned")
            return "late-private-result"
        finally:
            exited.set()

    borrowed = provider(responses)
    agent = Agent(instructions="Read.", tools=[wait_for_release], provider=borrowed)
    async with timeout(25), agent.session() as s, agent.session(tools=[]) as sibling:
        responses.respond(function_call("wait_for_release", {}, "slow"))
        turn = await s.submit("wait")
        waiting = asyncio.create_task(turn.get_result())
        stopping = None
        try:
            await started.wait()
            waiting.cancel("cancel only waiter")
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert not exited.is_set()
            with pytest.raises(SessionBusyError):
                await s.submit("must not steer")
            next_waiter = asyncio.create_task(turn.get_result())
            stopping = asyncio.create_task(turn.cancel())
            # Observe real acceptance before claiming anything about the stop.
            while not s._active.stopping:
                await asyncio.sleep(0)
            assert not stopping.done() and not next_waiter.done() and not exited.is_set()
            responses.respond(assistant_output("sibling"))
            assert (
                await (await sibling.submit("independent")).get_result()
            ).final_output == "sibling"
            release.set()
            await stopping
            with pytest.raises(TurnStoppedError) as ended:
                await next_waiter
            assert isinstance(ended.value, RuntimeError)
            assert not isinstance(ended.value, (NativeError, asyncio.CancelledError))
            assert exited.is_set() and effects == ["entered", "returned"]
            assert "late-private-result" not in json.dumps(responses.requests)
            stream = turn.events()
            events = [event async for event in stream]
            finished = [e for e in events if isinstance(e, TurnFinished)]
            assert len(finished) == 1 and finished[0].status == "cancelled"
            assert finished[0].error_type == "TurnStoppedError"
            # A settled cancel is still a no-op even while a new root runs.
            await turn.cancel()
        finally:
            release.set()
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
    await borrowed.aclose()


async def test_expired_manual_reply_differs_from_still_correctable_private_invalid_value(
    responses: ResponsesFixture,
) -> None:
    validated = []

    class Answer(BaseModel):
        value: int

        @model_validator(mode="before")
        @classmethod
        def check(cls, value: Any) -> Any:
            validated.append(True)
            return value

    manual = FunctionTool(
        name="lookup", description="Read", input_type=LookupArgs, output_type=Answer
    )
    borrowed = provider(responses)
    agent = Agent(instructions="Read.", tools=[manual], provider=borrowed)
    responses.respond(function_call("lookup", {"key": "A"}, "same-id"))
    responses.respond(assistant_output("done"))
    async with timeout(25), agent.session() as s:
        first = await s.submit("first")
        req = await next_request(s.interactions(), manual)
        secret = "credential-looking-private-field-that-must-not-escape"
        with pytest.raises(ValueError, match="failed validation") as invalid:
            await req.respond(cast(Any, {"value": secret}))
        assert not isinstance(invalid.value, CapabilityError)
        assert secret not in str(invalid.value) and invalid.value.__context__ is None
        await req.respond(Answer(value=3))
        assert (await first.get_result()).final_output == "done"
        responses.respond(function_call("lookup", {"key": "B"}, "same-id"))
        second = await s.submit("second")
        current = await next_request(s.interactions(), manual)
        count = len(validated)
        with pytest.raises(InteractionExpiredError) as expired:
            await req.respond(cast(Any, {"value": secret}))
        assert isinstance(expired.value, RuntimeError)
        assert secret not in str(expired.value) and len(validated) == count
        with pytest.raises(StaleTurnError):
            await first.steer("private stale steer")
        async with agent.session(tools=[]) as foreign:
            with pytest.raises(StaleTurnError):
                await foreign._wait(second)
        responses.respond(assistant_output("second"))
        await current.respond(Answer(value=8))
        assert (await second.get_result()).final_output == "second"
        assert "private stale steer" not in json.dumps(responses.requests)
    await borrowed.aclose()


@pytest.mark.parametrize("kind", ["manual", "approval", "form"])
async def test_reader_loss_expires_only_captured_reply_authority(
    responses: ResponsesFixture, kind: str
) -> None:
    secret = "private-expired-reader-value"
    async with timeout(25), AsyncExitStack() as stack:
        borrowed = provider(responses)
        stack.push_async_callback(borrowed.aclose)
        peer: Peer | AskingPeer | None = None
        if kind == "manual":
            tool = FunctionTool(
                name="lookup", description="Read", input_type=LookupArgs, output_type=str
            )
            event = function_call("lookup", {"key": "A"}, "pending-read")
            valid: Any = secret
            invalid: Any = {"private": secret}
            expected = ActionRequested
        elif kind == "approval":
            peer = Peer(names=("find-record",), annotations=None)
            url = await stack.enter_async_context(peer.serve())
            tool = HttpMCP(name="docs", url=url, tools=["find-record"])
            event = mcp_call("find_record", "mcp__docs", "pending-read")
            valid = "allow_once"
            invalid = secret
            expected = ApprovalRequest
        else:
            peer = AskingPeer(form_mode="form", schema=SCHEMA)
            url = await stack.enter_async_context(peer.serve())
            tool = HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")
            event = selected("form", "ordinary")
            valid = {**VALID, "email": "valid@example.invalid"}
            invalid = {**VALID, "display-name": secret}
            expected = ElicitationRequest
        responses.respond(event)
        responses.respond(assistant_output("should not continue after reader loss"))
        session = await stack.enter_async_context(
            Agent(
                instructions="Call the available tool.", provider=borrowed, tools=[tool]
            ).session()
        )
        turn = await session.submit("pending request")
        async with aclosing(session.interactions()) as cursor:
            request = await anext(cursor)
        assert isinstance(request, expected)
        assert request.turn_id == turn.turn_id and request.submission_id == turn.submission_id
        # Live bad input must not consume a grant or disclose the private input.
        with pytest.raises(ValueError) as live_invalid:
            await request.respond(cast(Any, invalid))
        assert not isinstance(live_invalid.value, InteractionExpiredError)
        assert secret not in str(live_invalid.value)
        pending = await session.pending_interactions()
        assert len(pending) == 1 and isinstance(pending[0], expected)
        assert pending[0].turn_id == request.turn_id
        assert pending[0].submission_id == request.submission_id
        if isinstance(request, ActionRequested):
            assert isinstance(pending[0], ActionRequested)
            assert pending[0].call_id == request.call_id
        assert len(responses.requests) == 1
        if kind == "approval":
            assert isinstance(peer, Peer) and not peer.calls
        if kind == "form":
            assert isinstance(peer, AskingPeer) and not peer.answers

        # As in the existing native reader-loss test, no event or grant is
        # forged. Submission, reply transport and teardown remain native.
        session._reader.cancel()
        await session._reader
        for answer in (invalid, valid):
            with pytest.raises(InteractionExpiredError) as expired:
                await request.respond(cast(Any, answer))
            assert secret not in str(expired.value)
        with pytest.raises(RuntimeError) as unavailable:
            await session.submit("unrelated new operation")
        assert not isinstance(unavailable.value, InteractionExpiredError)
        assert len(responses.requests) == 1
        assert secret not in json.dumps(responses.requests)
        if kind == "approval":
            assert isinstance(peer, Peer) and not peer.calls
        if kind == "form":
            assert isinstance(peer, AskingPeer)
            # Owned cleanup may cancel/decline; a revoked caller cannot accept.
            assert all(a.get("result", {}).get("action") != "accept" for a in peer.answers)
            assert secret not in json.dumps(peer.answers)


async def test_actual_native_not_submitted_receipt_is_only_for_the_failed_operation(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    borrowed = provider(responses)
    async with timeout(25), Agent(instructions="Read.", provider=borrowed).session() as s:
        native = s._native
        entered = asyncio.Event()
        release = asyncio.Event()
        receipts = []

        async def hold(event):
            if event["type"] == "response.created":
                entered.set()
                await release.wait()

        responses.before_event = hold
        responses.respond(assistant_output("native first"))

        class RealAdmissionRace:
            # Admit real native work between Python's idle check and native
            # admission. Do not manufacture a not_submitted receipt.
            def __getattr__(self, name):
                return getattr(native, name)

            async def _submit(self, request):
                receipts.append(json.loads(await native._submit(request)))
                assert receipts[-1]["status"] == "started"
                await entered.wait()
                result = await native._submit(request)
                receipts.append(json.loads(result))
                return result

        monkeypatch.setattr(s, "_native", RealAdmissionRace())
        try:
            submitted = await s.submit("specific failed admission")
            with pytest.raises(NativeError) as rejected:
                await submitted.get_result()
            assert receipts[1]["status"] == "not_submitted"
            assert rejected.value.effects == "not_started"
            assert len(responses.requests) == 1
            assert NativeError("unknown operation").effects == "unknown"
        finally:
            release.set()
    await borrowed.aclose()


async def test_provider_failure_after_real_callback_never_claims_earlier_effects_rolled_back(
    responses: ResponsesFixture,
) -> None:
    effects = []

    @function_tool
    def charge() -> str:
        effects.append("done")
        return "receipt"

    responses.respond(function_call("charge", {}, "charged"))
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "context_length_exceeded",
                    "message": "This model's maximum context length was exceeded.",
                    "type": "invalid_request_error",
                }
            },
        }
    )
    borrowed = provider(responses)
    async with (
        timeout(25),
        Agent(instructions="Charge.", tools=[charge], provider=borrowed).session() as s,
    ):
        turn = await s.submit("do it")
        with pytest.raises(NativeError) as error:
            await turn.get_result()
        assert error.value.effects == "unknown"
        assert effects == ["done"]
        assert error.value.category is not None
    await borrowed.aclose()


async def test_output_validation_and_callback_manual_capture_have_distinct_builtin_bases(
    responses: ResponsesFixture,
) -> None:
    borrowed = provider(responses)
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    async with (
        timeout(25),
        Agent(instructions="Read.", provider=borrowed, tools=[manual]).session() as s,
    ):
        responses.respond(assistant_output("invalid private text"))
        prompted = await s.submit("prompted", output_type=PromptedOutput(LookupArgs))
        with pytest.raises(OutputValidationError) as invalid:
            await prompted.get_result()
        assert invalid.value.raw_output == "invalid private text"
        assert isinstance(invalid.value, ValueError) and not isinstance(
            invalid.value, CapabilityError
        )
        responses.respond(function_call("lookup", {"key": "A"}, "native-schema"))

        async def choose(turn, *, step_index):
            return None

        structured = await s.submit("native", output_type=LookupArgs, before_step=choose)
        await next_request(s.interactions(), manual)
        # Native schemas can be captured, but portable state cannot restore
        # an operation callback. Refusal must preserve the live Turn.
        with pytest.raises(CapabilityError, match="before_step") as unsupported:
            await s.suspend()
        assert isinstance(unsupported.value, ValueError)
        assert (await s.checkpoint_status(kind="manual_wait")).status == "not_ready"
        await structured.cancel()
        with pytest.raises(TurnStoppedError):
            await structured.get_result()
    await borrowed.aclose()
