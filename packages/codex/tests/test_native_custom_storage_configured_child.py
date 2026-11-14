"""Configured child checkpoints have been claiming one canonical execution intent."""

import asyncio
import json
import os
from dataclasses import replace

import pytest

from agents import (
    CustomStorage,
    PendingRequests,
    SQLiteSessionStore,
    ToolContext,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import function_call
from tests.test_native_configured_agent_tool import fixture

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="SQLiteSessionStore requires POSIX locking"
)


class ClaimFaultStore(SQLiteSessionStore):
    fault = None
    fail_flush = False
    claims = 0

    def commit(self, session_id, token, expected_revision, write_id, payload):
        old = json.loads(self.read(session_id, token) or "{}")
        new = json.loads(payload)
        claiming = (
            bool((old.get("manual_checkpoint") or {}).get("configured_child"))
            and new.get("manual_checkpoint") is None
        )
        result_write = any(
            row.get("type") == "response_item"
            and row.get("payload", {}).get("type") == "function_call_output"
            and row.get("payload", {}).get("call_id") == "nested-parent-call"
            for row in new.get("items", [])
        )
        if result_write and self.fault == "result":
            raise RuntimeError("injected configured final receipt failure")
        marker = new.get("manual_checkpoint") or {}
        nested = ((marker.get("configured_child") or {}).get("state") or {}).get(
            "native_continuation"
        ) or {}
        pending = nested.get("pending_manual") or []
        resealing = len(pending) == 1 and pending[0].get("call_id") == "child-second-approval"
        if resealing and self.fault == "reseal_before":
            raise RuntimeError("injected failure before child reseal commit")
        if claiming and self.fault == "before":
            raise RuntimeError("injected failure before configured claim")
        super().commit(session_id, token, expected_revision, write_id, payload)
        if resealing and self.fault == "reseal_lost_ack":
            self.fault = None
            raise RuntimeError("injected lost child reseal acknowledgement")
        if resealing and self.fault == "reseal_after":
            self.fail_flush = True
        if claiming:
            self.claims += 1
            if self.fault == "after":
                self.fail_flush = True

    def flush(self, session_id, token):
        if self.fail_flush:
            raise RuntimeError("injected configured claim flush failure")
        super().flush(session_id, token)


def snapshot(backend, session_id):
    token = backend.acquire(session_id)
    try:
        return json.loads(backend.read(session_id, token))
    finally:
        backend.release(session_id, token)


async def capture(parent, context, storage):
    async with parent.session(context=context, storage=storage) as source:
        turn = await source.submit("Begin.")
        pending = await turn.wait_for_request_or_result()
        assert isinstance(pending, PendingRequests) and len(pending.requests) == 1
        return await source.suspend()


async def finish(parent, context, storage, state):
    async with parent.session(context=context, storage=storage, state=state) as session:
        assert session.restored_turn is not None
        pending = await session.restored_turn.wait_for_request_or_result()
        assert isinstance(pending, PendingRequests) and len(pending.requests) == 1
        await session.respond(pending.requests[0].reply(True))
        assert (await session.restored_turn.get_result()).final_output == "parent complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "before", "after"])
async def test_configured_child_claim_checked_fence(tmp_path, fault):
    async with timeout(30), fixture() as values:
        parent, _, context, root, child, effects, _, _ = values
        parent = replace(parent, tools=[parent.tools[0]])
        root.replies[0] = [root.replies[0][0]]
        path = tmp_path / "canonical.sqlite"
        backend = ClaimFaultStore(path)
        storage = CustomStorage(backend)
        saved = await capture(parent, context, storage)
        sealed = snapshot(backend, saved.source_session_id)
        assert sealed["manual_checkpoint"]["configured_child"]["kind"] == "configured-child-v1"
        assert not effects and len(root.requests) == len(child.requests) == 1
        backend.fault = fault
        if fault is None:
            await finish(parent, context, storage, saved)
            assert effects == ["child-effect"] and backend.claims == 1
            assert len(root.requests) == len(child.requests) == 2
            wire = [
                item
                for item in root.requests[1]["input"]
                if item.get("type") == "function_call_output"
                and item.get("call_id") == "nested-parent-call"
            ]
            assert len(wire) == 1
            canonical = snapshot(backend, saved.source_session_id)
            persisted = [
                row
                for row in canonical["items"]
                if row.get("type") == "response_item"
                and row.get("payload", {}).get("type") == "function_call_output"
                and row["payload"].get("call_id") == "nested-parent-call"
            ]
            assert len(persisted) == 1
            return
        with pytest.raises(RuntimeError):
            async with parent.session(context=context, storage=storage, state=saved):
                pytest.fail("Failed claim must refuse before publishing a Session")
        assert not effects and len(root.requests) == len(child.requests) == 1
        fresh = SQLiteSessionStore(path)
        durable = snapshot(fresh, saved.source_session_id)
        if fault == "before":
            assert durable["manual_checkpoint"] == sealed["manual_checkpoint"]
            await finish(parent, context, CustomStorage(fresh), saved)
            assert effects == ["child-effect"]
        else:
            assert durable.get("manual_checkpoint") is None
            with pytest.raises(RuntimeError):
                async with parent.session(
                    context=context, storage=CustomStorage(fresh), state=saved
                ):
                    pytest.fail("Consumed child intent must refuse an old frame")
            with pytest.raises(RuntimeError):
                async with parent.session(
                    context=context,
                    storage=CustomStorage(fresh),
                    resume=saved.source_session_id,
                ):
                    pytest.fail("Generic reopen must refuse the uncertain outer invocation")
            assert not effects and len(root.requests) == len(child.requests) == 1


