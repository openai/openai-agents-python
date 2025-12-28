from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..logger import logger
from .setup import get_trace_provider
from .span_data import (
    AgentSpanData,
    CustomSpanData,
    FunctionSpanData,
    GenerationSpanData,
    GuardrailSpanData,
    HandoffSpanData,
    MCPListToolsSpanData,
    ResponseSpanData,
    SpeechGroupSpanData,
    SpeechSpanData,
    TranscriptionSpanData,
)
from .spans import Span
from .traces import Trace

if TYPE_CHECKING:
    from openai.types.responses import Response


def trace(
    workflow_name: str,
    trace_id: str | None = None,
    group_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    disabled: bool = False,
) -> Trace:
    """
    Create a new trace. The trace will not be started automatically; you should either use
    it as a context manager (`with trace(...):`) or call `trace.start()` + `trace.finish()`
    manually.

    In addition to the workflow name and optional grouping identifier, you can provide
    an arbitrary metadata dictionary to attach additional user-defined information to
    the trace.

    Args:
        workflow_name: The name of the logical app or workflow. For example, you might provide
            "code_bot" for a coding agent, or "customer_support_agent" for a customer support agent.
        trace_id: The ID of the trace. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_trace_id()` to generate a trace ID, to guarantee that IDs are
            correctly formatted.
        group_id: Optional grouping identifier to link multiple traces from the same conversation
            or process. For instance, you might use a chat thread ID.
        metadata: Optional dictionary of additional metadata to attach to the trace.
        disabled: If True, we will return a Trace but the Trace will not be recorded.

    Returns:
        The newly created trace object.
    """
    current_trace = get_trace_provider().get_current_trace()
    if current_trace:
        logger.warning(
            "Trace already exists. Creating a new trace, but this is probably a mistake."
        )

    return get_trace_provider().create_trace(
        name=workflow_name,
        trace_id=trace_id,
        group_id=group_id,
        metadata=metadata,
        disabled=disabled,
    )


def get_current_trace() -> Trace | None:
    """Returns the currently active trace, if present."""
    return get_trace_provider().get_current_trace()


def get_current_span() -> Span[Any] | None:
    """Returns the currently active span, if present."""
    return get_trace_provider().get_current_span()


