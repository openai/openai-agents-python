"""Tests for OpenTelemetryTracingProcessor."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class MockOTelSpan:
    """Mock OpenTelemetry span for testing."""

    def __init__(self, name: str = "test"):
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.status: Any = None
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: Any) -> None:
        self.status = status

    def end(self) -> None:
        self.ended = True


class MockTracer:
    """Mock OpenTelemetry tracer for testing."""

    def __init__(self) -> None:
        self.spans: list[MockOTelSpan] = []

    def start_span(
        self,
        name: str,
        context: Any = None,
        kind: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> MockOTelSpan:
        span = MockOTelSpan(name)
        if attributes:
            span.attributes.update(attributes)
        self.spans.append(span)
        return span


class MockTrace:
    """Mock SDK Trace for testing."""

    def __init__(
        self,
        trace_id: str = "trace_abc",
        name: str = "Test",
        group_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.trace_id = trace_id
        self.name = name
        self.group_id = group_id
        self.metadata = metadata


class MockAgentSpanData:
    """Mock agent span data."""

    type = "agent"

    def __init__(
        self,
        name: str = "TestAgent",
        handoffs: list[str] | None = None,
        tools: list[str] | None = None,
        output_type: str | None = None,
    ):
        self.name = name
        self.handoffs = handoffs or []
        self.tools = tools or []
        self.output_type = output_type


class MockGenerationSpanData:
    """Mock generation span data."""

    type = "generation"

    def __init__(
        self,
        model: str | None = None,
        model_config: dict[str, Any] | None = None,
        usage: dict[str, int] | None = None,
        input: list[Any] | None = None,
        output: list[Any] | None = None,
    ):
        self.model = model
        self.model_config = model_config
        self.usage = usage
        self.input = input
        self.output = output


class MockFunctionSpanData:
    """Mock function/tool span data."""

    type = "function"

    def __init__(
        self,
        name: str = "test_tool",
        input: str | None = None,
        output: str | None = None,
        mcp_data: dict[str, Any] | None = None,
    ):
        self.name = name
        self.input = input
        self.output = output
        self.mcp_data = mcp_data


class MockHandoffSpanData:
    """Mock handoff span data."""

    type = "handoff"

    def __init__(
        self,
        from_agent: str | None = None,
        to_agent: str | None = None,
    ):
        self.from_agent = from_agent
        self.to_agent = to_agent


class MockGuardrailSpanData:
    """Mock guardrail span data."""

    type = "guardrail"

    def __init__(
        self,
        name: str = "test_guardrail",
        triggered: bool = False,
    ):
        self.name = name
        self.triggered = triggered


class MockCustomSpanData:
    """Mock custom span data."""

    type = "custom"

    def __init__(
        self,
        name: str = "custom_operation",
        data: dict[str, Any] | None = None,
    ):
        self.name = name
        self.data = data


class MockResponseSpanData:
    """Mock response span data."""

    type = "response"

    def __init__(self, response: Any = None):
        self.response = response


class MockTranscriptionSpanData:
    """Mock transcription span data."""

    type = "transcription"

    def __init__(
        self,
        model: str | None = None,
        output: str | None = None,
    ):
        self.model = model
        self.output = output


class MockSpeechSpanData:
    """Mock speech span data."""

    type = "speech"

    def __init__(self, model: str | None = None):
        self.model = model


class MockSpeechGroupSpanData:
    """Mock speech group span data."""

    type = "speech_group"


class MockMCPToolsSpanData:
    """Mock MCP tools span data."""

    type = "mcp_tools"

    def __init__(
        self,
        server: str | None = None,
        result: list[str] | None = None,
    ):
        self.server = server
        self.result = result


class MockUnknownSpanData:
    """Mock unknown span type for testing fallback."""

    type = "unknown_type"

    def export(self) -> dict[str, Any]:
        return {"type": "unknown_type", "custom_field": "custom_value"}


class MockSDKSpan:
    """Mock SDK Span for testing."""

    def __init__(
        self,
        span_id: str = "span_123",
        trace_id: str = "trace_abc",
        parent_id: str | None = None,
        span_data: Any = None,
        error: dict[str, Any] | None = None,
    ):
        self.span_id = span_id
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.span_data = span_data or MockAgentSpanData()
        self.error = error


class MockResponse:
    """Mock OpenAI Response object."""

    def __init__(self, id: str = "resp_123"):
        self.id = id


@pytest.fixture
def mock_otel() -> Any:
    """Fixture providing mocked OpenTelemetry components."""
    mock_trace = MagicMock()
    mock_tracer = MockTracer()
    mock_trace.get_tracer.return_value = mock_tracer
    mock_context = MagicMock()
    mock_context.attach.return_value = "token"
    mock_span_kind = MagicMock()
    mock_span_kind.INTERNAL = "INTERNAL"
    mock_span_kind.CLIENT = "CLIENT"
    mock_status = MagicMock()
    mock_status_code = MagicMock()
    mock_status_code.OK = "OK"
    mock_status_code.ERROR = "ERROR"

    with patch(
        "agents.extensions.tracing.opentelemetry_processor._try_import_opentelemetry",
        return_value=(mock_trace, mock_span_kind, mock_status, mock_status_code, mock_context),
    ):
        yield {"trace": mock_trace, "tracer": mock_tracer, "context": mock_context}


class TestOpenTelemetryTracingProcessor:
    """Tests for the OpenTelemetryTracingProcessor class."""

    def test_on_trace_start_creates_span(self, mock_otel: Any) -> None:
        """Test that on_trace_start creates an OTel span."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123", name="Test Workflow")
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        assert len(mock_otel["tracer"].spans) == 1
        assert "workflow: Test Workflow" in mock_otel["tracer"].spans[0].name

    def test_on_trace_start_with_group_id(self, mock_otel: Any) -> None:
        """Test that group_id is included in span attributes."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123", name="Test", group_id="group_456")
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        span = mock_otel["tracer"].spans[0]
        assert span.attributes["agent.group_id"] == "group_456"

    def test_on_trace_start_with_metadata(self, mock_otel: Any) -> None:
        """Test that metadata is included in span attributes."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(
            trace_id="trace_123",
            name="Test",
            metadata={"user_id": "user_789", "session": "sess_abc"},
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        span = mock_otel["tracer"].spans[0]
        assert span.attributes["agent.metadata.user_id"] == "user_789"
        assert span.attributes["agent.metadata.session"] == "sess_abc"

    def test_on_trace_end_closes_span(self, mock_otel: Any) -> None:
        """Test that on_trace_end closes the OTel span."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_trace_end(trace)  # type: ignore[arg-type]
        assert mock_otel["tracer"].spans[0].ended

    def test_on_trace_end_without_start_logs_warning(self, mock_otel: Any) -> None:
        """Test that ending a trace without starting logs a warning."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="nonexistent_trace")
        # Should not raise, just log warning.
        processor.on_trace_end(trace)  # type: ignore[arg-type]

    def test_custom_tracer_name(self, mock_otel: Any) -> None:
        """Test that custom tracer name is used."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor(tracer_name="my.custom.tracer")
        assert processor._tracer_name == "my.custom.tracer"
        mock_otel["trace"].get_tracer.assert_called_with("my.custom.tracer")


class TestAgentSpan:
    """Tests for agent span mapping."""

    def test_agent_span_basic(self, mock_otel: Any) -> None:
        """Test basic agent span creation."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockAgentSpanData(name="MyAgent"),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "agent: MyAgent" in otel_span.name
        assert otel_span.attributes["agent.name"] == "MyAgent"

    def test_agent_span_with_tools_and_handoffs(self, mock_otel: Any) -> None:
        """Test agent span with tools and handoffs."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockAgentSpanData(
                name="MyAgent",
                tools=["tool1", "tool2"],
                handoffs=["agent2", "agent3"],
                output_type="str",
            ),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["agent.tools"] == '["tool1", "tool2"]'
        assert otel_span.attributes["agent.handoffs"] == '["agent2", "agent3"]'
        assert otel_span.attributes["agent.output_type"] == "str"


class TestGenerationSpan:
    """Tests for generation span mapping."""

    def test_generation_span_attributes(self, mock_otel: Any) -> None:
        """Test generation span attributes."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockGenerationSpanData(model="gpt-4", usage={"input_tokens": 100}),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["gen_ai.request.model"] == "gpt-4"
        assert otel_span.attributes["gen_ai.usage.input_tokens"] == 100

    def test_generation_span_filters_empty_model_config(self, mock_otel: Any) -> None:
        """Test that None and empty string values in model_config are filtered out."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockGenerationSpanData(
                model="gpt-4",
                model_config={
                    "temperature": 0.7,
                    "top_p": None,
                    "frequency_penalty": "",
                    "max_tokens": 100,
                    "presence_penalty": None,
                },
            ),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]

        # Valid values should be present.
        assert otel_span.attributes["gen_ai.request.temperature"] == 0.7
        assert otel_span.attributes["gen_ai.request.max_tokens"] == 100

        # None and empty string values should be filtered out.
        assert "gen_ai.request.top_p" not in otel_span.attributes
        assert "gen_ai.request.frequency_penalty" not in otel_span.attributes
        assert "gen_ai.request.presence_penalty" not in otel_span.attributes

    def test_generation_span_with_usage_metrics(self, mock_otel: Any) -> None:
        """Test generation span captures all usage metrics."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockGenerationSpanData(
                model="gpt-4",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                },
            ),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["gen_ai.usage.input_tokens"] == 100
        assert otel_span.attributes["gen_ai.usage.output_tokens"] == 50
        assert otel_span.attributes["gen_ai.usage.total_tokens"] == 150

    def test_generation_span_with_input_output(self, mock_otel: Any) -> None:
        """Test generation span captures input/output preview."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockGenerationSpanData(
                model="gpt-4",
                input=[{"role": "user", "content": "Hello"}],
                output=[{"role": "assistant", "content": "Hi there!"}],
            ),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "gen_ai.input.preview" in otel_span.attributes
        assert "gen_ai.output.preview" in otel_span.attributes


class TestFunctionSpan:
    """Tests for function/tool span mapping."""

    def test_function_span_basic(self, mock_otel: Any) -> None:
        """Test basic function span creation."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockFunctionSpanData(name="search_web", input='{"query": "test"}'),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "tool: search_web" in otel_span.name
        assert otel_span.attributes["tool.name"] == "search_web"
        assert otel_span.attributes["tool.input"] == '{"query": "test"}'

    def test_function_span_with_output(self, mock_otel: Any) -> None:
        """Test function span captures output on end."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockFunctionSpanData(
                name="search_web",
                input='{"query": "test"}',
                output="Search results here",
            ),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["tool.output"] == "Search results here"

    def test_function_span_with_mcp_data(self, mock_otel: Any) -> None:
        """Test function span with MCP data."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockFunctionSpanData(
                name="mcp_tool",
                mcp_data={"server": "my_server", "tool": "my_tool"},
            ),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "tool.mcp_data" in otel_span.attributes


