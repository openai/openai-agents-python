"""
Tests for the A2A ↔ OpenAI Agents SDK converter module.

These tests use inline-constructed protobuf objects to avoid requiring a live
A2A server. They verify the bidirectional conversion fidelity for messages,
task history, artifacts, and streaming events.
"""

from __future__ import annotations

import json
import uuid

import pytest


def _make_part_text(text: str) -> "Part":
    """Create an A2A text Part."""
    from a2a.types.a2a_pb2 import Part

    p = Part(text=text)
    p.media_type = "text/plain"
    return p


def _make_part_url(url: str) -> "Part":
    """Create an A2A URL Part."""
    from a2a.types.a2a_pb2 import Part

    p = Part()
    p.url = url
    return p


def _make_part_data(data: dict) -> "Part":
    """Create an A2A data Part."""
    from a2a.types.a2a_pb2 import Part

    from google.protobuf.struct_pb2 import Value

    p = Part()
    json_str = json.dumps(data)
    p.media_type = "application/json"
    p.text = json_str
    return p


def _make_message(
    *,
    role: int = 1,
    text: str | None = None,
    parts: list["Part"] | None = None,
    message_id: str | None = None,
) -> "Message":
    """Create an A2A Message with sensible defaults."""
    from a2a.types.a2a_pb2 import Message

    if parts is None and text is not None:
        parts = [_make_part_text(text)]
    if parts is None:
        parts = []

    return Message(
        message_id=message_id or f"msg-{uuid.uuid4().hex[:12]}",
        role=role,
        parts=parts,
    )


def _make_run_result(
    final_output: object = "hello world",
    *,
    new_items: list | None = None,
    last_agent_name: str = "test_agent",
) -> "RunResult":
    """Create a minimal RunResult for testing."""
    from unittest.mock import MagicMock

    from agents.result import RunResult

    result = MagicMock(spec=RunResult)
    result.final_output = final_output
    result.new_items = new_items or []
    result._last_agent = MagicMock()
    result._last_agent.name = last_agent_name
    return result


# ---------------------------------------------------------------------------
# A2A → OpenAI tests
# ---------------------------------------------------------------------------


class TestA2AMessageToOpenAIInput:
    """Tests for a2a_message_to_openai_input_items."""

    def test_text_message_user_role(self):
        from agents.extensions.a2a._converter import a2a_message_to_openai_input_items

        msg = _make_message(role=1, text="Hello, agent!")
        items = a2a_message_to_openai_input_items(msg)

        assert len(items) == 1
        assert items[0]["role"] == "user"
        assert items[0]["content"] == "Hello, agent!"

    def test_text_message_agent_role(self):
        from agents.extensions.a2a._converter import a2a_message_to_openai_input_items

        msg = _make_message(role=2, text="Here is the answer.")
        items = a2a_message_to_openai_input_items(msg)

        assert len(items) == 1
        assert items[0]["role"] == "assistant"
        assert items[0]["content"] == "Here is the answer."

    def test_message_without_role_inclusion(self):
        from agents.extensions.a2a._converter import a2a_message_to_openai_input_items

        msg = _make_message(role=1, text="No role please")
        items = a2a_message_to_openai_input_items(msg, include_role=False)

        assert len(items) == 1
        assert "role" not in items[0]
        assert items[0]["content"] == "No role please"

    def test_message_with_url_part(self):
        from agents.extensions.a2a._converter import a2a_message_to_openai_input_items

        msg = _make_message(parts=[_make_part_url("https://example.com/report.pdf")])
        items = a2a_message_to_openai_input_items(msg)

        assert len(items) == 1
        assert "URL" in items[0]["content"]

    def test_message_with_multiple_parts(self):
        from agents.extensions.a2a._converter import a2a_message_to_openai_input_items

        msg = _make_message(
            parts=[
                _make_part_text("First part"),
                _make_part_text("Second part"),
            ]
        )
        items = a2a_message_to_openai_input_items(msg)

        assert len(items) == 2
        assert items[0]["content"] == "First part"
        assert items[1]["content"] == "Second part"

    def test_empty_message(self):
        from agents.extensions.a2a._converter import a2a_message_to_openai_input_items

        msg = _make_message(parts=[])
        items = a2a_message_to_openai_input_items(msg)

        assert items == []

    def test_unspecified_role_defaults_to_user(self):
        from agents.extensions.a2a._converter import a2a_message_to_openai_input_items

        msg = _make_message(role=0, text="Unspecified role")
        items = a2a_message_to_openai_input_items(msg)

        assert items[0]["role"] == "user"


