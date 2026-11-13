import asyncio
import json

import pytest

from agents import Agent, NativeError, TurnStoppedError
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider


async def test_one_shot_storage_policy_checks_combined_audio_envelope(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
):
    from _agents_codex import _media, agent, input as native_input

    for module in (_media, agent, native_input):
        monkeypatch.setattr(module, "_MAX_AUDIO_INPUT_BYTES", 2048)

    responses.respond(assistant_output("unexpected oversized request"))
    seen = []

    async def project(input, history):
        seen.append(input)
        assert "initial prefix" in json.dumps(history.items)
        return {
            "role": "user",
            "content": [{"type": "input_audio", "audio_url": "https://audio.test/" + "a" * 900}],
        }

    original = {"role": "user", "type": "message", "content": "original"}
    declared = Agent(
        instructions="Answer.",
        model="custom-model",
        model_settings={"input_modalities": ["text", "audio"]},
        provider=provider(responses),
    )
    with pytest.raises(ValueError, match="Audio input exceeds the native request capacity"):
        await declared.run(
            original,
            initial_messages=[{"role": "assistant", "content": "initial prefix" + "a" * 1300}],
            input_storage_policy=project,
        )

    assert seen == [original]
    assert responses.requests == []


async def test_projects_native_input_and_can_omit_all_new_parts(responses: ResponsesFixture):
    responses.respond(assistant_output("prior answer"))
    responses.respond(assistant_output("replacement answer"))
    responses.respond(assistant_output("omitted answer"))
    seen = []

    def project(input, history):
        seen.append(input)
        assert "prior answer" in json.dumps(history.items)
        return "approved replacement"

    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        await (await session.submit("retained prior input")).get_result()
        await (
            await session.submit("unapproved original", input_storage_policy=project)
        ).get_result()
        await (
            await session.submit("dropped original", input_storage_policy=lambda *_: None)
        ).get_result()
        saved = (await session.save_state()).to_json()

    assert seen == ["unapproved original"]
    for value in (saved, json.dumps(responses.requests)):
        assert "retained prior input" in value
        assert "approved replacement" in value
        assert "unapproved original" not in value
        assert "dropped original" not in value
    last_input = responses.requests[2]["input"]
    assert len([item for item in last_input if item.get("role") == "user"]) == 2


async def test_queued_policy_sees_predecessor_and_captured_input(responses: ResponsesFixture):
    responses.respond(assistant_output("preceding answer"))
    responses.respond(assistant_output("queued answer"))
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def wait_before_admission(input, _history):
        entered.set()
        await release.wait()
        return input

    async def project(input, history):
        assert input == {"role": "user", "content": "original queued input"}
        assert "preceding answer" in json.dumps(history.items)
        seen.append(input)
        return "approved queued input"

    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        first_task = asyncio.create_task(
            session.submit("preceding input", input_storage_policy=wait_before_admission)
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            supplied = {"role": "user", "content": "original queued input"}
            second = await asyncio.wait_for(
                session.submit(supplied, if_busy="queue", input_storage_policy=project), 3
            )
            supplied["content"] = "caller mutation after enqueue"
            assert not seen
        finally:
            release.set()
        assert (await (await first_task).get_result()).final_output == "preceding answer"
        assert (await second.get_result()).final_output == "queued answer"
        saved = (await session.save_state()).to_json()
    assert len(seen) == 1
    assert "approved queued input" in saved
    assert "original queued input" not in saved
    assert "caller mutation after enqueue" not in saved


async def test_policy_error_rejects_before_submission_and_session_stays_usable(
    responses: ResponsesFixture,
):
    responses.respond(assistant_output("healthy"))
    failure = RuntimeError("policy rejected")

    def reject(_input, _history):
        raise failure

    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        turn = await session.submit("rejected input", input_storage_policy=reject)
        with pytest.raises(RuntimeError, match="policy rejected") as raised:
            await turn.get_result()
        assert raised.value is failure
        assert not responses.requests
        assert "rejected input" not in json.dumps((await session.history_snapshot()).items)
        result = await (await session.submit("ordinary input")).get_result()
        assert result.final_output == "healthy"


async def test_one_shot_input_policy_uses_the_native_submission_path(responses: ResponsesFixture):
    responses.respond(assistant_output("approved"))
    seen = []

    def project(input, _history):
        seen.append(input)
        return "approved one-shot input"

    agent = Agent(instructions="Answer.", provider=provider(responses))
    result = await agent.run("original one-shot input", input_storage_policy=project)
    assert result.final_output == "approved"
    assert seen == ["original one-shot input"]
    wire = json.dumps(responses.requests)
    assert "approved one-shot input" in wire
    assert "original one-shot input" not in wire


async def test_one_shot_sync_uses_the_same_input_policy(responses: ResponsesFixture):
    responses.respond(assistant_output("approved"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    result = await asyncio.to_thread(
        agent.run_sync,
        "original sync input",
        input_storage_policy=lambda *_: "approved sync input",
    )
    assert result.final_output == "approved"
    assert "approved sync input" in json.dumps(responses.requests)
    assert "original sync input" not in json.dumps(responses.requests)


async def test_closing_retires_unadmitted_policy_result(responses: ResponsesFixture):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def project(_input, _history):
        entered.set()
        await release.wait()
        return "late replacement"

    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        turn = await session.submit("cancelled original", input_storage_policy=project)
        await asyncio.wait_for(entered.wait(), 3)
        closing = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing, 3)
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
    assert not responses.requests


async def test_request_projection_stays_separate_from_saved_input(responses: ResponsesFixture):
    responses.respond(assistant_output("separate views"))

    def project_request(request):
        return [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "model-only view"}],
            }
            if "approved stored view" in json.dumps(item)
            else item
            for item in request.items
        ]

    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        transform_context=project_request,
    )
    async with agent.session() as session:
        turn = await session.submit(
            "original private input", input_storage_policy=lambda *_: "approved stored view"
        )
        assert (await turn.get_result()).final_output == "separate views"
        state = await session.save_state()
    async with agent.session(state=state) as restored:
        stored = json.dumps((await restored.history_snapshot()).items)
    assert "approved stored view" in stored
    assert "original private input" not in stored
    assert "model-only view" not in stored
    wire = json.dumps(responses.requests)
    assert "model-only view" in wire
    assert "approved stored view" not in wire
    assert "original private input" not in wire


