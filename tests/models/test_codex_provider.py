import asyncio
import json
import sys
from collections import deque
from types import ModuleType

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    RunConfig,
    Runner,
    ToolExecutionConfig,
    UserError,
    function_tool,
    tool_namespace,
)
from agents.models.codex import CodexProvider


class FakeThread:
    thread_id = "thread_1"

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

    async def start_turn(self, inputs, schema, capture=None):
        self.operation_capture = capture
        self.inputs = json.loads(inputs)
        self.schema = json.loads(schema)
        self.started.set()
        return "turn_1"

    async def next_event(self):
        try:
            msg = await anext(self._events)
        except StopAsyncIteration:
            return None
        return json.dumps(msg if "method" in msg else {"id": "turn_1", "msg": msg})

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
@pytest.mark.parametrize(
    ("phase", "streamed"), [("input", False), ("output", True)], ids=["input", "streamed-output"]
)
async def test_classic_hooks_own_codex_tools_without_a_workspace_executor(
    tmp_path, threads, phase, streamed
):
    queue, _ = threads
    actions = []

    @function_tool
    async def publish(value: str) -> str:
        actions.append(value)
        return "internal"

    async def before(call):
        return {"value": "reviewed"}

    async def after(call, result):
        assert result == "internal"
        return "published"

    first = FakeThread(
        call={
            "callId": "req",
            "namespace": None,
            "tool": "publish",
            "arguments": {"value": "proposed"},
        }
    )
    second = FakeThread(text="done")
    queue.extend([first, second])
    run_config = config(tmp_path)
    run_config.tool_execution = ToolExecutionConfig(
        sdk_only_tool_hooks=True,
        prepare_tool_input=before if phase == "input" else None,
        prepare_tool_output=after if phase == "output" else None,
    )
    agent = Agent(name="test", tools=[publish])
    if streamed:
        result = Runner.run_streamed(agent, "go", run_config=run_config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "go", run_config=run_config)
    assert result.final_output == "done"
    assert actions == ["reviewed" if phase == "input" else "proposed"]
    assert first.settings["sdk_sandbox"] is True
    replay = second.history
    output = next(item for item in replay if item.get("type") == "function_call_output")
    assert output["output"] == ("published" if phase == "output" else "internal")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("namespace", "name", "reserved_name"),
    [
        ("apply_", "patch", "apply_patch"),
        ("spawn_", "agent", "spawn_agent"),
        ("multi_agent_v1", "spawn_agent", "spawn_agent"),
    ],
)
async def test_native_hooks_reject_sdk_tool_that_flattens_to_reserved_name(
    tmp_path, threads, namespace, name, reserved_name
):
    @function_tool(name_override=name)
    def local_tool(command: str) -> str:
        return command

    async def prepare(call):
        return call.arguments

    _, opened = threads
    tools = tool_namespace(name=namespace, description="SDK tools.", tools=[local_tool])
    run_config = config(tmp_path)
    run_config.tool_execution = ToolExecutionConfig(prepare_tool_input=prepare)

    with pytest.raises(UserError, match=f"Codex native hooks reserve {reserved_name}"):
        await Runner.run(Agent(name="test", tools=tools), "Use a tool.", run_config=run_config)

    assert opened == []


