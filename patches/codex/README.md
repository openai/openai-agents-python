# Codex Core operation observations

The Agents SDK has been embedding Codex Core `0.166.0` at commit `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. This patch has been exposing model and native tool observations through the embedded Rust binding. Python has been turning those observations into SDK spans for registered trace processors, including OpenTelemetry.

## Why the patch has been needed

Existing Core interfaces have been leaving gaps in operation tracing.

| Existing interface | Missing observation |
| --- | --- |
| Tool lifecycle hooks | General tool results and timing limited to handler execution after authorization |
| Session events | The final assembled inference request and its exact stream completion |
| File-backed inference traces | In-memory capture and one logical inference across identical-input transport retries |

The patch has been adding observations at those source boundaries. The maintained changes have been covering Core model requests, tool execution, bounded content capture, and response-body model metadata in `codex-api`. The SDK has been attaching an observer to each accepted turn through `WithTurnExtensionData`. Disabled tracing has been replacing the previous observer with an empty attachment.

## Build and verify the wheel

The commands below have been targeting a POSIX development host with Python 3.12, Git, `uv`, and `rustup`. Linux builds have been requiring a C/C++ compiler, CMake, `pkg-config`, Perl, and OpenSSL development libraries. The [preview workflow](../../.github/workflows/codex-preview.yml) has been supplying platform-specific prerequisites and release-compatible builds for the supported wheel targets.

Start at the SDK checkout root. Install the pinned build tools into a separate environment.

```sh
export SDK_ROOT="$PWD"
rustup toolchain install 1.95.0 --profile minimal --component rustfmt
uv venv --python 3.12 "$SDK_ROOT/.venv-codex-build"
uv pip install --python "$SDK_ROOT/.venv-codex-build" \
  --require-hashes --only-binary=:all: \
  --requirements native/codex/build-requirements.txt
```

On Linux, install `patchelf` into that environment for wheel repair.

```sh
uv pip install --python "$SDK_ROOT/.venv-codex-build" patchelf
```

Build the combined SDK wheel and run the native binding tests against the patched dependency graph.

```sh
export VIRTUAL_ENV="$SDK_ROOT/.venv-codex-build"
export PATH="$VIRTUAL_ENV/bin:$PATH"
export RUSTUP_TOOLCHAIN=1.95.0
export UV_NO_BUILD_ISOLATION=1
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_MIN_STACK=8388608
python scripts/build_codex_wheel.py --profile dev --test-native --out "$SDK_ROOT/dist"
```

The script has been copying the exact pinned upstream source into a temporary build directory and applying the checked-in patch there. Cargo source overrides have been selecting that patched workspace for the extension and execution helper. The wheel has been including the helper assets, licenses, source revision, patch, and this guide. For a release build, omit `--profile dev`.

Set `WHEEL` to the absolute wheel path printed by the build. Create a clean environment and run the installed-package verifier outside the checkout.

```sh
WHEEL=/absolute/path/to/built-wheel.whl
export VERIFY_ENV="$SDK_ROOT/.venv-codex-verify"
uv venv --python 3.12 "$VERIFY_ENV"
uv pip install --python "$VERIFY_ENV" "$WHEEL"
VERIFY_DIR=$(mktemp -d)
cd "$VERIFY_DIR"
"$VERIFY_ENV/bin/python" -I "$SDK_ROOT/native/codex/verify_installed.py" \
  --output "$VERIFY_DIR/installed.json"
```

The verifier has been checking the installed package, native imports, bundled helpers, authentication isolation, and real Runner behavior against a loopback model service. Python's `-I` option has been excluding checkout imports and ambient Python paths.

Install the test dependencies and run the live operation-tracing matrix with that same wheel.

```sh
uv pip install --python "$VERIFY_ENV" --only-binary=:all: \
  'pytest>=9.0.3' pytest-asyncio 'opentelemetry-sdk>=1.45.0,<2'
CODEX_TRACING_NATIVE_TEST=1 "$VERIFY_ENV/bin/python" -I -m pytest \
  "$SDK_ROOT/tests/models/test_operation_tracing_parity.py" \
  --import-mode=importlib -q
