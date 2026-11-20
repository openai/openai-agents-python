import asyncio
import json
import sys
from collections import deque
from types import ModuleType

import pytest
from pydantic import BaseModel

from agents import Agent, RunConfig, Runner, UserError, function_tool
from agents.models.codex import CodexProvider


class FakeThread:
    # This double preserves Core events and controlled suspension through the real Runner.
    def __init__(self, text="A Codex response", call=None, gate=None, raw_items=(), events=None):
        self.text = text
        self.raw_items = raw_items
        self.call = call
        self.gate = gate
        self.extra_events = events or []
        self.closed = False
        self.interrupted = False
        self.started = asyncio.Event()
        self.instructions: str | None = None
        self.settings: dict[str, object] = {}
        self.history = []
        self.schema: dict[str, object] | None = None
        self._events = self.events()

    async def inject_items(self, items):
        self.history = json.loads(items)

    async def start_turn(self, inputs, schema):
        self.inputs = json.loads(inputs)
        self.schema = json.loads(schema)
        self.started.set()
        return "turn_1"

    async def next_event(self):
        try:
            msg = await anext(self._events)
        except StopAsyncIteration:
            return None
        return json.dumps({"id": "turn_1", "msg": msg})

    async def interrupt(self):
        self.interrupted = True

    async def close(self):
        self.closed = True
        await self._events.aclose()

    async def events(self):
        for event in self.extra_events:
            yield event
        for raw in self.raw_items:
            yield {"type": "raw_response_item", "item": raw}
        if self.gate is not None:
            yield {"type": "agent_message_content_delta", "item_id": "item_1", "delta": "A "}
            await self.gate.wait()
        if self.call is not None:
            yield {
                "type": "raw_response_item",
                "item": {
                    "type": "function_call",
                    "call_id": self.call["callId"],
                    "name": self.call["tool"],
                    "namespace": self.call.get("namespace"),
                    "arguments": json.dumps(self.call["arguments"]),
                },
            }
            yield {"type": "dynamic_tool_call_request", "turnId": "turn_1", **self.call}
            yield {"type": "raw_response_completed", "response_id": "response_1"}
        else:
            if self.gate is None:
                for delta in [self.text[:2], self.text[2:]]:
                    yield {
                        "type": "agent_message_content_delta",
                        "item_id": "item_1",
                        "delta": delta,
                    }
            yield {
                "type": "item_completed",
                "thread_id": "thread_1",
                "turn_id": "turn_1",
                "item": {
                    "type": "AgentMessage",
                    "id": "item_1",
                    "content": [{"type": "Text", "text": self.text}],
                    "phase": "final_answer",
                },
            }
        yield {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 3,
                    "output_tokens": 7,
                    "reasoning_output_tokens": 2,
                    "total_tokens": 17,
                }
            },
        }
        yield {
            "type": "turn_aborted" if self.call else "task_complete",
            "turn_id": "turn_1",
            "last_agent_message": self.text,
        }


@pytest.fixture
def threads(monkeypatch):
    queue = deque()
    opened = []
    module = ModuleType("agents.models._codex_native")

    class Manager:
        def __init__(self, *args):
            opened.append(args)

        async def close(self):
            opened.append("closed")

        async def start_thread(self, tools, instructions, settings):
            thread = queue.popleft()
            thread.tools = json.loads(tools)
            thread.instructions = instructions
            thread.settings = json.loads(settings)
            thread.model = thread.settings["model"] or "native-default"
            return thread

    module.Manager = Manager
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return queue, opened


def config(tmp_path, **kwargs):
    return RunConfig(model_provider=CodexProvider(cwd=tmp_path, **kwargs), tracing_disabled=True)


@pytest.mark.asyncio
async def test_runner_text_usage_streaming_and_replay(tmp_path, threads):
    queue, _ = threads
    first, second = FakeThread(), FakeThread()
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
    assert second.history[0] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Start here."}],
    }
    assert second.history[1]["phase"] == "final_answer"
    assert second.inputs == [{"type": "text", "text": "What follows?", "text_elements": []}]
    assert first is not second
    assert second.closed