@pytest.mark.asyncio
async def test_runner_replays_typed_function_tool_result(tmp_path, threads):
    class Counts(BaseModel):
        total: int

    @function_tool(output_type=Counts)
    def count_items() -> dict[str, int]:
        return {"total": 3}

    queue, _ = threads
    first = FakeThread(call={"callId": "count", "tool": "count_items", "arguments": {}})
    second = FakeThread(text="Counted items.")
    queue.extend([first, second])

    result = await Runner.run(
        Agent(name="test", tools=[count_items]), "Count the items.", run_config=config(tmp_path)
    )

    tool_item = next(item for item in result.new_items if item.type == "tool_call_output_item")
    assert tool_item.output == Counts(total=3)
    replayed = next(item for item in second.history if item["type"] == "function_call_output")
    assert replayed["call_id"] == "count"
    assert json.loads(replayed["output"]) == {"total": 3}


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
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("primary", ["success", "failure", "cancel"])
@pytest.mark.parametrize("late_cancel", [False, True])
async def test_native_cleanup_preserves_cancellation_and_primary_failure(
    tmp_path, threads, restored, primary, late_cancel
):
    from agents.models.interface import ModelTracing

    close_started, release_close = asyncio.Event(), asyncio.Event()
    closed = []

    class HeldCloseThread(FakeThread):
        async def next_operation_event(self):
            await self.started.wait()
            return None

        async def resolve_approval(self, *args):
            self.started.set()
            if primary == "cancel":
                await asyncio.Event().wait()
            if primary == "failure":
                raise ValueError("primary failure")
            return "accepted", "turn_1", [], None

        async def close(self):
            close_started.set()
            try:
                await release_close.wait()
                raise RuntimeError("cleanup failure")
            finally:
                closed.append(True)
                await super().close()

    thread = HeldCloseThread(
        events=[{"type": "error"}] if primary == "failure" else [],
        gate=asyncio.Event() if primary == "cancel" else None,
    )
    threads[0].append(thread)
    provider = CodexProvider(cwd=tmp_path)
    checkpoint = {"version": 1, "cwd": str(tmp_path), "model": "fixture", "call": {}, "request": {}}
    operation = (
        provider._run_approved_native_call(checkpoint, ModelTracing.DISABLED)
        if restored
        else Runner.run(Agent(name="test"), "Answer", run_config=RunConfig(model_provider=provider))
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(thread.started.wait(), 2)
        if primary == "cancel":
            task.cancel("primary cancellation")
        await asyncio.wait_for(close_started.wait(), 2)
        if late_cancel:
            task.cancel("cleanup cancellation")
            await asyncio.sleep(0)
            task.cancel("repeated cancellation")
            await asyncio.sleep(0)
        assert not task.done()
        release_close.set()
        error, reason = RuntimeError, "cleanup failure"
        if primary == "failure":
            error = ValueError if restored else RuntimeError
            reason = "primary failure" if restored else "Codex model turn failed"
        if primary == "cancel" or late_cancel:
            error = asyncio.CancelledError
            first_cancel = primary == "cancel" and (restored or not late_cancel)
            reason = "primary cancellation" if first_cancel else "cleanup cancellation"
        with pytest.raises(error, match=reason):
            await task
        assert closed == [True]
    finally:
        release_close.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await provider.aclose()


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
        item.call_id
        for item in first_response.output
        if item.type == "function_call" and item.call_id.startswith("lookup_")
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


class OperationThread(FakeThread):
    # ASCII escapes would push this UTF-8 file over the capture budget.
    file_text = "native_result_secret" + "🙂" * 3000

    # This double preserves the negotiated wire and source timing at the real Runner boundary.
    def __init__(self, *, capture="metadata", malformed=False, active=False):
        super().__init__()
        self.capture = capture
        self.malformed = malformed
        self.active = active

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
async def test_codex_interrupted_visible_text_reaches_otel(tmp_path, threads, streamed, truncated):
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

    class InterruptedThread(OperationThread):
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

    queue, _ = threads
    thread = InterruptedThread(capture="content")
    queue.append(thread)
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
        assert thread.closed
    finally:
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "metadata", "content"])
@pytest.mark.parametrize("sensitive", [False, True])
async def test_codex_operations_preserve_source_spans_and_capture_policy(
    tmp_path, threads, detail, sensitive
):
    import json

    from tests.testing_processor import fetch_ordered_spans

    queue, _ = threads
    effective = "content" if detail == "content" and sensitive else "metadata"
    thread = OperationThread(capture=effective)
    queue.append(thread)
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
    assert thread.operation_capture == effective
    operations = [span for span in fetch_ordered_spans() if hasattr(span.span_data, "operation")]
    models = [span for span in operations if span.span_data.type in {"response", "generation"}]
    tools = [span for span in operations if span.span_data.type == "function"]
    assert len(models) == 2 and len(tools) == 1
    assert {span.parent_id for span in operations} == {models[0].parent_id}
    assert models[0].started_at == "2026-11-20T00:00:01.000000000Z"
    assert models[0].ended_at == "2026-11-20T00:00:01.250Z"
    assert models[-1].ended_at == "2026-11-20T00:00:03.250Z"
    for index, span in [(1, models[0]), (3, models[-1])]:
        usage = span.span_data.usage
        assert {
            key: usage[key] for key in ("requests", "input_tokens", "output_tokens", "total_tokens")
        } == {
            "requests": 1,
            "input_tokens": 10 * index,
            "output_tokens": 5 * index,
            "total_tokens": 15 * index,
        }
        assert usage["input_tokens_details"]["cached_tokens"] == 0
        assert usage["input_tokens_details"]["cache_write_tokens"] == 0
        assert usage["output_tokens_details"]["reasoning_tokens"] == 0
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
        assert json.loads(tools[0].span_data.output) == {"text": OperationThread.file_text}
    if not sensitive:
        assert all(span.span_data.operation.response_id is None for span in models)


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "metadata", "content"])
async def test_missing_native_operations_has_one_partial_coverage_marker_without_model_replay(
    tmp_path, threads, detail
):
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = threads
    queue.append(FakeThread())
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
async def test_codex_duplicate_and_foreign_telemetry_preserve_model_result(tmp_path, threads):
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = threads
    thread = OperationThread(malformed=True)
    queue.append(thread)
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
async def test_codex_early_stream_close_finishes_observed_operation(tmp_path, threads):
    from agents import ModelSettings
    from agents.models.interface import ModelTracing
    from agents.tracing import trace
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = threads
    thread = OperationThread(active=True)
    queue.append(thread)
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
    assert thread.closed
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "response"]
    assert len(models) == 1
    assert models[0].ended_at is not None
    assert models[0].span_data.operation.outcome == "interrupted"
    assert models[0].span_data.operation.estimated_end


