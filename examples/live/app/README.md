# GPT Live with an Agents SDK specialist

This local browser example connects GPT Live to an ordinary Agents SDK Agent through Responses delegation. GPT Live handles speech; its managed Responses backend calls `ask_order_agent`; the Python server runs the order specialist with `Runner.run()` and returns a function result.

## When to use this pattern

This example uses the following path for specialist work:

`GPT Live → managed Responses model → ask_order_agent → Agents SDK Agent`

Choose this pattern when you want to reuse an existing Agent and let Live supply conversation context to a managed Responses model that decides when to call the specialist. The application executes the function and returns its result. This keeps the example's integration code small while leaving the specialist's instructions and tools in the Agents SDK.

The tradeoff is an additional model step: both the managed Responses model and the specialist's model do work, and the managed model continues after receiving the function result. That extra model work has its own usage cost and can increase end-to-end latency. Compare useful spoken response time, task success, and cost on your workload when choosing between the two patterns.

### When client delegation is a better fit

With client delegation, the application can call the Agent directly:

`GPT Live → application delegation handler → Agents SDK Agent`

| Decision | This example's Responses delegation pattern | Client delegation |
| --- | --- | --- |
| Context | Live supplies context to the managed backend, which constructs the specialist request. | The application chooses the history, memory, and task state sent to the Agent. |
| Execution | The managed model selects the specialist function; the application runs it. | The application controls routing, execution, and which results return to Live. |
| Model work | Includes the managed model and the specialist's model calls. | Can omit the managed model and invoke the specialist directly. |

Choose client delegation when you need that direct control or want to avoid the intermediate model step. The application must collect conversation context, construct backend requests, track delegated work, and send results back to Live. A client delegation event contains metadata, not the user's request text, so it cannot simply be passed to `Runner.run()` as a complete prompt.

This example implements Responses delegation only. See the [Live delegation guide](https://developers.openai.com/api/docs/guides/live-delegation#choose-a-delegation-mode) for the comparison and the client delegation flow. In either mode, application code remains responsible for permissions, confirmations, and business state.

## Run

Use Python 3.10 or later, `uv`, a microphone, and a project API key with access to GPT Live. From the repository root:

```bash
export OPENAI_API_KEY="your-project-api-key"
uv run --with-requirements examples/live/app/requirements.txt python -m examples.live.app.server
```

Open [http://localhost:8000](http://localhost:8000), select **Start conversation**, and grant microphone permission. Ask "What is the status of order A0042?" Then try "Actually, please check A0043." These are fictional records: A0042 is shipped with an expected September 15 delivery; A0043 is processing without a confirmed shipping date.

If autoplay is blocked, select play in the audio controls. **Mute microphone** controls local capture without stopping backend work. **End conversation** cancels application-owned work, requests Live finalization, and then releases the microphone and connections.

The dependency override installs a Live-capable OpenAI Python SDK for this command without changing the repository's lockfile or base SDK requirements. The example requires `openai>=3.13.0,<4`. GPT Live voice duration and both backend models can incur usage charges.

## How it works

1. The browser creates a WebRTC offer and sends it over an application WebSocket.
2. The server creates a Live session with Responses delegation and attaches a sideband before returning the SDP answer. Audio travels between the browser and OpenAI; the sideband carries server events and commands.
3. The managed Responses model turns the conversation into a self-contained `ask_order_agent(request)` function call.
4. The server collects completed function items from nested `response.output_item.done` events. It does not read pending calls from the empty terminal `response.output` snapshot.
5. A separate worker runs the specialist while the receiver continues processing Live events. The specialist uses a read-only `lookup_order` tool.
6. The worker submits every required `function_call_output` for a completed response before sending one `response.create` continuation.

The browser never executes functions. Transcript fragments are appended exactly as received, separately for each speaker; they are not authoritative completed turns. Specialist output appears in its own panel because result submission does not establish that GPT Live spoke it or that the listener heard it.

### What the specialist knows on each call

During the Live conversation, Live supplies context to the managed Responses backend. The backend's instructions ask it to include the relevant context, order ID, and latest corrections in each `ask_order_agent` request. The specialist receives that request string, not the full voice transcript or an automatically shared conversation history.

For example, after "Check order A0042" followed by "Actually, A0043," a self-contained specialist request could be "Check the status of order A0043. The user corrected the earlier order ID A0042." A request such as "Actually, A0043" alone leaves the specialist without the task it needs to perform.

Each `Runner.run()` starts a separate specialist run. The specialist can use its model and tool exchanges within that run, but this example passes no session or previous run items into the next call. Reusing the same `Agent` object reuses its instructions and tools; it does not preserve conversation history between calls. If a request omits necessary information, the specialist is instructed to ask for clarification.

## Customize

Replace `create_order_agent()` in `agent.py` with your existing text Agent, and update the managed backend's function description and instructions. The two backend models can be chosen independently in `create_order_agent()` and `session_config()`. Keep detailed business logic in the specialist and pass relevant conversation context through the outer function's arguments.

The sample does not implement tool approvals. If you add approval-requiring tools, implement an explicit approval and resume flow before treating an interrupted run as a completed operation. Specialist guardrails apply to that Agent's execution; they do not approve all output from the managed Responses backend or all speech from GPT Live.

## Boundaries and failures

This is a localhost demo. The server binds to `127.0.0.1:8000` and accepts the two documented localhost origins. An origin check is not authentication: add application authentication, authorization, and resource limits before hosting this for other users.

The example has no persistence, automatic reconnection, application-level retries, write operations, or client delegation. Duplicate function events are ignored within one connection. Agent errors become a short failure result; transport errors stop processing instead of rerunning work or blindly resending results. The normal SDK model retry policy still applies within a specialist run.

Corrections are handled through the managed Responses model's conversation context and subsequent function calls. Every result includes its order ID and returns to the original call ID. The example does not infer cancellation from transcript fragments or claim that a spoken correction cancels work already running.

When setup fails after creation, the server attempts to attach only to close the created session. If a connection fails before `session.closed`, final voice usage is unconfirmed; the example reports this instead of claiming successful finalization. Canceling Agent work does not reverse an external operation. All tools in this demo are read-only.

## Offline checks

From the repository root, run `uv run pytest tests/test_live_example.py` for delegation and server lifecycle tests. With Node.js 18 or later, run `node --test examples/live/app/test_ui.cjs` for the controlled browser callback test. These checks do not connect to the OpenAI API.

## References

- [Live delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation)
- [Live WebRTC connections](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live)
- [Live server-side controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live)
