from collections import deque

import pytest
from pydantic import BaseModel

from agents import Agent, RunConfig, Runner, UserError, function_tool
from agents.models.codex import CodexProvider


class FakeServer:
    # This double preserves App Server events and suspension while exercising the real Runner.
    def __init__(self, text="A Codex response", call=None, gate=None, raw_items=()):
        self.text = text
        self.raw_items = raw_items
        self.call = call
        self.gate = gate
        self.requests = []
        self.closed = False
        self.interrupted = False

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread_1"}, "model": params.get("model")}
        if method == "modelProvider/capabilities/read":
            return {"webSearch": True}
        if method == "config/read":
            return {
                "config": {
                    "web_search": "cached",
                    "features": {"standalone_web_search": False},
                    "tools": {
                        "web_search": {
                            "context_size": "high",
                            "allowed_domains": ["example.com"],
                            "location": {"country": "US", "city": "Boston"},
                        }
                    },
                }
            }
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        if method == "turn/interrupt":
            self.interrupted = True
            return {}
        if method == "configRequirements/read":
            return {"requirements": None}
        if method in ("initialize", "account/login/start", "thread/inject_items"):
            return {}
        raise AssertionError(f"Unexpected App Server request: {method}")

    async def notify(self, method, params=None):
        self.requests.append((method, params))

    async def close(self):
        self.closed = True

    async def events(self):
        common = {"threadId": "thread_1", "turnId": "turn_1"}
        for raw in self.raw_items:
            yield {"method": "rawResponseItem/completed", "params": {**common, "item": raw}}
        if self.gate is not None:
            yield {
                "method": "item/agentMessage/delta",
                "params": {**common, "itemId": "item_1", "delta": "A "},
            }
            await self.gate.wait()
        if self.call is not None:
            yield {"id": 55, "method": "item/tool/call", "params": {**common, **self.call}}
            assert self.interrupted
        else:
            if self.gate is None:
                for delta in [self.text[:2], self.text[2:]]:
                    yield {
                        "method": "item/agentMessage/delta",
                        "params": {**common, "itemId": "item_1", "delta": delta},
                    }
            yield {
                "method": "item/completed",
                "params": {
                    **common,
                    "item": {
                        "type": "agentMessage",
                        "id": "item_1",
                        "text": self.text,
                        "phase": "final_answer",
                    },
                },
            }
        yield {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread_1",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 10,
                        "cachedInputTokens": 3,
                        "outputTokens": 7,
                        "reasoningOutputTokens": 2,
                        "totalTokens": 17,
                    }
                },
            },
        }
        yield {
            "method": "turn/completed",
            "params": {
                "threadId": "thread_1",
                "turn": {"id": "turn_1", "status": "interrupted" if self.call else "completed"},
            },
        }


@pytest.fixture
def servers(monkeypatch):
    queue = deque()
    opened = []

    async def open_server(*args):
        server = queue.popleft()
        opened.append(args)
        return server

    monkeypatch.setattr("agents.models.codex.CodexAppServer.open", open_server)
    monkeypatch.setattr("agents.models.codex.resolve_codex_executable", lambda _: "/stock/codex")
    return queue, opened


def config(tmp_path, **kwargs):
    return RunConfig(model_provider=CodexProvider(cwd=tmp_path, **kwargs), tracing_disabled=True)


@pytest.mark.asyncio
async def test_runner_text_usage_streaming_and_replay(tmp_path, servers):
    queue, _ = servers
    first, second = FakeServer(), FakeServer()
    queue.extend([first, second])
    agent = Agent(name="test", instructions="Be brief.")
    result = await Runner.run(agent, "Start here.", run_config=config(tmp_path))
    assert result.final_output == "A Codex response"
    assert result.context_wrapper.usage.total_tokens == 17
    assert first.closed
    history = result.to_input_list() + [{"role": "user", "content": "What follows?"}]
    run = Runner.run_streamed(agent, history, run_config=config(tmp_path))
    events = [event async for event in run.stream_events()]
    assert run.final_output == result.final_output
    assert [
        event.data.delta
        for event in events
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta"
    ] == ["A ", "Codex response"]
    assert any(method == "thread/inject_items" for method, _ in second.requests)
    assert dict(second.requests)["thread/start"]["ephemeral"] is True
    assert second.closed


@pytest.mark.asyncio
async def test_runner_owns_function_execution_and_history(tmp_path, servers):
    queue, _ = servers
    executions = []

    @function_tool
    def lookup(value: str) -> str:
        executions.append(value)
        return "Found " + value

    first = FakeServer(
        call={
            "callId": "call_1",
            "namespace": None,
            "tool": "lookup",
            "arguments": {"value": "record"},
        }
    )
    second = FakeServer(text="Found record")
    queue.extend([first, second])
    result = await Runner.run(
        Agent(name="test", tools=[lookup]), "Find a record.", run_config=config(tmp_path)
    )
    assert result.final_output == "Found record"
    assert executions == ["record"]
    assert first.interrupted and first.closed and second.closed
    injected = dict(second.requests)["thread/inject_items"]["items"]
    assert any(
        item.get("type") == "function_call_output" and item.get("call_id") == "call_1"
        for item in injected
    )


@pytest.mark.asyncio
async def test_runner_structured_output_and_explicit_key(tmp_path, servers):
    queue, opened = servers

    class Result(BaseModel):
        answer: str

    server = FakeServer(text='{"answer":"done"}')
    queue.append(server)
    result = await Runner.run(
        Agent(name="test", output_type=Result),
        "Answer",
        run_config=config(tmp_path, api_key="secret-test"),
    )
    assert result.final_output.answer == "done"
    assert dict(server.requests)["turn/start"]["outputSchema"]["type"] == "object"
    assert dict(server.requests)["account/login/start"] == {
        "type": "apiKey",
        "apiKey": "secret-test",
    }
    assert opened[0][2][-1] == 'cli_auth_credentials_store="ephemeral"'
    assert "secret-test" not in repr(opened)


