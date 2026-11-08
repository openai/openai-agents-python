# Optional: specialists, effects, and conversation cutover

Run one application on released v0, port it directly to native v1, or stage the package upgrade through classic. The examples use synthetic invoices and scripted models. They need no API key and make no model-service or tracing requests. Native tests start local Responses HTTP servers, so allow a direct connection or proxy bypass for `127.0.0.1`.

The application reads an invoice within a host-supplied tenant scope, asks a read-only policy specialist for a typed recommendation, and records a support case. A deliberately unauthorized tool request tests the write boundary. A second turn recalls the case without another write. The scripts check the actual effects and model inputs as well as the typed final answer.

## Prerequisites

Complete the [main walkthrough's matching-source installation](../README.md#install-the-matching-sdks) first. Reuse its `SDK_ROOT`, `MIGRATION_WORK`, and separate SDK environments, and install the [classic extra](../README.md#optional-stage-the-package-upgrade-through-classic). This optional application adds a specialist, writes, SQLite continuation, reviewed-fact cutover, and native checkpoint restore to the read-only walkthrough.

## Establish the v0 baseline

Run [v0.py](v0.py) with the released SDK:

```sh
PYTHONPATH="$SDK_ROOT" "$MIGRATION_WORK/v0-env/bin/python" \
  -m examples.codex.migration.advanced.v0 "$MIGRATION_WORK/direct-v0"
```

The final answer contains invoice `INV-42`, amount `4200`, case `case-1`, and label `atlas`. `new writes=['INV-42']` proves that the allowed operation reached the shared business function. The unauthorized `INV-private` request adds no write. The follow-up model input includes the earlier case result and label.

The script creates `classic.sqlite` and `reviewed-facts.json` in the supplied directory. The SQLite file belongs to the v0/classic history-store contract. The JSON file contains selected business facts from the validated answer. Review those facts against your application records before using them to start a new conversation. It contains no serialized run, tool handle, approval, or credentials.

[Business data and authorization](business.py) stay unchanged across the ports. `Application` is typed local context, `Invoice`, `PolicyReview`, and `SupportAnswer` are typed values, and `record_case()` checks authorization before appending an effect. This in-memory list records every invocation; it provides no durable deduplication or exactly-once guarantee.

## Port directly to native v1

Use the current-source wheel from that setup, rather than the published alpha.6 preview. Run [native.py](native.py) against the reviewed facts:

```sh
PYTHONPATH="$SDK_ROOT" "$MIGRATION_WORK/v1-env/bin/python" \
  -m examples.codex.migration.advanced.native "$MIGRATION_WORK/direct-native" \
  --facts "$MIGRATION_WORK/direct-v0/reviewed-facts.json"
```

The command performs two separate checks:

1. `native port` tests the port on a fresh synthetic application. The allowed operation records one case, the denied operation records none, and the native checkpoint restores the follow-up history without repeating the callback.
2. `native cutover` starts an empty native conversation from the reviewed v0 facts. The old case is already complete, so `new writes=[]`. The new native conversation then saves its own checkpoint, closes, and restores for a follow-up. It never reads the v0 SQLite store.

The fresh synthetic application in the first check has its own effect list. Do not replay completed business operations during a real cutover merely to test a port.

Compare [v0.py](v0.py) with [native.py](native.py). The business functions and output models remain the same. The SDK declarations change together:

| Contract | v0 / classic | Native port |
| --- | --- | --- |
| Context in a tool | `RunContextWrapper[Application]` | `ToolContext[Application]` |
| Agent execution | `Runner.run(agent, ...)` | `agent.run(...)` or Session submission and `turn.get_result()` |
| Effect authorization | Application check before `record_case()` | Native `before` returns `Allow` or `Deny`; the shared business check remains in place. |
| Specialist | Separate Agent and `ScriptedModel` | Separate Agent and `NativeProvider`; only the policy tool and `Policy` context are supplied. |
| Conversation | `SQLiteSession` | Live native Session, followed by opaque `SessionState` JSON. |
| Test provider | `agents.testing.ScriptedModel` | `agents.testing.ScriptedResponses`, which drives native HTTP and tools. |

In production, configure the root and specialist model/provider explicitly for the behavior each requires. Replacing a classic Python model with native provider settings requires a compatible Responses endpoint; see [provider compatibility](../../../../docs/sdk/providers.md). Scripted replies establish application behavior under those replies. They do not evaluate model quality, tool choice, or provider compatibility. Repeat your evaluations and bounded live integration checks before deployment.

## Stage through classic when needed

Start from another v0-produced store so both paths have independent data:

```sh
PYTHONPATH="$SDK_ROOT" "$MIGRATION_WORK/v0-env/bin/python" \
  -m examples.codex.migration.advanced.v0 "$MIGRATION_WORK/staged-v0"
PYTHONPATH="$SDK_ROOT" "$MIGRATION_WORK/v1-env/bin/python" \
  -m examples.codex.migration.advanced.classic "$MIGRATION_WORK/staged-v0" --resume
```

[Classic.py](classic.py) keeps the v0 workflow and changes its SDK imports to `agents.classic` and `agents.classic.testing`. The `--resume` invocation reads the actual v0 SQLite file, supplies its history to the next model request, and adds no write. It fails if the store is missing. Classic still runs the Python loop; it does not migrate this conversation to Codex.

After reviewing the resulting facts, start native work separately:

```sh
PYTHONPATH="$SDK_ROOT" "$MIGRATION_WORK/v1-env/bin/python" \
  -m examples.codex.migration.advanced.native "$MIGRATION_WORK/staged-native" \
  --facts "$MIGRATION_WORK/staged-v0/reviewed-facts.json"
```

The native target is the same port used by the direct path. Keep old conversations and pending approvals on their owning runtime. This example verifies SQLite history across v0/classic; it does not prove compatibility for every session adapter or serialized `RunState`. Test the stores, callback identities, integrations, and recovery paths your application uses.

## Keep ownership explicit

`consult_policy` runs a separately configured specialist and returns its result to Invoice support. It demonstrates an agent used as a tool. It does not transfer the conversation, change the owner of the next user turn, or create a native child Agent. The specialist receives only the read-only policy context and tool, but Python closures are not an automatic authority-isolation boundary.

If later user turns should go to the specialist, keep that ownership decision in application state and route each later input to the chosen Agent and its own conversation. For a workflow that relies on true v0 handoffs, preserve classic until you have ported and tested that routing, context transfer, and next-turn ownership. Consider [native child agents](../../../../docs/sdk/delegation.md) only when their configuration and lifecycle match the requirement. Keep useful MCP clients, sandbox/voice integrations, and application business services where their documented contracts remain applicable.

Native checkpoints require the producer's exact Codex revision and current application tools/context. Retain the matching SDK build and use trusted storage. These scripts only reopen idle, settled conversations. They do not recover an uncertain external effect, convert classic state, or guarantee portability across platforms. See [state compatibility and recovery](../../../../docs/sdk/state.md).

## Run the maintained checks

In a contributor environment for the current checkout, run:

```sh
uv run --no-sync pytest -q packages/codex/tests/test_migration_example.py
```

The released-v0 qualification is optional in ordinary unit runs because it needs the separate environment. Enable it to execute both real migration routes:

```sh
MIGRATION_V0_PYTHON="$MIGRATION_WORK/v0-env/bin/python" \
  uv run --no-sync pytest -q packages/codex/tests/test_migration_example.py
```

These tests execute the examples and inspect the next model requests for prior tool results, selected business facts, and restored history. They also check typed outputs, the specialist's actual tool result, the allowed effect, and the absent denied effect. For cancellation and provider-failure checks, adapt the existing [scripted testing guide](../../../../docs/sdk/testing.md) and [safe-effects recipe](../../recipes/safe_effects.py) to your application.
