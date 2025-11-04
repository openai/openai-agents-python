from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

pytest.importorskip("sqlalchemy")  # Skip tests if SQLAlchemy is not installed
from agents import Agent, Runner, TResponseInputItem, function_tool
from agents.extensions.memory import AdvancedSQLAlchemySession
from agents.result import RunResult
from agents.run_context import RunContextWrapper
from agents.usage import Usage
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio

DB_URL = "sqlite+aiosqlite:///:memory:"


@asynccontextmanager
async def managed_session(session_id: str) -> AsyncIterator[AdvancedSQLAlchemySession]:
    """Create an AdvancedSQLAlchemySession and ensure the engine is disposed afterwards."""
    session = AdvancedSQLAlchemySession.from_url(
        session_id,
        url=DB_URL,
        create_tables=True,
    )
    try:
        yield session
    finally:
        await session._engine.dispose()


@function_tool
async def test_tool(query: str) -> str:
    """A test tool for verifying tool tracking."""
    return f"Tool result for: {query}"


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model and tooling."""
    return Agent(name="advanced-sqlalchemy", model=FakeModel(), tools=[test_tool])


@pytest.fixture
def usage_data() -> Usage:
    """Fixture providing sample usage data."""
    return Usage(
        requests=1,
        input_tokens=50,
        output_tokens=30,
        total_tokens=80,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
    )


def create_mock_run_result(
    usage: Usage | None = None,
    agent: Usage | None = None,
) -> RunResult:
    """Helper function to create a RunResult carrying usage information."""
    if agent is None:
        agent = Agent(name="test", model=FakeModel())

    if usage is None:
        usage = Usage(
            requests=1,
            input_tokens=50,
            output_tokens=30,
            total_tokens=80,
            input_tokens_details=InputTokensDetails(cached_tokens=10),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
        )

    context_wrapper = RunContextWrapper(context=None, usage=usage)

    return RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="test output",
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=context_wrapper,
        _last_agent=agent,
    )


async def test_advanced_session_basic_functionality():
    async with managed_session("advanced_test") as session:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == "Hello"
        assert retrieved[1].get("content") == "Hi there!"


async def test_message_structure_tracking():
    async with managed_session("structure_test") as session:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "What's 2+2?"},
            {"type": "function_call", "name": "calculator", "arguments": '{"expression": "2+2"}'},  # type: ignore
            {"type": "function_call_output", "output": "4"},  # type: ignore
            {"role": "assistant", "content": "The answer is 4"},
            {"type": "reasoning", "summary": [{"text": "Simple math", "type": "summary_text"}]},  # type: ignore
        ]
        await session.add_items(items)

        conversation_turns = await session.get_conversation_by_turns()
        assert len(conversation_turns) == 1

        turn_1_items = conversation_turns[1]
        assert len(turn_1_items) == 5

        item_types = [item["type"] for item in turn_1_items]
        assert "user" in item_types
        assert "function_call" in item_types
        assert "function_call_output" in item_types
        assert "assistant" in item_types
        assert "reasoning" in item_types


async def test_tool_usage_tracking():
    async with managed_session("tools_test") as session:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Search for cats"},
            {"type": "function_call", "name": "web_search", "arguments": '{"query": "cats"}'},  # type: ignore
            {"type": "function_call_output", "output": "Found cat information"},  # type: ignore
            {"type": "function_call", "name": "calculator", "arguments": '{"expression": "1+1"}'},  # type: ignore
            {"type": "function_call_output", "output": "2"},  # type: ignore
            {"role": "assistant", "content": "I found information and calculated 1+1=2"},
        ]
        await session.add_items(items)

        tool_usage = await session.get_tool_usage()
        assert len(tool_usage) == 2

        tool_names = {usage[0] for usage in tool_usage}
        assert "web_search" in tool_names
        assert "calculator" in tool_names


async def test_branch_listing_and_search():
    async with managed_session("advanced_listing") as session:
        await session.add_items(
            [
                {"role": "user", "content": "Initial turn"},
                {"role": "assistant", "content": "Reply"},
            ]
        )

        await session.add_items(
            [
                {"role": "user", "content": "Second turn with tool"},
                {
                    "type": "mcp_call",
                    "server_label": "search",
                    "name": "lookup",
                    "role": "tool",
                    "content": [],
                },
            ]
        )

        branches = await session.list_branches()
        assert len(branches) == 1
        assert branches[0]["branch_id"] == "main"
        assert branches[0]["user_turns"] == 2

        turns = await session.get_conversation_turns()
        assert [turn["content"] for turn in turns] == ["Initial turn", "Second turn with tool"]

        matches = await session.find_turns_by_content("Second")
        assert len(matches) == 1
        assert matches[0]["turn"] == 2

        tool_usage = await session.get_tool_usage()
        assert tool_usage == [("search.lookup", 1, 2)]


async def test_branching_functionality():
    async with managed_session("branching_test") as session:
        turn_1_items: list[TResponseInputItem] = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
        ]
        await session.add_items(turn_1_items)

        turn_2_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Second question"},
            {"role": "assistant", "content": "Second answer"},
        ]
        await session.add_items(turn_2_items)

        turn_3_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Third question"},
            {"role": "assistant", "content": "Third answer"},
        ]
        await session.add_items(turn_3_items)

        all_items = await session.get_items()
        assert len(all_items) == 6

        branch_name = await session.create_branch_from_turn(2, "test_branch")
        assert branch_name == "test_branch"
        assert session._current_branch_id == "test_branch"

        branch_items = await session.get_items()
        assert len(branch_items) == 2
        assert branch_items[0].get("content") == "First question"
        assert branch_items[1].get("content") == "First answer"

        await session.switch_to_branch("main")
        assert session._current_branch_id == "main"

        main_items = await session.get_items()
        assert len(main_items) == 6

        branches = await session.list_branches()
        assert len(branches) == 2
        branch_ids = [branch["branch_id"] for branch in branches]
        assert "main" in branch_ids
        assert "test_branch" in branch_ids

        await session.delete_branch("test_branch")
        branches_after_delete = await session.list_branches()
        assert len(branches_after_delete) == 1
        assert branches_after_delete[0]["branch_id"] == "main"


async def test_get_conversation_turns():
    async with managed_session("conversation_turns_test") as session:
        turn_1_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello there"},
            {"role": "assistant", "content": "Hi!"},
        ]
        await session.add_items(turn_1_items)

        turn_2_items: list[TResponseInputItem] = [
            {"role": "user", "content": "How are you doing today?"},
            {"role": "assistant", "content": "I'm doing well, thanks!"},
        ]
        await session.add_items(turn_2_items)

        turns = await session.get_conversation_turns()
        assert len(turns) == 2

        assert turns[0]["turn"] == 1
        assert turns[0]["content"] == "Hello there"
        assert turns[0]["full_content"] == "Hello there"
        assert turns[0]["can_branch"] is True
        assert "timestamp" in turns[0]

        assert turns[1]["turn"] == 2
        assert turns[1]["content"] == "How are you doing today?"
        assert turns[1]["full_content"] == "How are you doing today?"
        assert turns[1]["can_branch"] is True


async def test_find_turns_by_content():
    async with managed_session("find_turns_test") as session:
        turn_1_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Tell me about cats"},
            {"role": "assistant", "content": "Cats are great pets"},
        ]
        await session.add_items(turn_1_items)

        turn_2_items: list[TResponseInputItem] = [
            {"role": "user", "content": "What about dogs?"},
            {"role": "assistant", "content": "Dogs are also great pets"},
        ]
        await session.add_items(turn_2_items)

        turn_3_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Tell me about cats again"},
            {"role": "assistant", "content": "Cats are wonderful companions"},
        ]
        await session.add_items(turn_3_items)

        cat_turns = await session.find_turns_by_content("cats")
        assert len(cat_turns) == 2
        assert cat_turns[0]["turn"] == 1
        assert cat_turns[1]["turn"] == 3

        dog_turns = await session.find_turns_by_content("dogs")
        assert len(dog_turns) == 1
        assert dog_turns[0]["turn"] == 2

        no_turns = await session.find_turns_by_content("elephants")
        assert len(no_turns) == 0


async def test_create_branch_from_content():
    async with managed_session("branch_from_content_test") as session:
        turn_1_items: list[TResponseInputItem] = [
            {"role": "user", "content": "First question about math"},
            {"role": "assistant", "content": "Math answer"},
        ]
        await session.add_items(turn_1_items)

        turn_2_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Second question about science"},
            {"role": "assistant", "content": "Science answer"},
        ]
        await session.add_items(turn_2_items)

        turn_3_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Another math question"},
            {"role": "assistant", "content": "Another math answer"},
        ]
        await session.add_items(turn_3_items)

        branch_name = await session.create_branch_from_content("math", "math_branch")
        assert branch_name == "math_branch"
        assert session._current_branch_id == "math_branch"

        branch_items = await session.get_items()
        assert len(branch_items) == 0

        with pytest.raises(ValueError, match="No user turns found containing 'nonexistent'"):
            await session.create_branch_from_content("nonexistent", "error_branch")


async def test_branch_specific_operations():
    async with managed_session("branch_specific_test") as session:
        turn_1_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Main branch question"},
            {"role": "assistant", "content": "Main branch answer"},
        ]
        await session.add_items(turn_1_items)

        usage_main = Usage(requests=1, input_tokens=50, output_tokens=30, total_tokens=80)
        run_result_main = create_mock_run_result(usage_main)
        await session.store_run_usage(run_result_main)

        await session.create_branch_from_turn(1, "test_branch")

        turn_2_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Branch question"},
            {"role": "assistant", "content": "Branch answer"},
        ]
        await session.add_items(turn_2_items)

        usage_branch = Usage(requests=1, input_tokens=40, output_tokens=20, total_tokens=60)
        run_result_branch = create_mock_run_result(usage_branch)
        await session.store_run_usage(run_result_branch)

        main_items = await session.get_items(branch_id="main")
        assert len(main_items) == 2
        assert main_items[0].get("content") == "Main branch question"

        current_items = await session.get_items()
        assert len(current_items) == 2

        main_turns = await session.get_conversation_turns(branch_id="main")
        assert len(main_turns) == 1
        assert main_turns[0]["content"] == "Main branch question"

        current_turns = await session.get_conversation_turns()
        assert len(current_turns) == 1

        main_usage = await session.get_session_usage(branch_id="main")
        assert main_usage is not None
        assert main_usage["total_turns"] == 1

        all_usage = await session.get_session_usage()
        assert all_usage is not None
        assert all_usage["total_turns"] == 2


async def test_branch_error_handling():
    async with managed_session("branch_error_test") as session:
        with pytest.raises(ValueError, match="Turn 5 does not contain a user message"):
            await session.create_branch_from_turn(5, "error_branch")

        with pytest.raises(ValueError, match="Branch 'nonexistent' does not exist"):
            await session.switch_to_branch("nonexistent")

        with pytest.raises(ValueError, match="Branch 'nonexistent' does not exist"):
            await session.delete_branch("nonexistent")

        with pytest.raises(ValueError, match="Cannot delete the 'main' branch"):
            await session.delete_branch("main")

        with pytest.raises(ValueError, match="Branch ID cannot be empty"):
            await session.delete_branch("")

        with pytest.raises(ValueError, match="Branch ID cannot be empty"):
            await session.delete_branch("   ")


async def test_branch_deletion_with_force():
    async with managed_session("force_delete_test") as session:
        await session.add_items([{"role": "user", "content": "Main question"}])
        await session.add_items([{"role": "user", "content": "Second question"}])

        await session.create_branch_from_turn(2, "temp_branch")
        assert session._current_branch_id == "temp_branch"

        await session.add_items([{"role": "user", "content": "Branch question"}])

        branches = await session.list_branches()
        branch_ids = [branch["branch_id"] for branch in branches]
        assert "temp_branch" in branch_ids

        with pytest.raises(ValueError, match="Cannot delete current branch"):
            await session.delete_branch("temp_branch")

        await session.delete_branch("temp_branch", force=True)
        assert session._current_branch_id == "main"

        branches_after = await session.list_branches()
        assert len(branches_after) == 1
        assert branches_after[0]["branch_id"] == "main"


async def test_get_items_with_parameters():
    async with managed_session("get_items_params_test") as session:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
            {"role": "assistant", "content": "Second answer"},
        ]
        await session.add_items(items)

        limited_items = await session.get_items(limit=2)
        assert len(limited_items) == 2
        assert limited_items[0].get("content") == "Second question"
        assert limited_items[1].get("content") == "Second answer"

        main_items = await session.get_items(branch_id="main")
        assert len(main_items) == 4

        all_items = await session.get_items()
        assert len(all_items) == 4

        await session.create_branch_from_turn(2, "test_branch")

        branch_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Branch question"},
            {"role": "assistant", "content": "Branch answer"},
        ]
        await session.add_items(branch_items)

        branch_items_result = await session.get_items(branch_id="test_branch")
        assert len(branch_items_result) == 4

        main_items_from_branch = await session.get_items(branch_id="main")
        assert len(main_items_from_branch) == 4


async def test_usage_tracking_storage(agent: Agent, usage_data: Usage):
    async with managed_session("usage_test") as session:
        await session.add_items([{"role": "user", "content": "First turn"}])
        run_result_1 = create_mock_run_result(usage_data)
        await session.store_run_usage(run_result_1)

        usage_data_2 = Usage(
            requests=2,
            input_tokens=75,
            output_tokens=45,
            total_tokens=120,
            input_tokens_details=InputTokensDetails(cached_tokens=20),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=15),
        )

        await session.add_items([{"role": "user", "content": "Second turn"}])
        run_result_2 = create_mock_run_result(usage_data_2)
        await session.store_run_usage(run_result_2)

        session_usage = await session.get_session_usage()
        assert session_usage is not None
        assert session_usage["requests"] == 3
        assert session_usage["total_tokens"] == 200
        assert session_usage["input_tokens"] == 125
        assert session_usage["output_tokens"] == 75
        assert session_usage["total_turns"] == 2

        turn_1_usage = await session.get_turn_usage(1)
        assert isinstance(turn_1_usage, dict)
        assert turn_1_usage["requests"] == 1
        assert turn_1_usage["total_tokens"] == 80
        assert turn_1_usage["input_tokens_details"]["cached_tokens"] == 10
        assert turn_1_usage["output_tokens_details"]["reasoning_tokens"] == 5

        turn_2_usage = await session.get_turn_usage(2)
        assert isinstance(turn_2_usage, dict)
        assert turn_2_usage["requests"] == 2
        assert turn_2_usage["total_tokens"] == 120
        assert turn_2_usage["input_tokens_details"]["cached_tokens"] == 20
        assert turn_2_usage["output_tokens_details"]["reasoning_tokens"] == 15

        all_turn_usage = await session.get_turn_usage()
        assert isinstance(all_turn_usage, list)
        assert len(all_turn_usage) == 2
        assert all_turn_usage[0]["user_turn_number"] == 1
        assert all_turn_usage[1]["user_turn_number"] == 2


async def test_runner_integration_with_usage_tracking(agent: Agent):
    async with managed_session("integration_test") as session:

        async def store_session_usage(result: Any, session: AdvancedSQLAlchemySession):
            try:
                await session.store_run_usage(result)
            except Exception:
                pass

        assert isinstance(agent.model, FakeModel)
        fake_model = agent.model
        fake_model.set_next_output([get_text_message("San Francisco")])

        result1 = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        assert result1.final_output == "San Francisco"
        await store_session_usage(result1, session)

        fake_model.set_next_output([get_text_message("California")])
        result2 = await Runner.run(
            agent,
            "What state is it in?",
            session=session,
        )
        assert result2.final_output == "California"
        await store_session_usage(result2, session)

        conversation_turns = await session.get_conversation_by_turns()
        assert len(conversation_turns) == 2

        session_usage = await session.get_session_usage()
        assert session_usage is not None
        assert session_usage["total_turns"] == 2
        assert "requests" in session_usage
        assert "total_tokens" in session_usage


async def test_sequence_ordering():
    async with managed_session("sequence_test") as session:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Message 2"},
            {"role": "assistant", "content": "Response 2"},
        ]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert len(retrieved) == 4
        assert retrieved[0].get("content") == "Message 1"
        assert retrieved[1].get("content") == "Response 1"
        assert retrieved[2].get("content") == "Message 2"
        assert retrieved[3].get("content") == "Response 2"


async def test_conversation_structure_with_multiple_turns():
    async with managed_session("multi_turn_test") as session:
        turn_1: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        await session.add_items(turn_1)

        turn_2: list[TResponseInputItem] = [
            {"role": "user", "content": "How are you?"},
            {"type": "function_call", "name": "mood_check", "arguments": "{}"},  # type: ignore
            {"type": "function_call_output", "output": "I'm good"},  # type: ignore
            {"role": "assistant", "content": "I'm doing well!"},
        ]
        await session.add_items(turn_2)

        turn_3: list[TResponseInputItem] = [
            {"role": "user", "content": "Goodbye"},
            {"role": "assistant", "content": "See you later!"},
        ]
        await session.add_items(turn_3)

        conversation_turns = await session.get_conversation_by_turns()
        assert len(conversation_turns) == 3

        assert len(conversation_turns[1]) == 2
        assert conversation_turns[1][0]["type"] == "user"
        assert conversation_turns[1][1]["type"] == "assistant"

        assert len(conversation_turns[2]) == 4
        turn_2_types = [item["type"] for item in conversation_turns[2]]
        assert "user" in turn_2_types
        assert "function_call" in turn_2_types
        assert "function_call_output" in turn_2_types
        assert "assistant" in turn_2_types

        assert len(conversation_turns[3]) == 2


async def test_empty_session_operations():
    async with managed_session("empty_test") as session:
        items = await session.get_items()
        assert len(items) == 0

        conversation = await session.get_conversation_by_turns()
        assert len(conversation) == 0

        tool_usage = await session.get_tool_usage()
        assert len(tool_usage) == 0

        session_usage = await session.get_session_usage()
        assert session_usage is None

        turns = await session.get_conversation_turns()
        assert len(turns) == 0


async def test_json_serialization_edge_cases(usage_data: Usage):
    async with managed_session("json_test") as session:
        await session.add_items([{"role": "user", "content": "First test"}])
        run_result_1 = create_mock_run_result(usage_data)
        await session.store_run_usage(run_result_1)

        run_result_none = create_mock_run_result(None)
        await session.store_run_usage(run_result_none)

        minimal_usage = Usage(
            requests=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        )
        await session.add_items([{"role": "user", "content": "Second test"}])
        run_result_2 = create_mock_run_result(minimal_usage)
        await session.store_run_usage(run_result_2)

        turn_1_usage = await session.get_turn_usage(1)
        assert isinstance(turn_1_usage, dict)
        assert turn_1_usage["requests"] == 1
        assert turn_1_usage["input_tokens_details"]["cached_tokens"] == 10

        turn_2_usage = await session.get_turn_usage(2)
        assert isinstance(turn_2_usage, dict)
        assert turn_2_usage["requests"] == 1
        assert turn_2_usage["input_tokens_details"]["cached_tokens"] == 0
        assert turn_2_usage["output_tokens_details"]["reasoning_tokens"] == 0


async def test_session_isolation():
    async with managed_session("session_1") as session1, managed_session("session_2") as session2:
        await session1.add_items([{"role": "user", "content": "Session 1 message"}])
        await session2.add_items([{"role": "user", "content": "Session 2 message"}])

        session1_items = await session1.get_items()
        session2_items = await session2.get_items()

        assert len(session1_items) == 1
        assert len(session2_items) == 1
        assert session1_items[0].get("content") == "Session 1 message"
        assert session2_items[0].get("content") == "Session 2 message"

        session1_turns = await session1.get_conversation_by_turns()
        session2_turns = await session2.get_conversation_by_turns()

        assert len(session1_turns) == 1
        assert len(session2_turns) == 1


async def test_error_handling_in_usage_tracking(usage_data: Usage):
    async with managed_session("error_test") as session:
        run_result = create_mock_run_result(usage_data)
        await session.store_run_usage(run_result)

        await session._engine.dispose()
        await session.store_run_usage(run_result)


async def test_advanced_tool_name_extraction():
    async with managed_session("advanced_tool_names_test") as session:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Use various tools"},
            {
                "type": "mcp_call",
                "server_label": "filesystem",
                "name": "read_file",
                "arguments": "{}",
            },  # type: ignore
            {
                "type": "mcp_approval_request",
                "server_label": "database",
                "name": "execute_query",
                "arguments": "{}",
            },  # type: ignore
            {"type": "computer_call", "arguments": "{}"},  # type: ignore
            {"type": "file_search_call", "arguments": "{}"},  # type: ignore
            {"type": "web_search_call", "arguments": "{}"},  # type: ignore
            {"type": "code_interpreter_call", "arguments": "{}"},  # type: ignore
            {"type": "function_call", "name": "calculator", "arguments": "{}"},  # type: ignore
            {"type": "custom_tool_call", "name": "custom_tool", "arguments": "{}"},  # type: ignore
        ]
        await session.add_items(items)

        conversation_turns = await session.get_conversation_by_turns()
        turn_items = conversation_turns[1]

        tool_items = [item for item in turn_items if item["tool_name"]]
        tool_names = [item["tool_name"] for item in tool_items]

        assert "filesystem.read_file" in tool_names
        assert "database.execute_query" in tool_names
        assert "computer_call" in tool_names
        assert "file_search_call" in tool_names
        assert "web_search_call" in tool_names
        assert "code_interpreter_call" in tool_names
        assert "calculator" in tool_names
        assert "custom_tool" in tool_names


async def test_branch_usage_tracking():
    async with managed_session("branch_usage_test") as session:
        await session.add_items([{"role": "user", "content": "Main question"}])
        usage_main = Usage(requests=1, input_tokens=50, output_tokens=30, total_tokens=80)
        run_result_main = create_mock_run_result(usage_main)
        await session.store_run_usage(run_result_main)

        await session.create_branch_from_turn(1, "usage_branch")
        await session.add_items([{"role": "user", "content": "Branch question"}])
        usage_branch = Usage(requests=2, input_tokens=100, output_tokens=60, total_tokens=160)
        run_result_branch = create_mock_run_result(usage_branch)
        await session.store_run_usage(run_result_branch)

        main_usage = await session.get_session_usage(branch_id="main")
        assert main_usage is not None
        assert main_usage["requests"] == 1
        assert main_usage["total_tokens"] == 80
        assert main_usage["total_turns"] == 1

        branch_usage = await session.get_session_usage(branch_id="usage_branch")
        assert branch_usage is not None
        assert branch_usage["requests"] == 2
        assert branch_usage["total_tokens"] == 160
        assert branch_usage["total_turns"] == 1

        total_usage = await session.get_session_usage()
        assert total_usage is not None
        assert total_usage["requests"] == 3
        assert total_usage["total_tokens"] == 240
        assert total_usage["total_turns"] == 2

        branch_turn_usage = await session.get_turn_usage(branch_id="usage_branch")
        assert isinstance(branch_turn_usage, list)
        assert len(branch_turn_usage) == 1
        assert branch_turn_usage[0]["requests"] == 2


async def test_tool_name_extraction():
    async with managed_session("tool_names_test") as session:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Use tools please"},
            {"type": "function_call", "name": "search_web", "arguments": "{}"},  # type: ignore
            {"type": "function_call_output", "tool_name": "search_web", "output": "result"},  # type: ignore
            {"type": "function_call", "name": "calculator", "arguments": "{}"},  # type: ignore
        ]
        await session.add_items(items)

        conversation_turns = await session.get_conversation_by_turns()
        turn_items = conversation_turns[1]

        tool_items = [item for item in turn_items if item["tool_name"]]
        tool_names = [item["tool_name"] for item in tool_items]

        assert "search_web" in tool_names
        assert "calculator" in tool_names


async def test_tool_execution_integration(agent: Agent):
    async with managed_session("tool_integration_test") as session:
        fake_model = cast(FakeModel, agent.model)
        fake_model.set_next_output(
            [
                {  # type: ignore
                    "type": "function_call",
                    "name": "test_tool",
                    "arguments": '{"query": "test query"}',
                    "call_id": "call_123",
                }
            ]
        )

        fake_model.set_next_output([get_text_message("Tool executed successfully")])

        result = await Runner.run(
            agent,
            "Please use the test tool",
            session=session,
        )

        assert "Tool result for: test query" in str(result.new_items)

        tool_usage = await session.get_tool_usage()
        assert len(tool_usage) > 0