class TestA2AHistoryToOpenAIInput:
    """Tests for a2a_history_to_openai_input_items."""

    def test_converts_history_preserving_roles(self):
        from agents.extensions.a2a._converter import a2a_history_to_openai_input_items

        history = [
            _make_message(role=1, text="User query"),
            _make_message(role=2, text="Agent response"),
            _make_message(role=1, text="Follow-up"),
        ]
        items = a2a_history_to_openai_input_items(history)

        assert len(items) == 3
        assert items[0]["role"] == "user"
        assert items[0]["content"] == "User query"
        assert items[1]["role"] == "assistant"
        assert items[1]["content"] == "Agent response"
        assert items[2]["role"] == "user"
        assert items[2]["content"] == "Follow-up"

    def test_empty_history(self):
        from agents.extensions.a2a._converter import a2a_history_to_openai_input_items

        assert a2a_history_to_openai_input_items([]) == []


# ---------------------------------------------------------------------------
# OpenAI → A2A tests
# ---------------------------------------------------------------------------


class TestOpenAIFinalOutputToArtifacts:
    """Tests for openai_final_output_to_artifacts."""

    def test_string_output(self):
        from agents.extensions.a2a._converter import openai_final_output_to_artifacts

        artifacts = openai_final_output_to_artifacts("result text")

        assert len(artifacts) == 1
        assert artifacts[0].name == "output"
        assert len(artifacts[0].parts) == 1
        assert artifacts[0].parts[0].text == "result text"
        assert artifacts[0].parts[0].media_type == "text/plain"

    def test_none_output(self):
        from agents.extensions.a2a._converter import openai_final_output_to_artifacts

        assert openai_final_output_to_artifacts(None) == []

    def test_dict_output(self):
        from agents.extensions.a2a._converter import openai_final_output_to_artifacts

        artifacts = openai_final_output_to_artifacts({"key": "value"})

        assert len(artifacts) == 1
        text = artifacts[0].parts[0].text
        parsed = json.loads(text)
        assert parsed == {"key": "value"}

    def test_custom_artifact_id(self):
        from agents.extensions.a2a._converter import openai_final_output_to_artifacts

        artifacts = openai_final_output_to_artifacts(
            "data", artifact_id="custom-id", artifact_name="report"
        )

        assert artifacts[0].artifact_id == "custom-id"
        assert artifacts[0].name == "report"

    def test_int_output_stringified(self):
        from agents.extensions.a2a._converter import openai_final_output_to_artifacts

        artifacts = openai_final_output_to_artifacts(42)

        assert len(artifacts) == 1
        assert "42" in artifacts[0].parts[0].text


class TestOpenAIItemsToA2AMessages:
    """Tests for openai_items_to_a2a_messages."""

    def test_user_item(self):
        from agents.extensions.a2a._converter import openai_items_to_a2a_messages

        items = [{"role": "user", "content": "User message"}]
        messages = openai_items_to_a2a_messages(items)

        assert len(messages) == 1
        assert messages[0].role == 1  # USER
        assert messages[0].parts[0].text == "User message"

    def test_assistant_item(self):
        from agents.extensions.a2a._converter import openai_items_to_a2a_messages

        items = [{"role": "assistant", "content": "Agent reply"}]
        messages = openai_items_to_a2a_messages(items)

        assert len(messages) == 1
        assert messages[0].role == 2  # AGENT

    def test_with_context_and_task_ids(self):
        from agents.extensions.a2a._converter import openai_items_to_a2a_messages

        items = [{"role": "user", "content": "Hi"}]
        messages = openai_items_to_a2a_messages(
            items, context_id="ctx-1", task_id="task-1"
        )

        assert messages[0].context_id == "ctx-1"
        assert messages[0].task_id == "task-1"

    def test_empty_items(self):
        from agents.extensions.a2a._converter import openai_items_to_a2a_messages

        assert openai_items_to_a2a_messages([]) == []

    def test_item_with_none_content_skipped(self):
        from agents.extensions.a2a._converter import openai_items_to_a2a_messages

        items = [{"role": "user", "content": None}]  # type: ignore[dict-item]
        messages = openai_items_to_a2a_messages(items)

        assert messages == []