@pytest.mark.asyncio
async def test_codex_nested_and_overlapping_tools_keep_explicit_parents(tmp_path, threads):
    from tests.testing_processor import fetch_ordered_spans

    class NestedThread(OperationThread):
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
            async for event in FakeThread.events(self):
                yield event

    queue, _ = threads
    queue.append(NestedThread())
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
async def test_disabled_codex_tracing_omits_negotiation_and_operations(tmp_path, threads):
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = threads
    thread = OperationThread(capture="content")
    queue.append(thread)
    result = await Runner.run(Agent(name="worker"), "Hello", run_config=config(tmp_path))
    assert result.final_output == "A Codex response"
    assert thread.operation_capture is None
    assert fetch_ordered_spans() == []


@pytest.mark.asyncio
async def test_codex_default_end_callback_receives_final_sdk_response_and_source_time(
    tmp_path, threads, monkeypatch
):
    from tests.testing_processor import SPAN_PROCESSOR_TESTING

    snapshots = []
    original = SPAN_PROCESSOR_TESTING.on_span_end

    def observe_end(span):
        if span.span_data.type == "response":
            snapshots.append((span.span_data.response, span.ended_at, span.span_data.usage))
        original(span)

    monkeypatch.setattr(SPAN_PROCESSOR_TESTING, "on_span_end", observe_end)
    queue, _ = threads
    queue.append(OperationThread())
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
    assert usage["requests"] == 1
    assert usage["input_tokens"] == 30
    assert usage["output_tokens"] == 15
    assert usage["total_tokens"] == 45
    assert usage["input_tokens_details"]["cached_tokens"] == 0
    assert usage["input_tokens_details"]["cache_write_tokens"] == 0
    assert usage["output_tokens_details"]["reasoning_tokens"] == 0


@pytest.mark.asyncio
async def test_codex_content_retains_wire_omission_and_truncation_markers(tmp_path, threads):
    import json

    from tests.testing_processor import fetch_ordered_spans

    class BoundedThread(OperationThread):
        async def events(self):
            async for event in super().events():
                params = event.get("params", {})
                if event.get("method") == "turn/operation/started" and params["kind"] == "model":
                    params["input"]["truncated"] = True
                    params["input"]["reason"] = "capture_budget"
                if event.get("method") == "turn/operation/completed" and params["kind"] == "tool":
                    params["output"] = {
                        "value": None,
                        "omitted": True,
                        "truncated": False,
                        "reason": "unsupported_content",
                    }
                yield event

    queue, _ = threads
    queue.append(BoundedThread(capture="content"))
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
    tmp_path, threads, disabled_parent
):
    from contextlib import nullcontext

    from agents.tracing import set_tracing_disabled, trace
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = threads
    thread = OperationThread(capture="content")
    queue.append(thread)
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
    assert thread.operation_capture is None
    assert fetch_ordered_spans() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_codex_teardown_failure_finishes_pending_source_span(tmp_path, threads, cancel):
    import asyncio

    from tests.testing_processor import fetch_ordered_spans

    closing = asyncio.Event()
    release = asyncio.Event()

    class TeardownThread(OperationThread):
        async def close(self):
            self.closed = True
            closing.set()
            if cancel:
                await release.wait()
                return
            raise RuntimeError("teardown failed")

    queue, _ = threads
    queue.append(TeardownThread())
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
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "response"]
    assert len(models) == 2
    assert models[-1].ended_at == "2026-11-20T00:00:03.250Z"


