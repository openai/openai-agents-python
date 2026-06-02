from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall

from agents import (
    Agent,
    Model,
    ModelResponse,
    ModelSettings,
    OpenAIConversationsSession,
    Runner,
    Usage,
    function_tool,
)
from agents.items import TResponseInputItem, TResponseStreamEvent
from tests.test_responses import get_text_message
from tests.utils.hitl import HITL_REJECTION_MSG
from tests.utils.simple_session import SimpleListSession

TOOL_ECHO = "approved_echo"
TOOL_NOTE = "approved_note"
USER_MESSAGES = [
    "Fetch profile for customer 104.",
    "Update note for customer 104.",
    "Delete note for customer 104.",
]

execute_counts: dict[str, int] = {}


@function_tool(
    name_override=TOOL_ECHO,
    description_override="Echoes back the provided query after approval.",
    needs_approval=True,
)
def approval_echo(query: str) -> str:
    execute_counts[TOOL_ECHO] = execute_counts.get(TOOL_ECHO, 0) + 1
    return f"approved:{query}"


@function_tool(
    name_override=TOOL_NOTE,
    description_override="Records the provided query after approval.",
    needs_approval=True,
)
def approval_note(query: str) -> str:
    execute_counts[TOOL_NOTE] = execute_counts.get(TOOL_NOTE, 0) + 1
    return f"approved_note:{query}"


@dataclass(frozen=True)
class ScenarioStep:
    label: str
    message: str
    tool_name: str
    approval: str
    expected_output: str


@dataclass(frozen=True)
class ScenarioResult:
    approval_item: Any
    items: list[TResponseInputItem]


class ScenarioModel(Model):
    def __init__(self) -> None:
        self._counter = 0

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any | None,
    ) -> ModelResponse:
        if input_has_rejection(input):
            return ModelResponse(
                output=[get_text_message(HITL_REJECTION_MSG)],
                usage=Usage(),
                response_id="resp-test",
            )
        tool_choice = model_settings.tool_choice
        tool_name = tool_choice if isinstance(tool_choice, str) else TOOL_ECHO
        self._counter += 1
        call_id = f"call_{self._counter}"
        query = extract_user_message(input)
        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name=tool_name,
            call_id=call_id,
            arguments=json.dumps({"query": query}),
        )
        return ModelResponse(output=[tool_call], usage=Usage(), response_id="resp-test")

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        if False:
            yield cast(TResponseStreamEvent, {})
        raise RuntimeError("Streaming is not supported in this scenario.")


@pytest.mark.asyncio
async def test_memory_session_hitl_scenario() -> None:
    execute_counts.clear()
    session = SimpleListSession(session_id="memory")
    model = ScenarioModel()

    steps = [
        ScenarioStep(
            label="turn 1",
            message=USER_MESSAGES[0],
            tool_name=TOOL_ECHO,
            approval="approve",
            expected_output=f"approved:{USER_MESSAGES[0]}",
        ),
        ScenarioStep(
            label="turn 2 (rehydrated)",
            message=USER_MESSAGES[1],
            tool_name=TOOL_NOTE,
            approval="approve",
            expected_output=f"approved_note:{USER_MESSAGES[1]}",
        ),
        ScenarioStep(
            label="turn 3 (rejected)",
            message=USER_MESSAGES[2],
            tool_name=TOOL_ECHO,
            approval="reject",
            expected_output=HITL_REJECTION_MSG,
        ),
    ]

    rehydrated: SimpleListSession | None = None

    try:
        first = await run_scenario_step(session, model, steps[0])
        assert_counts(first.items, 1)
        assert_step_output(first.items, first.approval_item, steps[0])

        rehydrated = SimpleListSession(
            session_id=session.session_id,
            history=first.items,
        )
        second = await run_scenario_step(rehydrated, model, steps[1])
        assert_counts(second.items, 2)
        assert_step_output(second.items, second.approval_item, steps[1])

        third = await run_scenario_step(rehydrated, model, steps[2])
        assert_counts(third.items, 3)
        assert_step_output(third.items, third.approval_item, steps[2])

        assert execute_counts.get(TOOL_ECHO) == 1
        assert execute_counts.get(TOOL_NOTE) == 1
    finally:
        await (rehydrated or session).clear_session()


