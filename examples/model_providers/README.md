# Custom LLM providers

The examples in this directory demonstrate how you might use a non-OpenAI LLM provider. To run them, first set a base URL, API key and model.

```bash
export EXAMPLE_BASE_URL="..."
export EXAMPLE_API_KEY="..."
export EXAMPLE_MODEL_NAME"..."
```

Then run the examples, e.g.:

```
python examples/model_providers/custom_example_provider.py

Loops within themselves,
Function calls its own being,
Depth without ending.
```


## LiteLLM Proxy Server integration

LiteLLM integration helps out switch between models easily and rapidly. This is easy to integrate via `AsyncOpenAI`:

```bash
# launch server proxy with your configuration
litellm --config examples/model_providers/litellm_config.yaml

# then use the proxy via the SDK
python3 examples/model_providers/litellm.py
```

### Testing the proxy server
Testing some basic models against proxy to verify it's operational:
```bash
# qwen2.5:14b
curl -s http://localhost:4000/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "qwen2.5:14b", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 10}' | jq

# claude-3-7
curl -s http://localhost:4000/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "claude-3-7", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 10}' | jq

# gpt-4o
curl -s http://localhost:4000/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 10}' | jq
```
