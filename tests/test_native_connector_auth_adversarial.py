"""Native Apps consent and immutable credential ownership across lifecycle changes."""

import asyncio
from contextlib import aclosing

import pytest

from agents import ChatGPTAppsMCP, ElicitationRequest, InteractionExpiredError
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses, tool_output
from tests.test_native_connector_auth import (
    ConnectorBackend,
    access_token,
    application,
    select,
    synthetic_backend,
)


class HeldInitiationBackend(ConnectorBackend):
    """Hold the real HTTP response until cancellation has retired its native owner."""

    def __init__(self):
        super().__init__()
        self.initiation_entered = asyncio.Event()
        self.release_initiation = asyncio.Event()
        self.initiation_returned = asyncio.Event()

    async def initiate(self, request):
        result = await super().initiate(request)
        self.initiation_entered.set()
        try:
            await self.release_initiation.wait()
        except asyncio.CancelledError:
            # Model backend work completing after its HTTP client disconnects.
            await self.release_initiation.wait()
        self.initiation_returned.set()
        return result


async def test_declining_apps_url_preserves_auth_failure_without_polling(responses, monkeypatch):
    peer = ConnectorBackend()
    peer.complete.set()
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("declined-before-consent"))
        responses.respond(assistant_output("declined"))
        app, _ = application(responses)
        issued = []

        def decline(question):
            assert isinstance(question, ElicitationRequest)
            issued.append(question)
            return question.reply(None)

        result = await app.run("go", on_interaction=decline)
        assert result.final_output == "declined"
        assert len(issued) == 1 and len(peer.init_requests) == 1
        assert not peer.poll_requests
        assert len(peer.calls) == 1
        assert "synthetic_original_authentication_failure" in tool_output(
            responses.requests[-1], "declined-before-consent"
        )
        with pytest.raises(InteractionExpiredError):
            await issued[0].accept_url()


async def test_cancel_during_apps_initiation_discards_late_url_response(responses, monkeypatch):
    peer = HeldInitiationBackend()
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("cancel-held-initiation"))
        app, _ = application(responses)
        issued = []

        async def collect(session):
            async with aclosing(session.interactions()) as cursor:
                async for question in cursor:
                    issued.append(question)

        async with app.session() as session:
            reader = asyncio.create_task(collect(session))
            try:
                await session.submit("go")
                await peer.initiation_entered.wait()
                await session.cancel_all()
                peer.release_initiation.set()
                await peer.initiation_returned.wait()
                responses.respond(assistant_output("still usable"))
                probe = await session.submit("continue after cancellation")
                assert (await probe.get_result()).final_output == "still usable"
            finally:
                peer.release_initiation.set()
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        assert not issued, "a cancelled initiation must never publish its late URL"
        assert len(peer.init_requests) == 1 and len(peer.calls) == 1
        assert not peer.poll_requests


@pytest.mark.parametrize("replacement", ["token", "account", "equivalent"])
async def test_apps_credentials_cannot_be_replaced_in_same_session(
    responses, monkeypatch, replacement
):
    peer = ConnectorBackend()
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        app, opening = application(responses)
        replacement_account = (
            "synthetic-account-B" if replacement == "account" else opening.account_id
        )
        candidate = ChatGPTAppsMCP(
            tools=["calendar_search"],
            access_token=(
                access_token(user="synthetic-user-B")
                if replacement == "token"
                else opening.access_token
            ),
            account_id=replacement_account,
            approval_mode="approve",
        )
        async with app.session() as session:
            with pytest.raises(ValueError, match="Apps registration is fixed"):
                await session.update(tools=[candidate])
            responses.respond(select("opening-credentials-survive"))
            responses.respond(assistant_output("declined"))
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            assert isinstance(question, ElicitationRequest)
            await question.decline()
            assert (await turn.get_result()).final_output == "declined"
        assert len(peer.init_requests) == 1 and not peer.poll_requests
        _, _, headers = peer.init_requests[0]
        assert headers["Authorization"] == "Bearer " + opening.access_token
        assert next(v for k, v in headers.items() if k.lower() == "chatgpt-account-id") == (
            opening.account_id
        )
        assert "synthetic_original_authentication_failure" in tool_output(
            responses.requests[-1], "opening-credentials-survive"
        )


async def test_closed_session_apps_wrapper_cannot_complete_new_session_request(
    responses, monkeypatch
):
    peer = ConnectorBackend()
    async with timeout(25), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("closed-owner"))
        app, _ = application(responses)
        async with app.session() as old_session:
            await old_session.submit("go")
            async with aclosing(old_session.interactions()) as cursor:
                old = await anext(cursor)
            assert isinstance(old, ElicitationRequest)
        peer.state_id = "synthetic-private-exact-oauth-state-new-session"
        responses.respond(select("new-owner"))
        responses.respond(assistant_output("finished"))
        fresh = ChatGPTAppsMCP(
            tools=["calendar_search"],
            access_token=access_token(user="synthetic-user-B"),
            account_id="synthetic-account-A",
            approval_mode="approve",
        )
        new_app, _ = application(responses, fresh)
        async with new_app.session() as new_session:
            turn = await new_session.submit("go again")
            async with aclosing(new_session.interactions()) as cursor:
                current = await anext(cursor)
            assert isinstance(current, ElicitationRequest)
            with pytest.raises(InteractionExpiredError):
                await old.accept_url()
            with pytest.raises(InteractionExpiredError):
                await old.wait_url_completion(timeout=1)
            assert not peer.poll_requests
            await current.accept_url()
            await peer.polled.wait()
            peer.complete.set()
            await current.wait_url_completion(timeout=5)
            assert (await turn.get_result()).final_output == "finished"
        assert len(peer.init_requests) == 2 and len(peer.calls) == 2
        assert peer.init_requests[-1][2]["Authorization"] == "Bearer " + fresh.access_token
        assert all(peer.state_id in path for path, _ in peer.poll_requests)
        assert "Authentication for Calendar completed" in tool_output(
            responses.requests[-1], "new-owner"
        )
