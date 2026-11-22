# Changes to Codex in the native SDK wheel

The SDK pins Codex in [Cargo.toml](../Cargo.toml). The wheel builder copies that revision into a temporary directory and applies these changes in filename order. It leaves the Cargo cache and any installed Codex CLI alone.

- [Detailed tracing](01-operation-tracing.md) observes the final model request and the start of tool execution.
- [Approval recovery](02-approval-recovery.md) lets a new worker resume a built-in command or edit after a human decision.

## Rules for every patch

Use a patch only for a capability the pinned Codex APIs cannot provide. Keep it as small as possible. Put SDK behavior and tests in ordinary source and test files.

Pair each `.patch` with a Markdown guide of the same name. Explain why the change is necessary, what it does, what can go wrong, how to test it and when to remove it. The build rejects missing or empty guides. The wheel includes each patch and guide in `agents/models/_codex_bin/licenses/codex-patches/`.

## Test a change

From the SDK repository root, use Python 3.12, uv, Git and the [native build prerequisites](../../../.github/workflows/codex-preview.yml). Build on the target machine. On Linux, install `patchelf` or add `--compatibility linux` for a local build.

```sh
uv sync --python 3.12
uv pip install --require-hashes -r native/codex/build-requirements.txt
uv run --no-sync python scripts/build_codex_wheel.py --profile dev --test-native --out dist/local
```

`--test-native` runs the SDK's Rust tests against the same source as the wheel. CI enables it for matrix entries with `test_native: true`. Every platform also runs the [installed-wheel checks](../verify_installed.py).

To check a local wheel on Linux or macOS, start with an empty `dist/local` and install the result in a fresh environment.

```sh
uv venv --python 3.12 .venv-wheel
uv pip install --python .venv-wheel/bin/python dist/local/*.whl
.venv-wheel/bin/python -I native/codex/verify_installed.py
```

When updating the Codex pin, remove each patch whose behavior is now available upstream. Rerun the same checks.
