"""Real sampled child steering and SDK ownership across delayed admission receipts."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from agents import NativeError
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import (
    agent_for,
    captured_turn,
    child_peer,
    handles,
    metadata,
    trust,
)
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize("succeeds", [True, False], ids=["sampled_steers", "genuine_failure"])
async def test_child_steers_sample_in_their_real_turn_or_preserve_genuine_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, succeeds: bool
) -> None:
    sampled, release = asyncio.Event(), asyncio.Event()
    next_sampled, next_release = asyncio.Event(), asyncio.Event()
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        ordinary = peer.response

        def child_program() -> list[dict[str, Any]]:
            result = ordinary()
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] == "/root":
                return result
            number = len(peer.child_requests["alpha"])
            answer = assistant_output("original answer" if number == 1 else "successor answer")
            answer["item"]["id"] = f"tail-alpha-{number}"
            if number == 1 and not succeeds:
                answer["item"]["content"] = []
            return [answer]

        async def hold(event: dict[str, Any]) -> None:
            item_id = event.get("item", {}).get("id")
            if item_id == "tail-alpha-1":
                sampled.set()
                await release.wait()
            if item_id == "tail-alpha-2":
                next_sampled.set()
                await next_release.wait()

        monkeypatch.setattr(peer, "response", child_program)
        peer.before_event = hold
        try:
            async with agent_for(peer, executor).session(
                environment=declaration(executor)
            ) as session:
                root = await session.submit("Start one child")
                assert (await root.get_result()).final_output == "root complete"
                await sampled.wait()
                await handles(session, 1)
                [child] = [entry async for entry in session.subagents()]
                first = child.turn
                assert first is not None
                await first.steer("retained child steer one")
                await first.steer("retained child steer two")
                with pytest.raises(RuntimeError, match="child work|callbacks"):
                    await session.save_state()
                release.set()

                failure_message = None
                expected_turn_id = first.turn_id
                if succeeds:
                    # This SSE gate precedes Core's post-sample input check.
                    # Both accepted steers must be consumed in the original
                    # turn rather than misrepresented as a new child turn.
                    await next_sampled.wait()
                    sampled_turn_ids = [row["turn_id"] for row in peer.child_metadata["alpha"]]
                    assert sampled_turn_ids == [first.turn_id, first.turn_id]
                else:
                    with pytest.raises(NativeError) as failed:
                        await first.get_result()
                    failure_message = str(failed.value)
                    assert failure_message in {
                        "Native child turn failed",
                        "Native model response ended without complete supported output",
                    }
                    assert not next_sampled.is_set()
                    # Real failure does not replay, and explicit fresh work
                    # remains possible with the original message still retained.
                    [live] = [entry async for entry in session.subagents()]
                    explicit = await live.start("fresh explicit child input")
                    expected_turn_id = explicit.turn_id
                    await next_sampled.wait()

                # The provider may sample before the SDK observes this turn.
                sampled_turn = await captured_turn(session, child.id)
                while sampled_turn is None or sampled_turn.turn_id != expected_turn_id:
                    await asyncio.sleep(0)
                    sampled_turn = await captured_turn(session, child.id)
                if not succeeds:
                    assert sampled_turn.turn_id == explicit.turn_id != first.turn_id
                    assert "fresh explicit child input" in json.dumps(
                        peer.child_requests["alpha"][-1]
                    )
                else:
                    assert sampled_turn.turn_id == first.turn_id
                    request = json.dumps(peer.child_requests["alpha"][-1]["input"])
                    assert request.count("retained child steer one") == 1
                    assert request.count("retained child steer two") == 1
                next_release.set()
                assert (await sampled_turn.get_result()).final_output == "successor answer"
                if succeeds:
                    assert (await first.get_result()).final_output == "successor answer"
                else:
                    with pytest.raises(NativeError) as repeated:
                        await first.get_result()
                    assert str(repeated.value) == failure_message
                first_events = [event async for event in first.events()]
                assert isinstance(first_events[-1], TurnFinished)
                assert first_events[-1].status == ("completed" if succeeds else "failed")
                if not succeeds:
                    next_events = [event async for event in sampled_turn.events()]
                    assert isinstance(next_events[-1], TurnFinished)
                    assert next_events[-1].status == "completed"
                assert len(peer.child_requests["alpha"]) == 2
        finally:
            release.set()
            next_release.set()


async def test_sampled_child_ack_ownership_blocks_save_and_all_stop_prevents_later_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled, release = asyncio.Event(), asyncio.Event()
    acknowledged, return_ack = asyncio.Event(), asyncio.Event()
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        ordinary = peer.response

        def child_program() -> list[dict[str, Any]]:
            result = ordinary()
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] == "/root":
                return result
            answer = assistant_output("original answer")
            answer["item"]["id"] = "tail-alpha-held"
            return [answer]

        async def hold(event: dict[str, Any]) -> None:
            if event.get("item", {}).get("id") == "tail-alpha-held":
                sampled.set()
                await release.wait()

        monkeypatch.setattr(peer, "response", child_program)
        peer.before_event = hold
        try:
            async with agent_for(peer, executor).session(
                environment=declaration(executor)
            ) as session:
                root = await session.submit("Start one child")
                assert (await root.get_result()).final_output == "root complete"
                await sampled.wait()
                await handles(session, 1)
                [child] = [entry async for entry in session.subagents()]
                original = child.turn
                assert original is not None
                actual = session._native

                class HeldAck:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(actual, name)

                    async def _submit_child(self, *args: Any) -> Any:
                        receipt = await actual._submit_child(*args)
                        acknowledged.set()
                        await return_ack.wait()
                        return receipt

                session._native = HeldAck()  # type: ignore[assignment]
                submitting = asyncio.create_task(original.steer("one retained late steer"))
                await acknowledged.wait()
                release.set()
                assert (await original.get_result()).final_output == "original answer"
                completed_requests = len(peer.child_requests["alpha"])
                assert {row["turn_id"] for row in peer.child_metadata["alpha"]} == {
                    original.turn_id
                }
                submitting.cancel()
                await asyncio.sleep(0)
                assert not submitting.done()
                with pytest.raises(RuntimeError, match="child work|callbacks"):
                    await session.save_state()
                stopping = asyncio.create_task(session.cancel_all())
                await asyncio.sleep(0)
                assert session._graph_interrupts
                assert not submitting.done()
                return_ack.set()
                with pytest.raises(asyncio.CancelledError):
                    await submitting
                await stopping
                assert (
                    await (await session.submit("resume root only")).get_result()
                ).final_output == ("root complete")
                sampled_turn_ids = [row["turn_id"] for row in peer.child_metadata["alpha"]]
                assert set(sampled_turn_ids) == {original.turn_id}, sampled_turn_ids
                assert len(peer.child_requests["alpha"]) == completed_requests
                assert (await original.get_result()).final_output == "original answer"
        finally:
            release.set()
            return_ack.set()


@pytest.mark.parametrize("operation", ["interrupt", "close"])
@pytest.mark.parametrize("failure_boundary", ["reservation", "await"])
async def test_failed_graph_stop_joins_an_accepted_child_admission_before_native_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_boundary: str,
) -> None:
    sampled, release = asyncio.Event(), asyncio.Event()
    acknowledged, return_ack = asyncio.Event(), asyncio.Event()
    bridge_failed = asyncio.Event()
    native_close_after_ack: list[bool] = []
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        AsyncExitStack() as stack,
    ):
        trust(executor, tmp_path, monkeypatch)
        ordinary = peer.response

        def child_program() -> list[dict[str, Any]]:
            result = ordinary()
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] == "/root":
                return result
            answer = assistant_output("original answer")
            answer["item"]["id"] = "tail-alpha-failed-stop"
            return [answer]

        async def hold(event: dict[str, Any]) -> None:
            if event.get("item", {}).get("id") == "tail-alpha-failed-stop":
                sampled.set()
                await release.wait()

        monkeypatch.setattr(peer, "response", child_program)
        peer.before_event = hold
        session = await stack.enter_async_context(
            agent_for(peer, executor).session(environment=declaration(executor))
        )
        try:
            assert (await (await session.submit("Start one child")).get_result()).final_output == (
                "root complete"
            )
            await sampled.wait()
            await handles(session, 1)
            [child] = [entry async for entry in session.subagents()]
            original = child.turn
            assert original is not None
            actual = session._native

            class HeldAckWithOneStopFailure:
                armed = True

                def __getattr__(self, name: str) -> Any:
                    return getattr(actual, name)

                async def _submit_child(self, *args: Any) -> Any:
                    receipt = await actual._submit_child(*args)
                    acknowledged.set()
                    await return_ack.wait()
                    return receipt

                def _interrupt_graph(self, mode: str) -> Any:
                    if not self.armed:
                        return actual._interrupt_graph(mode)
                    self.armed = False

                    def fail() -> None:
                        bridge_failed.set()
                        raise OSError("synthetic child graph bridge failure")

                    if failure_boundary == "reservation":
                        fail()

                    async def failed_stop() -> None:
                        fail()

                    return failed_stop()

                async def close(self) -> None:
                    native_close_after_ack.append(return_ack.is_set())
                    await actual.close()

            session._native = HeldAckWithOneStopFailure()  # type: ignore[assignment]
            submitting = asyncio.create_task(original.steer("retained child input"))
            await acknowledged.wait()
            release.set()
            assert (await original.get_result()).final_output == "original answer"
            completed_requests = len(peer.child_requests["alpha"])
            assert {row["turn_id"] for row in peer.child_metadata["alpha"]} == {original.turn_id}
            cancelling = None
            if operation == "interrupt":
                cancelling = asyncio.create_task(session.cancel_all())
                await bridge_failed.wait()
            stopping = asyncio.create_task(stack.aclose())
            await bridge_failed.wait()
            done, _ = await asyncio.wait((stopping,), timeout=0.1)
            assert not done
            assert not native_close_after_ack
            return_ack.set()
            assert await submitting is None
            with pytest.raises(OSError, match="synthetic child graph bridge failure"):
                await stopping
            if cancelling is not None:
                with pytest.raises(OSError, match="synthetic child graph bridge failure"):
                    await cancelling
            assert native_close_after_ack and all(native_close_after_ack)
            sampled_turn_ids = [row["turn_id"] for row in peer.child_metadata["alpha"]]
            assert set(sampled_turn_ids) == {original.turn_id}, sampled_turn_ids
            assert len(peer.child_requests["alpha"]) == completed_requests
        finally:
            release.set()
            return_ack.set()


@pytest.mark.parametrize("operation", ["cancel_save", "new_root", "close"])
async def test_checkpoint_preparation_keeps_completed_child_checks_owned_without_fencing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A normally sampled steer tests ownership, not a native final-request gap."""
    sampled, release_sample = asyncio.Event(), asyncio.Event()
    checking, release_check = asyncio.Event(), asyncio.Event()
    root_sampled, release_root = asyncio.Event(), asyncio.Event()
    checked, graph_reserved = asyncio.Event(), asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        AsyncExitStack() as stack,
    ):
        trust(executor, tmp_path, monkeypatch)
        ordinary = peer.response

        def child_program() -> list[dict[str, Any]]:
            result = ordinary()
            body = peer.requests[-1]
            if metadata(body, peer.request_headers[-1])["agent_name"] == "/root":
                if "root started during checkpoint preparation" in json.dumps(body["input"]):
                    answer = assistant_output("new root answer")
                    answer["item"]["id"] = "checkpoint-new-root"
                    return [answer]
                return result
            answer = assistant_output("sampled child answer")
            number = len(peer.child_requests["alpha"])
            answer["item"]["id"] = f"checkpoint-alpha-{number}"
            return [answer]

        async def hold(event: dict[str, Any]) -> None:
            item_id = event.get("item", {}).get("id")
            if item_id == "checkpoint-alpha-1":
                sampled.set()
                await release_sample.wait()
            elif item_id == "checkpoint-new-root":
                root_sampled.set()
                await release_root.wait()

        monkeypatch.setattr(peer, "response", child_program)
        peer.before_event = hold
        session = await stack.enter_async_context(
            agent_for(peer, executor).session(environment=declaration(executor))
        )
        try:
            root = await session.submit("Start one child")
            assert (await root.get_result()).final_output == "root complete"
            await sampled.wait()
            await handles(session, 1)
            [child] = [entry async for entry in session.subagents()]
            actual = session._native

            class HeldCompletedChildCheck:
                def __getattr__(self, name: str) -> Any:
                    return getattr(actual, name)

                async def _continue_child_turn(self, receipt: Any) -> Any:
                    checking.set()
                    try:
                        await release_check.wait()
                        return await actual._continue_child_turn(receipt)
                    finally:
                        checked.set()

                def _interrupt_graph(self, mode: str) -> Any:
                    pending = actual._interrupt_graph(mode)
                    # The native call reserves the fence before returning.
                    graph_reserved.set()
                    return pending

            session._native = HeldCompletedChildCheck()  # type: ignore[assignment]
            original = child.turn
            assert original is not None
            await original.steer("steer before the first sample completes")
            release_sample.set()
            assert (await original.get_result()).final_output == "sampled child answer"
            await checking.wait()
            completed_requests = len(peer.child_requests["alpha"])
            assert {row["turn_id"] for row in peer.child_metadata["alpha"]} == {original.turn_id}
            sampled_input = json.dumps(peer.child_requests["alpha"][-1]["input"])
            assert sampled_input.count("steer before the first sample completes") == 1

            saving = asyncio.create_task(session.save_state())
            tasks.append(saving)
            # Give the passive save caller one turn to reach the held check.
            await asyncio.sleep(0)
            assert not saving.done()
            assert not checked.is_set()

            if operation == "cancel_save":
                saving.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await saving
                assert not checked.is_set()
                # A new public save must still await the original owned check.
                retry = asyncio.create_task(session.save_state())
                tasks.append(retry)
                await asyncio.sleep(0)
                assert not retry.done()
                release_check.set()
                state = await retry
                assert state.to_json()
                assert (await original.get_result()).final_output == "sampled child answer"
            elif operation == "new_root":
                new_root = await session.submit("root started during checkpoint preparation")
                await root_sampled.wait()
                assert new_root.turn_id != root.turn_id
                release_check.set()
                with pytest.raises(RuntimeError, match="no longer.*boundary"):
                    await saving
                release_root.set()
                assert (await new_root.get_result()).final_output == "new root answer"
                assert (await original.get_result()).final_output == "sampled child answer"
            else:
                closing = asyncio.create_task(stack.aclose())
                tasks.append(closing)
                # Closing must reach the real native fence while the child
                # check is still gated; checkpoint preparation cannot own it.
                await graph_reserved.wait()
                assert not release_check.is_set()
                assert not closing.done()
                assert not saving.done()
                release_check.set()
                with pytest.raises(RuntimeError, match="Session is closed"):
                    await saving
                await closing

            assert checked.is_set()
            assert len(peer.child_requests["alpha"]) == completed_requests
            assert {row["turn_id"] for row in peer.child_metadata["alpha"]} == {original.turn_id}
        finally:
            release_sample.set()
            release_check.set()
            release_root.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
