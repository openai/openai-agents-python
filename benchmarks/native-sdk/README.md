# Stable versus native SDK benchmark

This directory contains a local SDK benchmark, its process-ownership helper and a source SHA-256 manifest. It compares stable `openai-agents==0.24.0` with a wheel built from a separately reviewed native SDK checkout. It uses a separate local Responses peer and synthetic credentials; no live model is called. It exercises public SDK APIs, without private dispatch or experimental transport flags.

Use Linux and the same standard CPython build for both SDK environments. The prepared configuration used CPython 3.13.16 on x86-64 Linux. This bundle has not been run on a new machine. Native wheels must match the host architecture, ABI and glibc. `resource.getrusage`, process groups and load averages make this a Linux reproducer.

## Prepare three isolated environments

Run the commands from this benchmark checkout. Set `NATIVE_SOURCE` to a separate clean checkout of the intended native SDK revision; that revision need not contain this benchmark directory. Install the source-build prerequisites described in `CONTRIBUTING.md` and `packages/codex/MAINTAINING.md`: Git, uv, Rust 1.95.0, Clang/CMake, pkg-config, protoc and OpenSSL development headers. Build each revision/source treatment with its own initially empty `CARGO_TARGET_DIR`; sharing the `CARGO_HOME` download cache is fine. These commands are setup instructions, not recorded successful builds.

```sh
BENCH="$PWD/benchmarks/native-sdk"
NATIVE_SOURCE="/absolute/path/to/native-sdk-checkout"
LAB="$(dirname "$NATIVE_SOURCE")/agents-benchmark-lab"
mkdir -p "$LAB/wheels"
git -C "$NATIVE_SOURCE" rev-parse HEAD > "$LAB/native-source-commit.txt"
python3 "$NATIVE_SOURCE/packages/codex/scripts/prepare_codex.py"
TARGET="$(mktemp -d "$LAB/cargo-target.XXXXXX")"
(cd "$NATIVE_SOURCE" && CARGO_TARGET_DIR="$TARGET" uv build --wheel --out-dir "$LAB/wheels")

# Select the exact produced wheel; do not select a stale wheel with a glob.
WHEEL="$LAB/wheels/<exact-native-wheel-filename>.whl"
sha256sum "$WHEEL" > "$LAB/native-wheel.sha256"
BASE_PY="$(command -v python3.13)"
uv venv --python "$BASE_PY" "$LAB/stable"
uv venv --python "$BASE_PY" "$LAB/native"
uv venv --python "$BASE_PY" "$LAB/peer"
STABLE_PY="$LAB/stable/bin/python"
NATIVE_PY="$LAB/native/bin/python"
PEER_PY="$LAB/peer/bin/python"
uv pip install --compile-bytecode --python "$STABLE_PY" 'openai-agents==0.24.0' \
  'openai==3.29.0' 'pydantic==2.12.3' 'typing-extensions==4.16.0'
uv pip install --compile-bytecode --python "$NATIVE_PY" "$WHEEL" \
  'pydantic==2.12.3' 'typing-extensions==4.16.0' \
  'jsonschema==4.26.0' 'referencing==0.37.0'
NATIVE_VERSION="$("$NATIVE_PY" -c 'from importlib.metadata import version; print(version("openai-agents"))')"
uv pip install --compile-bytecode --python "$PEER_PY" \
  'aiohttp==3.14.3' 'openai==3.41.0' 'pydantic==2.12.3'
```

The wheel hook builds the native extension and matching execution helpers. Install that exact wheel into the fresh environment; do not substitute an editable checkout or an unrelated native binary. The dependency versions above come from existing compatible environment records. They are not complete dependency locks. Keep the wheel hash, source commit, Python build and complete installed package inventories with results. Use `uv pip freeze --python <interpreter>` to record each environment. If registry access is unavailable, supply the exact stable release wheel locally.

Compile bytecode for every environment before measurement, as shown above. Workers and peers run with `-B` to prevent cache writes; they still read existing caches. Unequal cache coverage can change first-call CPU even when package versions match. Keep before/after inventories of installed Python sources and valid, non-optimized bytecode caches, including their interpreter cache tag and source validation. Worker rows record the effective cache settings, but those settings alone do not prove complete or equal cache coverage.

## Verify and run a small correctness check