class TestHandoffSpan:
    """Tests for handoff span mapping."""

    def test_handoff_span_basic(self, mock_otel: Any) -> None:
        """Test basic handoff span creation."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockHandoffSpanData(from_agent="Agent1", to_agent="Agent2"),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "handoff: Agent1 -> Agent2" in otel_span.name
        assert otel_span.attributes["agent.handoff.from"] == "Agent1"
        assert otel_span.attributes["agent.handoff.to"] == "Agent2"

    def test_handoff_span_unknown_agents(self, mock_otel: Any) -> None:
        """Test handoff span with unknown agents defaults."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockHandoffSpanData(from_agent=None, to_agent=None),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "handoff: unknown -> unknown" in otel_span.name


class TestGuardrailSpan:
    """Tests for guardrail span mapping."""

    def test_guardrail_span_not_triggered(self, mock_otel: Any) -> None:
        """Test guardrail span when not triggered."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockGuardrailSpanData(name="content_filter", triggered=False),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "guardrail: content_filter" in otel_span.name
        assert otel_span.attributes["agent.guardrail.name"] == "content_filter"
        assert otel_span.attributes["agent.guardrail.triggered"] is False

    def test_guardrail_span_triggered(self, mock_otel: Any) -> None:
        """Test guardrail span when triggered."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockGuardrailSpanData(name="pii_filter", triggered=True),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["agent.guardrail.triggered"] is True


