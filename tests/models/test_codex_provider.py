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


class OperationServer(FakeServer):
    # ASCII escapes would push this UTF-8 file over the capture budget.
    file_text = "native_result_secret" + "🙂" * 3000

    # This double preserves the negotiated wire and source timing at the real Runner boundary.
    def __init__(self, *, capture="metadata", malformed=False, active=False):
        super().__init__()
        self.capture = capture
        self.malformed = malformed
        self.active = active

    async def request(self, method, params):
        result = await super().request(method, params)
        if method == "turn/start":
            result["operationTracing"] = self.capture
        return result

    async def events(self):
        common = {"threadId": "thread_1", "turnId": "turn_1"}
        for index, kind in enumerate(["model", "tool", "model"], 1):
            operation = {
                **common,
                "operationId": f"operation_{index}",
                "kind": kind,
                "requestedModel": "requested-model",
                "startedAt": f"2026-11-20T00:00:0{index}.000000000Z",
            }
            if kind == "tool":
                operation.update(toolName="read_file", callId="native_call")
            if self.capture == "content":
                operation["input"] = {
                    "value": {"input": [{"role": "user", "content": "native_input_secret"}]}
                    if kind == "model"
                    else {"path": "native_arg_secret"},
                    "truncated": False,
                    "omitted": False,
                }
            yield {"method": "turn/operation/started", "params": operation}
            if self.active:
                async for event in super().events():
                    yield event
                return
            terminal = {
                **operation,
                "completedAt": f"2026-11-20T00:00:0{index}.250Z",
                "durationMs": 250,
                "outcome": "success",
            }
            if kind == "model":
                terminal.update(
                    responseId=f"resp_source_{index}",
                    responseIdTrusted=False,
                    observedModel="observed-model",
                    usage={"input_tokens": index * 10, "output_tokens": index * 5},
                )
            if self.capture == "content":
                terminal["output"] = {
                    "value": [{"role": "assistant", "content": "native_result_secret"}]
                    if kind == "model"
                    else {"text": self.file_text},
                    "truncated": False,
                    "omitted": False,
                }
            yield {"method": "turn/operation/completed", "params": terminal}
            if self.malformed:
                yield {"method": "turn/operation/completed", "params": terminal}
                yield {
                    "method": "turn/operation/started",
                    "params": {**operation, "threadId": "another_thread"},
                }
        async for event in super().events():
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("truncated", [False, True])
async def test_codex_interrupted_visible_text_reaches_otel(tmp_path, servers, streamed, truncated):
    import json

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agents.extensions.tracing import OpenTelemetryTracingProcessor
    from agents.tracing import set_trace_processors
    from tests.testing_processor import SPAN_PROCESSOR_TESTING, fetch_ordered_spans

    # This wire double preserves the visible partial item produced before OutputItemDone.
    partial = {
        "type": "output_text",
        "text": "visible partial answer",
        "partial": True,
        "truncated": truncated,
    }

    class InterruptedServer(OperationServer):
        async def events(self):
            operation = {
                "threadId": "thread_1",
                "turnId": "turn_1",
                "operationId": "model-1",
                "kind": "model",
                "requestedModel": "fixture",
                "startedAt": "2026-11-20T00:00:00Z",
            }
            yield {"method": "turn/operation/started", "params": operation}
            yield {
                "method": "turn/operation/completed",
                "params": {
                    **operation,
                    "completedAt": "2026-11-20T00:00:01Z",
                    "outcome": "interrupted",
                    "output": {"value": [partial], "truncated": False, "omitted": False},
                },
            }
            raise RuntimeError("synthetic interrupted turn")

    queue, _ = servers
    server = InterruptedServer(capture="content")
    queue.append(server)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    set_trace_processors(
        [SPAN_PROCESSOR_TESTING, OpenTelemetryTracingProcessor(tracer_provider=provider)]
    )
    try:
        config = RunConfig(
            model_provider=CodexProvider(cwd=tmp_path),
            tracing={"operation_detail": "content"},
        )
        with pytest.raises(RuntimeError, match="synthetic interrupted turn"):
            if streamed:
                run = Runner.run_streamed(Agent(name="worker"), "hello", run_config=config)
                async for _ in run.stream_events():
                    pass
            else:
                await Runner.run(Agent(name="worker"), "hello", run_config=config)
        native = next(span for span in fetch_ordered_spans() if span.span_data.type == "generation")
        otel = next(
            span
            for span in exporter.get_finished_spans()
            if span.attributes is not None
            and span.attributes.get("gen_ai.operation.name") == "chat"
        )
        assert native.span_data.output == [partial]
        assert otel.attributes is not None
        output = json.loads(json.dumps(otel.attributes["gen_ai.output.messages"]))
        assert output[0]["role"] == "assistant"
        assert output[0]["parts"][0] == {
            "type": "text",
            "content": partial["text"],
            "partial": True,
            "truncated": truncated,
        }
        assert native.span_data.operation.outcome == "interrupted"
        assert otel.attributes["openai.agents.operation.outcome"] == "interrupted"
        assert server.closed
    finally:
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "metadata", "content"])
@pytest.mark.parametrize("sensitive", [False, True])
async def test_codex_operations_preserve_source_spans_and_capture_policy(
    tmp_path, servers, detail, sensitive
):
    import json

    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    effective = "content" if detail == "content" and sensitive else "metadata"
    server = OperationServer(capture=effective)
    queue.append(server)
    result = await Runner.run(
        Agent(name="worker"),
        "sdk_input_secret",
        run_config=RunConfig(
            model_provider=CodexProvider(cwd=tmp_path),
            trace_include_sensitive_data=sensitive,
            tracing={} if detail is None else {"operation_detail": detail},
        ),
    )
    assert result.final_output == "A Codex response"
    assert dict(server.requests)["turn/start"]["operationTracing"] == effective
    operations = [span for span in fetch_ordered_spans() if hasattr(span.span_data, "operation")]
    models = [span for span in operations if span.span_data.type in {"response", "generation"}]
    tools = [span for span in operations if span.span_data.type == "function"]
    assert len(models) == 2 and len(tools) == 1
    assert {span.parent_id for span in operations} == {models[0].parent_id}
    assert models[0].started_at == "2026-11-20T00:00:01.000000000Z"
    assert models[0].ended_at == "2026-11-20T00:00:01.250Z"
    assert models[-1].ended_at == "2026-11-20T00:00:03.250Z"
    assert models[0].span_data.usage == {"input_tokens": 10, "output_tokens": 5}
    assert models[-1].span_data.usage == {"input_tokens": 30, "output_tokens": 15}
    assert models[-1].span_data.operation.response_model == "observed-model"
    assert models[-1].span_data.operation.outcome == "completed"
    if detail is None:
        assert all(span.span_data.type == "response" for span in models)
        assert models[0].span_data.input == (
            [{"role": "user", "content": "sdk_input_secret"}] if sensitive else None
        )
        assert models[-1].span_data.input is None
        assert (models[-1].span_data.response is not None) == sensitive
        if sensitive:
            assert models[-1].span_data.response.id == "turn_1"
            assert models[-1].span_data.response.usage.input_tokens == 10
        assert tools[0].span_data.input is None and tools[0].span_data.output is None
    else:
        assert all(span.span_data.type == "generation" for span in models)
        assert (models[0].span_data.input is not None) == (effective == "content")
        assert (tools[0].span_data.input is not None) == (effective == "content")
    exported = json.dumps([span.export() for span in operations])
    assert ("native_result_secret" in exported) == (effective == "content")
    if effective == "content":
        assert json.loads(tools[0].span_data.output) == {"text": OperationServer.file_text}
    if not sensitive:
        assert all(span.span_data.operation.response_id is None for span in models)


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "metadata", "content"])
async def test_stock_codex_has_one_partial_coverage_marker_without_synthetic_model_span(
    tmp_path, servers, detail
):
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    queue.append(FakeServer())
    result = await Runner.run(
        Agent(name="worker"),
        "Hello",
        run_config=RunConfig(
            model_provider=CodexProvider(cwd=tmp_path),
            tracing={} if detail is None else {"operation_detail": detail},
        ),
    )
    assert result.final_output == "A Codex response"
    spans = fetch_ordered_spans()
    markers = [
        span for span in spans if getattr(span.span_data, "name", None) == "codex.tracing.coverage"
    ]
    assert len(markers) == 1
    assert markers[0].span_data.data["coverage"] == "partial"
    assert not any(span.span_data.type in {"response", "generation"} for span in spans)


