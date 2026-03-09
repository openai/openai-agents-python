# Results

When you call the `Runner.run` methods, you receive one of two result types:

-   [`RunResult`][agents.result.RunResult] from `Runner.run(...)` or `Runner.run_sync(...)`
-   [`RunResultStreaming`][agents.result.RunResultStreaming] from `Runner.run_streamed(...)`

Both inherit from [`RunResultBase`][agents.result.RunResultBase], which exposes the shared result surfaces such as `final_output`, `new_items`, `last_agent`, `raw_responses`, and `to_state()`.

`RunResultStreaming` adds streaming-specific controls such as [`stream_events()`][agents.result.RunResultStreaming.stream_events], [`current_agent`][agents.result.RunResultStreaming.current_agent], [`is_complete`][agents.result.RunResultStreaming.is_complete], and [`cancel(...)`][agents.result.RunResultStreaming.cancel].

## Choose the right result surface

Most applications only need a few result properties or helpers:

| If you need... | Use |
| --- | --- |
| The final answer to show the user | `final_output` |
| A replay-ready next-turn input list with the full local transcript | `to_input_list()` |
| Rich run items with agent, tool, handoff, and approval metadata | `new_items` |
| The agent that should usually handle the next user turn | `last_agent` |
| OpenAI Responses API chaining with `previous_response_id` | `last_response_id` |
| Pending approvals and a resumable snapshot | `interruptions` and `to_state()` |
| Metadata about the current nested `Agent.as_tool()` invocation | `agent_tool_invocation` |
| Raw model calls or guardrail diagnostics | `raw_responses` and the guardrail result arrays |

## Final output

The [`final_output`][agents.result.RunResultBase.final_output] property contains the final output of the last agent that ran. This is either:

-   a `str`, if the last agent did not have an `output_type` defined
-   an object of type `last_agent.output_type`, if the last agent had an output type defined
-   `None`, if the run stopped before a final output was produced, for example because it paused on an approval interruption

!!! note

    `final_output` is typed as `Any`. Handoffs can change which agent finishes the run, so the SDK cannot statically know the full set of possible output types.

In streaming mode, `final_output` stays `None` until the stream has finished processing. See [Streaming](streaming.md) for the event-by-event flow.

## Input, next-turn history, and new items

These surfaces answer different questions:

| Property or helper | What it contains | Best for |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | The base input for this run segment. If a handoff input filter rewrote the history, this reflects the filtered input the run continued with. | Auditing what this run actually used as input |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | A replay-ready next-turn input list built from `input` plus the converted `new_items` from this run. | Manual chat loops and client-managed conversation state |
| [`new_items`][agents.result.RunResultBase.new_items] | Rich [`RunItem`][agents.items.RunItem] wrappers with agent, tool, handoff, and approval metadata. | Logs, UIs, audits, and debugging |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | Raw [`ModelResponse`][agents.items.ModelResponse] objects from each model call in the run. | Provider-level diagnostics or raw response inspection |

In practice:

-   Use `to_input_list()` when your application manually carries the entire conversation transcript.
-   Use [`session=...`](sessions/index.md) when you want the SDK to load and save history for you.
-   If you are using OpenAI server-managed state with `conversation_id` or `previous_response_id`, usually pass only the new user input and reuse the stored ID instead of resending `to_input_list()`.

Unlike the JavaScript SDK, Python does not expose a separate `output` property for the model-shaped delta only. Use `new_items` when you need SDK metadata, or inspect `raw_responses` when you need the raw model payloads.

Computer-tool replay follows the raw Responses payload shape. Preview-model `computer_call` items preserve a single `action`, while `gpt-5.4` computer calls can preserve batched `actions[]`. [`to_input_list()`][agents.result.RunResultBase.to_input_list] and [`RunState`][agents.run_state.RunState] keep whichever shape the model produced, so manual replay, pause/resume flows, and stored transcripts continue to work across both preview and GA computer-tool calls. Local execution results still appear as `computer_call_output` items in `new_items`.

### New items

[`new_items`][agents.result.RunResultBase.new_items] gives you the richest view of what happened during the run. Common item types are:

-   [`MessageOutputItem`][agents.items.MessageOutputItem] for assistant messages
-   [`ReasoningItem`][agents.items.ReasoningItem] for reasoning items
-   [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] and [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem] for Responses tool search requests and loaded tool-search results
-   [`ToolCallItem`][agents.items.ToolCallItem] and [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] for tool calls and their results
-   [`ToolApprovalItem`][agents.items.ToolApprovalItem] for tool calls that paused for approval
-   [`HandoffCallItem`][agents.items.HandoffCallItem] and [`HandoffOutputItem`][agents.items.HandoffOutputItem] for handoff requests and completed transfers

