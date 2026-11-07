"""A child URL wait survives its tool reply only within its original admission."""

import asyncio

import pytest

from agents import ElicitationRequest, HttpMCP, InteractionExpiredError
from tests._asyncio import timeout
from tests.native_fixtures import tool_output
from tests.test_native_combined_child_url import next_owned_question, owned_http_child
from tests.test_native_owned_child_prompts import cancel_child
from tests.test_native_root_url_lifetime import RootLifetimePeer


class SessionTrackedLifetimePeer(RootLifetimePeer):
    """Reuse the existing after-call peer, recording only actual session headers."""

    def __init__(self):
        super().__init__()
        self.call_sessions = []
        self.notification_sessions = []

    async def handle(self, request):
        body = await request.json()
        if body.get("method") == "tools/call":
            self.call_sessions.append(request.headers.get("Mcp-Session-Id"))
        return await super().handle(request)

    async def notifications(self, request):
        self.notification_sessions.append(request.headers.get("Mcp-Session-Id"))
        return await super().notifications(request)


@pytest.mark.parametrize("retirement", ["live", "cancelled"])
async def test_child_http_url_completion_after_original_tool_return(
    tmp_path, monkeypatch, retirement
):
    peer = SessionTrackedLifetimePeer()
    following_request = asyncio.Event()
    release_model = asyncio.Event()
    async with timeout(50), peer.serve() as url:
        registration = HttpMCP(
            name="records", url=url, tools=["search"], approval_mode="preapproved"
        )
        async with owned_http_child(tmp_path, monkeypatch, registration) as (
            model,
            session,
            alpha,
            children,
        ):
            original_before_event = model.before_event

            async def hold_child_finish(event):
                item = event.get("item", {})
                if item.get("type") == "message" and any(
                    part.get("text") == "alpha complete" for part in item.get("content", [])
                ):
                    # FamilyPeer has received and recorded alpha's next real
                    # provider request; do not let the native child finish yet.
                    following_request.set()
                    await release_model.wait()
                if original_before_event is not None:
                    await original_before_event(event)

            model.before_event = hold_child_finish
            waiter = None
            try:
                question = await next_owned_question(session, alpha)
                assert isinstance(question, ElicitationRequest)
                assert question.elicitation_id == peer.ids[0]
                await question.accept_url()
                await peer.answered.wait()
                await peer.finished.wait()
                await following_request.wait()
                # This is native model input, not merely a server-side send.
                assert "original root result" in tool_output(
                    model.child_requests["alpha"][-1], "child-request-1"
                )
                assert not peer.notification_sent.is_set()
                assert not release_model.is_set()
                assert peer.call_sessions == ["synthetic-required"]
                assert peer.call_sessions[0] in peer.notification_sessions
                assert len(peer.calls) == len(peer.answers) == 1
                assert peer.calls[0]["_meta"]["callId"] == "child-request-1"
                assert peer.answers[0]["result"] == {"action": "accept"}

                started = asyncio.Event()

                async def wait_original():
                    started.set()
                    await question.wait_url_completion(timeout=8)

                waiter = asyncio.create_task(wait_original())
                await started.wait()
                if retirement == "cancelled":
                    await cancel_child(session, alpha)
                # Only now release completion on this original MCP session.
                peer.complete.set()
                await peer.notification_sent.wait()
                if retirement == "live":
                    await waiter
                    assert not release_model.is_set()
                    release_model.set()
                    assert (await alpha.get_result()).final_output == "alpha complete"
                else:
                    with pytest.raises(InteractionExpiredError):
                        await waiter
                    with pytest.raises(InteractionExpiredError):
                        await question.accept_url()
                assert len(peer.calls) == len(peer.answers) == 1
                for child in children.values():
                    if child is not alpha:
                        assert (await child.get_result()).final_output == "beta complete"
            finally:
                release_model.set()
                peer.complete.set()
                if waiter is not None:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