Before execution, verify the eight source hashes and inventory against `benchmark-source-manifest.json`. Run ordinary Python without optimization or instrumentation. The controller and worker reject known environment overrides, even when the variables are empty. If a command reports `Unset inherited benchmark overrides`, unset every variable it lists and run the command again. `PYTHONSAFEPATH` and the controller's fixed `PYTHONHASHSEED=0` remain supported.

```sh
"$PEER_PY" - "$BENCH" <<'VERIFY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
expected = json.loads((root / "benchmark-source-manifest.json").read_text())
if set(expected) != {p.name for p in root.glob("*.py")}:
    raise SystemExit("Unexpected Python inventory")
for name, digest in expected.items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
        raise SystemExit("Source hash mismatch: " + name)
print("PASS: eight source hashes")
VERIFY
(
  unset PYTHONPATH PYTHONHOME PYTHONOPTIMIZE LD_PRELOAD LD_AUDIT
  unset PYTHONTRACEMALLOC PYTHONPROFILEIMPORTTIME CPUPROFILE
  unset CPUPROFILE_PER_THREAD_TIMERS CPUPROFILE_TIMER_SIGNAL CPUPROFILESIGNAL
  unset RUST_MIN_STACK TOKIO_WORKER_THREADS SDK_BENCH_EVENT_MODE
  unset SDK_NATIVE_FIXTURE_BOUNDARY_TRACE OPENAI_AGENTS_NATIVE_WHEEL
  "$PEER_PY" "$BENCH/validate_peer.py" &&
  "$PEER_PY" "$BENCH/compare.py" \
    --v0-python "$STABLE_PY" --v1-python "$NATIVE_PY" \
    --v0-version '0.24.0' --v1-version "$NATIVE_VERSION" \
    --server-python "$PEER_PY" --output-dir "$LAB/smoke-new" \
    --samples 1 --iterations 2 --timeout 60 \
    --cases oneshot_text oneshot_tool warm_stream
)
```

The output directory must be new. Expect validator PASS, six successful worker results and zero worker/peer validation errors. One sample per side intentionally reports insufficient samples; it is not a parity test. For a scheduled comparison, retain the provided case defaults, use at least five independent process samples, record iteration overrides and keep the host quiet. Selected cases do not establish whole-suite parity. Do not use cProfile timings for latency claims.

## Interpret the scope

### Requests and framing

The adapters have been fixing the model identifier to `gpt-5.1-codex`, instructions and tool schema, without a model/effort override. For caller-owned loopback final-only calls, stable has been using JSON while native has been using SSE by default. The peer has been performing that framing work, so large-output wall time has not been isolating client cost. Streamed cases have been validating chunks, and history/tool cases have been verifying retained request contents and callbacks. The optional default-host profile below has been checking the selected native wheel's actual HTTP/SSE or WSS route.

### Version and source identity

The controller requires explicit expected SDK versions and checks each worker's actual version before accepting the row. Match `importlib.metadata.version('openai-agents')` exactly: native metadata can say `0.24.0-alpha.6` while its wheel filename uses `0.24.0a6`. The controller deliberately does not normalize those spellings.

The two environments must report the same CPython build/ABI fields, pydantic version and typing-extensions version. Within each environment, SDK, module, Python and dependency identities must stay the same across rounds and cases. SDK and OpenAI dependency versions need not match across arms. Scheduled workload parameters and existing operation/tool-count receipts must match; the worker remains the authority for exact request/output/history/tool checks. The controller rehashes the sibling harness at the end, including failed runs.

It does not bind installed Python/native files to wheel members, hash the interpreter executable or prove the source commit. Before making commit-specific claims, verify installed wheel members and retain before/after evidence plus complete dependency inventories. The runner's output/history/tool checks do not establish full model/instruction/settings request equivalence.

### Process lifetime

Each worker runs in a dedicated process group; its peer inherits that group. The controller defers TERM/INT across spawn handoff and bounded teardown, then always retires the group, even if its leader exits first. The standalone validator owns its peer group, bounds readiness to 10 seconds and 64 KiB, then uses TERM, KILL and reap. Startup, teardown and OS failures can still fail a run; require joined exit 0, not just a saved JSON file. No SDK or performance qualification of this source bundle is claimed.

