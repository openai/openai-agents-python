# Maintain the native Agents SDK

The build produces one wheel for the root `openai-agents` distribution, which provides the `agents` Python package. For prebuilt preview wheels, see the [installation guide](../../docs/sdk/installation.md). For source builds, use the [SDK guides](../../docs/sdk/index.md) from the same commit.

Codex owns every model turn. Python supplies typed declarations, conversion, and callbacks, and manages its asynchronous tasks.

## Build from this source

### Install build requirements

Use a current patch release of CPython 3.10–3.14, `uv`, Git and Make. Install Rust 1.95.0 and make `cargo` and `rustc` available. The native build selects that toolchain by default. Install the system tools for your platform before building:

- **Linux:** A C/C++ compiler toolchain with Clang, CMake, `pkg-config`, Protocol Buffers (`protoc`) and OpenSSL development headers. Ubuntu CI installs `clang cmake pkg-config protobuf-compiler libssl-dev`.
- **macOS:** Xcode Command Line Tools for the compiler/linker, plus CMake, `pkgconf`, Protocol Buffers and OpenSSL. CI installs these packages with `brew install cmake pkgconf protobuf openssl@3`.
- **Windows:** MSVC C++ Build Tools and the Windows SDK for the compiler/linker, plus Protocol Buffers (`protoc`). The prepared Windows CI runner already has the compiler tools; the setup action adds `protoc` with Chocolatey.

The [native setup action](../../.github/actions/setup-native/action.yml) owns the CI toolchain and system-package setup. Python build isolation does not install these system tools.

Preparation uses the standard library on Python 3.11 and later. Python 3.10 also needs `tomli==2.2.1`. `uv build` installs its declared Python build dependencies separately, including `tomli` on Python 3.10. The build needs access to the pinned Codex and RMCP sources and package registries.

### Prepare and build

On Python 3.11 or later, run these commands from the repository root:

```sh
make native-prepare
make build
```

On Python 3.10, use this preparation command instead of `make native-prepare`. It supplies `tomli` to the preparation process without installing the SDK or building an editable checkout:

```sh
uv run --no-project --python 3.10 --with tomli==2.2.1 python packages/codex/scripts/prepare_codex.py
make build
```

Preparation verifies the pinned source. To use an authorized Codex checkout:

- On Python 3.11 or later, use `make native-prepare CODEX_SOURCE=/path/to/codex`.
- On Python 3.10, append `--source /path/to/codex` to the preparation command above.

`make build` runs `uv build --wheel` directly from the prepared checkout. It creates one root SDK wheel in `dist/`, including the native extension and helpers. This avoids preparing another tree from an intermediate source distribution. You do not need `make sync` first. It installs the SDK for contributor work and can trigger another native build.

On Python 3.11 or later, the same route without Make is `python3 packages/codex/scripts/prepare_codex.py`, followed by `uv build --wheel`. On Python 3.10, use the uv preparation command above, followed by `uv build --wheel`. If you need a source distribution for release qualification, build it explicitly with `uv build --sdist`.

### Install outside the checkout

Install the wheel in a fresh virtual environment outside the source checkout. The commands below start from the repository root after `make build`. Choose the exact wheel produced by this build; do not select an older wheel from `dist/`.

```sh
SDK_ROOT="$PWD"
SDK_WHEEL_CHECK_DIR=$(mktemp -d)
uv venv "$SDK_WHEEL_CHECK_DIR/.venv"
uv pip install --python "$SDK_WHEEL_CHECK_DIR/.venv/bin/python" "$SDK_ROOT"/dist/your-built-wheel.whl
cd "$SDK_WHEEL_CHECK_DIR"
.venv/bin/python -c "from importlib.metadata import version; print(version('openai-agents'))"
```

Replace `your-built-wheel.whl` with the actual filename before running the command. Compare the reported version with the root `pyproject.toml` and retain the exact source commit and build receipt. A version match alone does not identify the source. On Windows, create the environment in a directory you choose and use its `Scripts\python.exe` interpreter instead of `bin/python`.

To run the example with a real model, set your key in this shell first:

```sh
export OPENAI_API_KEY="your-key"
.venv/bin/python "$SDK_ROOT/examples/basic.py"
```

## Use a qualified wheel for Python edits

When a trusted **Tests** run provides a `native-<OS>-<ARCH>` artifact for the same source inputs, you can reuse that artifact in a fresh editable checkout. Otherwise build from source. Choose the artifact for your platform. Keep its wheel and adjacent `.whl.pristine.json` receipt together. The build hook checks the wheel hash, platform, both pinned source identities, both patch series and native build inputs before it installs the extension and helpers.

Replace the wheel path in this Linux example with the absolute path to your downloaded wheel. Run the commands from the SDK checkout.

```sh
OPENAI_AGENTS_NATIVE_WHEEL=/absolute/path/to/your-built-wheel.whl make sync
uv run --no-sync pytest tests/test_native_session_input.py
```

Edits under `src/agents` take effect on the next Python invocation. After changing Rust or native build inputs, leave `OPENAI_AGENTS_NATIVE_WHEEL` unset and run `uv sync --group dev --reinstall-package openai-agents` to rebuild the editable package. Run `make build` when you need a release wheel.

### Native products in Tests CI

The **Tests** workflow checks each pull request's merge commit. A separate push workflow runs the same native pipeline on other canonical-repository branches, without the broad SDK tests, lint or docs jobs. Default-branch and `feat/codex-harness` pushes keep the full Tests workflow and do not start a duplicate native-only run.

#### Identify the artifact

Every selected platform publishes a `native-snapshot-<OS>-<ARCH>` artifact before any SDK or installed-package qualification starts in that run. Qualification repacks the current Python SDK around the verified native payload, then publishes the existing `native-<OS>-<ARCH>` output. Installed-package, IPC, executable-symbol and receipt checks remain required.

#### Check donor eligibility

A donor is a push run that supplies a reusable native artifact. CI checks the complete caller and native workflows at the push commit, compares committed native and build inputs against the current checkout, and requires successful later platform qualification. CI selects original publications by artifact ID and SHA-256 digest before the earliest SDK execution across all attempts. A branch producer records its unique open PR, repository IDs and branch refs before publication. Consumers compare that captured identity with their current PR. Pre-PR or ambiguous associations do not qualify, and PR-scoped products never supply canonical pushes or another PR.

PR Tests prefers an existing compatible donor. If none exists, it can wait up to 75 minutes for one compatible push producer of its current head. The producer never waits for the PR consumer. Missing, failed, cancelled or incompatible producers use the fresh-build path. Incomplete source-history reads and corrupted downloads fail visibly. The search checks the latest 20 runs for each eligible source. A copied snapshot remains eligible only when the same source, publication and qualification checks pass.

#### Keep native and SDK caches separate

Native producers do not restore or save Actions caches, including Cargo targets, source trees, V8 bundles and Python build tools. Verified native snapshots provide reuse across runs; fresh job-local Cargo and uv directories provide reuse within one build. SDK-phase caches remain separate. Native-input or recipe changes require fresh production; local Python tests do not replace full platform qualification.

## Keep the pin and source verifiable

Use the existing source preparation and build tools. Keep `CODEX_REVISION` and the upstream repository fixed.

### Maintain the patch manifest

Store reviewed SDK-local changes as ordinary Git diffs under `patches/`. Never edit `.upstream/codex` directly or add executable patch hooks. Preparation authenticates the complete canonical base archive and applies the ordered patches in private Git storage. The result must match the tree recorded in the committed manifest. Fresh builds and source audits verify every resulting file, directory, symlink and Git mode. An empty series must reproduce the pristine pinned tree.

`patches/series.json` has exactly two fields:

- `patches`: an ordered list of `{"path": "0001-topic.patch", "sha256": "<64 lowercase hex characters>"}` entries. Paths must be unique sibling `.patch` filenames.
- `result_tree`: the full 40-character resulting Git tree ID.

Keep only the manifest and its listed regular patch files in the directory. To create or update the series:

