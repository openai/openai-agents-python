"""Check approvals and A2A results through Runner without inference."""

import asyncio
import json

import pytest

from agents import Agent, RunConfig, Runner, UserError
from agents.testing import ScriptedModel, function_call

pytest.importorskip("a2a")

from a2a.types import Task, TaskState

from examples.plugins.a2a.src.agents_specialist import specialist


async def paused(agent):
    result = await Runner.run(agent, "private chat", run_config=RunConfig(tracing_disabled=True))
    (call,) = result.interruptions
    assert json.loads(call.arguments) == {}
    return result.to_state(), call


async def finish(agent, decision, *, approve=True):
    state, call = decision
    if approve:
        state.approve(call)
    else:
        state.reject(call, rejection_message="declined")
    result = await Runner.run(agent, state, run_config=RunConfig(tracing_disabled=True))
    return result.final_output


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True], ids=["current-grant", "replaced-grant"])
async def test_loopback_status_rechecks_grants_and_keeps_owners_separate(changed):
    pytest.importorskip("agents.extensions.a2a")
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from examples.plugins.a2a.src.agents_specialist.example import QUESTION, release_agent, reviewer

    requests = []

    async def rpc(request):
        assert request.headers["A2A-Version"] == "1.0"
        requests.append(await request.json())
        return await reviewer(request)

    app = web.Application()
    app.router.add_post("/rpc", rpc)
    async with TestServer(app) as peer:
        url = str(peer.make_url("/rpc"))
        grants = {("pat", url, QUESTION): 1, ("lee", url, QUESTION): 1}
        pat = release_agent(url, "pat", grants)
        lee = release_agent(url, "lee", grants)
        pat_send, lee_send = await asyncio.gather(paused(pat), paused(lee))
        assert not requests
        del grants["lee", url, QUESTION]
        assert await finish(lee, lee_send) == "unknown"
        assert not requests
        assert await finish(pat, pat_send) == "pending"
        read = await paused(pat)
        assert len(requests) == 1
        if changed:
            grants["pat", url, QUESTION] = 2
        assert await finish(pat, read) == ("unknown" if changed else "ready")
    assert [r["method"] for r in requests] == (
        ["SendMessage"] if changed else ["SendMessage", "GetTask"]
    )
    assert requests[0]["params"]["message"]["parts"] == [{"text": QUESTION}]
    assert "private chat" not in json.dumps(requests)


class Peer:
    """Pause a send after delivery to test cancellation and ambiguous outcomes."""

    card_json = b"{}"

    def __init__(self, verdict="available"):
        self.verdict = verdict
        self.delivered = asyncio.Event()
        self.reply = asyncio.Event()
        self.messages = []

    def rpc_token_provider(self) -> str:
        return "synthetic"

    async def send_message(self, text: str, *, message_id: str) -> Task:
        self.messages.append(text)
        self.delivered.set()
        await self.reply.wait()
        task = Task(id="private-task", context_id="private-context")
        task.status.state = TaskState.TASK_STATE_COMPLETED
        task.artifacts.add(artifact_id="inventory").parts.add(text=self.verdict)
        return task

    async def get_task(self, *, task_id: str, context_id: str) -> Task:
        raise AssertionError("No pending task")

    def agent(self, *arguments: dict[str, str]) -> Agent[None]:
        return Agent(
            "Inventory",
            model=ScriptedModel(
                [
                    [function_call("ask_specialist", args, call_id=str(index))]
                    for index, args in enumerate(arguments)
                ]
            ),
            plugins=[
                specialist(
                    self, question="Check stock", artifact_id="inventory", verdicts=("available",)
                )
            ],
            tool_use_behavior="stop_on_first_tool",
        )


@pytest.mark.asyncio
async def test_declining_or_supplying_a_different_question_prevents_dispatch():
    peer = Peer()
    peer.reply.set()
    agent = peer.agent({}, {"question": "other"})
    assert await finish(agent, await paused(agent), approve=False) == "declined"
    with pytest.raises(UserError):
        await paused(agent)
    assert not peer.messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("available", "available"), ("available; private payload", "unknown")],
    ids=["allowed", "unrecognized"],
)
async def test_only_allowed_verdicts_reach_the_agent(verdict, expected):
    peer = Peer(verdict)
    peer.reply.set()
    agent = peer.agent({})
    assert await finish(agent, await paused(agent)) == expected
    assert peer.messages == ["Check stock"]


@pytest.mark.asyncio
async def test_cancellation_after_delivery_does_not_allow_a_second_send():
    peer = Peer()
    agent = peer.agent({}, {})
    sending = asyncio.create_task(finish(agent, await paused(agent)))
    try:
        await asyncio.wait_for(peer.delivered.wait(), 5)
    finally:
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        peer.reply.set()
    assert await finish(agent, await paused(agent)) == "unknown"
    assert peer.messages == ["Check stock"]