@pytest.mark.asyncio
async def test_runner_owns_function_execution_and_history(tmp_path, threads):
    queue, _ = threads
    executions = []

    @function_tool
    def lookup(value: str) -> str:
        executions.append(value)
        return "Found " + value

    first = FakeThread(
        call={
            "callId": "call_1",
            "namespace": None,
            "tool": "lookup",
            "arguments": {"value": "record"},
        }
    )
    second = FakeThread(text="Found record")
    queue.extend([first, second])
    result = await Runner.run(
        Agent(name="test", tools=[lookup]), "Find a record.", run_config=config(tmp_path)
    )
    assert result.final_output == "Found record"
    assert executions == ["record"]
    assert first.interrupted and first.closed and second.closed
    injected = second.history
    assert any(
        item.get("type") == "function_call_output" and item.get("call_id") == "call_1"
        for item in injected
    )


@pytest.mark.asyncio
async def test_unsupported_settings_fail_before_launch(tmp_path, threads):
    queue, opened = threads
    from agents import ModelSettings

    with pytest.raises(UserError, match="temperature"):
        await Runner.run(
            Agent(name="test", model_settings=ModelSettings(temperature=0.1)),
            "Hello",
            run_config=config(tmp_path),
        )
    assert not opened and not queue


@pytest.mark.asyncio
async def test_runner_handoff_preserves_runner_ownership(tmp_path, threads):
    queue, _ = threads
    specialist = Agent(name="Specialist", instructions="Resolve requests.")
    first = FakeThread(
        call={
            "callId": "handoff_1",
            "namespace": None,
            "tool": "transfer_to_specialist",
            "arguments": {},
        }
    )
    second = FakeThread(text="Specialist answer")
    queue.extend([first, second])
    result = await Runner.run(
        Agent(name="Coordinator", handoffs=[specialist]), "Transfer", run_config=config(tmp_path)
    )
    assert result.last_agent is specialist
    assert result.final_output == "Specialist answer"
    assert first.interrupted and first.closed
    assert second.instructions == "Resolve requests."


@pytest.mark.asyncio
@pytest.mark.parametrize("close_early", [False, True])
async def test_stream_yields_delta_before_codex_turn_completes(tmp_path, threads, close_early):
    import asyncio

    from agents import ModelSettings
    from agents.models.interface import ModelTracing

    queue, _ = threads
    gate = asyncio.Event()
    thread = FakeThread(gate=gate)
    queue.append(thread)
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
    assert created.response.model == "native-default"
    added = await anext(stream)
    assert added.type == "response.output_item.added"
    first = await anext(stream)
    assert first.type == "response.output_text.delta" and first.delta == "A "
    assert not gate.is_set() and not thread.closed
    if close_early:
        await stream.aclose()
    else:
        gate.set()
        remaining = [event async for event in stream]
        assert remaining[-1].type == "response.completed"
    assert thread.closed


@pytest.mark.asyncio
async def test_runner_preserves_completed_stock_deferred_discovery(tmp_path, threads):
    from agents import ToolSearchTool

    queue, _ = threads

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
    first = FakeThread(
        raw_items=[search, found],
        call={
            "callId": "weather_1",
            "namespace": "lookup_weather",
            "tool": "lookup_weather",
            "arguments": {"city": "Paris"},
        },
    )
    second = FakeThread(text="Sunny in Paris")
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
    assert first.closed and second.closed
    assert first.settings["deferred_namespaces"] == ["lookup_weather"]
    assert first.tools[0]["tools"][0]["deferLoading"] is True


@pytest.mark.asyncio
async def test_raw_reasoning_is_preserved_for_replay(tmp_path, threads):
    from agents import ModelSettings

    queue, _ = threads
    thread = FakeThread(
        raw_items=[
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "A summary."}],
                "encrypted_content": "opaque-reasoning",
            }
        ]
    )
    queue.append(thread)
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
async def test_non_strict_output_guides_json_without_native_strict_schema(tmp_path, threads):
    from agents import AgentOutputSchema

    queue, _ = threads

    class Result(BaseModel):
        values: dict[str, str]
        note: str | None = None

    thread = FakeThread(text='{"values":{"route":"manual"}}')
    queue.append(thread)
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
    assert thread.schema is None
    instructions = thread.instructions
    assert instructions is not None
    assert instructions.startswith("Preserve the optional note.")
    assert "JSON" in instructions and '"additionalProperties"' in instructions
    assert "not a server-enforced constraint" in instructions


