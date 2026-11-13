# Classic agent patterns

<a id="common-agentic-patterns"></a>

These examples compose agents with application control flow, tools, handoffs, guardrails, and approvals. Complete [Classic example setup](../README.md#setup), then run a module from the repository root:

```sh
python -m examples.classic.agent_patterns.deterministic
```

## Choose a pattern

| Pattern | Examples | Behavior |
| --- | --- | --- |
| <a id="deterministic-flows"></a>Ordered stages | [`deterministic.py`](deterministic.py) | The application generates an outline, checks quality and the science-fiction requirement, then writes a story only after both checks pass. |
| <a id="handoffs-and-routing"></a>Handoffs and routing | [`routing.py`](routing.py) | A triage agent hands conversation ownership to a language specialist. |
| <a id="agents-as-tools"></a>Agents as tools | [`agents_as_tools.py`](agents_as_tools.py) | An orchestrator calls translation specialists and receives their results. |
| Conditional tools | [`agents_as_tools_conditional.py`](agents_as_tools_conditional.py) | Application context controls which specialist tools are available. |
| Nested streaming | [`agents_as_tools_streaming.py`](agents_as_tools_streaming.py) | An `on_stream` callback exposes nested agent events. |
| Structured tool input | [`agents_as_tools_structured.py`](agents_as_tools_structured.py) | `Agent.as_tool()` accepts typed arguments. |
| Tool selection | [`forcing_tool_use.py`](forcing_tool_use.py) | Model settings select tool-use behavior. |
| <a id="llm-as-a-judge"></a>Feedback and revision | [`llm_as_a_judge.py`](llm_as_a_judge.py) | One agent drafts an outline while another supplies feedback for revision. |
| <a id="parallelization"></a>Parallel work | [`parallelization.py`](parallelization.py) | The application generates translations concurrently and selects one. |
| <a id="guardrails"></a>Input policy | [`input_guardrails.py`](input_guardrails.py) | An input guardrail rejects math-homework requests. |
| Output policy | [`output_guardrails.py`](output_guardrails.py) | An output guardrail checks the generated response. |
| Streaming policy | [`streaming_guardrails.py`](streaming_guardrails.py) | The example handles a guardrail failure while consuming a stream. |
| <a id="human-in-the-loop"></a>Human approval | [`human_in_the_loop.py`](human_in_the_loop.py) | The application pauses a run, collects approvals, and resumes its saved state. |
| Streaming approval | [`human_in_the_loop_stream.py`](human_in_the_loop_stream.py) | The application combines stream events with approval interruptions. |
| Rejection messages | [`human_in_the_loop_custom_rejection.py`](human_in_the_loop_custom_rejection.py) | Run-level error formatting and a per-call rejection message control tool feedback. |
| Server-owned approvals | [`human_in_the_loop_server.py`](human_in_the_loop_server.py) | The server retains snapshots and accepts owner-checked, single-use decisions. |
| <a id="hosted-multi-agent-experimental"></a>Hosted multi-agent, experimental | [`hosted_multi_agent_beta.py`](hosted_multi_agent_beta.py) | The Responses API coordinates hosted subagents while Classic Runner executes local function tools. |

## Handoffs or tools

Handoffs transfer the conversation to another agent with the prior history. Agents used as tools return a result to the calling agent. The translation examples show both approaches for the same task.

## Guardrail timing

The input-guardrail example runs its check alongside agent execution. A triggered guardrail raises `InputGuardrailTripwireTriggered`; the output check raises `OutputGuardrailTripwireTriggered`. Concurrent checks do not guarantee prevention of earlier tool effects. Blocking input guardrails use `run_in_parallel=False` when validation needs to finish before agent execution.

## Approval state

The local approval examples keep saved state under trusted application control. The server example's CLI simulates an authenticated client; HTTP authentication and shared production storage remain application responsibilities. Client-supplied serialized `RunState` does not serve as a trusted approval snapshot.
