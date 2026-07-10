from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import omit
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
)
from openai.types.shared.reasoning import Reasoning

from agents import (
    Agent,
    MaxTurnsExceeded,
    ModelProvider,
    ModelSettings,
    ModelTracing,
    RunConfig,
    RunContextWrapper,
    Runner,
    function_tool,
    handoff,
)
from agents.exceptions import UserError
from agents.extensions.experimental.hosted_multi_agent import (
    HostedMultiAgentConfig,
    OpenAIHostedMultiAgentModel,
    get_hosted_agent_metadata,
)
from agents.tool_context import ToolContext

pytestmark = pytest.mark.allow_call_model_methods


def _response(response_id: str, output: Sequence[object]) -> SimpleNamespace:
    return SimpleNamespace(id=response_id, output=list(output), usage=None)


def _usage(
    *,
    input_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        input_tokens_details=SimpleNamespace(
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
        ),
        output_tokens=output_tokens,
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        total_tokens=input_tokens + output_tokens,
    )


def _created(response_id: str, *, sequence_number: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        type="response.created",
        sequence_number=sequence_number,
        response=_response(response_id, []),
    )


def _done(item: object, *, sequence_number: int, output_index: int) -> SimpleNamespace:
    return SimpleNamespace(
        type="response.output_item.done",
        sequence_number=sequence_number,
        output_index=output_index,
        item=item,
        agent=getattr(item, "agent", None),
    )


def _completed(
    response_id: str,
    output: Sequence[object],
    *,
    sequence_number: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        type="response.completed",
        sequence_number=sequence_number,
        response=_response(response_id, output),
    )


def _root_final_message(text: str = "done") -> dict[str, Any]:
    return {
        "id": "msg_root",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "agent": {"agent_name": "/root"},
        "phase": "final_answer",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
                "logprobs": [],
            }
        ],
    }


def _subagent_message(text: str = "working") -> dict[str, Any]:
    return {
        "id": "msg_subagent",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
        "phase": "commentary",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
                "logprobs": [],
            }
        ],
    }


def _tool_flow_events(
    response_id: str,
    first_items: Sequence[object],
    final_items: Sequence[object],
) -> list[object]:
    events: list[object] = [_created(response_id)]
    sequence_number = 1
    for output_index, item in enumerate(first_items):
        sequence_number += 1
        events.append(_done(item, sequence_number=sequence_number, output_index=output_index))
    sequence_number += 1
    events.append(
        SimpleNamespace(
            type="response.inject.created",
            sequence_number=sequence_number,
            response_id=response_id,
        )
    )
    for final_index, item in enumerate(final_items):
        sequence_number += 1
        events.append(
            _done(
                item,
                sequence_number=sequence_number,
                output_index=len(first_items) + final_index,
            )
        )
    sequence_number += 1
    events.append(
        _completed(
            response_id,
            [*first_items, *final_items],
            sequence_number=sequence_number,
        )
    )
    return events


class _DummyConnection:
    def __init__(self, events: Sequence[object]) -> None:
        self.events = deque(events)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, event: dict[str, Any]) -> None:
        self.sent.append(event)

    async def recv(self) -> object:
        if not self.events:
            raise RuntimeError("Dummy WebSocket event queue exhausted")
        return self.events.popleft()

    async def close(self) -> None:
        self.closed = True


class _DummyConnectionManager:
    def __init__(self, connection: _DummyConnection) -> None:
        self.connection = connection

    async def enter(self) -> _DummyConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        await self.connection.close()


class _DummyBetaResponses:
    def __init__(self, event_batches: Sequence[Sequence[object]]) -> None:
        self.connections = [_DummyConnection(events) for events in event_batches]
        self._available_connections = deque(self.connections)
        self.connect_calls: list[dict[str, Any]] = []

    def connect(self, **kwargs: Any) -> _DummyConnectionManager:
        self.connect_calls.append(kwargs)
        return _DummyConnectionManager(self._available_connections.popleft())


class _DummyClient:
    def __init__(self, event_batches: Sequence[Sequence[object]]) -> None:
        self.beta = SimpleNamespace(responses=_DummyBetaResponses(event_batches))


class _StaticModelProvider(ModelProvider):
    def __init__(self, model: OpenAIHostedMultiAgentModel) -> None:
        self.model = model
        self.requested_names: list[str | None] = []

    def get_model(self, model_name: str | None) -> OpenAIHostedMultiAgentModel:
        self.requested_names.append(model_name)
        return self.model