@pytest.mark.asyncio
async def test_codex_releases_completed_tool_content_and_preserves_late_child_parent(
    tmp_path, threads, monkeypatch
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

    class LateChildThread(OperationThread):
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
            async for event in FakeThread.events(self):
                yield event

    queue, _ = threads
    queue.append(LateChildThread(capture="content"))
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
async def test_codex_default_span_retains_exact_sdk_visible_response(tmp_path, threads):
    from agents import ModelSettings
    from agents.models.interface import ModelTracing
    from agents.tracing import trace
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = threads
    queue.append(OperationThread())
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
async def test_codex_direct_model_sensitive_veto_downgrades_child_capture(tmp_path, threads):
    from agents import ModelSettings
    from agents.models.interface import ModelTracing
    from agents.tracing import trace
    from agents.tracing.config import operation_tracing_scope
    from tests.testing_processor import fetch_ordered_spans

    queue, _ = threads
    thread = OperationThread(capture="metadata")
    queue.append(thread)
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
    assert thread.operation_capture == "metadata"
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "generation"]
    assert len(models) == 2
    assert all(span.span_data.operation.detail == "metadata" for span in models)
    assert all(span.span_data.input is None and span.span_data.output is None for span in models)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_codex_llm_hook_global_disable_prevents_child_capture(tmp_path, threads, streamed):
    from agents import RunHooks
    from agents.tracing import get_current_span, get_current_trace, set_tracing_disabled
    from tests.testing_processor import fetch_ordered_spans

    class DisableCapture(RunHooks):
        async def on_llm_start(self, context, agent, system_prompt, input_items):
            active_trace, active_span = get_current_trace(), get_current_span()
            assert active_trace is not None and active_trace.trace_id != "no-op"
            assert active_span is not None and active_span.span_id != "no-op"
            set_tracing_disabled(True)

    queue, _ = threads
    thread = OperationThread(capture="content")
    queue.append(thread)
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
    assert thread.operation_capture is None
    assert not any(
        span.span_data.type in {"generation", "response"} for span in fetch_ordered_spans()
    )


@pytest.mark.asyncio
async def test_codex_dropped_native_observations_mark_partial_without_replaying(tmp_path, threads):
    from tests.testing_processor import fetch_ordered_spans

    class DroppedThread(OperationThread):
        async def events(self):
            yield {
                "method": "turn/operation/dropped",
                "params": {"threadId": "thread_1", "droppedCount": 2},
            }
            async for event in super().events():
                yield event

    queue, opened = threads
    queue.append(DroppedThread())
    result = await Runner.run(
        Agent(name="worker"),
        "Hello",
        run_config=RunConfig(model_provider=CodexProvider(cwd=tmp_path)),
    )
    assert result.final_output == "A Codex response"
    assert len(opened) == 1 and not queue
    markers = [
        span
        for span in fetch_ordered_spans()
        if getattr(span.span_data, "name", None) == "codex.tracing.coverage"
    ]
    assert len(markers) == 1
    assert markers[0].span_data.data["reason"] == "dropped_operations"


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
@pytest.mark.parametrize("image_output", [False, True])
async def test_native_completed_pairs_survive_session_replay_without_sdk_execution(
    tmp_path, threads, kind, streamed, image_output
):
    from agents import SQLiteSession
    from agents.decorators import tool

    executions = []

    @tool
    def exec_command(cmd: str) -> str:
        executions.append(cmd)
        return "SDK result"

    pair = native_pair(kind)
    if image_output:
        pair[1]["output"] = [{"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="}]
    first = FakeThread(text="Done", raw_items=pair)
    second = FakeThread(text="Continued")
    queue, _ = threads
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
    expected_output = (
        [{**pair[1]["output"][0], "detail": "auto"}] if image_output else pair[1]["output"]
    )
    assert saved[2]["output"] == expected_output
    assert [item.type for item in result.new_items] == [
        "tool_call_item",
        "tool_call_output_item",
        "message_output_item",
    ]
    # Reopen the durable store and provider instead of retaining a native process or thread.
    reopened = SQLiteSession("native-history", str(tmp_path / "history.sqlite"))
    await Runner.run(agent, "Continue", session=reopened, run_config=config(tmp_path))
    injected = second.history
    assert [(item["type"], item["call_id"]) for item in injected if "call_id" in item] == [
        (kind, "native_1"),
        (kind + "_output", "native_1"),
    ]
    assert injected[2]["output"] == saved[2]["output"]
    assert executions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize("cancellation_id", [None, "sdk_cancelled_output"])
