"""Response-scoped output guardrails and playback interruption."""

import asyncio
import logging

import pytest

import agents._debug as _debug
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail
from agents.realtime.agent import RealtimeAgent
from agents.realtime.config import RealtimeRunConfig
from agents.realtime.events import (
    RealtimeAgentEndEvent,
    RealtimeAudio,
    RealtimeError,
    RealtimeGuardrailTripped,
)
from agents.realtime.model import RealtimeModel
from agents.realtime.model_events import (
    RealtimeModelAudioEvent,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelTranscriptDeltaEvent,
    RealtimeModelTurnEndedEvent,
    RealtimeModelTurnStartedEvent,
)
from agents.realtime.model_inputs import (
    RealtimeModelSendInterrupt,
    RealtimeModelSendUserInput,
)
from agents.realtime.session import (
    RealtimeSession,
)

from . import session_test_support
from .session_test_support import RecordingRealtimeModel

# Bind shared fixtures explicitly so unrelated Realtime modules do not inherit them.
mock_agent = session_test_support.mock_agent
mock_model = session_test_support.mock_model


class TestGuardrailFunctionality:
    """Test suite for output guardrail functionality in RealtimeSession"""

    async def _wait_for_guardrail_tasks(self, session):
        """Wait for all pending guardrail tasks to complete."""
        import asyncio

        if session._guardrail_tasks:
            await asyncio.gather(*session._guardrail_tasks, return_exceptions=True)

    @pytest.fixture
    def triggered_guardrail(self):
        """Creates a guardrail that always triggers"""

        def guardrail_func(context, agent, output):
            return GuardrailFunctionOutput(
                output_info={"reason": "test trigger"}, tripwire_triggered=True
            )

        return OutputGuardrail(guardrail_function=guardrail_func, name="triggered_guardrail")

    @pytest.fixture
    def safe_guardrail(self):
        """Creates a guardrail that never triggers"""

        def guardrail_func(context, agent, output):
            return GuardrailFunctionOutput(
                output_info={"reason": "safe content"}, tripwire_triggered=False
            )

        return OutputGuardrail(guardrail_function=guardrail_func, name="safe_guardrail")

    @pytest.mark.parametrize(
        ("model_redacted", "tool_redacted"),
        [(True, False), (False, True), (False, False)],
        ids=["model_redacted", "tool_redacted", "diagnostic"],
    )
    @pytest.mark.asyncio
    async def test_output_guardrail_failure_follows_both_data_policies(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        mock_model: RealtimeModel,
        model_redacted: bool,
        tool_redacted: bool,
    ) -> None:
        error = RuntimeError("SECRET_REALTIME_GUARDRAIL_ERROR")

        async def failing_guardrail(context, agent, output):
            _ = context, agent, output
            raise error

        guardrail = OutputGuardrail(
            guardrail_function=failing_guardrail,
            name="SECRET_REALTIME_GUARDRAIL_NAME",
        )
        agent = RealtimeAgent(name="agent", output_guardrails=[guardrail])
        session = RealtimeSession(mock_model, agent, None)
        monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
        monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)

        with caplog.at_level(logging.DEBUG, logger="openai.agents"):
            triggered = await session._run_output_guardrails("model text", "response-id")

        assert triggered is False
        records = [
            record
            for record in caplog.records
            if "Output guardrail raised an exception" in record.getMessage()
        ]
        assert len(records) == 1
        record = records[0]
        redacted = model_redacted or tool_redacted
        if redacted:
            assert record.msg == "%s"
            assert record.args == ("Output guardrail raised an exception; skipping it",)
            assert record.exc_info is None
            assert record.exc_text is None
            assert "openai_agents_diagnostic_context" not in record.__dict__
            assert error not in record.__dict__.values()
            rendered = logging.Formatter().format(record)
            assert "SECRET_REALTIME_GUARDRAIL_ERROR" not in rendered
            assert "SECRET_REALTIME_GUARDRAIL_NAME" not in rendered
        else:
            context = record.__dict__["openai_agents_diagnostic_context"]
            assert context == {"guardrail_name": "SECRET_REALTIME_GUARDRAIL_NAME"}
            assert record.exc_info is not None
            assert record.exc_info[1] is error
            assert "SECRET_REALTIME_GUARDRAIL_ERROR" in logging.Formatter().format(record)

    @pytest.mark.asyncio
    async def test_output_guardrail_failure_tolerates_missing_callable_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        mock_model: RealtimeModel,
    ) -> None:
        class _FailingGuardrailCallable:
            async def __call__(self, context, agent, output):
                _ = context, agent, output
                raise RuntimeError("SECRET_UNNAMED_GUARDRAIL_ERROR")

        guardrail = OutputGuardrail(guardrail_function=_FailingGuardrailCallable())
        agent = RealtimeAgent(name="agent", output_guardrails=[guardrail])
        session = RealtimeSession(mock_model, agent, None)
        monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
        monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)

        with caplog.at_level(logging.WARNING, logger="openai.agents"):
            triggered = await session._run_output_guardrails("model text", "response-id")

        assert triggered is False
        records = [
            record
            for record in caplog.records
            if "Output guardrail raised an exception" in record.getMessage()
        ]
        assert len(records) == 1
        context = records[0].__dict__["openai_agents_diagnostic_context"]
        assert context["guardrail_type"].endswith("._FailingGuardrailCallable")
        assert records[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_transcript_delta_triggers_guardrail_at_threshold(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test that guardrails run when transcript delta reaches debounce threshold"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 10},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Send transcript delta that exceeds threshold (10 chars)
        transcript_event = RealtimeModelTranscriptDeltaEvent(
            item_id="item_1", delta="this is more than ten characters", response_id="resp_1"
        )

        await session.on_event(transcript_event)

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should have triggered guardrail and interrupted
        assert mock_model.interrupts_called == 1
        interrupt_event = next(
            event
            for event in mock_model.sent_events
            if isinstance(event, RealtimeModelSendInterrupt)
        )
        assert interrupt_event.force_response_cancel is True
        assert len(mock_model.sent_messages) == 1
        assert mock_model.sent_messages[0] == "guardrail triggered: triggered_guardrail"

        # Should have emitted guardrail_tripped event
        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        guardrail_events = [e for e in events if isinstance(e, RealtimeGuardrailTripped)]
        assert len(guardrail_events) == 1
        assert guardrail_events[0].message == "this is more than ten characters"

    @pytest.mark.asyncio
    async def test_output_text_delta_triggers_response_scoped_guardrail(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }
        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        await session.on_event(RealtimeModelTurnStartedEvent())
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        interrupt_event = next(
            event
            for event in mock_model.sent_events
            if isinstance(event, RealtimeModelSendInterrupt)
        )
        assert interrupt_event.force_response_cancel is True
        assert interrupt_event.response_id == "response_1"
        assert interrupt_event.cancel_response_only is True
        assert mock_model.sent_messages == ["guardrail triggered: triggered_guardrail"]

    @pytest.mark.asyncio
    async def test_stale_output_text_guardrail_does_not_affect_newer_response(self, mock_model):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        guardrail = OutputGuardrail(
            guardrail_function=delayed_guardrail,
            name="delayed_guardrail",
        )
        source_agent = RealtimeAgent(name="source", output_guardrails=[guardrail])
        session = RealtimeSession(
            mock_model,
            source_agent,
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 1}},
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="blocked",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_2"))
        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        assert not any(
            isinstance(event, RealtimeModelSendInterrupt) for event in mock_model.sent_events
        )
        assert mock_model.sent_messages == []
        queued_events = []
        while not session._event_queue.empty():
            queued_events.append(await session._event_queue.get())
        assert sum(isinstance(event, RealtimeGuardrailTripped) for event in queued_events) == 1

    @pytest.mark.asyncio
    async def test_stale_audio_guardrail_interrupts_only_source_playback(self, mock_model):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        session = RealtimeSession(
            mock_model,
            RealtimeAgent(
                name="source",
                output_guardrails=[
                    OutputGuardrail(
                        guardrail_function=delayed_guardrail,
                        name="delayed_guardrail",
                    )
                ],
            ),
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 1}},
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1",
                delta="blocked",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()
        await session.on_event(RealtimeModelTurnEndedEvent(response_id="response_1"))
        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_2"))

        assert mock_model.retired_audio_response_ids == []
        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        interrupts = [
            event
            for event in mock_model.sent_events
            if isinstance(event, RealtimeModelSendInterrupt)
        ]
        assert len(interrupts) == 1
        assert interrupts[0].response_id == "response_1"
        assert interrupts[0].playback_only is True
        assert interrupts[0].force_response_cancel is False
        assert mock_model.sent_messages == []
        assert mock_model.retired_audio_response_ids == ["response_1"]
        assert session._interrupted_response_ids == set()

    @pytest.mark.asyncio
    async def test_response_audio_cleanup_waits_for_delayed_guardrail(self, mock_agent):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()
        operations: list[str] = []

        class TrackingModel(RecordingRealtimeModel):
            async def send_event(self, event):
                await super().send_event(event)
                if isinstance(event, RealtimeModelSendInterrupt):
                    operations.append("interrupt")

            def _retire_response_audio(self, response_id: str) -> None:
                super()._retire_response_audio(response_id)
                operations.append("retire")

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        model = TrackingModel()
        session = RealtimeSession(
            model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [
                    OutputGuardrail(
                        guardrail_function=delayed_guardrail,
                        name="delayed_guardrail",
                    )
                ],
                "guardrails_settings": {"debounce_text_length": 1},
            },
        )

        await session.on_event(RealtimeModelTurnStartedEvent())
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1",
                delta="blocked",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()
        assert session._active_output_response_id == "response_1"
        await session.on_event(RealtimeModelTurnEndedEvent())
        await asyncio.sleep(0)

        assert operations == []
        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        assert operations == ["interrupt", "retire"]
        assert model.retired_audio_response_ids == ["response_1"]
        assert session._guardrail_tasks_by_response_id == {}
        assert session._responses_awaiting_guardrail_cleanup == set()

    @pytest.mark.asyncio
    async def test_response_audio_cleanup_runs_immediately_without_guardrail_tasks(
        self, mock_model, mock_agent
    ):
        session = RealtimeSession(mock_model, mock_agent, None)

        await session.on_event(RealtimeModelTurnEndedEvent(response_id="response_1"))

        assert mock_model.retired_audio_response_ids == ["response_1"]

    @pytest.mark.asyncio
    async def test_stale_explicit_turn_end_preserves_active_response_guardrail_state(
        self, mock_model, mock_agent
    ):
        session = RealtimeSession(mock_model, mock_agent, None)
        await session.on_event(RealtimeModelTurnStartedEvent(response_id="new_response"))
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="new_item",
                delta="still active",
                response_id="new_response",
            )
        )
        active_generation = session._active_output_response_generation
        active_agent = session._active_output_response_agent

        await session.on_event(RealtimeModelTurnEndedEvent(response_id="old_response"))

        assert mock_model.retired_audio_response_ids == ["old_response"]
        assert session._active_output_response_id == "new_response"
        assert session._active_output_response_generation == active_generation
        assert session._active_output_response_agent is active_agent
        assert session._item_transcripts == {"new_item": "still active"}
        assert session._item_guardrail_run_counts == {"new_item": 0}
        queued_events = []
        while not session._event_queue.empty():
            queued_events.append(await session._event_queue.get())
        assert not any(isinstance(event, RealtimeAgentEndEvent) for event in queued_events)

    @pytest.mark.asyncio
    async def test_interrupted_response_audio_delta_is_not_forwarded(self, mock_model, mock_agent):
        session = RealtimeSession(mock_model, mock_agent, None)
        session._interrupted_response_ids.add("response_1")

        await session.on_event(
            RealtimeModelAudioEvent(
                data=b"audio",
                response_id="response_1",
                item_id="item_1",
                content_index=0,
            )
        )

        queued_events = []
        while not session._event_queue.empty():
            queued_events.append(await session._event_queue.get())
        assert not any(isinstance(event, RealtimeAudio) for event in queued_events)

    @pytest.mark.asyncio
    async def test_response_audio_cleanup_error_releases_session_suppression(self, mock_agent):
        class FailingRetirementModel(RecordingRealtimeModel):
            def _retire_response_audio(self, response_id: str) -> None:
                raise RuntimeError(f"failed to retire {response_id}")

        session = RealtimeSession(FailingRetirementModel(), mock_agent, None)
        session._interrupted_response_ids.add("response_1")

        session._retire_response_audio("response_1")

        assert session._interrupted_response_ids == set()
        queued_event = await session._event_queue.get()
        assert isinstance(queued_event, RealtimeError)
        assert queued_event.error == {
            "message": "Response audio cleanup failed: failed to retire response_1"
        }

    @pytest.mark.asyncio
    async def test_output_text_guardrail_sends_feedback_after_source_turn_ends(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [triggered_guardrail],
                "guardrails_settings": {"debounce_text_length": 5},
            },
        )
        original_send_event = mock_model.send_event

        async def send_event(event):
            await original_send_event(event)
            if isinstance(event, RealtimeModelSendInterrupt):
                await session.on_event(RealtimeModelTurnEndedEvent())

        mock_model.send_event = send_event

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert mock_model.sent_messages == ["guardrail triggered: triggered_guardrail"]

    @pytest.mark.asyncio
    async def test_output_text_guardrail_skips_feedback_for_completed_idless_newer_turn(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [triggered_guardrail],
                "guardrails_settings": {"debounce_text_length": 5},
            },
        )
        original_send_event = mock_model.send_event

        async def send_event(event):
            await original_send_event(event)
            if isinstance(event, RealtimeModelSendInterrupt):
                await session.on_event(RealtimeModelTurnEndedEvent())
                await session.on_event(RealtimeModelTurnStartedEvent())
                await session.on_event(RealtimeModelTurnEndedEvent())

        mock_model.send_event = send_event

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert mock_model.sent_messages == []

    @pytest.mark.asyncio
    async def test_output_text_guardrail_rechecks_generation_at_feedback_send_boundary(
        self, mock_agent, triggered_guardrail
    ):
        feedback_send_started = asyncio.Event()
        release_feedback_send = asyncio.Event()

        class BoundaryCheckingModel(RecordingRealtimeModel):
            async def send_event_if(self, event, send_if):
                feedback_send_started.set()
                await release_feedback_send.wait()
                return await super().send_event_if(event, send_if)

        model = BoundaryCheckingModel()
        session = RealtimeSession(
            model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [triggered_guardrail],
                "guardrails_settings": {"debounce_text_length": 5},
            },
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await feedback_send_started.wait()

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_2"))
        release_feedback_send.set()
        await self._wait_for_guardrail_tasks(session)

        assert model.sent_messages == []

    @pytest.mark.asyncio
    async def test_output_text_guardrail_skips_feedback_without_atomic_model_send(
        self, mock_agent, triggered_guardrail
    ):
        class CustomModelWithoutAtomicSend(RecordingRealtimeModel):
            def __init__(self):
                super().__init__()
                self.feedback_send_started = False

            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendUserInput):
                    self.feedback_send_started = True
                    await asyncio.sleep(0)
                await super().send_event(event)

            async def send_event_if(self, event, send_if):
                return await RealtimeModel.send_event_if(self, event, send_if)

        model = CustomModelWithoutAtomicSend()
        session = RealtimeSession(
            model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [triggered_guardrail],
                "guardrails_settings": {"debounce_text_length": 5},
            },
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert any(isinstance(event, RealtimeModelSendInterrupt) for event in model.sent_events)
        assert model.feedback_send_started is False
        assert model.sent_messages == []

    @pytest.mark.asyncio
    async def test_output_text_guardrail_uses_agent_from_turn_start(self, mock_model):
        observed_agents: list[RealtimeAgent] = []
        replacement_called = False

        def source_guardrail(context, agent, output):
            _ = context, output
            observed_agents.append(agent)
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        def replacement_guardrail(context, agent, output):
            nonlocal replacement_called
            _ = context, agent, output
            replacement_called = True
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)

        source_agent = RealtimeAgent(
            name="source",
            output_guardrails=[
                OutputGuardrail(guardrail_function=source_guardrail, name="source_guardrail")
            ],
        )
        replacement_agent = RealtimeAgent(
            name="replacement",
            output_guardrails=[
                OutputGuardrail(
                    guardrail_function=replacement_guardrail,
                    name="replacement_guardrail",
                )
            ],
        )
        session = RealtimeSession(
            mock_model,
            source_agent,
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 5}},
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.update_agent(replacement_agent)
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert observed_agents == [source_agent]
        assert replacement_called is False
        assert mock_model.sent_messages == ["guardrail triggered: source_guardrail"]

    @pytest.mark.asyncio
    async def test_output_text_guardrail_retains_agent_for_matching_late_turn_start(
        self, mock_model
    ):
        observed_agents: list[RealtimeAgent] = []

        def source_guardrail(context, agent, output):
            _ = context, output
            observed_agents.append(agent)
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        source_agent = RealtimeAgent(
            name="source",
            output_guardrails=[
                OutputGuardrail(guardrail_function=source_guardrail, name="source_guardrail")
            ],
        )
        replacement_agent = RealtimeAgent(name="replacement")
        session = RealtimeSession(
            mock_model,
            source_agent,
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 5}},
        )

        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="he",
                response_id="response_1",
            )
        )
        await session.update_agent(replacement_agent)
        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="llo",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert observed_agents == [source_agent]
        assert mock_model.sent_messages == ["guardrail triggered: source_guardrail"]

    @pytest.mark.asyncio
    async def test_matching_late_turn_start_retains_pending_guardrail_generation(self, mock_model):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        source_agent = RealtimeAgent(
            name="source",
            output_guardrails=[
                OutputGuardrail(guardrail_function=delayed_guardrail, name="source_guardrail")
            ],
        )
        session = RealtimeSession(
            mock_model,
            source_agent,
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 2}},
        )

        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="he",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()
        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))

        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        interrupt_event = next(
            event
            for event in mock_model.sent_events
            if isinstance(event, RealtimeModelSendInterrupt)
        )
        assert interrupt_event.response_id == "response_1"
        assert mock_model.sent_messages == ["guardrail triggered: source_guardrail"]

    @pytest.mark.asyncio
    async def test_output_text_guardrail_sends_feedback_if_source_ends_during_evaluation(
        self, mock_model
    ):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        guardrail = OutputGuardrail(
            guardrail_function=delayed_guardrail,
            name="delayed_guardrail",
        )
        session = RealtimeSession(
            mock_model,
            RealtimeAgent(name="source", output_guardrails=[guardrail]),
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 1}},
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="blocked",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()

        await session.on_event(RealtimeModelTurnEndedEvent())
        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        assert not any(
            isinstance(event, RealtimeModelSendInterrupt) for event in mock_model.sent_events
        )
        assert mock_model.sent_messages == ["guardrail triggered: delayed_guardrail"]

    @pytest.mark.asyncio
    async def test_agent_and_run_config_guardrails_not_run_twice(self, mock_model):
        """Guardrails shared by agent and run config should execute once."""

        call_count = 0

        def guardrail_func(context, agent, output):
            nonlocal call_count
            call_count += 1
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)

        shared_guardrail = OutputGuardrail(
            guardrail_function=guardrail_func, name="shared_guardrail"
        )

        agent = RealtimeAgent(name="agent", output_guardrails=[shared_guardrail])
        run_config: RealtimeRunConfig = {
            "output_guardrails": [shared_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, agent, None, run_config=run_config)

        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="hello", response_id="resp_1")
        )

        await self._wait_for_guardrail_tasks(session)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_transcript_delta_multiple_thresholds_same_item(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test guardrails run at 1x, 2x, 3x thresholds for same item_id"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # First delta - reaches 1x threshold (5 chars)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="12345", response_id="resp_1")
        )

        # Second delta - reaches 2x threshold (10 chars total)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="67890", response_id="resp_1")
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should only trigger once due to interrupted_by_guardrail flag
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_large_transcript_delta_advances_past_each_crossed_threshold(
        self, mock_model, mock_agent
    ):
        calls = 0

        async def guardrail_func(context, agent, output):
            nonlocal calls
            calls += 1
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)

        guardrail = OutputGuardrail(guardrail_function=guardrail_func)
        run_config: RealtimeRunConfig = {
            "output_guardrails": [guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }
        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="123456789012", response_id="resp_1"
            )
        )
        await self._wait_for_guardrail_tasks(session)
        assert calls == 1

        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="3", response_id="resp_1")
        )
        await self._wait_for_guardrail_tasks(session)

        assert calls == 1

    @pytest.mark.asyncio
    async def test_transcript_delta_different_items_tracked_separately(
        self, mock_model, mock_agent, safe_guardrail
    ):
        """Test that different item_ids are tracked separately for debouncing"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [safe_guardrail],
            "guardrails_settings": {"debounce_text_length": 10},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Add text to item_1 (8 chars - below threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="12345678", response_id="resp_1"
            )
        )

        # Add text to item_2 (8 chars - below threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_2", delta="abcdefgh", response_id="resp_2"
            )
        )

        # Neither should trigger guardrails yet
        assert mock_model.interrupts_called == 0

        # Add more text to item_1 (total 12 chars - above threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="90ab", response_id="resp_1")
        )

        # item_1 should have triggered guardrail run (but not interrupted since safe)
        assert session._item_guardrail_run_counts["item_1"] == 1
        assert (
            "item_2" not in session._item_guardrail_run_counts
            or session._item_guardrail_run_counts["item_2"] == 0
        )

    @pytest.mark.asyncio
    async def test_turn_ended_clears_guardrail_state(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test that turn_ended event clears guardrail state for next turn"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Trigger guardrail
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="trigger", response_id="resp_1"
            )
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        assert len(session._item_transcripts) == 1

        # End turn
        await session.on_event(RealtimeModelTurnEndedEvent())

        # State should be cleared
        assert len(session._item_transcripts) == 0
        assert len(session._item_guardrail_run_counts) == 0

    @pytest.mark.asyncio
    async def test_multiple_guardrails_all_triggered(self, mock_model, mock_agent):
        """Test that all triggered guardrails are included in the event"""

        def create_triggered_guardrail(name):
            def guardrail_func(context, agent, output):
                return GuardrailFunctionOutput(output_info={"name": name}, tripwire_triggered=True)

            return OutputGuardrail(guardrail_function=guardrail_func, name=name)

        guardrail1 = create_triggered_guardrail("guardrail_1")
        guardrail2 = create_triggered_guardrail("guardrail_2")

        run_config: RealtimeRunConfig = {
            "output_guardrails": [guardrail1, guardrail2],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="trigger", response_id="resp_1"
            )
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should have interrupted and sent message with both guardrail names
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1
        message = mock_model.sent_messages[0]
        assert "guardrail_1" in message and "guardrail_2" in message

        # Should have emitted event with both guardrail results
        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        guardrail_events = [e for e in events if isinstance(e, RealtimeGuardrailTripped)]
        assert len(guardrail_events) == 1
        assert len(guardrail_events[0].guardrail_results) == 2

    @pytest.mark.asyncio
    async def test_agent_output_guardrails_triggered(self, mock_model, triggered_guardrail):
        """Test that guardrails defined on the agent are executed."""
        agent = RealtimeAgent(name="agent", output_guardrails=[triggered_guardrail])
        run_config: RealtimeRunConfig = {
            "guardrails_settings": {"debounce_text_length": 10},
        }

        session = RealtimeSession(mock_model, agent, None, run_config=run_config)

        transcript_event = RealtimeModelTranscriptDeltaEvent(
            item_id="item_1", delta="this is more than ten characters", response_id="resp_1"
        )

        await session.on_event(transcript_event)
        await self._wait_for_guardrail_tasks(session)

        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1
        assert "triggered_guardrail" in mock_model.sent_messages[0]

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        guardrail_events = [e for e in events if isinstance(e, RealtimeGuardrailTripped)]
        assert len(guardrail_events) == 1
        assert guardrail_events[0].message == "this is more than ten characters"

    @pytest.mark.asyncio
    async def test_concurrent_guardrail_tasks_interrupt_once_per_response(self, mock_model):
        """Even if multiple guardrail tasks trigger concurrently for the same response_id,
        only the first should interrupt and send a message."""
        import asyncio

        # Barrier to release both guardrail tasks at the same time
        start_event = asyncio.Event()

        async def async_trigger_guardrail(context, agent, output):
            await start_event.wait()
            return GuardrailFunctionOutput(
                output_info={"reason": "concurrent"}, tripwire_triggered=True
            )

        concurrent_guardrail = OutputGuardrail(
            guardrail_function=async_trigger_guardrail, name="concurrent_trigger"
        )

        run_config: RealtimeRunConfig = {
            "output_guardrails": [concurrent_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        # Use a minimal agent (guardrails from run_config)
        agent = RealtimeAgent(name="agent")
        session = RealtimeSession(mock_model, agent, None, run_config=run_config)

        # Two deltas for same item and response to enqueue two guardrail tasks
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="12345", response_id="resp_same"
            )
        )
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="67890", response_id="resp_same"
            )
        )

        # Wait until both tasks are enqueued
        for _ in range(50):
            if len(session._guardrail_tasks) >= 2:
                break
            await asyncio.sleep(0.01)

        # Release both tasks concurrently
        start_event.set()

        # Wait for completion
        if session._guardrail_tasks:
            await asyncio.gather(*session._guardrail_tasks, return_exceptions=True)

        # Only one interrupt and one message should be sent
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1
