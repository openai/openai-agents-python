import pytest

from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel


@function_tool
async def grab(x: int) -> int:
    return x * 2


async def collect_tool_events(run):
    events = []
    async for ev in run.stream_events():
        if hasattr(ev, "name"):
            item = getattr(ev, "item", None)
            events.append((ev.name, getattr(item, "name", None)))
        else:
            events.append((ev.type, None))
    return events


@pytest.mark.asyncio
async def test_stream_inner_events_single_agent(monkeypatch):
    """Verify we stream inner tool events for a single agent."""

    async def fake_stream(self):
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab_tool"})})
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab_tool"})})

    monkeypatch.setattr(
        Runner,
        "run_streamed",
        lambda *args, **kwargs: type("R", (), {"stream_events": fake_stream})(),
    )

    sub = Agent(name="sub", instructions="", tools=[grab])
    tool = sub.as_tool("grab_tool", "test", stream_inner_events=True)
    main = Agent(name="main", instructions="", tools=[tool])
    run = Runner.run_streamed(main, input="5")
    names = await collect_tool_events(run)
    assert names == [
        ("tool_called", "grab_tool"),
        ("tool_called", "grab"),
        ("tool_output", "grab"),
        ("tool_output", "grab_tool"),
    ]


@pytest.mark.asyncio
async def test_parallel_stream_inner_events(monkeypatch):
    """Verify we stream inner tool events for parallel tools."""

    async def fake_stream(self):
        for tool_name in ("A", "B"):
            yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": tool_name})})
            yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})})
            yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab"})})
            yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": tool_name})})

    monkeypatch.setattr(
        Runner,
        "run_streamed",
        lambda *args, **kwargs: type("R", (), {"stream_events": fake_stream})(),
    )

    sub = Agent(name="sub", instructions="", tools=[grab])
    a = sub.as_tool("A", "A", stream_inner_events=True)
    b = sub.as_tool("B", "B", stream_inner_events=True)
    main = Agent(name="main", instructions="", tools=[a, b])
    run = Runner.run_streamed(
        main,
        input="",
        run_config=RunConfig(model_settings=ModelSettings(parallel_tool_calls=True)),
    )
    names = await collect_tool_events(run)
    assert names.count(("tool_called", "grab")) == 2
    assert names.count(("tool_output", "grab")) == 2
    assert ("tool_called", "A") in names and ("tool_called", "B") in names


@pytest.mark.asyncio
async def test_as_tool_streams_nested_tool_calls(monkeypatch):
    """Ensure nested tool events surface when streaming a sub-agent."""

    async def fake_stream(self):
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "wrapper"})})
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "wrapper"})})

    monkeypatch.setattr(
        Runner, "run_streamed", lambda *a, **k: type("R", (), {"stream_events": fake_stream})()
    )
    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", lambda *a, **k: None)

    sub = Agent(name="sub", instructions="", tools=[grab])
    tool = sub.as_tool("wrapper", "desc", stream_inner_events=True)
    main = Agent(name="main", instructions="", tools=[tool])
    run = Runner.run_streamed(main, input="")

    events = await collect_tool_events(run)
    assert events == [
        ("tool_called", "wrapper"),
        ("tool_called", "grab"),
        ("tool_output", "grab"),
        ("tool_output", "wrapper"),
    ]


@pytest.mark.asyncio
async def test_as_tool_parallel_streams(monkeypatch):
    """Nested tool events appear for each tool when parallelized."""

    async def fake_stream(self):
        for name in ("A", "B"):
            yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": name})})
            yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})})
            yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab"})})
            yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": name})})

    monkeypatch.setattr(
        Runner, "run_streamed", lambda *a, **k: type("R", (), {"stream_events": fake_stream})()
    )
    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", lambda *a, **k: None)

    sub = Agent(name="sub", instructions="", tools=[grab])
    t1 = sub.as_tool("A", "A", stream_inner_events=True)
    t2 = sub.as_tool("B", "B", stream_inner_events=True)
    main = Agent(name="main", instructions="", tools=[t1, t2])
    run = Runner.run_streamed(
        main,
        input="",
        run_config=RunConfig(model_settings=ModelSettings(parallel_tool_calls=True)),
    )

    events = await collect_tool_events(run)
    assert events.count(("tool_called", "grab")) == 2
    assert events.count(("tool_output", "grab")) == 2
    assert ("tool_called", "A") in events and ("tool_called", "B") in events


@pytest.mark.asyncio
async def test_as_tool_error_propagation(monkeypatch):
    """Errors inside a sub-agent surface as outer tool_error events."""

    async def fake_stream(self):
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "outer"})})
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_error", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_error", "item": type("I", (), {"name": "outer"})})

    monkeypatch.setattr(
        Runner, "run_streamed", lambda *a, **k: type("R", (), {"stream_events": fake_stream})()
    )
    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", lambda *a, **k: None)

    sub = Agent(name="sub", instructions="", tools=[grab])
    tool = sub.as_tool("outer", "desc", stream_inner_events=True)
    main = Agent(name="main", instructions="", tools=[tool])
    run = Runner.run_streamed(main, input="")

    events = await collect_tool_events(run)
    assert ("tool_error", "outer") in events


