# Tests

Use `uv` for test commands. Run `make sync` for a fresh checkout, changed dependencies, or a dependency-resolution failure; do not repeat setup before every check.

## Running tests

For provider-neutral agent workflow tests, prefer `ScriptedModel` from `agents.testing` instead of adding a new mock or fake `Model`. Use `ScriptedRealtimeModel` from `agents.realtime.testing` for Realtime session tests, the scripted utilities from `agents.voice.testing` for Voice pipeline tests, and `scripted_sandbox_session()` from `agents.testing` for deterministic Sandbox session calls. Keep a specialized test double only when the test specifically requires provider-wire conversion, malformed streams, controlled suspension or concurrency, or an exact cancellation or lifecycle boundary that the scripted utilities cannot preserve; document that boundary in the test.

```
make tests
```

`make tests` runs the shard-safe suite first with pytest-xdist using up to nine workers, then runs the tests marked `serial` after all xdist workers have exited. Set `PYTEST_XDIST_AUTO_NUM_WORKERS` to a positive integer to override the automatic worker count and cap. The serial runner limits collection to test files containing the literal `pytest.mark.serial`, so keep that literal marker in every file containing serial tests. For indirect or custom serial marker spellings, use `uv run pytest -m serial` to perform generic pytest collection.

The `serial` marker means that a test needs exclusive execution after every xdist worker exits, not merely ordered execution within one worker. Use it for shared external resources, process-wide state, or timing-sensitive lifecycle tests that have demonstrated interference under xdist. Tests that use their own subprocess, random port, or temporary directory do not need `serial` solely for that reason; prove them under xdist instead.

`make tests-review` omits tests marked `review_optional`. These slow subsystem-specific integration, subprocess, or multiprocessing checks remain mandatory in final `make tests` verification. This broad subset is not a replacement for the focused checks used during implementation review or for the final suite.

During iterative implementation review, run focused tests for the changed behavior and relevant subsystem boundaries, including applicable `review_optional` cases without filtering them away. Resolve uncertain coverage by tracing affected callers and dependencies and selecting the needed focused checks. Follow [implementation-final-review](../.agents/skills/implementation-final-review/SKILL.md) for review sequencing; defer broad `make tests-review`, `make tests`, and repository-wide type checking until clean review. Then follow [code-change-verification](../.agents/skills/code-change-verification/SKILL.md) for the complete final SDK stack. Explicit requests to run a suite outside an implementation review remain supported.

`make typecheck` runs mypy and pyright concurrently. Mypy checks `src`, while Pyright checks the `src` and `tests` paths configured in `pyrightconfig.json`. Pyright uses four analysis threads by default; set `PYRIGHT_THREADS` to a positive integer to override the local thread count.

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
make tests-parallel
```

Compare test counts, skips, warnings, assertions, and lifecycle coverage as well as elapsed time. Full-suite wall-clock results depend on host load and worker scheduling, so treat repeated focused measurements as the stronger evidence for an individual optimization. Run the repository's required verification stack after the final test changes.

Release compatibility unit tests must exercise policy and validation logic with explicit constructed modules instead of inspecting the current checkout's shared import state. The prospective release-contract job validates the current source checkout once in a dedicated Python process, and the packaged integration profiles validate real wheel, sdist, optional-extra, and platform surfaces in isolated environments. Keep the combined serial focused runtime of release compatibility unit tests below 1.5 seconds and each case below 100 milliseconds in normal conditions. Tests that build or install distributions, isolate imports in new interpreters, access the network, start containers, or require external services belong in `integration_tests/` instead. Run the concrete local Docker-backed contracts with `make integration-tests-containers`; ordinary `make tests` must not pull container images. The released API manifest permits compatible suffix additions and records enum member construction from `Enum.__new__` so CPython's version-dependent enum metaclass signature is not treated as an SDK change. Historical `RunState` fixtures must be produced by the recorded historical writer rather than by editing a current payload's schema version; the corpus README documents the explicit canonical-reader exception for schema versions that never had a corresponding writer.

The released API manifest is a rolling latest-release contract. After a release PR has updated `pyproject.toml`, check out the clean release branch locally and run `make update-released-api-contract VERSION=<version>` before requesting final review. The command first rejects any incompatibility with the committed contract, then freezes the candidate's current top-level exports, every inspectable top-level class or function signature and execution kind, and every newly exported SDK-owned inspectable class or function in a tracked public submodule. Qualified submodule callables remain tracked in later releases. Callable contracts include Pydantic model field names and defaults plus the binding, signature, and execution kind of each public callable member declared directly on an exported class or inherited from an SDK-owned base class. Exact Python functions are classified as synchronous functions, generators, coroutines, or asynchronous generators, and decorated functions use their caller-visible standard `inspect.signature()` contract. Methods declared only by third-party base classes and arbitrary descriptors remain outside the contract. Before regeneration, add newly documented class properties or factory-result properties to `public_properties`, selected public `TypedDict` fields to `public_typed_dicts`, newly intended cross-module import identities to `canonical_imports`, and optional module/export declarations to `modules` in `tests/fixtures/released_api_contract_policy.json`; those policy decisions are deliberately not inferred from implementation modules. The generator records each selected `TypedDict` field's requiredness and declared annotation without enrolling arbitrary `TypedDict` classes or fields. It merges the curated policy into the prospective and released contracts and freezes declared unsupported platforms so validation remains portable. The v0.19.4 baseline includes base-package submodule paths, canonical identities, and documented result properties used by its shipped docs and examples; optional-extra and experimental paths remain outside this base contract. After rebasing the release branch, run `make check-released-api-contract VERSION=<version>` and regenerate only when it reports drift. No GitHub workflow imports candidate code with write credentials for this update.

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
