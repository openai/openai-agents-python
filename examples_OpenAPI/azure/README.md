# Azure OpenAI Examples

This directory contains examples demonstrating how to use the Agents SDK with Azure OpenAI service. The examples cover a variety of agent patterns and use cases.

## Basic Examples

- **hello_world.py**: Simple "Hello World" example using Azure OpenAI
- **hello_world_jupyter.py**: Example for use in Jupyter notebooks
- **tools.py**: Using function tools with Azure OpenAI
- **stream_text.py**: Streaming text responses from Azure OpenAI
- **stream_items.py**: Streaming various item types from Azure OpenAI
- **dynamic_system_prompt.py**: Dynamic system prompts with Azure OpenAI
- **life_cycle_example.py**: Agent lifecycle hooks with Azure OpenAI
- **agent_lifecycle_example.py**: More advanced agent lifecycle examples

## Agent Patterns

These examples showcase common agent patterns adapted for Azure OpenAI:

- **parallelization.py**: Running agents in parallel and selecting best results
- **agents_as_tools.py**: Using agents as tools for other agents
- **deterministic.py**: Sequential multi-step workflows
- **forcing_tool_use.py**: Forcing agents to use specific tools
- **input_guardrails.py**: Adding guardrails for agent inputs
- **llm_as_a_judge.py**: Using one agent to evaluate another's output
- **output_guardrails.py**: Adding guardrails for agent outputs
- **routing.py**: Routing user requests to specialized agents

## Advanced Examples

- **research_bot/**: Multi-agent system for web-based research
- **financial_research_agent/**: Financial analysis agent system
- **voice/**: Voice interaction examples using Azure OpenAI

## Getting Started

To run these examples, you'll need Azure OpenAI credentials. Set the following environment variables:

```bash
export AZURE_OPENAI_API_KEY="your_api_key"
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com"
export AZURE_OPENAI_API_VERSION="2023-05-15" # Optional
export AZURE_OPENAI_DEPLOYMENT="deployment_name" # Optional
```

Then run any example using Python:

```bash
python -m examples_OpenAPI.azure.basic.hello_world
```

## Additional Resources

For more information on using Azure OpenAI service, refer to:
- [Azure OpenAI Documentation](https://learn.microsoft.com/en-us/azure/ai-services/openai/)
- [Azure OpenAI Service Pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/)
- [Azure OpenAI Service Quotas and Limits](https://learn.microsoft.com/en-us/azure/ai-services/openai/quotas-limits)
