# Tracing

The Agents SDK includes built-in tracing, collecting a comprehensive record of events during an agent run: LLM generations, tool calls, handoffs, guardrails, and even custom events that occur. Using the [Traces dashboard](https://platform.openai.com/traces), you can debug, visualize, and monitor your workflows during development and in production.

!!!note

    Tracing is enabled by default. You can disable it in three common ways:

    1. You can globally disable tracing by setting the env var `OPENAI_AGENTS_DISABLE_TRACING=1`
    2. You can globally disable tracing in code with [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
    3. You can disable tracing for a single run by setting [`agents.run.RunConfig.tracing_disabled`][] to `True`

***Tracing is unavailable for organizations that use OpenAI's APIs under a Zero Data Retention (ZDR) policy.***

## Traces and spans

-   **Traces** represent a single end-to-end operation of a "workflow". They're composed of Spans. Traces have the following properties:
    -   `workflow_name`: This is the name of the logical workflow or app. For example "Code generation" or "Customer service".
    -   `trace_id`: A unique ID for the trace. Automatically generated if you don't pass one. Must have the format `trace_<32_alphanumeric>`.
    -   `group_id`: Optional group ID, to link multiple traces from the same conversation. For example, you might use a chat thread ID.
    -   `disabled`: If True, the trace will not be recorded.
    -   `metadata`: Optional metadata for the trace.
-   **Spans** represent operations that have a start and end time. Spans have:
    -   `started_at` and `ended_at` timestamps.
    -   `trace_id`, to represent the trace they belong to
    -   `parent_id`, which points to the parent Span of this Span (if any)
    -   `span_data`, which is information about the Span. For example, `AgentSpanData` contains information about the Agent, `GenerationSpanData` contains information about the LLM generation, etc.

## Default tracing

By default, the SDK traces the following:

-   The entire `Runner.{run, run_sync, run_streamed}()` is wrapped in a `trace()`.
-   Each runner invocation is wrapped in a `task_span()`.
-   Each model turn is wrapped in a `turn_span()`.
-   Each time an agent runs, it is wrapped in `agent_span()`
-   The Responses and embedded Codex providers use one `response_span()` per logical model call, including retries. Chat Completions uses `generation_span()`.
-   Function tool calls are each wrapped in `function_span()`
-   Guardrails are wrapped in `guardrail_span()`
-   Handoffs are wrapped in `handoff_span()`
-   Audio inputs (speech-to-text) are wrapped in a `transcription_span()`
-   Audio outputs (text-to-speech) are wrapped in a `speech_span()`
-   The SDK may parent related audio spans under a `speech_group_span()`

By default, the trace name is the literal string `Agent workflow`. You can set this name if you use `trace`, or you can configure the name and other properties with the [`RunConfig`][agents.run.RunConfig].

If you want a more compact hierarchy, disable the automatic task and turn spans for a run. Agent, generation, function, guardrail, handoff, and custom spans are still recorded.

```python
from agents import RunConfig, Runner

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(tracing={"include_task_and_turn_spans": False}),
)
```

In addition, you can set up [custom trace processors](#custom-tracing-processors) to push traces to other destinations (as a replacement, or secondary destination).

## Long-running workers and immediate exports

The default [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] exports traces in the background every few seconds, or sooner when the in-memory queue reaches its size trigger, and also performs a final flush when the process exits. In long-running workers such as Celery, RQ, Dramatiq, or FastAPI background tasks, this means traces are usually exported automatically without any extra code, but they may not appear in the Traces dashboard immediately after each job finishes.

If you need an immediate delivery guarantee at the end of a unit of work, call [`flush_traces()`][agents.tracing.flush_traces] after the trace context exits.

```python
from agents import Runner, flush_traces, trace


@celery_app.task
def run_agent_task(prompt: str):
    try:
        with trace("celery_task"):
            result = Runner.run_sync(agent, prompt)
        return result.final_output
    finally:
        flush_traces()
```

```python
from fastapi import BackgroundTasks, FastAPI
from agents import Runner, flush_traces, trace

app = FastAPI()


def process_in_background(prompt: str) -> None:
    try:
        with trace("background_job"):
            Runner.run_sync(agent, prompt)
    finally:
        flush_traces()


@app.post("/run")
async def run(prompt: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_in_background, prompt)
    return {"status": "queued"}
```

[`flush_traces()`][agents.tracing.flush_traces] blocks until currently buffered traces and spans are exported, so call it after `trace()` closes to avoid flushing a partially built trace. You can skip this call when the default export latency is acceptable.

Disabling tracing prevents the default provider from creating new traces and spans, but it does not discard data that its processors already buffered. [`flush_traces()`][agents.tracing.flush_traces] continues to flush that buffered data after tracing has been disabled through `set_tracing_disabled(True)` or `OPENAI_AGENTS_DISABLE_TRACING=1`.

## Higher level traces

Sometimes, you might want multiple calls to `run()` to be part of a single trace. You can do this by wrapping the entire code in a `trace()`.

```python
from agents import Agent, Runner, trace


async def main():
    agent = Agent(name="Joke generator", instructions="Tell funny jokes.")

    with trace("Joke workflow"):  # (1)!
        first_result = await Runner.run(agent, "Tell me a joke")
        second_result = await Runner.run(agent, f"Rate this joke: {first_result.final_output}")
        print(f"Joke: {first_result.final_output}")
        print(f"Rating: {second_result.final_output}")
```

1. Because the two calls to `Runner.run` are wrapped in a `with trace()`, both runs become part of one overall trace instead of each creating a separate trace.

## Creating traces

You can use the [`trace()`][agents.tracing.trace] function to create a trace. Traces need to be started and finished. You have two options to do so:

1. **Recommended**: use the trace as a context manager, i.e. `with trace(...) as my_trace`. This will automatically start and end the trace at the right time.
2. You can also manually call [`trace.start()`][agents.tracing.Trace.start] and [`trace.finish()`][agents.tracing.Trace.finish].

The current trace is tracked via a Python [`contextvar`](https://docs.python.org/3/library/contextvars.html). This means that it works with concurrency automatically. If you manually start and finish a trace, pass `mark_as_current` to `start()` and `reset_current` to `finish()` to update the current trace.

## Creating spans

You can use the various [`*_span()`][agents.tracing.create] methods to create a span. In general, you don't need to manually create spans. A [`custom_span()`][agents.tracing.custom_span] function is available for tracking custom span information.

Spans are automatically part of the current trace, and are nested under the nearest current span, which is tracked via a Python [`contextvar`](https://docs.python.org/3/library/contextvars.html).

## Sensitive data

Certain spans may capture potentially sensitive data.

The `generation_span()` stores the inputs/outputs of the LLM generation, and `function_span()` stores the inputs/outputs of function calls. These may contain sensitive data, so you can disable capturing that data via [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data].

With `operation_detail` omitted, a function span that pauses for approval does not store the SDK's internal result wrapper as tool output. If the application rejects the call with a custom rejection message, the function span stores that message as output and error text only when `trace_include_sensitive_data` is `True`. When the setting is `False`, the span omits the output and uses the generic error text `Tool execution rejected`. For detailed tracing, see [Payload capture](#payload-capture).

Similarly, Audio spans include base64-encoded PCM data for input and output audio by default. You can disable capturing this audio data by configuring [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data].

By default, `trace_include_sensitive_data` is `True`. You can set the default without code by exporting the `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` environment variable to `true/1` or `false/0` before running your app.

When `trace_include_sensitive_data` is `False`, Responses model spans omit the request input and response output. For calls to an official OpenAI endpoint, the spans still include the Responses API `response_id` as correlation metadata. The SDK omits that identifier from redacted spans for custom endpoints.

### Runner text redaction

Use [`RunConfig.redact_text`][agents.run.RunConfig.redact_text] with either provider to remove known secrets from run inputs, model replies, and SDK tool arguments and results. Replacements change what the model and Python tools receive. Supply a synchronous function that returns the same string for the same input and leaves sanitized text unchanged. Never log its input.

Install a [preview build with Runner text redaction](models/codex.md#install-a-github-prerelease-wheel). Run this example offline to check a sample policy. The scripted model deliberately returns the same test value that the caller supplied.

```python
import asyncio

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message


async def main():
    private_value = "example-private-ticket"

    def redact(text: str) -> str:
        return text.replace(private_value, "[redacted]")

    agent = Agent(
        name="Support",
        model=ScriptedModel([[assistant_message(f"Ticket: {private_value}")]]),
    )
    result = await Runner.run(
        agent,
        f"Check ticket {private_value}",
        run_config=RunConfig(redact_text=redact, tracing_disabled=True),
    )
    assert private_value not in result.final_output
    print(result.final_output)  # Ticket: [redacted]


asyncio.run(main())
```

With the callback active, the Runner limits automatic traces to metadata, even in content mode. It mutes the `openai.agents` and `openai.agents.tracing` loggers and withholds raw stream events, including text deltas. You can still consume completed run items. If the Runner can't safely inspect a value, validate a checkpoint, or preserve the action submitted for approval, it stops the run. Error messages may omit details.

For a worker that saves or resumes runs, take these steps.

1. Upgrade readers before writers. This preview writes `RunState` schema 1.24 even without redaction. Protected `ResumableSession` checkpoints use version 2. Older workers can't read these formats.
2. Pass the same redaction policy to every nested and resumed run. The SDK records the requirement in the checkpoint and keeps the function in memory only. Resume a restored protected state with the callback before exporting it again.
3. Use a session or replay the messages. Protected runs reject caller-supplied `conversation_id` and `previous_response_id` because the Runner can't inspect the remote history. Allow only one writer at a time per session. Each append checks available history for text split across turns.

Test the policy against your own data. Keep secrets out of static instructions, identifiers, configuration, and application metadata. The callback covers only the text your function recognizes. It can't scrub existing records, binary media, application or provider logs, or operations inside Codex.

## Custom tracing processors

The high level architecture for tracing is:

-   At initialization, we create a global [`TraceProvider`][agents.tracing.provider.TraceProvider], which is responsible for creating traces.
-   We configure the `TraceProvider` with a [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] that sends traces/spans in batches to a [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter], which exports the spans and traces to the OpenAI backend in batches.

To customize this default setup, to send traces to alternative or additional backends or modifying exporter behavior, you have two options:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] lets you add an **additional** trace processor that will receive traces and spans as they are ready. This lets you do your own processing in addition to sending traces to OpenAI's backend.
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] lets you **replace** the default processors with your own trace processors. This means traces will not be sent to the OpenAI backend unless you include a `TracingProcessor` that does so.

### Redaction before export

Trace processors are independent observers. The default provider catches a processor's callback exceptions and continues calling the other registered processors. A redaction processor registered before an exporter therefore does not prevent that exporter from receiving data if redaction fails. Adding a processor with `add_trace_processor()` also leaves the default OpenAI exporter registered.

When export depends on successful redaction, keep redaction and delivery inside the same application-owned exporter. Use `set_trace_processors()` to replace the default processors with a `BatchTraceProcessor` configured with that exporter. The exporter should copy the serialized payloads, redact the copies, and pass only the redacted results to the destination. If serialization, copying, or redaction fails, discard the batch before invoking the destination. Log a fixed failure message without the payload, exception text, or traceback.

The [trace redaction example](https://github.com/openai/openai-agents-python/blob/main/examples/basic/trace_redaction.py) demonstrates this composition using existing tracing APIs. The example prints only event categories and trace/span linkage IDs to the local console; it makes no API calls. Its allowlist omits names, metadata, errors, and span data. Caller-supplied IDs must contain no sensitive information, or the application must map those IDs to safe values. This diagnostic output is not the OpenAI tracing ingest schema; an application sending data to a backend must supply a redaction policy and destination compatible with that backend.

The redactor and destination are trusted application code. They must not independently log or send the original data. The batch processor can invoke the exporter during background export, explicit flush, or shutdown, so callbacks must be safe to use from those execution contexts. A failed batch is dropped; subsequent batches can still be exported. Replacement affects future processor callbacks and does not erase data already buffered by a previously registered processor. Configure the replacement before creating traces or running agents.

## OpenTelemetry

Use [`OpenTelemetryTracingProcessor`](ref/extensions/tracing/opentelemetry.md) to send SDK spans to your application's OpenTelemetry (OTel) tracer provider. Register it once for both the default Responses provider and the [embedded Codex provider](models/codex.md#operation-tracing).

For the example below, [download a preview wheel with operation tracing](models/codex.md#install-a-github-prerelease-wheel). The published `v1.0.0-alpha.1` release predates this integration. In a fresh virtual environment, replace `WHEEL_FILE` with the local path to the matching `.whl` file. Keep the `[opentelemetry]` extra after the filename.

```sh
python -m pip install --only-binary=:all: "WHEEL_FILE[opentelemetry]" "opentelemetry-sdk>=1.45,<2"
python -c "from agents.extensions.tracing import OpenTelemetryTracingProcessor"
```

### One setup for both providers

Save this script as `otel_agent.py`. Set `OPENAI_API_KEY` for Responses. For Codex, set `CODEX_API_KEY` or [use your saved login](models/codex.md#authenticate). Both runs make model requests. By default, the example prints trace metadata and the agent's answer to the console.

```python
import argparse
import asyncio
import os

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from agents import Agent, ModelSettings, RunConfig, Runner, set_trace_processors
from agents.decorators import tool
from agents.extensions.tracing import OpenTelemetryTracingProcessor
from agents.models.codex import CodexProvider

parser = argparse.ArgumentParser()
parser.add_argument("--codex", action="store_true")
parser.add_argument("--model")
parser.add_argument("--content", action="store_true")
args = parser.parse_args()

tracer_provider = TracerProvider()
tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
tracer = tracer_provider.get_tracer("example")
set_trace_processors([OpenTelemetryTracingProcessor(tracer_provider=tracer_provider)])


@tool
def square(value: int) -> int:
    """Return the square of an integer."""
    with tracer.start_as_current_span("square.calculate"):
        return value * value


agent = Agent(
    name="Calculator",
    instructions="Use square once, then reply with the result.",
    tools=[square],
    model_settings=ModelSettings(tool_choice="required"),
)
config = RunConfig(
    model=args.model,
    tracing={"operation_detail": "content" if args.content else "metadata"},
    trace_include_sensitive_data=args.content,
)
if args.codex:
    config.model_provider = CodexProvider(api_key=os.environ.get("CODEX_API_KEY"))


async def main():
    try:
        with tracer.start_as_current_span("calculator.request"):
            result = await Runner.run(agent, "What is the square of 12?", run_config=config)
        print(result.final_output)
    finally:
        await config.model_provider.aclose()


try:
    asyncio.run(main())
finally:
    tracer_provider.shutdown()
```

Run the script with each provider. Use `--model` to select a model that supports tool calls.

```sh
python otel_agent.py
python otel_agent.py --codex
```

Check for an answer of `144` in either mode. In the console trace, look for `square.calculate` beneath `execute_tool square`.

```text
calculator.request
  invoke_runner Agent workflow
    invoke_agent Calculator
      openai.agents.turn
        chat <model>
        execute_tool square
          square.calculate
      openai.agents.turn
        chat <model>
```

### Send spans to a collector

If your application already configures an OTel tracer provider, pass it to `OpenTelemetryTracingProcessor`. Use `set_trace_processors()` for OTel alone or `add_trace_processor()` to keep the existing SDK processors, including the default OpenAI exporter. Configure sampling, batching, and delivery on the OTel provider. In a long-running worker, call `tracer_provider.force_flush()` when needed and `tracer_provider.shutdown()` when the worker exits. The SDK's `flush_traces()` does not flush the OTel provider.

To send the example's spans over OTLP/HTTP, install the [Python exporter](https://opentelemetry.io/docs/languages/python/exporters/).

```sh
python -m pip install "opentelemetry-exporter-otlp-proto-http>=1.45,<2"
```

Set [`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`](https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/) to the collector's full trace URL, for example `http://127.0.0.1:4318/v1/traces` for a local collector. In `otel_agent.py`, replace the console exporter import and the `add_span_processor(...)` line with these imports and registration.

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor

tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
```

Run the script again and look for `calculator.request` in your tracing backend. In a worker, call `Runner.run()` or start and consume `Runner.run_streamed()` under the current request span. Across processes, [extract the caller's trace context](https://opentelemetry.io/docs/languages/python/propagation/) before starting that span. Without a parent, the worker starts a new trace. The processor does not inject trace headers into model gateway requests.

### Read the worker trace

Group spans by `gen_ai.operation.name`. By default, each step of the Runner's agent loop has an `openai.agents.turn` span. One Codex step can contain several `chat` and `execute_tool` spans. A Python callback has one execution span. Instrumented calls inside the callback appear beneath it.

For approvals, look for `openai.agents.tool_approval` with `openai.agents.operation.outcome` set to `paused` or `rejected`. An approved tool's execution time excludes the wait for a decision. On the replacement worker, start the resumed run under its current caller. If the tool has a call ID, match the decision and execution by `gen_ai.tool.call.id` within the same job.

Use `gen_ai.request.model` and `gen_ai.response.model` when available. Model spans report token totals as `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`. Nonzero counts can also appear as `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens`, and `gen_ai.usage.reasoning.output_tokens`. For billed cost, consult your gateway or provider.

Each logical model call includes the time spent waiting between retries. After a tool result, the next inference starts a new model span. Use the following fields to inspect retries observed by the SDK.

| Retry field or event | Role | Limit |
| --- | --- | --- |
| `span.span_data.operation.retry_timestamps` | ISO 8601 retry observation times | Up to 128 times per model span |
| OTel `openai.agents.model.retry` | One event per retained time, without request content | Subject to the tracer provider's event limits |
| `openai.agents.model.attempt` | Attempt number on the event | Starts at `2` for the first retry |

For Runner retries, the timestamp marks the start of the next attempt. If a transport reports only a retry count, those events share the time the SDK received the count. Retries internal to a gateway may be invisible to the worker.

### Payload capture

For Responses and Codex, `RunConfig.tracing["operation_detail"]` controls payload capture. The same calls, approvals, and retries appear in every mode. Set `trace_include_sensitive_data=False` to suppress model and tool payloads even with `"content"` selected.

The table applies when `redact_text` is unset. With [Runner text redaction](#runner-text-redaction), automatic traces contain only metadata in every row.

| `operation_detail` | `trace_include_sensitive_data` | SDK model and tool payload fields | OTel payload attributes |
| --- | --- | --- | --- |
| Omitted | `True`, the default | Legacy model `.input` and `.response`, SDK function input/output, native tool metadata | None |
| Omitted | `False` | None | None |
| `"metadata"` | Either value | None | None |
| `"content"` | `True` | Bounded model and tool content | Bounded permitted content |
| `"content"` | `False` | None | None |

Both explicit detail settings omit inputs and outputs from approval decision spans.

!!! warning "Trace settings do not redact stored data"

    Use `"metadata"` on every run, including nested and resumed runs, to keep automatic model and tool payloads out of SDK processors. With detail omitted, other processors can receive file contents even when OTel exports only metadata. Keep secrets out of span names, IDs, and custom data.

    Protect logs, conversation history, and saved approvals separately. A session checkpoint can contain a tool's arguments even when its trace contains only metadata. In content mode, the model span can capture the original arguments before `prepare_tool_input` rewrites them for an SDK function tool.

Pass `--content` only if every registered processor and destination may receive the text. Look for `gen_ai.input.messages`, `gen_ai.system_instructions`, `gen_ai.output.messages`, `gen_ai.tool.call.arguments`, and `gen_ai.tool.call.result`. On a continuation, the submitted messages may contain only new items. Use `openai.response.previous_response_id`, when available, to find the earlier call. See [Sensitive data](#sensitive-data) for response ID handling.

In content mode, each operation has separate 32 KiB limits for input and output, measured as UTF-8 JSON. Look for truncation and omission markers for large values, binary media, hidden reasoning, and known credential fields. For example, an oversized JSON string can produce `reason="capture_budget"` in Python. Free text can still contain secrets.

For the SDK fields available on each Codex call, see [Codex operation tracing](models/codex.md#operation-tracing).

## Tracing with non-OpenAI models

When using non-OpenAI models, you can provide an OpenAI API key to the tracing exporter to enable free tracing in the OpenAI Traces dashboard without disabling tracing. See the [Third-party adapters](models/index.md#third-party-adapters) section in the Models guide for adapter selection and setup caveats.

```python
import os
from agents import set_tracing_export_api_key, Agent
from agents.extensions.models.any_llm_model import AnyLLMModel

tracing_api_key = os.environ["OPENAI_API_KEY"]
set_tracing_export_api_key(tracing_api_key)

model = AnyLLMModel(
    model="your-provider/your-model-name",
    api_key="your-api-key",
)

agent = Agent(
    name="Assistant",
    model=model,
)
```

If you only need a different tracing key for a single run, pass it via `RunConfig` instead of changing the global exporter.

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## Additional notes
- View free traces at OpenAI Traces dashboard.


## Ecosystem integrations

The following community and vendor integrations support the tracing API surface of the OpenAI Agents SDK.

The integration maintainers provide support for their integrations. Inclusion in this list does not constitute an OpenAI endorsement or security certification. To request a new listing, follow the [integration listing criteria](https://github.com/openai/openai-agents-python/blob/main/CONTRIBUTING.md#tracing-integration-listings).

### External tracing processors list

-   [Weights & Biases](https://docs.wandb.ai/weave/guides/integrations/agents/openai-agents-sdk)
-   [Arize Phoenix](https://arize.com/docs/phoenix/integrations/llm-providers/openai/openai-agents-sdk-tracing)
-   [Future AGI](https://docs.futureagi.com/docs/tracing/auto/openai_agents/)
-   [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/integrations/openai-agent)
-   [Braintrust](https://www.braintrust.dev/docs/integrations/agent-frameworks/openai-agents-sdk)
-   [Pydantic Logfire](https://pydantic.dev/docs/logfire/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/features/tracing#agent-frameworks)
-   [Respan](https://www.respan.ai/docs/integrations/openai-agents-sdk)
-   [LangSmith](https://docs.langchain.com/langsmith/trace-openai)
-   [Maxim AI](https://www.getmaxim.ai/docs/sdk/python/integrations/openai/agents-sdk)
-   [Comet Opik](https://www.comet.com/docs/opik/integrations/openai_agents)
-   [Langfuse](https://langfuse.com/integrations/frameworks/openai-agents)
-   [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-   [Okahu-Monocle](https://github.com/monocle2ai/monocle)
-   [Galileo](https://docs.galileo.ai/how-to-guides/third-party-integrations/openai-agent-integration)
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk/)
-   [Agenta](https://agenta.ai/docs/observability/integrations/openai-agents)
-   [PostHog](https://posthog.com/docs/ai-observability/installation/openai-agents)
-   [Traccia](https://traccia.ai/docs/integrations/openai-agents/)
-   [PromptLayer](https://docs.promptlayer.com/features/observability/traces/integrations#openai-agents-sdk)
-   [HoneyHive](https://docs.honeyhive.ai/v2/integrations/openai-agents)
-   [Asqav](https://www.asqav.com/docs/integrations#openai-agents)
-   [Datadog](https://docs.datadoghq.com/llm_observability/instrumentation/auto_instrumentation/?tab=python#openai-agents)
-   [Latitude](https://docs.latitude.so/telemetry/frameworks/openai-agents)
-   [DProvenanceKit](https://dprovenance.dev/openai-agents/)
-   [Tuning Engines](https://github.com/cerebrixos-org/tuning-engines-cli/tree/main/packages/tuning-agents#openai-agents-sdk)
-   [Laminar](https://laminar.sh/docs/tracing/integrations/openai-agents-sdk)
-   [Noveum](https://github.com/Noveum/noveum-trace/blob/main/docs/OPENAI_AGENTS_INTEGRATION.md)
