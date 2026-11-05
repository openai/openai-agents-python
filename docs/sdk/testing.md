# Test an agent without a model service

Use deterministic tests to check Python tools, application state and the requests your agent sends. Test model quality separately with a live model and representative inputs.

`agents.testing.ScriptedResponses` starts a local HTTP server with scripted responses. The native Session and Python tools still run. The native provider follows process and operating-system proxy settings, so keep a direct connection or proxy bypass for `127.0.0.1` while the server and its Sessions run. The server does not isolate network access.

## Write a tool test

After [installing the matching SDK](installation.md), save this complete script as `test_agent.py`. It uses synthetic data and needs no real model API key:

<!-- docs-check: run -->

```python
import asyncio

from agents import Agent, function_tool
from agents.testing import ScriptedResponses, text, tool_call


@function_tool
async def greeting(name: str) -> str:
    return f"Hello, {name}!"


async def main() -> None:
    async with ScriptedResponses(
        [tool_call("greeting", {"name": "Ada"}, call_id="greeting-1")],
        [text("Hello, Ada!")],
    ) as peer:
        agent = Agent(instructions="Greet the user.", tools=[greeting], provider=peer.provider)
        result = await agent.run("Greet Ada.")
        assert result.final_output == "Hello, Ada!"
        assert peer.tool_output("greeting-1") == "Hello, Ada!"
        assert len(peer.requests) == 2
        print("Passed: greeting tool result and two model requests.")


if __name__ == "__main__":
    asyncio.run(main())
```

Each list supplies one model response. On successful context exit, the peer checks that the run used every response exactly once and made no unexpected requests. If the test or native loop raises an exception, the peer preserves it.

Close Sessions before leaving the peer context. One-shot `agent.run()` does this automatically. The peer records request bodies, including prompts and tool results, but not HTTP headers. Keep test data synthetic.

## Run the check

Use the Python interpreter from your SDK environment:

```sh
python test_agent.py
```

Expected output:

```text
Passed: greeting tool result and two model requests.
```

For pytest, install `pytest` in the same environment and add this function to `test_agent.py`. Keep the script's `main()` and its assertions:

```python
def test_greeting() -> None:
    asyncio.run(main())
```

```sh
python -m pip install pytest
python -m pytest -q test_agent.py
```

Pytest imports the file without running its `__main__` block, then calls `test_greeting()`. This test does not need an async pytest plugin.

## Choose assertions that catch application mistakes

The first response asks the runtime to call your tool. The second scripts the final text, so checking that text alone cannot prove the tool ran correctly. Check `peer.tool_output(call_id)` and relevant `peer.requests` fields too.

Use a new peer for each independent test. Keep request fixtures synthetic and avoid publishing sensitive snapshots. See [troubleshooting](troubleshooting.md) for unexpected requests and runtime failures.

The peer never chooses a fallback provider or simulates model reasoning, MCP servers, executor tools or WebSockets. Scripted tests verify behavior under supplied responses. Evaluate live tool choice, instruction following and answer quality separately.

## Evaluate with a live model

Keep a small set of representative cases beside your application tests. Define success before running them. For example, if your greeting agent must call `greeting` for a supplied name and ask for a missing name, include these cases:

| Input | Expected behavior |
| --- | --- |
| `Greet Ada.` | Call `greeting` with `name="Ada"` and return a greeting for Ada. |
| `Please welcome Grace.` | Recognize the request, call `greeting` with `name="Grace"`, and return a greeting for Grace. |
| `Greet me.` | Ask for the person's name without inventing one or calling the tool. |

Use synthetic names and add cases for actual application failures. Check required facts and behavior, allowing harmless wording differences.

### Run and compare trials

1. Configure the candidate agent with its instructions, tools, normal model and provider settings, and environment credentials. Do not use `peer.provider` or `ScriptedResponses`. Live requests can incur charges.
2. Fix the cases and trial count before comparing changes. Start independent cases with `await agent.run(input)`. For a conversation case, open a fresh Session and submit its messages in order.
3. Record each trial and check tool calls as well as the answer. A plausible greeting does not prove `greeting` ran.
4. Run the same cases with the previous configuration. Compare successes, failure types, latency and available usage. Inspect changed failures before accepting the candidate.

Record these trial details:

- Case, trial, SDK revision, instructions, and model and provider settings.
- Final output, failure, elapsed time and available [usage](observability.md#read-usage-without-double-counting), with its accounting limits.
- Arguments and returned values captured inside each Python tool. For `greeting`, capture `name` and the greeting before returning it. Keep those records synthetic.

### Interpret the results

Three trials per case can catch obvious failures, but cannot give a reliable success-rate estimate. Use more trials and cases for consequential applications. Keep some cases out of instruction tuning, and add coverage when real failures reveal gaps.

Keep live evaluations separate from deterministic pytest tests so service failures and model variation do not obscure application regressions. Passing one does not replace passing the other.

Follow the [evaluation runner guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/evaluation/README.md) to repeat cases, record configurations, compare a baseline and save failure details. The runner's scripted mode checks the test setup. Live results need outcome review. Neither scripted responses nor model confidence shows that confidence matches the actual success rate.
