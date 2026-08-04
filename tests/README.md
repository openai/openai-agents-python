# Tests

Before running any tests, make sure you have `uv` installed (and ideally run `make sync` after).

## Running tests

```
make tests
```

`make tests` runs the shard-safe suite in parallel and then runs tests marked `serial` in a separate serial pass.

## Performance and determinism

Tests should wait for observable state transitions rather than elapsed wall-clock time. Preserve the behavior and lifecycle branches under test when removing waits; a faster test is not equivalent if it replaces an active state with a completed state or bypasses the production finalization path.

Use these guidelines when adding or changing tests:

- Use events, deterministic fakes, immediate exceptions, and narrowly scoped mocks instead of real sleeps or retry backoff when elapsed time is not the behavior under test.
- Keep a real timeout or delay only when its duration semantics are the contract being tested. Use the smallest focused value that distinguishes the expected behavior.
- Preserve active, completed, failure, cancellation, and cleanup coverage as applicable. Release blocked tasks and clean up sessions, processes, and other resources in `finally` blocks so failed assertions cannot hang the suite.
- Parameterize cases that share the same setup, execution path, and assertions. Give each case a descriptive ID, and keep separate tests when their lifecycle or failure invariants differ.
- Capture expected warnings in the narrowest test with the specific warning category and a stable message match. Do not hide unrelated warnings with a global filter.
- Preserve subprocess isolation when import state, registration, shutdown, or interpreter lifecycle is under test. Instrument the exact side effect, such as construction or registration, instead of scanning the heap or waiting for it to occur.
- Run independent read-only subprocess or filesystem probes with bounded concurrency when useful. Keep cases that mutate shared fixtures, scripts, environment, ports, or external services sequential.
- Keep parallel tests shard-safe: avoid shared mutable global state, fixed writable paths, order dependence, and uncoordinated external resources. Mark a test `serial` only when isolation cannot express its required behavior.
- Keep timing and scheduler patches local to the test context, and continue exercising the production decision, retry, finalization, or cleanup path rather than replacing it wholesale.

Measure performance changes with both focused and broad runs:

```bash
uv run pytest tests/path/to/test_file.py --durations=10
uv run pytest -n auto --dist worksteal -m "not serial" --durations=20
```

Compare test counts, skips, warnings, assertions, and lifecycle coverage as well as elapsed time. Full-suite wall-clock results depend on host load and worker scheduling, so treat repeated focused measurements as the stronger evidence for an individual optimization. Run the repository's required verification stack after the final test changes.

## Snapshots

We use [inline-snapshots](https://15r10nk.github.io/inline-snapshot/latest/) for some tests. If your code adds new snapshot tests or breaks existing ones, you can fix/create them. After fixing/creating snapshots, run `make tests` again to verify the tests pass.

### Fixing snapshots

```
make snapshots-fix
```

### Creating snapshots

```
make snapshots-create
```
