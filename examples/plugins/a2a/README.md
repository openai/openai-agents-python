# Asking an Agent2Agent specialist for a verdict

In this example, a release manager has been asking a specialist one question over Agent2Agent (A2A) 1.0. The app has been choosing the user, destination, question, and allowed replies. The manager has been requesting separate approval to submit the question and to read the result.

## Local example

**Prerequisites**

- Python 3.10 or later in a virtual environment.
- An SDK checkout with both `agents.plugins` and `agents.extensions.a2a.A2AClient`.

Commands from the repository root

```sh
python -m pip install '.[a2a]' './examples/plugins/a2a[example]'
python -m agents_specialist.example
```

Expected output

```text
ask_specialist -> pending
refresh_specialist -> ready
```

The [example](src/agents_specialist/example.py) has been using the SDK client and a local JSON-RPC service with a simulated model, grant, and approvals. No API keys or inference charges.

## Using your own specialist

Factory for a release manager with an app-supplied client

```python
from agents import Agent
from agents_specialist import specialist


def release_manager(client):
    check = specialist(
        client,
        question="Check readiness of release R42.",
        artifact_id="readiness",
        verdicts=("ready", "blocked"),
    )
    return Agent(name="Release manager", plugins=[check])
```

The app has been supplying the exact text for the peer, the expected artifact ID, and the permitted text values. No model arguments for either tool.

| Condition | Tool result |
| --- | --- |
| Submitted or working task | `pending` |
| Completed task with one matching artifact and one allowed text part | The matching verdict |
| Other replies, including direct messages, failed tasks, and unexpected text | `unknown` |
| Client error, repeated send, or refresh without a pending task ID | `unknown` |

The plugin has been keeping task and context IDs in memory for approved `GetTask` reads. It has been returning only the verdict or status to the manager. No automatic polling, retry, or deployment authority.

## Application requirements

- One agent and plugin instance per authenticated user and question, on one event loop. No sharing of agents, approvals, or run state across users or grants.
- An app-approved HTTPS endpoint and local Agent Card bytes. A trusted client with the [`Client` interface](src/agents_specialist/__init__.py) on SDK previews without the built-in adapter. The same URL, card, and response checks as `A2AClient`, including task and context ID checks on reads.
- A real decision on each interruption, with the operation, trusted recipient label, and exact question visible to the user. No permanent approval or automatic approval for a live service.
- A grant check at token issuance for every RPC, scoped to the user, peer, and operation. A fresh grant revision on any policy change, including reinstatement. Permission checks at the receiving service, including ownership of the task on reads.
- Confirmation of the outcome from the receiving service before another send attempt.

## Uncertain delivery

The plugin has been reserving its one send attempt before waiting for a reply. After a timeout or local cancellation, the specialist may have been working on the request. No remote cancellation, durable receipt, or recovery after a lost first reply or process restart.
