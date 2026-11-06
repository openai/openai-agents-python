# A2A integration

The optional `agents.a2a` package connects native Agents SDK applications to independently hosted agents. It supports `a2a-sdk==1.3.0`, A2A 1.0 JSON-RPC, bounded text/JSON, and task-producing services. Install the matching SDK preview with its `a2a` extra. Server applications also install `a2a-sdk[http-server]==1.3.0` and an ASGI server.

## Call a specialist

`client.py` discovers one host-configured Agent Card, admits only its exact configured JSON-RPC interface, and then acquires a bearer token. Use HTTPS for remote services. For local development, HTTP works on `localhost`, `127.0.0.1`, and `[::1]`.

    export A2A_BEARER_TOKEN='your host-issued token'
    python examples/a2a/client.py \
      --card-url https://specialist.example/.well-known/agent-card.json \
      --rpc-url https://specialist.example/rpc \
      --question 'Summarize the synthetic task'

The application owns its registry and chooses the endpoint. A model may select a registered name through a normal FunctionTool; the host resolves that name to trusted configuration. Cards describe capabilities. They cannot select routes, credentials or permission grants. Discovery is anonymous in this profile. Redirects, alternate advertised routes and remote tenant selection are unsupported. Ordinary `import agents` does not require A2A dependencies.

`connect_a2a` yields the official client. The host owns result interpretation and remote error handling. `A2ALimits` provides defensive message, response, checkpoint, progress and timeout bounds; these values are adapter defaults, not platform quotas. Configuration parameters accept their typed objects or equivalent dictionaries. In a native function callback, pass `ctx.trace_context` as `connect_a2a(..., parent_trace=ctx.trace_context)`. Only validated `traceparent` and `tracestate` headers cross this boundary. The adapter adopts no ambient tracing provider and stores no live carrier in task checkpoints.

For work that must survive the calling worker, register `A2AJobProvider` with existing `DeferredTools` and an application `JobJournal`. The provider's `encode` converts current typed arguments into a bounded text/JSON Message. Its `project` validates completed Task artifacts into bounded application output. Register with `require_reconciliation=False`: generic A2A cannot reconcile a lost creation acknowledgment.

The provider checks `authorize(claim, operation)` against current host policy before contacting the service. After admitting the configured card, it calls `authorization()` for that endpoint's bearer token. Immediately before each mutation, it checks authority again and calls `reserve(claim, operation, action_id)`. The host must atomically reserve that action under the current claim and retain the reservation across worker loss. A reply reservation identifies the task, challenge and generation, independent of the answer's contents. Keep credentials, claims, receipts and checkpoints out of model input and logs.

### Credential rotation and host validation

`A2AJobProvider` rejects reflections of the bearer acquired for the current operation and clears its credential references when that operation ends. This filter does not recognize every credential previously issued to the service. A peer can return an earlier token after rotation, including through a task ID, form or completed artifact. Treat all peer data as private until the host validates it.

The host must enforce its credential and output policy before logging, storing or releasing peer data. The policy must cover credentials that remain valid after rotation, including those used by another worker or job. Restore this policy from the host's credential authority when a worker restarts. A check against only the latest token, or a token list kept in one provider instance, is insufficient. Keep bearer tokens in the host's credential service; do not copy them into task receipts, results or native checkpoints.

Apply the policy at these existing boundaries:

- In `project(task)`, validate the complete private Task before logging or copying its fields. Return only approved application output. Reject unexpected fields or values with an exception that does not include the private input.
- In `JobJournal.record(claim, update)`, validate the full receipt and value before writing either. Include task/context IDs and all input-required or auth-required form content. A refusal must raise before writing the rejected data. The deferred runner awaits this method before running the deferred tool's output projector or delivering output to the model.
- In `JobJournal.save_result`, validate the final output before storing it for model delivery.
- In journal `claim` and `authorize`, apply the current policy to saved updates and results before reuse. A restored job can use those rows without calling the provider or `record` again. Validate or quarantine rows written under an older policy, and refuse to resume if the required policy is unavailable.

These hooks let the application enforce its own policy; the SDK does not supply that policy automatically. Keep the checks bound to the current tenant, principal, endpoint and job. For example, admit a closed set of expected result codes and map them to host-owned output instead of copying arbitrary peer text.

