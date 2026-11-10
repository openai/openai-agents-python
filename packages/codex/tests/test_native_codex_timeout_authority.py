"""Callback deadlines cannot publish CLI thread authority after cancellation."""

import asyncio
from dataclasses import replace

import pytest

from _agents_shared.codex_cli.events import TurnCompletedEvent
from _agents_shared.codex_cli.thread import StreamedTurn
from agents import Agent
from agents.codex import codex_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider


class CancellationSuppressingClient:
    def __init__(self):
        self.started = []
        self.resumed = []
        self.cancelled = 0

    def start_thread(self, options=None):
        thread_id = "late-thread" if not self.started else f"fresh-{len(self.started)}"
        self.started.append(thread_id)
        return CancellationSuppressingThread(self, thread_id)

    def resume_thread(self, thread_id, options=None):
        self.resumed.append(thread_id)
        return CancellationSuppressingThread(self, thread_id)


class CancellationSuppressingThread:
    def __init__(self, client, thread_id):
        self.client = client
        self.id = thread_id

    async def run_streamed(self, prompt, options=None):
        async def events():
            if self.client.cancelled == 0:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.client.cancelled += 1
                    task = asyncio.current_task()
                    assert task is not None
                    uncancel = getattr(task, "uncancel", None)
                    if uncancel is not None:
                        uncancel()
            yield TurnCompletedEvent()

        return StreamedTurn(events=events())


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("selection", ["explicit", "implicit"])
async def test_callback_timeout_cannot_publish_completed_thread_authority(
    responses, persist, selection
):
    client = CancellationSuppressingClient()
    declaration = replace(
        codex_tool(codex=client, persist_session=persist),
        callback_timeout=0.05,
        timeout_behavior="error_as_result",
    )
    async with (
        timeout(15),
        Agent(
            instructions="Use codex.", tools=[declaration], provider=provider(responses)
        ).session() as session,
    ):
        responses.respond(function_call("codex", {"prompt": "first"}, "first-cli"))
        responses.respond(assistant_output("timed out"))
        first = await session.submit("Use codex.")
        assert (await first.get_result()).final_output == "timed out"
        assert client.cancelled == 1

        arguments = {"prompt": "second"}
        if selection == "explicit":
            arguments["thread_id"] = "late-thread"
        responses.respond(function_call("codex", arguments, "second-cli"))
        responses.respond(assistant_output("done"))
        second = await session.submit("Use codex again.")
        if selection == "explicit":
            denied = False
            try:
                await second.get_result()
            except Exception:
                denied = True
            assert client.resumed == [], "Timed-out completion granted a resumable thread ID"
            assert denied, "Explicit thread selection must fail after the original timeout"
        else:
            assert (await second.get_result()).final_output == "done"
            assert client.started == ["late-thread", "fresh-1"], (
                "Timed-out completion became the Session's implicit persistent thread"
            )