@pytest.mark.asyncio
async def test_codex_duplicate_and_foreign_telemetry_preserve_model_result(tmp_path, servers):
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    server = OperationServer(malformed=True)
    queue.append(server)
    run = Runner.run_streamed(
        Agent(name="worker"),
        "Hello",
        run_config=RunConfig(model_provider=CodexProvider(cwd=tmp_path)),
    )
    async for _ in run.stream_events():
        pass
    assert run.final_output == "A Codex response"
    spans = fetch_ordered_spans()
    assert (
        sum(getattr(span.span_data, "name", None) == "codex.tracing.coverage" for span in spans)
        == 1
    )
    assert sum(span.span_data.type == "response" for span in spans) == 2


@pytest.mark.asyncio
async def test_codex_early_stream_close_finishes_observed_operation(tmp_path, servers):
    from agents import ModelSettings
    from agents.models.interface import ModelTracing
    from agents.tracing import trace
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    server = OperationServer(active=True)
    queue.append(server)
    model = CodexProvider(cwd=tmp_path).get_model("test-model")
    with trace("test"):
        stream = model.stream_response(
            None,
            "Hello",
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.ENABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
        assert (await anext(stream)).type == "response.created"
        assert (await anext(stream)).type == "response.output_item.added"
        await stream.aclose()
    assert server.closed
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "response"]
    assert len(models) == 1
    assert models[0].ended_at is not None
    assert models[0].span_data.operation.outcome == "interrupted"
    assert models[0].span_data.operation.estimated_end


