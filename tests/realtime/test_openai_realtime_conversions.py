from typing import cast

import pytest
from openai.types.realtime.realtime_conversation_item_user_message import (
    RealtimeConversationItemUserMessage,
)
from openai.types.realtime.realtime_tracing_config import (
    TracingConfiguration,
)

from agents import Agent, function_tool, tool_namespace
from agents.exceptions import UserError
from agents.handoffs import handoff
from agents.realtime.config import RealtimeModelTracingConfig
from agents.realtime.model_inputs import (
    RealtimeModelSendRawMessage,
    RealtimeModelSendUserInput,
    RealtimeModelUserInputMessage,
)
from agents.realtime.openai_realtime import (
    OpenAIRealtimeWebSocketModel,
    _ConversionHelper,
    get_api_key,
)
from agents.tool import Tool


@pytest.mark.asyncio
async def test_get_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert await get_api_key(None) == "env-key"


@pytest.mark.asyncio
async def test_get_api_key_from_callable_async():
    async def f():
        return "k"

    assert await get_api_key(f) == "k"


def test_try_convert_raw_message_invalid_returns_none():
    msg = RealtimeModelSendRawMessage(message={"type": "invalid.event", "other_data": {}})
    assert _ConversionHelper.try_convert_raw_message(msg) is None


def test_convert_user_input_to_conversation_item_dict_and_str():
    # Dict with mixed, including unknown parts (silently skipped)
    dict_input_any = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "hello"},
            {"type": "input_image", "image_url": "http://x/y.png", "detail": "auto"},
            {"type": "bogus", "x": 1},
        ],
    }
    event = RealtimeModelSendUserInput(
        user_input=cast(RealtimeModelUserInputMessage, dict_input_any)
    )
    item_any = _ConversionHelper.convert_user_input_to_conversation_item(event)
    item = cast(RealtimeConversationItemUserMessage, item_any)
    assert item.role == "user"

    # String input becomes input_text
    event2 = RealtimeModelSendUserInput(user_input="hi")
    item2_any = _ConversionHelper.convert_user_input_to_conversation_item(event2)
    item2 = cast(RealtimeConversationItemUserMessage, item2_any)
    assert item2.content[0].type == "input_text"


def test_convert_user_input_dict_skips_invalid_input_text_parts():
    """input_text parts with missing/non-string text must be skipped, not
    forwarded as Content(text=None) which the realtime API rejects."""
    dict_input_any = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text"},  # missing text
            {"type": "input_text", "text": 123},  # non-string text
            {"type": "input_text", "text": "ok"},  # valid
        ],
    }
    event = RealtimeModelSendUserInput(
        user_input=cast(RealtimeModelUserInputMessage, dict_input_any)
    )
    item = cast(
        RealtimeConversationItemUserMessage,
        _ConversionHelper.convert_user_input_to_conversation_item(event),
    )
    assert item.content is not None
    assert len(item.content) == 1
    assert item.content[0].type == "input_text"
    assert item.content[0].text == "ok"


def test_convert_tracing_config_variants():
    from agents.realtime.openai_realtime import _ConversionHelper as CH

    assert CH.convert_tracing_config(None) is None
    assert CH.convert_tracing_config("auto") == "auto"
    cfg: RealtimeModelTracingConfig = {
        "group_id": "g",
        "metadata": {"k": "v"},
        "workflow_name": "wf",
    }
    oc_any = CH.convert_tracing_config(cfg)
    oc = cast(TracingConfiguration, oc_any)
    assert oc.group_id == "g"
    assert oc.workflow_name == "wf"


def test_tools_to_session_tools_raises_on_non_function_tool():
    class NotFunctionTool:
        def __init__(self):
            self.name = "x"

    m = OpenAIRealtimeWebSocketModel()
    with pytest.raises(UserError):
        m._tools_to_session_tools(cast(list[Tool], [NotFunctionTool()]), [])


def test_tools_to_session_tools_includes_handoffs():
    a = Agent(name="a")
    h = handoff(a)
    m = OpenAIRealtimeWebSocketModel()
    out = m._tools_to_session_tools([], [h])
    assert out[0].name is not None and out[0].name.startswith("transfer_to_")


def test_tools_to_session_tools_rejects_namespaced_function_tools():
    tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]
    m = OpenAIRealtimeWebSocketModel()

    with pytest.raises(UserError, match="tool_namespace\\(\\)"):
        m._tools_to_session_tools([tool], [])


def test_tools_to_session_tools_rejects_deferred_function_tools():
    tool = function_tool(
        lambda customer_id: customer_id,
        name_override="lookup_account",
        defer_loading=True,
    )
    m = OpenAIRealtimeWebSocketModel()

    with pytest.raises(UserError, match="defer_loading=True"):
        m._tools_to_session_tools([tool], [])