class TestCustomSpan:
    """Tests for custom span mapping."""

    def test_custom_span_basic(self, mock_otel: Any) -> None:
        """Test basic custom span creation."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockCustomSpanData(name="my_operation"),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "custom: my_operation" in otel_span.name
        assert otel_span.attributes["custom.name"] == "my_operation"

    def test_custom_span_with_data(self, mock_otel: Any) -> None:
        """Test custom span with custom data."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockCustomSpanData(
                name="my_operation",
                data={"key1": "value1", "key2": 42},
            ),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["custom.data.key1"] == "value1"
        assert otel_span.attributes["custom.data.key2"] == 42


class TestResponseSpan:
    """Tests for response span mapping."""

    def test_response_span_with_response(self, mock_otel: Any) -> None:
        """Test response span with response object."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockResponseSpanData(response=MockResponse(id="resp_abc123")),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "gen_ai.response" in otel_span.name
        assert otel_span.attributes["gen_ai.response.id"] == "resp_abc123"

    def test_response_span_without_response(self, mock_otel: Any) -> None:
        """Test response span without response object."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockResponseSpanData(response=None),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "gen_ai.response" in otel_span.name
        assert "gen_ai.response.id" not in otel_span.attributes


class TestTranscriptionSpan:
    """Tests for transcription span mapping."""

    def test_transcription_span_basic(self, mock_otel: Any) -> None:
        """Test basic transcription span creation."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockTranscriptionSpanData(model="whisper-1"),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "audio.transcription: whisper-1" in otel_span.name
        assert otel_span.attributes["audio.model"] == "whisper-1"

    def test_transcription_span_with_output(self, mock_otel: Any) -> None:
        """Test transcription span captures output."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockTranscriptionSpanData(model="whisper-1", output="Hello world"),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["audio.transcription.output"] == "Hello world"


