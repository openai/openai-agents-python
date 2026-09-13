"""Function-tool approvals, rejection decisions, and approval guardrail ordering."""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

import agents._debug as _debug
from agents._tool_identity import get_function_tool_lookup_key_for_tool
from agents.exceptions import ModelBehaviorError
from agents.realtime.agent import RealtimeAgent
from agents.realtime.events import (
    RealtimeToolApprovalRequired,
    RealtimeToolEnd,
    RealtimeToolStart,
)
from agents.realtime.model_events import (
    RealtimeModelToolCallEvent,
)
from agents.realtime.model_inputs import (
    RealtimeModelSendToolOutput,
)
from agents.realtime.session import (
    REJECTION_MESSAGE,
    RealtimeSession,
)
from agents.tool import FunctionTool, function_tool, tool_namespace
from agents.tool_context import ToolContext
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    tool_input_guardrail,
)

from . import session_test_support
from .session_test_support import (
    RecordingRealtimeModel,
    _named_function_tool,
    _sent_tool_output_strings,
)

# Bind shared fixtures explicitly so unrelated Realtime modules do not inherit them.
mock_agent = session_test_support.mock_agent
mock_function_tool = session_test_support.mock_function_tool
mock_model = session_test_support.mock_model


class TestToolCallExecution:
    """Test suite for tool call execution flow in RealtimeSession._handle_tool_call"""

    @pytest.mark.asyncio
    async def test_approval_resume_uses_pending_initial_settings_dispatch_snapshot(
        self, mock_model
    ):
        approved_tool = _named_function_tool(
            "approval_tool",
            "approved implementation",
            needs_approval=True,
        )
        replacement_tool = _named_function_tool("approval_tool", "replacement implementation")
        initial_agent = RealtimeAgent(name="initial", tools=[], handoffs=[])
        replacement_agent = RealtimeAgent(name="replacement", tools=[replacement_tool], handoffs=[])
        session = RealtimeSession(
            mock_model,
            initial_agent,
            None,
            model_config={"initial_model_settings": {"tools": [approved_tool]}},
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="approval_tool",
            call_id="call_pending_snapshot",
            arguments="{}",
        )

        await session.__aenter__()
        try:
            await session._handle_tool_call(tool_call_event)
            assert list(session._pending_tool_calls) == [tool_call_event.call_id]

            await session.update_agent(replacement_agent)
            await session.approve_tool_call(tool_call_event.call_id)

            assert _sent_tool_output_strings(mock_model) == ["approved implementation"]
        finally:
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_function_tool_needs_approval_emits_event(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Tools marked as needs_approval should pause and emit an approval request."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_needs_approval", arguments='{"param": "value"}'
        )

        await session._handle_tool_call(tool_call_event)

        assert tool_call_event.call_id in session._pending_tool_calls
        assert mock_function_tool.on_invoke_tool.call_count == 0

        approval_event = await session._event_queue.get()
        assert isinstance(approval_event, RealtimeToolApprovalRequired)
        assert approval_event.call_id == tool_call_event.call_id
        assert approval_event.tool == mock_function_tool

    @pytest.mark.parametrize(
        "arguments",
        [
            "",
            '{"subject": "refund"',
            "null",
            "[]",
            '{"amount": NaN}',
            '{"amount": Infinity}',
            '{"amount": -Infinity}',
        ],
    )
    @pytest.mark.asyncio
    async def test_callable_function_approval_fails_closed_for_invalid_arguments(
        self, mock_model, arguments: str
    ) -> None:
        approval_inputs: list[dict[str, Any]] = []
        tool_inputs: list[str] = []

        async def needs_approval(_ctx: Any, params: dict[str, Any], _call_id: str) -> bool:
            approval_inputs.append(params)
            return False

        async def invoke_tool(_ctx: ToolContext[Any], raw_arguments: str) -> str:
            tool_inputs.append(raw_arguments)
            return "sent"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=needs_approval,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        tool_call_event = RealtimeModelToolCallEvent(
            name=tool.name,
            call_id="call-invalid",
            arguments=arguments,
        )

        await session._handle_tool_call(tool_call_event)

        assert tool_call_event.call_id in session._pending_tool_calls
        assert approval_inputs == []
        assert tool_inputs == []
        approval_event = await session._event_queue.get()
        assert isinstance(approval_event, RealtimeToolApprovalRequired)

    @pytest.mark.asyncio
    async def test_callable_function_approval_receives_valid_object_arguments(
        self, mock_model
    ) -> None:
        approval_inputs: list[dict[str, Any]] = []
        tool_inputs: list[str] = []

        async def needs_approval(_ctx: Any, params: dict[str, Any], _call_id: str) -> bool:
            approval_inputs.append(params)
            return False

        async def invoke_tool(_ctx: ToolContext[Any], raw_arguments: str) -> str:
            tool_inputs.append(raw_arguments)
            return "sent"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {"subject": {"type": "string"}}},
            on_invoke_tool=invoke_tool,
            needs_approval=needs_approval,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        arguments = '{"subject": "status update"}'
        tool_call_event = RealtimeModelToolCallEvent(
            name=tool.name,
            call_id="call-valid",
            arguments=arguments,
        )

        await session._handle_tool_call(tool_call_event)

        assert approval_inputs == [{"subject": "status update"}]
        assert tool_inputs == [arguments]
        assert tool_call_event.call_id not in session._pending_tool_calls

    @pytest.mark.asyncio
    async def test_tool_input_guardrail_rejects_before_realtime_function_execution(
        self, mock_model
    ):
        """Tool input guardrails should run before regular realtime function tool execution."""
        executed = False

        @tool_input_guardrail
        def reject_guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.reject_content("blocked before execution")

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            nonlocal executed
            executed = True
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            tool_input_guardrails=[reject_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_guardrail_reject", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        assert executed is False
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "blocked before execution"
        assert start_response is True

    @pytest.mark.asyncio
    async def test_realtime_pending_approval_skips_tool_input_guardrails_by_default(
        self, mock_model
    ):
        guardrail_runs = 0

        @tool_input_guardrail
        def count_guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            nonlocal guardrail_runs
            guardrail_runs += 1
            return ToolGuardrailFunctionOutput.allow()

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
            tool_input_guardrails=[count_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_guardrail_pending", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        assert tool_call_event.call_id in session._pending_tool_calls
        assert guardrail_runs == 0

    @pytest.mark.asyncio
    async def test_realtime_pre_approval_tool_input_guardrail_rejects_pending_approval(
        self, mock_model
    ):
        executed = False

        @tool_input_guardrail
        def reject_guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.reject_content("blocked before approval")

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            nonlocal executed
            executed = True
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
            tool_input_guardrails=[reject_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={
                "async_tool_calls": False,
                "tool_execution": {"pre_approval_tool_input_guardrails": True},
            },
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_pre_approval_reject", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        assert executed is False
        assert tool_call_event.call_id not in session._pending_tool_calls
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "blocked before approval"
        assert start_response is True

    @pytest.mark.asyncio
    async def test_realtime_pre_approval_tool_input_guardrails_rerun_after_approval(
        self, mock_model
    ):
        guardrail_runs = 0
        executed = 0

        @tool_input_guardrail
        def count_guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            nonlocal guardrail_runs
            guardrail_runs += 1
            return ToolGuardrailFunctionOutput.allow()

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            nonlocal executed
            executed += 1
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
            tool_input_guardrails=[count_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={
                "async_tool_calls": False,
                "tool_execution": {"pre_approval_tool_input_guardrails": True},
            },
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_pre_approval_rerun", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        assert guardrail_runs == 1
        assert executed == 0

        await session.approve_tool_call(tool_call_event.call_id)

        assert guardrail_runs == 2
        assert executed == 1
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "ok"
        assert start_response is True

    @pytest.mark.asyncio
    async def test_duplicate_pending_approval_call_id_is_ignored_and_approval_runs_once(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """A duplicate approval-gated call should not enqueue another approval or run twice."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_duplicate_approval", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session._handle_tool_call(tool_call_event)

        changed_event = RealtimeModelToolCallEvent(
            name="test_function",
            call_id=tool_call_event.call_id,
            arguments='{"changed":true}',
        )
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)

        assert list(session._pending_tool_calls) == [tool_call_event.call_id]
        approval_events = []
        while not session._event_queue.empty():
            event = await session._event_queue.get()
            if isinstance(event, RealtimeToolApprovalRequired):
                approval_events.append(event)
        assert len(approval_events) == 1

        await session.approve_tool_call(tool_call_event.call_id)
        await session._handle_tool_call(tool_call_event)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_approve_pending_tool_call_runs_tool(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Approving a pending tool call should resume execution."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_approve", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.approve_tool_call(tool_call_event.call_id)

        assert mock_function_tool.on_invoke_tool.call_count == 1
        assert len(mock_model.sent_tool_outputs) == 1
        assert session._pending_tool_calls == {}

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        assert any(isinstance(ev, RealtimeToolStart) for ev in events)
        assert any(isinstance(ev, RealtimeToolEnd) for ev in events)

    @pytest.mark.asyncio
    async def test_async_approve_pending_tool_call_reserves_call_id_before_task_runs(
        self, mock_model
    ):
        """A duplicate event after approval should not outrun the approved async task."""
        approved_calls: list[str] = []
        duplicate_calls: list[str] = []

        async def invoke_approved_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            approved_calls.append("approved")
            return "approved_result"

        async def invoke_duplicate_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            duplicate_calls.append("duplicate")
            return "duplicate_result"

        approved_tool = FunctionTool(
            name="test_function",
            description="approved",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_approved_tool,
            needs_approval=True,
        )
        duplicate_tool = FunctionTool(
            name="test_function",
            description="duplicate",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_duplicate_tool,
            needs_approval=False,
        )
        approved_agent = RealtimeAgent(name="approved_agent", tools=[approved_tool])
        duplicate_agent = RealtimeAgent(name="duplicate_agent", tools=[duplicate_tool])
        session = RealtimeSession(mock_model, approved_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_async_approval_race", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.approve_tool_call(tool_call_event.call_id)

        assert tool_call_event.call_id in session._active_tool_invocations
        await session._handle_tool_call(tool_call_event, agent_snapshot=duplicate_agent)

        tool_call_tasks = list(session._tool_call_tasks)
        assert len(tool_call_tasks) == 1
        await asyncio.gather(*tool_call_tasks)

        assert approved_calls == ["approved"]
        assert duplicate_calls == []
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, _start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "approved_result"

    @pytest.mark.asyncio
    async def test_always_approve_namespaced_tool_call_does_not_approve_bare_tool(self, mock_model):
        """Always approval should stay scoped to the namespaced tool key."""
        tool_calls: list[str] = []

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "account"

        namespaced_tool = tool_namespace(
            name="crm",
            description="CRM tools",
            tools=[
                FunctionTool(
                    name="lookup_account",
                    description="Look up account",
                    params_json_schema={"type": "object", "properties": {}},
                    on_invoke_tool=invoke_tool,
                    needs_approval=True,
                )
            ],
        )[0]
        bare_tool = FunctionTool(
            name="lookup_account",
            description="Look up account",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
        )
        namespaced_agent = RealtimeAgent(name="crm_agent", tools=[namespaced_tool])
        bare_agent = RealtimeAgent(name="bare_agent", tools=[bare_tool])

        session = RealtimeSession(
            mock_model,
            namespaced_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        first_call = RealtimeModelToolCallEvent(
            name="lookup_account", call_id="call_first", arguments="{}"
        )
        second_call = RealtimeModelToolCallEvent(
            name="lookup_account", call_id="call_second", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        await session.approve_tool_call(first_call.call_id, always=True)
        await session._handle_tool_call(second_call, agent_snapshot=bare_agent)

        assert (
            session._context_wrapper.get_approval_status(
                "lookup_account",
                second_call.call_id,
            )
            is None
        )
        assert "crm.lookup_account" in session._context_wrapper._approvals
        assert "lookup_account" not in session._context_wrapper._approvals
        assert sorted(session._pending_tool_calls) == [second_call.call_id]
        assert len(mock_model.sent_tool_outputs) == 1
        assert tool_calls == ["called"]

    @pytest.mark.asyncio
    async def test_reject_pending_tool_call_sends_rejection_output(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Rejecting a pending tool call should notify the model and skip execution."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_reject", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.reject_tool_call(tool_call_event.call_id)
        await session._handle_tool_call(tool_call_event)

        assert mock_function_tool.on_invoke_tool.call_count == 0
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == REJECTION_MESSAGE
        assert start_response is True
        assert session._pending_tool_calls == {}

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        assert any(
            isinstance(ev, RealtimeToolEnd) and ev.output == REJECTION_MESSAGE for ev in events
        )

    @pytest.mark.asyncio
    async def test_reject_pending_tool_call_reserves_call_id_before_sending(
        self, mock_agent, mock_function_tool
    ):
        """A duplicate event during rejection output sending should not emit a second output."""

        class BlockingToolOutputModel(RecordingRealtimeModel):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.block_next_tool_output = True

            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendToolOutput) and self.block_next_tool_output:
                    self.block_next_tool_output = False
                    self.started.set()
                    await self.release.wait()
                await super().send_event(event)

        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        mock_model = BlockingToolOutputModel()
        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_reject_race", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        reject_task = asyncio.create_task(session.reject_tool_call(tool_call_event.call_id))
        await asyncio.wait_for(mock_model.started.wait(), timeout=1)

        await session._handle_tool_call(tool_call_event)

        mock_model.release.set()
        await reject_task

        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_reject_pending_tool_call_uses_run_level_formatter(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Rejecting a pending tool call should use the run-level formatter output."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={
                "tool_error_formatter": (
                    lambda args: f"run-level {args.tool_name} denied ({args.call_id})"
                )
            },
        )

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_reject_custom", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.reject_tool_call(tool_call_event.call_id)

        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "run-level test_function denied (call_reject_custom)"
        assert start_response is True

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        assert any(
            isinstance(ev, RealtimeToolEnd)
            and ev.output == "run-level test_function denied (call_reject_custom)"
            for ev in events
        )

    @pytest.mark.asyncio
    async def test_rejection_formatter_error_is_redacted(
        self, monkeypatch, mock_model, mock_agent, mock_function_tool
    ):
        monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)

        def fail_formatter(_args):
            raise ValueError("SECRET_REALTIME_TOOL_FORMATTER")

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"tool_error_formatter": fail_formatter},
        )

        with patch("agents.realtime.session.logger") as mock_logger:
            message = await session._resolve_approval_rejection_message(
                tool=mock_function_tool,
                call_id="call_reject_error",
            )

        assert message
        mock_logger.error.assert_called_once_with("%s", "Tool error formatter failed", stacklevel=3)

    @pytest.mark.asyncio
    async def test_cancelled_rejection_formatter_leaves_invocation_executed(
        self, mock_model, mock_agent
    ):
        formatter_entered = asyncio.Event()

        @function_tool
        def approval_tool() -> str:
            return "done"

        async def blocking_formatter(_args):
            formatter_entered.set()
            await asyncio.Event().wait()
            return "rejected"

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"tool_error_formatter": blocking_formatter},
        )
        tool_call = RealtimeModelToolCallEvent(
            name=approval_tool.name,
            call_id="call_rejected_cancelled",
            arguments="{}",
        )
        canonical_call = session._build_tool_approval_item(  # noqa: SLF001
            approval_tool,
            tool_call,
            mock_agent,
        ).raw_item
        lookup_key = get_function_tool_lookup_key_for_tool(approval_tool)
        assert session._context_wrapper._tool_invocation_status(  # noqa: SLF001
            canonical_call,
            tool_lookup_key=lookup_key,
        ) == (("function_call", "call_rejected_cancelled"), False, False)

        task = asyncio.create_task(
            session._resolve_approval_rejection_message(  # noqa: SLF001
                tool=approval_tool,
                call_id=tool_call.call_id,
                tool_call=canonical_call,
            )
        )
        await formatter_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session._context_wrapper._tool_invocation_status(  # noqa: SLF001
            canonical_call,
            tool_lookup_key=lookup_key,
        ) == (("function_call", "call_rejected_cancelled"), False, True)

    @pytest.mark.asyncio
    async def test_reject_pending_tool_call_prefers_explicit_message(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Rejecting a pending tool call should prefer the explicit rejection message."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={
                "tool_error_formatter": (
                    lambda args: f"run-level {args.tool_name} denied ({args.call_id})"
                )
            },
        )

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_reject_explicit", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.reject_tool_call(
            tool_call_event.call_id,
            rejection_message="explicit rejection message",
        )

        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "explicit rejection message"
        assert start_response is True

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        assert any(
            isinstance(ev, RealtimeToolEnd) and ev.output == "explicit rejection message"
            for ev in events
        )

    @pytest.mark.asyncio
    async def test_always_reject_namespaced_tool_call_reuses_explicit_message(self, mock_model):
        """Always rejection should reuse explicit messages through the qualified tool key."""
        tool_calls: list[str] = []

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "account"

        namespaced_tool = tool_namespace(
            name="crm",
            description="CRM tools",
            tools=[
                FunctionTool(
                    name="lookup_account",
                    description="Look up account",
                    params_json_schema={"type": "object", "properties": {}},
                    on_invoke_tool=invoke_tool,
                    needs_approval=True,
                )
            ],
        )[0]
        agent = RealtimeAgent(name="crm_agent", tools=[namespaced_tool])
        session = RealtimeSession(mock_model, agent, None)

        first_call = RealtimeModelToolCallEvent(
            name="lookup_account", call_id="call_reject_first", arguments="{}"
        )
        second_call = RealtimeModelToolCallEvent(
            name="lookup_account", call_id="call_reject_second", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        await session.reject_tool_call(
            first_call.call_id,
            always=True,
            rejection_message="explicit crm rejection",
        )
        await session._handle_tool_call(second_call)

        assert "crm.lookup_account" in session._context_wrapper._approvals
        assert "lookup_account" not in session._context_wrapper._approvals
        assert session._pending_tool_calls == {}
        assert [output for _call, output, _start in mock_model.sent_tool_outputs] == [
            "explicit crm rejection",
            "explicit crm rejection",
        ]
        assert tool_calls == []

    @pytest.mark.asyncio
    async def test_sticky_rejection_does_not_bind_duplicate_call_id_payload(
        self, mock_model, mock_agent, mock_function_tool
    ):
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(mock_model, mock_agent, None)
        first_call = RealtimeModelToolCallEvent(
            name="test_function", call_id="call-sticky-reject", arguments="{}"
        )
        changed_call = RealtimeModelToolCallEvent(
            name="test_function",
            call_id=first_call.call_id,
            arguments='{"changed":true}',
        )

        await session._handle_tool_call(first_call)
        await session.reject_tool_call(first_call.call_id, always=True)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_call)

        mock_function_tool.on_invoke_tool.assert_not_called()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_sticky_rejection_skips_dynamic_approval_checker(self, mock_model):
        checker_calls: list[str] = []
        tool_calls: list[str] = []

        async def needs_approval(_ctx: Any, _params: dict[str, Any], call_id: str) -> bool:
            checker_calls.append(call_id)
            if call_id != "call-reject-first":
                raise AssertionError("sticky rejection must bypass needs_approval")
            return True

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "should-not-run"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=needs_approval,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        first_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-reject-first", arguments="{}"
        )
        second_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-reject-second", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        await session.reject_tool_call(first_call.call_id, always=True)
        await session._handle_tool_call(second_call)

        assert checker_calls == ["call-reject-first"]
        assert tool_calls == []
        assert session._pending_tool_calls == {}
        assert len(mock_model.sent_tool_outputs) == 2

    @pytest.mark.asyncio
    async def test_sticky_rejection_wins_while_dynamic_approval_checker_is_pending(
        self, mock_model
    ):
        checker_started = asyncio.Event()
        checker_release = asyncio.Event()
        checker_calls: list[str] = []
        tool_calls: list[str] = []

        async def needs_approval(_ctx: Any, _params: dict[str, Any], call_id: str) -> bool:
            checker_calls.append(call_id)
            if call_id == "call-pending-checker":
                checker_started.set()
                await checker_release.wait()
                return False
            return True

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "should-not-run"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=needs_approval,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None)
        first_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-reject-first", arguments="{}"
        )
        pending_checker_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-pending-checker", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        pending_checker_task = asyncio.create_task(session._handle_tool_call(pending_checker_call))
        try:
            await asyncio.wait_for(checker_started.wait(), timeout=1)
            await session.reject_tool_call(
                first_call.call_id,
                always=True,
                rejection_message="sticky rejection",
            )
        finally:
            checker_release.set()
        await pending_checker_task

        assert checker_calls == ["call-reject-first", "call-pending-checker"]
        assert tool_calls == []
        assert session._pending_tool_calls == {}
        assert [output for _call, output, _start in mock_model.sent_tool_outputs] == [
            "sticky rejection",
            "sticky rejection",
        ]

    @pytest.mark.parametrize("approved", [True, False], ids=["approved", "rejected"])
    @pytest.mark.asyncio
    async def test_sticky_decision_wins_while_rejecting_pre_approval_guardrail_is_pending(
        self, mock_model, approved: bool
    ):
        guardrail_started = asyncio.Event()
        guardrail_release = asyncio.Event()
        guardrail_calls: list[str | None] = []
        tool_calls: list[str] = []

        @tool_input_guardrail
        async def blocking_guardrail(
            data: ToolInputGuardrailData,
        ) -> ToolGuardrailFunctionOutput:
            call_id = data.context.tool_call_id
            guardrail_calls.append(call_id)
            if call_id == "call-pending-guardrail":
                guardrail_started.set()
                await guardrail_release.wait()
                return ToolGuardrailFunctionOutput.reject_content("guardrail rejection")
            return ToolGuardrailFunctionOutput.allow()

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "tool output"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
            tool_input_guardrails=[blocking_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={"tool_execution": {"pre_approval_tool_input_guardrails": True}},
        )
        first_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-reject-first", arguments="{}"
        )
        pending_guardrail_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-pending-guardrail", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        pending_guardrail_task = asyncio.create_task(
            session._handle_tool_call(pending_guardrail_call)
        )
        try:
            await asyncio.wait_for(guardrail_started.wait(), timeout=1)
            approval_item = session._pending_tool_calls[first_call.call_id].approval_item
            if approved:
                session._context_wrapper.approve_tool(approval_item, always_approve=True)
            else:
                session._context_wrapper.reject_tool(
                    approval_item,
                    always_reject=True,
                    rejection_message="sticky rejection",
                )
        finally:
            guardrail_release.set()
        await pending_guardrail_task

        assert pending_guardrail_call.call_id not in session._pending_tool_calls
        outputs = [output for _call, output, _start in mock_model.sent_tool_outputs]
        if approved:
            assert guardrail_calls == [
                "call-reject-first",
                "call-pending-guardrail",
                "call-pending-guardrail",
            ]
            assert tool_calls == []
            assert outputs == ["guardrail rejection"]
        else:
            assert guardrail_calls == ["call-reject-first", "call-pending-guardrail"]
            assert tool_calls == []
            assert outputs == ["sticky rejection"]
