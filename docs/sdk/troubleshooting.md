# Troubleshoot and operate an agent

Start with the symptom below. Preserve the original exception and inspect the failed operation before retrying. A failed request or timeout does not prove that earlier tools had no effects.

| Symptom | What to check |
| --- | --- |
| Installation fails, a wheel is unsupported, or GitHub returns 404 | Follow the [installation checks](installation.md#if-installation-fails). Confirm the guide and installed wheel describe the same version. |
| `get_result` or another documented method is missing | Check the [installation guide](installation.md) and use documentation for your installed preview release. Run `python -m pip show openai-agents` with your application's interpreter to check its version. |
| A notebook reports that an event loop is already running | Use `await agent.run(...)`. Reserve `run_sync()` and the scripts' `asyncio.run(main())` entry point for code outside an existing event loop. |
| A new prompt raises `SessionBusyError` | Wait for the current Turn, or [queue or steer](sessions.md#conversations-events-and-state). Retrying `submit()` without checking the current work can duplicate input. |
| A one-shot run raises `InteractionRequired` | Inspect its optional checkpoint and follow [manual continuation](tools.md#continue-a-one-shot-manual-request). Do not submit the original prompt again as a substitute for the pending reply. |
| A Python tool raises an exception | Inspect the [tool error contract](tools.md#return-text-images-and-tool-errors) and any completed effects before submitting new work. |
| The agent is waiting for your application | Check for [manual results or approvals](tools.md#answer-interactions-during-a-one-shot-run). Reply to the exact live request. |
| Streamed text is incomplete or an `EventGap` appears | The observation stream can drop records. Use `turn.get_result()` and its final output; see [streaming](sessions.md#stream-observations). |
| A result fails validation | Catch [`OutputValidationError`](configuration.md#structured-output). A new submission can repeat completed tool effects; validation does not undo them. |
| A provider or native operation fails | Inspect available `NativeError` metadata and [effect status](sessions.md#failures-and-recovery). Missing metadata remains unknown. |
| Portable restore reports an unsupported Codex revision or `checkpoint core revision differs` | Use the SDK wheel with the same pinned Codex revision that saved the checkpoint. Editing `codex_revision` does not migrate the state. See [checkpoint compatibility](sessions.md#compact-save-and-restore). |
| Local resume reports an unknown tool outcome or an active descendant | The saved conversation cannot reopen in that state. Preserve its profile and reconcile external effects using application records. Repeating `resume` does not repair missing outcomes; see [local recovery limits](sessions.md#reopen-a-conversation-from-local-storage). |
| Opening fails and cleanup also fails | Follow [provider cleanup recovery](configuration.md#recover-a-failed-managed-environment-opening) before discarding the owning event loop. |
| A checkpoint cannot be saved or restored | Check [capture readiness and supported boundaries](sessions.md#check-support-and-capture-readiness), matching tools and instructions, and current permissions. |
| A scripted test uses an unexpected request or leaves responses unused | Each script entry is one model response. Check [request snapshots and exact exhaustion](testing.md). |

## Bound a wait and stop owned work

Timing out `turn.get_result()` stops waiting for the result. The Turn can continue running. When your application owns the entire Session, let the timeout leave its async context. Session exit stops its work and waits for it to finish before raising the original exception:

```python
import asyncio

from agents import Agent


async def main() -> None:
    agent = Agent(instructions="Answer briefly.")
    async with agent.session() as session:
        turn = await session.submit("Explain how a rainbow forms.")
        result = await asyncio.wait_for(turn.get_result(), timeout=30)
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

This limits the wait for the result to 30 seconds. Cancellation and cleanup can take longer. Catch `asyncio.TimeoutError` outside the `async with` block if your application needs to handle it. If cleanup also fails, the timeout remains the primary exception and the cleanup failure is retained in its exception context.

Python cannot forcibly stop a synchronous tool already running in a worker. Session exit waits for its work to finish and does not undo completed effects. If you own only a root Turn, use `await turn.cancel(mode="force")`. Independent children may continue. See [interruption](sessions.md#interrupt-work) and [Python tool cancellation](tools.md#python-functions).

## Read usage without double counting

After a successful run, inspect the optional `Completed.usage` snapshot:

```python
# result is the Completed value returned by agent.run() or turn.get_result().
usage = result.usage
if usage is None:
    print("No usage was reported.")
else:
    print("Thread total:", usage.total_token_usage.total_tokens)
    print("Latest reported response:", usage.last_token_usage.total_tokens)
    print("Model context window:", usage.model_context_window)
```

`total_token_usage` is cumulative for the native thread, including restored history. Do not sum successive snapshots as separate costs. `last_token_usage` may describe only one response from a Turn that needed several responses. A snapshot can repeat if no new usage was reported. These counts are not a bill.

Both counts expose `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` and `total_tokens`. Cached and reasoning tokens are already included in the totals. Do not add them again. `model_context_window` can be `None`. Root usage excludes child threads. [Prompted-output repairs](configuration.md#structured-output) have separate records. See the [usage types](reference.md#tokenusageinfo-and-tokenusage).

For a live display, `UsageUpdated` from `agents.observation` carries an optional `usage` snapshot. Replace the displayed snapshot rather than adding each event's counts. The observation stream can drop records. Read the final result after completion.

## Plan the application lifecycle

The SDK runs inside your application, which manages its workers, storage and credentials. Saving a checkpoint does not provide a hosted recovery service.

| Application decision | Supported path |
| --- | --- |
| When to initialize diagnostics | Configure [telemetry](configuration.md#telemetry) before the first Session opens. |
| Which work belongs to a conversation | Own each [Session](sessions.md) explicitly and choose queueing or steering when submissions overlap. |
| Where history survives a restart | Choose [portable checkpoints or a local profile](sessions.md#choose-how-to-store-a-conversation). Retain the required state or conversation ID in application storage. |
| What tools may do | Supply current [tools and execution permissions](tools.md). Restored history does not supply current credentials or grant new authority. |
| Whether work can be retried | Inspect [effect metadata and history](sessions.md#failures-and-recovery), together with your application's records of external effects. |
| How to shut down | Close owned Sessions, then close any [explicit providers](configuration.md) after their Sessions are closed. Shut down [telemetry](configuration.md#telemetry) after all Sessions close. |

## Prepare a useful failure report

Record the installed SDK version, Python version, operating system, operation, and a small reproduction with synthetic inputs. Include whether it fails with [scripted responses](testing.md) or only with the live provider. For `NativeError`, record the available `status_code`, `code`, `request_id`, `category` and `effects` only when your data policy permits them.

Exception messages, request snapshots, prompts, tool results and checkpoints can contain private data. Inspect and redact a report before sending it to [GitHub issues](https://github.com/openai/openai-agents-python/issues). Never include credentials. Use [private security reporting](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/SECURITY.md) for vulnerabilities.

For ongoing diagnostics, configure [telemetry](configuration.md#telemetry) before the first Session opens. Close Sessions before shutting telemetry down. Select the signals and content your application permits the collector to receive.

## Diagnose a trace with history and application receipts

Run the synthetic local [trace walkthrough](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/observability/trace_walkthrough.py) with `python -m examples.observability.trace_walkthrough --scenario tool --view --output synthetic-trace.json`. Other scenarios are `tool-error`, `validation` and `provider`. The example starts a local OTLP JSON receiver. It prints a receipt summary and local viewer URL. It needs no collector account or real model key. Keep receiver output synthetic.

Match the application's `example.run_id`, available span `traceId`, Session identity and available `conversation.id` fields. Read the application outcome beside telemetry and the worked [history reader](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/observability/history_reader.py). The reader joins tool calls and results by turn and call identity. It leaves missing timestamps and usage as missing values.

| Evidence | What it can show | What it does not prove |
| --- | --- | --- |
| Application receipt around `get_result()` | Settled output, raised error and application effect records | Native logs need not contain a separate record for a Python callback exception or output-validation failure. |
| `session.turns()` | Recorded turn status, failure, and available usage and timestamps | Missing usage or timestamps are not zero. Recorded turn usage differs from cumulative live thread snapshots. |
| `session.items()` messages and reasoning | Supported user and assistant text, user inline images, and retained reasoning summaries | The SDK can omit developer and system messages, unsupported content and records without usable identities. This is not the complete UI conversation or hidden reasoning. |
| Recorded function calls and results | Normalized function and custom calls and supported native tool records, joined by turn and call identity | A completed output record with `error=None` can contain a returned tool error. Inspect the actual output and application outcome. Cancellation can leave incomplete pairs. |
| Recorded web search | Recorded query, action and text fields | These fields cannot reconstruct authoritative source records or citation spans. |
| Collector export | Received native spans and logs and configured telemetry fields | A received export does not prove that telemetry covers all Python failures. It does not undo earlier application effects or define how long the collector keeps data. |

A tool may return an error value and let the turn complete, or raise an exception that fails the turn. Final-output validation can fail after a tool effect. Diagnose those cases separately. Closing the Session does not delete data already exported to the collector.