@pytest.mark.asyncio
async def test_configured_child_competing_restores_and_consumed_intent(tmp_path):
    async with timeout(30), fixture() as values:
        parent, _, context, root, child, effects, _, _ = values
        parent = replace(parent, tools=[parent.tools[0]])
        root.replies[0] = [root.replies[0][0]]
        path = tmp_path / "canonical.sqlite"
        backend = ClaimFaultStore(path)
        saved = await capture(parent, context, CustomStorage(backend))
        async with parent.session(
            context=context, storage=CustomStorage(backend), state=saved
        ) as first:
            assert first.restored_turn is not None
            pending = await first.restored_turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            with pytest.raises(RuntimeError):
                async with parent.session(
                    context=context,
                    storage=CustomStorage(SQLiteSessionStore(path)),
                    state=saved,
                ):
                    pytest.fail("A second writer must not acquire the pending child")
            assert not effects and len(root.requests) == len(child.requests) == 1
            assert backend.claims == 1
        # Closing the fresh admitted owner before a reply has been consuming its
        # intent without granting the old source checkpoint a replay right.
        with pytest.raises(RuntimeError):
            async with parent.session(
                context=context,
                storage=CustomStorage(SQLiteSessionStore(path)),
                state=saved,
            ):
                pytest.fail("A closed admitted owner must not regrant the old child")
        assert not effects and len(root.requests) == len(child.requests) == 1


@pytest.mark.asyncio
async def test_configured_child_second_boundary_reseals_accepted_effect(tmp_path):
    async with timeout(45), fixture() as values:
        parent, child_agent, context, root, child, effects, _, _ = values

        @function_tool(needs_approval=True)
        async def child_effect(context: ToolContext[dict[str, str]]) -> str:
            effects.append(context.call.call_id)
            return "child effect output"

        child_agent = replace(child_agent, tools=[child_effect])
        parent = replace(
            parent,
            tools=[
                child_agent.as_tool(
                    tool_name="nested_child",
                    tool_description="Run the configured child.",
                )
            ],
        )
        root.replies[0] = [root.replies[0][0]]
        child.replies.insert(1, [function_call("child_effect", {}, "child-second-approval")])
        backend = ClaimFaultStore(tmp_path / "canonical.sqlite")
        storage = CustomStorage(backend)
        first = await capture(parent, context, storage)
        async with parent.session(context=context, storage=storage, state=first) as resumed:
            assert resumed.restored_turn is not None
            pending = await resumed.restored_turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            await resumed.respond(pending.requests[0].reply(True))
            next_pending = await resumed.restored_turn.wait_for_request_or_result()
            assert isinstance(next_pending, PendingRequests)
            assert len(next_pending.requests) == 1
            assert effects == ["child-approval-call"]
            second = await resumed.suspend()
        assert backend.claims == 1
        with pytest.raises(RuntimeError):
            async with parent.session(context=context, storage=storage, state=first):
                pytest.fail("Old boundary must not replace the current child seal")
        assert effects == ["child-approval-call"]
        await finish(parent, context, storage, second)
        assert effects == ["child-approval-call", "child-second-approval"]
        assert backend.claims == 2
        assert len(root.requests) == 2 and len(child.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["source", "turn", "call"])