Choose `new_items` over `to_input_list()` whenever you need agent associations, tool outputs, handoff boundaries, or approval boundaries.

When you use hosted tool search, inspect `ToolSearchCallItem.raw_item` to see the search request the model emitted, and `ToolSearchOutputItem.raw_item` to see which namespaces, functions, or hosted MCP servers were loaded for that turn.

## Continue or resume the conversation

### Next-turn agent

[`last_agent`][agents.result.RunResultBase.last_agent] contains the last agent that ran. This is often the best agent to reuse for the next user turn after handoffs.

In streaming mode, [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] updates as the run progresses, so you can observe handoffs before the stream finishes.

### Interruptions and run state

If a tool needs approval, pending approvals are exposed in [`RunResult.interruptions`][agents.result.RunResult.interruptions] or [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]. This can include approvals raised by direct tools, by tools reached after a handoff, or by nested [`Agent.as_tool()`][agents.agent.Agent.as_tool] runs.

Call [`to_state()`][agents.result.RunResult.to_state] to capture a resumable [`RunState`][agents.run_state.RunState], approve or reject the pending items, and then resume with `Runner.run(...)` or `Runner.run_streamed(...)`.

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="Use tools when needed.")
result = await Runner.run(agent, "Delete temp files that are no longer needed.")

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state)
```

For streaming runs, finish consuming [`stream_events()`][agents.result.RunResultStreaming.stream_events] first, then inspect `result.interruptions` and resume from `result.to_state()`. For the full approval flow, see [Human-in-the-loop](human_in_the_loop.md).

### Server-managed continuation

[`last_response_id`][agents.result.RunResultBase.last_response_id] is the latest model response ID from the run. Pass it back as `previous_response_id` on the next turn when you want to continue an OpenAI Responses API chain.

If you already continue the conversation with `to_input_list()`, `session`, or `conversation_id`, you usually do not need `last_response_id`. If you need every model response from a multi-step run, inspect `raw_responses` instead.

## Agent-as-tool metadata

When a result comes from a nested [`Agent.as_tool()`][agents.agent.Agent.as_tool] run, [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] exposes immutable metadata about the outer tool call:

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

For ordinary top-level runs, `agent_tool_invocation` is `None`.

This is especially useful inside `custom_output_extractor`, where you may need the outer tool name, call ID, or raw arguments while post-processing the nested result. See [Tools](tools.md) for the surrounding `Agent.as_tool()` patterns.

If you also need the parsed structured input for that nested run, read `context_wrapper.tool_input`. That is the field [`RunState`][agents.run_state.RunState] serializes generically for nested tool input, while `agent_tool_invocation` is the live result accessor for the current nested invocation.

## Streaming lifecycle and diagnostics

[`RunResultStreaming`][agents.result.RunResultStreaming] inherits the same result surfaces above, but adds streaming-specific controls:

-   [`stream_events()`][agents.result.RunResultStreaming.stream_events] to consume semantic stream events
-   [`current_agent`][agents.result.RunResultStreaming.current_agent] to track the active agent mid-run
-   [`is_complete`][agents.result.RunResultStreaming.is_complete] to see whether the streamed run has fully finished
-   [`cancel(...)`][agents.result.RunResultStreaming.cancel] to stop the run immediately or after the current turn

Keep consuming `stream_events()` until the async iterator finishes. A streaming run is not complete until that iterator ends, and summary properties such as `final_output`, `interruptions`, `raw_responses`, and session-persistence side effects may still be settling after the last visible token arrives.

If you call `cancel()`, continue consuming `stream_events()` so cancellation and cleanup can finish correctly.

Python does not expose a separate streamed `completed` promise or `error` property. Terminal streaming failures are surfaced by raising from `stream_events()`, and `is_complete` reflects whether the run has reached its terminal state.

### Raw responses

[`raw_responses`][agents.result.RunResultBase.raw_responses] contains the raw model responses collected during the run. Multi-step runs can produce more than one response, for example across handoffs or repeated model/tool/model cycles.

[`last_response_id`][agents.result.RunResultBase.last_response_id] is just the ID from the last entry in `raw_responses`.

### Guardrail results

Agent-level guardrails are exposed as [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] and [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results].

Tool guardrails are exposed separately as [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] and [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results].

These arrays accumulate across the run, so they are useful for logging decisions, storing extra guardrail metadata, or debugging why a run was blocked.

### Context and usage

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] exposes your app context together with SDK-managed runtime metadata such as approvals, usage, and nested `tool_input`.

Usage is tracked on `context_wrapper.usage`. For streamed runs, the usage totals can lag until the stream's final chunks have been processed. See [Context management](context.md) for the full wrapper shape and persistence caveats.
