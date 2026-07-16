# Session Persistence

Use this reference for changes to client-managed sessions, session input callbacks, per-turn persistence, retries, rewind, compaction replacement, or session backend implementations.

Read [Conversation state ownership](conversation-state-ownership.md) first when server-managed continuation is also involved. A client-managed session is a history store; it is not a second owner for a server-managed conversation.

## Session Contract

- `get_items(limit=N)` returns the latest `N` items in chronological order.
- `add_items()` appends one logical batch. Backends should make the batch atomic so partial turns are not visible after failure.
- `pop_item()` removes the current tail item and is used only for guarded rollback of items the current run can prove it owns.
- `clear_session()` clears the session boundary; compaction decorators that replace history must provide stronger restore behavior around destructive replacement.

Third-party implementations target the `Session` protocol. Internal base classes and backend-specific metadata are not the compatibility contract unless explicitly documented.

## Backend Consistency

- An explicit `get_items(limit=N)` argument overrides the backend's default session limit. Return the latest `N` items in chronological order, with a deterministic tie-breaker when timestamps can collide.
- Preserve caller batch order. Persist the items and any indexes or structural metadata required to read them as one atomic operation. A failed batch must leave earlier history unchanged, and any backend-internal retry must not create duplicates.
- Serialize initialization and conflicting writes at the backend's actual consistency boundary. Concurrent first writers must not race, and cancellation or failure must not strand locks or transactions.
- Apply configured table or collection names and session settings consistently across reads, writes, deletes, metadata updates, and wrapper operations.
- For backends that deserialize stored records, a corrupt record must not hide valid history or cause unrelated records to be deleted. Define consistent `get_items()` and `pop_item()` behavior that isolates the bad record and continues safely.
- Preserve creation timestamps and advance update timestamps deliberately. Backend-only identifiers and metadata must not leak into model-facing session items.
- Close only resources the backend owns. An injected engine, client, or connection remains caller-owned unless the public contract explicitly transfers ownership.

## Preparing Input Versus Persisting Input

`prepare_input_with_session()` returns two different values: the normalized input for the next model request and the subset of new-turn items that should be appended to the session.

- Existing history must not be re-appended as new input, even when `session_input_callback` deep-copies, reorders, filters, duplicates, or reconstructs items.
- A callback may change the model view without rewriting already stored history.
- Handoff and model-input filters may omit items from the next request while `session_step_items` retains the complete unfiltered sequence for history and observability.
- Normalize and deduplicate the model request and persistence candidates through the same canonical item helpers, then apply boundary-specific sanitization.

## Per-Turn Save and Resume

- Persist each completed turn, not only the final run result. Tool outputs and handoff items must survive a later error or interruption.
- `_current_turn_persisted_item_count` tracks which generated items have already been saved during streaming, retry, or resume. Count items after conversion and persistence filtering, not from the unsanitized source list.
- Resuming an interruption must save newly produced approval and tool output items without duplicating inputs or previously persisted outputs.
- Preserve full session items separately from filtered model input when updating `RunState` after resume.
- A guardrail trip must preserve the accepted user input while excluding speculative assistant or tool work that the tripwire invalidated. Test sequential and parallel guardrails in streaming and non-streaming modes because their persistence timing differs even though the resulting history must remain coherent.

## Retry Rewind

Retry cleanup is ownership-sensitive and best effort.

- Rewind only an exact serialized suffix that belongs to the failed attempt. Never scan backward and delete merely similar historical items.
- Verify the complete suffix before popping. If a pop fails or returns an unexpected item, restore already popped items in chronological order.
- Wait for backends with asynchronous cleanup semantics before starting the next retry when stale tail items could be observed.
- Do not forward live `RunContextWrapper` objects through retry rewind or compaction storage paths unless the session API explicitly owns that runtime context.

## Compaction Replacement

- Treat history replacement as a transaction: capture the prior state, apply the compacted state, and restore the prior state if clear or replacement fails.
- Defer response-based compaction while local tool outputs still need to be associated with the response chain.
- Choose input-based or previous-response-based compaction according to the actual state owner and `store` behavior; do not combine a local replay with a server-owned history chain.
- Compaction output is a run item and must follow the item lifecycle, session sanitization, and `RunState` rules rather than bypassing them as backend-only data.

## Review Checklist

1. Distinguish model input, new-turn persistence candidates, and full session history.
2. Test atomic failure, duplicate content, reordered callbacks, and filtered handoff input.
3. Test save behavior after tool execution, handoff, guardrail trip, interruption, and resume.
4. Prove retry rewind removes only the attempt-owned suffix and restores on partial failure.
5. Test compaction replacement failures without losing the previous history.
6. Test backend ordering, atomic batches, concurrent first writes, configured names and limits, corrupt records, and resource ownership.

## Sources

- `src/agents/memory/session.py`
- `src/agents/memory/session_settings.py`
- `src/agents/memory/sqlite_session.py`
- `src/agents/extensions/memory/`
- `src/agents/run_internal/session_persistence.py`
- `src/agents/run_internal/items.py`
- `src/agents/run_internal/run_steps.py`
- `tests/memory/`
- `tests/extensions/memory/`
- `tests/test_agent_runner.py`
- `tests/test_agent_runner_streamed.py`
