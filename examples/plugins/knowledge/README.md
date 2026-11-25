# Knowledge search

Give an agent read access to documents available to the signed-in user. Supply a search client through the local run context. Keep document access checks in your service and apply them to every request, including requests with search operators. Treat the query as untrusted.

## Try it offline

Use Python 3.10 or later and an environment with a plugin-enabled `openai-agents` build. Check for the preview API before installing this package from its directory.

```sh
python -c "from agents.plugins import Plugin"
python -m pip install --no-deps .
python -m unittest discover -s tests
```

Save the following as `demo.py` and run `python demo.py`. Expect `Open 9 to 5 [desk.hours]` from a scripted model and a synthetic desk directory. No API key required.

```python
import asyncio
import json

from agents import Agent, RunConfig, Runner
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents_knowledge import Hit, knowledge


class Desk:
    async def search(self, query: str, *, limit: int) -> list[Hit]:
        if query == "hours":
            return [Hit("desk.hours", "Open 9 to 5")][:limit]
        return []


def answer(call):
    item = next(i for i in call.input if i.get("type") == "function_call_output")
    hit = json.loads(item["output"])["results"][0]
    return [assistant_message(f"{hit['excerpt']} [{hit['source']}]")]


async def main():
    model = ScriptedModel(
        [
            [function_call("knowledge_search", {"query": "hours"}, call_id="lookup")],
            ModelStep.respond(answer),
        ]
    )
    agent = Agent("Reader", model=model, plugins=[knowledge()])
    result = await Runner.run(
        agent,
        "When is the desk open?",
        context=Desk(),
        run_config=RunConfig(tracing_disabled=True),
    )
    model.assert_complete()
    print(result.final_output)


asyncio.run(main())
```

## Connect a service

Implement the same asynchronous `search(query, *, limit)` method. Supply a client scoped to one user on each run. For a richer application context, use `knowledge(lambda context: context.client)`. Avoid capturing a user or client in a shared factory. Keep conversation history separate per user.

- Supply only excerpts and source IDs suitable for disclosure to the model.
- Use stable source IDs of 1–64 ASCII letters, digits, periods, underscores
or hyphens. Resolve IDs in your application with fresh authorization.
- Expect at most three hits, each with the first 512 characters of its excerpt.
Use nonblank queries of up to 256 characters.
- Expect `{"results":[]}` for no matches and an `error` message for invalid
queries, invalid hits or service failures. Set request timeouts in the client.

Treat generated citations as suggestions. Verify them against the returned sources before displaying links.

## Use a configured provider

Use the following function with an authenticated provider and your chosen model name. Allow for inference charges. For Codex, install a compatible SDK build and its matching native binding, then pass `use_codex=True`.

```python
from agents import Agent, RunConfig, Runner
from agents_knowledge import knowledge


async def ask(client, model_name, question, *, use_codex=False):
    agent = Agent("Reader", model=model_name, plugins=[knowledge()])
    if not use_codex:
        return await Runner.run(
            agent, question, context=client, run_config=RunConfig(tracing_disabled=True)
        )

    from agents.models.codex import CodexProvider

    provider = CodexProvider()
    try:
        return await Runner.run(
            agent,
            question,
            context=client,
            run_config=RunConfig(model_provider=provider, tracing_disabled=True),
        )
    finally:
        await provider.aclose()
```