def _model(
    events: Sequence[object] | None = None,
    *,
    event_batches: Sequence[Sequence[object]] | None = None,
    config: HostedMultiAgentConfig | Mapping[str, Any] | None = None,
) -> tuple[OpenAIHostedMultiAgentModel, _DummyClient]:
    client = _DummyClient(event_batches or [events or []])
    model = OpenAIHostedMultiAgentModel(
        model="gpt-5.6-sol",
        openai_client=cast(Any, client),
        config=config,
    )
    return model, client


def test_build_request_enables_beta_and_preserves_context_management() -> None:
    model, _ = _model(config=HostedMultiAgentConfig(max_concurrent_subagents=2))

    kwargs = model._build_response_create_kwargs(
        system_instructions="delegate",
        input="hello",
        model_settings=ModelSettings(
            context_management=[{"type": "compaction", "compact_threshold": 10}]
        ),
        tools=[],
        output_schema=None,
        handoffs=[],
    )

    assert kwargs["multi_agent"] == {
        "enabled": True,
        "max_concurrent_subagents": 2,
    }
    assert kwargs["betas"] == ["responses_multi_agent=v1"]
    assert kwargs["context_management"] == [{"type": "compaction", "compact_threshold": 10}]


def test_model_accepts_config_mapping() -> None:
    model, _ = _model(config={"max_concurrent_subagents": 2})

    assert model.config == HostedMultiAgentConfig(max_concurrent_subagents=2)


def test_model_accepts_omitted_client() -> None:
    model = OpenAIHostedMultiAgentModel(
        model="gpt-5.6-sol",
        config={"max_concurrent_subagents": 2},
    )

    assert model._client is None


@pytest.mark.parametrize("value", [0, -1, True])
def test_config_rejects_non_positive_concurrency(value: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        HostedMultiAgentConfig(max_concurrent_subagents=value)


def test_reserved_settings_fail_before_transport() -> None:
    model, _ = _model()

    with pytest.raises(UserError, match="through OpenAIHostedMultiAgentModel"):
        model._build_response_create_kwargs(
            system_instructions=None,
            input="hello",
            model_settings=ModelSettings(extra_args={"multi_agent": {"enabled": True}}),
            tools=[],
            output_schema=None,
            handoffs=[],
        )

    with pytest.raises(UserError, match="max_tool_calls"):
        model._build_response_create_kwargs(
            system_instructions=None,
            input="hello",
            model_settings=ModelSettings(extra_args={"max_tool_calls": 1}),
            tools=[],
            output_schema=None,
            handoffs=[],
        )

    with pytest.raises(UserError, match="reasoning.summary"):
        model._build_response_create_kwargs(
            system_instructions=None,
            input="hello",
            model_settings=ModelSettings(reasoning=Reasoning(summary="auto")),
            tools=[],
            output_schema=None,
            handoffs=[],
        )


def test_sdk_handoffs_are_rejected() -> None:
    model, _ = _model()
    target = Agent(name="target")

    with pytest.raises(UserError, match="cannot be combined with SDK handoffs"):
        model._build_response_create_kwargs(
            system_instructions=None,
            input="hello",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[handoff(target)],
        )


@pytest.mark.asyncio
async def test_runner_routes_subagent_tool_call_without_exposing_hosted_items() -> None:
    first_items = [
        {
            "id": "mac_1",
            "type": "multi_agent_call",
            "call_id": "call_spawn",
            "action": "spawn_agent",
            "arguments": "{}",
            "agent": {"agent_name": "/root"},
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_lookup",
            "name": "lookup_document",
            "arguments": '{"section":"alpha"}',
            "status": "completed",
            "agent": {"agent_name": "/root/researcher"},
        },
    ]
    final_items = [
        _subagent_message(),
        {
            "id": "maco_1",
            "type": "multi_agent_call_output",
            "call_id": "call_spawn",
            "action": "spawn_agent",
            "output": [],
            "agent": {"agent_name": "/root"},
        },
        _root_final_message(),
    ]
    model, client = _model(_tool_flow_events("resp_1", first_items, final_items))
    callers: list[str] = []

    @function_tool
    def lookup_document(ctx: ToolContext[Any], section: str) -> str:
        metadata = get_hosted_agent_metadata(ctx)
        assert metadata is not None
        callers.append(metadata.agent_name)
        return f"document:{section}"

    agent = Agent(
        name="SDK root",
        instructions="Delegate the lookup and synthesize the answer.",
        model=model,
        tools=[lookup_document],
    )

    result = await Runner.run(
        agent,
        "Compare alpha.",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "done"
    assert callers == ["/root/researcher"]
    assert [item.type for item in result.new_items] == [
        "tool_call_item",
        "tool_call_output_item",
        "message_output_item",
    ]

    connection = client.beta.responses.connections[0]
    assert connection.sent[0]["type"] == "response.create"
    assert "stream" not in connection.sent[0]
    assert "betas" not in connection.sent[0]
    assert connection.sent[1] == {
        "type": "response.inject",
        "response_id": "resp_1",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call_lookup",
                "output": "document:alpha",
            }
        ],
    }
    assert client.beta.responses.connect_calls[0]["extra_headers"]["OpenAI-Beta"] == (
        "responses_multi_agent=v1"
    )
    assert connection.closed


