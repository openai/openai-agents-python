# Model provider examples

The examples in this directory show how to route models through adapter layers such as LiteLLM and
any-llm. The default examples all use OpenRouter so you only need one API key:

```bash
export OPENROUTER_API_KEY="..."
```

Run one of the adapter examples:

```bash
uv run examples/model_providers/any_llm_provider.py
uv run examples/model_providers/any_llm_auto.py
uv run examples/model_providers/litellm_provider.py
uv run examples/model_providers/litellm_auto.py
```

Direct-model examples let you override the target model:

```bash
uv run examples/model_providers/any_llm_provider.py --model openrouter/openai/gpt-5.4-mini
uv run examples/model_providers/litellm_provider.py --model openrouter/openai/gpt-5.4-mini
```

## Using Codex alongside these adapters

Setting `RunConfig(model_provider=CodexProvider())` sends model names to Codex. It does not make Codex run LiteLLM or any-llm. A model object passed to the agent handles inference directly. See the [Codex provider guide](../../docs/models/codex.md) for setup and limitations.

To keep both paths in the same application, register Codex under a prefix:

```python
from agents import MultiProvider, RunConfig
from agents.models.codex import CodexProvider
from agents.models.multi_provider import MultiProviderMap

routes = MultiProviderMap()
routes.add_provider("codex", CodexProvider())
config = RunConfig(model_provider=MultiProvider(provider_map=routes))
```

Pass `run_config=config` to `Runner.run` and select `model="codex/" + model_id` on the agent to use Codex. The existing `litellm/...` and `any-llm/...` names still select their original adapters. To change **Codex's own backend**, use its `config.toml` (for instance, to point to a local server with a streaming `/v1/responses` endpoint). An endpoint that only implements `/v1/chat/completions` is not sufficient.

For a prompt example that needs no hosted prompt ID, run:

```bash
uv run examples/basic/prompt_template.py --local
uv run examples/basic/prompt_template.py --local --codex
```

Here the application supplies the prompt text. For tool access, the [Calendar example](../hosted_mcp/connectors.py) includes an explicit SDK-managed MCP path. Neither example translates an OpenAI-hosted prompt or connector ID into Codex configuration.
