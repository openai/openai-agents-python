# Codex operation tracing patch guide

This patch adds private model and native tool observations to Codex 0.166.0. See the validation record for completed checks.

Apply `patches/codex/0001-operation-tracing.patch` to `rust-v0.166.0` at `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. Keep Python responsible for canonical Agents SDK spans and all OpenTelemetry export. Supply Rust observations for the active turn and nested native tools. Exclude independent agents and gateway propagation. Keep Rust exporter setup and PyO3 bindings outside this patch.

## Wire contract

Add an experimental `operationTracing` field to `turn/start` with `metadata` or `content`. Echo the effective mode in the reply. Ask for metadata by default when Python tracing is active. Python already holds the SDK-visible input and reply and can populate the existing SDK fields locally. Ask for content only with explicit per-run `"content"` and permission to trace sensitive data. With tracing disabled, omit the field and emit zero observations. Identify stock fallback as partial coverage.

Emit `turn/operation/started` immediately, followed by `turn/operation/completed`. Include `operationId`, `threadId`, `turnId`, optional `parentOperationId`, and a `model` or `tool` kind. Identify each nested tool's parent. Include source start and end times, elapsed time from a monotonic clock, and a `success`, `error`, or `interrupted` outcome. Include the model name, source response ID subject to the SDK trust policy, and per-call usage when available. For a tool, include its name and call ID.

Metadata example without captured content.

```json
[
  {
    "method": "turn/operation/started",
    "params": {
      "operationId": "op-1",
      "threadId": "thread-1",
      "turnId": "turn-1",
      "kind": "model",
      "requestedModel": "example-model",
      "startedAt": "2026-11-20T00:00:00.000Z"
    }
  },
  {
    "method": "turn/operation/completed",
    "params": {
      "operationId": "op-1",
      "threadId": "thread-1",
      "turnId": "turn-1",
      "kind": "model",
      "requestedModel": "example-model",
      "startedAt": "2026-11-20T00:00:00.000Z",
      "completedAt": "2026-11-20T00:00:01.000Z",
      "durationMs": 1000,
      "outcome": "success"
    }
  }
]
```

Extend `TurnStartParams`, `TurnStartResponse`, and the notification registry. Keep payload fields exclusive to effective content mode. [Turn protocol](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/protocol/v2/turn.rs) · [Notification registry](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/protocol/common.rs)

## Source observations

Capture HTTP and WebSocket requests after final assembly and input bounding in `core/src/client.rs`. Include exact submitted values within the capture budget and `previous_response_id` for continuation. Label wire deltas separately from expanded context. Group identical-input retries under one operation and split after changed assembled input. End model timing at stream termination before draining tools. Preserve requested and observed model identity when available. [Client](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/client.rs) · [Sampling](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/session/turn.rs) · [Retry guidance](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/responses_retry.rs)

Observe native execution at the tool registry, with deferred start points immediately after authorization for sandboxed tools and MCP. Project actual results at the registry result boundary, including nested code-mode results. Keep dynamic requests as Python-owned callback interrupts and trace callback execution in Python. [Lifecycle types](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/ext/extension-api/src/contributors/tool_lifecycle.rs) · [Registry](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/registry.rs)

## Privacy and lifecycle

Bind observations before sampling to the initiating opted-in connection. Deliver exclusively to that connection, including multiple-subscriber cases. Keep captured data in memory and exclude it from rollout persistence and logs. Existing outgoing helpers have been logging notifications and broadcasting to empty recipient lists. Add explicit targeting and redacted logging. [Outgoing delivery](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/outgoing_message.rs)

Enforce a 32 KiB UTF-8 serialized capture budget per direction per operation before sending. Mark missing content with `omitted` and shortened content with `truncated`, including the reason. Describe completeness accurately.

Use one finalizer per operation. Source turn completion closes private queue admission and awaits its drain before publishing `turn/completed`, including interrupted turns. Empty turns also close their observers. On child-process termination without a Rust terminal, close open IDs in Python with interruption and incomplete-observation metadata.

## Build and checks

Use the pinned Rust 1.95.0 toolchain and build prerequisites, including `just`. Replace the absolute paths below. Require a fresh checkout and the exact SHA before application. [Installation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/docs/install.md)

```sh
set -eu
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_MIN_STACK=8388608
SDK_ROOT=/path/to/sdk
CODEX_CHECKOUT=/path/to/fresh-codex
PATCH_FILE="$SDK_ROOT/patches/codex/0001-operation-tracing.patch"
git clone --branch rust-v0.166.0 --single-branch https://github.com/openai/codex.git "$CODEX_CHECKOUT"
cd "$CODEX_CHECKOUT"
test "$(git rev-parse HEAD)" = 6ff1e1ca3b30fc8b914720681fcf5fe62e95f856
test -z "$(git status --porcelain)"
git apply --check "$PATCH_FILE"
git apply "$PATCH_FILE"
just write-app-server-schema
just write-app-server-schema --experimental
cd "$CODEX_CHECKOUT/codex-rs"
cargo build -p codex-cli --bin codex
CODEX_TRACING_TEST_EXECUTABLE="$CODEX_CHECKOUT/codex-rs/target/debug/codex"
"$CODEX_TRACING_TEST_EXECUTABLE" --version
cd "$CODEX_CHECKOUT"
just fmt-check
git diff --check
cd "$CODEX_CHECKOUT/codex-rs"
cargo test -p codex-core -p codex-app-server --lib operation_observation
cargo test -p codex-core --lib new_turn_only
cargo test -p codex-app-server-protocol --lib
cargo test -p codex-api --lib process_sse_reports_body_model_without_routing_authority
cd "$SDK_ROOT"
CODEX_TRACING_TEST_EXECUTABLE="$CODEX_TRACING_TEST_EXECUTABLE" uv run --extra opentelemetry python -m pytest tests/models/test_operation_tracing_parity.py
```

For the optional real-stock gate on Linux x86_64, download the pinned official asset below. Choose the corresponding platform asset from the same release on other hosts. Keep the patched executable selected separately.

```sh
STOCK_DIRECTORY=/path/to/codex-stock-0.166.0
mkdir -p "$STOCK_DIRECTORY"
curl --fail --location --output "$STOCK_DIRECTORY/codex.tar.gz" \
  https://github.com/openai/codex/releases/download/rust-v0.166.0/codex-x86_64-unknown-linux-musl.tar.gz