Known receipts support observation without creating another task. Lost create, input or cancellation acknowledgments remain unknown; neither protocol message IDs nor another connection make a replay safe. The host owns reconciliation and scheduling. Business input requires one data-object proposal with a nonempty string `challenge` and a positive whole-number `generation`. Unsupported proposals retain the acknowledged receipt as unknown and cannot reserve or send an answer. `auth-required` remains separate from business approval. A host-approved `A2AInputReply` must match the current remote task, context, challenge and generation. The host must validate domain answers before reserving them.

## Expose a native Agent

`server.py` composes `NativeA2AExecutor` with official routes and an application-provided `NativeA2AStore`. The host supplies authentication middleware and a `ServerCallContextBuilder` that includes the authenticated user and the incoming `a2a-version` header in `state['headers']`. The host factory receives the trusted principal/task/context binding and current official `ServerCallContext`. It returns current `NativeA2AAgent` configuration and application context. Filter incoming trace headers from that call context and supply the resulting `parent_trace`; a fresh worker supplies a fresh parent. It must not reconstruct authority from peer metadata or checkpoint contents.

Data parts must preserve JSON values exactly. The executor fails a task if encoding its result or approval proposal would round a number. The job provider rejects such replies before reserving an action. Use strings for large integer identifiers that must retain every digit.

Each A2A task owns one native Session. Reusing a conversation context ID does not combine Sessions. Core owns model requests, tools and accepted history. Tool progress is passive; successful final artifacts follow validated `get_result()` and joined Session cleanup. Disconnecting an SSE observer leaves remote work running. Explicit CancelTask joins owned native work and reports settled state; it does not roll back completed external effects.

The first continuation profile supports one settled root `ActionRequested`. Other live, nested or simultaneous manual requests are rejected. The adapter suspends the Session and saves the checkpoint with an opaque challenge. Current tool declarations validate an answer before the host spends the continuation claim. Malformed answers preserve the pending checkpoint for correction. Restored Sessions issue current grants; stale, foreign and repeated answers cannot acquire authority. Arbitrary files, URLs, MCP UI, push notifications, message-only responses, other protocol versions and other transports are outside this profile.

## Store and publication recovery

`NativeA2AStore` extends the official `VersionedTaskStore`. The host implements private ownership through `own(binding)`, whose lease supplies `load()` and conditional `commit(record, expected_generation=...)`. Keep ownership exclusive until the context exits. Before replacing an owner, stop and join its worker and official event consumer. Check principal, task/context identity, current ownership and generation on every operation. TaskStore Get, List, Subscribe, Cancel and follow-up requests need the same access policy as creation. Opaque task IDs are references, not credentials.

Commit the private checkpoint or terminal outcome and its desired complete public snapshot atomically. `NativeA2ATask.generation` protects private writes; `publication_revision` identifies a desired snapshot. Both differ from the official `TaskVersion` compare-and-swap token. Snapshots are immutable bytes; `record.task()` returns a separate mutable protobuf object. Before using a loaded private record, the executor checks both its binding and its saved Task identifiers against the authenticated requested binding.

The executor uses the official TaskManager to project an event batch, then commits the desired snapshot before enqueueing ordinary official status/artifact events. The host's official `save` method acknowledges a revision only after storing the exact snapshot for which `record.acknowledges(task)` returns true. Intermediate progress and artifact writes cannot acknowledge a different snapshot. Enqueue success is not a durability acknowledgment. Reject stale writes rather than replacing a newer or terminal snapshot. A failed follow-up must not erase the still-valid pending challenge or replace its saved boundary with a framework-generated failure. Official cancellation can bypass an inactive executor after restart or task eviction; in that case, atomically retire the waiting checkpoint/challenge and adopt the authenticated canceled snapshot.

A save can succeed before its acknowledgment fails. Retain the desired snapshot and revision in that case. Once the old worker and its official handler have stopped, the host acquires exclusive ownership and calls `executor.recover(binding, authenticated_context)`. Recovery uses the same official store to republish the retained complete snapshot. It does not concatenate old artifact chunks, construct an Agent or rerun native work. The host schedules recovery and retains records for its required period. Running or claimed work without a committed settled outcome remains unknown and requires host reconciliation.

This contract supports publication recovery. It does not make separate host databases transactional or supply distributed leases, production authentication, artifact retention, a scheduler or a storage implementation.