@pytest.mark.asyncio
async def test_unsupported_settings_fail_before_launch(tmp_path, servers):
    queue, opened = servers
    from agents import ModelSettings

    with pytest.raises(UserError, match="temperature"):
        await Runner.run(
            Agent(name="test", model_settings=ModelSettings(temperature=0.1)),
            "Hello",
            run_config=config(tmp_path),
        )
    assert not opened and not queue


@pytest.mark.asyncio
async def test_cancellation_closes_server_before_returning(tmp_path, servers):
    import asyncio

    queue, _ = servers
    server = FakeServer(gate=asyncio.Event())
    queue.append(server)
    task = asyncio.create_task(Runner.run(Agent(name="test"), "Hello", run_config=config(tmp_path)))
    while not server.requests or not any(method == "turn/start" for method, _ in server.requests):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.closed


@pytest.mark.asyncio
async def test_runner_handoff_preserves_runner_ownership(tmp_path, servers):
    queue, _ = servers
    specialist = Agent(name="Specialist", instructions="Resolve requests.")
    first = FakeServer(
        call={
            "callId": "handoff_1",
            "namespace": None,
            "tool": "transfer_to_specialist",
            "arguments": {},
        }
    )
    second = FakeServer(text="Specialist answer")
    queue.extend([first, second])
    result = await Runner.run(
        Agent(name="Coordinator", handoffs=[specialist]), "Transfer", run_config=config(tmp_path)
    )
    assert result.last_agent is specialist
    assert result.final_output == "Specialist answer"
    assert first.interrupted and first.closed
    assert dict(second.requests)["thread/start"]["developerInstructions"] == "Resolve requests."


@pytest.mark.asyncio
async def test_stream_yields_delta_before_codex_turn_completes(tmp_path, servers):
    import asyncio

    from agents import ModelSettings
    from agents.models.interface import ModelTracing

    queue, _ = servers
    gate = asyncio.Event()
    server = FakeServer(gate=gate)
    queue.append(server)
    model = CodexProvider(cwd=tmp_path).get_model(None)
    stream = model.stream_response(
        None,
        "Hello",
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )
    created = await anext(stream)
    assert created.type == "response.created"
    added = await anext(stream)
    assert added.type == "response.output_item.added"
    first = await anext(stream)
    assert first.type == "response.output_text.delta" and first.delta == "A "
    assert not gate.is_set() and not server.closed
    gate.set()
    remaining = [event async for event in stream]
    assert remaining[-1].type == "response.completed"
    assert server.closed


@pytest.mark.asyncio
async def test_runner_preserves_completed_stock_deferred_discovery(tmp_path, servers):
    from agents import ToolSearchTool

    queue, _ = servers

    @function_tool(defer_loading=True)
    def lookup_weather(city: str) -> str:
        return "Sunny in " + city

    search = {
        "type": "tool_search_call",
        "id": "search_1",
        "call_id": "search_call_1",
        "execution": "client",
        "arguments": {"query": "lookup_weather"},
    }
    found = {
        "type": "tool_search_output",
        "id": "search_output_1",
        "call_id": "search_call_1",
        "execution": "client",
        "status": "completed",
        "tools": [
            {
                "type": "namespace",
                "name": "lookup_weather",
                "description": "Weather lookup.",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup_weather",
                        "description": "Weather lookup.",
                        "strict": False,
                        "defer_loading": True,
                        "parameters": lookup_weather.params_json_schema,
                    }
                ],
            }
        ],
    }
    first = FakeServer(
        raw_items=[search, found],
        call={
            "callId": "weather_1",
            "namespace": "lookup_weather",
            "tool": "lookup_weather",
            "arguments": {"city": "Paris"},
        },
    )
    second = FakeServer(text="Sunny in Paris")
    queue.extend([first, second])
    result = await Runner.run(
        Agent(name="test", tools=[ToolSearchTool(), lookup_weather]),
        "Find Paris weather",
        run_config=config(tmp_path),
    )
    assert result.final_output == "Sunny in Paris"
    assert [
        item.raw_item.type
        for item in result.new_items
        if item.type in ("tool_search_call_item", "tool_search_output_item")
    ] == ["tool_search_call", "tool_search_output"]
    assert dict(first.requests)["thread/start"]["experimentalRawEvents"] is True


@pytest.mark.asyncio
async def test_raw_reasoning_is_preserved_for_replay(tmp_path, servers):
    from agents import ModelSettings

    queue, _ = servers
    server = FakeServer(
        raw_items=[
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "A summary."}],
                "encrypted_content": "opaque-reasoning",
            }
        ]
    )
    queue.append(server)
    result = await Runner.run(
        Agent(
            name="test",
            model_settings=ModelSettings(response_include=["reasoning.encrypted_content"]),
        ),
        "Hello",
        run_config=config(tmp_path),
    )
    reasoning = next(item for item in result.to_input_list() if item.get("type") == "reasoning")
    assert reasoning["encrypted_content"] == "opaque-reasoning"
    assert reasoning["summary"] == [{"type": "summary_text", "text": "A summary."}]