tar -xzf "$STOCK_DIRECTORY/codex.tar.gz" -C "$STOCK_DIRECTORY"
CODEX_TRACING_STOCK_EXECUTABLE="$STOCK_DIRECTORY/codex-x86_64-unknown-linux-musl"
"$CODEX_TRACING_STOCK_EXECUTABLE" --version
CODEX_TRACING_TEST_EXECUTABLE="$CODEX_TRACING_TEST_EXECUTABLE" CODEX_TRACING_STOCK_EXECUTABLE="$CODEX_TRACING_STOCK_EXECUTABLE" uv run --extra opentelemetry python -m pytest tests/models/test_operation_tracing_parity.py
```

Require the executable and mode acknowledgment in full integration tests. Cover default, disabled, metadata, and content modes with SDK tracing and OpenTelemetry separately and together. Verify starts, retries, nested tools, cancellation, source timing, and owner isolation. Use a synthetic canary to detect content in other clients, logs, or stored telemetry. Keep Rust regressions inside the patch.

The executable embeds generated exports, so run both generators before building. The shipped patch contains hand-maintained source and tests only. The stable generator updates JSON/TypeScript fixtures, stable exports, and Python SDK types. The experimental generator updates `codex-rs/app-server-protocol/schema/precomputed/app-server-exports-experimental.json.zst`. Include generated outputs in the future upstream Codex PR. [Schema generator](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/scripts/write_schema_fixtures.py)

## Validation

Require the shared matrix to pass against the selected patched binary. With `CODEX_TRACING_STOCK_EXECUTABLE`, also verify explicit partial coverage on stock 0.166.0. Check live start/terminal ordering and closure after child-process interruption. The steering regressions must preserve the current turn's owner, reject future observation authority from an unacknowledged steer, and retain the existing `WithTurnExtensionData::new` behavior for other callers.

Verify a fresh apply followed by both generators against the complete source tree used for the build. Keep hashes, timings, logs, and the resulting tree comparison in the PR validation record.

## Refresh and upstream handoff

Review source, tests, and generated outputs in the Codex checkout. Mark new Rust files with `git add -N` before refreshing the SDK-maintained patch. Keep generated files available for the future upstream PR and exclude them from this reviewable patch. Use the paths from the build commands above.

```sh
set -eu
cd "$CODEX_CHECKOUT"
base=6ff1e1ca3b30fc8b914720681fcf5fe62e95f856
git diff --check "$base"
candidate=$(mktemp "${PATCH_FILE}.XXXXXX")
git -c diff.suppressBlankEmpty=true diff --binary "$base" -- . \
  ':!codex-rs/Cargo.lock' \
  ':!codex-rs/app-server-protocol/schema/**' \
  ':!sdk/python/src/openai_codex/generated/**' > "$candidate"
test -s "$candidate"
git apply --stat "$candidate"
mv "$candidate" "$PATCH_FILE"
```

Repeat the clone, apply, build, and checks with another unused checkout path. Prepare an upstream handoff containing the patch, base revision, wire examples, privacy decisions, and recorded validation. Remove the local patch after an upstream release with equivalent behavior passes integration. Retain capability negotiation and partial fallback during migration.
