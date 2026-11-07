"""Live Core exec and patch approval qualification with explicit legacy permissions."""

import asyncio
import json
from contextlib import aclosing

import pytest

from agents import (
    Agent,
    ApprovalRequest,
    InteractionExpiredError,
    InteractionRequired,
    TurnStoppedError,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_executor_tools import declaration


class Observe:
    """Keep the actual, unmodified native event stream for emission assertions."""

    def __init__(self, original):
        self.original = original
        self.events = []

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def _next_session_event(self):
        result = await self.original._next_session_event()
        value = json.loads(result)
        if value is not None:
            self.events.append(value)
        return result


@pytest.mark.parametrize("route", ["escalate-exec", "outside-patch"])
@pytest.mark.parametrize("decision", ["allow_once", "deny", "cancel"])
async def test_owned_native_environment_prompt(responses, tmp_path, monkeypatch, route, decision):
    async with (
        timeout(25),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        aclosing(provider(responses)) as native,
    ):
        trust(peer, tmp_path, monkeypatch)
        target = peer.root / "owned-proof" if "exec" in route else tmp_path / "outside-proof"
        if route.endswith("exec"):
            responses.respond(
                function_call(
                    "exec_command",
                    {
                        "cmd": f"printf proof > {target}",
                        "sandbox_permissions": "require_escalated",
                        "justification": "qualification of an original native approval",
                    },
                    "same-model-call",
                )
            )
        else:
            responses.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": "same-model-call",
                        "input": f"*** Begin Patch\n*** Add File: {target}\n+proof\n*** End Patch",
                    },
                }
            )
        responses.respond(assistant_output("finished"))
        async with Agent(instructions="Use selected tools", provider=native).session(
            environment=declaration(peer, write_policy="legacy")
        ) as session:
            observe = Observe(session._native)
            session._native = observe
            turn = await session.submit("Use the original native builtin.")
            async with aclosing(session.interactions()) as reader:
                question = await anext(reader)
            assert isinstance(question, ApprovalRequest)
            assert question.call is None
            assert question.subagent_id is None and question.turn_id == turn.turn_id
            assert question.submission_id == turn.submission_id
            # Descriptions embed native JSON argv/patches, including escaped
            # Windows path separators. Check the entire encoded path.
            encoded_target = json.dumps(str(target), ensure_ascii=False)[1:-1]
            assert encoded_target in question.description
            assert str(target) not in repr(question)
            assert encoded_target not in repr(question)
            assert not target.exists()
            assert len(responses.requests) == 1
            with pytest.raises(ValueError, match="Approval decision"):
                await question.respond("allow_always")
            assert await session.pending_interactions() == (question,)
            if decision == "cancel":
                await turn.cancel()
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            else:
                await question.respond(decision)
            with pytest.raises(InteractionExpiredError, match="expired|answered"):
                await question.respond("allow_once")
            with pytest.raises(InteractionExpiredError, match="expired|answered"):
                await question.respond("allow_always")
            error, output = None, None
            try:
                output = (await turn.get_result()).final_output
            except BaseException as caught:
                error = type(caught).__name__, str(caught)
            kind = (
                "exec_approval_request"
                if route.endswith("exec")
                else "apply_patch_approval_request"
            )
            events = [e["msg"] for e in observe.events if e["msg"]["type"] == kind]
            assert len(events) == 1
            assert events[0]["call_id"] == "same-model-call"
            assert events[0]["turn_id"] == turn.turn_id
            if decision == "allow_once":
                assert error is None and output == "finished"
                assert target.read_text() == ("proof" if route.endswith("exec") else "proof\n")
                assert len(responses.requests) == 2
            if decision != "allow_once":
                assert not target.exists()
            if decision == "deny":
                assert output == "finished" and len(responses.requests) == 2
        # Keep the original Session alive: revocation cannot depend on whether
        # the weak reference has happened to be collected.
        with pytest.raises(InteractionExpiredError, match="expired|answered"):
            await question.respond("allow_once")


@pytest.mark.parametrize("route", ["exec", "patch"])
@pytest.mark.parametrize("interactive", [True, False], ids=["handler", "headless"])
async def test_run_only_answers_live_environment_approval_with_handler(
    responses, tmp_path, monkeypatch, route, interactive
):
    async with (
        timeout(25),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        aclosing(provider(responses)) as native,
    ):
        trust(peer, tmp_path, monkeypatch)
        target = tmp_path / "readable-proof"
        if route == "exec":
            responses.respond(
                function_call(
                    "exec_command",
                    {
                        "cmd": f"printf proof > {target}",
                        "sandbox_permissions": "require_escalated",
                        "justification": "one application-owned command",
                    },
                    "reused-model-id",
                )
            )
        else:
            responses.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": "reused-model-id",
                        "input": f"*** Begin Patch\n*** Add File: {target}\n+proof\n*** End Patch",
                    },
                }
            )
        responses.respond(assistant_output("finished"))
        delivered = []

        def answer(question):
            assert isinstance(question, ApprovalRequest)
            assert not target.exists()
            delivered.append(question)
            return question.reply("allow_once")

        agent = Agent(instructions="Use selected tools", provider=native)
        if interactive:
            result = await agent.run(
                "Use the selected builtin",
                environment=declaration(peer, write_policy="legacy"),
                on_interaction=answer,
            )
            assert result.final_output == "finished"
            assert len(delivered) == 1
            with pytest.raises(InteractionExpiredError, match="expired|answered"):
                await delivered[0].respond("deny")
            assert target.read_text() == ("proof" if route == "exec" else "proof\n")
        else:
            with pytest.raises(InteractionRequired, match="on_interaction"):
                await agent.run(
                    "Use the selected builtin", environment=declaration(peer, write_policy="legacy")
                )
            assert not target.exists()
            assert delivered == []