### Memory and timing

Raw `max_rss_kib` is explicitly unqualified Linux `RUSAGE_SELF.ru_maxrss`. It includes import/setup/validation and may inherit a parent high-water floor. It remains in individual worker data, but never enters summary comparisons, bootstrap intervals or verdicts. A memory comparison needs a separate qualified method.

Operation timers include actual run/submit-result work; prefill and later checks are outside measured CPU/wall spans. Conversation opening/final close are separate, while a one-shot run includes its own Session lifetime. First-result latency starts at adapter import/setup, not interpreter or peer startup. Large-output peer framing work remains part of observed wall time.

### Paired statistics

Each round supplies a matched v0/v1 process pair identified by sample index. The fixed-seed 10,000-resample bootstrap draws those pairs together and computes the ratio of their process medians. It rejects missing/duplicate pairs and unequal, empty, nonfinite or nonpositive metric vectors. Summaries retain every raw per-side metric value in pair order. Pairing retains observed shared round variation; it does not eliminate host drift, order effects or multiple exploration. One pair gives a degenerate interval, not uncertainty evidence.

Five-pair defaults remain exploratory; the parity option is an engineering non-regression rule, not proof of behavioral equivalence or guaranteed 95% coverage.

### Compare with JavaScript

Compared with the JS benchmark, this harness keeps a fresh peer per worker rather than a persistent fixture. Their JIT/heap/cache lifetimes therefore differ. Python operation p95 uses linear interpolation at `(n-1)*p`; bootstrap bounds retain the existing order-statistic indices `floor((B-1)*p)`. JS uses its own order-statistic quantiles. Do not silently treat these percentile definitions as identical. Neither these paired statistics nor raw high-water rows qualify a memory comparison.

## Focused checks and diagnostic profiling

The focused tests have been using the peer environment's existing aiohttp dependency. They have been checking statistics, adapter ownership and WSS tool continuation without importing either SDK or launching benchmark workers:

```sh
"$PEER_PY" -m unittest discover -s "$BENCH/tests" -v
```

The optional native-SDK cursor check uses the loopback peer to verify a retained conversation beyond the observation buffer's retention and rejection of a genuine SDK gap. It reports correctness only:

```sh
uv pip install --compile-bytecode --python "$NATIVE_PY" 'aiohttp==3.14.3'
"$NATIVE_PY" "$BENCH/tests/native_stream_cursor.py" -v
```

Direct cProfile collection remains available after clearing the same inherited controls:

```sh
SDK_BENCH_SERVER_PYTHON="$PEER_PY" "$NATIVE_PY" "$BENCH/worker.py" \
  --engine v1 --case oneshot_text --iterations 2 \
  --profile "$LAB/diagnostic.prof" --output "$LAB/diagnostic.json"
```

A `--profile` row is explicitly diagnostic. The comparison rejects diagnostic/profiled/instrumented rows rather than mixing them into latency/CPU summaries. Profiling retains the existing exercise window, which includes setup/warmup/close; instrumented counts or times are not uninstrumented latency shares.

Generated metadata, errors and logs can contain local absolute paths. Review and separately sanitize a public export, preserving original hashes and recording the export's own digest. This directory contains no historical raw results, profiles, installed environments or credentials. Record the source-manifest hash with results; a manifest identifies source bytes, not a performance qualification.

## Optional native buffered Responses mode

The default command keeps the native provider's streaming transport and omits the new provider keyword, so it can still benchmark older native SDK wheels. A native wheel that supports `NativeProvider(response_mode="buffered")` can opt into final-result HTTP JSON delivery with `compare.py --v1-response-mode buffered`. Unsupported wheels fail explicitly; the benchmark never retries in another mode. The stable v0 adapter uses SSE for live workloads and JSON for final-result workloads. Its behavior does not change when selecting the native mode.

Select only final-result workloads for a buffered comparison, for example:

```sh
"$PEER_PY" "$BENCH/compare.py" \
  --v0-python "$STABLE_PY" --v1-python "$NATIVE_PY" \
  --v0-version 0.24.0 --v1-version "$NATIVE_VERSION" \
  --server-python "$PEER_PY" --output-dir "$LAB/buffered" \
  --v1-response-mode buffered --samples 5 --iterations 20 \
  --cases oneshot_text oneshot_tool oneshot_output_64k_1_plain \
          oneshot_output_64k_256_plain oneshot_output_64k_4096_plain
```

