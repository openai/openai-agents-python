# Maintain the native Agents SDK

The root `openai-agents` wheel is the single deliverable. Its public Python package is `agents`, and Codex owns every model turn. Use the [v1 SDK guides](../../docs/sdk/index.md) from the same source commit as your build. This snapshot has no public v1 wheels or release tags. Python supplies typed declarations, conversion, callbacks and owned async integration.

## Build from this source

Use a current patch release of CPython 3.10–3.14, `uv`, Git and Make. Install Rust 1.95.0 with `cargo` and `rustc` available; the native build selects that toolchain by default. Install the system tools for your platform before building:

- **Linux:** A C/C++ compiler toolchain with Clang, CMake, `pkg-config`, Protocol Buffers (`protoc`) and OpenSSL development headers. Ubuntu CI installs `clang cmake pkg-config protobuf-compiler libssl-dev`.
- **macOS:** Xcode Command Line Tools for the compiler/linker, plus CMake, `pkgconf`, Protocol Buffers and OpenSSL. CI installs these packages with `brew install cmake pkgconf protobuf openssl@3`.
- **Windows:** MSVC C++ Build Tools and the Windows SDK for the compiler/linker, plus Protocol Buffers (`protoc`). The prepared Windows CI runner already has the compiler tools; the setup action adds `protoc` with Chocolatey.

The [native setup action](../../.github/actions/setup-native/action.yml) owns the CI toolchain and system-package setup. Python build isolation does not install these system tools. Preparation uses the standard library on Python 3.11 and later; Python 3.10 also needs the existing build dependency `tomli==2.2.1`. `uv build` installs its declared Python build dependencies separately, including `tomli` on Python 3.10. The build also needs access to the pinned Codex and RMCP sources and package registries.

From the repository root, run:

```sh
make native-prepare
make build
```

`make native-prepare` verifies the pinned source. If you already have an authorized Codex checkout, pass its path with `make native-prepare CODEX_SOURCE=/path/to/codex`. `make build` runs `uv build --wheel` directly from this prepared checkout. It creates one root SDK wheel in `dist/`, with its required native extension and helpers. Building the wheel directly avoids preparing another source tree from an intermediate source distribution. You do not need to run `make sync` first; that command installs the SDK for contributor work and can trigger its own native build.

The same route without Make is `python3 packages/codex/scripts/prepare_codex.py`, followed by `uv build --wheel`. If you need a source distribution for release qualification, build it explicitly with `uv build --sdist`.

Install the wheel in a fresh virtual environment outside the source checkout. The commands below start from the repository root after `make build`. Choose the exact wheel produced by this build; do not select an older wheel from `dist/`.

```sh
SDK_ROOT="$PWD"
SDK_WHEEL_CHECK_DIR=$(mktemp -d)
uv venv "$SDK_WHEEL_CHECK_DIR/.venv"
uv pip install --python "$SDK_WHEEL_CHECK_DIR/.venv/bin/python" "$SDK_ROOT"/dist/your-built-wheel.whl
cd "$SDK_WHEEL_CHECK_DIR"
.venv/bin/python -c "from importlib.metadata import version; print(version('openai-agents'))"
```

Replace `your-built-wheel.whl` with the actual filename before running the command. Compare the reported version with the root `pyproject.toml` and retain the exact source commit and build receipt; a version match alone does not identify the source. On Windows, create the environment in a directory you choose and use its `Scripts\python.exe` interpreter instead of `bin/python`.

To run the example with a real model, set your key in this shell first:

```sh
export OPENAI_API_KEY="your-key"
.venv/bin/python "$SDK_ROOT/examples/basic.py"
```

## Use a qualified wheel for Python edits

When a trusted v1 Tests run provides a `native-<OS>-<ARCH>` artifact for the same source inputs, you can reuse that artifact in a fresh editable checkout. Otherwise build from source. Choose the artifact for your platform. Keep its wheel and adjacent `.whl.pristine.json` receipt together. The build hook checks the wheel hash, platform, both pinned source identities, both patch series and native build inputs before it installs the extension and helpers.

Replace the wheel path in this Linux example with the absolute path to your downloaded wheel. Run the commands from the SDK checkout.

```sh
OPENAI_AGENTS_NATIVE_WHEEL=/absolute/path/to/your-built-wheel.whl make sync
uv run --no-sync pytest tests/test_native_session_input.py
```

Edits under `src/agents` take effect on the next Python invocation. After changing Rust or native build inputs, leave `OPENAI_AGENTS_NATIVE_WHEEL` unset and run `uv sync --group dev --reinstall-package openai-agents` to rebuild the editable package. Run `make build` when you need a release wheel.

## Keep the pin and source verifiable