@pytest.mark.asyncio
async def test_native_web_search_config_and_result(tmp_path, servers):
    from agents import WebSearchTool

    queue, opened = servers
    server = FakeServer(
        raw_items=[
            {
                "type": "web_search_call",
                "id": "web_1",
                "status": "completed",
                "action": {"type": "search", "query": "weather"},
            }
        ]
    )
    queue.append(server)
    result = await Runner.run(
        Agent(
            name="test",
            model="gpt-5.5",
            tools=[
                WebSearchTool(
                    external_web_access=False,
                    search_context_size="high",
                    filters={"allowed_domains": ["example.com"]},
                    user_location={"type": "approximate", "country": "US", "city": "Boston"},
                )
            ],
        ),
        "Search",
        run_config=config(tmp_path),
    )
    assert any(item.get("type") == "web_search_call" for item in result.to_input_list())
    overrides = opened[0][2]
    assert 'web_search="cached"' in overrides
    assert 'tools.web_search.context_size="high"' in overrides
    assert 'tools.web_search.allowed_domains=["example.com"]' in overrides
    assert 'tools.web_search.location.city="Boston"' in overrides
    assert "dynamicTools" not in dict(server.requests)["thread/start"]
    assert "configRequirements/read" in dict(server.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [None, True, False])
async def test_native_host_may_narrow_web_access_to_cached(tmp_path, servers, permission):
    from agents import WebSearchTool

    class CachedHost(FakeServer):
        async def request(self, method, params):
            result = await super().request(method, params)
            if method == "config/read":
                # Stock reports the configured mode here, then applies host policy on the turn.
                assert isinstance(result, dict)
                config = result["config"]
                assert isinstance(config, dict)
                config["web_search"] = "cached" if permission is False else "live"
            elif method == "configRequirements/read":
                return {"requirements": {"allowedWebSearchModes": ["cached", "disabled"]}}
            return result

    queue, _ = servers
    queue.append(
        CachedHost(
            raw_items=[
                {
                    "type": "web_search_call",
                    "id": "ws_1",
                    "status": "completed",
                    "action": {"type": "search", "query": "weather"},
                }
            ]
        )
    )
    result = await Runner.run(
        Agent(
            name="Search",
            model="gpt-5.4",
            tools=[WebSearchTool(external_web_access=permission, search_context_size="high")],
        ),
        "Search",
        run_config=config(tmp_path),
    )
    assert any(item.get("type") == "web_search_call" for item in result.to_input_list())


@pytest.mark.asyncio
async def test_audio_requires_effective_model_catalog_capability_before_turn(tmp_path, servers):
    import base64
    import io
    import wave

    audio_bytes = io.BytesIO()
    with wave.open(audio_bytes, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 160)
    encoded_audio = base64.b64encode(audio_bytes.getvalue()).decode()
    queue, _ = servers

    class AudioServer(FakeServer):
        async def request(self, method, params):
            if method == "thread/start":
                self.requests.append((method, params))
                return {"thread": {"id": "thread_1"}, "model": "effective-audio"}
            if method == "model/list":
                self.requests.append((method, params))
                if params["cursor"] is None:
                    return {
                        "data": [{"id": "other", "inputModalities": ["text"]}],
                        "nextCursor": "page-2",
                    }
                return {
                    "data": [{"id": "effective-audio", "inputModalities": ["text", "audio"]}],
                    "nextCursor": None,
                }
            return await super().request(method, params)

    server = AudioServer()
    queue.append(server)
    audio = [
        {
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": encoded_audio, "format": "wav"}}
            ],
        }
    ]
    result = await Runner.run(
        Agent(name="test", model="requested-model"), audio, run_config=config(tmp_path)
    )
    assert result.final_output == "A Codex response"
    pages = [params for method, params in server.requests if method == "model/list"]
    assert [page["cursor"] for page in pages] == [None, "page-2"]

    class NoAudioServer(AudioServer):
        async def request(self, method, params):
            if method == "model/list":
                return {
                    "data": [{"id": "effective-audio", "inputModalities": ["text"]}],
                    "nextCursor": None,
                }
            return await super().request(method, params)

    rejected = NoAudioServer()
    queue.append(rejected)
    with pytest.raises(UserError, match="does not support audio"):
        await Runner.run(Agent(name="test"), audio, run_config=config(tmp_path))
    assert rejected.closed
    assert not any(method == "turn/start" for method, _ in rejected.requests)


@pytest.mark.asyncio
async def test_codex_named_model_uses_provider_defaults(tmp_path, servers):
    queue, _ = servers
    server = FakeServer()
    queue.append(server)
    await Runner.run(Agent(name="test", model="gpt-5.4-mini"), "Hello", run_config=config(tmp_path))
    assert "effort" not in dict(server.requests)["turn/start"]
    assert "config" not in dict(server.requests)["thread/start"]


@pytest.mark.asyncio
async def test_sdk_owned_shell_disables_native_workspace_tool_descriptors(tmp_path, servers):
    from agents.tool import ShellTool

    queue, opened = servers
    server = FakeServer(text="done")
    queue.append(server)
    shell = ShellTool(executor=lambda request: "workspace")
    result = await Runner.run(
        Agent(name="test", tools=[shell]),
        "hello",
        run_config=config(tmp_path),
    )
    assert result.final_output == "done"
    overrides = opened[0][2]
    assert "features.shell_tool=false" in overrides
    assert "features.view_image=false" in overrides
    requests = dict(server.requests)
    assert requests["thread/start"]["environments"] == []
    assert requests["turn/start"]["environments"] == []
    assert "SDK-provided tools" in requests["thread/start"]["developerInstructions"]