Omit `--v1-response-mode buffered` and use a fresh output directory for the default caller-owned loopback SSE control. Keep the same source, wheel, dependencies, interpreter, CA and host conditions. The three 64 KiB cases are final-only `Agent.run` calls in independent fresh conversations, with one prefill and 20 measured calls by default. Their chunk setting controls the SSE reference workload; a JSON response has zero SSE chunks. This removes peer framing work too, so wall-time differences do not isolate client processing. Client process CPU and peer CPU remain separate.

For a paired comparison of two native modes, `--reference-engine v1 --reference-response-mode streaming --v1-response-mode buffered` has been selecting SSE for the reference and JSON for the target. Without `--reference-response-mode`, both native arms have been inheriting `--v1-response-mode`. The following command has been using the same installed native wheel on both sides, with one caller-owned Provider per Adapter and fresh conversations per call:

```sh
"$PEER_PY" "$BENCH/compare.py" \
  --reference-engine v1 --reference-response-mode streaming --v1-response-mode buffered \
  --native-provider-mode caller-owned --native-transport http \
  --v0-python "$NATIVE_PY" --v1-python "$NATIVE_PY" \
  --v0-version "$NATIVE_VERSION" --v1-version "$NATIVE_VERSION" \
  --server-python "$PEER_PY" --output-dir "$LAB/native-response-modes-new" \
  --samples 5 --iterations 20 --cases oneshot_tool_output_16k_128
```

This loopback comparison has been excluding default-host TLS and implicit-provider cache behavior. The controller has been recording each arm's response mode in metadata and summaries, and checking it against that arm's worker and peer receipts. Buffered mode has been remaining restricted to final-result workloads outside the default-host profile.

Rows record the native response mode and expected wire transport, checked against all actual request receipts, per-response chunk counts and total emitted chunks. Both modes retain the same final output, input/history and tool-result checks. Buffered comparisons reject live-observation workloads. They report no first-delta metric or token-live claim; buffered progress arrives after the complete body. Existing live streaming cursor, gap and exact chunk checks remain unchanged.

`elapsed_ms`, `cpu_ms` and latency p50/p95 have been covering measured operations after prefill. For oneshot cases, these have been measuring warmed, fresh conversations. `full_owner_wall_ms` and `full_owner_cpu_ms` have been measuring Adapter lifetime from construction through exit, including prefill, ordinary client scheduling, prompt work, in-scope peer-stat requests, caller-owned Provider close when present, and telemetry shutdown. The `full_owner_scope: "adapter-lifetime"` label has been excluding SDK import, peer startup, final correctness validation, and process-final cache cleanup. These overlapping spans have been supplying separate measurements, without addition to the existing metrics. Failed runs have not been qualifying.

`charged_operations = concurrency * (prefill + iterations)` counts turns, including prefill. It counts fresh conversations only for oneshot cases; conversation cases reuse Sessions. Summaries apply the existing paired estimator to `full_owner_wall_ms_per_operation` and `full_owner_cpu_ms_per_operation`, retaining raw totals and matched vectors. Import-only rows have no owner/operation/transport metrics. RSS stays unqualified and does not enter comparisons. The existing confidence-interval limitations and engineering parity rule still apply.

## Focused native WebSocket reproduction

The `--reference-engine v1` option has been selecting a native adapter for the reference slot named `--v0-python`. The target slot has been continuing to use the native adapter. In the caller-owned loopback examples below, both adapters have been retaining one Provider while each `Agent.run` has been opening a fresh conversation. HTTP has been remaining that configuration's default transport.

The loopback WebSocket example below has been selecting `--cases oneshot_text` with streaming response mode. The peer has been keeping warmup and generating history local to each socket, and rejecting continuation from another response. The default-host fresh-tool cases below have been exercising bounded tool continuation on that same peer. Worker validation has been requiring WebSocket receipts and joined socket retirement. Warmup frames have been appearing separately as `websocket_warmups`, outside the generating-request count.