Use the existing source preparation and build tools. Keep `CODEX_REVISION` and the upstream repository fixed. Reviewed SDK-local changes live as ordinary Git diffs under `patches/`; never edit `.upstream/codex` directly or add executable patch hooks. Preparation authenticates the complete canonical base archive, applies the ordered patches in private Git storage, and requires the committed resulting tree. Fresh builds and source audits verify every resulting file, directory, symlink and Git mode. An empty series must reproduce the pristine pinned tree.

`patches/series.json` has exactly two fields: `patches`, an ordered list of `{"path": "0001-topic.patch", "sha256": "<64 lowercase hex characters>"}` entries, and `result_tree`, the full 40-character resulting Git tree ID. Patch paths are unique sibling `.patch` filenames. Keep only that manifest and its listed regular patch files in the directory. Generate each patch with `git diff --full-index --binary` between the previous reviewed tree and the next tree in a disposable checkout of the pin; additions and deletions are supported. Hash the exact patch bytes with SHA-256. After staging the complete ordered result in that disposable checkout, use `git write-tree` for `result_tree`. Review the patch bytes, their order and the expected tree together. Updating a patch requires updating its hash and the expected tree, then preparing a fresh source directory.

The source receipt retains the authenticated base commit and file map, reviewed patch identity and resulting file map. The adjacent `.whl.pristine.json` suffix remains a compatibility filename; a patched result is not pristine upstream source. Build receipts use `reviewed-codex-rmcp-v4` and bind both base commits, both ordered patch series, both result trees, current native/build inputs and the exact wheel hash. Source-free wheel reuse requires that trusted receipt and matching current manifest/patch bytes; when prepared source exists, reuse also verifies its full result tree. Old receipts or any changed, missing, extra or unlisted patch input require a fresh build. The source distribution and native cache keys include the manifest and patch files. This policy authorizes only reviewed SDK-local patches; it does not authorize changing the pin, upstream repository or public Agents API.

RMCP keeps the Git URL, revision and exact version already declared in `Cargo.toml`. `rmcp-patches/series.json` uses the same strict manifest schema as the Codex series. Preparation writes the complete verified rust-sdk workspace to `.upstream/rust-sdk`; the two root Cargo overrides select its `rmcp` and `rmcp-macros` crates. No source tree is committed or included in the source distribution. The source distribution includes both patch directories and the preparation scripts. Builds copy the pinned RMCP license into the wheel and verify both source results before and after compilation.

Run `python3 packages/codex/scripts/prepare_codex.py --source /path/to/codex --rmcp-source /path/to/rust-sdk` to use existing authorized Git repositories. Omit either source argument to fetch that source from its fixed Git origin. Preparation authenticates the declared commit rather than trusting the supplied repository's current checkout. Reusing an existing Codex archive still prepares and verifies RMCP. A source-free wheel receipt carries the two source identities separately; a Codex-only receipt cannot qualify this build.

The RMCP codec patch preserves general form JSON on `elicitation/create` for the SDK's bounded validator. Treat this as an SDK capability; MCP schema conformance needs separate verification. Primitive validation and supported general-schema limits remain defined by `ElicitationRequest`.

The response-stream patch carries selected local context through HTTP response streams and reconnects without serializing it. Codex uses this context to reject requests that arrive after their original child tool call has ended.

Record exact SDK/native revisions, compiled input hashes, prepared source identity and platform when qualifying a native artifact. Changing Python module names requires matching PyO3/import paths and a built extension. Copying an old binary to a new path does not establish that match.

## Verify the delivered wheel

Run focused public native workflows and typing checks first. Complete the required independent review before the final SDK verification command. Use the root commands and repository skills; do not start a second build system or SDK distribution.

Verify the installed wheel outside the checkout with only its declared runtime dependencies. Check its Python files, native extension, platform helpers, shared libraries, required notices, RECORD entries and executable bits against the build receipt. Exercise a real native provider request, admitted Python callback and final result. Recheck Python 3.10 cancellation and joined cleanup when those boundaries change.

Keep source tests, installed native tests, synthetic provider tests, hosted CI, platform qualification and package publication separate. A passing Linux synthetic workflow does not establish another platform or live provider. Retain failed receipts and document their causes; later fixes do not rewrite them.

## Preserve lifecycle and authority

Use one native thread/event reader per Session and one accepted tool generation. Validate native admission at application-code entry points and after awaited work. Join owned Python callbacks and cleanup on cancellation or close. Passive observations grant no authority. Load checkpoints only from trusted application storage and keep current credentials outside saved state.

Use synthetic fixtures and keep credentials, private prompts and payloads out of logs, test snapshots and diagnostics. Preserve release source validation, artifact integrity, least-privilege CI permissions, full action pins and PyPI OIDC controls.