@pytest.mark.asyncio
async def test_sdk_function_stream_has_correlated_lifecycle_and_complete_arguments(
    tmp_path, servers
):
    queue, _ = servers

    @function_tool
    def lookup(value: str) -> str:
        return value

    class StartedCallServer(FakeServer):
        async def events(self):
            common = {"threadId": "thread_1", "turnId": "turn_1"}
            yield {
                "method": "item/started",
                "params": {
                    **common,
                    "item": {
                        "type": "dynamicToolCall",
                        "id": "call_1",
                        "namespace": None,
                        "tool": "lookup",
                        "arguments": {"value": "record"},
                        "status": "inProgress",
                    },
                },
            }
            yield {
                "method": "item/started",
                "params": {
                    **common,
                    "item": {
                        "type": "dynamicToolCall",
                        "id": "unaccepted_call",
                        "namespace": None,
                        "tool": "lookup",
                        "arguments": {"value": "unaccepted"},
                        "status": "inProgress",
                    },
                },
            }
            async for event in super().events():
                yield event

    first = StartedCallServer(
        call={
            "callId": "call_1",
            "namespace": None,
            "tool": "lookup",
            "arguments": {"value": "record"},
        }
    )
    queue.extend([first, FakeServer(text="record")])
    run = Runner.run_streamed(
        Agent(name="test", tools=[lookup]), "Find record", run_config=config(tmp_path)
    )
    events = [
        event.data async for event in run.stream_events() if event.type == "raw_response_event"
    ]
    completed = next(
        index for index, event in enumerate(events) if event.type == "response.completed"
    )
    first_turn = events[: completed + 1]
    assert [event.type for event in first_turn] == [
        "response.created",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event.sequence_number for event in first_turn] == list(range(len(first_turn)))
    added, delta, arguments_done, done = first_turn[1:5]
    assert added.item.id == added.item.call_id == delta.item_id == done.item.id == "call_1"
    assert (
        added.output_index
        == delta.output_index
        == arguments_done.output_index
        == done.output_index
        == 0
    )
    assert added.item.status == "in_progress" and added.item.arguments == ""
    assert delta.delta == arguments_done.arguments == done.item.arguments == '{"value":"record"}'
    assert done.item.status == "completed" and first.closed
    assert first_turn[-1].response.output == [done.item]
    assert run.final_output == "record"
    assert "unaccepted_call" not in str([event.model_dump() for event in events])


@pytest.mark.asyncio
async def test_assistant_stream_uses_stock_start_and_done_without_duplicate_text(tmp_path, servers):
    queue, _ = servers

    class StartedMessageServer(FakeServer):
        async def events(self):
            common = {"threadId": "thread_1", "turnId": "turn_1"}
            yield {
                "method": "item/started",
                "params": {
                    **common,
                    "item": {
                        "type": "agentMessage",
                        "id": "item_1",
                        "text": "",
                        "phase": "final_answer",
                    },
                },
            }
            async for event in super().events():
                yield event
                if event["method"] == "item/completed":
                    yield event

    queue.append(StartedMessageServer())
    run = Runner.run_streamed(Agent(name="test"), "Hello", run_config=config(tmp_path))
    events = [
        event.data async for event in run.stream_events() if event.type == "raw_response_event"
    ]
    assert [event.type for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event.sequence_number for event in events] == list(range(len(events)))
    assert events[0].response.status == "in_progress" and events[0].response.output == []
    assert events[1].item.id == events[4].item.id == "item_1"
    assert (
        events[1].output_index
        == events[2].output_index
        == events[3].output_index
        == events[4].output_index
        == 0
    )
    assert events[1].item.content == []
    assert (
        "".join(event.delta for event in events if event.type == "response.output_text.delta")
        == "A Codex response"
    )
    assert events[-1].response.output == [events[4].item]


@pytest.mark.asyncio
async def test_non_strict_output_guides_json_without_native_strict_schema(tmp_path, servers):
    from agents import AgentOutputSchema

    queue, _ = servers

    class Result(BaseModel):
        values: dict[str, str]
        note: str | None = None

    server = FakeServer(text='{"values":{"route":"manual"}}')
    queue.append(server)
    result = await Runner.run(
        Agent(
            name="test",
            instructions="Preserve the optional note.",
            output_type=AgentOutputSchema(Result, strict_json_schema=False),
        ),
        "Return a route",
        run_config=config(tmp_path),
    )
    assert result.final_output == Result(values={"route": "manual"})
    requests = dict(server.requests)
    assert "outputSchema" not in requests["turn/start"]
    instructions = requests["thread/start"]["developerInstructions"]
    assert instructions.startswith("Preserve the optional note.")
    assert "JSON" in instructions and '"additionalProperties"' in instructions
    assert "not a server-enforced constraint" in instructions


@pytest.mark.asyncio
async def test_non_strict_output_still_uses_runner_validation(tmp_path, servers):
    from agents import AgentOutputSchema, ModelBehaviorError

    queue, _ = servers

    class Result(BaseModel):
        values: dict[str, str]

    queue.append(FakeServer(text='{"values":42}'))
    with pytest.raises(ModelBehaviorError):
        await Runner.run(
            Agent(name="test", output_type=AgentOutputSchema(Result, strict_json_schema=False)),
            "Return a route",
            run_config=config(tmp_path),
        )


