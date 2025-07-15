from openai.types.beta.realtime.conversation_item import ConversationItem
from openai.types.beta.realtime.conversation_item_content import ConversationItemContent

from agents.realtime.items import (
    AssistantMessageItem,
    RealtimeMessageItem,
    SystemMessageItem,
    UserMessageItem,
)
from agents.realtime.openai_realtime import _ConversionHelper


def test_user_message_conversion() -> None:
    item = ConversationItem(
        id="123",
        type="message",
        role="user",
        content=[
            ConversationItemContent(
                id=None, audio=None, text=None, transcript=None, type="input_text"
            )
        ],
    )

    converted: RealtimeMessageItem = _ConversionHelper.conversation_item_to_realtime_message_item(
        item, None
    )

    assert isinstance(converted, UserMessageItem)

    item = ConversationItem(
        id="123",
        type="message",
        role="user",
        content=[
            ConversationItemContent(
                id=None, audio=None, text=None, transcript=None, type="input_audio"
            )
        ],
    )

    converted = _ConversionHelper.conversation_item_to_realtime_message_item(item, None)

    assert isinstance(converted, UserMessageItem)


def test_assistant_message_conversion() -> None:
    item = ConversationItem(
        id="123",
        type="message",
        role="assistant",
        content=[
            ConversationItemContent(id=None, audio=None, text=None, transcript=None, type="text")
        ],
    )

    converted: RealtimeMessageItem = _ConversionHelper.conversation_item_to_realtime_message_item(
        item, None
    )

    assert isinstance(converted, AssistantMessageItem)


def test_system_message_conversion() -> None:
    item = ConversationItem(
        id="123",
        type="message",
        role="system",
        content=[
            ConversationItemContent(
                id=None, audio=None, text=None, transcript=None, type="input_text"
            )
        ],
    )

    converted: RealtimeMessageItem = _ConversionHelper.conversation_item_to_realtime_message_item(
        item, None
    )

    assert isinstance(converted, SystemMessageItem)
