"""Normalize AnyLLM reasoning shapes before receipt-bound native replay."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from _agents_codex._model_provider_metadata import _normalize_anyllm_reasoning
from agents import Agent, NativeProvider, SessionState, function_tool
from agents.models import AnyLLMModel


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"reasoning_content": "", "reasoning": {"content": "Private plan"}}, "Private plan"),
        ({"thinking": [{"text": "Private "}, {"thinking": "plan"}]}, "Private plan"),
        ({"reasoning": "Private plan"}, "Private plan"),
    ],
    ids=["nested-reasoning", "nested-thinking", "plain-reasoning"],
)
async def test_native_anyllm_reasoning_replays_after_tool_and_cold_restore(
    stream, metadata, expected
):
    requests = []
    effects = []
    tool_call = {
        "id": "call-lookup",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }

    async def complete(**options):
        requests.append(options)
        opening = len(requests) == 1
        identity = f"chat-{len(requests)}"
        message = (
            {"role": "assistant", "tool_calls": [tool_call], **metadata}
            if opening
            else {"role": "assistant", "content": "Done."}
        )
        finish = "tool_calls" if opening else "stop"
        if not stream:
            return {"id": identity, "choices": [{"message": message, "finish_reason": finish}]}

        async def chunks():
            delta = dict(message)
            if opening:
                delta["tool_calls"] = [{"index": 0, **tool_call}]
            yield {
                "id": identity,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        return chunks()

    @function_tool
    def lookup() -> str:
        effects.append("once")
        return "record"

    client = SimpleNamespace(PROVIDER_NAME="openrouter", acompletion=complete)
    backend = AnyLLMModel(client, "openrouter/reasoning-fixture", stream=stream)
    async with NativeProvider(model_backend=backend) as provider:
        agent = Agent(
            model="gpt-5.5", instructions="Read the record.", provider=provider, tools=[lookup]
        )
        async with agent.session() as session:
            result = await (await session.submit("Read.")).get_result()
            assert result.final_output == "Done."
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with agent.session(state=saved) as session:
            result = await (await session.submit("Continue.")).get_result()
            assert result.final_output == "Done."

    assert effects == ["once"]
    for request in requests[1:]:
        tool_message = next(row for row in request["messages"] if row.get("tool_calls"))
        assert tool_message["reasoning"] == expected
        assert sum(row.get("reasoning") == expected for row in request["messages"]) == 1


@pytest.mark.parametrize("container", ["object", "list"])
def test_anyllm_invalid_nesting_releases_provider_payload_from_traceback(container):
    value = "synthetic-private-reasoning-canary"
    for _ in range(34):
        value = {"content": value} if container == "object" else ["other-private-prefix", value]

    with pytest.raises(ValueError, match="supported nesting depth") as failure:
        _normalize_anyllm_reasoning({"reasoning": value})

    traceback = failure.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if Path(frame.f_code.co_filename).name == "_model_provider_metadata.py":
            assert not {"message", "reasoning", "value", "part", "parts", "text"} & set(
                frame.f_locals
            )
            assert "synthetic-private-reasoning-canary" not in repr(frame.f_locals)
        traceback = traceback.tb_next
