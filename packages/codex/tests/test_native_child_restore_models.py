"""Saved child models use current metadata even when a role stops repeating its model."""

from dataclasses import replace

import pytest

from agents import NativeAgents, NativeRole
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import (
    agent_for,
    child_peer,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize("keep_role_model", [True, False])
async def test_restored_child_retains_current_model_capabilities(
    tmp_path, monkeypatch, keep_role_model
):
    async with (
        timeout(40),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original_response = peer.response

        def response():
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] != "/root":
                peer.child_requests.setdefault("alpha", []).append(peer.requests[-1])
                return [assistant_output("child complete")]
            return original_response()

        peer.response = response
        base = replace(agent_for(peer, executor), model="gpt-5.6-sol")
        native = next(tool for tool in base.tools if isinstance(tool, NativeAgents))
        role = NativeRole(
            name="research", description="Read", instructions="Read.", model="gpt-5.5"
        )
        source = replace(base, tools=[replace(native, additional_roles=[role])])
        async with source.session(environment=declaration(executor)) as first:
            await (await first.submit("Create child")).get_result()
            [child] = await handles(first, 1)
            await child.get_result()
            saved = await first.save_state()
        before = len(peer.child_requests["alpha"])
        destination = replace(
            base,
            tools=[
                replace(
                    native,
                    additional_roles=[replace(role, model=role.model if keep_role_model else None)],
                )
            ],
        )
        peer.action = collaboration(
            "followup_task", {"target": "/root/alpha", "message": "Again"}, "wake"
        )
        async with destination.session(environment=declaration(executor), state=saved) as restored:
            await (await restored.submit("Wake child")).get_result()
            [child] = await handles(restored, 1)
            assert (await child.get_result()).final_output == "child complete"
        initial = peer.child_requests["alpha"][0]
        reloaded = peer.child_requests["alpha"][before]
        assert initial["model"] == reloaded["model"] == "gpt-5.5"
        assert initial["tools"] == reloaded["tools"]
        assert any(tool.get("name") == "apply_patch" for tool in reloaded["tools"])
        assert initial.get("text") == reloaded.get("text")

        # Saved names are not authority: an unknown child model must fail current
        # admission before another executor connection or any model request.
        invalid_child = saved.descendants[0].model_copy(
            update={"metadata": {**saved.descendants[0].metadata, "model": "unselected-model"}}
        )
        invalid = saved.model_copy(update={"descendants": [invalid_child]})
        model_requests = len(peer.requests)
        executor_requests = len(executor.requests)
        with pytest.raises(RuntimeError, match="metadata is incompatible with NativeAgents"):
            async with destination.session(environment=declaration(executor), state=invalid):
                pytest.fail("Unvalidated saved child model was admitted")
        assert len(peer.requests) == model_requests
        assert len(executor.requests) == executor_requests