@pytest.mark.asyncio
async def test_codex_nested_and_overlapping_tools_keep_explicit_parents(tmp_path, servers):
    from tests.testing_processor import fetch_ordered_spans

    class NestedServer(OperationServer):
        async def events(self):
            common = {"threadId": "thread_1", "turnId": "turn_1"}
            model = {
                **common,
                "operationId": "model",
                "kind": "model",
                "requestedModel": "test-model",
                "startedAt": "2026-11-20T00:00:01Z",
            }
            yield {"method": "turn/operation/started", "params": model}
            yield {
                "method": "turn/operation/completed",
                "params": {**model, "completedAt": "2026-11-20T00:00:02Z", "outcome": "success"},
            }
            calls = []
            for name, parent in [("outer", None), ("nested", "outer"), ("parallel", None)]:
                event = {
                    **common,
                    "operationId": name,
                    "kind": "tool",
                    "toolName": name,
                    "startedAt": "2026-11-20T00:00:02Z",
                }
                if parent:
                    event["parentOperationId"] = parent
                calls.append(event)
                yield {"method": "turn/operation/started", "params": event}
            for event in reversed(calls):
                yield {
                    "method": "turn/operation/completed",
                    "params": {
                        **event,
                        "completedAt": "2026-11-20T00:00:03Z",
                        "outcome": "success",
                    },
                }
            async for event in FakeServer.events(self):
                yield event

    queue, _ = servers
    queue.append(NestedServer())
    result = await Runner.run(
        Agent(name="worker"),
        "Hello",
        run_config=RunConfig(model_provider=CodexProvider(cwd=tmp_path)),
    )
    assert result.final_output == "A Codex response"
    tools = {
        span.span_data.name: span
        for span in fetch_ordered_spans()
        if span.span_data.type == "function"
    }
    assert tools["nested"].parent_id == tools["outer"].span_id
    assert tools["parallel"].parent_id == tools["outer"].parent_id
    assert tools["parallel"].started_at == tools["outer"].started_at
    assert tools["parallel"].ended_at == tools["outer"].ended_at


@pytest.mark.asyncio
async def test_disabled_codex_tracing_omits_negotiation_and_operations(tmp_path, servers):
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    server = OperationServer(capture="content")
    queue.append(server)
    result = await Runner.run(Agent(name="worker"), "Hello", run_config=config(tmp_path))
    assert result.final_output == "A Codex response"
    assert "operationTracing" not in dict(server.requests)["turn/start"]
    assert fetch_ordered_spans() == []


@pytest.mark.asyncio
async def test_codex_default_end_callback_receives_final_sdk_response_and_source_time(
    tmp_path, servers, monkeypatch
):
    from tests.testing_processor import SPAN_PROCESSOR_TESTING

    snapshots = []
    original = SPAN_PROCESSOR_TESTING.on_span_end

    def observe_end(span):
        if span.span_data.type == "response":
            snapshots.append((span.span_data.response, span.ended_at, span.span_data.usage))
        original(span)

    monkeypatch.setattr(SPAN_PROCESSOR_TESTING, "on_span_end", observe_end)
    queue, _ = servers
    queue.append(OperationServer())
    result = await Runner.run(
        Agent(name="worker"),
        "Hello",
        run_config=RunConfig(model_provider=CodexProvider(cwd=tmp_path)),
    )
    assert result.final_output == "A Codex response"
    assert len(snapshots) == 2
    assert snapshots[0][0] is None
    response, ended_at, usage = snapshots[-1]
    assert response.id == "turn_1"
    assert response.output[0].content[0].text == "A Codex response"
    assert ended_at == "2026-11-20T00:00:03.250Z"
    assert usage == {"input_tokens": 30, "output_tokens": 15}


