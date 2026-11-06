# Migrate existing workflows with the classic API

The source on this branch includes `agents.classic`. It retains the v0 Python runner and its integrations. The root `agents` API continues to use native Codex. This addition is not available in the previously published preview wheels.

The classic source comes from v0 `main` commit `814de8b1cfdaf6c70212dca9ca23563dd0b789ac`, with v0.24.0 as the released compatibility baseline. It includes later fixes from that pinned commit. Its namespace changes do not move execution onto Codex.

## Install from the matching source

Follow [the contributor setup](CONTRIBUTING.md) for the native build prerequisites, then install this source with the classic extra:

```sh
python -m pip install '.[classic]'
```

Classic is part of the same `openai-agents` distribution. The extra installs the dependencies needed by the old Python runner. It does not remove the native wheel's platform requirements or source-build prerequisites. Keep using the existing v0 release in its own environment if your platform cannot install the new SDK.

Optional v0 integrations use `classic-` prefixed extras, for example `.[classic,classic-voice]`, `.[classic,classic-litellm]`, or `.[classic,classic-sqlalchemy]`. Install only the integrations your application uses. The base classic extra does not install every optional service client.

### SQLite session drivers

`AsyncSQLiteSession` requires `aiosqlite` in addition to the classic extra:

```sh
python -m pip install '.[classic]' aiosqlite
```

`SQLAlchemySession` requires an async driver that matches your database URL. For SQLite URLs that start with `sqlite+aiosqlite://`, install:

```sh
python -m pip install '.[classic-sqlalchemy]' aiosqlite
```

The `classic-sqlalchemy` extra includes the classic runtime and `asyncpg` for PostgreSQL URLs that start with `postgresql+asyncpg://`. Install drivers for other databases separately, as in v0.

### Experimental Codex integration

`agents.classic.extensions.experimental.codex` runs the Codex CLI as a separate process. Install the CLI separately and make `codex` available on `PATH`, or select its executable with `CODEX_PATH` or `Codex(codex_path_override="/absolute/path/to/codex")`. This distribution does not bundle that CLI. The native SDK helper executables serve the root `agents` API and are not replacements for the CLI used by this integration.

## Change imports before changing behavior

Change imports for each existing workflow together:

| Existing import | Classic import |
| --- | --- |
| `from agents import Agent, Runner, function_tool` | `from agents.classic import Agent, Runner, function_tool` |
| `from agents.models import ...` | `from agents.classic.models import ...` |
| `from agents.tracing import ...` | `from agents.classic.tracing import ...` |
| `from agents.mcp import ...` | `from agents.classic.mcp import ...` |
| `from agents.realtime import ...` | `from agents.classic.realtime import ...` |
| `from agents.voice import ...` | `from agents.classic.voice import ...` |
| `from agents.extensions import ...` | `from agents.classic.extensions import ...` |
| `from agents.testing import ...` | `from agents.classic.testing import ...` |

The same prefix change applies to other public v0 submodules. Keep each workflow's Agent, tools, Runner, configuration, tracing and session objects within classic. Libraries that import `agents` internally also need an update before they can work with classic objects. An import change in your application does not rewrite those libraries.

Run `python examples/codex/classic.py` for a deterministic tool workflow. It needs no API key and sends no model or tracing requests. For live workflows, configure the classic provider and credentials as you did in v0.

## Introduce native workflows separately

You can select both APIs explicitly in one application:

```python
from agents import Agent as NativeAgent
from agents.classic import Agent as ClassicAgent, Runner
```

Classic preserves the Python loop, model providers, handoffs, guardrails, streamed results, and supported v0 session and RunState behavior. Native Agent and Session objects have their own execution and storage contracts. Do not pass classic tools, agents, sessions or RunState objects into native APIs. Keep existing conversations in classic; start a fresh native conversation when porting a workflow. This package provides no automatic conversion to native checkpoints.

For a supported single-agent workflow, the experimental [SDKRunner migration adapter](MIGRATION.md) can run an existing Classic Agent and function tools through the native loop. It has a narrower contract than the Classic Runner and rejects unsupported features before execution. Keep workflows that require those features on Classic until you port them to the native API.

Test application behavior after changing imports, including tool effects, guardrail ordering, streamed events, provider settings and saved-state restoration. Application-defined callback and class locations may also participate in saved identity checks; keep those locations stable while testing the namespace change.

Keep a copy of persisted data and the previous dependency lock while migrating. Returning to the prior v0 environment remains an application deployment decision; do not write native checkpoints into a classic store.

The [Classic example collection](examples/classic/README.md) includes the established runtime examples, with explicit `agents.classic` imports.
