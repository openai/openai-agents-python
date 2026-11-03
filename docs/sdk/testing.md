# Test an agent without a model service

Use deterministic tests to check Python tools, application state and the requests your agent sends. Test model quality separately with a live model and representative inputs.

`agents.testing.ScriptedResponses` starts a local HTTP server that supplies scripted model responses. The real native Session and Python tools still run. Configure a direct connection or proxy bypass for `127.0.0.1`, including applicable operating-system proxy settings. Keep that configuration while the server and Session run. The server follows current proxy configuration. It does not isolate network access.

After installing the matching SDK, save this complete script as `test_agent.py`. It uses synthetic data and needs no real model API key:

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

Each list supplies one model response. On a successful exit from `async with ScriptedResponses(...)`, the peer checks that the run used all responses and made no unexpected requests. If the test or native loop raised an exception, the peer preserves it. Close all Sessions before leaving the peer context. One-shot `agent.run()` does this before returning. The peer records request bodies, including prompts and tool results, for you to inspect. It does not record HTTP headers.

The peer does not simulate model reasoning, MCP servers, executor tools or WebSockets, and it never chooses a fallback provider. Use the request snapshots and `tool_output(call_id)` to check what the native loop submitted. Keep prompts, scripted responses and tool data synthetic.

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

Other snippets in this guide are fragments that reuse the complete script above.

## Choose assertions that catch application mistakes

The first response asks the real runtime to call your tool. The second supplies the final text. The final text is scripted, so checking it alone does not prove the tool ran correctly. Check `peer.tool_output(call_id)` and relevant `peer.requests` fields as well. On a successful exit, the peer checks that the run used every scripted response exactly once.

Use a new peer for each independent test. Keep request fixtures synthetic and avoid publishing sensitive snapshots.

These tests verify application behavior under the supplied responses. They do not establish that a live model chooses the right tool, follows instructions, or produces a useful answer. Evaluate those outcomes separately. See [troubleshooting](troubleshooting.md) for unexpected requests and runtime failures.

## Evaluate with a live model

Keep a small set of representative cases beside your application tests. Define success before running them. For example, if your greeting agent must call `greeting` for a supplied name and ask for a missing name, include these cases:

| Input | Expected behavior |
| --- | --- |
| `Greet Ada.` | Call `greeting` with `name="Ada"` and return a greeting for Ada. |
| `Please welcome Grace.` | Recognize the request, call `greeting` with `name="Grace"`, and return a greeting for Grace. |
| `Greet me.` | Ask for the person's name without inventing one or calling the tool. |

Use synthetic names and add cases that represent your application's actual failures. Check required facts and behavior. Allow harmless differences in wording.

1. Create the agent with your candidate instructions, tools and normal model and provider configuration. Do not pass `peer.provider` or run inside `ScriptedResponses`. Supply the real provider credentials from your environment. These runs make live model requests and can incur charges.
2. Call `await agent.run(input)` for each case so independent cases start fresh. For a conversation test, open a new Session for that case and submit its messages in order. Start with a fixed number of trials per case, such as three. Set that number and the case set before comparing changes.
3. Record the case, trial, SDK revision, instructions, model and provider settings, final output, failure and elapsed time. Record received arguments and returned values inside your Python tool function. For this example, capture `name` and the greeting before returning it. Keep the data synthetic. Check these call records as well as the answer. A plausible greeting alone does not prove the required tool ran. Record available [usage](troubleshooting.md#read-usage-without-double-counting) with its accounting limits.
4. Run the same cases against the previous configuration. Compare successful trials, failure types, latency and available usage. Inspect every changed failure before accepting a candidate. Keep some cases out of instruction tuning, and expand the set when real failures reveal missing coverage.

Three trials can catch obvious failures, but they do not give a reliable estimate of success rates. Choose a larger evaluation set and trial count for consequential applications. Keep these live checks separate from the deterministic pytest suite. This separates service failures and model variation from application regressions. A passing scripted test cannot substitute for a passing live evaluation.

To repeat cases, record configurations, compare a baseline and save failure details, use the [evaluation runner](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/evaluation/run.py). Its synthetic mode checks the test setup. Live runs measure model behavior and need a separate review of their results. Neither scripted responses nor a confidence value from a model shows that the model's confidence matches its actual rate of success.
