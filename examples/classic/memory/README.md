# Classic conversation memory

These examples preserve conversation history between `Runner` calls. A Classic session stores history; a native Session owns a live conversation. See [Classic setup](../../../CLASSIC.md) before running these examples.

From the repository root, with `OPENAI_API_KEY` configured:

```sh
python -m examples.classic.memory.sqlite_session_example
```

The later turns ask about the city named in the first turn, showing history reuse.

## Choose a store

| Example | Store or behavior | Additional setup beyond `classic` |
| --- | --- | --- |
| [`sqlite_session_example.py`](sqlite_session_example.py) | SQLite history | None. |
| [`advanced_sqlite_session_example.py`](advanced_sqlite_session_example.py) | SQLite branches and usage | None. |
| [`sqlalchemy_session_example.py`](sqlalchemy_session_example.py) | Async SQLAlchemy history | `classic-sqlalchemy` and `aiosqlite` for this example's SQLite URL. |
| [`redis_session_example.py`](redis_session_example.py) | Redis history | `classic-redis`; reachable Redis at `REDIS_URL`, default `redis://localhost:6379/0`. |
| [`mongodb_session_example.py`](mongodb_session_example.py) | MongoDB history | `classic-mongodb`; the script's local MongoDB setup. |
| [`dapr_session_example.py`](dapr_session_example.py) | Dapr state store | `classic-dapr`; Dapr CLI, runtime, and configured state component. See the script's setup instructions. |
| [`encrypted_session_example.py`](encrypted_session_example.py) | Encryption over an in-memory SQLite session | `classic-encrypt`. |
| [`openai_session_example.py`](openai_session_example.py) | OpenAI Conversations | OpenAI API credentials. |
| [`compaction_session_example.py`](compaction_session_example.py) | Responses history compaction | OpenAI API access to the configured compaction model. |
| [`compaction_session_stateless_example.py`](compaction_session_stateless_example.py) | Input-based compaction with `store=False` | OpenAI API access to the configured compaction model. |
| [`custom_session.py`](custom_session.py) | Adapter around a synchronous history store | None; deterministic offline example with trace export disabled. |

For example, install the SQLite async driver into an already activated matching-wheel environment:

```sh
python -m pip install aiosqlite
```

The [Classic driver guide](../../../CLASSIC.md#sqlite-session-drivers) covers the corresponding SDK extras.

## Approval and persistence examples

| Example | Focus |
| --- | --- |
| [`memory_session_hitl_example.py`](memory_session_hitl_example.py) | Approvals with SQLite history. |
| [`openai_session_hitl_example.py`](openai_session_hitl_example.py) | Approvals with OpenAI Conversations. |
| [`file_hitl_example.py`](file_hitl_example.py) | Approvals with the example's JSON file store. |
| [`hitl_session_scenario.py`](hitl_session_scenario.py) | Approval, rehydration, and rejection scenarios. |
| [`file_session.py`](file_session.py) | Supporting file-store implementation rather than a runnable demo. |

The file-backed examples write local session data. The Redis example clears its fixed demo session at startup. Use dedicated test data and inspect each script's session ID before connecting it to an existing store.

`encrypted_session_example.py` creates a fresh key on each run and uses an in-memory store. Persistent deployments need the same securely stored key and session ID after restart; this example does not demonstrate key management.