@pytest.mark.asyncio
async def test_openai_conversations_session_hitl_scenario() -> None:
    execute_counts.clear()
    stored_items: list[dict[str, Any]] = []

    async def create_items(*, conversation_id: str, items: list[Any]) -> None:
        stored_items.extend(items)

    def list_items(*, conversation_id: str, order: str, limit: int | None = None):
        class StoredItem:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def model_dump(self, exclude_unset: bool = True) -> dict[str, Any]:
                return self._payload

        async def iterator():
            if order == "desc":
                items_iter = list(reversed(stored_items))
            else:
                items_iter = list(stored_items)
            if limit is not None:
                items_iter = items_iter[:limit]
            for item in items_iter:
                yield StoredItem(item)

        return iterator()

    class ConversationsItems:
        create = staticmethod(create_items)
        list = staticmethod(list_items)

        async def delete(self, *args: Any, **kwargs: Any) -> None:
            return None

    class Conversations:
        items = ConversationsItems()

        async def create(self, *args: Any, **kwargs: Any) -> Any:
            return type("Response", (), {"id": "conv_test"})()

        async def delete(self, *args: Any, **kwargs: Any) -> None:
            return None

    class Client:
        conversations = Conversations()

    client = Client()
    typed_client = cast(Any, client)
    session = OpenAIConversationsSession(conversation_id="conv_test", openai_client=typed_client)
    rehydrated_session = OpenAIConversationsSession(
        conversation_id="conv_test", openai_client=typed_client
    )
    model = ScenarioModel()

    steps = [
        ScenarioStep(
            label="turn 1",
            message=USER_MESSAGES[0],
            tool_name=TOOL_ECHO,
            approval="approve",
            expected_output=f"approved:{USER_MESSAGES[0]}",
        ),
        ScenarioStep(
            label="turn 2 (rehydrated)",
            message=USER_MESSAGES[1],
            tool_name=TOOL_NOTE,
            approval="approve",
            expected_output=f"approved_note:{USER_MESSAGES[1]}",
        ),
        ScenarioStep(
            label="turn 3 (rejected)",
            message=USER_MESSAGES[2],
            tool_name=TOOL_ECHO,
            approval="reject",
            expected_output=HITL_REJECTION_MSG,
        ),
    ]

    offset = 0
    first = await run_scenario_step(session, model, steps[0])
    first_items = stored_items[offset:]
    offset = len(stored_items)
    assert_step_items(first_items, steps[0], first.approval_item)

    second = await run_scenario_step(rehydrated_session, model, steps[1])
    second_items = stored_items[offset:]
    offset = len(stored_items)
    assert_step_items(second_items, steps[1], second.approval_item)

    third = await run_scenario_step(rehydrated_session, model, steps[2])
    third_items = stored_items[offset:]
    assert_step_items(third_items, steps[2], third.approval_item)

    assert execute_counts.get(TOOL_ECHO) == 1
    assert execute_counts.get(TOOL_NOTE) == 1


async def run_scenario_step(
    session: Any,
    model: ScenarioModel,
    step: ScenarioStep,
) -> ScenarioResult:
    agent = Agent(
        name=f"Scenario {step.label}",
        instructions=f"Always call {step.tool_name} before responding.",
        model=model,
        tools=[approval_echo, approval_note],
        model_settings=ModelSettings(tool_choice=step.tool_name),
        tool_use_behavior="stop_on_first_tool",
    )

    first_run = await Runner.run(agent, step.message, session=session)
    assert len(first_run.interruptions) == 1

    approval = first_run.interruptions[0]
    state = first_run.to_state()
    if step.approval == "reject":
        state.reject(approval)
    else:
        state.approve(approval)

    resumed = await Runner.run(agent, state, session=session)
    assert resumed.interruptions == []
    assert resumed.final_output == step.expected_output

    return ScenarioResult(approval_item=approval, items=await session.get_items())


def assert_counts(items: list[TResponseInputItem], turn: int) -> None:
    assert count_user_messages(items) == turn
    assert count_function_calls(items) == turn
    assert count_function_outputs(items) == turn