def agent_span(
    name: str,
    handoffs: list[str] | None = None,
    tools: list[str] | None = None,
    output_type: str | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[AgentSpanData]:
    """Create a new agent span. The span will not be started automatically, you should either do
    `with agent_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        name: The name of the agent.
        handoffs: Optional list of agent names to which this agent could hand off control.
        tools: Optional list of tool names available to this agent.
        output_type: Optional name of the output type produced by the agent.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.

    Returns:
        The newly created agent span.
    """
    return get_trace_provider().create_span(
        span_data=AgentSpanData(name=name, handoffs=handoffs, tools=tools, output_type=output_type),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def function_span(
    name: str,
    input: str | None = None,
    output: str | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[FunctionSpanData]:
    """Create a new function span. The span will not be started automatically, you should either do
    `with function_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        name: The name of the function.
        input: The input to the function.
        output: The output of the function.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.

    Returns:
        The newly created function span.
    """
    return get_trace_provider().create_span(
        span_data=FunctionSpanData(name=name, input=input, output=output),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def generation_span(
    input: Sequence[Mapping[str, Any]] | None = None,
    output: Sequence[Mapping[str, Any]] | None = None,
    model: str | None = None,
    model_config: Mapping[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[GenerationSpanData]:
    """Create a new generation span. The span will not be started automatically, you should either
    do `with generation_span() ...` or call `span.start()` + `span.finish()` manually.

    This span captures the details of a model generation, including the
    input message sequence, any generated outputs, the model name and
    configuration, and usage data. If you only need to capture a model
    response identifier, use `response_span()` instead.

    Args:
        input: The sequence of input messages sent to the model.
        output: The sequence of output messages received from the model.
        model: The model identifier used for the generation.
        model_config: The model configuration (hyperparameters) used.
        usage: A dictionary of usage information (input tokens, output tokens, etc.).
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.

    Returns:
        The newly created generation span.
    """
    return get_trace_provider().create_span(
        span_data=GenerationSpanData(
            input=input,
            output=output,
            model=model,
            model_config=model_config,
            usage=usage,
        ),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def response_span(
    response: Response | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[ResponseSpanData]:
    """Create a new response span. The span will not be started automatically, you should either do
    `with response_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        response: The OpenAI Response object.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.
    """
    return get_trace_provider().create_span(
        span_data=ResponseSpanData(response=response),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def handoff_span(
    from_agent: str | None = None,
    to_agent: str | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[HandoffSpanData]:
    """Create a new handoff span. The span will not be started automatically, you should either do
    `with handoff_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        from_agent: The name of the agent that is handing off.
        to_agent: The name of the agent that is receiving the handoff.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.

    Returns:
        The newly created handoff span.
    """
    return get_trace_provider().create_span(
        span_data=HandoffSpanData(from_agent=from_agent, to_agent=to_agent),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def custom_span(
    name: str,
    data: dict[str, Any] | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[CustomSpanData]:
    """Create a new custom span, to which you can add your own metadata. The span will not be
    started automatically, you should either do `with custom_span() ...` or call
    `span.start()` + `span.finish()` manually.

    Args:
        name: The name of the custom span.
        data: Arbitrary structured data to associate with the span.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.

    Returns:
        The newly created custom span.
    """
    return get_trace_provider().create_span(
        span_data=CustomSpanData(name=name, data=data or {}),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def guardrail_span(
    name: str,
    triggered: bool = False,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[GuardrailSpanData]:
    """Create a new guardrail span. The span will not be started automatically, you should either
    do `with guardrail_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        name: The name of the guardrail.
        triggered: Whether the guardrail was triggered.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.
    """
    return get_trace_provider().create_span(
        span_data=GuardrailSpanData(name=name, triggered=triggered),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def transcription_span(
    model: str | None = None,
    input: str | None = None,
    input_format: str | None = "pcm",
    output: str | None = None,
    model_config: Mapping[str, Any] | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[TranscriptionSpanData]:
    """Create a new transcription span. The span will not be started automatically, you should
    either do `with transcription_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        model: The name of the model used for the speech-to-text.
        input: The audio input of the speech-to-text transcription, as a base64 encoded string of
            audio bytes.
        input_format: The format of the audio input (defaults to "pcm").
        output: The output of the speech-to-text transcription.
        model_config: The model configuration (hyperparameters) used.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.

    Returns:
        The newly created speech-to-text span.
    """
    return get_trace_provider().create_span(
        span_data=TranscriptionSpanData(
            input=input,
            input_format=input_format,
            output=output,
            model=model,
            model_config=model_config,
        ),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def speech_span(
    model: str | None = None,
    input: str | None = None,
    output: str | None = None,
    output_format: str | None = "pcm",
    model_config: Mapping[str, Any] | None = None,
    first_content_at: str | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[SpeechSpanData]:
    """Create a new speech span. The span will not be started automatically, you should either do
    `with speech_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        model: The name of the model used for the text-to-speech.
        input: The text input of the text-to-speech.
        output: The audio output of the text-to-speech as base64 encoded string of PCM audio bytes.
        output_format: The format of the audio output (defaults to "pcm").
        model_config: The model configuration (hyperparameters) used.
        first_content_at: The time of the first byte of the audio output.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.
    """
    return get_trace_provider().create_span(
        span_data=SpeechSpanData(
            model=model,
            input=input,
            output=output,
            output_format=output_format,
            model_config=model_config,
            first_content_at=first_content_at,
        ),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def speech_group_span(
    input: str | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[SpeechGroupSpanData]:
    """Create a new speech group span. The span will not be started automatically, you should
    either do `with speech_group_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        input: The input text used for the speech request.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.
    """
    return get_trace_provider().create_span(
        span_data=SpeechGroupSpanData(input=input),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )


def mcp_tools_span(
    server: str | None = None,
    result: list[str] | None = None,
    span_id: str | None = None,
    parent: Trace | Span[Any] | None = None,
    disabled: bool = False,
) -> Span[MCPListToolsSpanData]:
    """Create a new MCP list tools span. The span will not be started automatically, you should
    either do `with mcp_tools_span() ...` or call `span.start()` + `span.finish()` manually.

    Args:
        server: The name of the MCP server.
        result: The result of the MCP list tools call.
        span_id: The ID of the span. Optional. If not provided, we will generate an ID. We
            recommend using `util.gen_span_id()` to generate a span ID, to guarantee that IDs are
            correctly formatted.
        parent: The parent span or trace. If not provided, we will automatically use the current
            trace/span as the parent.
        disabled: If True, we will return a Span but the Span will not be recorded.
    """
    return get_trace_provider().create_span(
        span_data=MCPListToolsSpanData(server=server, result=result),
        span_id=span_id,
        parent=parent,
        disabled=disabled,
    )
