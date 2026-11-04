# Troubleshoot and operate an agent

Start with the symptom below. Preserve the original exception and inspect the failed operation before retrying. A failed request or timeout does not prove that earlier tools had no effects.

## Install and start

| Symptom | What to check |
| --- | --- |
| Installation fails, a wheel is unsupported, or GitHub returns 404 | Check [installation](installation.md#if-installation-fails) and match the guide version to the wheel. |
| `get_result` or another documented method is missing | Check the version with your application's `python -m pip show openai-agents`. Use the matching [preview guide](installation.md). |
| A notebook reports that an event loop is already running | Use `await agent.run(...)`. Use `run_sync()` or a script's `asyncio.run(main())` only outside a running event loop. |

## Run and observe work

| Symptom | What to check |
| --- | --- |
| A new prompt raises `SessionBusyError` | Wait for the Turn, or [queue or steer](sessions.md#conversations-events-and-state). Retrying `submit()` can duplicate input. |
| A one-shot run raises `InteractionRequired` | Inspect its optional checkpoint and [continue the pending request](tools.md#continue-a-one-shot-manual-request). Do not resubmit the original prompt. |
| A Python tool raises an exception | Inspect the [tool error contract](tools.md#return-text-images-and-tool-errors) and any completed effects before submitting new work. |
| The agent is waiting for your application | Check for [manual results or approvals](tools.md#answer-interactions-during-a-one-shot-run). Reply to the exact live request. |
| Streamed text is incomplete or an `EventGap` appears | The observation stream can drop records. Use `turn.get_result()` and its final output; see [streaming](sessions.md#stream-observations). |
| A result fails validation | Catch [`OutputValidationError`](configuration.md#structured-output). A new submission can repeat completed tool effects; validation does not undo them. |
| A provider or native operation fails | Inspect available `NativeError` metadata and [effect status](sessions.md#failures-and-recovery). Missing metadata remains unknown. |
| A scripted test uses an unexpected request or leaves responses unused | Each script entry is one model response. Check [request snapshots and exact exhaustion](testing.md). |

## Restore and clean up

| Symptom | What to check |
| --- | --- |
| Portable restore reports an unsupported Codex revision or `checkpoint core revision differs` | Use a wheel with the saving SDK's pinned Codex revision. Editing `codex_revision` does not migrate state. See [compatibility](sessions.md#compact-save-and-restore). |
| Local resume reports an unknown tool outcome or an active descendant | Keep the profile. Check application records and supply confirmed outcomes for unresolved root calls. If a child still blocks reopening, keep the conversation closed. See [local recovery](sessions.md#recover-after-interruption). |
| Opening fails and cleanup also fails | Follow [provider cleanup recovery](configuration.md#recover-a-failed-managed-environment-opening) before discarding the owning event loop. |
| A checkpoint cannot be saved or restored | Check [capture readiness and supported boundaries](sessions.md#check-support-and-capture-readiness), matching tools and instructions, and current permissions. |

## Bound a wait and stop owned work

Timing out `turn.get_result()` stops only the wait. The Turn can continue. If your application owns the whole Session, let the timeout leave its async context. Session exit stops and waits for its work before raising the original exception:

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

The 30-second limit applies to the result wait. Cleanup can take longer. Catch `asyncio.TimeoutError` outside `async with`. If cleanup also fails, the timeout stays primary and retains the cleanup error in its exception context.

Cancellation has these limits:

- Python cannot forcibly stop a synchronous tool already running in a worker. Session exit waits for it.
- Closing a Session does not undo completed effects.
- If you own only the root Turn, use `await turn.cancel(mode="force")`. Independent children may continue.

See [interruption](sessions.md#interrupt-work) and [Python tool cancellation](tools.md#python-functions).

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

Read usage with these accounting rules:

- `total_token_usage` is cumulative for the native thread, including restored history. Do not add successive snapshots as separate costs.
- `last_token_usage` may cover only one response from a Turn that needed several. A snapshot may repeat when no new usage arrives.
- Cached and reasoning tokens are already included in totals. Do not add them again.
- Root usage excludes child threads. [Prompted-output repairs](configuration.md#structured-output) have separate records.

Both counts expose the fields listed in [usage types](reference.md#tokenusageinfo-and-tokenusage). `model_context_window` can be `None`. These counts are not billing records.

For a live display, replace the snapshot with `UsageUpdated.usage` from `agents.observation`, which can be `None`. Do not add event counts. Because observations can drop, read the final result after completion.

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

Include these details in a failure report:

- SDK version, Python version, operating system and failed operation.
- A small reproduction with synthetic inputs, and whether it fails with [scripted responses](testing.md) or only with the live provider.
- Available `NativeError` fields `status_code`, `code`, `request_id`, `category` and `effects`, if your data policy permits them.

Messages, request snapshots, prompts, tool results and checkpoints can contain private data. Inspect and redact reports before sending them to [GitHub issues](https://github.com/openai/openai-agents-python/issues). Never include credentials. Report vulnerabilities through [private security reporting](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/SECURITY.md).

For ongoing diagnostics, follow [telemetry configuration](configuration.md#telemetry) for startup, shutdown and permitted collector data.

## Diagnose a trace with history and application receipts

### Run the local walkthrough

The synthetic [trace walkthrough](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/observability/trace_walkthrough.py) starts a local OTLP JSON receiver and prints a receipt summary and viewer URL. It needs no collector account or model key. Keep receiver output synthetic.

First follow the [example-project setup](installation.md#examples-and-upgrades) to obtain the source files matching your installed wheel. From the directory containing `examples/`, run `python -m examples.observability.trace_walkthrough --scenario tool --view --output synthetic-trace.json`. Other scenarios are `tool-error`, `validation` and `provider`.

### Compare evidence

Match the application's `example.run_id`, available span `traceId`, Session identity and available `conversation.id`. Compare application outcomes with telemetry and the [history reader](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/observability/history_reader.py), which joins tool calls and results by turn and call identity. Missing timestamps and usage stay missing.

| Evidence | What it can show | What it does not prove |
| --- | --- | --- |
| Application receipt around `get_result()` | Settled output, raised error and application effect records | Native logs need not contain a separate record for a Python callback exception or output-validation failure. |
| `session.turns()` | Recorded turn status, failure, and available usage and timestamps | Missing usage or timestamps are not zero. Recorded turn usage differs from cumulative live thread snapshots. |
| `session.items()` messages and reasoning | Supported user and assistant text, user inline images, and retained reasoning summaries | Developer and system messages, unsupported content and unidentified records may be absent. This is not a full UI transcript or hidden reasoning. |
| Recorded function calls and results | Normalized function and custom calls and supported native tool records, joined by turn and call ID | `error=None` can accompany a returned tool error. Inspect the output and application outcome. Cancellation can leave incomplete pairs. |
| Recorded web search | Recorded query, action and text fields | These fields cannot reconstruct authoritative source records or citation spans. |
| Collector export | Received native spans and logs and configured telemetry fields | Export does not prove coverage of all Python failures, undo effects or define collector retention. |

A tool may return an error value and let the turn complete, or raise an exception that fails the turn. Final-output validation can fail after a tool effect. Diagnose those cases separately. Closing the Session does not delete data already exported to the collector.
