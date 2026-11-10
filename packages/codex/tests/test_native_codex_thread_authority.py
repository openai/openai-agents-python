import asyncio
import json
import os
from pathlib import Path

import pytest

from agents import Agent
from agents.codex import CodexOptions, TurnOptions, codex_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_codex_cli import cli

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Synthetic executable uses a Unix shebang")


async def call(session, responses, thread_id=None, *, succeeds=True):
    responses.respond(
        function_call(
            "codex", {"prompt": "Inspect", "thread_id": thread_id}, f"cli-{len(responses.requests)}"
        )
    )
    if succeeds:
        responses.respond(assistant_output("done"))
    turn = await session.submit("Use codex.")
    if succeeds:
        assert (await turn.get_result()).final_output == "done"
    else:
        with pytest.raises(Exception, match="Codex|function|thread_id|tool"):
            await turn.get_result()


def arguments(tmp_path: Path):
    return [json.loads(line) for line in (tmp_path / "arguments.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("persist", [False, True])
async def test_model_thread_id_cannot_cross_native_sessions(tmp_path, responses, persist):
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(cli(tmp_path))),
        persist_session=persist,
    )
    agent = Agent(instructions="Use codex.", tools=[declaration], provider=provider(responses))
    async with timeout(15), agent.session() as first, agent.session() as other:
        await call(first, responses)
        await call(other, responses, "synthetic-thread", succeeds=False)
    assert len(responses.requests) == 3
    assert len(arguments(tmp_path)) == 1
    assert "resume" not in arguments(tmp_path)[0]


@pytest.mark.parametrize("persist", [False, True])
async def test_successful_thread_ids_resume_only_in_the_issuing_session(
    tmp_path, responses, persist
):
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(cli(tmp_path))),
        persist_session=persist,
    )
    async with (
        timeout(15),
        Agent(
            instructions="Use codex.", tools=[declaration], provider=provider(responses)
        ).session() as session,
    ):
        await call(session, responses)
        await call(session, responses, "synthetic-thread")
        await call(session, responses)
    calls = arguments(tmp_path)
    assert "resume" not in calls[0]
    assert "resume" in calls[1] and "synthetic-thread" in calls[1]
    assert ("resume" in calls[2]) is persist


@pytest.mark.parametrize("persist", [False, True])
async def test_application_initial_thread_and_persistence_precedence(tmp_path, responses, persist):
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(cli(tmp_path))),
        persist_session=persist,
        thread_id=" configured-thread ",
    )
    agent = Agent(instructions="Use codex.", tools=[declaration], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        await call(session, responses)
        await call(session, responses, "synthetic-thread")
        await call(session, responses)
    async with timeout(15), agent.session() as fresh:
        await call(fresh, responses, "configured-thread")
    calls = arguments(tmp_path)
    assert "configured-thread" in calls[0]
    assert "synthetic-thread" in calls[1]
    assert ("synthetic-thread" if persist else "configured-thread") in calls[2]
    assert "configured-thread" in calls[3]


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("terminal", ["incomplete", "aborted"])
async def test_incomplete_or_aborted_stream_cannot_issue_a_thread_id(
    tmp_path, responses, persist, terminal
):
    signal = asyncio.Event()
    path = cli(tmp_path)
    if terminal == "incomplete":
        source = path.read_text()
        marker = "for event in events: print(json.dumps(event), flush=True)"
        assert marker in source
        path.write_text(
            source.replace(
                marker,
                "for event in events:\n"
                " if event['type'] != 'turn.completed': print(json.dumps(event), flush=True)",
            )
        )

    def observer(event):
        if terminal == "aborted" and event.type == "turn.completed":
            signal.set()

    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(path)),
        persist_session=persist,
        turn_options=TurnOptions(signal=signal),
        on_event=observer,
    )
    async with (
        timeout(15),
        Agent(
            instructions="Use codex.", tools=[declaration], provider=provider(responses)
        ).session() as session,
    ):
        await call(session, responses, succeeds=False)
        signal.clear()
        await call(session, responses, "synthetic-thread", succeeds=False)
    assert len(responses.requests) == 2
    assert len(arguments(tmp_path)) == 1


@pytest.mark.parametrize("thread_id", ["", "--other-home"])
def test_application_thread_authority_is_validated_at_declaration(thread_id):
    with pytest.raises(ValueError, match="thread_id"):
        codex_tool(thread_id=thread_id)
