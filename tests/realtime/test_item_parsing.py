from openai.types.realtime.realtime_conversation_item_assistant_message import (
    Content as AssistantMessageContent,
    RealtimeConversationItemAssistantMessage,
)
from openai.types.realtime.realtime_conversation_item_system_message import (
    Content as SystemMessageContent,
    RealtimeConversationItemSystemMessage,
)
from openai.types.realtime.realtime_conversation_item_user_message import (
    Content as UserMessageContent,
    RealtimeConversationItemUserMessage,
)

from agents.realtime.items import (
    AssistantMessageItem,
    RealtimeMessageItem,
    SystemMessageItem,
    UserMessageItem,
)
from agents.realtime.openai_realtime import _ConversionHelper


def test_user_message_conversion() -> None:
    item = RealtimeConversationItemUserMessage(
        id="123",
        type="message",
        role="user",
        content=[
            UserMessageContent(type="input_text", text=None),
        ],
    )

    converted: RealtimeMessageItem = _ConversionHelper.conversation_item_to_realtime_message_item(
        item, None
    )

    assert isinstance(converted, UserMessageItem)

    item = RealtimeConversationItemUserMessage(
        id="123",
        type="message",
        role="user",
        content=[
            UserMessageContent(type="input_audio", audio=None),
        ],
    )

    converted = _ConversionHelper.conversation_item_to_realtime_message_item(item, None)

    assert isinstance(converted, UserMessageItem)


def test_assistant_message_conversion() -> None:
    item = RealtimeConversationItemAssistantMessage(
        id="123",
        type="message",
        role="assistant",
        content=[AssistantMessageContent(type="output_text", text=None)],
    )

    converted: RealtimeMessageItem = _ConversionHelper.conversation_item_to_realtime_message_item(
        item, None
    )

    assert isinstance(converted, AssistantMessageItem)


def test_system_message_conversion() -> None:
    item = RealtimeConversationItemSystemMessage(
        id="123",
        type="message",
        role="system",
        content=[SystemMessageContent(type="input_text", text=None)],
    )

    converted: RealtimeMessageItem = _ConversionHelper.conversation_item_to_realtime_message_item(
        item, None
    )

    assert isinstance(converted, SystemMessageItem)