@pytest.mark.asyncio
async def test_deferred_discovery_preserves_unrelated_direct_namespaces(tmp_path, servers):
    from agents import ToolSearchTool

    queue, _ = servers

    @function_tool(defer_loading=True)
    def lookup_weather() -> str:
        return "SUNNY"

    class ConfiguredServer(FakeServer):
        async def request(self, method, params):
            if method == "config/read":
                self.requests.append((method, params))
                return {
                    "config": {
                        "features": {
                            "code_mode": {
                                "direct_only_tool_namespaces": ["keep_existing", "lookup_weather"],
                            }
                        }
                    }
                }
            return await super().request(method, params)

    server = ConfiguredServer(text="done")
    queue.append(server)
    await Runner.run(
        Agent(name="test", tools=[ToolSearchTool(), lookup_weather]),
        "hello",
        run_config=config(tmp_path),
    )
    thread = dict(server.requests)["thread/start"]
    assert thread["config"]["features.code_mode.direct_only_tool_namespaces"] == [
        "keep_existing",
        "functions",
    ]
    assert thread["config"]["features.code_mode_tool_search"] is True
    namespace = next(
        tool for tool in thread["dynamicTools"] if tool.get("name") == "lookup_weather"
    )
    assert namespace["tools"][0]["deferLoading"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_auto_previous_response_id_rejects_before_codex_startup(tmp_path, servers, streamed):
    _, opened = servers
    with pytest.raises(UserError, match="does not support auto_previous_response_id"):
        if streamed:
            result = Runner.run_streamed(
                Agent(name="test", model="gpt-5.4"),
                "hello",
                run_config=config(tmp_path),
                auto_previous_response_id=True,
            )
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(
                Agent(name="test", model="gpt-5.4"),
                "hello",
                run_config=config(tmp_path),
                auto_previous_response_id=True,
            )
    assert opened == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.4-mini"])
async def test_verified_text_web_models_accept_explicit_default_content_types(
    tmp_path, servers, model
):
    from agents import WebSearchTool

    queue, _ = servers
    queue.append(FakeServer(text="done"))
    result = await Runner.run(
        Agent(
            name="test",
            model=model,
            tools=[
                WebSearchTool(
                    external_web_access=False,
                    search_context_size="high",
                    search_content_types=["text"],
                )
            ],
        ),
        "hello",
        run_config=config(tmp_path),
    )
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_text_web_model_rejects_unavailable_image_content_before_turn(tmp_path, servers):
    from agents import WebSearchTool

    queue, _ = servers
    server = FakeServer(text="done")
    queue.append(server)
    with pytest.raises(UserError, match="default web search content types"):
        await Runner.run(
            Agent(
                name="test",
                model="gpt-5.4",
                tools=[
                    WebSearchTool(
                        external_web_access=False,
                        search_context_size="high",
                        search_content_types=["text", "image"],
                    )
                ],
            ),
            "hello",
            run_config=config(tmp_path),
        )
    assert all(method != "turn/start" for method, _ in server.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_native_summary_notifications_stream_only_public_opted_in_text(
    tmp_path,
    servers,
    requested,
):
    from openai.types.responses import ResponseReasoningSummaryTextDeltaEvent

    from agents import ModelSettings

    queue, _ = servers

    class SummaryServer(FakeServer):
        async def events(self):
            common = {"threadId": "thread_1", "turnId": "turn_1", "itemId": "rs_1"}
            yield {
                "method": "item/reasoning/summaryPartAdded",
                "params": {
                    **common,
                    "summaryIndex": 0,
                },
            }
            for delta in ("Public ", "summary."):
                yield {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {
                        **common,
                        "summaryIndex": 0,
                        "delta": delta,
                    },
                }
            yield {
                "method": "item/reasoning/textDelta",
                "params": {
                    **common,
                    "contentIndex": 0,
                    "delta": "HIDDEN_REASONING_TEXT",
                },
            }
            async for event in super().events():
                yield event

    server = SummaryServer(
        raw_items=[
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "Public summary."}],
                "encrypted_content": "opaque-replay",
            }
        ]
    )
    queue.append(server)
    settings = ModelSettings(
        reasoning={"summary": "auto"} if requested else None,
        response_include=["reasoning.encrypted_content"],
    )
    stream = Runner.run_streamed(
        Agent(name="test", model_settings=settings), "hello", run_config=config(tmp_path)
    )
    events = []
    deltas = []
    async for event in stream.stream_events():
        if event.type != "raw_response_event":
            continue
        events.append(event.data)
        if isinstance(event.data, ResponseReasoningSummaryTextDeltaEvent):
            assert not stream.is_complete
            assert event.data.item_id == "rs_1"
            assert event.data.summary_index == 0
            assert event.data.output_index == 0
            deltas.append(event.data.delta)
    assert deltas == (["Public ", "summary."] if requested else [])
    parts = [event for event in events if event.type == "response.reasoning_summary_part.added"]
    assert len(parts) == (1 if requested else 0)
    assert all("HIDDEN_REASONING_TEXT" not in event.model_dump_json() for event in events)
    reasoning = next(item for item in stream.to_input_list() if item.get("type") == "reasoning")
    assert reasoning["encrypted_content"] == "opaque-replay"
    assert reasoning["summary"] == [{"type": "summary_text", "text": "Public summary."}]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("use_handoff", [False, True])