class TestSpeechSpan:
    """Tests for speech span mapping."""

    def test_speech_span_basic(self, mock_otel: Any) -> None:
        """Test basic speech span creation."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockSpeechSpanData(model="tts-1"),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "audio.speech: tts-1" in otel_span.name
        assert otel_span.attributes["audio.model"] == "tts-1"

    def test_speech_group_span(self, mock_otel: Any) -> None:
        """Test speech group span creation."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockSpeechGroupSpanData(),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "audio.speech_group" in otel_span.name


class TestMCPToolsSpan:
    """Tests for MCP tools span mapping."""

    def test_mcp_tools_span_basic(self, mock_otel: Any) -> None:
        """Test basic MCP tools span creation."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockMCPToolsSpanData(server="my_mcp_server"),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "mcp.list_tools: my_mcp_server" in otel_span.name
        assert otel_span.attributes["mcp.server"] == "my_mcp_server"

    def test_mcp_tools_span_with_result(self, mock_otel: Any) -> None:
        """Test MCP tools span captures result."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockMCPToolsSpanData(
                server="my_mcp_server",
                result=["tool1", "tool2", "tool3"],
            ),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["mcp.tools.count"] == 3
        assert "tool1" in otel_span.attributes["mcp.tools.list"]


class TestUnknownSpanType:
    """Tests for unknown span type fallback."""

    def test_unknown_span_type_fallback(self, mock_otel: Any) -> None:
        """Test that unknown span types are handled gracefully."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockUnknownSpanData(),
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert "agent.unknown_type" in otel_span.name
        assert otel_span.attributes["span.custom_field"] == "custom_value"


class TestErrorHandling:
    """Tests for error handling in spans."""

    def test_span_with_error(self, mock_otel: Any) -> None:
        """Test that span errors are properly recorded."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockAgentSpanData(name="FailingAgent"),
            error={"message": "Something went wrong", "data": {"code": 500}},
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["error.message"] == "Something went wrong"
        assert "500" in otel_span.attributes["error.data"]

    def test_span_with_error_no_data(self, mock_otel: Any) -> None:
        """Test that span errors without data are handled."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockAgentSpanData(name="FailingAgent"),
            error={"message": "Error occurred"},
        )
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]
        otel_span = mock_otel["tracer"].spans[1]
        assert otel_span.attributes["error.message"] == "Error occurred"
        assert "error.data" not in otel_span.attributes

    def test_span_end_without_start(self, mock_otel: Any) -> None:
        """Test that ending a span without starting logs a warning."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(span_id="nonexistent_span", trace_id="trace_123")
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        # Should not raise, just log warning.
        processor.on_span_end(span)  # type: ignore[arg-type]

    def test_span_with_non_serializable_error_data(self, mock_otel: Any) -> None:
        """Test that non-serializable error.data doesn't prevent span from ending.

        Previously, json.dumps() on non-serializable data would raise an exception,
        causing span.end() to never be called and leaking the span.
        """
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")

        # Create an object that can't be JSON serialized.
        class NonSerializable:
            def __str__(self) -> str:
                return "NonSerializable()"

        span = MockSDKSpan(
            trace_id="trace_123",
            span_data=MockAgentSpanData(name="TestAgent"),
            error={
                "message": "Something failed",
                "data": {"obj": NonSerializable()},  # This would fail json.dumps()
            },
        )

        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]
        processor.on_span_end(span)  # type: ignore[arg-type]

        # The span should still be ended despite non-serializable error data.
        otel_span = mock_otel["tracer"].spans[1]  # spans[0] is the trace
        assert otel_span.ended, "Span should be ended even with non-serializable error data"
        assert "error.message" in otel_span.attributes
        assert otel_span.attributes["error.message"] == "Something failed"


