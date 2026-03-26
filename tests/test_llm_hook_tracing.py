from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from agents import Agent, AgentHooks, RunHooks, Runner, get_current_span
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.run_context import RunContextWrapper, TContext
from agents.tool import Tool
from agents.tracing.span_data import ResponseSpanData

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message
from .testing_processor import fetch_normalized_spans, fetch_ordered_spans


class SpanAwareRunHooks(RunHooks):
    def __init__(self) -> None:
        self.start_span_types: list[str | None] = []
        self.end_span_types: list[str | None] = []

    async def on_llm_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        current_span = get_current_span()
        self.start_span_types.append(current_span.span_data.type if current_span else None)
        if current_span is not None and isinstance(current_span.span_data, ResponseSpanData):
            current_span.span_data.metadata["run_hook_start_agent"] = agent.name

    async def on_llm_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        response: ModelResponse,
    ) -> None:
        current_span = get_current_span()
        self.end_span_types.append(current_span.span_data.type if current_span else None)
        if current_span is not None and isinstance(current_span.span_data, ResponseSpanData):
            current_span.span_data.metadata["run_hook_end_response_id"] = response.response_id


class SpanAwareAgentHooks(AgentHooks):
    def __init__(self) -> None:
        self.start_span_types: list[str | None] = []
        self.end_span_types: list[str | None] = []

    async def on_llm_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        current_span = get_current_span()
        self.start_span_types.append(current_span.span_data.type if current_span else None)
        if current_span is not None and isinstance(current_span.span_data, ResponseSpanData):
            current_span.span_data.metadata["agent_hook_start_agent"] = agent.name

    async def on_llm_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        response: ModelResponse,
    ) -> None:
        current_span = get_current_span()
        self.end_span_types.append(current_span.span_data.type if current_span else None)
        if current_span is not None and isinstance(current_span.span_data, ResponseSpanData):
            current_span.span_data.metadata["agent_hook_end_response_id"] = response.response_id


def _find_response_spans() -> list[ResponseSpanData]:
    return [
        span.span_data
        for span in fetch_ordered_spans()
        if isinstance(span.span_data, ResponseSpanData)
    ]


def _find_exported_response_spans() -> list[dict[str, Any]]:
    exported_spans: list[dict[str, Any]] = []

    def _walk(node: dict[str, Any]) -> None:
        if node.get("type") == "response":
            exported_spans.append(node)
        for child in node.get("children", []):
            _walk(child)

    for trace in fetch_normalized_spans():
        _walk(trace)

    return exported_spans


def _make_legacy_signature_model() -> FakeModel:
    model = FakeModel()
    original_get_response = model.get_response
    original_stream_response = model.stream_response

    async def legacy_get_response(
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any | None,
    ) -> ModelResponse:
        return await original_get_response(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

    async def legacy_stream_response(
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        async for event in original_stream_response(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        ):
            yield event

    legacy_model = cast(Any, model)
    legacy_model.get_response = legacy_get_response
    legacy_model.stream_response = legacy_stream_response
    return model


@pytest.mark.asyncio
async def test_non_streamed_llm_hooks_can_mutate_response_span_metadata() -> None:
    run_hooks = SpanAwareRunHooks()
    agent_hooks = SpanAwareAgentHooks()
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])
    agent = Agent(name="hooked-agent", model=model, hooks=agent_hooks)

    await Runner.run(agent, input="hello", hooks=run_hooks)

    assert run_hooks.start_span_types == ["response"]
    assert run_hooks.end_span_types == ["response"]
    assert agent_hooks.start_span_types == ["response"]
    assert agent_hooks.end_span_types == ["response"]

    response_spans = _find_response_spans()
    assert len(response_spans) == 1
    assert response_spans[0].metadata == {
        "run_hook_start_agent": "hooked-agent",
        "run_hook_end_response_id": "resp-789",
        "agent_hook_start_agent": "hooked-agent",
        "agent_hook_end_response_id": "resp-789",
    }
    assert _find_exported_response_spans() == [
        {
            "type": "response",
            "data": {
                "response_id": "resp-789",
                "metadata": {
                    "run_hook_start_agent": "hooked-agent",
                    "run_hook_end_response_id": "resp-789",
                    "agent_hook_start_agent": "hooked-agent",
                    "agent_hook_end_response_id": "resp-789",
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_streamed_llm_hooks_can_mutate_response_span_metadata() -> None:
    run_hooks = SpanAwareRunHooks()
    agent_hooks = SpanAwareAgentHooks()
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])
    agent = Agent(name="streamed-agent", model=model, hooks=agent_hooks)

    result = Runner.run_streamed(agent, input="hello", hooks=run_hooks)
    async for _ in result.stream_events():
        pass

    assert run_hooks.start_span_types == ["response"]
    assert run_hooks.end_span_types == ["response"]
    assert agent_hooks.start_span_types == ["response"]
    assert agent_hooks.end_span_types == ["response"]

    response_spans = _find_response_spans()
    assert len(response_spans) == 1
    assert response_spans[0].metadata == {
        "run_hook_start_agent": "streamed-agent",
        "run_hook_end_response_id": "resp-789",
        "agent_hook_start_agent": "streamed-agent",
        "agent_hook_end_response_id": "resp-789",
    }
    assert _find_exported_response_spans() == [
        {
            "type": "response",
            "data": {
                "response_id": "resp-789",
                "metadata": {
                    "run_hook_start_agent": "streamed-agent",
                    "run_hook_end_response_id": "resp-789",
                    "agent_hook_start_agent": "streamed-agent",
                    "agent_hook_end_response_id": "resp-789",
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_runner_accepts_legacy_models_without_response_span_kwarg() -> None:
    run_hooks = SpanAwareRunHooks()
    model = _make_legacy_signature_model()
    model.set_next_output([get_text_message("legacy-ok")])
    agent = Agent(name="legacy-agent", model=model)

    result = await Runner.run(agent, input="hello", hooks=run_hooks)

    assert result.final_output == "legacy-ok"
    assert run_hooks.start_span_types == ["response"]
    assert run_hooks.end_span_types == ["response"]


@pytest.mark.asyncio
async def test_runner_streamed_accepts_legacy_models_without_response_span_kwarg() -> None:
    run_hooks = SpanAwareRunHooks()
    model = _make_legacy_signature_model()
    model.set_next_output([get_text_message("legacy-stream-ok")])
    agent = Agent(name="legacy-stream-agent", model=model)

    result = Runner.run_streamed(agent, input="hello", hooks=run_hooks)
    async for _ in result.stream_events():
        pass

    assert result.final_output == "legacy-stream-ok"
    assert run_hooks.start_span_types == ["response"]
    assert run_hooks.end_span_types == ["response"]


@pytest.mark.asyncio
async def test_streamed_tool_spans_are_not_nested_under_response_spans() -> None:
    model = FakeModel(tracing_enabled=True)
    model.add_multiple_turn_outputs(
        [
            [get_text_message("a_message"), get_function_tool_call("foo", '{"a": "b"}')],
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="streamed-tool-agent",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )

    result = Runner.run_streamed(agent, input="hello")
    async for _ in result.stream_events():
        pass

    agent_children = fetch_normalized_spans()[0]["children"][0]["children"]
    assert [child["type"] for child in agent_children] == ["response", "function", "response"]
