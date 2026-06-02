"""
Tests for the A2AClientTool class.

Uses a mock/fake A2A client so tests run without a live server. The fake
client implements the exact ``Client`` interface, enabling deterministic
verification of request construction and result extraction.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agents.run_context import RunContextWrapper


# ---------------------------------------------------------------------------
# Fake A2A Client — implements the same async interface
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal stream response wrapper for faking send_message."""

    def __init__(self, task: Any = None, status_update: Any = None):
        self._task = task
        self._status_update = status_update

    def WhichOneof(self, name: str) -> str | None:  # noqa: N802
        if self._task is not None:
            return "task"
        if self._status_update is not None:
            return "task_status_update_event"
        return None

    @property
    def task(self) -> Any:
        return self._task

    @property
    def task_status_update_event(self) -> Any:
        return self._status_update


class FakeA2AClient:
    """A fake A2A Client that replays pre-configured responses."""

    def __init__(self, responses: list[Any] | None = None):
        self._responses: list[Any] = responses or []
        self.send_message_calls: list[Any] = []
        self.get_task_calls: list[Any] = []
        self.cancel_task_calls: list[Any] = []

    async def send_message(
        self, request: Any, *, context: Any = None
    ) -> AsyncIterator[Any]:
        self.send_message_calls.append(request)
        for response in self._responses:
            yield _FakeStreamResponse(task=response)

    async def get_task(self, request: Any, *, context: Any = None) -> Any:
        self.get_task_calls.append(request)
        if self._responses:
            return self._responses[-1]
        raise RuntimeError("No responses configured")

    async def cancel_task(self, request: Any, *, context: Any = None) -> Any:
        self.cancel_task_calls.append(request)
        return None

    async def close(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers for building fake A2A protobuf messages
# ---------------------------------------------------------------------------


def _fake_completed_task(task_id: str, text: str) -> Any:
    """Build a fake completed A2A Task with a text artifact."""
    from a2a.types.a2a_pb2 import Artifact, Message, Part, Task, TaskState, TaskStatus

    from google.protobuf.timestamp_pb2 import Timestamp

    artifact = Artifact(
        artifact_id=f"art-{uuid.uuid4().hex[:8]}",
        name="output",
        parts=[_text_part(text)],
    )

    timestamp = Timestamp()
    timestamp.GetCurrentTime()

    status = TaskStatus(
        state=TaskState.TASK_STATE_COMPLETED,
        message=Message(
            message_id=f"status-{uuid.uuid4().hex[:8]}",
            role=2,
            parts=[_text_part("Task completed.")],
        ),
        timestamp=timestamp,
    )

    return Task(
        id=task_id,
        status=status,
        artifacts=[artifact],
        history=[
            Message(
                message_id=f"msg-{uuid.uuid4().hex[:8]}",
                role=1,  # USER
                parts=[_text_part("user query")],
            ),
            Message(
                message_id=f"msg-{uuid.uuid4().hex[:8]}",
                role=2,  # AGENT
                parts=[_text_part(text)],
            ),
        ],
    )


def _fake_failed_task(task_id: str, error_msg: str) -> Any:
    """Build a fake failed A2A Task."""
    from a2a.types.a2a_pb2 import Message, Task, TaskState, TaskStatus

    from google.protobuf.timestamp_pb2 import Timestamp

    timestamp = Timestamp()
    timestamp.GetCurrentTime()

    status = TaskStatus(
        state=TaskState.TASK_STATE_FAILED,
        message=Message(
            message_id=f"status-{uuid.uuid4().hex[:8]}",
            role=2,
            parts=[_text_part(error_msg)],
        ),
        timestamp=timestamp,
    )

    return Task(id=task_id, status=status)


def _text_part(text: str) -> Any:
    """Create an A2A text Part."""
    from a2a.types.a2a_pb2 import Part

    p = Part(text=text)
    p.media_type = "text/plain"
    return p


def _minimal_card() -> Any:
    """Create a minimal AgentCard for testing."""
    from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface

    return AgentCard(
        name="test_agent",
        description="A test agent",
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="jsonrpc",
                url="http://localhost:9999",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestA2AClientToolConstruction:
    """Tests for constructing A2AClientTool instances."""

    def test_from_card_sync(self):
        from agents.extensions.a2a._client_tool import A2AClientTool

        tool = A2AClientTool.from_card(
            card=_minimal_card(),
            tool_name="test_tool",
            tool_description="Test tool description",
        )
        assert tool.tool_name == "test_tool"
        assert tool.tool_description == "Test tool description"
        assert tool.agent_card is not None

    def test_missing_card_and_url_raises(self):
        from agents.extensions.a2a._client_tool import A2AClientTool

        with pytest.raises(ValueError, match="agent_card.*agent_card_url"):
            A2AClientTool(
                tool_name="bad",
                tool_description="bad",
            )

    def test_as_function_tool_returns_valid_tool(self):
        from agents.extensions.a2a._client_tool import A2AClientTool

        tool = A2AClientTool.from_card(
            card=_minimal_card(),
            tool_name="test_tool",
            tool_description="Test tool",
        )
        ft = tool.as_function_tool()
        assert ft.name == "test_tool"
        assert ft.description == "Test tool"
        assert "message" in str(ft.params_json_schema)


class TestA2AClientToolInvocation:
    """Tests for the tool invocation path with a fake client."""

    async def test_successful_call_extracts_artifact_text(self):
        from agents.extensions.a2a._client_tool import A2AClientTool

        task = _fake_completed_task("task-1", "The answer is 42.")

        tool = A2AClientTool.from_card(
            card=_minimal_card(),
            tool_name="math_agent",
            tool_description="Does math",
        )
        fake_client = FakeA2AClient([task])
        tool._client = fake_client

        result = await tool._invoke_impl(
            RunContextWrapper(context=None),
            '{"message": "What is the answer?"}',
        )

        assert "The answer is 42." in result
        assert len(fake_client.send_message_calls) == 1

    async def test_failed_task_raises_model_behavior_error(self):
        from agents.exceptions import ModelBehaviorError
        from agents.extensions.a2a._client_tool import A2AClientTool

        task = _fake_failed_task("task-fail", "Something went wrong")

        tool = A2AClientTool.from_card(
            card=_minimal_card(),
            tool_name="unreliable",
            tool_description="Fails sometimes",
        )
        fake_client = FakeA2AClient([task])
        tool._client = fake_client

        with pytest.raises(ModelBehaviorError, match="Something went wrong"):
            await tool._invoke_impl(
                RunContextWrapper(context=None),
                '{"message": "Do something"}',
            )

    async def test_timeout_cancels_remote_task(self):
        """
        When the request times out, the tool should attempt to cancel the
        remote task before raising.
        """
        from agents.extensions.a2a._client_tool import A2AClientTool

        # Build a fake client whose send_message never yields a completed task
        class NeverFinishesClient:
            async def send_message(self, request, *, context=None):
                while True:
                    await asyncio.sleep(0.1)
                    yield _FakeStreamResponse()
                return  # pragma: no cover

            async def get_task(self, request, *, context=None):
                raise RuntimeError("not reached")

            async def cancel_task(self, request, *, context=None):
                pass

            async def close(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        tool = A2AClientTool.from_card(
            card=_minimal_card(),
            tool_name="slow_agent",
            tool_description="Too slow",
            timeout_seconds=0.5,
        )
        tool._client = NeverFinishesClient()

        with pytest.raises(Exception):  # ModelBehaviorError or TimeoutError
            await tool._invoke_impl(
                RunContextWrapper(context=None),
                '{"message": "Do something slow"}',
            )

    async def test_context_id_is_propagated_to_request(self):
        from agents.extensions.a2a._client_tool import A2AClientTool

        task = _fake_completed_task("task-ctx", "Done with context")

        tool = A2AClientTool.from_card(
            card=_minimal_card(),
            tool_name="context_agent",
            tool_description="Uses context",
        )
        fake_client = FakeA2AClient([task])
        tool._client = fake_client

        await tool._invoke_impl(
            RunContextWrapper(context=None),
            '{"message": "Continue conversation", "context_id": "ctx-42"}',
        )

        request = fake_client.send_message_calls[0]
        assert request.message.context_id == "ctx-42"