async def test_required_tool_choice_returns_a_real_sdk_call(
    tmp_path, servers, streamed, use_handoff
):
    from agents import ModelSettings

    queue, opened = servers
    executions = []

    @function_tool
    def lookup(value: str) -> str:
        executions.append(value)
        return "Found " + value

    target = Agent(name="specialist")
    first = FakeServer(
        call={
            "callId": "call_required",
            "namespace": None,
            "tool": "transfer_to_specialist" if use_handoff else "lookup",
            "arguments": {} if use_handoff else {"value": "record"},
        }
    )
    second = FakeServer(text="Finished")
    queue.extend([first, second] if use_handoff else [first])
    agent = Agent(
        name="test",
        instructions="Keep this instruction.",
        model_settings=ModelSettings(tool_choice="required"),
        tools=[] if use_handoff else [lookup],
        handoffs=[target] if use_handoff else [],
        tool_use_behavior="stop_on_first_tool",
    )
    if streamed:
        run = Runner.run_streamed(
            agent,
            "Find a record.",
            run_config=config(
                tmp_path, config_overrides=["features.shell_tool=true", "features.view_image=true"]
            ),
        )
        events = [event async for event in run.stream_events()]
        completed = [
            event.data.response
            for event in events
            if event.type == "raw_response_event" and event.data.type == "response.completed"
        ]
        assert completed[0].tool_choice == "required"
        assert any(item.type == "function_call" for item in completed[0].output)
        result = run
    else:
        result = await Runner.run(
            agent,
            "Find a record.",
            run_config=config(
                tmp_path, config_overrides=["features.shell_tool=true", "features.view_image=true"]
            ),
        )
    assert result.final_output == ("Finished" if use_handoff else "Found record")
    assert executions == ([] if use_handoff else ["record"])
    assert first.closed and first.interrupted
    instructions = dict(first.requests)["thread/start"]["developerInstructions"]
    assert "Keep this instruction." in instructions
    assert "must call" in instructions
    assert "SDK tool or handoff" in instructions

    assert opened[0][2] == [
        "features.shell_tool=true",
        "features.view_image=true",
        "features.shell_tool=false",
        "features.view_image=false",
    ]
    assert "environments" not in dict(first.requests)["thread/start"]
    assert "environments" not in dict(first.requests)["turn/start"]
    assert "agents_sdk namespace" not in instructions


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("native_shell", [False, True, "empty"])
async def test_required_tool_choice_never_completes_with_only_text_or_native_tools(
    tmp_path, servers, streamed, native_shell
):
    from agents import ModelRetrySettings, ModelSettings

    queue, opened = servers

    @function_tool
    def lookup() -> str:
        raise AssertionError("No SDK tool call was returned.")

    class NativeServer(FakeServer):
        async def events(self):
            if native_shell == "empty":
                yield {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_1",
                        "turn": {"id": "turn_1", "status": "completed"},
                    },
                }
                return
            if native_shell is True:
                yield {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_1",
                        "turnId": "turn_1",
                        "item": {
                            "type": "commandExecution",
                            "id": "native_shell",
                            "command": "pwd",
                            "status": "completed",
                            "aggregatedOutput": "/workspace",
                            "exitCode": 0,
                        },
                    },
                }
            async for event in super().events():
                yield event

    server = NativeServer(text="I answered without an SDK call.")
    queue.append(server)
    agent = Agent(
        name="test",
        tools=[lookup],
        model_settings=ModelSettings(
            tool_choice="required", retry=ModelRetrySettings(max_retries=2)
        ),
    )
    events = []
    with pytest.raises(UserError, match="required.*SDK tool or handoff"):
        if streamed:
            run = Runner.run_streamed(agent, "Find a record.", run_config=config(tmp_path))
            async for event in run.stream_events():
                events.append(event)
        else:
            await Runner.run(agent, "Find a record.", run_config=config(tmp_path))
    assert not any(
        event.type == "raw_response_event" and event.data.type == "response.completed"
        for event in events
    )
    assert server.closed
    assert len(opened) == 1
    assert not queue


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("choice", ["required", "none", "lookup"])
async def test_required_without_eligible_tools_and_other_choices_fail_before_launch(
    tmp_path, servers, streamed, choice
):
    from agents import ModelSettings

    queue, opened = servers
    agent = Agent(name="test", model_settings=ModelSettings(tool_choice=choice))
    with pytest.raises(UserError, match="required.*SDK tool or handoff|tool_choice"):
        if streamed:
            run = Runner.run_streamed(agent, "Hello", run_config=config(tmp_path))
            async for _event in run.stream_events():
                pass
        else:
            await Runner.run(agent, "Hello", run_config=config(tmp_path))
    assert not opened and not queue


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [None, "auto"])
async def test_default_and_auto_tool_choice_keep_text_completion(tmp_path, servers, choice):
    from agents import ModelSettings

    queue, opened = servers
    server = FakeServer()
    queue.append(server)
    result = await Runner.run(
        Agent(
            name="test",
            instructions="Only this instruction.",
            model_settings=ModelSettings(tool_choice=choice),
        ),
        "Hello",
        run_config=config(tmp_path),
    )
    assert result.final_output == "A Codex response"
    assert (
        dict(server.requests)["thread/start"]["developerInstructions"] == "Only this instruction."
    )
    assert server.closed

    assert opened[0][2] == []


