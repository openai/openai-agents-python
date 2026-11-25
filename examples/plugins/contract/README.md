# Test an installed plugin

Use this kit to test a function tool or [observe a run without a tool call](#test-an-observer). The helper checks that the agent offers the tool and that the expected text reaches the next model request. Your test checks what the tool reads or changes. The model's final answer is scripted, so it cannot prove that a write happened.

## Run the ticket test

Copy this directory outside the SDK and plugin source trees. Use a fresh Python 3.10+ environment with matching plugin-enabled `openai-agents` and `agents-ticket-desk` wheels installed. Keep the test and its helpers together. Run both commands from the copied directory.

```sh
python -m pip check
python -EsB test_ticket.py
```

Expect the package versions, `Python (ScriptedModel)`, and `OK`. An assertion or import error exits with a nonzero status. For test discovery, use `python -I -m unittest discover -s .`. The test uses no live inference or ticket service.

For a plugin-enabled SDK build with a compatible native Codex binding, run `python -EsB test_ticket.py --codex`. This uses the Codex provider, a temporary `CODEX_HOME`, and synthetic Responses from a local HTTP server. The server checks the client on each request. The test fails if the native route is unavailable. It covers SDK function tools only.

If startup reports that `socksio` is missing and your environment uses a SOCKS proxy, install the optional transport in the test environment with `python -m pip install 'httpx2[socks]'`. For local fixtures only, if your policy permits direct loopback and the extra is unavailable, prefix the test command with `env -u ALL_PROXY -u all_proxy`. Keep proxy settings in place for installs, downloads and remote services.

## Adapt the checks

In [test_ticket.py](test_ticket.py), replace the package import, distribution name, plugin factory, and test client with yours. Pass the tool name, arguments, and expected text to [`expect_tool_output`](plugin_contract.py). Use the returned model on your agent and the run config on each `Runner.run` call. Include any other plugins you use on that agent.

For a write, inspect the proposed call and check for effects before and after review. The ticket test gives two tenants the same ticket ID and checks four cases.

| Case | Calls to the desk | Writes | Text on the next turn |
| --- | --- | --- | --- |
| Approved for the owner | 1 | 1 for the owner | `closed` |
| Rejected | 0 | 0 | `declined` |
| Grant revoked after approval | 1 | 0 | `denied` |
| Other tenant uses the owner's agent | 0 | 0 | `denied` |

For a tool without review, remove the interruption and resume steps. Keep the check for effects and let the run continue to the second model turn.

## Test an observer

A tracing plugin can work without a tool call. Pass `calls=[]` to [the local model server](loopback.py) for a run that ends after one response. Save this example as `check_observer.py` in the copied contract directory.

```python
import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import Agent, RunConfig, Runner
from agents.plugins import Plugin, PluginHooks
from loopback import MODEL, route


async def main(workspace, codex):
    outcomes, requests = [], []
    observer = Plugin(name="audit", hooks=PluginHooks(on_run_end=outcomes.append))
    async with route(codex, workspace, requests, calls=[]) as provider:
        agent = Agent("Support", model=MODEL, plugins=[observer])
        config = RunConfig(model_provider=provider, tracing_disabled=True)
        result = await Runner.run(agent, "private test prompt", run_config=config)
    assert len(requests) == 1
    assert result.final_output == "done"
    assert [outcome.status for outcome in outcomes] == ["completed"]
    print("One completed run")


with TemporaryDirectory() as tmp:
    os.environ["CODEX_HOME"] = tmp
    asyncio.run(main(Path(tmp), "--codex" in sys.argv))
```

Run `python -EsB check_observer.py` for Python. With the combined wheel, run `python -EsB check_observer.py --codex` for Codex. Expect `One completed run` each time.

For a trace exporter, replace the in-memory observer with your plugin. Check the project, status and record count at the receiving service. Verify that the private test prompt stays out of the trace. Run once without the plugin and expect no new record. For a tool that requires approval, check for separate records at the pause and after the decision. A rejected tool can still be part of a completed run.

## Test with the upstream Hello World agent

Use a copy of this directory and the preview 2 environment with `agents-specialist==0.3.0` installed. The [upstream sample](https://github.com/a2aproject/a2a-samples/tree/b7a2a689ff3a86e756386aa488e3f7b02a355590/samples/python/agents/helloworld) runs a real A2A server on your machine. It echoes a fixed test question. It requires no account or model key. The model in the test still uses local fixtures. The service assigns a fresh ID to each task's artifact.

**Terminal 1.** Start in the copied directory. Use a separate virtual environment for the server because the sample and the plugin use different versions of the A2A library. These commands download two source files at a pinned revision. Check that both files pass the checksum check and review the code before running it. Make sure port 9999 is free. Keep the server log private.

```sh
mkdir .a2a-peer
base=https://raw.githubusercontent.com/a2aproject/a2a-samples/b7a2a689ff3a86e756386aa488e3f7b02a355590/samples/python/agents/helloworld
curl -fsS "$base/__main__.py" -o .a2a-peer/__main__.py
curl -fsS "$base/agent_executor.py" -o .a2a-peer/agent_executor.py
sha256sum -c <<'CHECKSUMS'
d32cadcdd9ee146258902534e70a283795051d46c89c0ca7af50bf39aee4de71  .a2a-peer/__main__.py
4ca9015d8a626fb141c619e6dd81027550b2d154eeeb993106c972d627e71ae6  .a2a-peer/agent_executor.py
CHECKSUMS
```

After both checks pass, start the server in terminal 1.

```sh
python3.12 -m venv .a2a-peer/venv
server=.a2a-peer/venv/bin/python
"$server" -m pip install 'a2a-sdk[http-server]==1.1.0' 'uvicorn==0.54.0'
"$server" -EsBu .a2a-peer/__main__.py >.a2a-peer/server.log 2>&1
```

**Terminal 2.** Start in the same copied directory. Activate the plugin environment with `. /path/to/plugin-venv/bin/activate`, substituting the path from the install step. Save the local Agent Card, which describes the service. The test pins the destination to the sample's loopback address and uses a synthetic token.

```sh
curl -fsS --noproxy '*' http://127.0.0.1:9999/.well-known/agent-card.json -o .a2a-peer/card.json
python -EsB check_helloworld.py .a2a-peer/card.json .a2a-peer/server.log
```

With the combined Linux wheel, repeat the last command with `--codex`. Each run first rejects a call. The server log must show zero new requests. It then approves a call. The log must show one new POST and one new echo. The test also checks the actual tool result on the next model turn. Keep other clients disconnected during this check. Stop the server with Ctrl+C in terminal 1.

The [test code](check_helloworld.py) passes `artifact_id=None` because this service returns one artifact with a new ID. It accepts only the exact greeting for the fixed question. Keep a fixed ID when your service promises one. The sample only listens on loopback. Use an approved HTTPS endpoint, a trusted card, real authorization and service-side evidence for a partner deployment.

## Check upgrades and rollbacks

Keep the host test unchanged. Run it with the current wheels, the proposed wheels, and the original wheels again. Use a fresh environment for each pair. Record the wheel hashes, Python version, route, and exit status. Repeat on each route you support.

These tests start fresh runs and resume in memory. If your app saves approvals, also test checkpoints across deployments. Bind each checkpoint to its authenticated owner and use a new `plugin_revision` when policy or tool behavior changes, including on rollback. Check that stale approvals cannot cause writes.