async def test_native_results_survive_serialized_adjacent_sdk_approval(
    tmp_path, threads, kind, cancellation_id
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

    class InterruptedThread(FakeThread):
        async def events(self):
            async for event in super().events():
                yield event
                if event["type"] == "dynamic_tool_call_request":
                    cancellation = {
                        "type": "function_call_output",
                        "call_id": "sdk_call",
                        "output": "dynamic tool call was cancelled before receiving a response",
                    }
                    if cancellation_id is not None:
                        cancellation["id"] = cancellation_id
                    yield {"type": "raw_response_item", "item": cancellation}

    first = InterruptedThread(
        raw_items=[*native],
        call={
            "callId": "sdk_call",
            "namespace": None,
            "tool": "lookup",
            "arguments": {"value": "record"},
        },
    )
    second = FakeThread(text="Done")
    queue, _ = threads
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
    injected = second.history
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
async def test_incomplete_native_pairs_fail_without_sdk_execution(tmp_path, threads, raw_items):
    server = FakeThread(raw_items=raw_items)
    queue, _ = threads
    queue.append(server)
    with pytest.raises(RuntimeError, match="native tool"):
        await Runner.run(Agent(name="test"), "Start", run_config=config(tmp_path))
    assert server.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"type": "context_compacted"},
        {"type": "item_started", "item": {"type": "ContextCompaction"}},
        {"type": "item_completed", "item": {"type": "ContextCompaction"}},
    ],
)
async def test_native_context_compaction_fails_before_acknowledging_history(
    tmp_path, threads, event
):
    queue, _ = threads
    thread = FakeThread(events=[event])
    queue.append(thread)
    with pytest.raises(UserError, match="native context compaction cannot be replayed"):
        await Runner.run(Agent(name="test"), "Start", run_config=config(tmp_path))
    assert thread.closed


@pytest.mark.asyncio
async def test_public_sdk_compaction_session_replays_into_codex(tmp_path, threads):
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
    queue, _ = threads
    server = FakeThread()
    queue.append(server)
    await Runner.run(Agent(name="test"), "Continue", session=session, run_config=config(tmp_path))
    injected = server.history
    assert {
        "type": "compaction",
        "id": "sdk_compaction",
        "encrypted_content": "synthetic-encrypted-context",
    } in injected