@pytest.mark.asyncio
async def test_non_strict_output_still_uses_runner_validation(tmp_path, threads):
    from agents import AgentOutputSchema, ModelBehaviorError

    queue, _ = threads

    class Result(BaseModel):
        values: dict[str, str]

    queue.append(FakeThread(text='{"values":42}'))
    with pytest.raises(ModelBehaviorError):
        await Runner.run(
            Agent(name="test", output_type=AgentOutputSchema(Result, strict_json_schema=False)),
            "Return a route",
            run_config=config(tmp_path),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_auto_previous_response_id_rejects_before_codex_startup(tmp_path, threads, streamed):
    _, opened = threads
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
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("use_handoff", [False, True])
async def test_required_tool_choice_returns_a_real_sdk_call(
    tmp_path, threads, streamed, use_handoff
):
    from agents import ModelSettings

    queue, opened = threads
    executions = []

    @function_tool
    def lookup(value: str) -> str:
        executions.append(value)
        return "Found " + value

    target = Agent(name="specialist")
    first = FakeThread(
        call={
            "callId": "call_required",
            "namespace": None,
            "tool": "transfer_to_specialist" if use_handoff else "lookup",
            "arguments": {} if use_handoff else {"value": "record"},
        }
    )
    second = FakeThread(text="Finished")
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
    instructions = first.instructions
    assert instructions is not None
    assert "Keep this instruction." in instructions
    assert "must call" in instructions
    assert "SDK tool or handoff" in instructions

    assert opened[0][2] == ["features.shell_tool=true", "features.view_image=true"]
    assert first.settings["require_sdk_call"] is True

    assert "agents_sdk namespace" not in instructions


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("choice", ["required", "none", "lookup"])
async def test_required_without_eligible_tools_and_other_choices_fail_before_launch(
    tmp_path, threads, streamed, choice
):
    from agents import ModelSettings

    queue, opened = threads
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
async def test_default_and_auto_tool_choice_keep_text_completion(tmp_path, threads, choice):
    from agents import ModelSettings

    queue, opened = threads
    thread = FakeThread()
    queue.append(thread)
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
    assert thread.instructions == "Only this instruction."
    assert thread.closed

    assert opened[0][2] == []


@pytest.mark.asyncio
async def test_required_rejects_only_hosted_tools_before_launch(tmp_path, threads):
    from agents import ModelSettings, WebSearchTool

    queue, opened = threads
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
async def test_required_accepts_sdk_local_tool_callbacks(tmp_path, threads, streamed, kind):
    from agents import ApplyPatchTool, ModelSettings
    from agents.editor import ApplyPatchResult
    from agents.tool import ShellTool

    queue, _ = threads
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
    first = FakeThread(
        call={
            "callId": "local_required",
            "namespace": "agents_sdk",
            "tool": kind,
            "arguments": arguments,
        }
    )
    second = FakeThread(text="Local SDK tool finished.")
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
    assert first.settings["sdk_sandbox"] is True
    assert first.instructions is not None
    assert "SDK-provided tools" in first.instructions


@pytest.mark.asyncio
async def test_cancellation_closes_only_the_cancelled_thread(tmp_path, threads):
    queue, opened = threads
    closing = asyncio.Event()
    release_close = asyncio.Event()

    class DelayedCloseThread(FakeThread):
        async def close(self):
            closing.set()
            await release_close.wait()
            await super().close()

    blocked = DelayedCloseThread(gate=asyncio.Event())
    survivor = FakeThread(text="Surviving response")
    queue.extend([blocked, survivor])
    provider = CodexProvider(cwd=tmp_path)
    run_config = RunConfig(model_provider=provider, tracing_disabled=True)
    task = asyncio.create_task(Runner.run(Agent(name="test"), "Wait", run_config=run_config))
    try:
        await blocked.started.wait()
        result = await Runner.run(Agent(name="test"), "Continue", run_config=run_config)
        task.cancel()
        await closing.wait()
        task.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert result.final_output == "Surviving response"
        assert blocked.closed and survivor.closed
        assert len(opened) == 1
        await provider.aclose()
        assert opened[-1] == "closed"
        with pytest.raises(RuntimeError, match="closed"):
            await Runner.run(Agent(name="test"), "New request", run_config=run_config)
    finally:
        release_close.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runner_structured_output_and_explicit_key(tmp_path, threads):
    queue, opened = threads

    class Result(BaseModel):
        answer: str

    thread = FakeThread(text='{"answer":"done"}')
    queue.append(thread)
    result = await Runner.run(
        Agent(name="test", output_type=Result),
        "Answer",
        run_config=config(tmp_path, api_key="synthetic-test-key"),
    )
    assert result.final_output == Result(answer="done")
    assert thread.schema is not None
    assert thread.schema["type"] == "object"
    assert opened[0][3] == "synthetic-test-key"
    assert thread.closed


@pytest.mark.asyncio
async def test_missing_native_module_fails_only_when_opted_in(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "agents.models._codex_native", None)
    provider = CodexProvider(cwd=tmp_path)
    provider.get_model(None)
    with pytest.raises(UserError, match="wheel|native|Codex"):
        await Runner.run(
            Agent(name="test"),
            "Hello",
            run_config=RunConfig(model_provider=provider, tracing_disabled=True),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_incomplete_turn_preserves_failure_and_cleanup(tmp_path, threads, streamed):
    class IncompleteThread(FakeThread):
        async def events(self):
            yield {
                "type": "agent_message_content_delta",
                "item_id": "unfinished",
                "delta": "Partial",
            }

    queue, _ = threads
    thread = IncompleteThread()
    queue.append(thread)
    events = []
    with pytest.raises(RuntimeError, match="without a completed response"):
        if streamed:
            run = Runner.run_streamed(Agent(name="test"), "Hello", run_config=config(tmp_path))
            async for event in run.stream_events():
                events.append(event)
        else:
            await Runner.run(Agent(name="test"), "Hello", run_config=config(tmp_path))
    assert thread.closed
    assert not any(
        event.type == "raw_response_event" and event.data.type == "response.completed"
        for event in events
    )


@pytest.mark.asyncio
async def test_sdk_function_stream_has_correlated_lifecycle_and_complete_arguments(
    tmp_path, threads
):
    queue, _ = threads

    @function_tool
    def lookup(value: str) -> str:
        return value

    first = FakeThread(
        call={
            "callId": "call_1",
            "namespace": None,
            "tool": "lookup",
            "arguments": {"value": "record"},
        }
    )
    queue.extend([first, FakeThread(text="record")])
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


@pytest.mark.asyncio
async def test_assistant_stream_uses_stock_start_and_done_without_duplicate_text(tmp_path, threads):
    queue, _ = threads

    class StartedMessageThread(FakeThread):
        async def events(self):
            yield {
                "type": "item_started",
                "item": {
                    "type": "AgentMessage",
                    "id": "item_1",
                    "content": [],
                    "phase": "final_answer",
                },
            }
            async for event in super().events():
                yield event
                if event["type"] == "item_completed":
                    yield event

    queue.append(StartedMessageThread())
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
@pytest.mark.parametrize("requested", [False, True])
async def test_native_summaries_expose_only_opted_in_public_text(tmp_path, threads, requested):
    from agents import ModelSettings

    queue, _ = threads
    queue.append(
        FakeThread(
            events=[
                {"type": "agent_reasoning_section_break", "item_id": "rs_1", "summary_index": 0},
                {
                    "type": "reasoning_content_delta",
                    "item_id": "rs_1",
                    "summary_index": 0,
                    "delta": "Public summary.",
                },
                {
                    "type": "reasoning_raw_content_delta",
                    "item_id": "rs_1",
                    "delta": "HIDDEN_REASONING",
                },
            ],
            raw_items=[
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Public summary."}],
                    "encrypted_content": "opaque-replay",
                }
            ],
        )
    )
    run = Runner.run_streamed(
        Agent(
            name="test",
            model_settings=ModelSettings(
                reasoning={"summary": "auto"} if requested else None,
                response_include=["reasoning.encrypted_content"],
            ),
        ),
        "Hello",
        run_config=config(tmp_path),
    )
    events = [
        event.data async for event in run.stream_events() if event.type == "raw_response_event"
    ]
    summaries = [
        event.delta for event in events if event.type == "response.reasoning_summary_text.delta"
    ]
    assert summaries == (["Public summary."] if requested else [])
    assert all("HIDDEN_REASONING" not in event.model_dump_json() for event in events)
    reasoning = next(item for item in run.to_input_list() if item.get("type") == "reasoning")
    assert reasoning["encrypted_content"] == "opaque-replay"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_required_tool_choice_rejects_text_only_completion(tmp_path, threads, streamed):
    from agents import ModelSettings

    @function_tool
    def lookup() -> str:
        raise AssertionError("The model did not request this tool.")

    queue, _ = threads
    thread = FakeThread(text="No SDK call")
    queue.append(thread)
    agent = Agent(name="test", tools=[lookup], model_settings=ModelSettings(tool_choice="required"))
    events = []
    with pytest.raises(UserError, match="required.*SDK tool or handoff"):
        if streamed:
            run = Runner.run_streamed(agent, "Find a record", run_config=config(tmp_path))
            async for event in run.stream_events():
                events.append(event)
        else:
            await Runner.run(agent, "Find a record", run_config=config(tmp_path))
    assert thread.closed
    assert not any(
        event.type == "raw_response_event" and event.data.type == "response.completed"
        for event in events
    )


@pytest.mark.asyncio
async def test_native_web_search_preserves_request_and_result(tmp_path, threads):
    from agents import WebSearchTool

    queue, _ = threads
    thread = FakeThread(
        raw_items=[
            {
                "type": "web_search_call",
                "id": "search_1",
                "status": "completed",
                "action": {"type": "search", "query": "weather"},
            }
        ]
    )
    queue.append(thread)
    result = await Runner.run(
        Agent(
            name="Search",
            model="gpt-5.5",
            tools=[
                WebSearchTool(
                    external_web_access=False,
                    search_context_size="high",
                    filters={"allowed_domains": ["example.com"]},
                    user_location={"type": "approximate", "country": "US", "city": "Boston"},
                    search_content_types=["text"],
                )
            ],
        ),
        "Search",
        run_config=config(tmp_path),
    )
    assert any(item.get("type") == "web_search_call" for item in result.to_input_list())
    assert thread.settings["web_search"] == {
        "external_web_access": False,
        "search_context_size": "high",
        "allowed_domains": ["example.com"],
        "user_location": {"country": "US", "city": "Boston"},
        "search_content_types": ["text"],
    }
    assert thread.settings["model"] == "gpt-5.5"
    assert thread.settings["reasoning_effort"] is None
    assert thread.tools == []


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
async def test_raw_usage_preserves_each_native_response_when_requested(
    tmp_path, threads, preserve, streamed
):
    from agents import ModelSettings

    metadata = [
        {"input_tokens": 4, "output_tokens": 0, "provider_details": {"cached": False}},
        {"input_tokens": 6, "output_tokens": 7},
    ]
    queue, _ = threads
    thread = FakeThread(
        events=[{"type": "raw_response_completed", "usage_metadata": entry} for entry in metadata]
    )
    queue.append(thread)
    agent = Agent(name="test", model_settings=ModelSettings(preserve_raw_usage=preserve))
    if streamed:
        result = Runner.run_streamed(agent, "Hello", run_config=config(tmp_path))
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "Hello", run_config=config(tmp_path))
    assert result.context_wrapper.usage.total_tokens == 17
    assert result.context_wrapper.usage.input_tokens_details.cached_tokens == 3
    assert result.context_wrapper.usage.output_tokens_details.reasoning_tokens == 2
    raw = result.raw_responses[0].raw_usage
    if preserve:
        assert raw is not None
        assert raw == {
            "responses": metadata,
            "token_count": {
                "total_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 3,
                    "output_tokens": 7,
                    "reasoning_output_tokens": 2,
                    "total_tokens": 17,
                }
            },
        }
        metadata[0]["input_tokens"] = 1000
        assert raw["responses"][0]["input_tokens"] == 4
    else:
        assert raw is None


