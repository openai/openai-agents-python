"""Native formatter context stays separate from explicit application reasoning options."""

from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agents import Agent, ChatCompletionsModel, ModelRequest, ModelResponse, NativeProvider
from agents.models import ResponsesModel
from agents.session import Session
from tests.native_fixtures import assistant_output


def request(reasoning):
    return ModelRequest(
        {"model": "fixture", "input": [], "reasoning": reasoning}, None, "session", "turn"
    )


def chat_client(seen):
    async def create(**options):
        seen.append(options)
        if options.get("stream"):

            async def chunks():
                yield {
                    "id": "chat_fixture",
                    "choices": [
                        {"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}
                    ],
                }

            return chunks()
        return {
            "id": "chat_fixture",
            "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
        }

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_direct_request_cannot_opt_into_implicit_context():
    value = request({"context": "all_turns"})
    assert value._native_reasoning_context is False
    with pytest.raises(TypeError):
        ModelRequest({}, None, "session", "turn", _native_reasoning_context=True)
    object.__setattr__(value, "_native_reasoning_context", True)
    assert "native_reasoning_context" not in repr(value)
    assert value == request({"context": "all_turns"})
    assert "_native_reasoning_context" not in asdict(value)
    assert ModelRequest(**asdict(value))._native_reasoning_context is False
    assert replace(value)._native_reasoning_context is False


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("implicit", [False, True])
@pytest.mark.parametrize(
    "reasoning,accepted",
    [
        ({}, True),
        ({"effort": "low", "summary": "auto"}, True),
        ({"context": "all_turns"}, True),
        ({"effort": "low", "summary": "auto", "context": "all_turns"}, True),
        ({"context": "current_turn"}, False),
        ({"context": "auto"}, False),
        ({"context": None}, False),
        ({"context": "all_turns", "mode": "unexpected"}, False),
        ({"context": "all_turns", "unrecognized": True}, False),
    ],
)
async def test_chat_adapter_accepts_only_attested_formatter_context(
    implicit, reasoning, accepted, streaming
):
    seen = []
    adapter = ChatCompletionsModel(chat_client(seen), stream=streaming)
    value = request(deepcopy(reasoning))
    object.__setattr__(value, "_native_reasoning_context", implicit)
    original = deepcopy(value.body)

    async def invoke():
        if streaming:
            events = [event async for event in adapter.stream(value)]
            return events[-1]["response"]["output"]
        return (await adapter.complete(value)).output

    if accepted and (implicit or "context" not in reasoning):
        output = await invoke()
        assert output[0]["content"][0]["text"] == "done"
        assert len(seen) == 1
        assert "context" not in seen[0]
        assert "reasoning" not in seen[0]
        assert "context" not in seen[0].get("extra_body", {})
    else:
        with pytest.raises(ValueError, match="Extended reasoning options"):
            await invoke()
        assert not seen
    assert value.body == original


async def test_responses_adapter_keeps_formatter_context_in_the_raw_request():
    seen = []

    async def create(**options):
        seen.append(options)
        return {
            "id": "response_fixture",
            "status": "completed",
            "output": [assistant_output("done")["item"]],
        }

    value = request({"context": "all_turns"})
    object.__setattr__(value, "_native_reasoning_context", True)
    original = deepcopy(value.body)
    adapter = ResponsesModel(SimpleNamespace(responses=SimpleNamespace(create=create)))
    await adapter.complete(value)
    assert seen[0]["extra_body"]["reasoning"] == {"context": "all_turns"}
    assert value.body == original


@pytest.mark.parametrize("marker", [True, False, None, "true", 1])
async def test_model_callback_reads_only_the_typed_event_marker(monkeypatch, marker):
    captured = []
    raw = []
    produce = Session._produce_model_response

    async def receive(self, state, message):
        message = {**message, "data": deepcopy(message["data"])}
        message["data"]["native_reasoning_context"] = True
        message.pop("native_reasoning_context", None)
        if marker is not None:
            message["native_reasoning_context"] = marker
        raw.append(deepcopy(message["data"]))
        await produce(self, state, message)

    class Model:
        def complete(self, value):
            captured.append(value)
            return ModelResponse([assistant_output("done")["item"]])

    monkeypatch.setattr(Session, "_produce_model_response", receive)
    async with NativeProvider(model_backend=Model()) as provider:
        result = await Agent(instructions="Answer.", provider=provider).run("Begin.")
    assert result.final_output == "done"
    assert len(captured) == len(raw) == 1
    assert captured[0]._native_reasoning_context is (marker is True)
    assert captured[0].body == raw[0]
