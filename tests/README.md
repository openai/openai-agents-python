# Tests

Before running any tests, make sure you have `uv` installed (and ideally run `make sync` after).

## Running tests

### Unit tests (default)

Runs all tests except integration tests (which require Docker):

```bash
make tests
# or explicitly
make tests-unit
```

### Integration tests

Runs integration tests that require Docker (e.g., Dapr, Redis containers):

```bash
make tests-integration
```

**Note**: Integration tests use `testcontainers` to automatically manage Docker containers. Ensure Docker is running before executing integration tests.

### All tests

Runs all tests including integration tests:

```bash
make tests-all
```

### Running specific tests

To run a single test by name:

```bash
uv run pytest -s -k <test_name>
```

To run integration tests for a specific module:

```bash
uv run pytest -m integration tests/extensions/memory/
```

## Snapshots

We use [inline-snapshots](https://15r10nk.github.io/inline-snapshot/latest/) for some tests. If your code adds new snapshot tests or breaks existing ones, you can fix/create them. After fixing/creating snapshots, run `make tests` again to verify the tests pass.

### Fixing snapshots

```
make snapshots-fix
```

### Creating snapshots

```
make snapshots-update
```
