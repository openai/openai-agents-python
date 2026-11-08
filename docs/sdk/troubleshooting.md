# Troubleshoot and operate an agent

Find the symptom below, then inspect the failed operation and any completed tool effects before retrying. Preserve the original exception. A failed request or timeout does not prove that earlier tools had no effects.

## Install and start

| Symptom | What to check |
| --- | --- |
| Installation fails, a wheel is unsupported, or GitHub returns 404 | Check [installation](installation.md#if-installation-fails) and match the guide version to the wheel. |
| `get_result` or another documented method is missing | Check the version with your application's `python -m pip show openai-agents`. Use the matching [preview guide](installation.md). |
| A notebook reports that an event loop is already running | Use `await agent.run(...)`. Use `run_sync()` or a script's `asyncio.run(main())` only outside a running event loop. |

## Run and observe work

| Symptom | What to check |
| --- | --- |
| A new prompt raises `SessionBusyError` | Wait for the Turn, or [queue or steer](sessions.md). Retrying `submit()` can duplicate input. |
| A one-shot run raises `InteractionRequired` | Inspect its optional checkpoint and [continue the pending request](approvals.md#continue-a-one-shot-manual-request). Do not resubmit the original prompt. |
| A Python tool raises an exception | Inspect the [tool error contract](tools.md#return-text-images-and-tool-errors) and any completed effects before submitting new work. |
| The agent is waiting for your application | Check for [manual results or approvals](approvals.md#answer-interactions-during-a-one-shot-run). Reply to the exact live request. |
| Streamed text is incomplete or an `EventGap` appears | The observation stream can drop records. Use `turn.get_result()` and its final output; see [streaming](sessions.md#stream-observations). |
| A result fails validation | Catch [`OutputValidationError`](configuration.md#structured-output). A new submission can repeat completed tool effects; validation does not undo them. |
| A provider or native operation fails | Inspect available `NativeError` metadata and [effect status](sessions.md#failures-and-recovery). Missing metadata remains unknown. |
| A provider stream succeeds but the SDK rejects its output | With `0.24.0-alpha.7` or later, inspect the typed [response integrity reason](sessions.md#diagnose-response-integrity-failures) and compare [retained response facts](sessions.md#inspect-response-attempts) with the SDK result. Earlier tool effects can remain. |
| Usage totals appear inflated or usage is missing | Check [usage accounting](observability.md#read-usage-without-double-counting). Replace cumulative snapshots when updating a display, and keep missing usage as unknown. |
| A scripted test uses an unexpected request or leaves responses unused | Each script entry is one model response. Check [request snapshots and exact exhaustion](testing.md). |

## Restore and clean up

| Symptom | What to check |
| --- | --- |
| Portable restore reports an unsupported Codex revision or `checkpoint core revision differs` | Use a wheel with the saving SDK's pinned Codex revision. Editing `codex_revision` does not migrate state. See [compatibility](state.md#save-and-restore-a-portable-checkpoint). |
| Local resume reports an unknown tool outcome or an active descendant | Keep the profile. Check application records and supply confirmed outcomes for unresolved root calls. If a child still blocks reopening, keep the conversation closed. See [local recovery](state.md#recover-after-interruption). |
| Opening fails and cleanup also fails | Follow [provider cleanup recovery](configuration.md#recover-a-failed-managed-environment-opening) before discarding the owning event loop. |
| A checkpoint cannot be saved or restored | Check [capture readiness and supported boundaries](state.md#check-support-and-capture-readiness), matching tools and instructions, and current permissions. |

## Bound a wait and stop owned work

Choose cancellation based on the work your application owns. Timing out `turn.get_result()` stops waiting without canceling the Turn. If your application owns the whole Session and needs that work to stop, let the timeout leave the Session's async context. Session exit stops its work and waits for cleanup before propagating the original exception:

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

The 30-second limit applies to waiting for the result; cleanup can take longer. Catch `asyncio.TimeoutError` outside `async with` so Session cleanup runs before your timeout handler. If cleanup also fails, the SDK keeps the timeout as the primary exception and retains the cleanup error in its exception context.

Cancellation has these limits:

- Python cannot forcibly stop a synchronous tool already running in a worker. Session exit waits for it.
- Closing a Session does not undo completed effects.
- If you own only the root Turn, use `await turn.cancel(mode="force")`. Independent children may continue.

See [interruption](sessions.md#interrupt-work) and [Python tool cancellation](tools.md#python-functions).

## Read usage without double counting

Read [usage snapshots and accounting limits](observability.md#read-usage-without-double-counting) before combining counts or displaying live usage.

## Plan the application lifecycle

Follow the [application lifecycle decisions](configuration.md#plan-the-application-lifecycle) for Session ownership, persistence, permissions, and shutdown.

## Prepare a useful failure report

Include these details in a failure report:

- SDK version, Python version, operating system and failed operation.
- A small reproduction with synthetic inputs, and whether it fails with [scripted responses](testing.md) or only with the live provider.
- Available `NativeError` fields `status_code`, `code`, `request_id`, `category` and `effects`, if your data policy permits them.
- In `0.24.0-alpha.7` or later, the optional `response_integrity_reason`. See [response integrity failures](sessions.md#diagnose-response-integrity-failures) for its values and limits.

Messages, request snapshots, prompts, tool results and checkpoints can contain private data. Inspect and redact reports before sending them to [GitHub issues](https://github.com/openai/openai-agents-python/issues). Never include credentials. Report vulnerabilities through [private security reporting](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/SECURITY.md).

For ongoing diagnostics, follow [telemetry configuration](observability.md#configure-telemetry) for startup, shutdown, and permitted collector data.

## Diagnose a trace with history and application receipts

Compare [history, collector exports, and application outcomes](observability.md#diagnose-a-trace-with-history-and-application-receipts) before deciding whether to retry failed work.

### Run the local walkthrough

Use the [synthetic local walkthrough](observability.md#run-the-local-walkthrough) to inspect a trace without a model key or collector account.

### Compare evidence

Use the [evidence comparison](observability.md#compare-evidence) to interpret missing records, tool errors, and completed effects.