@pytest.mark.asyncio
async def test_audio_tool_output_requests_native_modality_verification(tmp_path, threads):
    import base64
    import io
    import wave

    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 160)
    encoded = base64.b64encode(data.getvalue()).decode()
    queue, _ = threads
    thread = FakeThread(text="Heard")
    queue.append(thread)
    result = await Runner.run(
        Agent(name="test"),
        [
            {"type": "function_call", "call_id": "audio_1", "name": "listen", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "audio_1",
                "output": [
                    {"type": "input_audio", "input_audio": {"data": encoded, "format": "wav"}},
                ],
            },
        ],
        run_config=config(tmp_path),
    )
    assert result.final_output == "Heard"
    assert thread.settings["has_audio"] is True
    assert thread.history[1]["output"] == [
        {"type": "input_audio", "audio_url": f"data:audio/wav;base64,{encoded}"},
    ]
    assert thread.inputs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("native_error", [False, True])
async def test_cleanup_failure_never_returns_success_or_masks_attempt_error(
    tmp_path, threads, native_error
):
    class FailedCloseThread(FakeThread):
        async def close(self):
            await super().close()
            raise RuntimeError("Synthetic native cleanup failure")

    queue, _ = threads
    thread = FailedCloseThread(events=[{"type": "error"}] if native_error else [])
    queue.append(thread)
    message = "Codex model turn failed" if native_error else "Synthetic native cleanup failure"
    with pytest.raises(RuntimeError, match=message):
        await Runner.run(Agent(name="test"), "Answer", run_config=config(tmp_path))
    assert thread.closed


