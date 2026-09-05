"""Function-tool output serialization, delivery, and retry ownership."""

import asyncio
import dataclasses
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import BaseModel, ConfigDict

from agents.exceptions import ModelBehaviorError
from agents.handoffs import Handoff
from agents.realtime.agent import RealtimeAgent
from agents.realtime.events import (
    RealtimeToolEnd,
)
from agents.realtime.model_events import (
    RealtimeModelToolCallEvent,
)
from agents.realtime.model_inputs import (
    RealtimeModelSendToolOutput,
)
from agents.realtime.session import (
    RealtimeSession,
    _serialize_tool_output,
)
from agents.tool import FunctionTool

from . import session_test_support
from .session_test_support import RecordingRealtimeModel, _set_default_timeout_fields

# Bind shared fixtures explicitly so unrelated Realtime modules do not inherit them.
mock_agent = session_test_support.mock_agent
mock_function_tool = session_test_support.mock_function_tool
mock_model = session_test_support.mock_model


class TestToolCallExecution:
    """Test suite for tool call execution flow in RealtimeSession._handle_tool_call"""

    @pytest.mark.asyncio
    async def test_approved_function_tool_failure_replay_does_not_rerun(
        self, mock_model, mock_agent, mock_function_tool
    ):
        mock_function_tool.needs_approval = True
        mock_function_tool.on_invoke_tool.side_effect = RuntimeError("failed after side effect")
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_failed", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        with pytest.raises(RuntimeError, match="failed after side effect"):
            await session.approve_tool_call(tool_call_event.call_id)

        with pytest.raises(ModelBehaviorError, match="already executed"):
            await session._handle_tool_call(tool_call_event)

        mock_function_tool.on_invoke_tool.assert_awaited_once()
        assert len(mock_model.sent_tool_outputs) == 0

    @pytest.mark.parametrize("always", [False, True], ids=["per-call", "sticky"])
    @pytest.mark.parametrize("changed_field", ["arguments", "tool_name"])
    @pytest.mark.asyncio
    async def test_function_tool_send_failure_retries_cached_output_without_rerun(
        self,
        mock_agent,
        mock_function_tool,
        always: bool,
        changed_field: str,
    ):
        """An approved call should retry cached output only for the same invocation."""

        class FailingToolOutputModel(RecordingRealtimeModel):
            def __init__(self):
                super().__init__()
                self.fail_next_tool_output = True

            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendToolOutput) and self.fail_next_tool_output:
                    self.fail_next_tool_output = False
                    raise RuntimeError("send failed")
                await super().send_event(event)

        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        mock_model = FailingToolOutputModel()
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_retry_output", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        with pytest.raises(RuntimeError, match="send failed"):
            await session.approve_tool_call(tool_call_event.call_id, always=always)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 0

        changed_event = RealtimeModelToolCallEvent(
            name="other_function" if changed_field == "tool_name" else tool_call_event.name,
            call_id=tool_call_event.call_id,
            arguments=(
                tool_call_event.arguments if changed_field == "tool_name" else '{"changed":true}'
            ),
        )
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)
        await session._handle_tool_call(tool_call_event)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_tool_end_cancellation_after_output_send_does_not_resend(
        self, mock_model, mock_agent, mock_function_tool
    ) -> None:
        """Provider delivery commits the output before local end-event publication."""
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function",
            call_id="call_tool_end_cancelled",
            arguments="{}",
        )
        original_put_event_nowait = session._put_event_nowait

        def cancel_tool_end(event: Any) -> bool:
            if isinstance(event, RealtimeToolEnd):
                raise asyncio.CancelledError
            return original_put_event_nowait(event)

        session._put_event_nowait = cancel_tool_end  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await session._handle_tool_call(tool_call_event)

        invocation = session._context_wrapper._tool_invocations[tool_call_event.call_id]
        assert invocation.executed is True
        assert invocation.completed is True
        assert tool_call_event.call_id not in session._pending_tool_outputs
        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

        session._put_event_nowait = original_put_event_nowait  # type: ignore[method-assign]
        await session._handle_tool_call(tool_call_event)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.parametrize("always", [False, True], ids=["per-call", "sticky"])
    @pytest.mark.parametrize("changed_field", ["arguments", "tool_name"])
    @pytest.mark.asyncio
    async def test_async_function_tool_send_failure_retries_cached_output_without_rerun(
        self,
        mock_agent,
        mock_function_tool,
        always: bool,
        changed_field: str,
    ):
        """The async approval path should bind retries to the original invocation."""

        class FailingToolOutputModel(RecordingRealtimeModel):
            def __init__(self):
                super().__init__()
                self.fail_next_tool_output = True

            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendToolOutput) and self.fail_next_tool_output:
                    self.fail_next_tool_output = False
                    raise RuntimeError("send failed")
                await super().send_event(event)

        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        mock_model = FailingToolOutputModel()
        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_async_retry_output", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.approve_tool_call(tool_call_event.call_id, always=always)
        tool_call_tasks = list(session._tool_call_tasks)
        assert len(tool_call_tasks) == 1
        task_results = await asyncio.gather(*tool_call_tasks, return_exceptions=True)
        await asyncio.sleep(0)

        assert len(task_results) == 1
        assert isinstance(task_results[0], RuntimeError)
        assert session._stored_exception is None
        assert tool_call_event.call_id in session._pending_tool_outputs
        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 0

        changed_event = RealtimeModelToolCallEvent(
            name="other_function" if changed_field == "tool_name" else tool_call_event.name,
            call_id=tool_call_event.call_id,
            arguments=(
                tool_call_event.arguments if changed_field == "tool_name" else '{"changed":true}'
            ),
        )
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)
        await session.on_event(tool_call_event)
        tool_call_tasks = list(session._tool_call_tasks)
        assert len(tool_call_tasks) == 1
        await asyncio.gather(*tool_call_tasks)

        assert session._stored_exception is None
        assert tool_call_event.call_id not in session._pending_tool_outputs
        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_pending_function_output_rejects_handoff_role_reuse(self):
        class FailingToolOutputModel(RecordingRealtimeModel):
            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendToolOutput):
                    raise RuntimeError("send failed")
                await super().send_event(event)

        function_callback = AsyncMock(return_value="function result")
        function_tool = FunctionTool(
            name="route",
            description="Run a function.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=function_callback,
        )
        function_agent = RealtimeAgent(name="function", tools=[function_tool])
        target = RealtimeAgent(name="target")
        route_name = Handoff.default_tool_name(target)
        function_tool.name = route_name
        handoff_agent = RealtimeAgent(name="handoff", handoffs=[target])
        session = RealtimeSession(
            FailingToolOutputModel(),
            function_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        event = RealtimeModelToolCallEvent(name=route_name, call_id="shared", arguments="{}")

        with pytest.raises(RuntimeError, match="send failed"):
            await session._handle_tool_call(event)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(event, agent_snapshot=handoff_agent)

        function_callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_async_exact_function_retry_after_serialization_failure_does_not_repeat_callback(
        self,
        mock_model,
    ):
        callback = AsyncMock(return_value={"result": "ok"})
        tool = FunctionTool(
            name="run_function",
            description="Run a function.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=callback,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None)
        event = RealtimeModelToolCallEvent(
            name=tool.name,
            call_id="shared",
            arguments="{}",
        )

        with patch(
            "agents.realtime.session._serialize_tool_output",
            side_effect=RuntimeError("serialization failed"),
        ):
            await session.on_event(event)
            first_results = await asyncio.gather(
                *list(session._tool_call_tasks),
                return_exceptions=True,
            )

        await session.on_event(event)
        retry_results = await asyncio.gather(
            *list(session._tool_call_tasks),
            return_exceptions=True,
        )

        assert any(
            isinstance(result, RuntimeError) and str(result) == "serialization failed"
            for result in first_results
        )
        assert any(isinstance(result, ModelBehaviorError) for result in retry_results)
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tool_result_conversion_to_string(self, mock_model, mock_agent):
        """Test that structured tool results are serialized to JSON for model output."""
        # Create tool that returns non-string result
        tool = _set_default_timeout_fields(Mock(spec=FunctionTool))
        tool.name = "test_function"
        tool.on_invoke_tool = AsyncMock(return_value={"result": "data", "count": 42})
        tool.needs_approval = False

        mock_agent.get_all_tools.return_value = [tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_conversion", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Verify result was serialized to JSON
        sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert isinstance(sent_output, str)
        assert sent_output == json.dumps({"result": "data", "count": 42})

    @pytest.mark.asyncio
    async def test_tool_result_conversion_serializes_pydantic_models(self, mock_model, mock_agent):
        """Test that pydantic tool results are serialized to JSON for model output."""

        class ToolResult(BaseModel):
            name: str
            score: int

        tool = _set_default_timeout_fields(Mock(spec=FunctionTool))
        tool.name = "test_function"
        tool.on_invoke_tool = AsyncMock(return_value=ToolResult(name="demo", score=7))
        tool.needs_approval = False

        mock_agent.get_all_tools.return_value = [tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_pydantic_conversion", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        _sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert sent_output == json.dumps({"name": "demo", "score": 7})

    def test_serialize_tool_output_ignores_non_pydantic_model_dump_objects(self) -> None:
        class ModelDumpObject:
            def model_dump(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
                raise AssertionError("non-pydantic objects should not use model_dump")

            def __str__(self) -> str:
                return "fake-model-dump-object"

        assert _serialize_tool_output(ModelDumpObject()) == "fake-model-dump-object"

    def test_serialize_tool_output_falls_back_when_pydantic_json_dump_fails(self) -> None:
        class FallbackModel(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)

            payload: object

            def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                if kwargs.get("mode") == "json":
                    raise ValueError("json mode failed")
                return {"payload": "ok"}

        assert _serialize_tool_output(FallbackModel(payload=object())) == json.dumps(
            {"payload": "ok"}
        )

    def test_serialize_tool_output_returns_string_when_pydantic_dump_fails(self) -> None:
        class BrokenModel(BaseModel):
            value: int

            def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                raise ValueError("dump failed")

            def __str__(self) -> str:
                return "broken-model"

        assert _serialize_tool_output(BrokenModel(value=1)) == "broken-model"

    def test_serialize_tool_output_returns_string_when_dataclass_asdict_fails(self) -> None:
        @dataclasses.dataclass
        class BrokenDataclass:
            lock: Any

            def __str__(self) -> str:
                return "broken-dataclass"

        assert _serialize_tool_output(BrokenDataclass(lock=threading.Lock())) == "broken-dataclass"

    @dataclasses.dataclass
    class ToolResult:
        label: str
        values: list[int]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(None, "null", id="none"),
            pytest.param(
                ["hello", 1, True, None],
                json.dumps(["hello", 1, True, None]),
                id="list",
            ),
            pytest.param(
                ToolResult(label="demo", values=[1, 2]),
                json.dumps({"label": "demo", "values": [1, 2]}),
                id="dataclass",
            ),
            pytest.param(b"abc", "b'abc'", id="bytes"),
        ],
    )
    def test_serialize_tool_output_edge_cases(self, value: Any, expected: str) -> None:
        assert _serialize_tool_output(value) == expected
