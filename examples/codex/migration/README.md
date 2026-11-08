# Port a read-only invoice lookup

Port one agent and one read-only tool from released v0 to native v1. Both versions use typed application context and return a typed invoice. You can keep using v0; classic is an optional staging step.

## Install the matching SDKs

These files require the current `feat/codex-harness` source. The published `v0.24.0-alpha.6` wheel predates classic and APIs used here. It cannot run this tutorial. Keep one checkout for the current SDK, these examples, and their documentation.

Follow [Build the SDK from source](../../../CONTRIBUTING.md#build-the-sdk-from-source) in that checkout, including its platform and toolchain prerequisites. Record `git rev-parse HEAD` and retain the exact wheel produced in `dist/`. A package version alone does not identify a development build. Source builds require native tools; the old PyPI package is not a substitute for this build.

The following commands use a POSIX shell and start at that checkout's root. On Windows, choose an equivalent working directory, use `py -m venv`, and replace each environment's `bin/python` with `Scripts\python.exe`. Set `PYTHONPATH` to the checkout in your shell.

```sh
SDK_ROOT="$PWD"
MIGRATION_WORK=$(mktemp -d)
python3 -m venv "$MIGRATION_WORK/v0-env"
"$MIGRATION_WORK/v0-env/bin/python" -m pip install 'openai-agents==0.24.0'

python3 -m venv "$MIGRATION_WORK/v1-env"
```

Set `MIGRATION_WHEEL` to the absolute path of the wheel you just built, replacing the placeholder below with its actual filename. Install it in the second environment:

```sh
MIGRATION_WHEEL=/absolute/path/to/the-wheel-you-built.whl
"$MIGRATION_WORK/v1-env/bin/python" -m pip install "$MIGRATION_WHEEL"
```

The same wheel provides the native API and the optional classic API. Keep both environments outside the checkout so a source import cannot hide the installed SDK. `PYTHONPATH="$SDK_ROOT"` below exposes the example modules; the SDK package lives under `src/` and comes from the selected environment.

If a proxy is configured, add `127.0.0.1` to its bypass list before running native scripts. Install any transport extras required by your proxy in the relevant environment. The walkthrough run commands below use live models. The maintained tests use scripted replies without API keys.

## 1. Run the v0 application

Read [business.py](business.py) and [v0.py](v0.py). The host supplies an `Application` containing the invoice this user can read. `lookup_invoice` receives that object through `RunContextWrapper[Application]`; the model supplies only the invoice ID. `output_type=Invoice` gives the final answer the same typed fields as the tool result.

For a run with model requests, set `OPENAI_API_KEY` in your shell as shown in [API key setup](../../../docs/sdk/index.md). Keep the key outside source files. For checks without credentials, use [the maintained tests](#check-without-model-credentials) instead.

Run from the checkout root:

```sh
PYTHONPATH="$SDK_ROOT" "$MIGRATION_WORK/v0-env/bin/python" \
  -m examples.codex.migration.v0
```

This command makes live model requests. Expect a JSON invoice with `invoice_id` equal to `INV-42`, `amount_cents` equal to `4200`, and `status` equal to `paid`.

## 2. Port directly to native

Read [native.py](native.py). Keep `Application`, `Invoice`, and `read_invoice()` unchanged. Change the tool context to `ToolContext[Application]` and replace `Runner.run(agent, ...)` with `agent.run(...)`. The root `agents` imports now select native declarations. Set native provider/model configuration for your deployment; Python model objects do not plug into the native loop. See [provider compatibility](../../../docs/sdk/providers.md).

```sh
PYTHONPATH="$SDK_ROOT" "$MIGRATION_WORK/v1-env/bin/python" \
  -m examples.codex.migration.native
```

This command also makes live model requests and should return the same invoice fields. The one-shot run owns and closes its native Session. No conversation history is transferred between these independent runs.

## Optional: stage the package upgrade through classic

If a workflow must retain the v0 runner while you upgrade the package, change only its SDK imports first:

```python
from agents.classic import Agent, Model, RunContextWrapper, Runner, function_tool
```

Install the same wheel with its classic dependencies:

```sh
"$MIGRATION_WORK/v1-env/bin/python" -m pip install "${MIGRATION_WHEEL}[classic]"
PYTHONPATH="$SDK_ROOT" "$MIGRATION_WORK/v1-env/bin/python" \
  -m examples.codex.migration.classic
```

The maintained [classic.py](classic.py) makes this import change and keeps the rest of `v0.py` unchanged. Run your workflow's checks. Then apply the same native port in step 2. Classic is optional and does not convert saved execution state. See [classic setup and lifecycle](../../../CLASSIC.md).

## Check without model credentials

Prepare this checkout's [contributor environment](../../../CONTRIBUTING.md) with `make sync`, which installs the classic dependencies used by the tests. The `uv run --no-sync` command below uses that checkout environment, not the separate wheel environment created above. The maintained tests inject scripted providers into these application declarations. They check the actual tool result, the typed final output, host-supplied context, and denial of an invoice outside that context. Set `MIGRATION_V0_PYTHON` to qualify both direct and staged migration against released v0.24.0, including SQLite continuation and a native checkpoint round trip.

```sh
MIGRATION_V0_PYTHON="$MIGRATION_WORK/v0-env/bin/python" \
  uv run --no-sync pytest -q packages/codex/tests/test_migration_example.py
```

Without `MIGRATION_V0_PYTHON`, tests requiring the separate released-v0 environment are skipped. These checks make no model-service or tracing requests; native tests use local Responses HTTP servers. Scripted replies test application behavior, not model quality or production provider compatibility.

## Keep state and advanced features separate

Keep v0/classic conversations and pending work on their owning engine. Start native conversations from selected current application facts; no API converts old execution history or pending approvals. Native checkpoints restore only native work and require a matching Codex revision. See [state compatibility and recovery](../../../docs/sdk/state.md).

Reuse the existing [specialist-agent recipe](../recipes/specialist_agents.py) and [safe-effects recipe](../recipes/safe_effects.py) when those features are needed. The [migration guide](../../../MIGRATION.md) provides the compatibility checklist and remaining API mappings.
