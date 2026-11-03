# Install the v1 source preview

The public `feat/codex-harness` branch contains the native Codex-based Agents SDK. This snapshot has no public v1 wheels or release tags. Build a wheel from source and install it in a separate environment. `pip install openai-agents` installs the published v0 SDK; it does not install this branch.

## Requirements

Use a current patch release of standard CPython 3.10–3.14, Git, `uv`, and Rust 1.95.0. Install the [platform-specific system tools](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/packages/codex/MAINTAINING.md#build-from-this-source), including the compiler and Protocol Buffers, before building. Python package installers do not install these system tools. The build needs access to the pinned public Codex and RMCP sources and package registries. Make is optional when you use the commands below.

The native CI targets are Linux x86-64 on Ubuntu 24.04, macOS on Apple Silicon (arm64), and Windows x64, using standard CPython 3.10–3.14. These are the source-build targets for this preview; the instructions do not establish support for other operating systems, architectures, PyPy, or free-threaded Python. The old private wheel's `manylinux_2_39` tag describes that wheel and is not a general source-build prerequisite.

## Build and install

Clone the public branch into a new directory:

```sh
git clone --branch feat/codex-harness https://github.com/openai/openai-agents-python.git
cd openai-agents-python
git rev-parse HEAD
python3 packages/codex/scripts/prepare_codex.py
uv build --wheel
```

On Windows, use `py -3` instead of `python3` for source preparation. On Python 3.10, install `tomli==2.2.1` in the environment that runs source preparation first. The isolated wheel build installs its own declared Python build dependencies.

Record the printed commit and keep the documentation and examples from that checkout. Preparation verifies the pinned native sources; the wheel build compiles the extension and matching execution helpers. See the [maintenance guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/packages/codex/MAINTAINING.md#build-from-this-source) for details.

Create a fresh environment outside the checkout, then install the exact wheel generated in `dist/`. Replace the wheel path below with that file's absolute path. Do not install a private preview wheel or an unrelated wheel already in `dist/`.

```sh
uv venv /absolute/path/to/agent-project/.venv
uv pip install --python /absolute/path/to/agent-project/.venv/bin/python /absolute/path/to/openai-agents-python/dist/your-built-wheel.whl
```

On Windows, use an absolute Windows path and `.venv\Scripts\python.exe` for the environment's interpreter.

## Check the installation

Use the environment's Python interpreter to inspect the package and import the native module:

```sh
python -c "from importlib.metadata import version; import agents._native; print(version('openai-agents'))"
```

Run this command outside the SDK checkout, with the environment activated as described below. Compare the reported version with `pyproject.toml` from the recorded commit. This snapshot retains `0.24.0-alpha.5` package metadata from its private-preview lineage; it is not a public alpha.5 wheel. A matching version alone does not prove that a wheel came from that commit; keep the built wheel and build records together.

## If setup fails

- If native compilation fails, check Rust 1.95.0 and the system tools in the maintenance guide.
- If source preparation cannot fetch a pin, check access to the public upstream sources and inspect the reported failure. Do not bypass source verification.
- If a documented method is missing, confirm that the active interpreter uses your source-built wheel. Public v0 and historical private-preview packages use different APIs.
- If a dependency is unavailable, check your configured Python package index and platform. A source build does not establish support for every Python implementation or platform.

## Activate the environment

The guides use `python` to run examples. Activate the environment in your application directory first.

On Linux or macOS:

```sh
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Download example files

Use the examples in the checkout you built. Do not download examples from a v0 release or mix examples from different commits. Run `python examples/basic.py` from that checkout with the installed environment activated. Live examples need an [OpenAI API key](published.md#set-an-openai-api-key); the [testing guide](sdk/testing.md) also provides a no-key workflow.