async def test_graceful_graph_stop_refuses_policy_entry_and_keeps_session_usable(
    responses: ResponsesFixture,
):
    responses.respond(assistant_output("later answer"))
    called = []

    async def project(_input, _history):
        called.append(True)
        return "stopped replacement"

    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        turn = await session.submit("stopped raw input", input_storage_policy=project)
        await asyncio.wait_for(session.cancel_all(mode="graceful"), 3)
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert called == []
        assert responses.requests == []
        later = await session.submit("later queued input", if_busy="queue")
        assert (await later.get_result()).final_output == "later answer"
        history = json.dumps((await session.history_snapshot()).items)
        for value in (history, json.dumps(responses.requests)):
            assert "later queued input" in value
            assert "stopped raw input" not in value
            assert "stopped replacement" not in value


async def test_graceful_root_stop_cancels_policy_and_preserves_queued_successor(
    responses: ResponsesFixture,
):
    responses.respond(assistant_output("later answer"))
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def project(_input, _history):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return "late stopped replacement"

    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        turn = await session.submit("stopped raw input", input_storage_policy=project)
        await asyncio.wait_for(entered.wait(), 3)
        later = await session.submit(
            "later queued input",
            if_busy="queue",
            input_storage_policy=lambda *_: "approved later input",
        )
        await asyncio.wait_for(turn.cancel(mode="graceful"), 3)
        assert cancelled.is_set()
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert (await later.get_result()).final_output == "later answer"
        assert len(responses.requests) == 1
        history = json.dumps((await session.history_snapshot()).items)
        for value in (history, json.dumps(responses.requests)):
            assert "approved later input" in value
            assert "later queued input" not in value
            assert "stopped raw input" not in value
            assert "late stopped replacement" not in value


@pytest.mark.parametrize("bound", ["records", "bytes"])
async def test_policy_history_limits_reject_before_callback_and_native_admission(
    responses: ResponsesFixture, bound
):
    items = (
        [{"type": "application_note", "index": index} for index in range(4097)]
        if bound == "records"
        else [{"type": "application_note", "text": "x" * (8 * 1024 * 1024 + 1)}]
    )
    called = False

    def project(input, history):
        nonlocal called
        called = True
        raise AssertionError("Oversized history must reject before the callback")

    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        opening = await session.history_snapshot()
        await session.transact_history(expected_revision=opening.revision, keep=0, items=items)
        before = await session.history_snapshot(limit=0)
        with pytest.raises(NativeError, match="Input storage policy history exceeds"):
            await (
                await session.submit("private pending input", input_storage_policy=project)
            ).get_result()
        assert not called
        assert responses.requests == []
        assert await session.history_snapshot(limit=0) == before

        responses.respond(assistant_output("ordinary submission remains supported"))
        assert (await (await session.submit("ordinary input")).get_result()).final_output == (
            "ordinary submission remains supported"
        )
        assert len(responses.requests) == 1
