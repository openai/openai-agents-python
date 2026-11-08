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
NATIVE_VERSION='0.24.0-alpha.6' # Exact installed distribution metadata version.
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
uv pip install --compile-bytecode --python "$PEER_PY" \
  'aiohttp==3.14.3' 'openai==3.41.0' 'pydantic==2.12.3'
```

The wheel hook builds the native extension and matching execution helpers. Install that exact wheel into the fresh environment; do not substitute an editable checkout or an unrelated native binary. The dependency versions above come from existing compatible environment records. They are not complete dependency locks. Keep the wheel hash, source commit, Python build and complete installed package inventories with results. Use `uv pip freeze --python <interpreter>` to record each environment. If registry access is unavailable, supply the exact stable release wheel locally.

Compile bytecode for every environment before measurement, as shown above. Workers and peers run with `-B` to prevent cache writes; they still read existing caches. Unequal cache coverage can change first-call CPU even when package versions match. Keep before/after inventories of installed Python sources and valid, non-optimized bytecode caches, including their interpreter cache tag and source validation. Worker rows record the effective cache settings, but those settings alone do not prove complete or equal cache coverage.

## Verify and run a small correctness check

Before execution, verify the eight source hashes and inventory against `benchmark-source-manifest.json`. Keep exactly eight `.py` files in this directory, including `compare.py`; the controller records every Python file. Run ordinary Python without optimization or instrumentation. The controller and worker reject known inherited Python path/home, allocator, preload, profiling and scheduler overrides, including empty environment values. They also reject active Python trace/profile hooks and profiling `-X` options. Clear the listed controls rather than relying on a shell that happens to omit them. `PYTHONSAFEPATH` and the controller's fixed `PYTHONHASHSEED=0` remain supported. These checks cover known controls, not every possible external profiler or modified interpreter.

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

The adapters fix the model identifier to `gpt-5.1-codex`, instructions and tool schema; there is no model/effort override. For final-only calls, stable uses JSON while native uses SSE. The peer performs that framing work, so large-output wall time does not isolate client cost. Streamed cases also validate chunks, and history/tool cases verify retained request contents and callbacks.

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

The statistics/qualification tests use only the standard library and do not import either SDK or spawn benchmark workers:

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
  --v0-version 0.24.0 --v1-version 0.24.0-alpha.6 \
  --server-python "$PEER_PY" --output-dir "$RESULTS/buffered" \
  --v1-response-mode buffered --samples 5 --iterations 20 \
  --cases oneshot_text oneshot_tool oneshot_output_64k_1_plain \
          oneshot_output_64k_256_plain oneshot_output_64k_4096_plain
```

Omit `--v1-response-mode buffered` and use a fresh output directory for the default SSE control. Keep the same source, wheel, dependencies, interpreter, CA and host conditions. The three 64 KiB cases are final-only `Agent.run` calls in independent fresh conversations, with one prefill and 20 measured calls by default. Their chunk setting controls the SSE reference workload; a JSON response has zero SSE chunks. This removes peer framing work too, so wall-time differences do not isolate client processing. Client process CPU and peer CPU remain separate.

Rows record the native response mode and expected wire transport, checked against all actual request receipts, per-response chunk counts and total emitted chunks. Both modes retain the same final output, input/history and tool-result checks. Buffered comparisons reject live-observation workloads. They report no first-delta metric or token-live claim; buffered progress arrives after the complete body. Existing live streaming cursor, gap and exact chunk checks remain unchanged.

`elapsed_ms`, `cpu_ms` and latency p50/p95 still cover measured operations after prefill. For oneshot cases, these are warmed fresh-conversation measurements. New `full_owner_wall_ms` and `full_owner_cpu_ms` span Adapter construction through successful joined provider/telemetry exit, including prefill and ordinary client scheduling, prompt work and in-scope peer-stat requests. They exclude SDK import, peer startup and final correctness validation. These spans overlap the existing metrics and must not be added to them. Failed runs cannot qualify.

`charged_operations = concurrency * (prefill + iterations)` counts turns, including prefill. It counts fresh conversations only for oneshot cases; conversation cases reuse Sessions. Summaries apply the existing paired estimator to `full_owner_wall_ms_per_operation` and `full_owner_cpu_ms_per_operation`, retaining raw totals and matched vectors. Import-only rows have no owner/operation/transport metrics. RSS stays unqualified and does not enter comparisons. The existing confidence-interval limitations and engineering parity rule still apply.
