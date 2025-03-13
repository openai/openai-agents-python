# LiteLLM Provider Examples

This directory contains examples demonstrating how to use the LiteLLM provider with the Agents SDK.

## Prerequisites

1. Install and run the LiteLLM proxy server:
```bash
pip install litellm
litellm --model ollama/llama2 --port 8000
```

2. Set up environment variables:
```bash
# LiteLLM configuration
export LITELLM_API_KEY="your-litellm-api-key"  # If required by your proxy
export LITELLM_API_BASE="http://localhost:8000"

# Provider API keys (as needed)
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export GEMINI_API_KEY="..."
```

## Examples

### Multi-Provider Workflow (`multi_provider_workflow.py`)

This example demonstrates using multiple LLM providers in a workflow:

1. A triage agent (using OpenAI directly) determines the task type
2. Based on the task type, it routes to specialized agents:
   - Summarization tasks → Claude (via LiteLLM)
   - Coding tasks → GPT-4 (via LiteLLM)
   - Creative tasks → Gemini (via LiteLLM)

The example uses `RunConfig` to specify which provider to use for each agent:

```python
# For OpenAI provider
openai_config = RunConfig(model_provider=openai_provider)
result = await Runner.run(triage_agent, input="...", run_config=openai_config)

# For LiteLLM provider
litellm_config = RunConfig(model_provider=litellm_provider)
result = await Runner.run(target_agent, input="...", run_config=litellm_config)
```

To run:
```bash
python examples/litellm/multi_provider_workflow.py
```

The example will process three different types of requests to demonstrate the routing:
1. A summarization request about the French Revolution
2. A coding request to implement a Fibonacci sequence
3. A creative writing request about a time-traveling coffee cup

## Notes

- The LiteLLM provider automatically routes model names to their appropriate providers (e.g., `claude-3` → Anthropic, `gpt-4` → OpenAI)
- You can explicitly specify providers using prefixes (e.g., `anthropic/claude-3`, `openai/gpt-4`)
- The provider handles passing API keys and configuration through headers
- All Agents SDK features (handoffs, tools, tracing) work with the LiteLLM provider
- Use `RunConfig` to specify which provider to use when calling `Runner.run()` 