# PR: feat: add ToolContext.send_progress() for streaming tool progress events

### Summary

Adds `ToolContext.send_progress(data)` — a simple API for function tools to emit intermediate progress events during execution. Events appear in `RunResultStreaming.stream_events()` as `ToolProgressStreamEvent` while the tool is still running. In non-streaming mode (`Runner.run()`), calls are silently ignored.

**Motivation (re: #1333)**

Existing lifecycle hooks (`on_tool_start` / `on_tool_end`) fire at the boundaries of tool execution, but they don't cover cases where a tool needs to emit multiple intermediate updates from inside the tool body. Without framework support, developers resort to external shared state or event buses, which add complexity and couple tool logic to infrastructure concerns. Providing an official way for tools to emit mid-execution progress events improves developer experience and makes responsive UIs and long-running workflows (data processing, web scraping, multi-step API calls) much easier to build.

**New stream event type**

A new `ToolProgressStreamEvent` is added to the `StreamEvent` union alongside the existing `RawResponsesStreamEvent`, `RunItemStreamEvent`, and `AgentUpdatedStreamEvent`. It carries:

- `tool_name: str` — identifies which tool emitted the event
- `tool_call_id: str` — correlates with a specific tool call (important when parallel tools run)
- `data: Any` — arbitrary progress payload (dict, string, number, etc.)
- `type: Literal["tool_progress_stream_event"]` — discriminator for pattern matching

Consumers can filter for progress events via `isinstance(event, ToolProgressStreamEvent)` or `event.type == "tool_progress_stream_event"`.

**Design**

- **Transport**: A `_StreamContext` dataclass (holding `event_queue` and `event_loop`) on `RunContextWrapper` — piggybacking on an object already threaded through the entire execution chain. Zero intermediate function signature changes.
- **API**: `send_progress()` method on `ToolContext` — scoped to function tools, reads per-tool identity (`tool_name`, `tool_call_id`) from the instance. No shared mutable state.
- **Thread safety**: Uses `loop.call_soon_threadsafe()` with a stored event loop reference so sync tools (`sync_invoker=True`) running in worker threads can safely call `send_progress()`. The loop is captured at wiring time (on the event loop thread), not at call time.
- **Nested agent-as-tool**: Each `Runner.run_streamed()` creates a new `RunContextWrapper` with its own `_stream_context`. No cross-contamination between outer and inner runs.

**Usage — basic tool with progress**

```python
from agents import Agent, Runner, function_tool, ToolProgressStreamEvent
from agents.tool_context import ToolContext

@function_tool
async def analyze_data(ctx: ToolContext, query: str) -> str:
    ctx.send_progress({"status": "fetching", "progress": 0.25})
    # ... work ...
    ctx.send_progress({"status": "processing", "progress": 0.75})
    # ... more work ...
    return "analysis complete"

agent = Agent(name="Analyst", tools=[analyze_data])

result = Runner.run_streamed(agent, "Analyze Q4 sales")
async for event in result.stream_events():
    if isinstance(event, ToolProgressStreamEvent):
        print(f"[{event.tool_name}] {event.data}")
```

**Usage — agent-as-tool with `on_stream` handler**

When an agent is used as a tool via `as_tool()`, inner progress events are delivered to the `on_stream` callback:

```python
from agents import Agent
from agents.stream_events import ToolProgressStreamEvent

def handle_inner_stream(payload):
    event = payload["event"]
    if isinstance(event, ToolProgressStreamEvent):
        print(f"Inner tool progress: {event.data}")

inner_agent = Agent(name="Researcher", tools=[analyze_data])
outer_agent = Agent(
    name="Orchestrator",
    tools=[inner_agent.as_tool(on_stream=handle_inner_stream)],
)
```

**Usage — non-streaming mode (no-op)**

```python
# send_progress is silently ignored — no error, no side effects
result = await Runner.run(agent, "Analyze Q4 sales")
```

### Test plan

- Unit tests for `send_progress` with active stream context, without context (no-op), and with broken context (failure isolation)
- Unit tests for `ToolProgressStreamEvent` field validation and `data: Any` flexibility
- Propagation tests: `_stream_context` survives `_fork_with_tool_input`, `_fork_without_tool_input`, and `ToolContext.from_agent_context`
- Integration: streaming run with progress events appearing in `stream_events()`
- Integration: non-streaming run with `send_progress` as no-op
- Integration: parallel tools emitting progress with correct `tool_call_id` attribution
- Integration: progress events arrive before `tool_output` event for the same tool
- 13 tests total, all passing

### Issue number

Closes #1333

### Checks

- [x] I've added new tests (if relevant)
- [x] I've added/updated the relevant documentation
- [x] I've run `make lint` and `make format`
- [x] I've made sure tests pass