@pytest.mark.asyncio
async def test_closing_stream_during_tool_publication_retires_pending_native_call(
    tmp_path, threads
):
    from agents import ModelSettings
    from agents.models.interface import ModelTracing

    @function_tool
    def lookup() -> str:
        raise AssertionError("Only the Runner may execute this callback.")

    queue, _ = threads
    thread = FakeThread(
        call={"callId": "lookup_1", "namespace": None, "tool": "lookup", "arguments": {}}
    )
    queue.append(thread)
    stream = (
        CodexProvider(cwd=tmp_path)
        .get_model(None)
        .stream_response(
            None,
            "Look up a record",
            ModelSettings(),
            [lookup],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    )
    try:
        assert (await anext(stream)).type == "response.created"
        pending = await anext(stream)
        assert pending.type == "response.output_item.added"
        assert pending.item.type == "function_call"
        assert not thread.closed and not thread.interrupted
    finally:
        await stream.aclose()
    assert thread.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("first_callback_after_completion", [False, True])
async def test_sdk_batch_waits_for_its_complete_sampling_response(
    tmp_path, threads, first_callback_after_completion
):
    from agents import ModelSettings

    class SamplingThread(FakeThread):
        async def events(self):
            # An earlier Core sampling pass resolves an invalid call itself.
            # Its completion must not release the later SDK batch.
            yield {
                "type": "raw_response_item",
                "item": {
                    "type": "function_call",
                    "call_id": "invalid_earlier",
                    "name": "lookup",
                    "arguments": "not JSON",
                },
            }
            yield {
                "type": "raw_response_item",
                "item": {
                    "type": "function_call_output",
                    "call_id": "invalid_earlier",
                    "output": "Invalid arguments",
                },
            }
            yield {
                "type": "raw_response_completed",
                "response_id": "earlier",
                "token_usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                "usage_metadata": {"sampling": "earlier"},
            }
            yield {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 5}}}
            for suffix in ("a", "b"):
                yield {
                    "type": "raw_response_item",
                    "item": {
                        "type": "function_call",
                        "call_id": f"lookup_{suffix}",
                        "name": "lookup",
                        "namespace": "functions",
                        "arguments": json.dumps({"value": suffix}),
                    },
                }
                if suffix == "a" and not first_callback_after_completion:
                    yield {
                        "type": "dynamic_tool_call_request",
                        "callId": "lookup_a",
                        "turnId": "turn_1",
                        "tool": "lookup",
                        "arguments": {"value": "a"},
                    }
                    if self.interrupted:
                        yield {"type": "turn_aborted"}
                        return
            yield {
                "type": "raw_response_item",
                "item": {
                    "type": "function_call",
                    "call_id": "invalid_current",
                    "name": "lookup",
                    "arguments": "NaN",
                },
            }
            yield {
                "type": "raw_response_item",
                "item": {
                    "type": "reasoning",
                    "id": "late_reasoning",
                    "summary": [],
                    "encrypted_content": "late-opaque-replay",
                },
            }
            yield {
                "type": "raw_response_completed",
                "response_id": "sdk_batch",
                "token_usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
                "usage_metadata": {"sampling": "sdk_batch"},
            }
            # The second raw call may remain blocked by a native hook. Return
            # only actual admissions that arrive before the terminal event.
            for suffix in ("a",) if first_callback_after_completion else ("b",):
                yield {
                    "type": "dynamic_tool_call_request",
                    "callId": f"lookup_{suffix}",
                    "turnId": "turn_1",
                    "tool": "lookup",
                    "arguments": {"value": suffix},
                }
            yield {
                "type": "raw_response_item",
                "item": {
                    "type": "function_call_output",
                    "call_id": "invalid_current",
                    "output": "Invalid arguments",
                },
            }
            yield {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 20}}}
            yield {"type": "turn_aborted"}

    queue, _ = threads
    first = SamplingThread()
    queue.extend([first, FakeThread(text="Both records found")])
    effects = []

    @function_tool
    def lookup(value: str) -> str:
        assert first.closed
        effects.append(value)
        return "Found " + value

    result = await Runner.run(
        Agent(name="test", tools=[lookup], model_settings=ModelSettings(preserve_raw_usage=True)),
        "Find both records",
        run_config=config(tmp_path),
    )
    assert result.final_output == "Both records found"
    expected_calls = ["lookup_a"] if first_callback_after_completion else ["lookup_a", "lookup_b"]
    assert effects == (["a"] if first_callback_after_completion else ["a", "b"])
    assert result.context_wrapper.usage.total_tokens == 37
    first_response = result.raw_responses[0]
    assert [
        item.call_id for item in first_response.output if item.type == "function_call"
    ] == expected_calls
    reasoning = next(item for item in first_response.output if item.type == "reasoning")
    assert reasoning.encrypted_content == "late-opaque-replay"
    assert first_response.raw_usage is not None
    assert first_response.raw_usage["responses"] == [
        {"sampling": "earlier"},
        {"sampling": "sdk_batch"},
    ]