1. In a disposable checkout of the pin, generate each patch with `git diff --full-index --binary` between the previous reviewed tree and the next tree. Additions and deletions are supported.
2. Hash the exact patch bytes with SHA-256.
3. Stage the complete ordered result in that checkout, then use `git write-tree` for `result_tree`.
4. Review the patch bytes, order, and expected tree together.

After updating a patch, update its hash and the expected tree, then prepare a fresh source directory.

### Verify receipts before reuse

The source receipt retains the authenticated base commit and file map, reviewed patch identity, and resulting file map. Wheel receipts keep the `.whl.pristine.json` suffix for compatibility. A patched result is not pristine upstream source.

Build receipts use `reviewed-codex-rmcp-v4` and bind both base commits, both ordered patch series, both result trees, current native/build inputs, and the exact wheel hash. Reuse always requires a trusted receipt and matching current manifest and patch bytes. When prepared source exists, reuse also verifies its full result tree. Old receipts or changed, missing, extra, or unlisted patch inputs require a fresh build.

The source distribution and native cache keys include the manifest and patch files. Patch approval does not authorize changes to the pin, upstream repository, or public Agents API.

### Prepare and verify RMCP

RMCP keeps the Git URL, revision and exact version already declared in `Cargo.toml`. `rmcp-patches/series.json` uses the same strict manifest schema as the Codex series.

Preparation writes the complete verified rust-sdk workspace to `.upstream/rust-sdk`. The two root Cargo overrides select its `rmcp` and `rmcp-macros` crates. No source tree is committed or included in the source distribution. The source distribution includes both patch directories and the preparation scripts. Builds copy the pinned RMCP license into the wheel and verify both source results before and after compilation.

To use existing authorized Git repositories:

- On Python 3.11 or later, run `python3 packages/codex/scripts/prepare_codex.py --source /path/to/codex --rmcp-source /path/to/rust-sdk`.
- On Python 3.10, append the same `--source` and `--rmcp-source` arguments to the preparation command above.
- Omit either source argument to fetch that source from its fixed Git origin.

Preparation authenticates the declared commit rather than trusting the supplied checkout. Reusing a Codex archive still prepares and verifies RMCP. Receipts for reuse without prepared source record both source identities. A Codex-only receipt cannot qualify this build.

The RMCP codec patch preserves general form JSON on `elicitation/create` for the SDK's bounded validator. This patch supports an SDK capability. Verify MCP schema conformance separately. `ElicitationRequest` defines primitive validation and the supported general-schema limits.

### Preserve native context and identity

The response-stream patch carries selected local context through HTTP response streams and reconnects without serializing it. Codex uses this context to reject requests that arrive after their original child tool call has ended.

Record exact SDK/native revisions, compiled input hashes, prepared source identity, and platform for each qualified artifact. Changing Python module names requires matching PyO3/import paths and a built extension. Copying an old binary to a new path does not establish that match.

## Verify the delivered wheel

Run focused public native workflows and typing checks first. Complete the required independent review before the final SDK verification command. Use the root commands and repository skills; do not start a second build system or SDK distribution.

Install the wheel outside the checkout with only its declared runtime dependencies. Then verify the installed package:

1. Check Python files, the native extension, platform helpers, shared libraries, required notices, RECORD entries, and executable bits against the build receipt.
2. Exercise a real native provider request, an admitted Python callback, and a final result.
3. Recheck Python 3.10 cancellation and joined cleanup when those boundaries change.

Keep source tests, installed native tests, synthetic provider tests, hosted CI, platform qualification and package publication separate. A passing synthetic workflow on Linux does not verify another platform or a live provider. Retain failed receipts and document their causes. Later fixes do not rewrite them.

## Preserve lifecycle and authority

Use one native thread/event reader per Session and one accepted tool generation. Validate native admission at application-code entry points and after awaited work. On cancellation or close, wait for the Session's Python callbacks and cleanup to finish. Passive observations grant no authority. Load checkpoints only from trusted application storage and keep current credentials outside saved state.

Use synthetic fixtures and keep credentials, private prompts and payloads out of logs, test snapshots and diagnostics. Preserve release source validation, artifact integrity, least-privilege CI permissions, full action pins and PyPI OIDC controls.
