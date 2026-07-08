# Session memory examples

This folder contains runnable examples for Agents SDK session memory: built-in backends, compaction, human-in-the-loop flows, and history management patterns.

## Getting started

- [`sqlite_session_example.py`](./sqlite_session_example.py) — Basic multi-turn conversation with `SQLiteSession`.
- [`session_limit_example.py`](./session_limit_example.py) — Cap retrieved history with `SessionSettings(limit=N)`.
- [`session_input_callback_example.py`](./session_input_callback_example.py) — Prune or customize history merging via `RunConfig.session_input_callback`.
- [`session_pop_item_example.py`](./session_pop_item_example.py) — Undo the latest turn with `pop_item` before re-asking.

## Built-in session backends

- [`redis_session_example.py`](./redis_session_example.py) — Shared memory across workers with Redis.
- [`sqlalchemy_session_example.py`](./sqlalchemy_session_example.py) — SQLAlchemy-backed persistence.
- [`mongodb_session_example.py`](./mongodb_session_example.py) — MongoDB-backed sessions with a shared client.
- [`dapr_session_example.py`](./dapr_session_example.py) — Dapr state store sessions.
- [`encrypted_session_example.py`](./encrypted_session_example.py) — Transparent encryption wrapper.
- [`advanced_sqlite_session_example.py`](./advanced_sqlite_session_example.py) — Branching and usage analytics.

## OpenAI-managed history

- [`openai_session_example.py`](./openai_session_example.py) — `OpenAIConversationsSession`.
- [`compaction_session_example.py`](./compaction_session_example.py) — Auto-compaction with `OpenAIResponsesCompactionSession`.
- [`compaction_session_stateless_example.py`](./compaction_session_stateless_example.py) — Compaction with `ModelSettings(store=False)`.

## Custom and file-backed sessions

- [`file_session.py`](./file_session.py) — Reusable `FileSession` implementation for examples.
- [`file_hitl_example.py`](./file_hitl_example.py) — File-backed session with human-in-the-loop approvals.

## Human-in-the-loop across sessions

- [`memory_session_hitl_example.py`](./memory_session_hitl_example.py) — SQLite in-memory session with approvals.
- [`openai_session_hitl_example.py`](./openai_session_hitl_example.py) — OpenAI Conversations session with approvals.
- [`hitl_session_scenario.py`](./hitl_session_scenario.py) — Approval, rejection, and rehydration scenarios.