async def test_configured_child_malformed_owner_preserves_seal(tmp_path, field):
    async with timeout(30), fixture() as values:
        parent, _, context, root, child, effects, _, _ = values
        parent = replace(parent, tools=[parent.tools[0]])
        root.replies[0] = [root.replies[0][0]]
        backend = ClaimFaultStore(tmp_path / "canonical.sqlite")
        storage = CustomStorage(backend)
        saved = await capture(parent, context, storage)
        sealed = snapshot(backend, saved.source_session_id)["manual_checkpoint"]
        payload = json.loads(saved.to_json())
        nested = payload["native_continuation"]["pending_manual"][0]["continuation"]["state"]
        if field == "source":
            nested["source_session_id"] = "invalid-native-thread-id"
        elif field == "turn":
            nested["native_continuation"]["turn_id"] = ""
        else:
            nested["native_continuation"]["pending_manual"][0]["call_id"] = ""
        with pytest.raises((ValueError, RuntimeError)):
            malformed = SessionState.from_json(json.dumps(payload))
            async with parent.session(context=context, storage=storage, state=malformed):
                pytest.fail("Malformed owner must not acquire a child execution intent")
        assert not effects and len(root.requests) == len(child.requests) == 1
        assert snapshot(backend, saved.source_session_id)["manual_checkpoint"] == sealed
        await finish(parent, context, storage, saved)
        assert effects == ["child-effect"] and backend.claims == 1


@pytest.mark.asyncio
async def test_configured_child_final_receipt_failure_settles_parent(tmp_path):
    async with timeout(30), fixture() as values:
        parent, _, context, root, child, effects, _, _ = values
        parent = replace(parent, tools=[parent.tools[0]])
        root.replies[0] = [root.replies[0][0]]
        backend = ClaimFaultStore(tmp_path / "canonical.sqlite")
        storage = CustomStorage(backend)
        saved = await capture(parent, context, storage)
        backend.fault = "result"
        async with parent.session(context=context, storage=storage, state=saved) as session:
            assert session.restored_turn is not None
            pending = await session.restored_turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            await session.respond(pending.requests[0].reply(True))
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(session.restored_turn.get_result(), 10)
        assert effects == ["child-effect"]
        assert len(root.requests) == 1 and len(child.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["reseal_before", "reseal_after", "reseal_lost_ack"])
async def test_configured_child_reseal_fault_boundary(tmp_path, fault):
    async with timeout(45), fixture() as values:
        parent, child_agent, context, root, child, effects, _, _ = values

        @function_tool(needs_approval=True)
        async def child_effect(context: ToolContext[dict[str, str]]) -> str:
            effects.append(context.call.call_id)
            return "child effect output"

        child_agent = replace(child_agent, tools=[child_effect])
        parent = replace(
            parent,
            tools=[
                child_agent.as_tool(
                    tool_name="nested_child",
                    tool_description="Run the configured child.",
                )
            ],
        )
        root.replies[0] = [root.replies[0][0]]
        child.replies.insert(1, [function_call("child_effect", {}, "child-second-approval")])
        backend = ClaimFaultStore(tmp_path / "canonical.sqlite")
        storage = CustomStorage(backend)
        first = await capture(parent, context, storage)
        second = None
        async with parent.session(context=context, storage=storage, state=first) as resumed:
            assert resumed.restored_turn is not None
            pending = await resumed.restored_turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            await resumed.respond(pending.requests[0].reply(True))
            next_pending = await resumed.restored_turn.wait_for_request_or_result()
            assert isinstance(next_pending, PendingRequests)
            assert len(next_pending.requests) == 1
            assert effects == ["child-approval-call"]
            backend.fault = fault
            if fault == "reseal_lost_ack":
                second = await resumed.suspend()
            else:
                exposed = None
                with pytest.raises(RuntimeError):
                    exposed = await resumed.suspend()
                assert exposed is None
                with pytest.raises(RuntimeError):
                    await resumed.respond(next_pending.requests[0].reply(True))
        assert backend.claims == 1
        if fault != "reseal_lost_ack":
            fresh = SQLiteSessionStore(backend.path)
            marker = snapshot(fresh, first.source_session_id).get("manual_checkpoint")
            if fault == "reseal_before":
                assert marker is None
            else:
                current = marker["configured_child"]["state"]["native_continuation"]
                assert current["pending_manual"][0]["call_id"] == "child-second-approval"
            with pytest.raises(RuntimeError):
                async with parent.session(
                    context=context, storage=CustomStorage(fresh), state=first
                ):
                    pytest.fail("Old consumed frame must remain refused after failed reseal")
            assert effects == ["child-approval-call"]
            assert len(root.requests) == 1 and len(child.requests) == 2
            return
        assert second is not None
        with pytest.raises(RuntimeError):
            async with parent.session(context=context, storage=storage, state=first):
                pytest.fail("Old boundary must not replace the current child seal")
        assert effects == ["child-approval-call"]
        await finish(parent, context, storage, second)
        assert effects == ["child-approval-call", "child-second-approval"]
        assert backend.claims == 2
        assert len(root.requests) == 2 and len(child.requests) == 3
