from dataclasses import replace
from hashlib import sha256

import pytest

from agents import Agent, ApprovalRequest, HttpMCP, PendingRequests, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("trusted", [False, True])
async def test_native_portable_remembered_choice_requires_verifier(responses, approve, trusted):
    peer = Peer(names=("write",), annotations=None)
    async with timeout(20), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=["write"])
        for call_id in ("original", "cold"):
            responses.respond(mcp_call("write", "mcp__docs", call_id))
            responses.respond(assistant_output(call_id))
        agent = Agent(
            instructions="Use write.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[descriptor],
        )
        async with agent.session() as original:
            turn = await original.submit("original")
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            request = pending.requests[0]
            assert isinstance(request, ApprovalRequest)
            await request.respond("allow_once" if approve else "deny", always=True)
            assert (await turn.get_result()).final_output == "original"
            encoded = (await original.save_state()).to_json()
        saved = SessionState.from_json(
            encoded, expected_sha256=sha256(encoded.encode()).hexdigest()
        )
        invoked = []

        async def verify(state):
            invoked.append(state.to_json())
            assert set(state.remembered_mcp_approvals.values()) == {approve}
            for key in state.remembered_mcp_approvals:
                state.remembered_mcp_approvals[key] = not approve
            return True

        options = {"verify_remembered_approvals": verify} if trusted else {}
        if trusted:
            # A verified choice must survive a checkpoint before the MCP tool is used.
            async with agent.session(state=saved, **options) as unused:
                again = await unused.save_state()
                assert again.remembered_mcp_approvals == saved.remembered_mcp_approvals
            assert invoked == [saved.to_json()]
            encoded_again = again.to_json()
            saved = SessionState.from_json(
                encoded_again, expected_sha256=sha256(encoded_again.encode()).hexdigest()
            )
            invoked.clear()
        async with agent.session(state=saved, **options) as restored:
            turn = await restored.submit("cold")
            outcome = await turn.wait_for_request_or_result()
            if trusted:
                assert not isinstance(outcome, PendingRequests)
                assert outcome.final_output == "cold"
            else:
                assert isinstance(outcome, PendingRequests)
                request = outcome.requests[0]
                assert isinstance(request, ApprovalRequest)
                await request.respond("deny")
                assert (await turn.get_result()).final_output == "cold"
        assert invoked == ([saved.to_json()] if trusted else [])
        assert url not in encoded
        if trusted:
            assert set(saved.remembered_mcp_approvals.values()) == {approve}
        assert [row["_meta"]["callId"] for row in peer.calls] == (
            ["original", "cold"] if approve and trusted else ["original"] if approve else []
        )


async def test_native_portable_mcp_refuses_changed_credentials(responses):
    peer = Peer(names=("write",), annotations=None)
    async with timeout(20), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=["write"])
        for call_id in ("original", "cold"):
            responses.respond(mcp_call("write", "mcp__docs", call_id))
            responses.respond(assistant_output(call_id))

        def agent_for(tool):
            return Agent(
                instructions="Use write.",
                model="gpt-5.5",
                provider=provider(responses),
                tools=[tool],
            )

        async with agent_for(descriptor).session() as original:
            turn = await original.submit("original")
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            request = pending.requests[0]
            assert isinstance(request, ApprovalRequest)
            await request.respond("allow_once", always=True)
            assert (await turn.get_result()).final_output == "original"
            encoded = (await original.save_state()).to_json()
        saved = SessionState.from_json(
            encoded, expected_sha256=sha256(encoded.encode()).hexdigest()
        )
        changed = replace(descriptor, bearer_token="new-synthetic-credential")
        async with agent_for(changed).session(
            state=saved, verify_remembered_approvals=lambda _: True
        ) as restored:
            turn = await restored.submit("cold")
            outcome = await turn.wait_for_request_or_result()
            assert isinstance(outcome, PendingRequests)
            request = outcome.requests[0]
            assert isinstance(request, ApprovalRequest)
            await request.respond("deny")
            assert (await turn.get_result()).final_output == "cold"
        assert [row["_meta"]["callId"] for row in peer.calls] == ["original"]
        assert "Bearer new-synthetic-credential" in peer.auth