@pytest.mark.asyncio
async def test_other_models_cannot_bypass_sdk_approval_with_completed_output(tmp_path, threads):
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
    assert not threads[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_failure", [False, True])
@pytest.mark.parametrize("kind", ["function_call", "custom_tool_call"])
async def test_native_preparation_redacts_result_and_session_history(
    tmp_path, threads, monkeypatch, hook_failure, kind
):
    from agents import SQLiteSession
    from agents.models._codex_tool_hooks import CodexToolHooks

    configured = []

    async def configure(self, resources):
        configured.append(self)
        return {}

    monkeypatch.setattr(CodexToolHooks, "configure", configure)
    monkeypatch.setattr(CodexToolHooks, "verify", lambda self, thread: None)

    async def before(call):
        return {"command": "redacted-input"}

    async def after(call, output):
        if hook_failure:
            raise RuntimeError("Synthetic hook failure")
        return "redacted-output"

    class PreparedThread(FakeThread):
        async def events(self):
            call, output = native_pair(
                kind, name="exec_command" if kind == "function_call" else "apply_patch"
            )
            yield {"type": "raw_response_item", "item": call}
            hook = configured[0]
            assert await hook.decision(
                {
                    "event": "PreToolUse",
                    "name": "Bash",
                    "call_id": "native_1",
                    "arguments": {"command": "private-input"},
                }
            )
            assert await hook.decision(
                {
                    "event": "PostToolUse",
                    "name": "Bash",
                    "call_id": "native_1",
                    "arguments": {"command": "redacted-input"},
                    "result": "private-output",
                }
            )
            output["output"] = "private-output"
            yield {"type": "raw_response_item", "item": output}
            async for event in super().events():
                yield event

    queue, _ = threads
    queue.extend([PreparedThread(text="Done"), FakeThread()])
    session = SQLiteSession("prepared-native", str(tmp_path / "prepared.sqlite"))
    run_config = config(tmp_path)
    run_config.tool_execution = ToolExecutionConfig(
        prepare_tool_input=before, prepare_tool_output=after
    )
    if hook_failure:
        with pytest.raises(RuntimeError, match="Synthetic hook failure"):
            await Runner.run(Agent(name="test"), "Start", session=session, run_config=run_config)
        assert await session.get_items() == [{"role": "user", "content": "Start"}]
        return
    result = await Runner.run(Agent(name="test"), "Start", session=session, run_config=run_config)
    if kind == "function_call":
        assert result.new_items[0].raw_item.arguments == '{"cmd": "redacted-input"}'
    else:
        assert result.new_items[0].raw_item.input == "redacted-input"
    assert result.new_items[1].output == "redacted-output"
    saved = await session.get_items()
    if kind == "function_call":
        assert saved[1]["arguments"] == '{"cmd": "redacted-input"}'
    else:
        assert saved[1]["input"] == "redacted-input"
    assert saved[2]["output"] == "redacted-output"
    assert "private-output" not in json.dumps(result.to_input_list())


@pytest.mark.asyncio
async def test_interleaved_native_pairs_keep_model_order(tmp_path, threads):
    first_call, first_output = native_pair(call_id="native_a")
    second_call, second_output = native_pair("custom_tool_call", "native_b", "apply_patch")
    queue, _ = threads
    queue.append(FakeThread(raw_items=[first_call, second_call, second_output, first_output]))
    result = await Runner.run(Agent(name="test"), "Start", run_config=config(tmp_path))
    assert [
        (item["type"], item["call_id"]) for item in result.to_input_list() if "call_id" in item
    ] == [
        ("function_call", "native_a"),
        ("custom_tool_call", "native_b"),
        ("custom_tool_call_output", "native_b"),
        ("function_call_output", "native_a"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("post_tool_use", [False, True])
async def test_native_input_replay_retains_core_preserved_selectors(
    tmp_path, threads, monkeypatch, post_tool_use
):
    from agents import SQLiteSession
    from agents.models._codex_tool_hooks import CodexToolHooks

    configured = []

    async def configure(self, resources):
        configured.append(self)
        return {}

    monkeypatch.setattr(CodexToolHooks, "configure", configure)
    monkeypatch.setattr(CodexToolHooks, "verify", lambda self, thread: None)

    async def before(call):
        return {"value": "prepared"}

    class PreparedThread(FakeThread):
        async def events(self):
            call, output = native_pair(name="native_lookup")
            call["arguments"] = json.dumps({"value": "original", "environment_id": "workspace"})
            yield {"type": "raw_response_item", "item": call}
            hook = configured[0]
            assert "PostToolUse" in hook.events
            await hook.decision(
                {
                    "event": "PreToolUse",
                    "name": "native_lookup",
                    "call_id": "native_1",
                    "arguments": {"value": "original", "environment_id": "workspace"},
                }
            )
            if post_tool_use:
                await hook.decision(
                    {
                        "event": "PostToolUse",
                        "name": "native_lookup",
                        "call_id": "native_1",
                        "arguments": {"value": "prepared", "environment_id": "workspace"},
                        "result": output["output"],
                    }
                )
            if not post_tool_use:
                # Core omits PostToolUse when invocation returns a failed result.
                output["output"] = "native tool failed after invocation"
            yield {"type": "raw_response_item", "item": output}
            async for event in super().events():
                yield event

    queue, _ = threads
    queue.append(PreparedThread())
    run_config = config(tmp_path)
    run_config.tool_execution = ToolExecutionConfig(prepare_tool_input=before)
    session = SQLiteSession("effective-native", str(tmp_path / "effective.sqlite"))
    if not post_tool_use:
        with pytest.raises(UserError, match="without confirmed effective arguments"):
            await Runner.run(Agent(name="test"), "Start", session=session, run_config=run_config)
        assert await session.get_items() == [{"role": "user", "content": "Start"}]
        return
    result = await Runner.run(Agent(name="test"), "Start", session=session, run_config=run_config)
    assert json.loads(result.to_input_list()[1]["arguments"]) == {
        "value": "prepared",
        "environment_id": "workspace",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["function_call", "custom_tool_call"])
async def test_prepared_native_history_reaches_tracing_before_stream_completion(
    tmp_path, threads, monkeypatch, kind
):
    from agents.models._codex_tool_hooks import CodexToolHooks
    from tests.testing_processor import SPAN_PROCESSOR_TESTING

    configured = []
    snapshots = []
    original_end = SPAN_PROCESSOR_TESTING.on_span_end

    def on_span_end(span):
        if span.span_data.type == "response" and span.span_data.response is not None:
            snapshots.append(span.span_data.response.model_dump_json())
        original_end(span)

    async def configure(self, resources):
        configured.append(self)
        return {}

    monkeypatch.setattr(SPAN_PROCESSOR_TESTING, "on_span_end", on_span_end)
    monkeypatch.setattr(CodexToolHooks, "configure", configure)
    monkeypatch.setattr(CodexToolHooks, "verify", lambda self, thread: None)

    async def before(call):
        return {"command": "redacted-input"}

    async def after(call, output):
        return "redacted-output"

    class PreparedThread(OperationThread):
        async def events(self):
            call, output = native_pair(
                kind, name="exec_command" if kind == "function_call" else "apply_patch"
            )
            if kind == "function_call":
                call["arguments"] = json.dumps({"cmd": "private-input"})
            else:
                call["input"] = "private-input"
            output["output"] = "private-output"
            async for event in super().events():
                yield event
                params = event.get("params", {})
                if event.get("method") != "turn/operation/completed":
                    continue
                hook = configured[0]
                if params["operationId"] == "operation_1":
                    yield {"type": "raw_response_item", "item": call}
                    await hook.decision(
                        {
                            "event": "PreToolUse",
                            "name": "Bash",
                            "call_id": "native_1",
                            "arguments": {"command": "private-input"},
                        }
                    )
                elif params["operationId"] == "operation_2":
                    await hook.decision(
                        {
                            "event": "PostToolUse",
                            "name": "Bash",
                            "call_id": "native_1",
                            "arguments": {"command": "redacted-input"},
                            "result": "private-output",
                        }
                    )
                    yield {"type": "raw_response_item", "item": output}

    queue, _ = threads
    thread = PreparedThread()
    queue.append(thread)
    run = Runner.run_streamed(
        Agent(name="test"),
        "Start",
        run_config=RunConfig(
            model_provider=CodexProvider(cwd=tmp_path),
            tool_execution=ToolExecutionConfig(
                prepare_tool_input=before, prepare_tool_output=after
            ),
        ),
    )
    native_done = []
    async for event in run.stream_events():
        if event.type != "raw_response_event" or event.data.type != "response.output_item.done":
            continue
        item = event.data.item
        if getattr(item, "call_id", None) != "native_1":
            continue
        native_done.append(item.type)
        assert thread.closed
        assert len(snapshots) == 1
        assert "redacted-input" in snapshots[0] and "redacted-output" in snapshots[0]
        assert "private-input" not in snapshots[0] and "private-output" not in snapshots[0]
    assert native_done == [kind, kind + "_output"]
    assert run.final_output == "A Codex response"
    assert "private-input" not in json.dumps(run.to_input_list())
    assert "private-output" not in json.dumps(run.to_input_list())
