from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai.types.responses import Response, ResponseInputItemParam


class SpanData(abc.ABC):
    """
    Represents span data in the trace.
    """

    @abc.abstractmethod
    def export(self) -> dict[str, Any]:
        """Export the span data as a dictionary."""
        pass

    @property
    @abc.abstractmethod
    def type(self) -> str:
        """Return the type of the span."""
        pass


class AgentSpanData(SpanData):
    """
    Represents an Agent Span in the trace.
    Includes name, handoffs, tools, and output type.
    """

    __slots__ = ("name", "handoffs", "tools", "output_type")

    def __init__(
        self,
        name: str,
        handoffs: list[str] | None = None,
        tools: list[str] | None = None,
        output_type: str | None = None,
    ):
        self.name = name
        self.handoffs: list[str] | None = handoffs
        self.tools: list[str] | None = tools
        self.output_type: str | None = output_type

    @property
    def type(self) -> str:
        return "agent"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "handoffs": self.handoffs,
            "tools": self.tools,
            "output_type": self.output_type,
        }


class FunctionSpanData(SpanData):
    """
    Represents a Function Span in the trace.
    Includes input, output and MCP data (if applicable).
    """

    __slots__ = ("name", "input", "output", "mcp_data")

    def __init__(
        self,
        name: str,
        input: str | None,
        output: Any | None,
        mcp_data: dict[str, Any] | None = None,
    ):
        self.name = name
        self.input = input
        self.output = output
        self.mcp_data = mcp_data

    @property
    def type(self) -> str:
        return "function"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "input": self.input,
            "output": str(self.output) if self.output else None,
            "mcp_data": self.mcp_data,
        }


class GenerationSpanData(SpanData):
    """
    Represents a Generation Span in the trace.
    Includes input, output, model, model configuration, and usage.
    """

    __slots__ = (
        "input",
        "output",
        "model",
        "model_config",
        "usage",
    )

    def __init__(
        self,
        input: Sequence[Mapping[str, Any]] | None = None,
        output: Sequence[Mapping[str, Any]] | None = None,
        model: str | None = None,
        model_config: Mapping[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ):
        self.input = input
        self.output = output
        self.model = model
        self.model_config = model_config
        self.usage = usage

    @property
    def type(self) -> str:
        return "generation"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "input": self.input,
            "output": self.output,
            "model": self.model,
            "model_config": self.model_config,
            "usage": self.usage,
        }


class ResponseSpanData(SpanData):
    """
    Represents a Response Span in the trace.
    Includes response and input.
    """

    __slots__ = ("response", "input")

    def __init__(
        self,
        response: Response | None = None,
        input: str | list[ResponseInputItemParam] | None = None,
    ) -> None:
        self.response = response
        # This is not used by the OpenAI trace processors, but is useful for other tracing
        # processor implementations
        self.input = input

    @property
    def type(self) -> str:
        return "response"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "response_id": self.response.id if self.response else None,
        }


class HandoffSpanData(SpanData):
    """
    Represents a Handoff Span in the trace.
    Includes source and destination agents.
    """

    __slots__ = ("from_agent", "to_agent")

    def __init__(self, from_agent: str | None, to_agent: str | None):
        self.from_agent = from_agent
        self.to_agent = to_agent

    @property
    def type(self) -> str:
        return "handoff"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
        }


class CustomSpanData(SpanData):
    """
    Represents a Custom Span in the trace.
    Includes name and data property bag.
    """

    __slots__ = ("name", "data")

    def __init__(self, name: str, data: dict[str, Any]):
        self.name = name
        self.data = data

    @property
    def type(self) -> str:
        return "custom"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "data": self.data,
        }


class GuardrailSpanData(SpanData):
    """
    Represents a Guardrail Span in the trace.
    Includes name and triggered status.
    """

    __slots__ = ("name", "triggered")

    def __init__(self, name: str, triggered: bool = False):
        self.name = name
        self.triggered = triggered

    @property
    def type(self) -> str:
        return "guardrail"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "triggered": self.triggered,
        }


class TranscriptionSpanData(SpanData):
    """
    Represents a Transcription Span in the trace.
    Includes input, output, model, and model configuration.
    """

    __slots__ = (
        "input",
        "output",
        "model",
        "model_config",
    )

    def __init__(
        self,
        input: str | None = None,
        input_format: str | None = "pcm",
        output: str | None = None,
        model: str | None = None,
        model_config: Mapping[str, Any] | None = None,
    ):
        self.input = input
        self.input_format = input_format
        self.output = output
        self.model = model
        self.model_config = model_config

    @property
    def type(self) -> str:
        return "transcription"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "input": {
                "data": self.input or "",
                "format": self.input_format,
            },
            "output": self.output,
            "model": self.model,
            "model_config": self.model_config,
        }


class SpeechSpanData(SpanData):
    """
    Represents a Speech Span in the trace.
    Includes input, output, model, model configuration, and first content timestamp.
    """

    __slots__ = ("input", "output", "model", "model_config", "first_content_at")

    def __init__(
        self,
        input: str | None = None,
        output: str | None = None,
        output_format: str | None = "pcm",
        model: str | None = None,
        model_config: Mapping[str, Any] | None = None,
        first_content_at: str | None = None,
    ):
        self.input = input
        self.output = output
        self.output_format = output_format
        self.model = model
        self.model_config = model_config
        self.first_content_at = first_content_at

    @property
    def type(self) -> str:
        return "speech"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "input": self.input,
            "output": {
                "data": self.output or "",
                "format": self.output_format,
            },
            "model": self.model,
            "model_config": self.model_config,
            "first_content_at": self.first_content_at,
        }


class SpeechGroupSpanData(SpanData):
    """
    Represents a Speech Group Span in the trace.
    """

    __slots__ = "input"

    def __init__(
        self,
        input: str | None = None,
    ):
        self.input = input

    @property
    def type(self) -> str:
        return "speech_group"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "input": self.input,
        }


class MCPListToolsSpanData(SpanData):
    """
    Represents an MCP List Tools Span in the trace.
    Includes server and result.
    """

    __slots__ = (
        "server",
        "result",
    )

    def __init__(self, server: str | None = None, result: list[str] | None = None):
        self.server = server
        self.result = result

    @property
    def type(self) -> str:
        return "mcp_tools"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "server": self.server,
            "result": self.result,
        }