class TestOverlappingSpans:
    """Tests for overlapping span scenarios."""

    def test_overlapping_spans_maintain_correct_parents(self, mock_otel: Any) -> None:
        """Test that overlapping spans don't break parent-child relationships.

        Scenario: Span A starts, Span B starts, Span A ends, Span B ends.
        Both A and B should correctly reference the trace as parent.
        Previously, this would break due to out-of-order context detaches.
        """
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")

        # Two sibling spans (both children of the trace, not of each other).
        span_a = MockSDKSpan(
            span_id="span_a",
            trace_id="trace_123",
            parent_id=None,  # Parent is the trace root
            span_data=MockFunctionSpanData(name="tool_a"),
        )
        span_b = MockSDKSpan(
            span_id="span_b",
            trace_id="trace_123",
            parent_id=None,  # Parent is the trace root
            span_data=MockFunctionSpanData(name="tool_b"),
        )

        # Start trace and both spans.
        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span_a)  # type: ignore[arg-type]
        processor.on_span_start(span_b)  # type: ignore[arg-type]

        # End spans in opposite order (A ends before B).
        processor.on_span_end(span_a)  # type: ignore[arg-type]
        processor.on_span_end(span_b)  # type: ignore[arg-type]
        processor.on_trace_end(trace)  # type: ignore[arg-type]

        # All spans should be ended successfully.
        assert len(mock_otel["tracer"].spans) == 3  # trace + span_a + span_b
        for otel_span in mock_otel["tracer"].spans:
            assert otel_span.ended, f"Span {otel_span.name} was not ended"


