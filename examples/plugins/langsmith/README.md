# LangSmith outcome traces

Use the [v0 plugin preview wheel](../README.md#install-the-preview) in the same environment as this package.

One LangSmith trace per Runner submission on the starting agent. Status and duration only. No global trace processor or model and tool spans.

## Offline demo

Python 3.10+ and a local Agents SDK preview with `PluginHooks.on_run_end`. Installation from this directory in the preview environment

```sh
python -m pip install .
python -m agents_langsmith
```

Expected output with a scripted model and a console client

```text
Demo completed
Ready
```

## Send a trace to LangSmith

For the hosted service, your key in the `LANGSMITH_API_KEY` environment variable and the Python client

```sh
python -m pip install langsmith==0.15.0
```

One real export to the `Desk` project, with no live model call

```python
from langsmith import Client

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message
from agents_langsmith import langsmith

client = Client(timeout_ms=3000)
try:
    agent = Agent(
        name="Support",
        model=ScriptedModel([[assistant_message("Ready")]]),
        plugins=[langsmith(client, project_name="Desk")],
    )
    Runner.run_sync(agent, "Check status", run_config=RunConfig(tracing_disabled=True))
finally:
    client.close()
```

Expected in the `Desk` project — a chain named `Agent run` with `{"status": "completed"}` in its outputs. Default project — `Agents`.

## Scope and privacy

- **Activation** — Only through `plugins` on the starting agent, even with `tracing_disabled=True`. No exports from this plugin without that selection.
- **Status** — `completed`, `paused` for approval, `failed`, or `cancelled`. A separate trace on resume. For streams, full consumption of `stream_events()` through cleanup.
- **Data** — A fresh UUID, project, fixed name, status and UTC times. End time at callback delivery and start time estimated from the SDK duration. No prompt, answer, SDK identifiers, agent names, tool data or exception text. Possible extra runtime metadata from the [LangSmith client](https://github.com/langchain-ai/langsmith-sdk/blob/main/python/langsmith/client.py).
- **Delivery** — One best-effort [`Client.create_run`](https://reference.langchain.com/python/langsmith/client/Client/create_run) call on a worker thread. Possible delay in run completion. On a client exception, a generic SDK warning and no change to the run outcome. Client timeouts, retries, credentials and cleanup under application control.
