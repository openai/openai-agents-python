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

[OrcaRouter](https://www.orcarouter.ai) is another OpenAI-compatible gateway that exposes a
single base URL across many models, with adaptive routing, automatic failover, and
gateway-level security for AI agents. To try it with a direct model, set the
`ORCAROUTER_API_KEY` environment variable and run the named example:

```bash
export ORCAROUTER_API_KEY="..."
uv run examples/model_providers/custom_example_orcarouter.py
```

By default the example uses `gpt-5.6-luna`; override it with the `ORCAROUTER_MODEL`
environment variable.