class TestOpenAIRunResultToTask:
    """Tests for openai_run_result_to_task."""

    def test_builds_completed_task(self):
        from agents.extensions.a2a._converter import openai_run_result_to_task

        result = _make_run_result(
            final_output="done",
            new_items=[
                {"role": "user", "content": "query"},
                {"role": "assistant", "content": "response"},
            ],
        )
        task = openai_run_result_to_task(result, task_id="task-abc")

        assert task.id == "task-abc"
        assert task.status.state == 3  # TASK_STATE_COMPLETED
        assert len(task.artifacts) == 1
        assert task.artifacts[0].parts[0].text == "done"
        assert len(task.history) == 2

    def test_task_with_context_id(self):
        from agents.extensions.a2a._converter import openai_run_result_to_task

        result = _make_run_result(final_output="ok")
        task = openai_run_result_to_task(
            result, task_id="t1", context_id="ctx-42"
        )

        assert task.context_id == "ctx-42"


class TestOpenAIErrorToFailedTask:
    """Tests for openai_error_to_failed_task."""

    def test_builds_failed_task(self):
        from agents.extensions.a2a._converter import openai_error_to_failed_task

        task = openai_error_to_failed_task(
            ValueError("something went wrong"), task_id="task-fail"
        )

        assert task.id == "task-fail"
        assert task.status.state == 4  # TASK_STATE_FAILED
        assert "something went wrong" in task.status.message.parts[0].text


# ---------------------------------------------------------------------------
# Round-trip fidelity tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """End-to-end conversion fidelity tests."""

    def test_text_message_round_trip(self):
        """A2A text Message → OpenAI items → A2A Messages should preserve text."""
        from agents.extensions.a2a._converter import (
            a2a_message_to_openai_input_items,
            openai_items_to_a2a_messages,
        )

        original = _make_message(role=1, text="Hello, world!")
        items = a2a_message_to_openai_input_items(original)
        restored = openai_items_to_a2a_messages(items)

        assert len(restored) == 1
        assert restored[0].parts[0].text == "Hello, world!"

    def test_multiturn_conversation_round_trip(self):
        """A full conversation should survive the round-trip."""
        from agents.extensions.a2a._converter import (
            a2a_history_to_openai_input_items,
            openai_items_to_a2a_messages,
        )

        history = [
            _make_message(role=1, text="What is 2+2?"),
            _make_message(role=2, text="4"),
            _make_message(role=1, text="Thanks!"),
        ]
        items = a2a_history_to_openai_input_items(history)
        restored = openai_items_to_a2a_messages(items)

        assert len(restored) == 3
        assert restored[0].parts[0].text == "What is 2+2?"
        assert restored[0].role == 1  # USER
        assert restored[1].parts[0].text == "4"
        assert restored[1].role == 2  # AGENT
        assert restored[2].parts[0].text == "Thanks!"
        assert restored[2].role == 1  # USER


# ---------------------------------------------------------------------------
# Streaming event tests
# ---------------------------------------------------------------------------


class TestStreamEventConversion:
    """Tests for openai_stream_event_to_task_status."""

    def test_run_item_event_returns_working_status(self):
        import dataclasses

        from agents.stream_events import RunItemStreamEvent

        from agents.extensions.a2a._converter import openai_stream_event_to_task_status

        # Create a minimal RunItem mock
        class FakeRunItem:
            type = "message_output_item"

        event = RunItemStreamEvent(
            name="message_output_created",
            item=FakeRunItem(),  # type: ignore[arg-type]
        )
        status = openai_stream_event_to_task_status(event, task_id="task-s1")

        assert status is not None
        assert status.state == 2  # TASK_STATE_WORKING

    def test_agent_updated_event_returns_working_status(self):
        from unittest.mock import MagicMock

        from agents.stream_events import AgentUpdatedStreamEvent

        from agents.extensions.a2a._converter import openai_stream_event_to_task_status

        agent = MagicMock()
        agent.name = "new_agent"
        event = AgentUpdatedStreamEvent(new_agent=agent)
        status = openai_stream_event_to_task_status(event, task_id="task-s2")

        assert status is not None
        assert status.state == 2  # TASK_STATE_WORKING
        assert "new_agent" in status.message.parts[0].text

    def test_raw_event_returns_none(self):
        from agents.stream_events import RawResponsesStreamEvent

        from agents.extensions.a2a._converter import openai_stream_event_to_task_status

        event = RawResponsesStreamEvent(data={"type": "response.output_text.delta"})  # type: ignore[arg-type]
        status = openai_stream_event_to_task_status(event, task_id="task-s3")

        assert status is None