@pytest.mark.asyncio
async def test_as_tool_empty_inner_run(monkeypatch):
    """An inner agent that does nothing still emits wrapper events."""

    async def fake_stream(self):
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "outer"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "outer"})})

    monkeypatch.setattr(
        Runner, "run_streamed", lambda *a, **k: type("R", (), {"stream_events": fake_stream})()
    )
    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", lambda *a, **k: None)

    sub = Agent(name="sub", instructions="", tools=[grab])
    tool = sub.as_tool("outer", "desc", stream_inner_events=True)
    main = Agent(name="main", instructions="", tools=[tool])
    run = Runner.run_streamed(main, input="")

    events = await collect_tool_events(run)
    assert events == [
        ("tool_called", "outer"),
        ("tool_output", "outer"),
    ]


@pytest.mark.asyncio
async def test_as_tool_mixed_reasoning_and_tools(monkeypatch):
    """Wrapper forwards reasoning and tool events in order."""

    async def fake_stream(self):
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "outer"})})
        yield type(
            "E", (), {"name": "reasoning_item_created", "item": type("I", (), {"name": "r"})}
        )
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab"})})
        yield type(
            "E", (), {"name": "reasoning_item_created", "item": type("I", (), {"name": "r2"})}
        )
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "outer"})})

    monkeypatch.setattr(
        Runner, "run_streamed", lambda *a, **k: type("R", (), {"stream_events": fake_stream})()
    )
    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", lambda *a, **k: None)

    sub = Agent(name="sub", instructions="", tools=[grab])
    tool = sub.as_tool("outer", "desc", stream_inner_events=True)
    main = Agent(name="main", instructions="", tools=[tool])
    run = Runner.run_streamed(main, input="")

    events = await collect_tool_events(run)
    assert events == [
        ("tool_called", "outer"),
        ("reasoning_item_created", "r"),
        ("tool_called", "grab"),
        ("tool_output", "grab"),
        ("reasoning_item_created", "r2"),
        ("tool_output", "outer"),
    ]


@pytest.mark.asyncio
async def test_as_tool_multiple_inner_tools(monkeypatch):
    """Two inner tools are streamed sequentially."""

    async def fake_stream(self):
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "outer"})})
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab"})})
        yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab2"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab2"})})
        yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "outer"})})

    monkeypatch.setattr(
        Runner, "run_streamed", lambda *a, **k: type("R", (), {"stream_events": fake_stream})()
    )
    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", lambda *a, **k: None)

    @function_tool
    async def grab2(x: int) -> int:
        return x + 1

    sub = Agent(name="sub", instructions="", tools=[grab, grab2])
    tool = sub.as_tool("outer", "desc", stream_inner_events=True)
    main = Agent(name="main", instructions="", tools=[tool])
    run = Runner.run_streamed(main, input="")

    events = await collect_tool_events(run)
    assert events == [
        ("tool_called", "outer"),
        ("tool_called", "grab"),
        ("tool_output", "grab"),
        ("tool_called", "grab2"),
        ("tool_output", "grab2"),
        ("tool_output", "outer"),
    ]


@pytest.mark.asyncio
async def test_as_tool_heavy_concurrency_ordering(monkeypatch):
    """Nested events from many tools appear once and in order."""

    async def fake_stream(self):
        for name in ("A", "B", "C", "D"):
            yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": name})})
            yield type("E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})})
            yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab"})})
            yield type("E", (), {"name": "tool_output", "item": type("I", (), {"name": name})})

    monkeypatch.setattr(
        Runner, "run_streamed", lambda *a, **k: type("R", (), {"stream_events": fake_stream})()
    )
    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", lambda *a, **k: None)

    sub = Agent(name="sub", instructions="", tools=[grab])
    tools = [sub.as_tool(n, n, stream_inner_events=True) for n in ("A", "B", "C", "D")]
    main = Agent(name="main", instructions="", tools=tools)
    run = Runner.run_streamed(
        main,
        input="",
        run_config=RunConfig(model_settings=ModelSettings(parallel_tool_calls=True)),
    )

    events = await collect_tool_events(run)
    assert events.count(("tool_called", "grab")) == 4
    assert events.count(("tool_output", "grab")) == 4
    for name in ("A", "B", "C", "D"):
        assert ("tool_called", name) in events


@pytest.mark.asyncio
async def test_as_tool_backward_compatibility(monkeypatch):
    """When stream_inner_events is False, inner events are hidden."""

    def fake_run_streamed(agent, *args, **kwargs):
        async def fake_stream(self):
            if agent.name == "sub":
                yield type(
                    "E", (), {"name": "tool_called", "item": type("I", (), {"name": "grab"})}
                )
                yield type(
                    "E", (), {"name": "tool_output", "item": type("I", (), {"name": "grab"})}
                )
            else:
                yield type(
                    "E", (), {"name": "tool_called", "item": type("I", (), {"name": "outer"})}
                )
                yield type(
                    "E", (), {"name": "tool_output", "item": type("I", (), {"name": "outer"})}
                )

        return type("R", (), {"stream_events": fake_stream})()

    monkeypatch.setattr(Runner, "run_streamed", fake_run_streamed)
    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", lambda *a, **k: None)

    sub = Agent(name="sub", instructions="", tools=[grab])
    tool = sub.as_tool("outer", "desc", stream_inner_events=False)
    main = Agent(name="main", instructions="", tools=[tool])
    run = Runner.run_streamed(main, input="")

    events = await collect_tool_events(run)
    assert events == [
        ("tool_called", "outer"),
        ("tool_output", "outer"),
    ]
