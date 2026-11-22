from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from ._content import bound_content
from .config import OperationDetail, get_operation_detail

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

    __slots__ = ("name", "handoffs", "tools", "output_type", "metadata")

    def __init__(
        self,
        name: str,
        handoffs: list[str] | None = None,
        tools: list[str] | None = None,
        output_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.name = name
        self.handoffs: list[str] | None = handoffs
        self.tools: list[str] | None = tools
        self.output_type: str | None = output_type
        self.metadata = metadata

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


class TaskSpanData(SpanData):
    """Represents one top-level Runner run."""

    __slots__ = ("name", "usage", "metadata")

    def __init__(
        self,
        name: str,
        usage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.name = name
        self.usage = usage
        self.metadata = metadata

    @property
    def type(self) -> str:
        return "task"

    def export(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sdk_span_type": self.type,
            "name": self.name,
        }
        if self.usage is not None:
            data["usage"] = self.usage

        return {
            "type": "custom",
            "name": self.type,
            "data": data,
        }


class TurnSpanData(SpanData):
    """Represents one agent loop turn."""

    __slots__ = ("turn", "agent_name", "usage", "metadata")

    def __init__(
        self,
        turn: int,
        agent_name: str,
        usage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.turn = turn
        self.agent_name = agent_name
        self.usage = usage
        self.metadata = metadata

    @property
    def type(self) -> str:
        return "turn"

    def export(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sdk_span_type": self.type,
            "turn": self.turn,
            "agent_name": self.agent_name,
        }
        if self.usage is not None:
            data["usage"] = self.usage

        return {
            "type": "custom",
            "name": self.type,
            "data": data,
        }


@dataclass(slots=True)
class OperationMetadata:
    """Capture permission and source facts for one model call or tool execution."""

    detail: OperationDetail | None = field(default_factory=get_operation_detail)
    api_type: str | None = None
    model: str | None = None
    response_model: str | None = None
    response_id: str | None = None
    previous_response_id: str | None = None
    tool_call_id: str | None = None
    operation_id: str | None = None
    outcome: str | None = None
    estimated_end: bool = False
    retry_timestamps: list[str] = field(default_factory=list)
    """ISO 8601 observation times for attempts after the first, in order."""

    def export(self) -> dict[str, Any]:
        """Supply detailed operation facts alongside the existing hosted span schema."""
        values = {
            "detail": self.detail,
            "api_type": self.api_type,
            "model": self.model,
            "response_model": self.response_model,
            "response_id": self.response_id,
            "previous_response_id": self.previous_response_id,
            "tool_call_id": self.tool_call_id,
            "operation_id": self.operation_id,
            "outcome": self.outcome,
            "estimated_end": True if self.estimated_end else None,
            "retry_timestamps": self.retry_timestamps or None,
        }
        return {key: value for key, value in values.items() if value is not None}


_Payload = TypeVar("_Payload")
_Input = TypeVar("_Input")
_Output = TypeVar("_Output")


def _capture_payload(operation: OperationMetadata, value: _Payload) -> _Payload | None:
    if operation.detail == "metadata":
        return None
    if operation.detail == "content" and value is not None:
        return cast(_Payload, bound_content(value))
    return value


class _CapturedSpanData(SpanData, Generic[_Input, _Output]):
    __slots__ = ("_input", "_output", "operation")
    operation: OperationMetadata

    @property
    def input(self) -> _Input | None:
        return self._input

    @input.setter
    def input(self, value: _Input | None) -> None:
        self._input = _capture_payload(self.operation, value)

    @property
    def output(self) -> _Output | None:
        return self._output

    @output.setter
    def output(self, value: _Output | None) -> None:
        self._output = _capture_payload(self.operation, value)


class FunctionSpanData(_CapturedSpanData[str, Any]):
    """
    Represents a Function Span in the trace.
    Includes input, output and MCP data (if applicable).
    """

    __slots__ = ("name", "mcp_data")

    def __init__(
        self,
        name: str,
        input: str | None,
        output: Any | None,
        mcp_data: dict[str, Any] | None = None,
    ):
        self.name = name
        self.operation = OperationMetadata()
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
            "output": str(self.output) if self.output is not None else None,
            "mcp_data": self.mcp_data,
        }


class GenerationSpanData(
    _CapturedSpanData[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]
):
    """
    Represents a Generation Span in the trace.
    Includes input, output, model, model configuration, usage, and optional tool definitions.
    """

    __slots__ = (
        "model",
        "_model_config",
        "usage",
        "_tools",
    )

    def __init__(
        self,
        input: Sequence[Mapping[str, Any]] | None = None,
        output: Sequence[Mapping[str, Any]] | None = None,
        model: str | None = None,
        model_config: Mapping[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ):
        self.operation = OperationMetadata(model=model)
        self._input = None
        self._model_config = None
        self._tools = None
        self.input = input
        self.output = output
        self.model = model
        self.model_config = model_config
        self.usage = usage
        self.tools = tools

    def _set_input_payload(self, field: str, value: Any) -> None:
        if self.operation.detail != "content":
            setattr(self, f"_{field}", _capture_payload(self.operation, value))
            return
        # Keep submitted messages ahead of optional configuration and tool definitions.
        payload = {"input": self._input, "model_config": self._model_config, "tools": self._tools}
        payload[field] = value
        captured = bound_content({key: item for key, item in payload.items() if item is not None})
        if not isinstance(captured, Mapping):
            captured = {"input": [{"omitted": True, "reason": "serialization_failed"}]}
        captured = cast(Mapping[str, Any], captured)
        self._input = captured.get("input")
        self._model_config = captured.get("model_config")
        self._tools = captured.get("tools")
        if captured.get("truncated"):
            marker = {"truncated": True}
            if self._input is None:
                self._input = [marker]
            elif isinstance(self._input, list) and marker not in self._input:
                self._input = [*self._input, marker]

    @property
    def input(self) -> Sequence[Mapping[str, Any]] | None:
        return self._input

    @input.setter
    def input(self, value: Sequence[Mapping[str, Any]] | None) -> None:
        self._set_input_payload("input", value)

    @property
    def model_config(self) -> Mapping[str, Any] | None:
        return self._model_config

    @model_config.setter
    def model_config(self, value: Mapping[str, Any] | None) -> None:
        self._set_input_payload("model_config", value)

    @property
    def tools(self) -> Sequence[Mapping[str, Any]] | None:
        return self._tools

    @tools.setter
    def tools(self, value: Sequence[Mapping[str, Any]] | None) -> None:
        self._set_input_payload("tools", value)

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
            **({"tools": self.tools} if self.tools is not None else {}),
        }


class ResponseSpanData(SpanData):
    """
    Represents a Response Span in the trace.
    Default capture keeps the SDK response and input. Content capture keeps
    bounded input, output and instructions without retaining the raw response.
    """

    __slots__ = (
        "response",
        "input",
        "output",
        "model_config",
        "usage",
        "_response_id",
        "operation",
    )

    def __init__(
        self,
        response: Response | None = None,
        input: str | list[ResponseInputItemParam] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.response = response
        # This is not used by the OpenAI trace processors, but is useful for other tracing
        # processor implementations
        self.input = input
        self.output: Sequence[Mapping[str, Any]] | None = None
        self.model_config: Mapping[str, Any] | None = None
        self.usage = usage
        self._response_id: str | None = None
        self.operation = OperationMetadata()

    @property
    def type(self) -> str:
        return "response"

    def export(self) -> dict[str, Any]:
        response_id = self.response.id if self.response is not None else self._response_id
        if self.operation.operation_id is not None:
            response_id = self.operation.response_id
        return {
            "type": self.type,
            "response_id": response_id,
            "usage": self.usage,
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
        "input_format",
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

    __slots__ = ("input", "output", "output_format", "model", "model_config", "first_content_at")

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

    __slots__ = ("input",)

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
