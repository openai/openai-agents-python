from copy import deepcopy

import pytest

from _agents_codex.initial_messages import _capture_initial_messages


def test_initial_messages_capture_order_text_and_media_without_aliasing():
    messages = [
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "same"},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "same"},
                {"type": "input_image", "file_id": "file-image", "detail": "original"},
                {
                    "type": "input_file",
                    "file_data": "data:application/pdf;base64,YQ==",
                    "filename": "a.pdf",
                },
            ],
        },
    ]
    expected = deepcopy(messages)
    captured = _capture_initial_messages(messages)
    assert captured == expected
    messages[2]["content"][0]["text"] = "mutated"
    assert captured == expected
    assert _capture_initial_messages(captured) == expected


@pytest.mark.parametrize(
    "messages",
    [
        "bad",
        [{"role": "system", "content": "x"}],
        [{"role": "assistant", "content": "\ud800"}],
        [{"role": "assistant", "content": []}],
        [{"role": "assistant", "content": "x", "id": "caller"}],
        [{"role": "assistant", "content": "x", "receipts": []}],
        [{"role": "user", "content": [{"type": "function_call_output", "output": "x"}]}],
    ],
)
def test_initial_messages_reject_unsettled_or_authoritative_data(messages):
    with pytest.raises((TypeError, ValueError)):
        _capture_initial_messages(messages)


@pytest.mark.parametrize("opening", [{"state": object()}, {"resume": "existing"}])
def test_initial_messages_empty_is_still_present(opening):
    assert _capture_initial_messages(None, **opening) is None
    with pytest.raises(ValueError, match="fresh"):
        _capture_initial_messages([], **opening)
