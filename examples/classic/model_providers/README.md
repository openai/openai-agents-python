# Model provider examples

These examples route model requests through LiteLLM or any-llm. Complete [Classic setup](../../../CLASSIC.md), then install the matching wheel with `classic-litellm` for LiteLLM or `classic-any-llm` for any-llm. The any-llm extra requires Python 3.11 or later.

The adapter examples use OpenRouter and disable SDK trace export. Set its API key:

```bash
export OPENROUTER_API_KEY="..."
```

From the repository root, with the SDK environment activated:

```bash
python examples/classic/model_providers/any_llm_provider.py
python examples/classic/model_providers/any_llm_auto.py
python examples/classic/model_providers/litellm_provider.py
python examples/classic/model_providers/litellm_auto.py
```

The direct-model variants accept a model override:

```bash
python examples/classic/model_providers/any_llm_provider.py --model openrouter/openai/gpt-5.4-mini
python examples/classic/model_providers/litellm_provider.py --model openrouter/openai/gpt-5.4-mini
```

The `custom_example_agent.py`, `custom_example_global.py`, and `custom_example_provider.py` scripts demonstrate custom OpenAI-compatible clients at agent, global, and provider scope. Their setup blocks read `EXAMPLE_BASE_URL`, `EXAMPLE_API_KEY`, and `EXAMPLE_MODEL_NAME`. These settings apply to those scripts separately from the OpenRouter examples.
