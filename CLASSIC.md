# Stage a migration with the classic API

Prefer [migrating directly from v0 to native v1](MIGRATION.md). Use classic only when you need to upgrade the package before a workflow can adopt native execution. You can also keep that workflow on v0; upgrading is optional.

Starting with alpha.7, preview wheels include `agents.classic`. It retains the v0 Python runner. The root `agents` API uses native Codex.

The classic source comes from v0 `main` commit `814de8b1cfdaf6c70212dca9ca23563dd0b789ac`, with v0.24.0 as the released compatibility baseline. It includes later fixes from that pinned commit. Its namespace changes do not move execution onto Codex.

## Install the classic extra

Download the wheel for your platform from the [alpha.7 release](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0-alpha.7). Follow the [installation guide](docs/sdk/installation.md) to create and activate a Python environment. From the directory containing the downloaded wheel, replace `WHEEL.whl` below with its full filename:

```sh
python -m pip install './WHEEL.whl[classic]'
```

Classic is part of the same `openai-agents` distribution. The extra installs the dependencies needed by the old Python runner. The native wheel's platform requirements still apply. Keep using the existing v0 release in its own environment if your platform cannot install the new SDK.

Optional v0 integrations use `classic-` prefixed extras, such as `classic-voice`, `classic-litellm`, or `classic-sqlalchemy`. Append the extras you need to the wheel filename, for example `'./WHEEL.whl[classic,classic-voice]'`. The base classic extra does not install every optional service client.

### SQLite session drivers

`AsyncSQLiteSession` requires `aiosqlite` in addition to the classic extra:

```sh
python -m pip install './WHEEL.whl[classic]' aiosqlite
```

`SQLAlchemySession` requires an async driver that matches your database URL. For SQLite URLs that start with `sqlite+aiosqlite://`, install:

```sh
python -m pip install './WHEEL.whl[classic-sqlalchemy]' aiosqlite
```

The `classic-sqlalchemy` extra includes the classic runtime and `asyncpg` for PostgreSQL URLs that start with `postgresql+asyncpg://`. Install drivers for other databases separately, as in v0.

### Experimental Codex integration

`agents.classic.extensions.experimental.codex` runs the Codex CLI as a separate process. Install the CLI separately and make `codex` available on `PATH`, or select its executable with `CODEX_PATH` or `Codex(codex_path_override="/absolute/path/to/codex")`. This distribution does not bundle that CLI. The native SDK helper executables serve the root `agents` API and are not replacements for the CLI used by this integration.

### Install from the matching source

To develop the SDK or run an example that needs a source installation, follow [the contributor setup](CONTRIBUTING.md), then install from the repository root:

```sh
python -m pip install '.[classic]'
```

## Stage the package and import change

Change imports for each existing workflow together:

| Existing import | Classic import |
| --- | --- |
| `from agents import Agent, Runner, function_tool` | `from agents.classic import Agent, Runner, function_tool` |
| `from agents.models import ...` | `from agents.classic.models import ...` |
| `from agents.tracing import ...` | `from agents.classic.tracing import ...` |
| `from agents.mcp import ...` | `from agents.classic.mcp import ...` |
| `from agents.realtime import ...` | Non-conflicting Classic names at the original path; `agents.classic.realtime` for a wholly Classic namespace |
| `from agents.voice import ...` | Original Voice paths, including Classic single-agent workflows |
| `from agents.extensions import ...` | `from agents.classic.extensions import ...` |
| `from agents.testing import ...` | `from agents.classic.testing import ...` |

Use the same prefix for other retained public v0 submodules. Keep each workflow's Agent, tools, Runner, configuration, tracing, and session objects within classic. Libraries that import `agents` internally also need an update before they can work with classic objects. An import change in your application does not rewrite those libraries.

Check optional integrations against the matching source before upgrading. For example, this classic tree does not include v0.24.0's `agents.extensions.sandbox.runloop` integration. Keep a workflow that requires it on v0 until you can port that integration.

Follow the [invoice lookup walkthrough](examples/codex/migration/README.md#optional-stage-the-package-upgrade-through-classic) for the import change and focused tests against a released-v0 SQLite store.

Run `python examples/codex/classic.py` for a deterministic tool workflow. It needs no API key and sends no model or tracing requests. For live workflows, configure the classic provider and credentials as you did in v0.

## Introduce native workflows separately

You can select both APIs explicitly in one application:

```python
from agents import Agent as NativeAgent
from agents.classic import Agent as ClassicAgent, Runner
```

Classic preserves the Python loop, model providers, handoffs, guardrails, streamed results, and supported v0 session and RunState behavior. Native Agent and Session objects have their own execution and storage contracts. Do not pass classic tools, agents, sessions, or RunState objects into native APIs. Keep existing conversations in classic; start a fresh native conversation when porting a workflow. This package provides no automatic conversion to native checkpoints.

When the workflow can adopt native execution, follow the [native migration guide](MIGRATION.md#port-one-workflow). Port its declarations, provider settings, policies, event consumers, and state handling together. Classic does not automatically translate these contracts into native behavior.

Test application behavior after changing imports, including tool effects, guardrail ordering, streamed events, provider settings, and saved-state restoration. Application-defined callback and class locations may also participate in saved identity checks; keep those locations stable while testing the namespace change.

Keep a copy of persisted data and the previous dependency lock while migrating. Returning to the prior v0 environment remains an application deployment decision; do not write native checkpoints into a classic store.

The [Classic example collection](examples/classic/README.md) includes the established runtime examples, with explicit `agents.classic` imports.

## Classic lifecycle

Classic is retained in v1 and is not deprecated by this change. The intended sequence is deprecation in a later v1 minor release, followed by removal in v2. No specific release or date is set here. Use the staging period to complete the native port; see [the migration plan](MIGRATION.md#use-classic-only-when-staging-is-necessary).

## Compatible Voice and Realtime imports

Original Voice module paths expose the retained Classic objects. `agents.voice.SingleAgentVoiceWorkflow`, `SingleAgentWorkflowCallbacks`, and `VoiceWorkflowHelper` retain the Classic workflow implementation. `NativeSessionVoiceWorkflow` uses explicit native Session execution.

```python
from agents.classic import Agent
from agents.voice import SingleAgentVoiceWorkflow, VoicePipeline

workflow = SingleAgentVoiceWorkflow(Agent(name="Assistant", instructions="Help."))
pipeline = VoicePipeline(workflow=workflow)
```

Non-conflicting `agents.realtime` names, including `RealtimeAgent` and `RealtimeRunner`, load their retained Classic objects only when requested. `RealtimeRunner.run()` returns a Classic session. Original module-specific imports such as `agents.realtime.model` and `agents.realtime.session` retain Classic ownership. Wildcard imports from `agents.realtime` expose the native API with the base installation.

```python
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.session import RealtimeSession  # Retained Classic type.

runner = RealtimeRunner(RealtimeAgent(name="Assistant", instructions="Help."))
```

The top-level `agents.realtime` names `RealtimeSession`, `realtime_handoff`, `RealtimeAudio`, `RealtimeResponse`, `RealtimeUserInput`, `RealtimeUserInputMessage`, and `RealtimeUserInputText` retain their existing native identities. For a wholly Classic flow, import these names from `agents.classic.realtime`.

The package root retains native ownership. These aliases do not restore Classic root `Agent` or `Runner`, rewrite dependent-library imports, or route unsupported native operations through Classic.