@pytest.mark.asyncio
async def test_runner_injects_two_subagent_calls_into_one_active_response() -> None:
    spawn = {
        "id": "mac_two",
        "type": "multi_agent_call",
        "call_id": "call_spawn_two",
        "action": "spawn_agent",
        "arguments": "{}",
        "agent": {"agent_name": "/root"},
    }
    alpha_call = {
        "id": "fc_alpha",
        "type": "function_call",
        "call_id": "call_alpha",
        "name": "get_proposal",
        "arguments": '{"proposal":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/alpha"},
    }
    beta_call = {
        "id": "fc_beta",
        "type": "function_call",
        "call_id": "call_beta",
        "name": "get_proposal",
        "arguments": '{"proposal":"beta"}',
        "status": "completed",
        "agent": {"agent_name": "/root/beta"},
    }
    alpha_message = _subagent_message("alpha complete")
    alpha_message["id"] = "msg_alpha"
    alpha_message["agent"] = {"agent_name": "/root/alpha"}
    beta_message = _subagent_message("beta complete")
    beta_message["id"] = "msg_beta"
    beta_message["agent"] = {"agent_name": "/root/beta"}
    root_final = _root_final_message("comparison complete")
    output = [spawn, alpha_call, alpha_message, beta_call, beta_message, root_final]
    events = [
        _created("resp_two"),
        _done(spawn, sequence_number=2, output_index=0),
        _done(alpha_call, sequence_number=3, output_index=1),
        SimpleNamespace(
            type="response.inject.created",
            sequence_number=4,
            response_id="resp_two",
        ),
        _done(alpha_message, sequence_number=5, output_index=2),
        _done(beta_call, sequence_number=6, output_index=3),
        SimpleNamespace(
            type="response.inject.created",
            sequence_number=7,
            response_id="resp_two",
        ),
        _done(beta_message, sequence_number=8, output_index=4),
        _done(root_final, sequence_number=9, output_index=5),
        _completed("resp_two", output, sequence_number=10),
    ]
    model, client = _model(events)
    callers: list[str] = []

    @function_tool
    def get_proposal(ctx: ToolContext[Any], proposal: str) -> str:
        metadata = get_hosted_agent_metadata(ctx)
        assert metadata is not None
        callers.append(metadata.agent_name)
        return f"proposal:{proposal}"

    result = await Runner.run(
        Agent(name="SDK root", model=model, tools=[get_proposal]),
        "Compare both proposals.",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "comparison complete"
    assert callers == ["/root/alpha", "/root/beta"]
    inject_frames = [
        frame
        for frame in client.beta.responses.connections[0].sent
        if frame["type"] == "response.inject"
    ]
    assert [frame["input"][0]["call_id"] for frame in inject_frames] == [
        "call_alpha",
        "call_beta",
    ]
    assert [item.type for item in result.new_items] == [
        "tool_call_item",
        "tool_call_output_item",
        "tool_call_item",
        "tool_call_output_item",
        "message_output_item",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_injection_failure_after_completion_starts_continuation_response(
    streamed: bool,
) -> None:
    function_call = {
        "id": "fc_fallback",
        "type": "function_call",
        "call_id": "call_fallback",
        "name": "lookup_document",
        "arguments": '{"section":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
    }
    function_output = {
        "type": "function_call_output",
        "call_id": "call_fallback",
        "output": "document:alpha",
    }
    completed_first_response = _response("resp_old", [function_call])
    completed_first_response.usage = _usage(
        input_tokens=12,
        cached_tokens=3,
        cache_write_tokens=1,
        output_tokens=4,
        reasoning_tokens=2,
    )
    first_events = [
        _created("resp_old"),
        _done(function_call, sequence_number=2, output_index=0),
        SimpleNamespace(
            type="response.completed",
            sequence_number=3,
            response=completed_first_response,
        ),
        SimpleNamespace(
            type="response.inject.failed",
            sequence_number=4,
            response_id="resp_old",
            input=[function_output],
            error=SimpleNamespace(
                code="response_already_completed",
                message="The response already completed.",
            ),
        ),
    ]
    final_message = _root_final_message("continued")
    completed_second_response = _response("resp_new", [final_message])
    completed_second_response.usage = _usage(
        input_tokens=7,
        cached_tokens=2,
        cache_write_tokens=4,
        output_tokens=3,
        reasoning_tokens=1,
    )
    second_events = [
        _created("resp_new"),
        _done(final_message, sequence_number=2, output_index=0),
        SimpleNamespace(
            type="response.completed",
            sequence_number=3,
            response=completed_second_response,
        ),
    ]
    model, client = _model([*first_events, *second_events])

    @function_tool
    def lookup_document(section: str) -> str:
        return f"document:{section}"

    agent = Agent(
        name="SDK root",
        model=model,
        model_settings=ModelSettings(store=False),
        tools=[lookup_document],
    )
    result: Any
    if streamed:
        result = Runner.run_streamed(
            agent,
            "Inspect alpha.",
            run_config=RunConfig(tracing_disabled=True),
        )
        _ = [event async for event in result.stream_events()]
    else:
        result = await Runner.run(
            agent,
            "Inspect alpha.",
            run_config=RunConfig(tracing_disabled=True),
        )

    assert result.final_output == "continued"
    assert result.context_wrapper.usage.input_tokens == 19
    assert result.context_wrapper.usage.input_tokens_details.cached_tokens == 5
    assert (
        getattr(
            result.context_wrapper.usage.input_tokens_details,
            "cache_write_tokens",
            None,
        )
        == 5
    )
    assert result.context_wrapper.usage.output_tokens == 7
    assert result.context_wrapper.usage.output_tokens_details.reasoning_tokens == 3
    assert result.context_wrapper.usage.total_tokens == 26
    assert result.context_wrapper.usage.requests == 2
    assert len(result.context_wrapper.usage.request_usage_entries) == 2
    assert [entry.total_tokens for entry in result.context_wrapper.usage.request_usage_entries] == [
        16,
        10,
    ]

    assert len(client.beta.responses.connections) == 1
    connection = client.beta.responses.connections[0]
    continuation_frame = connection.sent[2]
    assert continuation_frame["type"] == "response.create"
    assert continuation_frame["previous_response_id"] == "resp_old"
    assert continuation_frame["input"] == [function_output]
    assert connection.closed


@pytest.mark.asyncio
async def test_injection_failure_continues_with_conversation_without_previous_response_id() -> None:
    function_call = {
        "id": "fc_conversation",
        "type": "function_call",
        "call_id": "call_conversation",
        "name": "lookup_document",
        "arguments": '{"section":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
    }
    function_output = {
        "type": "function_call_output",
        "call_id": "call_conversation",
        "output": "document:alpha",
    }
    final_message = _root_final_message("continued in conversation")
    model, client = _model(
        [
            _created("resp_conversation_old"),
            _done(function_call, sequence_number=2, output_index=0),
            _completed("resp_conversation_old", [function_call], sequence_number=3),
            SimpleNamespace(
                type="response.inject.failed",
                sequence_number=4,
                response_id="resp_conversation_old",
                input=[function_output],
                error=SimpleNamespace(
                    code="response_already_completed",
                    message="The response already completed.",
                ),
            ),
            _created("resp_conversation_new"),
            _done(final_message, sequence_number=2, output_index=0),
            _completed("resp_conversation_new", [final_message], sequence_number=3),
        ]
    )

    @function_tool
    def lookup_document(section: str) -> str:
        return f"document:{section}"

    result = await Runner.run(
        Agent(name="SDK root", model=model, tools=[lookup_document]),
        "Inspect alpha.",
        conversation_id="conv_123",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "continued in conversation"
    connection = client.beta.responses.connections[0]
    continuation_frame = connection.sent[2]
    assert continuation_frame["conversation"] == "conv_123"
    assert "previous_response_id" not in continuation_frame
    assert continuation_frame["input"] == [function_output]
    assert connection.closed


@pytest.mark.asyncio
async def test_nonrecoverable_injection_failure_closes_active_response() -> None:
    function_call = {
        "id": "fc_failed",
        "type": "function_call",
        "call_id": "call_failed",
        "name": "lookup_document",
        "arguments": '{"section":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
    }
    events = [
        _created("resp_failed"),
        _done(function_call, sequence_number=2, output_index=0),
        SimpleNamespace(
            type="response.inject.failed",
            sequence_number=3,
            response_id="resp_failed",
            input=[],
            error=SimpleNamespace(code="response_not_found", message="Missing response."),
        ),
    ]
    model, client = _model(events)

    @function_tool
    def lookup_document(section: str) -> str:
        return f"document:{section}"

    with pytest.raises(UserError, match="response_not_found"):
        await Runner.run(
            Agent(name="SDK root", model=model, tools=[lookup_document]),
            "Inspect alpha.",
            run_config=RunConfig(tracing_disabled=True),
        )

    assert client.beta.responses.connections[0].closed
    assert model._active_response is None


@pytest.mark.asyncio
async def test_model_close_releases_paused_response() -> None:
    function_call = {
        "id": "fc_close",
        "type": "function_call",
        "call_id": "call_close",
        "name": "lookup_document",
        "arguments": '{"section":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
    }
    model, client = _model(
        [
            _created("resp_close"),
            _done(function_call, sequence_number=2, output_index=0),
        ]
    )

    response = await model.get_response(
        system_instructions=None,
        input="Inspect alpha.",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert response.response_id == "resp_close"
    assert not client.beta.responses.connections[0].closed
    await model.close()
    assert client.beta.responses.connections[0].closed
    assert model._active_response is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_runner_closes_paused_response_when_max_turns_is_exceeded(
    streamed: bool,
) -> None:
    function_call = {
        "id": "fc_max_turns",
        "type": "function_call",
        "call_id": "call_max_turns",
        "name": "lookup_document",
        "arguments": '{"section":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
    }
    model, client = _model(
        [
            _created("resp_max_turns"),
            _done(function_call, sequence_number=2, output_index=0),
        ]
    )

    @function_tool
    def lookup_document(section: str) -> str:
        return f"document:{section}"

    agent = Agent(name="SDK root", model=model, tools=[lookup_document])
    with pytest.raises(MaxTurnsExceeded):
        if streamed:
            result = Runner.run_streamed(
                agent,
                "Inspect alpha.",
                max_turns=1,
                run_config=RunConfig(tracing_disabled=True),
            )
            _ = [event async for event in result.stream_events()]
        else:
            await Runner.run(
                agent,
                "Inspect alpha.",
                max_turns=1,
                run_config=RunConfig(tracing_disabled=True),
            )

    assert client.beta.responses.connections[0].closed
    assert model._active_response is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("model_source", ["run_config", "agent"])
async def test_runner_cleans_up_provider_resolved_paused_response(
    streamed: bool,
    model_source: str,
) -> None:
    function_call = {
        "id": "fc_provider",
        "type": "function_call",
        "call_id": "call_provider",
        "name": "lookup_document",
        "arguments": '{"section":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
    }
    model, client = _model(
        [
            _created("resp_provider"),
            _done(function_call, sequence_number=2, output_index=0),
        ]
    )
    provider = _StaticModelProvider(model)

    @function_tool
    def lookup_document(section: str) -> str:
        return f"document:{section}"

    agent = Agent(
        name="SDK root",
        model="hosted" if model_source == "agent" else "unused",
        tools=[lookup_document],
    )
    run_config = RunConfig(
        model="hosted" if model_source == "run_config" else None,
        model_provider=provider,
        tracing_disabled=True,
    )

    with pytest.raises(MaxTurnsExceeded):
        if streamed:
            result = Runner.run_streamed(
                agent,
                "Inspect alpha.",
                max_turns=1,
                run_config=run_config,
            )
            _ = [event async for event in result.stream_events()]
        else:
            await Runner.run(
                agent,
                "Inspect alpha.",
                max_turns=1,
                run_config=run_config,
            )

    assert provider.requested_names == ["hosted"]
    assert client.beta.responses.connections[0].closed
    assert model._active_response is None


@pytest.mark.asyncio
async def test_runner_closes_paused_response_when_tool_execution_fails() -> None:
    function_call = {
        "id": "fc_tool_failure",
        "type": "function_call",
        "call_id": "call_tool_failure",
        "name": "lookup_document",
        "arguments": '{"section":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
    }
    recovered_message = _root_final_message("recovered")
    model, client = _model(
        event_batches=[
            [
                _created("resp_tool_failure"),
                _done(function_call, sequence_number=2, output_index=0),
            ],
            [
                _created("resp_recovered"),
                _done(recovered_message, sequence_number=2, output_index=0),
                _completed("resp_recovered", [recovered_message], sequence_number=3),
            ],
        ]
    )

    def lookup_document(section: str) -> str:
        raise RuntimeError(f"Unable to read {section}")

    tool = function_tool(lookup_document, failure_error_function=None)
    agent = Agent(name="SDK root", model=model, tools=[tool])
    with pytest.raises(UserError, match="Unable to read alpha"):
        await Runner.run(
            agent,
            "Inspect alpha.",
            run_config=RunConfig(tracing_disabled=True),
        )

    assert client.beta.responses.connections[0].closed
    assert model._active_response is None

    result = await Runner.run(
        agent,
        "Try again.",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "recovered"
    assert len(client.beta.responses.connect_calls) == 2
    assert client.beta.responses.connections[1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_concurrent_run_does_not_consume_or_close_paused_response(
    streamed: bool,
) -> None:
    function_call = {
        "id": "fc_concurrent",
        "type": "function_call",
        "call_id": "call_concurrent",
        "name": "lookup_document",
        "arguments": '{"section":"alpha"}',
        "status": "completed",
        "agent": {"agent_name": "/root/researcher"},
    }
    final_message = _root_final_message("continued")
    model, client = _model(_tool_flow_events("resp_concurrent", [function_call], [final_message]))
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    @function_tool
    async def lookup_document(section: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return f"document:{section}"

    agent = Agent(name="SDK root", model=model, tools=[lookup_document])
    shared_context = RunContextWrapper(context=None)

    async def run_agent(prompt: str) -> Any:
        if not streamed:
            return await Runner.run(
                agent,
                prompt,
                context=shared_context,
                run_config=RunConfig(tracing_disabled=True),
            )
        result = Runner.run_streamed(
            agent,
            prompt,
            context=shared_context,
            run_config=RunConfig(tracing_disabled=True),
        )
        _ = [event async for event in result.stream_events()]
        return result

    first_run = asyncio.create_task(run_agent("Inspect alpha."))
    await tool_started.wait()
    paused_response = model._active_response
    assert paused_response is not None

    try:
        with pytest.raises(UserError, match="another agent run"):
            await run_agent("Inspect beta.")

        assert model._active_response is paused_response
        assert len(client.beta.responses.connect_calls) == 1
        assert not client.beta.responses.connections[0].closed
    finally:
        release_tool.set()

    result = await first_run
    assert result.final_output == "continued"
    assert client.beta.responses.connections[0].closed


@pytest.mark.asyncio
async def test_non_root_and_unknown_items_are_filtered_from_model_response() -> None:
    output = [
        _subagent_message(),
        {"type": "future_beta_item", "id": "future_1"},
        _root_final_message(),
    ]
    model, _ = _model(
        [
            _created("resp_1"),
            *[
                _done(item, sequence_number=index + 2, output_index=index)
                for index, item in enumerate(output)
            ],
            _completed("resp_1", output, sequence_number=5),
        ]
    )

    response = await model.get_response(
        system_instructions=None,
        input="hello",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert len(response.output) == 1
    assert isinstance(response.output[0], ResponseOutputMessage)
    assert get_hosted_agent_metadata(response.output[0]) is not None


@pytest.mark.asyncio
async def test_beta_usage_details_are_converted_to_stable_usage_types() -> None:
    final_message = _root_final_message("usage")
    terminal_response = _response("resp_usage", [final_message])
    terminal_response.usage = SimpleNamespace(
        input_tokens=12,
        input_tokens_details=SimpleNamespace(cached_tokens=3, cache_write_tokens=1),
        output_tokens=4,
        output_tokens_details=SimpleNamespace(reasoning_tokens=2),
        total_tokens=16,
    )
    model, _ = _model(
        [
            _created("resp_usage"),
            _done(final_message, sequence_number=2, output_index=0),
            SimpleNamespace(
                type="response.completed",
                sequence_number=3,
                response=terminal_response,
            ),
        ]
    )

    response = await model.get_response(
        system_instructions=None,
        input="hello",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert response.usage.input_tokens == 12
    assert response.usage.input_tokens_details.cached_tokens == 3
    assert response.usage.output_tokens_details.reasoning_tokens == 2


@pytest.mark.asyncio
async def test_stream_preserves_hosted_raw_event_and_normalizes_root_output() -> None:
    output = [_subagent_message(), _root_final_message()]
    model, _ = _model(
        [
            _created("resp_stream"),
            _done(output[0], sequence_number=2, output_index=0),
            _done(output[1], sequence_number=3, output_index=1),
            _completed("resp_stream", output, sequence_number=4),
        ]
    )
    events = [
        event
        async for event in model.stream_response(
            system_instructions=None,
            input="hello",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
    ]

    done_events = [event for event in events if isinstance(event, ResponseOutputItemDoneEvent)]
    assert len(done_events) == 1
    assert isinstance(done_events[0].item, ResponseOutputMessage)
    raw_subagent_event = next(
        event
        for event in events
        if getattr(event, "type", None) == "response.output_item.done"
        and not isinstance(event, ResponseOutputItemDoneEvent)
    )
    assert get_hosted_agent_metadata(cast(Any, raw_subagent_event).item) is not None
    completed_event = next(event for event in events if isinstance(event, ResponseCompletedEvent))
    assert len(completed_event.response.output) == 1
    assert isinstance(completed_event.response.output[0], ResponseOutputMessage)


@pytest.mark.asyncio
async def test_streamed_runner_routes_tools_and_emits_raw_hosted_items() -> None:
    first_items = [
        {
            "id": "mac_stream",
            "type": "multi_agent_call",
            "call_id": "call_spawn_stream",
            "action": "spawn_agent",
            "arguments": "{}",
            "agent": {"agent_name": "/root"},
        },
        {
            "id": "fc_stream",
            "type": "function_call",
            "call_id": "call_lookup_stream",
            "name": "lookup_document",
            "arguments": '{"section":"beta"}',
            "status": "completed",
            "agent": {"agent_name": "/root/reviewer"},
        },
    ]
    final_items = [_subagent_message("review complete"), _root_final_message("stream done")]
    model, _ = _model(_tool_flow_events("resp_stream", first_items, final_items))
    callers: list[str] = []

    @function_tool
    def lookup_document(ctx: ToolContext[Any], section: str) -> str:
        metadata = get_hosted_agent_metadata(ctx)
        assert metadata is not None
        callers.append(metadata.agent_name)
        return f"document:{section}"

    agent = Agent(
        name="SDK root",
        instructions="Delegate and synthesize.",
        model=model,
        tools=[lookup_document],
    )
    result = Runner.run_streamed(
        agent,
        "Compare beta.",
        run_config=RunConfig(tracing_disabled=True),
    )
    events = [event async for event in result.stream_events()]

    assert result.final_output == "stream done"
    assert callers == ["/root/reviewer"]
    assert any(
        event.type == "raw_response_event"
        and getattr(event.data, "type", None) == "response.output_item.done"
        and getattr(event.data, "item", {}).get("type") == "multi_agent_call"
        for event in events
    )


def test_tools_with_approval_settings_are_rejected_before_transport() -> None:
    model, client = _model()

    async def requires_approval(
        _ctx: Any,
        _params: dict[str, Any],
        _call_id: str,
    ) -> bool:
        return False

    @function_tool(needs_approval=True)
    def always_sensitive(section: str) -> str:
        return f"sensitive:{section}"

    @function_tool(needs_approval=requires_approval)
    def dynamically_sensitive(section: str) -> str:
        return f"sensitive:{section}"

    for tool in (always_sensitive, dynamically_sensitive):
        with pytest.raises(UserError, match="does not support SDK tool approval interruptions"):
            model._build_response_create_kwargs(
                system_instructions=None,
                input="hello",
                model_settings=ModelSettings(),
                tools=[tool],
                output_schema=None,
                handoffs=[],
            )

    assert client.beta.responses.connect_calls == []


def test_default_config_omits_service_default_concurrency() -> None:
    model, _ = _model()
    kwargs = model._build_response_create_kwargs(
        system_instructions=None,
        input="hello",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
    )

    assert kwargs["multi_agent"] == {"enabled": True}
    assert kwargs["stream"] is omit
