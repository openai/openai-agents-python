"""Admission-owned stdio processes prove origin and acknowledge real cleanup."""

import asyncio
import json
import os
import sys
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path

import pytest

from agents import ElicitationRequest, InteractionExpiredError, NativeError, StdioMCP
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import (
    agent_for,
    captured_turn,
    child_peer,
    collaboration,
    metadata,
    trust,
)
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import records
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_owned_child_prompts import cancel_child, interactive

SERVER = Path(__file__).parent / "fixtures/native_owned_stdio_mcp.py"


def owned_stdio(tmp_path, *args):
    return StdioMCP(
        name="vault",
        command=sys.executable,
        args=(
            "-I",
            "-B",
            str(SERVER),
            "--log",
            str(tmp_path / "stdio"),
            "--state",
            str(tmp_path / "external.jsonl"),
            *args,
        ),
        tools=("search", "state"),
        approval_mode="preapproved",
    )


def install_stdio_program(peer):
    original = peer.response
    seen = set()

    def response():
        body = peer.requests[-1]
        meta = metadata(body, peer.request_headers[-1])
        path = meta["agent_name"]
        if path == "/root":
            return original()
        name = path.rsplit("/", 1)[-1]
        peer.child_requests.setdefault(name, []).append(body)
        peer.child_metadata.setdefault(name, []).append(meta)
        key = (path, meta["turn_id"])
        if key not in seen:
            seen.add(key)
            return [mcp_call("search", "mcp__vault", "same-child-call")]
        return [assistant_output(name + " complete")]

    peer.response = response


async def next_form(session):
    async with aclosing(session.interactions()) as cursor:
        request = await anext(cursor)
    assert isinstance(request, ElicitationRequest)
    return request


async def matching_turn(session, request):
    while True:
        turn = await captured_turn(session, request.subagent_id)
        if turn is not None and turn.turn_id == request.turn_id:
            assert turn.submission_id == request.submission_id
            return turn
        await asyncio.sleep(0.01)


async def wait_record(log, event, *, pid=None, generation=None):
    while True:
        matches = [
            item
            for item in records(log)
            if item["event"] == event
            and (pid is None or item["pid"] == pid)
            and (generation is None or item.get("generation") == generation)
        ]
        if matches:
            return matches[-1]
        await asyncio.sleep(0.01)


