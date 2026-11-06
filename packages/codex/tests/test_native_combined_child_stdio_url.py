"""Ordinary URL completion retains its admission-owned stdio lifetime."""

import asyncio
import json
import os
import sys
from contextlib import aclosing
from dataclasses import replace

import pytest

from agents import ElicitationRequest, InteractionExpiredError, NativeError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import agent_for, child_peer, collaboration, trust
from tests.test_native_executor_policies import OutputEvents
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import records
from tests.test_native_owned_child_prompts import cancel_child, interactive
from tests.test_native_owned_child_stdio import (
    external,
    install_stdio_program,
    matching_turn,
    next_form,
    owned_stdio,
    process_gone,
    wait_record,
)


class HeldCompletionEvents(OutputEvents):
    """Delay one actual native status without changing its contents or source."""

    def __init__(self, native):
        super().__init__(native)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.request_id = None

    async def _next_session_event(self):
        event = await super()._next_session_event()
        if event is not None:
            message = (json.loads(event) or {}).get("msg", {})
            if (
                message.get("type") == "elicitation_status"
                and message.get("completed") is True
                and message.get("request_id") == self.request_id
            ):
                self.entered.set()
                await self.release.wait()
        return event


class HeldReceiptEvents(HeldCompletionEvents):
    """Hold publication after reading a real completed native receipt."""

    def __init__(self, native):
        super().__init__(native)
        self.receipt_entered = asyncio.Event()
        self.receipt_release = asyncio.Event()

    async def _child_url_completion_status(self, question_id):
        status = await self.native._child_url_completion_status(question_id)
        if status is True:
            self.receipt_entered.set()
            await self.receipt_release.wait()
        return status


@pytest.mark.parametrize("early", [False, True])
async def test_owned_stdio_child_url_completes_on_original_native_owner(
    tmp_path, monkeypatch, early
):
    release = tmp_path / "complete"
    release.mkdir()
    async with (
        timeout(50),
        child_peer(("default",)) as model,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        install_stdio_program(model)
        flags = ("--url-early",) if early else ()
        app = interactive(agent_for(model, executor))
        app = replace(
            app,
            tools=[
                *app.tools,
                owned_stdio(tmp_path, "--url-flow", "--completion-release", str(release), *flags),
            ],
        )
        async with app.session(environment=declaration(executor)) as session:
            captured = HeldCompletionEvents(session._native)
            try:
                if early:
                    session._native = captured
                await (await session.submit("Start the original child")).get_result()
                question = await next_form(session)
                assert isinstance(question, ElicitationRequest)
                child = await matching_turn(session, question)
                assert question.url == "https://example.invalid/owned-child-never-open"
                assert question.elicitation_id == "owned-child-url-A"
                captured.request_id = question._message["id"]
                await question.accept_url()
                accepted = await wait_record(tmp_path / "stdio", "url_accepted")
                waiter = asyncio.create_task(question.wait_url_completion(timeout=8))
                try:
                    if not early:
                        await asyncio.sleep(0)
                        assert not waiter.done()
                        (release / str(accepted["pid"])).touch()
                    else:
                        await captured.entered.wait()
                    await waiter
                    # The immutable native receipt settles despite delayed status delivery.
                    captured.release.set()
                    assert (await child.get_result()).final_output == "alpha complete"
                    with pytest.raises(InteractionExpiredError):
                        await question.accept_url()
                    assert len(external(tmp_path)) == 1
                    if os.name != "nt":
                        process_gone(accepted["pid"])
                finally:
                    captured.release.set()
                    (release / str(accepted["pid"])).touch()
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)

            finally:
                captured.release.set()


