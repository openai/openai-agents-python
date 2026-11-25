# Ask an Agent2Agent specialist for a verdict

Use `specialist` to send one app-selected question over Agent2Agent (A2A) 1.0. Approve the send and each status check separately. For recovery after a lost reply or restart, use `DurableCall` with a specialist that supports receipt lookup.

## Try the local example

[Install the pinned SDK preview](../README.md#install-the-preview) in a Python 3.10+ virtual environment. Then run these commands from the repository root.

```sh
python -m pip install './examples/plugins/a2a[example]'
python -m agents_specialist.example
```

Expected output

```text
ask_specialist -> pending
refresh_specialist -> ready
```

See the [loopback example](src/agents_specialist/example.py) for the SDK client, scripted model, and simulated approvals. No API key required.

## Select the question and allowed replies

```python
from agents import Agent
from agents_specialist import specialist


def release_manager(client, *, durable=None):
    check = specialist(
        client,
        question="Check readiness of release R42.",
        artifact_id="readiness",
        verdicts=("ready", "blocked"),
        durable=durable,
    )
    return Agent(name="Release manager", plugins=[check])
```

Expect no model arguments for either tool and only a status or verdict in the output.

| Condition | Tool result |
| --- | --- |
| Submitted or working task | `pending` |
| Completed task with exactly one artifact, the expected ID, and one allowed text part | The matching verdict |
| Other reply, error, repeated send, or no matching task | `unknown` |

Without `durable`, use the same plugin instance to refresh a pending task. In this mode, expect no recovery after a lost first reply or a process restart. The only copy of the task and context IDs on the manager's side is in memory.

## Recover after a lost reply or restart

Use the factory above with a private SQLite file and a trusted receipt lookup. Choose an operation ID unique across all owners in that file. Keep the ID in trusted application storage, independent of model tool call IDs and saved approvals.

```python
from pathlib import Path

from agents_specialist import DurableCall


def restore_manager(client, owner, operation_id, private_dir, lookup_receipt):
    call = DurableCall(
        owner=owner,
        operation_id=operation_id,
        db_path=Path(private_dir).resolve() / "specialist.db",
        lookup_receipt=lookup_receipt,
    )
    return release_manager(client, durable=call)
```

1. On the specialist, commit the task and its immutable receipt together. Bind the authenticated owner, exact RPC URL, and incoming message ID to the question digest, task ID, and context ID. Compute the digest with `hashlib.sha256(question.encode()).hexdigest()`.
2. Supply `async def lookup_receipt(owner, endpoint, message_id)`. Check current access and read the specialist's record over a trusted channel. Return `agents_specialist.Receipt(owner, endpoint, message_id, digest, task_id, context_id)` from that record, or `None` for a missing receipt. This lookup is application-specific. Even a successful send requires a matching receipt.
3. Use `restore_manager` for the first send and after a restart. Preserve the same file, owner, operation ID, endpoint, and question. Request fresh approval for `refresh_specialist` to verify the receipt and call `GetTask`. A missing or mismatched receipt yields `unknown` without another send.

Only the worker that commits a new reservation can send. The reservation contains the operation ID, owner, endpoint, question digest, and random message ID. It excludes the question text and credentials. Protect the file and its backups. Digests can reveal predictable questions.

**An unknown outcome does not mean the send failed.** A crash between reservation and send can leave the question unsent. A crash after delivery can leave the specialist working. Never delete a reservation or choose a new ID to retry without checking with the specialist first. Losing the file or restoring an older copy can permit another send. There is no exactly-once delivery guarantee.

Run the [recovery tests](tests/test_durable.py) from the repository root. In the restart test, the specialist commits the task and closes the connection. The sender exits with an `unknown` result. A new manager reads `ready` without a second `SendMessage`.

```sh
python -m unittest discover -s examples/plugins/a2a/tests -v
```

## Before connecting a live specialist

- Create a separate client, plugin, and agent for each signed-in user and question. Before restoring a saved approval, check the owner, operation, destination, question, and current grant against trusted application records. Never reuse an approval for a different request.
- Use an approved HTTPS URL without credentials and local Agent Card bytes. Supply `A2AClient` or a trusted [client](src/agents_specialist/__init__.py) with the same checks on the URL, card, and responses. Verify the task and context IDs on reads. Use a fixed `rpc_url` for durable calls.
- Show the reviewer the operation, trusted recipient, and exact question. Ask for a new decision at each interruption.
- Recheck the user's grant before issuing each RPC token and at each receipt lookup. Use a fresh revision after any grant change, including reinstatement. Enforce access and task ownership at the specialist.