def process_gone(pid):
    # A terminal receipt must follow reaping, not merely a signal or a zombie exit.
    if os.name == "nt":
        pytest.skip("The controlled SIGTERM retirement peer requires POSIX signals")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def external(tmp_path):
    path = tmp_path / "external.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_stdio_first_second_child_turn_and_siblings_preserve_external_state(
    tmp_path, monkeypatch
):
    async with (
        timeout(45),
        child_peer(("default", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        install_stdio_program(peer)
        app = interactive(agent_for(peer, executor))
        app = replace(app, tools=[*app.tools, owned_stdio(tmp_path)])
        async with app.session(environment=declaration(executor)) as session:
            root = await session.submit("Start alpha and beta")
            assert (await root.get_result()).final_output == "root complete"
            first = await next_form(session)
            async with aclosing(session.interactions()) as cursor:
                second = await anext(cursor)
                if second is first:
                    second = await anext(cursor)
            assert first.subagent_id != second.subagent_id
            alpha = (
                first
                if first.subagent_id == peer.child_metadata["alpha"][0]["thread_id"]
                else second
            )
            beta = second if alpha is first else first
            old = await matching_turn(session, alpha)
            sibling = await matching_turn(session, beta)
            await alpha.respond({"answer": "alpha-first"})
            await old.get_result()
            alpha_pid = next(
                item["pid"]
                for item in records(tmp_path / "stdio")
                if item["event"] == "effect" and item["answer"] == {"answer": "alpha-first"}
            )
            process_gone(alpha_pid)
            assert beta in await session.pending_interactions()
            with pytest.raises(InteractionExpiredError):
                await alpha.respond({"answer": "cannot-answer-beta"})
            await beta.respond({"answer": "beta-first"})
            await sibling.get_result()
            peer.action = collaboration(
                "followup_task", {"target": "/root/alpha", "message": "Read again"}, "next-alpha"
            )
            await (await session.submit("Start alpha's next real turn")).get_result()
            current = await next_form(session)
            successor = await matching_turn(session, current)
            assert successor.child_id == old.child_id and successor.turn_id != old.turn_id
            assert successor.instance_id == old.instance_id
            assert not await session._native._child_question_pending(
                alpha._message["_sdk_question_id"]
            )
            await current.respond({"answer": "alpha-second"})
            await successor.get_result()
            effects = external(tmp_path)
            assert len(effects) == 3 and {row["local_calls"] for row in effects} == {1}
            assert len({row["pid"] for row in effects}) == 3
            for row in effects:
                process_gone(row["pid"])
            # The root keeps its existing connection across separate root turns.
            for number in (1, 2):
                peer.action = mcp_call("state", "mcp__vault", f"root-read-{number}")
                turn = await session.submit("Read root process state without a form")
                await turn.get_result()
                assert not await session.pending_interactions()
            root_state = [row for row in records(tmp_path / "stdio") if row["event"] == "state"]
            assert len(root_state) == 2 and root_state[0]["pid"] == root_state[1]["pid"]
            assert [row["local_calls"] for row in root_state] == [1, 2]


@pytest.mark.skipif(
    os.name == "nt", reason="Controlled retired stdout and process probes require POSIX"
)
@pytest.mark.parametrize("cancel", [False, True])
async def test_stdio_retirement_joins_delayed_process_with_outstanding_request(
    tmp_path, monkeypatch, cancel
):
    release = tmp_path / "release"
    release.mkdir()
    async with (
        timeout(45),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        install_stdio_program(peer)
        app = interactive(agent_for(peer, executor))
        app = replace(
            app, tools=[*app.tools, owned_stdio(tmp_path, "--stop-release", str(release))]
        )
        async with app.session(environment=declaration(executor)) as session:
            try:
                await (await session.submit("Start alpha")).get_result()
                old = await next_form(session)
                turn = await matching_turn(session, old)
                if cancel:
                    ending = asyncio.create_task(cancel_child(session, turn))
                else:
                    await old.respond({"answer": "accepted-before-retirement"})
                    ending = asyncio.create_task(turn.get_result())
                waiting = await wait_record(tmp_path / "stdio", "cleanup_wait")
                assert waiting["trigger"] in {"signal", "eof"}
                assert not ending.done(), (
                    "Native completion preceded process cleanup acknowledgement"
                )
                assert not await session._native._child_question_pending(
                    old._message["_sdk_question_id"]
                )
                (release / str(waiting["pid"])).touch()
                await ending
                process_gone(waiting["pid"])
                if cancel:
                    with pytest.raises(NativeError, match="^Native child turn was interrupted$"):
                        await turn.get_result()
                    assert external(tmp_path) == []
                retired = [
                    row for row in records(tmp_path / "stdio") if row["pid"] == waiting["pid"]
                ]
                assert any(row["event"] == "old_stdout" for row in retired), retired
                assert not await session.pending_interactions()
                peer.action = collaboration(
                    "followup_task",
                    {"target": "/root/alpha", "message": "Start successor"},
                    "successor",
                )
                await (await session.submit("Follow up")).get_result()
                current = await next_form(session)
                successor = await matching_turn(session, current)
                assert successor.turn_id != turn.turn_id and current.subagent_id == old.subagent_id
                with pytest.raises(InteractionExpiredError):
                    await old.respond({"answer": "stale"})
                assert current in await session.pending_interactions()
                await current.respond({"answer": "successor"})
                # Release only this new generation; old stdout retained the old owner.
                waiting = await wait_record(
                    tmp_path / "stdio",
                    "cleanup_wait",
                    pid=int(current._message["request"]["message"].rsplit(":", 1)[1]),
                )
                assert waiting["trigger"] in {"signal", "eof"}
                (release / str(waiting["pid"])).touch()
                await successor.get_result()
            finally:
                # Let actual teardown finish even when an assertion fails.
                for row in records(tmp_path / "stdio"):
                    if row["event"] == "start":
                        (release / str(row["pid"])).touch()


@pytest.mark.parametrize("failure", ["prompt", "catalog", "hold"])
async def test_stdio_unowned_startup_failure_or_cancel_joins_candidate(
    tmp_path, monkeypatch, failure
):
    async with (
        timeout(45),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        app = interactive(agent_for(peer, executor))
        app = replace(
            app,
            tools=[
                *app.tools,
                owned_stdio(tmp_path, "--startup-at", "2", "--startup-mode", failure),
            ],
        )
        async with app.session(environment=declaration(executor)) as session:
            root = await session.submit("Attempt child startup")
            candidate = await wait_record(tmp_path / "stdio", "start", generation=2)
            if failure == "hold":
                await wait_record(tmp_path / "stdio", "startup_wait", pid=candidate["pid"])
                await session._aclose()
            else:
                await root.get_result()
                assert not peer.child_requests
                assert not any([child.turn is not None async for child in session.subagents()])
                assert not await session.pending_interactions()
                assert (
                    await (await session.submit("Continue root")).get_result()
                ).final_output == "root complete"
            process_gone(candidate["pid"])
            assert external(tmp_path) == []


async def test_stdio_startup_prompt_after_real_admission_has_original_turn_owner(
    tmp_path, monkeypatch
):
    async with (
        timeout(45),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        install_stdio_program(peer)
        app = interactive(agent_for(peer, executor))
        app = replace(
            app,
            tools=[
                *app.tools,
                owned_stdio(tmp_path, "--startup-at", "3", "--startup-mode", "prompt"),
            ],
        )
        async with app.session(environment=declaration(executor)) as session:
            await (await session.submit("Start alpha")).get_result()
            startup = await next_form(session)
            assert (
                startup._message["request"]["message"].startswith("startup:")
                and startup.subagent_id is not None
            )
            assert not peer.child_requests
            await startup.respond({"answer": "initialize-this-admission"})
            request = await next_form(session)
            turn = await matching_turn(session, request)
            assert startup.turn_id == turn.turn_id and startup.submission_id == turn.submission_id
            assert peer.child_metadata["alpha"][0]["turn_id"] == turn.turn_id
            await request.respond({"answer": "child-tool"})
            await turn.get_result()
            process_gone(external(tmp_path)[0]["pid"])
