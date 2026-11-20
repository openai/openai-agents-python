# GPT-Live with an Agents SDK specialist

This local browser example uses GPT-Live-1.1, an Agents SDK order specialist, and hosted web search to demonstrate speech-triggered standby, multilingual conversation, and Responses delegation.

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

Open [http://localhost:8000](http://localhost:8000), select **Connect**, and grant microphone permission. Try this sequence in a single call:

1. When the demo is waiting for speech, ask for the status of order A0042 and spell out the letter and each digit, including the zeros. To try a language switch, ask and request a reply in Japanese. The fictional order A0042 has shipped and is expected September 15.
2. After the assistant finishes speaking, stay quiet until the inference status shows **Standby**. The demo pauses inference after twenty seconds without an input or output transcript. The WebRTC connection and conversation history are retained.
3. Say "Actually, the last digit is three. Could you tell me the status in English?" The session resumes on speech. Check the specialist activity and listen for the corrected order A0043, which is processing with no confirmed shipping date.

If the browser blocks autoplay, select **Play assistant audio**. The microphone button controls local capture while backend work continues. A slash marks the muted state. **Disconnect** cancels application-owned work, requests Live finalization, and then releases the microphone and connections.

To try hosted Web Search, say "Search the web for the latest OpenAI API news." When the backend cites sources, their links appear in the conversation. Turn on **Debug events** to see the logs beside the conversation on wider screens or below it on smaller screens.

The dependency override installs `openai>=3.57.0,<4` for WebRTC standby without changing the repository's lockfile or base SDK requirements. Active and standby voice time have [different rates](https://developers.openai.com/api/docs/models/gpt-live-1.1). The demo shows their durations after ending the call when Live reports the breakdown. Both backend models can incur separate usage charges.

## How it works

1. The browser creates a WebRTC offer and sends it over an application WebSocket.
2. The server creates a Live session with speech wake and Responses delegation and attaches a sideband before returning the SDP answer. Audio travels between the browser and OpenAI. The server forwards `session.paused` and `session.resumed` from the sideband so the browser can display the inference state.
3. The managed Responses model turns the conversation into a self-contained `ask_order_agent(request)` function call.
4. The server collects completed function items from nested `response.output_item.done` events. It does not read pending calls from the empty terminal `response.output` snapshot.
5. A separate worker runs the specialist while the receiver continues processing Live events. The specialist uses a read-only `lookup_order` tool.
6. The worker submits every required `function_call_output` for a completed response before sending one `response.create` continuation.

The server executes custom functions, while the managed Responses backend runs [hosted web search](https://developers.openai.com/api/docs/guides/live-delegation#add-hosted-tools) directly. The browser's conversation timeline shows transcript fragments, specialist updates, and web citations in the order they arrive. It keeps transcript fragments verbatim and groups adjacent transcript or specialist updates from the same source. Use the Specialist entries to follow lookup progress and result submission. Listen to the call for the assistant's spoken reply.

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

From the repository root, run `uv run pytest tests/test_live_example.py` for delegation and server lifecycle tests. With Node.js 18 or later, run `node --test examples/live/app/test_ui.cjs` for browser event handling and standby display tests. These checks do not connect to the OpenAI API.

## References

- [Live delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation)
- [Live standby configuration](https://developers.openai.com/api/docs/guides/live-conversations#configure-standby)
- [Live WebRTC connections](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live)
- [Live server-side controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live)
