import json

import pytest

from agents import Agent, ResumableSession, RunConfig, Runner, function_tool
from agents.models._codex_items import native_approval_checkpoint
from agents.models.codex import CodexProvider
from tests.memory.test_resumable_session import MemoryStore

from .test_codex_pending_approval import ApprovalThread, approval, raw
from .test_codex_provider import FakeThread, native_pair, threads as threads


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
async def test_codex_selects_model_after_failed_sdk_tool(tmp_path, threads, streamed):
    queue, _ = threads
    context = {"tests_failed": False}
    selections = []

    @function_tool
    def run_tests() -> str:
        context["tests_failed"] = True
        return "Test failed: expected 1, got 2."

    def choose_model(turn):
        assert turn.context is context
        selections.append((turn.agent_name, turn.model_name, turn.turn_number))
        return "model-b" if turn.context["tests_failed"] else None

    first = FakeThread(call={"callId": "tests", "tool": "run_tests", "arguments": {}})
    second = FakeThread(text="The failing assertion compares 1 with 2.")
    queue.extend([first, second])
    agent = Agent(name="test worker", model="model-a", tools=[run_tests])
    provider = CodexProvider(cwd=tmp_path)
    config = RunConfig(model_provider=provider, model_selector=choose_model, tracing_disabled=True)

    if streamed:
        result = Runner.run_streamed(agent, "Run the tests.", context=context, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "Run the tests.", context=context, run_config=config)

    assert result.final_output == "The failing assertion compares 1 with 2."
    assert [first.settings["model"], second.settings["model"]] == ["model-a", "model-b"]
    assert selections == [("test worker", "model-a", 1), ("test worker", "model-a", 2)]
    assert agent.model == "model-a" and config.model is None
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "tests"
        and "expected 1, got 2" in item["output"]
        for item in second.history
    )
    assert first.interrupted and first.closed and second.closed
    assert not queue


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("approved", [False, True], ids=["deny", "approve"])
async def test_codex_approval_restore_keeps_selected_model_until_next_request(
    tmp_path, threads, streamed, approved
):
    queue, _ = threads
    selections = []
    effects = []
    call, _ = native_pair(call_id="pending")
    pending_thread = ApprovalThread(
        events=[raw(call), approval(), {"type": "raw_response_completed"}]
    )

    def choose_model(turn):
        selections.append((turn.agent_name, turn.model_name, turn.turn_number))
        return "model-b" if turn.turn_number == 1 else None

    class RestoredThread(FakeThread):
        async def resolve_approval(self, call_json, request_json, preconditions_json):
            # Recovery executes the reviewed request before any new selection.
            assert selections == [("test worker", "model-a", 1)]
            assert self.settings["model"] == "model-b"
            effects.append(
                (
                    json.loads(call_json),
                    json.loads(request_json),
                    json.loads(preconditions_json),
                )
            )
            return "reviewed command completed"

    recovered_thread = RestoredThread()
    final_thread = FakeThread(text="Finished.")
    queue.append(pending_thread)
    agent = Agent(name="test worker", model="model-a")
    store = MemoryStore()
    session = await ResumableSession.create(store)
    config = RunConfig(
        model_provider=CodexProvider(cwd=tmp_path),
        model_selector=choose_model,
        tracing_disabled=True,
    )
    if streamed:
        paused = Runner.run_streamed(agent, "Start.", session=session, run_config=config)
        async for _ in paused.stream_events():
            pass
    else:
        paused = await Runner.run(agent, "Start.", session=session, run_config=config)
    assert len(paused.interruptions) == 1
    assert pending_thread.settings["model"] == "model-b"
    checkpoint = native_approval_checkpoint(paused.interruptions[0].raw_item)
    assert checkpoint is not None and checkpoint["model"] == "model-b"
    assert selections == [("test worker", "model-a", 1)]

    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    interruption = state.get_interruptions()[0]
    if approved:
        state.approve(interruption)
        queue.append(recovered_thread)
    else:
        state.reject(interruption)
    queue.append(final_thread)
    fresh_config = RunConfig(
        model_provider=CodexProvider(cwd=tmp_path),
        model_selector=choose_model,
        tracing_disabled=True,
    )
    if streamed:
        result = Runner.run_streamed(agent, state, run_config=fresh_config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, state, run_config=fresh_config)

    assert result.final_output == "Finished."
    assert selections == [("test worker", "model-a", 1), ("test worker", "model-a", 2)]
    assert final_thread.settings["model"] == "model-a"
    assert len(effects) == int(approved)
    if approved:
        assert effects[0] == (
            checkpoint["call"],
            checkpoint["request"],
            checkpoint["preconditions"],
        )
        assert recovered_thread.closed
    assert pending_thread.closed and final_thread.closed
    assert agent.model == "model-a" and not queue