def assert_step_output(
    items: list[TResponseInputItem],
    approval_item: Any,
    step: ScenarioStep,
) -> None:
    last_user = get_last_user_text(items)
    assert last_user == step.message

    last_call = find_last_function_call(items)
    last_result = find_last_function_output(items)

    approval_call_id = extract_call_id(approval_item.raw_item)
    assert last_call is not None
    assert last_call.get("name") == step.tool_name
    assert last_call.get("call_id") == approval_call_id

    assert last_result is not None
    assert last_result.get("call_id") == approval_call_id
    assert extract_output_text(last_result) == step.expected_output


def assert_step_items(
    items: list[dict[str, Any]],
    step: ScenarioStep,
    approval_item: Any,
) -> None:
    user_items = [item for item in items if item.get("role") == "user"]
    function_calls = [item for item in items if item.get("type") == "function_call"]
    function_outputs = [item for item in items if item.get("type") == "function_call_output"]

    assert len(user_items) == 1
    assert len(function_calls) == 1
    assert len(function_outputs) == 1

    assert extract_user_text(user_items[0]) == step.message
    assert function_calls[0].get("name") == step.tool_name

    approval_call_id = extract_call_id(approval_item.raw_item)
    assert function_calls[0].get("call_id") == approval_call_id
    assert function_outputs[0].get("call_id") == approval_call_id
    assert extract_output_text(function_outputs[0]) == step.expected_output


def extract_user_message(input: str | list[TResponseInputItem]) -> str:
    if isinstance(input, str):
        return input

    for item in reversed(input):
        if isinstance(item, dict) and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                text = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "input_text"
                )
                if text:
                    return text

    return ""


def input_has_rejection(input: str | list[TResponseInputItem]) -> bool:
    if not isinstance(input, list):
        return False
    for item in input:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        output = item.get("output")
        if output == HITL_REJECTION_MSG:
            return True
        if isinstance(output, dict) and output.get("type") == "input_text":
            if output.get("text") == HITL_REJECTION_MSG:
                return True
        if isinstance(output, list):
            for entry in output:
                if isinstance(entry, dict) and entry.get("type") == "input_text":
                    if entry.get("text") == HITL_REJECTION_MSG:
                        return True
    return False


def count_user_messages(items: list[TResponseInputItem]) -> int:
    return sum(1 for item in items if isinstance(item, dict) and item.get("role") == "user")


def count_function_calls(items: list[TResponseInputItem]) -> int:
    return sum(
        1 for item in items if isinstance(item, dict) and item.get("type") == "function_call"
    )


def count_function_outputs(items: list[TResponseInputItem]) -> int:
    return sum(
        1 for item in items if isinstance(item, dict) and item.get("type") == "function_call_output"
    )


def find_last_function_call(
    items: list[TResponseInputItem],
) -> dict[str, Any] | None:
    for item in reversed(items):
        if isinstance(item, dict) and item.get("type") == "function_call":
            return cast(dict[str, Any], item)
    return None


def find_last_function_output(
    items: list[TResponseInputItem],
) -> dict[str, Any] | None:
    for item in reversed(items):
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            return cast(dict[str, Any], item)
    return None


def get_last_user_text(items: list[TResponseInputItem]) -> str | None:
    for item in reversed(items):
        if isinstance(item, dict) and item.get("role") == "user":
            return extract_user_text(cast(dict[str, Any], item))
    return None


def extract_user_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "input_text"
        )
    return ""


def extract_call_id(item: Any) -> str | None:
    if isinstance(item, dict):
        return item.get("call_id") or item.get("id")
    return getattr(item, "call_id", None) or getattr(item, "id", None)


def extract_output_text(item: dict[str, Any] | None) -> str:
    if not item:
        return ""

    output = item.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        for entry in output:
            if isinstance(entry, dict) and entry.get("type") == "input_text":
                text = entry.get("text")
                return text if isinstance(text, str) else ""
    if isinstance(output, dict) and output.get("type") == "input_text":
        text = output.get("text")
        return text if isinstance(text, str) else ""
    return ""
