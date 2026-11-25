# LangSmith outcome traces

One best-effort export per Runner submission, including approval pauses and failures. Enable it on the starting agent with `plugins=[langsmith(client)]`. No global trace processor, model spans or tool spans.

## Try it offline

Use Python 3.10+ with the [v0 plugin preview wheel](../README.md#install-the-preview). From this directory, install the plugin and run the console demo.

```sh
python -m pip install .
python -m agents_langsmith
```

Expected output with a scripted model and no LangSmith client, account or network request

```text
Demo completed
Ready
```

For an HTTP test, use Python 3.11+ and install the optional `langsmith==0.15.0` client.

```sh
python -m pip install '.[example]'
python -I -m unittest discover -s tests
```

See the [local test](tests/test_langsmith.py) for a loopback `api_url`, a synthetic `api_key` and privacy checks on `POST /runs`.

## Send a trace to LangSmith

On Python 3.11+, install the same extra and set `LANGSMITH_API_KEY` for your service. Check the endpoint and workspace before running this example. Expect a real export with a scripted model.

```python
from langsmith import Client

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message
from agents_langsmith import langsmith

client = Client(
    timeout_ms=3000,
    omit_traced_runtime_info=True,
    auto_batch_tracing=False,
)
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

Look for a chain named `Agent run` in the `Desk` project with `{"status": "completed"}` in its outputs. Default project — `Agents`. To disable this export, remove the plugin from the starting agent, even with `tracing_disabled=True`.

## Data and delivery

- **Status** — `completed`, `paused` for approval, `failed`, or `cancelled`. Expect a separate trace on resume. For streams, consume `stream_events()` through cleanup.
- **Data** — A fresh UUID, project, fixed name, status and UTC times. End time at the callback and start time estimated from the SDK duration. No prompts, answers, SDK identifiers, agent names, tool data or exception text from the plugin. Keep `omit_traced_runtime_info=True` to exclude the client's default runtime and environment metadata.
- **Delivery** — One [`Client.create_run`](https://reference.langchain.com/python/langsmith/client/Client/create_run) call on a worker thread. With the settings above, a direct write with no background queue or need to flush. Allow for client sampling, filters and retries. Check the project to confirm storage, even after a successful run or `client.close()`.
- **Errors** — On a client exception, expect a generic SDK warning and the original run outcome. Check the client's own logs for sensitive details before sharing them.
- **Cleanup** — Use an app-owned synchronous client. Close it after all submissions and streams finish. On cancellation during export, expect a wait for the client call, then the original cancellation. In that case, look for the status from before the interruption. Set client timeouts and account for retries to avoid an indefinite wait.