@pytest.mark.asyncio
async def test_required_rejects_only_hosted_tools_before_launch(tmp_path, servers):
    from agents import ModelSettings, WebSearchTool

    queue, opened = servers
    with pytest.raises(UserError, match="required.*SDK tool or handoff"):
        await Runner.run(
            Agent(
                name="test",
                tools=[WebSearchTool()],
                model_settings=ModelSettings(tool_choice="required"),
            ),
            "Use a tool.",
            run_config=config(tmp_path),
        )
    assert not opened and not queue


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("kind", ["shell", "apply_patch"])
async def test_required_accepts_sdk_local_tool_callbacks(tmp_path, servers, streamed, kind):
    from agents import ApplyPatchTool, ModelSettings
    from agents.editor import ApplyPatchResult
    from agents.tool import ShellTool

    queue, _ = servers
    executions = []

    def shell_executor(request):
        executions.extend(request.data.action.commands)
        return "workspace"

    class Editor:
        def create_file(self, operation):
            executions.append(operation.path)
            return ApplyPatchResult(output="Created test.txt")

    tool = (
        ShellTool(executor=shell_executor) if kind == "shell" else ApplyPatchTool(editor=Editor())
    )
    arguments = (
        {"action": {"commands": ["pwd"]}}
        if kind == "shell"
        else {"operation": {"type": "create_file", "path": "test.txt", "diff": "+hello"}}
    )
    first = FakeServer(
        call={
            "callId": "local_required",
            "namespace": "agents_sdk",
            "tool": kind,
            "arguments": arguments,
        }
    )
    second = FakeServer(text="Local SDK tool finished.")
    queue.extend([first, second])
    agent = Agent(name="test", tools=[tool], model_settings=ModelSettings(tool_choice="required"))
    if streamed:
        run = Runner.run_streamed(agent, "Use the SDK tool.", run_config=config(tmp_path))
        events = [event async for event in run.stream_events()]
        completed = [
            event.data.response
            for event in events
            if event.type == "raw_response_event" and event.data.type == "response.completed"
        ]
        assert completed[0].tool_choice == "required"
        assert any(item.type == kind + "_call" for item in completed[0].output)
        result = run
    else:
        result = await Runner.run(agent, "Use the SDK tool.", run_config=config(tmp_path))
    assert result.final_output == "Local SDK tool finished."
    assert executions == (["pwd"] if kind == "shell" else ["test.txt"])
    assert first.interrupted and first.closed and second.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_required_preserves_incomplete_turn_runtime_error(tmp_path, servers, streamed):
    from agents import ModelSettings

    queue, _ = servers

    @function_tool
    def lookup() -> str:
        return "unused"

    class IncompleteServer(FakeServer):
        async def events(self):
            yield {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread_1",
                    "turnId": "turn_1",
                    "itemId": "text",
                    "delta": "Unfinished",
                },
            }

    server = IncompleteServer()
    queue.append(server)
    agent = Agent(name="test", tools=[lookup], model_settings=ModelSettings(tool_choice="required"))
    with pytest.raises(RuntimeError, match="without a completed response"):
        if streamed:
            run = Runner.run_streamed(agent, "Use a tool.", run_config=config(tmp_path))
            async for _event in run.stream_events():
                pass
        else:
            await Runner.run(agent, "Use a tool.", run_config=config(tmp_path))
    assert server.closed


def native_pair(kind="function_call", call_id="native_1", name="exec_command"):
    call = {
        "type": kind,
        "id": "call_item_" + call_id,
        "call_id": call_id,
        "name": name,
        "arguments" if kind == "function_call" else "input": (
            '{"cmd":"printf native-result"}'
            if kind == "function_call"
            else "*** Begin Patch\n*** End Patch"
        ),
    }
    output = {
        "type": kind + "_output",
        "id": "output_item_" + call_id,
        "call_id": call_id,
        "output": "native-result-not-in-assistant-message",
    }
    return call, output


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize("streamed", [False, True])
async def test_native_completed_pairs_survive_session_replay_without_sdk_execution(
    tmp_path, servers, kind, streamed
):
    from agents import SQLiteSession
    from agents.decorators import tool

    executions = []

    @tool
    def exec_command(cmd: str) -> str:
        executions.append(cmd)
        return "SDK result"

    first = FakeServer(text="Done", raw_items=native_pair(kind))
    second = FakeServer(text="Continued")
    queue, _ = servers
    queue.extend([first, second])
    agent = Agent(name="test", tools=[exec_command])
    session = SQLiteSession("native-history", str(tmp_path / "history.sqlite"))
    if streamed:
        result = Runner.run_streamed(agent, "Start", session=session, run_config=config(tmp_path))
        events = [event async for event in result.stream_events()]
        assert [event.name for event in events if event.type == "run_item_stream_event"] == [
            "tool_called",
            "tool_output",
            "message_output_created",
        ]
    else:
        result = await Runner.run(agent, "Start", session=session, run_config=config(tmp_path))
    assert executions == []
    saved = await session.get_items()
    assert [item.get("type", "message") for item in saved] == [
        "message",
        kind,
        kind + "_output",
        "message",
    ]
    assert saved[1]["call_id"] == saved[2]["call_id"] == "native_1"
    assert saved[2]["output"] == "native-result-not-in-assistant-message"
    assert [item.type for item in result.new_items] == [
        "tool_call_item",
        "tool_call_output_item",
        "message_output_item",
    ]
    # Reopen the durable store and provider instead of retaining a native process or thread.
    reopened = SQLiteSession("native-history", str(tmp_path / "history.sqlite"))
    await Runner.run(agent, "Continue", session=reopened, run_config=config(tmp_path))
    injected = dict(second.requests)["thread/inject_items"]["items"]
    assert [(item["type"], item["call_id"]) for item in injected if "call_id" in item] == [
        (kind, "native_1"),
        (kind + "_output", "native_1"),
    ]
    assert injected[2]["output"] == saved[2]["output"]
    assert executions == []


