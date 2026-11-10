# Keep explicit facts across native Sessions

Use `agents.memory.SQLiteMemory` to save named string values between separate native Sessions. The application selects a database path and a trusted scope, such as an authenticated user ID. Each store keeps that scope for its lifetime. Its tools let the model select only a key and, when saving, a value.

## Run the example

Use Python 3.10 through 3.14 and an SDK environment from this checkout with its matching native extension. Follow the [source setup](../../packages/codex/MAINTAINING.md#build-from-this-source) if you have not prepared that environment. The example uses Python's built-in SQLite module and needs no extra database package or provider key.

From the repository root, run:

```sh
PYTHONPATH=. uv run --no-sync python -m examples.codex.packaged_memory
```

The [complete example](packaged_memory.py) creates a temporary database and uses a local synthetic Responses endpoint with the real native engine. Each request opens a fresh Agent, store, and Session. The example closes the Session and store after saving, then opens them again to recall the fact. A second scope cannot read the first scope's value. Finally, the example deletes the value and checks another fresh store.

Expected output:

```text
Alice after reopen: Plain language.
Bob: no saved value
Alice after delete and reopen: no saved value
```

The example removes its temporary database on exit. Keep an application-owned private path when you need the facts to survive the program.

## Use the store in an application

Import `SQLiteMemory` from `agents.memory`. Open it with `SQLiteMemory(path, scope=trusted_scope)` and pass `memory.tools()` to `Agent(tools=...)`. The tools are `memory_get`, `memory_set`, and `memory_delete`. Finish all native work that uses these tools before closing the store. A `with` block closes it, and repeated `close()` calls are safe.

Your application can call `get(key)`, `set(key, value)`, and `delete(key)` directly. A missing key returns `None`; an empty string remains a saved value. Deleting an absent key succeeds. Each write commits before returning. Scope and key must each contain 1 to 256 characters and cannot be blank. Values can contain up to 65,536 characters. Operations on a closed store fail.

Scope, key, and value must contain well-formed Unicode. The store rejects surrogate code points (`U+D800` through `U+DFFF`) before opening a database or executing a statement. Characters such as emoji and the literal replacement character (`U+FFFD`) remain unchanged.

Choose the scope from your authenticated application context. A scope separates rows in the same database. It does not authenticate a user. Other processes with access to the database file can read saved facts. Your application controls tool access and file permissions. Treat recalled text as data when constructing instructions or displaying results.

The store saves explicit values. Your application supplies any extraction, search, encryption, expiry, or retention policy. A failed model run does not roll back a fact that a tool already saved. Deleting a value leaves any copies in model history, checkpoints, files, and exported telemetry under their own retention rules.