@pytest.mark.parametrize("route", ["exec", "patch"])
async def test_root_approval_enqueue_joins_before_successor(
    responses, tmp_path, monkeypatch, route
):
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        aclosing(provider(responses)) as native,
    ):
        trust(peer, tmp_path, monkeypatch)
        first = peer.root / "first-denied" if route == "exec" else tmp_path / "first-denied"
        second = peer.root / "second-denied" if route == "exec" else tmp_path / "second-denied"

        def prompt(path):
            if route == "exec":
                return function_call(
                    "exec_command",
                    {
                        "cmd": f"printf controlled > {path}",
                        "sandbox_permissions": "require_escalated",
                        "justification": "one explicit native approval",
                    },
                    "repeated-call",
                )
            return {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "repeated-call",
                    "input": f"*** Begin Patch\n*** Add File: {path}\n+controlled\n*** End Patch",
                },
            }

        responses.respond(prompt(first))
        async with Agent(instructions="Use selected tools", provider=native).session(
            environment=declaration(peer, write_policy="legacy")
        ) as session:
            reached = asyncio.Event()
            cancelling = asyncio.Event()
            released = asyncio.Event()
            original = session._native

            class HeldEnqueue:
                def __getattr__(self, name):
                    return getattr(original, name)

                async def call(self, operation, *args):
                    if operation == route + "_approval" and not reached.is_set():
                        reached.set()
                        try:
                            await released.wait()
                        except asyncio.CancelledError:
                            # Preserve actual retirement ownership until this
                            # controlled, already running operation releases.
                            cancelling.set()
                            await released.wait()
                            raise
                    return await original.call(operation, *args)

            session._native = HeldEnqueue()
            old = await session.submit("First root turn")
            async with aclosing(session.interactions()) as reader:
                question = await anext(reader)
            assert isinstance(question, ApprovalRequest)
            pending = asyncio.create_task(question.respond("allow_once"))
            stopping = None
            notice = None
            try:
                await reached.wait()
                assert not first.exists()
                stopping = asyncio.create_task(old.cancel())
                notice = asyncio.create_task(cancelling.wait())
                done, _ = await asyncio.wait(
                    (stopping, notice), return_when=asyncio.FIRST_COMPLETED
                )
                assert notice in done, "Turn.cancel returned without retiring its consumed approval"
                assert not stopping.done()
                with pytest.raises(RuntimeError, match="Session is finishing root interruption"):
                    await session.submit("Cannot reuse the occupied owner")
            finally:
                released.set()
                if notice is not None:
                    notice.cancel()
                    await asyncio.gather(notice, return_exceptions=True)
                if stopping is not None:
                    await stopping
                result = await asyncio.gather(pending, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            with pytest.raises(TurnStoppedError):
                await old.get_result()
            assert not first.exists()
            responses.respond(prompt(second))
            responses.respond(assistant_output("new owner continued"))
            current = await session.submit("Successor with the same model call ID")
            async with aclosing(session.interactions()) as reader:
                fresh = await anext(reader)
            assert isinstance(fresh, ApprovalRequest)
            assert fresh.turn_id == current.turn_id != old.turn_id
            assert fresh.submission_id == current.submission_id != old.submission_id
            assert not first.exists() and not second.exists()
            with pytest.raises(InteractionExpiredError, match="expired|answered"):
                await question.respond("allow_once")
            assert await session.pending_interactions() == (fresh,)
            await fresh.respond("deny")
            assert (await current.get_result()).final_output == "new owner continued"
            assert not first.exists() and not second.exists()


async def test_cancelled_responder_preserves_consumed_live_root_approval(
    responses, tmp_path, monkeypatch
):
    async with (
        timeout(18),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        aclosing(provider(responses)) as native,
    ):
        trust(peer, tmp_path, monkeypatch)
        target = peer.root / "single-approved-effect"
        responses.respond(
            function_call(
                "exec_command",
                {
                    "cmd": f"printf proof >> {target}",
                    "sandbox_permissions": "require_escalated",
                    "justification": "one live root approval",
                },
                "owned-native-call",
            )
        )
        responses.respond(assistant_output("finished"))
        async with Agent(instructions="Use selected tools", provider=native).session(
            environment=declaration(peer, write_policy="legacy")
        ) as session:
            observe = Observe(session._native)
            session._native = observe
            turn = await session.submit("Run the original builtin")
            async with aclosing(session.interactions()) as reader:
                question = await anext(reader)
            assert isinstance(question, ApprovalRequest)
            assert await session.pending_interactions() == (question,)
            responder = asyncio.create_task(question.respond("allow_once"))
            # The responder consumes the grant and queues the private submit;
            # this task then resumes before that submit gets its first step.
            await asyncio.sleep(0)
            assert await session.pending_interactions() == ()
            assert responder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await responder
            assert (await asyncio.wait_for(turn.get_result(), 4)).final_output == "finished"
            assert target.read_text() == "proof"
            approvals = [
                e["msg"] for e in observe.events if e["msg"]["type"] == "exec_approval_request"
            ]
            assert len(approvals) == 1
            assert approvals[0]["turn_id"] == turn.turn_id
            assert approvals[0]["call_id"] == "owned-native-call"
            with pytest.raises(InteractionExpiredError, match="expired|answered"):
                await question.respond("allow_once")
            responses.respond(assistant_output("successor"))
            assert (
                await (await session.submit("Run a new root")).get_result()
            ).final_output == "successor"
            assert target.read_text() == "proof"