@pytest.mark.asyncio
async def test_aborted_sampling_does_not_publish_pending_sdk_calls(tmp_path, threads):
    class AbortedSamplingThread(FakeThread):
        async def events(self):
            yield {
                "type": "raw_response_item",
                "item": {
                    "type": "function_call",
                    "call_id": "uncompleted_call",
                    "name": "lookup",
                    "arguments": "{}",
                },
            }
            yield {
                "type": "dynamic_tool_call_request",
                "callId": "uncompleted_call",
                "turnId": "turn_1",
                "tool": "lookup",
                "arguments": {},
            }
            yield {"type": "turn_aborted"}

    @function_tool
    def lookup() -> str:
        raise AssertionError("An incomplete sample must not execute SDK callbacks.")

    queue, _ = threads
    thread = AbortedSamplingThread()
    queue.append(thread)
    with pytest.raises(RuntimeError, match="did not finish the turn successfully"):
        await Runner.run(Agent(name="test", tools=[lookup]), "Look up", run_config=config(tmp_path))
    assert thread.closed


@pytest.mark.asyncio
async def test_reasoning_replay_with_null_content_preserves_opaque_state(tmp_path, threads):
    queue, _ = threads
    thread = FakeThread(text="Continued")
    queue.append(thread)
    reasoning = {
        "type": "reasoning",
        "id": "prior_reasoning",
        "summary": [],
        "content": None,
        "encrypted_content": "opaque-prior-reasoning",
    }
    result = await Runner.run(
        Agent(name="test"),
        [reasoning, {"role": "user", "content": "Continue"}],
        run_config=config(tmp_path),
    )
    assert result.final_output == "Continued"
    assert thread.history == [reasoning]
    assert thread.settings["has_audio"] is False


@pytest.mark.asyncio
async def test_nested_sdk_admission_uses_outer_sampling_boundary(tmp_path, threads):
    class NestedThread(FakeThread):
        async def events(self):
            yield {
                "type": "raw_response_item",
                "item": {
                    "type": "custom_tool_call",
                    "call_id": "outer_code_execution",
                    "name": "execute",
                    "input": "lookup from a native code cell",
                },
            }
            yield {"type": "raw_response_completed", "response_id": "outer_response"}
            yield {
                "type": "dynamic_tool_call_request",
                "callId": "nested_lookup",
                "turnId": "turn_1",
                "tool": "lookup",
                "arguments": {},
            }
            assert self.interrupted
            yield {"type": "turn_aborted"}

    queue, _ = threads
    thread = NestedThread()
    queue.extend([thread, FakeThread(text="Nested lookup complete")])
    effects = []

    @function_tool
    def lookup() -> str:
        assert thread.closed
        effects.append("lookup")
        return "Nested value"

    result = await Runner.run(
        Agent(name="test", tools=[lookup]), "Look up", run_config=config(tmp_path)
    )
    assert result.final_output == "Nested lookup complete"
    assert effects == ["lookup"]
    assert result.raw_responses[0].output[0].call_id == "nested_lookup"
