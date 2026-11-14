from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from _agents_codex._model_provider_metadata import (
    _bind_input,
    _capture_frame,
    _capture_output,
    _fold_chat_reasoning,
    _retain_frame,
    _retain_output,
)
from _agents_codex.models import _ChatStreamEvent
from agents.models import LitellmModel, ModelRequest, ModelResponse

BLOCKS = [
    {"type": "thinking", "thinking": "", "signature": "signed_empty"},
    {"type": "thinking", "thinking": "plan", "signature": "signed_plan"},
    {"type": "redacted_thinking", "data": "opaque_block"},
]


def response():
    request = ModelRequest({"model": "fixture", "input": []}, None, "thread", "turn")
    adapter = LitellmModel(SimpleNamespace(), model="anthropic/claude-fixture")
    value = {
        "id": "chat_1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "plan",
                    "thinking_blocks": BLOCKS,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
    }
    return adapter, adapter._completed_response(request, value, None)


def test_complete_capture_replays_blocks_once_and_scopes_exact_model():
    adapter, produced = response()
    annotations = _capture_output(produced)
    assert annotations[0]["metadata"]["value"]["thinking_blocks"] == BLOCKS
    items = deepcopy(produced.output)
    request = ModelRequest(
        {
            "model": "fixture",
            "reasoning": {"effort": "high"},
            "input": [
                *items,
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            ],
            "tools": [],
        },
        None,
        "thread",
        "turn",
    )
    _bind_input(
        request, [{"input_index": 0, "item": items[0], "metadata": annotations[0]["metadata"]}]
    )
    wire, _ = adapter._parameters(request, False)
    assert [
        message["thinking_blocks"] for message in wire["messages"] if "thinking_blocks" in message
    ] == [BLOCKS]
    other = LitellmModel(SimpleNamespace(), model="anthropic/claude-other")
    wire, _ = other._parameters(request, False)
    assert all("thinking_blocks" not in message for message in wire["messages"])
    request.body["reasoning"] = {}
    wire, _ = adapter._parameters(request, False)
    assert all("thinking_blocks" not in message for message in wire["messages"])


def test_capture_and_stream_annotations_reject_post_capture_mutation():
    _, produced = response()
    event = _ChatStreamEvent(
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": deepcopy(produced.output[0]),
        }
    )
    _retain_frame(event, produced, 0)
    import json

    assert "__sdk_model_provider_metadata" in _capture_frame(json.dumps(event), event)
    event["item"]["summary"] = [{"type": "summary_text", "text": "changed"}]
    with pytest.raises(ValueError, match="changed"):
        _capture_frame(json.dumps(event), event)
    produced.output[0]["summary"] = [{"type": "summary_text", "text": "changed"}]
    with pytest.raises(ValueError, match="changed"):
        _capture_output(produced)


def test_streamed_signature_closes_each_thinking_block():
    target = {}
    for block in [
        BLOCKS[0],
        {"type": "thinking", "thinking": "pl"},
        {"type": "thinking", "thinking": "an"},
        {"type": "thinking", "signature": "signed_plan"},
        BLOCKS[2],
    ]:
        _fold_chat_reasoning(target, {"thinking_blocks": [block]})
    assert target["thinking_blocks"] == BLOCKS


def test_unbound_reasoning_fails_before_client_dispatch():
    adapter, produced = response()
    request = ModelRequest({"model": "fixture", "input": produced.output}, None, "thread", "turn")
    with pytest.raises(ValueError, match="settled provider receipt"):
        adapter._parameters(request, False)


def test_output_occurrence_metadata_rejects_duplicates():
    output = ModelResponse([{"type": "reasoning", "summary": []}])
    with pytest.raises(ValueError, match="occurrences"):
        _retain_output(output, "litellm", "model", [(0, {}), (0, {})])