Prerequisites: the same interpreter, dependency and bytecode conditions described above; two native-wheel environments with recorded source commits and wheel hashes. `REFERENCE_NATIVE_PY` has been naming the reference environment's Python, and `NATIVE_PY` has been naming the candidate environment's Python. The peer has been continuing to run in its separate process. Existing override-clearing instructions have been applying to both commands.

```sh
# Select the exact reference wheel produced from its recorded source revision.
REFERENCE_WHEEL="$LAB/reference-wheels/exact-reference-native-wheel.whl"
sha256sum "$REFERENCE_WHEEL" > "$LAB/reference-native-wheel.sha256"
uv venv --python "$BASE_PY" "$LAB/reference-native"
REFERENCE_NATIVE_PY="$LAB/reference-native/bin/python"
uv pip install --compile-bytecode --python "$REFERENCE_NATIVE_PY" "$REFERENCE_WHEEL" \
  'pydantic==2.12.3' 'typing-extensions==4.16.0' \
  'jsonschema==4.26.0' 'referencing==0.37.0'
REFERENCE_NATIVE_VERSION="$("$REFERENCE_NATIVE_PY" -c 'from importlib.metadata import version; print(version("openai-agents"))')"

"$PEER_PY" "$BENCH/validate_peer.py" --check-websocket
"$PEER_PY" "$BENCH/compare.py" \
  --reference-engine v1 --native-transport websocket \
  --v0-python "$REFERENCE_NATIVE_PY" --v1-python "$NATIVE_PY" \
  --v0-version "$REFERENCE_NATIVE_VERSION" --v1-version "$NATIVE_VERSION" \
  --server-python "$PEER_PY" --output-dir "$LAB/native-websocket" \
  --samples 5 --iterations 100 --timeout 60 --cases oneshot_text
```

Native-reference summaries have been labeling measurements `reference` and `target`, including `ratio_target_over_reference`. The alternating process order, all-thread client process CPU, full-owner measurements and paired bootstrap have been remaining unchanged. Peer CPU has been remaining separate from client CPU. Five samples have been supplying an exploratory comparison, without reproducing a larger fixed multi-arm qualification.

A separate context command has been comparing stable v0 over HTTP with the native candidate over WebSockets. Its output directory has been keeping that context distinct from the native before/after comparison:

```sh
"$PEER_PY" "$BENCH/compare.py" \
  --native-transport websocket \
  --v0-python "$STABLE_PY" --v1-python "$NATIVE_PY" \
  --v0-version 0.24.0 --v1-version "$NATIVE_VERSION" \
  --server-python "$PEER_PY" --output-dir "$LAB/stable-http-context" \
  --samples 5 --iterations 100 --timeout 60 --cases oneshot_text
```

## Optional default-host container smoke

The [default-host container guide](container-default-host/README.md) has been supplying an offline functional check against a local TLS peer at the normal API hostname. Its image setup has been provisioning matching Python environments and explicit certifi prerequisites. Scope: fresh native HTTP/SSE calls or omitted-Provider WSS calls, with no tools or one asynchronous function tool. The optional smoke has been supplying correctness checks, without a performance claim.

`--native-provider-mode omitted` has been selecting the public `Agent.run` entrypoint without constructing a caller-owned `NativeProvider` or passing the Agent's `provider` argument. The default `caller-owned` mode has been retaining one Provider per worker. Both modes have been opening a fresh conversation for every oneshot call; neither has been reusing conversation history. The single flag has been applying to every v1 arm, including `--reference-engine v1`, while leaving the v0 adapter unchanged. Worker rows and comparison summaries have been recording `native_provider_mode`.

Omitted mode has been accepting the default-host profile's fresh no-tool cases and one asynchronous function tool per call. No-tool cases have been including cold, large-input and oneshot concurrent calls. The tool profile has been excluding retained conversations, synchronous tools, fanout and multiple tool rounds. Live observation has been using the one-tool active-reader case. The worker has been discarding inherited credentials and setting only `OPENAI_API_KEY=synthetic-benchmark-key` before importing the SDK. The [complete container example](container-default-host/README.md#run-one-offline-functional-smoke) has been selecting this mode without a custom endpoint or trust override.

In default-host omitted mode, `--native-transport` has been declaring the target wheel's expected default route, without changing the SDK transport. `--reference-transport` has been declaring that route for a v1 reference arm. A WSS reference and an HTTP/SSE target have been remaining comparable with the same omitted-Provider entrypoint. Standalone summaries have been recording both effective transports. Caller-owned default-host mode has been continuing to accept only HTTP/SSE.