```

The matrix has been exercising both Runner entry points, capture permissions, model retries, SDK and native tools, source timings, and interruption through the compiled extension. Its model service has been using loopback HTTP with fixture responses. The installed-wheel matrix has been passing 46 cases without skips against the pinned Core revision. API credentials have been remaining unnecessary for these checks.

## Check the patch independently

Use a second, clean Codex checkout to check source application independently of wheel assembly. Set `CODEX_CHECKOUT` to a new absolute directory.

```sh
CODEX_CHECKOUT=/absolute/path/to/fresh-codex
PATCH_FILE="$SDK_ROOT/patches/codex/0001-operation-tracing.patch"
git clone --branch rust-v0.166.0 --single-branch \
  https://github.com/openai/codex.git "$CODEX_CHECKOUT"
cd "$CODEX_CHECKOUT"
test "$(git rev-parse HEAD)" = 6ff1e1ca3b30fc8b914720681fcf5fe62e95f856
test -z "$(git status --porcelain)"
git apply --check "$PATCH_FILE"
git apply "$PATCH_FILE"
git diff --check
cd codex-rs
cargo +1.95.0 test -p codex-core --lib operation_observation
cargo +1.95.0 test -p codex-core --lib mcp_tool_call_ids_are_added_to_request_meta
cargo +1.95.0 test -p codex-api --lib process_sse_reports_body_model_without_routing_authority
cargo +1.95.0 fmt --all -- --check
```

## Observation and capture contract

The `codex_core::operation_observation` module has been exposing the observer, event, phase, attachment, and capture-mode types. Core has been calling `emit(OperationEvent)` synchronously and awaiting `turn_finished` before the ordinary turn terminal. The binding has been owning private delivery, bounded queues, loss reporting, and ordering into Python.

The [binding observer](../../native/codex/src/operation_events.rs) has been providing a host integration example. Started and completed events have been carrying operation, thread, and turn identifiers with source timestamps. Available fields have been including parent identifiers, outcome, model metadata, usage, and tool identity. The Rust-only `phase` field has been selecting the Python envelopes `turn/operation/started` and `turn/operation/completed`.

| Mode | Captured content |
| --- | --- |
| Metadata | Model and tool facts without input or output |
| Content | Permitted input and output with a separate 32 KiB UTF-8 JSON budget for each direction |
| Disabled | No observer |

Raw strings that resemble JSON have been receiving a byte check before parsing. Typed values have been passing through capped serialization before capture has been allocating JSON. Oversized text has been retaining a bounded UTF-8 prefix with truncation markers. Structured values and media have been receiving omission markers. Request identity hashing has been borrowing prepared fields. The upstream `ToolOutput::code_mode_result` conversion has been remaining outside auxiliary capture-memory accounting.

Content has been staying in memory, outside rollout storage and diagnostic logs. HTTP and WebSocket observations have been including the final submitted input and continuation fields. Identical-input retries have been sharing one model operation. Changed semantic input has been starting another. Response-body model names have been supplying observation metadata without changing routing authority. Stream completion has been ending the model observation before session code has been draining tool futures.

Native tool observations have been measuring handler execution after authorization, including nested code-mode results. Python has been owning SDK function execution spans. Observer panics have been leaving model and tool execution unaffected, and operation guards have been producing one completion on success, error, or interruption.

## Prepare an upstream change

Keep maintained Rust source and tests in the patch. Leave local Cargo lock changes out. Refresh the patch from the pinned checkout after reviewing source changes.

```sh
cd "$CODEX_CHECKOUT"
base=6ff1e1ca3b30fc8b914720681fcf5fe62e95f856
git add -N codex-rs/core/src/operation_observation.rs \
  codex-rs/core/src/tools/operation_observation.rs \
  codex-rs/core/src/tools/operation_observation_tests.rs
git diff --check "$base"
candidate=$(mktemp "${PATCH_FILE}.XXXXXX")
git -c diff.suppressBlankEmpty=true diff --binary "$base" -- \
  codex-rs/core codex-rs/codex-api codex-rs/otel > "$candidate"
test -s "$candidate"
git apply --stat "$candidate"
mv "$candidate" "$PATCH_FILE"
```

Before opening an upstream PR, apply the refreshed patch in another clean checkout and rerun the Rust checks above. Include the pinned revision, observer API example, capture contract, source hashes, and actual source and installed-wheel test results in the PR. Follow the upstream repository's contribution instructions.

## Retire the patch

Wait for an upstream release with equivalent source facts, capture controls, and observer isolation. Run the installed-wheel matrix against that release and compare SDK and OpenTelemetry output for retries, native tools, interruption, concurrent capture permissions, and content canaries.

After those checks pass, update the Core pin and remove the patch and its build-time application together. Rerun source checks, native binding tests, installed-package verification, and live parity for the resulting wheel.
