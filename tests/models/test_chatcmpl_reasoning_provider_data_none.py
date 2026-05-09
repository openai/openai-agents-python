"""Regression test for chatcmpl_converter handling of reasoning items
with ``provider_data`` explicitly set to ``None``.

JSON roundtripping (and some external producers) can store a reasoning item
where ``provider_data`` is the literal ``None`` rather than missing or a dict.
``Converter.items_to_messages`` previously assumed the field was always either
absent or a dict and called ``.get("model", "")`` directly on it, which raised
``AttributeError: 'NoneType' object has no attribute 'get'`` for these
otherwise valid items.
"""

from __future__ import annotations

import json
from typing import cast

import pytest

from agents.items import TResponseInputItem
from agents.models.chatcmpl_converter import Converter


def _reasoning_item_with_provider_data_none() -> TResponseInputItem:
    return cast(
        TResponseInputItem,
        {
            "type": "reasoning",
            "id": "__fake_id__",
            "summary": [{"type": "summary_text", "text": "thinking"}],
            "content": [{"type": "reasoning_text", "text": "step"}],
            "encrypted_content": None,
            "status": None,
            "provider_data": None,
        },
    )


def test_items_to_messages_handles_provider_data_none() -> None:
    """Converter must not crash when a reasoning item has provider_data=None."""
    items = [_reasoning_item_with_provider_data_none()]

    # The bug was a hard AttributeError raised at conversion time. The exact
    # contents of the resulting messages are not the focus here — we only need
    # to assert that conversion completes for both Claude and non-Claude
    # targets and returns a list.
    messages_claude = Converter.items_to_messages(
        items,
        model="claude-sonnet-4",
        preserve_thinking_blocks=True,
    )
    assert isinstance(messages_claude, list)

    messages_gpt = Converter.items_to_messages(
        items,
        model="gpt-4o",
        preserve_thinking_blocks=False,
    )
    assert isinstance(messages_gpt, list)


def test_items_to_messages_handles_provider_data_none_after_json_roundtrip() -> None:
    """JSON serialization preserves a None value, exercising the same path."""
    item = _reasoning_item_with_provider_data_none()
    roundtripped = cast(TResponseInputItem, json.loads(json.dumps(item)))

    # Sanity check: the None survives the roundtrip.
    assert roundtripped["provider_data"] is None  # type: ignore[index]

    messages = Converter.items_to_messages(
        [roundtripped],
        model="claude-sonnet-4",
        preserve_thinking_blocks=True,
    )
    assert isinstance(messages, list)


@pytest.mark.parametrize(
    "bogus_provider_data",
    [123, "not-a-dict", ["model", "x"]],
)
def test_items_to_messages_handles_non_dict_provider_data(
    bogus_provider_data: object,
) -> None:
    """Non-dict provider_data values are treated as missing rather than crashing."""
    item = _reasoning_item_with_provider_data_none()
    item["provider_data"] = bogus_provider_data  # type: ignore[index]

    messages = Converter.items_to_messages(
        [item],
        model="claude-sonnet-4",
        preserve_thinking_blocks=True,
    )
    assert isinstance(messages, list)
