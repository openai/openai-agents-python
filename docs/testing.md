# Testing

The SDK provides deterministic, provider-neutral testing utilities for Agent workflows, Sandbox sessions, Realtime sessions, and Voice pipelines. These utilities run in memory, make no model, sandbox-provider, or Realtime API requests, and record the normalized interactions that the SDK owns. The runnable recipes below disable tracing for each run so that the default trace processor does not upload test activity when an OpenAI API key is configured.

Use them to test orchestration owned by your application and the SDK: tool execution, handoffs, guardrails, retries, streaming, session behavior, Sandbox capabilities, Realtime event handling, and Voice pipeline composition. Use real provider adapters or integration environments for behavior owned by an external model, network protocol, sandbox provider, or audio system.

## Find the recipe you need

| I want to... | Use | Go to |
| --- | --- | --- |
| Return a fixed final answer | `ScriptedModel` with `assistant_message()` | [Return a fixed response](#return-a-fixed-response) |
| Exercise a multi-turn tool loop | `function_call()` followed by an assistant response | [Test a tool workflow](#test-a-tool-workflow) |
| Choose a response from the request | `ModelStep.respond()` or a `responder` mapping | [Derive a response from the request](#derive-a-response-from-the-request) |
| Assert what the runner sent to the model | `calls`, `first_call`, or `last_call` | [Inspect model calls](#inspect-model-calls) |
| Test a streamed run | A normal response step, or `ModelStep.stream()` for exact events | [Test streaming](#test-streaming) |
| Test an error or retry decision | `ModelStep.raise_error()` | [Inject model failures](#inject-model-failures) |
| Detect an accidental workflow change | Exact FIFO steps plus `assert_complete()` | [Detect workflow drift](#detect-workflow-drift) |
| Test a `SandboxAgent` without starting a sandbox | `scripted_sandbox_session()` plus `ScriptedModel` | [Test a Sandbox Agent workflow](#test-a-sandbox-agent-workflow) |
| Match Sandbox calls or derive their results | `match` or `responder` on a Sandbox step | [Configure Sandbox steps](#configure-sandbox-steps) |
| Test a Realtime session without opening a connection | `ScriptedRealtimeModel` and `RealtimeStep` | [Test a Realtime session](#test-a-realtime-session) |
| Test a Realtime tool workflow | Emit a `RealtimeModelToolCallEvent` and expect tool output | [Test a Realtime tool workflow](#test-a-realtime-tool-workflow) |
| Test a static or streamed Voice pipeline | `ScriptedSTTModel`, `ScriptedTTSModel`, and a scripted or real workflow | [Test a Voice pipeline](#test-a-voice-pipeline) |
| Test provider serialization or wire payloads | The real provider adapter with a controlled network transport | [Choose the correct boundary](#choose-the-correct-boundary) |

## Imports

The testing APIs live next to the runtime boundary they replace:

| Boundary | Import path |
| --- | --- |
| Agent model and Sandbox workflows | `agents.testing` |
| Realtime model transport | `agents.realtime.testing` |
| Voice STT, TTS, and workflow components | `agents.voice.testing` |

Testing symbols are intentionally kept out of the top-level `agents` import.

## Agent workflow recipes

### Return a fixed response

Pass one sequence of normalized output items for each expected model call. The output-sequence shorthand receives a deterministic response ID and usage for one request.

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message


@pytest.mark.asyncio
async def test_fixed_response() -> None:
    model = ScriptedModel(
        [[assistant_message("Paris is the capital of France.")]]
    )
    agent = Agent(name="Geography assistant", model=model)

    result = await Runner.run(
        agent,
        "What is the capital of France?",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Paris is the capital of France."
    assert len(model.calls) == 1
    model.assert_complete()
```

Finish deterministic workflow tests with `model.assert_complete()`. It catches the case where the workflow stopped before consuming every configured step.

### Test a tool workflow

Script one model response that calls the tool and a second response that produces the final answer. The real SDK tool pipeline runs between those model calls.

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call


@tool
def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"{city}: sunny"


@pytest.mark.asyncio
async def test_tool_workflow() -> None:
    model = ScriptedModel(
        [
            [function_call("get_weather", {"city": "Tokyo"}, call_id="call_1")],
            [assistant_message("It is sunny in Tokyo.")],
        ]
    )
    agent = Agent(name="Weather assistant", model=model, tools=[get_weather])

    result = await Runner.run(
        agent,
        "What is the weather in Tokyo?",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "It is sunny in Tokyo."
    assert len(model.calls) == 2
    assert model.last_call is not None
    assert any(
        item.get("type") == "function_call_output"
        for item in model.last_call.input
    )
    model.assert_complete()
```

This pattern covers tool input validation, execution, result conversion, hooks, guardrails, and the next model turn. Calling the Python function directly would bypass those SDK behaviors.

### Derive a response from the request

Use `ModelStep.respond()` when a response genuinely depends on the normalized model call or when an assertion belongs at the model boundary. The responder may be synchronous or asynchronous and may return any step shape accepted by `ScriptedModel`.

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message


def respond(call: ModelCall):
    assert call.streamed is False
    assert call.input == [{"content": "Summarize this", "role": "user"}]
    return {"output": [assistant_message("Handled the normalized request.")]}


@pytest.mark.asyncio
async def test_request_aware_response() -> None:
    model = ScriptedModel([ModelStep.respond(respond)])
    agent = Agent(name="Assistant", model=model)

    result = await Runner.run(
        agent,
        "Summarize this",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Handled the normalized request."
    model.assert_complete()
```

`ScriptedModel` accepts `ModelStep`, the equivalent dictionary form, `ModelResponse`, a normalized output-item sequence, or an exception. Prefer fixed output sequences when a response does not depend on the call because fixed scripts make unexpected turns easier to diagnose.

### Inspect model calls

`ScriptedModel` records each call before it resolves or raises the selected step.

| Member | Contains |
| --- | --- |
| `calls` | Every `ModelCall` in invocation order |
| `first_call` | The first call, or `None` |
| `last_call` | The most recent call, or `None` |
| `remaining_steps` | The number of configured steps not yet consumed |

Common assertions include `call.input`, `call.model_settings`, `call.tools`, `call.handoffs`, and `call.streamed`. Mutable request data is snapshotted at the invocation boundary, and each public history accessor returns detached snapshots. Tool, handoff, output-schema, and tracing objects keep their runtime identity.

Structured `call_index` and `input_index` error fields are zero-based so they directly index `calls[...]` or the supplied step sequence. Human-readable error messages display one-based call or step numbers.

Use `enqueue()` or `extend()` when one test needs to append model steps incrementally. Create a new `ScriptedModel` for an independent scenario; the utility does not reset consumed steps or call history.

### Test streaming

A normal response step supports both `Runner.run()` and `Runner.run_streamed()`. For common assistant messages, reasoning items, function calls, and apply-patch calls, `ScriptedModel` generates normalized start, delta, item-completion, and terminal response events. The terminal response carries the complete output and usage.

Use `ModelStep.stream()` only when the exact normalized `TResponseStreamEvent` sequence is part of the behavior under test:

```python
step = ModelStep.stream(
    events,
    output=[assistant_message("The terminal output used by the runner.")],
)
```

`events` may be a fixed sequence or an async factory that receives the recorded `ModelCall`. The optional `output` is the response returned if the same step is used in a non-streaming call. Exact stream events are SDK-normalized events, not Responses API or Chat Completions wire chunks.

Automatic streaming rejects normalized output-item kinds whose incremental lifecycle is not implemented. Use `ModelStep.stream(...)` for those items instead of relying on a partial event sequence.

### Inject model failures

Use `ModelStep.raise_error()` to fail one model call. Optional retry advice belongs to that exact scripted error:

```python
from agents import ModelRetryAdvice
from agents.testing import ModelStep


step = ModelStep.raise_error(
    RuntimeError("temporary failure"),
    retry_advice=ModelRetryAdvice(suggested=True, replay_safety="safe"),
)
```

The runner's retry policy decides whether advice causes another attempt. Each retry is another model call and consumes the next scripted step. The Python helper accepts a fixed `ModelRetryAdvice` value; use a custom `Model` when retry advice itself must vary dynamically by attempt.

### Detect workflow drift

Treat the scripted calls as the expected workflow shape. An extra model request raises `UnexpectedModelCall`; an early exit leaves steps for `assert_complete()` to report.

When your test framework supports teardown or finalizers, place `assert_complete()` there if you also want unconsumed steps reported after another assertion fails. Do not catch mismatch errors in a normal regression test.

| Error | Structured fields | Meaning |
| --- | --- | --- |
| `InvalidModelStep` | `reason`, `input_index` | A step is malformed and is rejected before entering the queue |
| `UnexpectedModelCall` | `call`, `call_index` | The workflow made another model call after the script ended |
| `UnconsumedModelSteps` | `remaining_steps` | The workflow ended before using every step |

## Sandbox Agent recipes

### Test a Sandbox Agent workflow

Combine `ScriptedModel` with `scripted_sandbox_session()` to exercise the real `SandboxAgent` runtime without creating a local container or remote sandbox. The model script chooses a capability tool, while the Sandbox script defines what the corresponding `SandboxSession` method returns.

```python
import pytest

from agents import RunConfig, Runner
from agents.sandbox import ExecResult, SandboxAgent
from agents.sandbox.capabilities import Shell
from agents.testing import (
    ScriptedModel,
    assistant_message,
    function_call,
    scripted_sandbox_session,
)


@pytest.mark.asyncio
async def test_sandbox_workflow() -> None:
    sandbox = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "match": lambda call: call.args == ("pwd",),
                "result": ExecResult(
                    stdout=b"/workspace\n",
                    stderr=b"",
                    exit_code=0,
                ),
            }
        ]
    )
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "pwd"}, call_id="call_1")],
            [assistant_message("The workspace is /workspace.")],
        ]
    )
    agent = SandboxAgent(
        name="Workspace assistant",
        model=model,
        capabilities=[Shell()],
    )

    async with sandbox:
        result = await Runner.run(
            agent,
            "Which directory are you in?",
            run_config=RunConfig(
                sandbox={"session": sandbox},
                tracing_disabled=True,
            ),
        )

    assert result.final_output == "The workspace is /workspace."
    assert [call.method for call in sandbox.calls] == ["exec"]
    sandbox.assert_complete()
    model.assert_complete()
```

This test crosses two normalized SDK boundaries. It covers tool argument validation, capability routing, Sandbox session invocation, delivery of the tool result to the next model turn, and final output handling. It does not test whether a real model chooses the command or how a real sandbox provider executes it.

### Configure Sandbox steps

Each matching Sandbox call consumes the next step in one global FIFO sequence. A method mismatch, matcher rejection, or matcher exception leaves that step pending. Set `method`, choose exactly one outcome, and add `match` only when the call details matter.

| Step member | Use it when... |
| --- | --- |
| `result` | The method should return a fixed typed value |
| `responder` | The result depends on the detached `SandboxCall` |
| `error` | The method should raise a specific exception |
| `match` | The call should be rejected before producing its outcome unless the matcher returns a value other than `False` |

The supported scripted method names are `apply_patch`, `exec`, `ls`, `mkdir`, `pty_exec_start`, `pty_write_stdin`, `read`, `rm`, and `write`. Only configured model-facing capabilities are exposed. The two PTY methods are exposed together when either PTY method is configured because they form one interactive-shell capability, but calls still consume the global FIFO script.

`sandbox.calls` contains detached `SandboxCall` snapshots with zero-based `call_index`, `method`, positional `args`, and read-only `kwargs`. Static results are also snapshotted when the script is created. `io.BytesIO` and `io.StringIO` values are supported; use a custom Sandbox session for other live stream objects or lifecycle behavior.

| Error | Structured fields | Meaning |
| --- | --- | --- |
| `InvalidSandboxStep` | `reason`, `input_index`, `method` | A step is malformed or names an unsupported method |
| `UnexpectedSandboxCall` | `call`, `call_index`, `actual_method`, `expected_method`, `remaining_steps` | The workflow called the wrong method or continued after the script ended |
| `SandboxCallMatcherError` | `call`, `call_index`, `method` | A step matcher returned `False` |
| `UnconsumedSandboxSteps` | `remaining_steps`, `pending_methods` | The workflow ended before using every step |

The returned object is the session itself. Pass it directly to `RunConfig(sandbox={"session": sandbox})`; there is no wrapper `.session` attribute.

## Realtime recipes

### Test a Realtime session

`ScriptedRealtimeModel` implements the Python SDK's normalized `RealtimeModel` boundary. Each `RealtimeStep` matches one outbound `RealtimeModelSendEvent` and then emits normalized inbound `RealtimeModelEvent` objects or raises an injected error.

```python
import pytest

from agents.realtime import (
    RealtimeAgent,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelSendUserInput,
    RealtimeRawModelEvent,
    RealtimeRunner,
)
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


@pytest.mark.asyncio
async def test_realtime_message() -> None:
    reply = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="Hello!",
        response_id="response_1",
    )
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(
                expect=RealtimeModelSendUserInput(user_input="Hello"),
                emit=[reply],
            )
        ]
    )
    runner = RealtimeRunner(
        RealtimeAgent(name="Assistant"),
        model=model,
        config={"tracing_disabled": True},
    )

    observed_reply = False
    async with await runner.run() as session:
        await session.send_message("Hello")
        async for event in session:
            if isinstance(event, RealtimeRawModelEvent) and event.data == reply:
                observed_reply = True
                break

    assert observed_reply
    assert model.sent_events == (RealtimeModelSendUserInput(user_input="Hello"),)
    assert model.closed is True
    model.assert_complete()
```

An expectation may be an exact event value, an event class matched with `isinstance`, or a callable that receives the outbound event and returns `True` for a match. Strict mode is enabled by default. With `strict=False`, unrelated outbound events are recorded but do not consume a pending step; this is useful when a session emits incidental events that are outside the behavior under test.

Use `connect_events` to emit inbound events during connection. Use `connect_error` or `close_error` for lifecycle failures, and use `RealtimeStep(error=...)` for a failure tied to one matched send. A step cannot define both `emit` and `error`.

### Test a Realtime tool workflow

Attach a real function tool to `RealtimeAgent`, emit a normalized tool call, and expect the SDK to send the tool output through the model boundary. Setting `async_tool_calls` to `False` makes this small example complete during connection without test-specific waiting machinery.

```python
import pytest

from agents.decorators import tool
from agents.realtime import (
    RealtimeAgent,
    RealtimeModelSendToolOutput,
    RealtimeModelToolCallEvent,
    RealtimeRunner,
)
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by ID."""
    return f"Order {order_id} has shipped."


@pytest.mark.asyncio
async def test_realtime_tool_workflow() -> None:
    tool_call = RealtimeModelToolCallEvent(
        name="lookup_order",
        call_id="call_1",
        arguments='{"order_id":"order_123"}',
    )

    def matches_tool_output(event) -> bool:
        return (
            isinstance(event, RealtimeModelSendToolOutput)
            and event.tool_call.call_id == "call_1"
            and event.output == "Order order_123 has shipped."
        )

    model = ScriptedRealtimeModel(
        [RealtimeStep(expect=matches_tool_output)],
        connect_events=[tool_call],
    )
    agent = RealtimeAgent(
        name="Order assistant",
        tools=[lookup_order],
    )
    runner = RealtimeRunner(
        agent,
        model=model,
        config={"async_tool_calls": False, "tracing_disabled": True},
    )

    async with await runner.run():
        pass

    model.assert_complete()
```

This exercises the real Realtime tool lookup, argument validation, execution, and output routing. It does not prove that a real model will choose the tool.

### Inspect Realtime calls and lifecycle

| Member | Contains |
| --- | --- |
| `connect_calls` | Credential-free, detached connection snapshots |
| `sent_events` | Detached outbound event snapshots in invocation order |
| `remaining_steps` | Expected outbound sends that remain |
| `listeners` | Currently registered listener objects |
| `connected`, `closed`, `close_calls` | Current in-memory lifecycle state |

Connection history records only whether API-key or header fields were supplied; it never stores their values. URL snapshots remove user information, query parameters, and fragments. Mutable event data and settings are detached, while live SDK objects such as tools, handoffs, and playback trackers preserve identity.

Finish with `model.assert_complete()` and let the `RealtimeSession` async context manager close the model. The Python utility intentionally does not provide pending expectation promises, implicit timeouts, or a separate `assert_closed()` helper.

| Error | Structured fields | Meaning |
| --- | --- | --- |
| `UnexpectedRealtimeSend` | `actual`, `expected` | A strict outbound send did not match the next step, or no step remained |
| `UnconsumedRealtimeSteps` | `remaining_steps` | The session ended before using every expected send |
| `RealtimeScriptError` | none | The script was used in an invalid lifecycle state, such as sending while disconnected |

## Voice pipeline recipes

### Test a Voice pipeline

Combine scripted STT and TTS models with `SingleAgentVoiceWorkflow` and an Agent backed by `ScriptedModel` to test the full speech-to-text -> Agent -> text-to-speech pipeline without provider requests.

```python
import numpy as np
import pytest

from agents import Agent
from agents.testing import ScriptedModel, assistant_message
from agents.voice import AudioInput, SingleAgentVoiceWorkflow, VoicePipeline
from agents.voice.testing import (
    ScriptedSTTModel,
    ScriptedTTSModel,
    TTSResult,
    pcm16_samples,
)


@pytest.mark.asyncio
async def test_voice_pipeline() -> None:
    model = ScriptedModel([[assistant_message("Hello there.")]])
    stt = ScriptedSTTModel("hello")
    pcm = pcm16_samples([0, 100, -100, 0])
    tts = ScriptedTTSModel([TTSResult([pcm])])
    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(
            Agent(name="Voice assistant", model=model)
        ),
        stt_model=stt,
        tts_model=tts,
        config={"tracing_disabled": True, "tts_settings": {"buffer_size": 1}},
    )

    result = await pipeline.run(AudioInput(np.zeros(2, dtype=np.int16)))
    events = [event async for event in result.stream()]

    assert events
    assert [call.text for call in tts.calls] == ["Hello there."]
    stt.assert_complete()
    tts.assert_complete()
    model.assert_complete()
```

Use `ScriptedVoiceWorkflow` instead when the pipeline's STT/TTS lifecycle is under test but Agent orchestration is not:

```python
from agents.voice.testing import ScriptedVoiceWorkflow


workflow = ScriptedVoiceWorkflow(
    turns=["Hello there."],
    start="Welcome.",
)
```

The `start` step is consumed by `on_start()`. `VoicePipeline` calls `on_start()` only for `StreamedAudioInput`; a static `AudioInput` run does not consume `start`. Each normal turn records its transcription and consumes one configured result. A string is one fragment; a sequence of strings controls fragment boundaries before text splitting and TTS.

### Test streamed transcription

`ScriptedSTTModel` accepts static `transcriptions` and independently scripted streamed `sessions`. A session may be a `ScriptedTranscriptionSession`, a sequence of transcription turns, an exception, or a single string:

```python
from agents.voice.testing import ScriptedSTTModel, ScriptedTranscriptionSession


session = ScriptedTranscriptionSession(["first turn", "second turn"])
stt = ScriptedSTTModel(sessions=[session])
```

Closing `ScriptedTranscriptionSession` stops iteration and leaves skipped turns for `assert_complete()` to report. `ScriptedTTSModel` similarly consumes one `TTSResult`, byte-chunk sequence, or exception per call.

### Inspect Voice calls

| Component | Recorded history |
| --- | --- |
| `ScriptedSTTModel` | `calls`, `session_calls`, and live `created_sessions` identities |
| `ScriptedTTSModel` | `calls` containing text and detached settings |
| `ScriptedVoiceWorkflow` | `transcriptions` in turn order |

Static audio buffers and mutable settings are snapshotted at invocation time. A `StreamedAudioInput` and created transcription-session objects keep their live identity because the pipeline continues to use them.

| Error | Structured fields | Meaning |
| --- | --- | --- |
| `UnexpectedVoiceCall` | `operation` | A static transcription, streamed session, TTS call, workflow start, or workflow turn had no configured step |
| `UnconsumedVoiceSteps` | `remaining_steps` | One or more configured Voice steps remain |

Call `assert_complete()` on every scripted Voice component that the test configures. `ScriptedSTTModel.assert_complete()` also checks turns in the transcription sessions that it created.

## Choose the correct boundary

Use `ScriptedModel` when a test should exercise the SDK run loop, tools, handoffs, guardrails, sessions, retries, or normalized streaming without depending on a model provider.

Use `scripted_sandbox_session()` with `ScriptedModel` when a test should exercise `SandboxAgent` capabilities and orchestration without starting a sandbox provider. Keep provider creation, process execution, filesystem fidelity, persistence, resource limits, and isolation checks in integration tests against the real sandbox provider.

Use `ScriptedRealtimeModel` when a test should exercise `RealtimeSession` behavior or `RealtimeAgent` tool and handoff orchestration without opening a WebSocket connection. Keep raw Realtime client/server events, authentication, network recovery, and audio transport behavior on the real transport or in an integration environment. Realtime API sessions keep a connection open while the client sends input and receives events, so those network and protocol concerns belong below the normalized model boundary. See the [OpenAI Realtime API guide](https://developers.openai.com/api/docs/guides/realtime) for production connection architectures.

Use the Voice testing components when a test should exercise STT/TTS ordering, streamed transcription cleanup, workflow fragment delivery, or complete Voice pipeline composition without speech providers. Use real audio models and representative audio when transcription quality, generated speech, encoding compatibility, latency, or playback is the subject of the test.

Do not use these utilities to test Responses API or Chat Completions request serialization, authentication headers, provider defaults, HTTP payloads, provider stream chunks, Realtime wire frames, or provider-specific lifecycle behavior. Keep the real adapter and replace or control its network boundary for those tests. With `openai` v3, OpenAI adapter tests should use `httpx2` request, response, transport, and exception types; legacy `httpx` is not a core dependency of the Agents SDK.

## Final checklist

- Script only interactions owned by the normalized model, Sandbox session, Realtime model, or Voice pipeline boundary.
- Assert important public request or call fields instead of private runner state.
- Prefer fixed response steps; use responders only for request-dependent behavior.
- Prefer automatic model streaming; use exact streams only when event-level behavior matters.
- End each scripted component test with its `assert_complete()` method.
- Use async context managers for Realtime and Sandbox lifecycle cleanup when the surrounding test owns that lifecycle.
- Assert structured error fields instead of parsing human-readable messages.
- Keep provider wire tests on real adapters with controlled network transports.

## Scope and current limitations

The testing modules deliberately do not provide:

- Convenience builders for every normalized model output item. Use `assistant_message()` and `function_call()` for common cases, and pass other normalized items directly.
- A provider-protocol simulator. Exact model streams use normalized SDK events rather than Responses API or Chat Completions wire chunks.
- A high-level simulated Realtime server. Tests explicitly match normalized outbound sends and emit the normalized inbound events required by the scenario.
- Unordered Sandbox or Realtime expectations. Both utilities consume expected steps in one global order.
- Test-runner-specific matchers, fixtures, implicit timeouts, or automatic teardown.
- Reset APIs. `ScriptedModel` supports `enqueue()` and `extend()` for an incremental script, but create a new scripted component for an independent scenario.

Use a custom implementation of the corresponding public interface when a test requires malformed streams, controlled suspension or concurrency, exact cancellation, or a lifecycle boundary that the scripted utilities cannot preserve. Document that specialized boundary in the test.

## API reference

- [`agents.testing`](ref/testing.md)
- [`agents.realtime.testing`](ref/realtime/testing.md)
- [`agents.voice.testing`](ref/voice/testing.md)
