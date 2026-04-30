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

## Perplexity

Perplexity exposes an OpenAI-compatible chat completions endpoint, so you can route
requests through an `AsyncOpenAI` client by overriding `base_url`:

```bash
export PERPLEXITY_API_KEY="..."
uv run examples/model_providers/perplexity_provider.py
```

See [`perplexity_provider.py`](perplexity_provider.py) for the full setup. A search-tool
example using Perplexity's Search API is at
[`examples/tools/perplexity_search.py`](../tools/perplexity_search.py).