@pytest.mark.asyncio
async def test_codex_content_retains_wire_omission_and_truncation_markers(tmp_path, servers):
    import json

    from tests.testing_processor import fetch_ordered_spans

    class BoundedServer(OperationServer):
        async def events(self):
            async for event in super().events():
                params = event.get("params", {})
                if event["method"] == "turn/operation/started" and params["kind"] == "model":
                    params["input"]["truncated"] = True
                    params["input"]["reason"] = "capture_budget"
                if event["method"] == "turn/operation/completed" and params["kind"] == "tool":
                    params["output"] = {
                        "value": None,
                        "omitted": True,
                        "truncated": False,
                        "reason": "unsupported_content",
                    }
                yield event

    queue, _ = servers
    queue.append(BoundedServer(capture="content"))
    result = await Runner.run(
        Agent(name="worker"),
        "Hello",
        run_config=RunConfig(
            model_provider=CodexProvider(cwd=tmp_path),
            tracing={"operation_detail": "content"},
        ),
    )
    assert result.final_output == "A Codex response"
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "generation"]
    assert all(any(item.get("truncated") for item in span.span_data.input) for span in models)
    native = next(span for span in fetch_ordered_spans() if span.span_data.type == "function")
    assert json.loads(native.span_data.output) == {"omitted": True, "reason": "unsupported_content"}


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_parent", [False, True])
async def test_codex_suppresses_capture_when_trace_provider_or_parent_is_disabled(
    tmp_path, servers, disabled_parent
):
    from contextlib import nullcontext

    from agents.tracing import set_tracing_disabled, trace
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    server = OperationServer(capture="content")
    queue.append(server)
    parent = trace("disabled", disabled=True) if disabled_parent else nullcontext()
    if not disabled_parent:
        set_tracing_disabled(True)
    try:
        with parent:
            result = await Runner.run(
                Agent(name="worker"),
                "Hello",
                run_config=RunConfig(
                    model_provider=CodexProvider(cwd=tmp_path),
                    tracing={"operation_detail": "content"},
                ),
            )
    finally:
        if not disabled_parent:
            set_tracing_disabled(False)
    assert result.final_output == "A Codex response"
    assert "operationTracing" not in dict(server.requests)["turn/start"]
    assert fetch_ordered_spans() == []


@pytest.mark.asyncio
async def test_codex_capture_downgrade_marks_partial_coverage(tmp_path, servers):
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    server = OperationServer(capture="metadata")
    queue.append(server)
    result = await Runner.run(
        Agent(name="worker"),
        "Hello",
        run_config=RunConfig(
            model_provider=CodexProvider(cwd=tmp_path),
            tracing={"operation_detail": "content"},
        ),
    )
    assert result.final_output == "A Codex response"
    assert dict(server.requests)["turn/start"]["operationTracing"] == "content"
    markers = [
        span
        for span in fetch_ordered_spans()
        if getattr(span.span_data, "name", None) == "codex.tracing.coverage"
    ]
    assert len(markers) == 1
    assert markers[0].span_data.data["reason"] == "capture_downgraded"
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "generation"]
    assert len(models) == 2
    assert all(span.span_data.input is None and span.span_data.output is None for span in models)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_codex_teardown_failure_finishes_pending_source_span(tmp_path, servers, cancel):
    import asyncio

    from tests.testing_processor import fetch_ordered_spans

    closing = asyncio.Event()

    class TeardownServer(OperationServer):
        async def close(self):
            self.closed = True
            closing.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("teardown failed")

    queue, _ = servers
    queue.append(TeardownServer())
    task = asyncio.create_task(
        Runner.run(
            Agent(name="worker"),
            "Hello",
            run_config=RunConfig(model_provider=CodexProvider(cwd=tmp_path)),
        )
    )
    await closing.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "response"]
    assert len(models) == 2
    assert models[-1].ended_at == "2026-11-20T00:00:03.250Z"


