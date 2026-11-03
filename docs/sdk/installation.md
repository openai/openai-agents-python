# Install the SDK

Install the SDK preview from a Python wheel, which includes the Python package and compiled native runtime. You need a supported Python installation and a package installer such as pip.

## Preview availability

The first public preview wheels are not available yet. Follow these steps once they are published. Preview downloads will be attached to versioned [GitHub releases](https://github.com/openai/openai-agents-python/releases).

The SDK preview will stay off PyPI until general availability. The `openai-agents` package currently on PyPI is the previous SDK and does not provide the API in these guides.

## Requirements

Use standard CPython 3.10 through 3.14. Each release will list the operating systems and architectures covered by its wheels. Choose a wheel that matches your computer and Python version. PyPy and free-threaded Python are not supported by these wheels.

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

Once a preview release is available, download the wheel for your platform from that release. Use the release's installation command, or pass the downloaded file's full path to the command below. Replace `WHEEL_FILE.whl` with the actual filename. Do not rename the wheel.

```sh
python -m pip install "/path/to/WHEEL_FILE.whl"
```

Use a Windows path on Windows, for example `"C:\Users\you\Downloads\WHEEL_FILE.whl"`. pip installs the SDK and its Python dependencies. The SDK's native runtime is already compiled in the wheel.

Check the installed version and native module:

```sh
python -c "from importlib.metadata import version; import agents._native; print(version('openai-agents'))"
```

Check that the printed version matches the release you downloaded. Continue with the [quickstart](index.md) to set your API key and run your first agent.

## Examples and upgrades

Use documentation and example files from the same release as the installed wheel. Copy a self-contained example into your application, or download that release's source archive to run its example projects. You do not need to build the SDK to use the examples.

To upgrade, download a newer compatible preview wheel and install it in the same environment with `python -m pip install --upgrade` followed by the wheel's path. Read that release's notes for changes before upgrading.

## If installation fails

- **The wheel is not supported on this platform:** check its Python version, operating system, and CPU architecture against the release's download list.
- **A documented method is missing:** run `python -m pip show openai-agents` in your application environment and check that its version matches your downloaded release and documentation.
- **A dependency cannot be downloaded:** check your package index configuration and network access. pip gets dependencies from your configured index.

For changes to the SDK itself, see the [contributor guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md).
