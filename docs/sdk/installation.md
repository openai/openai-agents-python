# Install the SDK

Install the SDK preview from a Python wheel, which includes the Python package and compiled native runtime. You need a supported Python installation and a package installer such as pip.

## Preview availability

[0.24.0-alpha.6](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0-alpha.6) is available as a GitHub prerelease with prebuilt wheels. The release includes checksums in `SHA256SUMS`.

The supervised worker controls in these guides, including `Environment.local()` and `Turn.wait_for_attention()`, require a preview wheel newer than alpha.6 that includes those APIs. Check the wheel's release notes before using the [supervised worker recipe](recipes.md#run-supervised-workers).

The SDK preview will stay off PyPI until general availability. The `openai-agents` package currently on PyPI is the previous SDK and does not provide the API in these guides.

## Requirements

Use standard CPython 3.10 through 3.14 on one of these platforms:

| Platform | Requirements |
| --- | --- |
| Linux | x86_64 with glibc 2.39 or newer, such as Ubuntu 24.04. |
| macOS | Apple Silicon (arm64), macOS 11 or newer. |
| Windows | x64 (amd64). |

Choose the wheel for your operating system and CPU architecture. The `cp310-abi3` wheels work with all supported Python versions, including 3.14. Wheels are not provided for Intel Macs, ARM Linux, ARM Windows, Alpine/musl, PyPy or free-threaded Python.

## Create a Python environment

Create a directory for your application and a virtual environment inside it.

On Linux or macOS:

```sh
mkdir my-agent
cd my-agent
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
mkdir my-agent
cd my-agent
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Run the remaining commands in this activated environment.

## Install a preview wheel

Run the command for your platform. pip downloads the wheel and installs the SDK with its Python dependencies. The native runtime is already compiled in the wheel.

**Linux x86_64 (glibc 2.39 or newer)**

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.6/openai_agents-0.24.0a6-cp310-abi3-manylinux_2_39_x86_64.whl"
```

**macOS 11 or newer on Apple Silicon**

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.6/openai_agents-0.24.0a6-cp310-abi3-macosx_11_0_arm64.whl"
```

**Windows x64**

```powershell
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.6/openai_agents-0.24.0a6-cp310-abi3-win_amd64.whl"
```

Check the installed version and native module:

```sh
python -c "from importlib.metadata import version; import agents._native; print(version('openai-agents'))"
```

Expect `0.24.0-alpha.6` or its equivalent Python packaging spelling, `0.24.0a6`. Continue with the [quickstart](index.md) to set your API key and run your first agent.

## Examples and upgrades

Use documentation and example files from the same release as the installed wheel. You can copy a self-contained example into your application.

The wheel does not include example files. To run an example project:

1. Download and extract the [0.24.0-alpha.6 source archive](https://github.com/openai/openai-agents-python/archive/refs/tags/v0.24.0-alpha.6.zip).
2. Keep your wheel's Python environment activated. In your shell, change to the extracted directory that contains `examples/`.
3. Follow the example's setup notes and run its command from that directory.

You do not need to build the SDK.

To upgrade, download a newer compatible preview wheel and install it in the same environment with `python -m pip install --upgrade` followed by the wheel's path. Read that release's notes for changes before upgrading.

## If installation fails

- **The wheel is not supported on this platform:** check its Python version, operating system, and CPU architecture against the release's download list.
- **A documented method is missing:** run `python -m pip show openai-agents` in your application environment and check that its version matches your downloaded release and documentation.
- **A dependency cannot be downloaded:** check your package index configuration and network access. pip gets dependencies from your configured index.

For changes to the SDK itself, see the [contributor guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md).
