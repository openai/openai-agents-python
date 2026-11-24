# Call an A2A service

Install the optional client with `pip install '.[a2a]'` from this checkout. A direct call needs no registry, database or service discovery.

## Send a message and check the result

Supply the approved endpoint as `rpc_url` and its UTF-8 Agent Card JSON bytes as `card_json`. On each request, `rpc_token_provider()` must check the current user's grant for that endpoint and return a fresh token. Keep this function non-blocking. The service must enforce task ownership.

This example assumes the user may read a direct reply or the completed task's `verdict` artifact. It accepts only one text part, `yes` or `no`. Inside an async function, send once and check progress at most once.

```python
from uuid import uuid4

from a2a.types import Message, Task, TaskState

from agents.extensions.a2a import A2AClient

client = A2AClient(rpc_url=rpc_url, card_json=card_json, rpc_token_provider=rpc_token_provider)
message_id = uuid4().hex
reply = await client.send_message("Ready?", message_id=message_id)
if isinstance(reply, Task) and reply.status.state in (
    TaskState.TASK_STATE_SUBMITTED,
    TaskState.TASK_STATE_WORKING,
):
    reply = await client.refresh_task(reply)

verdict = None
if isinstance(reply, Message):
    (part,) = reply.parts
    verdict = part.text
elif reply.status.state == TaskState.TASK_STATE_COMPLETED:
    (artifact,) = reply.artifacts
    if artifact.artifact_id != "verdict":
        raise ValueError("Unexpected artifact")
    (part,) = artifact.parts
    verdict = part.text

if verdict is not None and verdict not in ("yes", "no"):
    raise ValueError("Unexpected verdict")
```

If `verdict` is `None`, inspect the task's status. Keep the task for any later authorized read. Use a new message ID only for a new request. If the send fails, delivery may be uncertain. Do not resend.

## Client limits and credentials

The adapter uses the official Python SDK's [A2A 1.0](https://a2a-protocol.org/v1.0.0/specification/) message types.

Only use application-approved destinations. Use HTTPS for `rpc_url`. If fetching a card, supply a `card_url` on the same origin. HTTP is available on `127.0.0.1` and `::1` for development. The card must list the exact RPC URL with binding `JSONRPC`, version `1.0` and no tenant. Each operation checks this before requesting RPC credentials. Card metadata cannot select endpoints or credentials. The adapter follows no redirects and uses no environment proxies or shared cookies. The application owns DNS and network access policy.

The [A2A Python client](https://a2a-protocol.org/latest/sdk/python/api/a2a.client.base_client.html) also exposes `send_message` and `get_task`. Its `send_message` returns an async iterator. Here, `send_message` makes one non-streaming `SendMessage` request with one user text part and returns an `a2a.types.Message` or `a2a.types.Task`. A direct agent reply has its own message ID and may omit the context ID. `get_task` makes one `GetTask` request and requires the task and context IDs to match. Both operations check the JSON-RPC response ID.

`refresh_task(task)` calls `get_task(task_id=task.id, context_id=task.context_id)` once. Use the same client and owner. A task grants no access. The reply can still be pending.

Use artifacts only after checking for `TaskState.TASK_STATE_COMPLETED`. Treat all peer fields as private and untrusted, including text that might repeat credentials. Avoid logging replies, HTTP traffic, exception locals or Runner's attached `run_data`.

The maximum encoded request is 64 KiB. Each card and RPC response has a 1 MiB limit. Compressed responses are unsupported. The default `timeout` is ten seconds across discovery and RPC. `A2AError.reason` distinguishes request, authorization, timeout, transport, protocol and remote errors. Local cancellation propagates as `asyncio.CancelledError`. A timeout, disconnect or cancellation may leave remote work running. The adapter never retries, polls or sends `CancelTask`. Remote cancellation requires separate authorization and cannot guarantee rollback.

## Try it without an API key

From a source checkout, run the [loopback example tests](../../tests/test_a2a.py).

```sh
make sync
uv run --no-sync pytest -q tests/test_a2a.py
```

The scripted manager filters 500 names and calls two services on separate ports. One uses a stored card. The other requires separate credentials for discovery and task calls. The recovery test drops a reply after commit, closes both databases and restarts the receiver. The application retrieves the original verdict by call ID without a second send.

## Optional manager and SQLite recovery

[manager.py](manager.py) defines two ordinary function tools. The manager looks up names with `find_specialists`. It asks a yes/no question with `ask_specialist`. The verdict returns to the manager under the original tool call ID. Neither tool hands off control of the run.

Receipt recovery requires a reservation before the send. The direct call above creates no reservation.

1. Load approved names and URLs from application configuration for the authenticated owner. Construct an `agents.extensions.a2a.A2AClient` for each name. Supply `rpc_url` and either UTF-8 Agent Card JSON as `card_json` bytes or an exact `card_url`. Keep names and URLs free of secrets.
2. For bearer authentication, supply separate zero-argument `card_token_provider` and `rpc_token_provider` functions. Omit a supplier only for an anonymous route. Each function must check the current grant and return a token for its URL. Both must be non-blocking. The receiver must authenticate the caller and enforce ownership on every task read.
3. Construct a `Registry` with the owner, a stable run ID, the authorized name mapping, a dedicated SQLite connection and a trusted `lookup_receipt` function. Keep the grants fixed during each invocation. Reload them after restart. Protect both databases, including question digests and receipts.
4. Add both tools to the manager's `Agent.tools`. Pass the registry as `context` to `Runner.run`. The model sees only the prefix, name and question arguments. Search returns at most ten names. Every registered name is callable, including names outside the search results.

### Recover a lost acknowledgement

The manager requires a cooperating receiver that returns a task. A direct message leaves the call unresolved and the reservation intact. Its business effect is one immutable verdict. The receiver commits that verdict and its receipt together under a unique message ID. A2A alone provides no receipt lookup by message ID or guarantee against replay.

The manager reserves the owner, run and tool call ID in SQLite before sending. It binds the reservation to the name, approved endpoint, question digest and a new message ID. The reservation survives failure even if the request never reached the service. A new run or call ID permits a new send.

A failed run may leave the application without the question the model generated. The journal stores only its digest. On restart, reopen the same database and restore the registry for the authenticated owner and original run. List the call IDs and specialist names your current grants allow.

```python
calls = registry.reserved_calls()  # (call_id, name) pairs
```

Completed calls also have reservations. Listing them makes no network requests and does not verify their outcomes.

For the call you need to reconcile, use `verdict = await registry.recover(call_id)`. This application method never creates a reservation, sends a question or resumes the manager. If the manager repeats an existing call with identical arguments, `ask` uses the same read path.

Both paths check the current grant for the saved name and endpoint. The trusted lookup must return the receiver's immutable `Receipt` for that owner, endpoint, message ID and question digest. Recovery then reads the task with `GetTask`. The example checks both task and context IDs, the completed state and a single text verdict matching the receipt. Only `yes` or `no` passes verification.

Missing or conflicting evidence leaves the call unresolved. Removed grants, changed endpoints or different arguments for the same call ID block reuse. Tool failures stop the run for application handling. Never delete a reservation or generate a replacement call to resolve an uncertain outcome.