`--native-request-transport {auto,http,websocket}` has been selecting the per-run `ModelSettings.request_transport` for every native arm, including `--reference-engine v1`. This option has been requiring fresh calls with an omitted Provider in default-host mode. The v0 adapter has been keeping its existing behavior.

Omitting the option has been preserving prompt-only `Agent.run` calls and the SDK's default transport selection. `--native-transport` and `--reference-transport` have been checking the observed wire independently. Rows, metadata, and summaries have been recording `native_request_transport` separately from those expectations. Explicit HTTP requests have been using the omitted Provider and normal API hostname.

In omitted mode, `Agent.run` has been owning each call's Provider creation and close inside the call measurement. Process-owned caches have been remaining outside the Adapter's final-close ownership. The existing all-thread process CPU clocks have been retaining their measurement windows; `full_owner_*` has not been claiming process-exit CPU or final cached-resource retirement. Comparisons have been describing the selected Provider lifetime and its transport ownership, without isolating Engine construction cost.

### Fresh implicit tools and dense output

`oneshot_tool` has been supplying the short fresh-tool case. The following cases have been sharing the same 16 KiB logical output, operation IDs and one-tool history:

- `oneshot_tool_output_16k_1`
- `oneshot_tool_output_16k_16`
- `oneshot_tool_output_16k_128`
- `oneshot_tool_output_16k_128_active`

Their `identity_family` has been keeping inputs and outputs identical while chunk count or public observation has been changing. Each worker has been using one prefill and 20 measured calls by default. The peer has been checking every tool result and keeping continuation history on the same fresh WSS connection. Connection counts have been counting conversations; generating-request counts have been including the tool round and final response. Warmups have been receiving separate counters.

In omitted-provider tool mode, the native adapter has been requiring a real `ToolContext[None]`, its actual call ID and `context.submission is result.turn`. Worker rows have been recording distinct native Session IDs and those checks. The active case has been creating its reader outside the callback, then attaching it to the callback's real Turn. Readiness has been following the first public non-text event. The reader has been draining an unread suffix only after a first stream without its terminal event. Every cursor drain, reader join and release of callback references has been remaining inside `Adapter.run`. Native wheels without these public APIs have been failing explicitly. Existing caller-owned cases have been retaining their original callbacks.

The v0 adapter has been remaining unchanged: final-only cases have been using `Runner.run` with JSON, while active cases have been using `Runner.run_streamed` with SSE. Native transport declarations have been checking the wheel's actual HTTP/SSE or WSS route. These have been comparing public application paths; JSON, SSE and WSS costs have not been receiving separate causal attribution.

