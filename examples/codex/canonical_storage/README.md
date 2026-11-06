# Application-owned native storage preview

`CustomStorage` lets your application store a native Session in its own backend. This example uses the persistent `SQLiteSessionStore` reference adapter to start a conversation and resume it in a second process. Native Codex still owns model requests, turns and tool dispatch.

## Set up and run

`CustomStorage` was added after `0.24.0-alpha.6`. Follow the [development-branch setup](../../../docs/sdk/installation.md#examples-and-upgrades) and use the example files from the same build. The reference adapter requires a trusted local POSIX filesystem. Provide your current model credentials through the normal provider configuration; model requests can incur charges. Run from the repository root with that build's Python environment active:

```sh
python examples/codex/canonical_storage/main.py ./conversation.sqlite
python examples/codex/canonical_storage/main.py ./conversation.sqlite --resume SESSION_ID --prompt 'What did you find?'
```

Use the Session ID printed by the first process. Both processes must use the same database. Each open uses the application's current tool callbacks, context and credentials. Saved history never supplies credentials or executable callbacks. Only load trusted application history: it includes instructions and tool data.

## Backend contract

Pass `CustomStorage(backend)` to `Agent.session(storage=...)`. The backend implements the public `SessionStore` protocol. Methods are synchronous and run on native worker threads. They must be thread-safe and must not call back into the SDK.

Each commit stores native thread metadata and ordered canonical history, including the original thread, turn and call identities and recorded tool results. An in-memory projection serves native reads. The SDK acknowledges a write only after the application backend commits it. A backend failure never selects another store.

| Method | Required behavior |
| --- | --- |
| `acquire(session_id) -> token` | Acquire exclusive writer ownership. Return a new fencing token; stale owners must lose write authority. |
| `read(session_id, token) -> str or None` | Under that ownership, read the complete authoritative snapshot. Return `None` only when no committed snapshot exists. |
| `commit(session_id, token, expected_revision, write_id, payload)` | Atomically check the token and previous revision, then durably replace the opaque snapshot. An identical accepted write ID and payload is an idempotent success. |
| `flush(session_id, token)` | Confirm every accepted canonical write is durable and readable. |
| `release(session_id, token)` | Release ownership after success or failure while retaining committed history. Release must be idempotent for the same token after success. The SDK retains the token when release fails so cleanup can retry; implementations should release locally owned resources even on failure. |

The SDK acquires ownership before reading resume history or selecting a saved model. It holds ownership through initialization, execution and close. Failed initialization and close release the writer. It reconciles a lost commit acknowledgment by reading back the exact payload under the same ownership, then repeating that same write identity and requiring a successful durable acknowledgment. A readable snapshot alone does not prove durability. If either check fails, the Session fails and must close. Commits use a stable writer token plus revision as their write identity. Backends must enforce the token at the atomic commit boundary.

Canonical writes include thread metadata, accepted input, tool intent, tool results and execution history. They include the complete snapshots passed to the backend. Model caches, search indexes and telemetry are rebuildable and do not replace canonical history. Successful commit means the entire snapshot survives process death and is readable by the next owner. Successful flush confirms that boundary for all prior writes. Backend implementers must state whether their storage also survives host loss.

The native runtime commits and flushes tool intent before dispatch. Python callback results commit before they return to native work, including results that finish ahead of an earlier sibling. Native result barriers prevent dependent model work after a failed result write. An intent failure starts no affected tool. A result failure cannot undo an effect that already occurred or stop a sibling that already started. The original call without a recorded result remains an unknown outcome. Append, flush and close failures remain visible; later successful storage work does not repair the failed Session.

## Recovery

Close a failed Session before recovery. A missing receipt blocks reopening for automatic model or tool work, including repeated reopen attempts. Check the external system using the original turn and call IDs. When authoritative records establish the actual outcome, record the result with `storage.reconcile()`.

In the following excerpt, `storage` is the same `CustomStorage` configuration, the IDs identify the unresolved call, and `verified_result` comes from your external-system check. Pass a string or a `ToolOutput` for error, image or mixed content; reconciliation uses the same conversion as an ordinary callback:

```python
await storage.reconcile(
    session_id,
    turn_id=original_turn_id,
    call_id=original_call_id,
    output=verified_result,
)
```

Reconciliation acquires the same exclusive ownership and commits a result for that unresolved original identity. It never invokes the tool and cannot replace a known receipt. Reopen the Session afterward with the current tools and credentials. If the external system cannot establish the outcome, keep the Session blocked. This API does not guarantee exactly-once external actions.

## Supported state and limits

This preview supports root Sessions with automatic Python tools. Before opening native execution, it rejects native tools (including MCP and web search), environments, child agents, goals, checklists, code mode, and manual tools. The adapter does not yet persist the additional state and receipts those features require.

Portable `SessionState` and custom canonical storage cannot be combined. Thread listing, archival, forks, and local rollout-path access are unsupported. Use `LocalStorage` for its other [supported combinations](../../../docs/sdk/state.md#choose-how-to-store-a-conversation).

In development builds after alpha.6, `LocalStorage` also rejects `programmatic_tool_calling=True`. Nested tool calls lack durable intent and result receipts, so the SDK rejects this combination before storage acquisition or model/tool work. Nonpersistent code mode remains available. Use the matching build and example files described in [setup](#set-up-and-run).

Snapshots check native schema version 1, the Python SDK family and the pinned Codex revision. These checks do not prove compatibility with a different SDK binding revision. Snapshots are opaque backend values. Do not edit their JSON or assume compatibility across SDK versions or between Python and JavaScript. Keep the matching SDK build when reopening saved history; upgrade and migration qualification are separate work. The application owns storage access, encryption, retention and backups.

### Use the SQLite reference adapter safely

`SQLiteSessionStore` uses SQLite transactions with `synchronous=FULL`, local POSIX file locks, and database fencing tokens. Process death releases the OS lock. A new owner replaces the token before reading history. Committed snapshots survive process death. Host-loss durability depends on the filesystem and disk honoring `fsync`. Distinct Session IDs can share one database.

Symlinks resolve once to the canonical database path; hard-linked databases are rejected. Keep the supplied path, its ancestor directories and every symlink target under trusted control and protected from untrusted replacement during initialization and use. File checks do not make attacker-controlled paths safe.

Existing database, SQLite sidecar and writer lock files must belong to the effective user and have no group or other permissions. The adapter rejects unsafe files before writes. Use files created privately: changing permissions on an exposed file cannot revoke a reader that already opened it. Do not replace the database or its lock files while Sessions are open.

Network filesystems, cross-host writers and Windows are unsupported. Do not delete lock files to recover ownership; closing the writer or terminating its process releases its lock safely. The reference adapter rewrites the whole snapshot on each canonical mutation, so it favors correctness and a small contract over large-history throughput.