@pytest.mark.asyncio
async def test_codex_releases_completed_tool_content_and_preserves_late_child_parent(
    tmp_path, servers, monkeypatch
):
    import gc
    import weakref

    from tests.testing_processor import SPAN_PROCESSOR_TESTING

    completed_tools = {}
    tool_edges = {}
    original = SPAN_PROCESSOR_TESTING.on_span_end

    def release_completed(span):
        if span.span_data.type == "function":
            completed_tools[span.span_data.name] = weakref.ref(span)
            tool_edges[span.span_data.name] = (span.span_id, span.parent_id)
        else:
            original(span)

    monkeypatch.setattr(SPAN_PROCESSOR_TESTING, "on_span_end", release_completed)

    class LateChildServer(OperationServer):
        async def events(self):
            common = {"threadId": "thread_1", "turnId": "turn_1"}
            for name, parent in [("outer", None), ("late_child", "outer")]:
                if parent is not None:
                    gc.collect()
                    assert completed_tools["outer"]() is None
                params = {
                    **common,
                    "operationId": name,
                    "kind": "tool",
                    "toolName": name,
                    "startedAt": "2026-11-20T00:00:01Z",
                    "input": {"value": "synthetic_content" * 1000},
                }
                if parent is not None:
                    params["parentOperationId"] = parent
                yield {"method": "turn/operation/started", "params": params}
                yield {
                    "method": "turn/operation/completed",
                    "params": {
                        **params,
                        "completedAt": "2026-11-20T00:00:02Z",
                        "outcome": "success",
                    },
                }
            async for event in FakeServer.events(self):
                yield event

    queue, _ = servers
    queue.append(LateChildServer(capture="content"))
    result = await Runner.run(
        Agent(name="worker"),
        "Hello",
        run_config=RunConfig(
            model_provider=CodexProvider(cwd=tmp_path),
            tracing={"operation_detail": "content"},
        ),
    )
    assert result.final_output == "A Codex response"
    assert tool_edges["late_child"][1] == tool_edges["outer"][0]


@pytest.mark.asyncio
async def test_codex_default_span_retains_exact_sdk_visible_response(tmp_path, servers):
    from agents import ModelSettings
    from agents.models.interface import ModelTracing
    from agents.tracing import trace
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    queue.append(OperationServer())
    with trace("test"):
        events = [
            event
            async for event in CodexProvider(cwd=tmp_path)
            .get_model(None)
            .stream_response(
                None,
                "Hello",
                ModelSettings(),
                [],
                None,
                [],
                ModelTracing.ENABLED,
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            )
        ]
    final = events[-1].response
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "response"]
    assert models[-1].span_data.response is final
    assert final.id == "turn_1"
    assert final.usage is not None
    assert final.usage.input_tokens == 10
    assert models[-1].span_data.usage["input_tokens"] == 30
    assert models[-1].span_data.operation.response_id == "resp_source_3"


@pytest.mark.asyncio
async def test_codex_direct_model_sensitive_veto_downgrades_child_capture(tmp_path, servers):
    from agents import ModelSettings
    from agents.models.interface import ModelTracing
    from agents.tracing import trace
    from agents.tracing.config import operation_tracing_scope
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = servers
    server = OperationServer(capture="metadata")
    queue.append(server)
    with trace("test"), operation_tracing_scope({"operation_detail": "content"}, True):
        async for _ in (
            CodexProvider(cwd=tmp_path)
            .get_model(None)
            .stream_response(
                None,
                "Hello",
                ModelSettings(),
                [],
                None,
                [],
                ModelTracing.ENABLED_WITHOUT_DATA,
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            )
        ):
            pass
    assert dict(server.requests)["turn/start"]["operationTracing"] == "metadata"
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "generation"]
    assert len(models) == 2
    assert all(span.span_data.operation.detail == "metadata" for span in models)
    assert all(span.span_data.input is None and span.span_data.output is None for span in models)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_codex_llm_hook_global_disable_prevents_child_capture(tmp_path, servers, streamed):
    from agents import RunHooks
    from agents.tracing import get_current_span, get_current_trace, set_tracing_disabled
    from tests.testing_processor import fetch_ordered_spans

    class DisableCapture(RunHooks):
        async def on_llm_start(self, context, agent, system_prompt, input_items):
            active_trace, active_span = get_current_trace(), get_current_span()
            assert active_trace is not None and active_trace.trace_id != "no-op"
            assert active_span is not None and active_span.span_id != "no-op"
            set_tracing_disabled(True)

    queue, _ = servers
    server = OperationServer(capture="content")
    queue.append(server)
    run_config = RunConfig(
        model_provider=CodexProvider(cwd=tmp_path), tracing={"operation_detail": "content"}
    )
    try:
        if streamed:
            result = Runner.run_streamed(
                Agent(name="worker"),
                "Hello",
                hooks=DisableCapture(),
                run_config=run_config,
            )
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(
                Agent(name="worker"),
                "Hello",
                hooks=DisableCapture(),
                run_config=run_config,
            )
    finally:
        set_tracing_disabled(False)
    assert result.final_output == "A Codex response"
    assert "operationTracing" not in dict(server.requests)["turn/start"]
    assert not any(
        span.span_data.type in {"generation", "response"} for span in fetch_ordered_spans()
    )