For an unscored functional check, the [container tool example](container-default-host/README.md#fresh-tool-and-active-reader-smoke) has been reusing the existing offline setup. For a paired native comparison, the [second-wheel image steps](container-default-host/README.md#provision-a-reference-native-wheel) have been provisioning `/opt/reference` alongside `/opt/sdk` and `/opt/peer`. The shell below has been replacing the setup and worker body inside that guide's `docker run`, with all three environments trusting the same synthetic CA. Outer container deadline for this 240-worker comparison: `7200s` instead of the single-smoke `180s`; the same output mount, logs, trap and container/image cleanup.

```sh
BENCH=/bench
LAB=/out
PEER_PY=/opt/peer/bin/python
REFERENCE_NATIVE_PY=/opt/reference/bin/python
NATIVE_PY=/opt/sdk/bin/python
REFERENCE_NATIVE_VERSION="$("$REFERENCE_NATIVE_PY" -B -c "from importlib.metadata import version; print(version(\"openai-agents\"))")"
NATIVE_VERSION="$("$NATIVE_PY" -B -c "from importlib.metadata import version; print(version(\"openai-agents\"))")"
"$PEER_PY" -B "$BENCH/container-default-host/setup.py" \
  --directory /run/sdk-benchmark \
  --python "$REFERENCE_NATIVE_PY" --python "$NATIVE_PY" --python "$PEER_PY"
cp /input/sdk-packages.txt /input/peer-packages.txt /reference/packages.txt /out/
exec env OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX=1 PYTHONHASHSEED=0 \
  "$PEER_PY" -B "$BENCH/compare.py" \
  --reference-engine v1 --default-host-setup /run/sdk-benchmark/setup.json \
  --native-provider-mode omitted \
  --reference-transport websocket --native-transport websocket \
  --v0-python "$REFERENCE_NATIVE_PY" --v1-python "$NATIVE_PY" \
  --v0-version "$REFERENCE_NATIVE_VERSION" --v1-version "$NATIVE_VERSION" \
  --server-python "$PEER_PY" --output-dir "$LAB/fresh-tools" \
  --samples 24 --iterations 20 --timeout 60 \
  --cases oneshot_tool oneshot_tool_output_16k_1 oneshot_tool_output_16k_16 \
          oneshot_tool_output_16k_128 oneshot_tool_output_16k_128_active
```

The existing estimator has been retaining matched block order, raw metric vectors and paired bootstrap intervals. This command has not been carrying a source-specific optimization gate or a speedup claim. All-client CPU has been covering the existing warm interval; full-owner CPU has been covering Adapter construction through exit. Process-owned cache retirement and process-exit CPU have been remaining outside those owner clocks.


## Optional HTTP/2 peer

The default peer has been remaining aiohttp HTTP/1.1, including its WebSocket upgrade path. The explicit `--peer-http-mode dual` option has been selecting a TLS peer advertising both `h2` and `http/1.1`, using the same response, tool and history validation. This option has been requiring the existing default-host container setup. Native HTTP arms have been requiring actual H2 receipts; v0 HTTP and native WebSocket arms have been requiring HTTP/1.1. Unsupported native wheels have been failing validation rather than silently qualifying an HTTP/1.1 fallback.

The optional peer dependency has been remaining outside both SDK environments. Before measurement, install it only in the peer environment:

```sh
uv pip install --compile-bytecode --python "$PEER_PY" \
  --requirement "$BENCH/requirements-http2-peer.txt"
```

For the [default-host container guide](container-default-host/README.md), add the following line to its generated Dockerfile after the existing environment provisioning. Rebuild the image before the offline run:

```dockerfile
RUN uv pip install --no-build --compile-bytecode --python /opt/peer/bin/python -r /bench/requirements-http2-peer.txt \
    && uv pip freeze --python /opt/peer/bin/python > /input/peer-packages.txt
```

Inside that guide's existing owned container command, the following comparison has been selecting H2 for both native HTTP arms, retaining fresh Sessions and the real async tool. Use the installed versions and paths from the guide's second-wheel setup:

```sh
"$PEER_PY" "$BENCH/compare.py" \
  --peer-http-mode dual --default-host-setup /run/sdk-benchmark/setup.json \
  --reference-engine v1 --native-provider-mode omitted \
  --reference-transport http --native-transport http --native-request-transport http \
  --v0-python "$REFERENCE_NATIVE_PY" --v1-python "$NATIVE_PY" \
  --v0-version "$REFERENCE_NATIVE_VERSION" --v1-version "$NATIVE_VERSION" \
  --server-python "$PEER_PY" --output-dir "$LAB/fresh-tools-h2" \
  --samples 5 --iterations 100 --timeout 60 --cases oneshot_tool_output_16k_128
```

Each request receipt has been recording its HTTP version, negotiated ALPN and TLS connection ID. `server.tls_transports` has been recording the matching connection and request count. Repeated IDs across different operation IDs have been showing actual connection reuse; Provider lifetime alone has not been proving reuse. HTTP responses have been completing their final body send before their receipt ends. The peer has not been adding a delayed EOF after `response.completed`.

The two server modes have been using different HTTP implementations. Comparing their wall or CPU measurements has not been isolating SDK work or establishing a performance improvement. Keep the selected mode identical across a matched SDK comparison. All original measured, prefill and full-owner clocks have been remaining unchanged. Hypercorn has been staying pinned because the benchmark's TLS connection/shutdown adapter has been using its private `TCPServer` interface.

The focused transport check has been launching a local TLS peer, verifying H2 reuse, HTTP/1.1 JSON and WebSocket delivery, complete bodies and joined shutdown, without importing either SDK:

```sh
"$PEER_PY" -B -m unittest discover -s "$BENCH/tests" -p 'test_http2_peer.py'
```