class TestParentChildRelationships:
    """Tests for parent-child span relationships."""

    def test_span_with_parent(self, mock_otel: Any) -> None:
        """Test that child spans reference parent correctly."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        parent_span = MockSDKSpan(
            span_id="parent_span",
            trace_id="trace_123",
            span_data=MockAgentSpanData(name="ParentAgent"),
        )
        child_span = MockSDKSpan(
            span_id="child_span",
            trace_id="trace_123",
            parent_id="parent_span",
            span_data=MockFunctionSpanData(name="child_tool"),
        )

        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(parent_span)  # type: ignore[arg-type]
        processor.on_span_start(child_span)  # type: ignore[arg-type]

        # Both spans should be created.
        assert len(mock_otel["tracer"].spans) == 3  # trace + parent + child
        child_otel_span = mock_otel["tracer"].spans[2]
        assert child_otel_span.attributes["agent.parent_span_id"] == "parent_span"


class TestShutdownAndFlush:
    """Tests for shutdown and force_flush methods."""

    def test_shutdown_clears_spans(self, mock_otel: Any) -> None:
        """Test that shutdown clears active spans."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(trace_id="trace_123")

        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]

        # Verify spans are tracked.
        assert len(processor._active_spans) == 1
        assert len(processor._trace_root_spans) == 1

        processor.shutdown()

        # Verify spans are cleared.
        assert len(processor._active_spans) == 0
        assert len(processor._trace_root_spans) == 0

    def test_shutdown_ends_unclosed_spans(self, mock_otel: Any) -> None:
        """Test that shutdown ends any unclosed spans."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        processor = OpenTelemetryTracingProcessor()
        trace = MockTrace(trace_id="trace_123")
        span = MockSDKSpan(trace_id="trace_123")

        processor.on_trace_start(trace)  # type: ignore[arg-type]
        processor.on_span_start(span)  # type: ignore[arg-type]

        processor.shutdown()

        # All spans should be ended.
        for otel_span in mock_otel["tracer"].spans:
            assert otel_span.ended

    def test_force_flush(self, mock_otel: Any) -> None:
        """Test that force_flush delegates to tracer provider."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        mock_provider = MagicMock()
        mock_otel["trace"].get_tracer_provider.return_value = mock_provider

        processor = OpenTelemetryTracingProcessor()
        processor.force_flush()

        mock_provider.force_flush.assert_called_once()

    def test_force_flush_no_provider_method(self, mock_otel: Any) -> None:
        """Test that force_flush handles provider without force_flush method."""
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        mock_provider = MagicMock(spec=[])  # No force_flush method.
        mock_otel["trace"].get_tracer_provider.return_value = mock_provider

        processor = OpenTelemetryTracingProcessor()
        # Should not raise.
        processor.force_flush()


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_safe_attribute_value_string(self) -> None:
        """Test _safe_attribute_value with string."""
        from agents.extensions.tracing.opentelemetry_processor import _safe_attribute_value

        assert _safe_attribute_value("test") == "test"

    def test_safe_attribute_value_int(self) -> None:
        """Test _safe_attribute_value with int."""
        from agents.extensions.tracing.opentelemetry_processor import _safe_attribute_value

        assert _safe_attribute_value(42) == 42

    def test_safe_attribute_value_float(self) -> None:
        """Test _safe_attribute_value with float."""
        from agents.extensions.tracing.opentelemetry_processor import _safe_attribute_value

        assert _safe_attribute_value(3.14) == 3.14

    def test_safe_attribute_value_bool(self) -> None:
        """Test _safe_attribute_value with bool."""
        from agents.extensions.tracing.opentelemetry_processor import _safe_attribute_value

        assert _safe_attribute_value(True) is True
        assert _safe_attribute_value(False) is False

    def test_safe_attribute_value_none(self) -> None:
        """Test _safe_attribute_value with None."""
        from agents.extensions.tracing.opentelemetry_processor import _safe_attribute_value

        assert _safe_attribute_value(None) == ""

    def test_safe_attribute_value_dict(self) -> None:
        """Test _safe_attribute_value with dict (JSON serialization)."""
        from agents.extensions.tracing.opentelemetry_processor import _safe_attribute_value

        result = _safe_attribute_value({"a": 1, "b": "test"})
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": "test"}

    def test_safe_attribute_value_list(self) -> None:
        """Test _safe_attribute_value with list (JSON serialization)."""
        from agents.extensions.tracing.opentelemetry_processor import _safe_attribute_value

        result = _safe_attribute_value([1, 2, 3])
        assert result == "[1, 2, 3]"

    def test_safe_attribute_value_non_serializable(self) -> None:
        """Test _safe_attribute_value with non-serializable object."""
        from agents.extensions.tracing.opentelemetry_processor import _safe_attribute_value

        class CustomObject:
            def __str__(self) -> str:
                return "CustomObject()"

        result = _safe_attribute_value(CustomObject())
        assert result == "CustomObject()"

    def test_truncate_string_short(self) -> None:
        """Test _truncate_string with short string."""
        from agents.extensions.tracing.opentelemetry_processor import _truncate_string

        assert _truncate_string("short", 100) == "short"

    def test_truncate_string_exact(self) -> None:
        """Test _truncate_string with exact length string."""
        from agents.extensions.tracing.opentelemetry_processor import _truncate_string

        assert _truncate_string("12345", 5) == "12345"

    def test_truncate_string_long(self) -> None:
        """Test _truncate_string with long string."""
        from agents.extensions.tracing.opentelemetry_processor import _truncate_string

        result = _truncate_string("x" * 100, 10)
        assert len(result) == 10
        assert result.endswith("...")

    def test_truncate_string_default_max(self) -> None:
        """Test _truncate_string with default max length."""
        from agents.extensions.tracing.opentelemetry_processor import _truncate_string

        short_string = "x" * 100
        assert _truncate_string(short_string) == short_string

        long_string = "x" * 5000
        result = _truncate_string(long_string)
        assert len(result) == 4096
        assert result.endswith("...")


class TestImportError:
    """Tests for OpenTelemetry import error handling."""

    def test_import_error_message(self) -> None:
        """Test that ImportError has helpful message."""
        with patch.dict("sys.modules", {"opentelemetry": None}):
            with patch(
                "agents.extensions.tracing.opentelemetry_processor._try_import_opentelemetry",
                side_effect=ImportError(
                    "OpenTelemetry packages are required for OpenTelemetryTracingProcessor. "
                    "Install them with: pip install opentelemetry-api opentelemetry-sdk "
                    "or pip install openai-agents[opentelemetry]"
                ),
            ):
                from agents.extensions.tracing import OpenTelemetryTracingProcessor

                with pytest.raises(ImportError) as exc_info:
                    OpenTelemetryTracingProcessor()

                assert "opentelemetry-api" in str(exc_info.value)
                assert "openai-agents[opentelemetry]" in str(exc_info.value)