async def test_session_close_preserves_completed_child_url_receipt_before_status_delivery(
    tmp_path, monkeypatch
):
    async with (
        timeout(60),
        child_peer(("default",)) as model,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        install_stdio_program(model)
        app = interactive(agent_for(model, executor))
        app = replace(app, tools=[*app.tools, owned_stdio(tmp_path, "--url-flow", "--url-early")])
        scope = app.session(environment=declaration(executor))
        session = await scope.__aenter__()
        captured = HeldReceiptEvents(session._native)
        session._native = captured
        waiter = closing = None
        try:
            await (await session.submit("Start original child")).get_result()
            question = await next_form(session)
            captured.request_id = question._message["id"]
            waiter = asyncio.create_task(question.wait_url_completion(timeout=20))
            await asyncio.sleep(0)
            await question.accept_url()
            await captured.receipt_entered.wait()
            assert not waiter.done()
            closing = asyncio.create_task(scope.__aexit__(None, None, None))
            await asyncio.sleep(0)
            assert not closing.done()
            captured.receipt_release.set()
            await waiter
            captured.release.set()
            await closing
            with pytest.raises(InteractionExpiredError):
                await question.accept_url()
        finally:
            captured.receipt_release.set()
            captured.release.set()
            if waiter is not None:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            if closing is None:
                await scope.__aexit__(*sys.exc_info())
            else:
                await closing


@pytest.mark.skipif(os.name == "nt", reason="Controlled SIGTERM retirement uses POSIX signals")
async def test_consumed_child_url_consent_does_not_outlive_owned_transport(tmp_path, monkeypatch):
    complete, retire = tmp_path / "complete", tmp_path / "retire"
    complete.mkdir()
    retire.mkdir()
    async with (
        timeout(60),
        child_peer(("default",)) as model,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        install_stdio_program(model)
        app = interactive(agent_for(model, executor))
        app = replace(
            app,
            tools=[
                *app.tools,
                owned_stdio(
                    tmp_path,
                    "--url-flow",
                    "--completion-release",
                    str(complete),
                    "--stop-release",
                    str(retire),
                ),
            ],
        )
        async with app.session(environment=declaration(executor)) as session:
            ending = waiter = None
            try:
                await (await session.submit("Start alpha")).get_result()
                old = await next_form(session)
                original = await matching_turn(session, old)
                await old.accept_url()
                accepted = await wait_record(tmp_path / "stdio", "url_accepted")
                waiter = asyncio.create_task(old.wait_url_completion(timeout=15))
                ending = asyncio.create_task(cancel_child(session, original))
                blocked = await wait_record(tmp_path / "stdio", "cleanup_wait", pid=accepted["pid"])
                assert not ending.done()
                with pytest.raises(InteractionExpiredError):
                    await old.accept_url()
                (retire / str(blocked["pid"])).touch()
                await ending
                with pytest.raises(NativeError, match="^Native child turn was interrupted$"):
                    await original.get_result()
                with pytest.raises(InteractionExpiredError):
                    await waiter
                process_gone(accepted["pid"])
                assert external(tmp_path) == []
                model.action = collaboration(
                    "followup_task",
                    {"target": "/root/alpha", "message": "Start successor"},
                    "successor",
                )
                await (await session.submit("Start successor")).get_result()
                current = await next_form(session)
                successor = await matching_turn(session, current)
                assert current.subagent_id == old.subagent_id
                assert successor.turn_id != original.turn_id
                assert current.elicitation_id == old.elicitation_id
                with pytest.raises(InteractionExpiredError):
                    await old.accept_url()
                await current.accept_url()
                while True:
                    matches = [
                        row
                        for row in records(tmp_path / "stdio")
                        if row["event"] == "url_accepted" and row["pid"] != accepted["pid"]
                    ]
                    if matches:
                        next_accept = matches[-1]
                        break
                    await asyncio.sleep(0.01)
                (complete / str(next_accept["pid"])).touch()
                (retire / str(next_accept["pid"])).touch()
                await current.wait_url_completion(timeout=8)
                await successor.get_result()
                assert len(external(tmp_path)) == 1
                process_gone(next_accept["pid"])
            finally:
                for row in records(tmp_path / "stdio"):
                    (retire / str(row["pid"])).touch()
                    (complete / str(row["pid"])).touch()
                for task in (ending, waiter):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (ending, waiter) if task is not None),
                    return_exceptions=True,
                )


@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_owned_stdio_url_batch_reserves_all_original_child_records(
    tmp_path, monkeypatch, decision
):
    release = tmp_path / "complete"
    release.mkdir()
    async with (
        timeout(50),
        child_peer(("default",)) as model,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        install_stdio_program(model)
        app = interactive(agent_for(model, executor))
        app = replace(
            app,
            tools=[
                *app.tools,
                owned_stdio(tmp_path, "--url-batch", "--completion-release", str(release)),
            ],
        )
        async with app.session(environment=declaration(executor)) as session:
            await (await session.submit("Start original child")).get_result()
            async with aclosing(session.interactions()) as cursor:
                first = await anext(cursor)
                issued = await wait_record(tmp_path / "stdio", "batch_error")
                (release / str(issued["pid"])).touch()
                await wait_record(tmp_path / "stdio", "batch_complete", pid=issued["pid"])
                second = await anext(cursor)
            owner = await matching_turn(session, first)
            assert second.subagent_id == first.subagent_id == owner.child_id
            assert second.turn_id == first.turn_id == owner.turn_id
            assert second.submission_id == first.submission_id == owner.submission_id
            assert {first.elicitation_id, second.elicitation_id} == {
                "owned-batch-A",
                "owned-batch-B",
            }
            if decision == "accept":
                await first.accept_url()
                await second.accept_url()
                await asyncio.gather(
                    first.wait_url_completion(timeout=8),
                    second.wait_url_completion(timeout=8),
                )
            else:
                await second.decline()
                with pytest.raises(InteractionExpiredError):
                    await first.accept_url()
                with pytest.raises(InteractionExpiredError):
                    await first.wait_url_completion(timeout=8)
            assert (await owner.get_result()).final_output == "alpha complete"
            calls = [
                row
                for row in records(tmp_path / "stdio")
                if row.get("message", {}).get("method") == "tools/call"
            ]
            assert len(calls) == 1
            assert not any(row["event"] == "answer" for row in records(tmp_path / "stdio"))
            assert external(tmp_path) == []
