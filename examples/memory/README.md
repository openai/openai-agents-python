# Conversation memory examples

These examples preserve history between `Runner` calls. Follow the [example setup](../README.md), configure `OPENAI_API_KEY` for live model calls, and run a script from the repository root:

```sh
uv run python examples/memory/sqlite_session_example.py
```

Later turns ask about the city named in the first turn.

## Choose a store

| Example | Store or behavior | Extra setup |
| --- | --- | --- |
| [SQLite](sqlite_session_example.py) | Local conversation history | None. |
| [Advanced SQLite](advanced_sqlite_session_example.py) | Branches and usage | None. |
| [SQLAlchemy](sqlalchemy_session_example.py) | Async database history | `sqlalchemy` extra and `aiosqlite`. |
| [Redis](redis_session_example.py) | Redis history | `redis` extra and reachable `REDIS_URL`; defaults to local Redis. |
| [MongoDB](mongodb_session_example.py) | MongoDB history | `mongodb` extra and the script's MongoDB setup. |
| [Dapr](dapr_session_example.py) | Dapr state store | `dapr` extra, Dapr CLI, runtime, and state component. |
| [Encrypted](encrypted_session_example.py) | Encryption over in-memory SQLite | `encrypt` extra. |
| [OpenAI Conversations](openai_session_example.py) | Server-side conversation | OpenAI API credentials. |
| [Compaction](compaction_session_example.py) and [stateless compaction](compaction_session_stateless_example.py) | Responses history compaction | Access to the configured compaction model. |
| [Custom session](custom_session.py) | Adapter around a synchronous store | Offline; tracing disabled. |

For SQLAlchemy's SQLite example, use `uv run --extra sqlalchemy --with aiosqlite python examples/memory/sqlalchemy_session_example.py`. Select other extras with `uv run --extra <name> python ...`.

## Approvals and recovery

- [SQLite approvals](memory_session_hitl_example.py) and [OpenAI Conversations approvals](openai_session_hitl_example.py) combine history with approval decisions.
- [File approvals](file_hitl_example.py) and [approval scenarios](hitl_session_scenario.py) use the supporting [file store](file_session.py).
- Development-only [SQLite recovery](resumable_sqlite_session.py) and [Redis recovery](resumable_redis_session.py) demonstrate persisted approval recovery.

File-backed examples write local session data. The Redis example clears its fixed demo session; use dedicated test data. The encryption example generates a fresh key and uses an in-memory store. A persistent application must retain its key and session ID securely across restarts.