@pytest.mark.asyncio
async def test_native_pairs_preserve_interleaved_order_and_ignore_injected_history(
    tmp_path, servers
):
    first_call, first_output = native_pair(call_id="native_a")
    second_call, second_output = native_pair("custom_tool_call", "native_b", "apply_patch")

    class InjectedHistoryServer(FakeServer):
        async def events(self):
            for item in native_pair(call_id="injected"):
                yield {
                    "method": "rawResponseItem/completed",
                    "params": {"threadId": "thread_1", "turnId": "auto-compact-0", "item": item},
                }
            async for event in super().events():
                yield event

    server = InjectedHistoryServer(raw_items=[first_call, second_call, second_output, first_output])
    queue, _ = servers
    queue.append(server)
    result = await Runner.run(Agent(name="test"), "Start", run_config=config(tmp_path))
    replay = result.to_input_list()
    assert [(item["type"], item["call_id"]) for item in replay if "call_id" in item] == [
        ("function_call", "native_a"),
        ("custom_tool_call", "native_b"),
        ("custom_tool_call_output", "native_b"),
        ("function_call_output", "native_a"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize("cancellation_id", [None, "sdk_cancelled_output"])
async def test_native_results_survive_serialized_adjacent_sdk_approval(
    tmp_path, servers, kind, cancellation_id
):
    import json

    from agents import RunState, SQLiteSession
    from agents.decorators import tool

    executions = []

    @tool(needs_approval=True)
    def lookup(value: str) -> str:
        executions.append(value)
        return "SDK lookup result"

    native = native_pair(kind)
    raw_sdk_call = {
        "type": "function_call",
        "id": "sdk_item",
        "call_id": "sdk_call",
        "name": "lookup",
        "arguments": '{"value":"record"}',
        "status": "completed",
    }

    class InterruptedSdkServer(FakeServer):
        async def events(self):
            async for event in super().events():
                yield event
                if event.get("method") == "item/tool/call":
                    cancellation = {
                        "type": "function_call_output",
                        "call_id": "sdk_call",
                        "output": "dynamic tool call was cancelled before receiving a response",
                    }
                    if cancellation_id is not None:
                        cancellation["id"] = cancellation_id
                    yield {
                        "method": "rawResponseItem/completed",
                        "params": {
                            "threadId": "thread_1",
                            "turnId": "turn_1",
                            "item": cancellation,
                        },
                    }

    first = InterruptedSdkServer(
        raw_items=[*native, raw_sdk_call],
        call={
            "callId": "sdk_call",
            "namespace": None,
            "tool": "lookup",
            "arguments": {"value": "record"},
        },
    )
    second = FakeServer(text="Done")
    queue, _ = servers
    queue.extend([first, second])
    agent = Agent(name="test", tools=[lookup])
    session = SQLiteSession("native-approval", str(tmp_path / "approval.sqlite"))
    paused = await Runner.run(agent, "Start", session=session, run_config=config(tmp_path))
    assert len(paused.interruptions) == 1
    assert executions == []
    state = await RunState.from_json(agent, json.loads(paused.to_state().to_string()))
    state.approve(state.get_interruptions()[0])
    await Runner.run(agent, state, session=session, run_config=config(tmp_path))
    assert executions == ["record"]
    injected = dict(second.requests)["thread/inject_items"]["items"]
    assert [(item["type"], item["call_id"]) for item in injected if "call_id" in item] == [
        (kind, "native_1"),
        (kind + "_output", "native_1"),
        ("function_call", "sdk_call"),
        ("function_call_output", "sdk_call"),
    ]
    saved = await session.get_items()
    assert len([item for item in saved if item.get("call_id") == "native_1"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_items", [native_pair()[:1], native_pair()[1:]])
async def test_incomplete_native_pairs_fail_without_sdk_execution(tmp_path, servers, raw_items):
    server = FakeServer(raw_items=raw_items)
    queue, _ = servers
    queue.append(server)
    with pytest.raises(RuntimeError, match="native tool"):
        await Runner.run(Agent(name="test"), "Start", run_config=config(tmp_path))
    assert server.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["item/started", "item/completed"])
async def test_native_context_compaction_fails_before_acknowledging_history(
    tmp_path, servers, method
):
    class CompactingServer(FakeServer):
        async def events(self):
            yield {
                "method": method,
                "params": {
                    "threadId": "thread_1",
                    "turnId": "native-compact",
                    "item": {"type": "contextCompaction", "id": "compact-native"},
                },
            }
            async for event in super().events():
                yield event

    queue, _ = servers
    server = CompactingServer()
    queue.append(server)
    with pytest.raises(UserError, match="native context compaction cannot be replayed"):
        await Runner.run(Agent(name="test"), "Start", run_config=config(tmp_path))
    assert server.closed


@pytest.mark.asyncio
async def test_public_sdk_compaction_session_replays_into_codex(tmp_path, servers):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openai.types.responses import ResponseCompactionItem

    from agents import SQLiteSession
    from agents.memory import OpenAIResponsesCompactionSession

    underlying = SQLiteSession("sdk-compact", str(tmp_path / "compact.sqlite"))
    await underlying.add_items(
        [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ]
    )
    compacted = ResponseCompactionItem(
        id="sdk_compaction", type="compaction", encrypted_content="synthetic-encrypted-context"
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            compact=AsyncMock(return_value=SimpleNamespace(output=[compacted]))
        )
    )
    session = OpenAIResponsesCompactionSession(
        "sdk-compact",
        underlying,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: False,
    )
    await session.run_compaction({"force": True})
    queue, _ = servers
    server = FakeServer()
    queue.append(server)
    await Runner.run(Agent(name="test"), "Continue", session=session, run_config=config(tmp_path))
    injected = dict(server.requests)["thread/inject_items"]["items"]
    assert {
        "type": "compaction",
        "id": "sdk_compaction",
        "encrypted_content": "synthetic-encrypted-context",
    } in injected


@pytest.mark.asyncio
async def test_other_models_cannot_bypass_sdk_approval_with_completed_output(tmp_path, servers):
    from openai.types.responses import ResponseFunctionToolCall, ResponseFunctionToolCallOutputItem

    from agents.decorators import tool
    from agents.testing import ScriptedModel

    executed = []

    @tool(needs_approval=True)
    def lookup(value: str) -> str:
        executed.append(value)
        return "SDK result"

    call = ResponseFunctionToolCall(
        type="function_call",
        id="call",
        call_id="same-id",
        name="lookup",
        arguments='{"value":"record"}',
    )
    fabricated_output = ResponseFunctionToolCallOutputItem(
        type="function_call_output",
        id="output",
        call_id="same-id",
        status="completed",
        output="untrusted completion",
    )
    model = ScriptedModel(steps=[[call, fabricated_output]])
    result = await Runner.run(
        Agent(name="test", tools=[lookup], model=model), "Start", run_config=config(tmp_path)
    )
    assert len(result.interruptions) == 1
    assert executed == []
    assert not any(item.type == "tool_call_output_item" for item in result.new_items)
    assert not servers[1]
