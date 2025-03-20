# Examples

Explore a variety of sample implementations of the SDK in the [examples](https://github.com/openai/openai-agents-python/tree/main/examples) section of the repository. These examples are organized into several categories, each demonstrating different patterns and capabilities.

## Table of Contents
- [Agent Patterns](#agent-patterns)
- [Basic](#basic)
- [Tools](#tools)
- [Model Providers](#model-providers)
- [Handoffs](#handoffs)
- [Customer Service](#customer-service)
- [Research Bot](#research-bot)
- [Voice](#voice)
- [Financial Research Agent](#financial-research-agent)

## Categories

### Agent Patterns
Examples in this category illustrate common agent design patterns:
- **[Agents as Tools](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/agents_as_tools.py)** – Using agents to accomplish complex tasks via other agents.
- **[Deterministic Workflows](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/deterministic.py)** – Creating predictable and repeatable agent behaviors.
- **[Forcing Tool Use](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/forcing_tool_use.py)** – Ensuring agents utilize specific tools.
- **[Input Guardrails](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/input_guardrails.py)** – Validating and constraining agent inputs.
- **[LLM as a Judge](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/llm_as_a_judge.py)** – Using language models to evaluate agent outputs.
- **[Output Guardrails](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/output_guardrails.py)** – Ensuring agent outputs conform to desired behaviors.
- **[Parallel Agent Execution](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/parallelization.py)** – Running multiple agents concurrently.
- **[Routing](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/routing.py)** – Directing tasks to appropriate agents.

### Basic
Foundational SDK capabilities:
- **[Agent Lifecycle Events](https://github.com/openai/openai-agents-python/blob/main/examples/basic/agent_lifecycle_example.py)** – Handling different stages of an agent’s lifecycle.
- **[Dynamic System Prompts](https://github.com/openai/openai-agents-python/blob/main/examples/basic/dynamic_system_prompt.py)** – Modifying system prompts dynamically.
- **[Hello World Examples](https://github.com/openai/openai-agents-python/blob/main/examples/basic/hello_world.py)** – Simple implementations to get started.
- **[Streaming Outputs](https://github.com/openai/openai-agents-python/blob/main/examples/basic/stream_text.py)** – Handling streaming responses.
- **[Tool Integration](https://github.com/openai/openai-agents-python/blob/main/examples/basic/tools.py)** – Extending agent functionality via tools.

### Tools
Implementing and integrating tools:
- **[Web Search Tool](https://github.com/openai/openai-agents-python/blob/main/examples/tools/web_search.py)** – Performing live web searches.
- **[File Search Tool](https://github.com/openai/openai-agents-python/blob/main/examples/tools/file_search.py)** – Searching within specified files.
- **[Computer Use Tool](https://github.com/openai/openai-agents-python/blob/main/examples/tools/computer_use.py)** – Simulating user interactions with a computer.

### Model Providers
Using non-OpenAI models with the SDK:
- **[Custom Example Agent](https://github.com/openai/openai-agents-python/blob/main/examples/model_providers/custom_example_agent.py)** – Setting up an agent with a custom model provider.
- **[Custom Example Global](https://github.com/openai/openai-agents-python/blob/main/examples/model_providers/custom_example_global.py)** – Configuring a global model provider.
- **[Custom Example Provider](https://github.com/openai/openai-agents-python/blob/main/examples/model_providers/custom_example_provider.py)** – Implementing a provider for specific use cases.

### Handoffs
Practical agent handoff scenarios:
- **[Message Filter](https://github.com/openai/openai-agents-python/blob/main/examples/handoffs/message_filter.py)** – Transferring tasks based on message content.
- **[Message Filter with Streaming](https://github.com/openai/openai-agents-python/blob/main/examples/handoffs/message_filter_streaming.py)** – Handling message filtering with streaming responses.

### Customer Service
A real-world example:
- **[Customer Service System](https://github.com/openai/openai-agents-python/blob/main/examples/customer_service/main.py)** – An airline customer service system handling FAQs and bookings.

### Research Bot
A simple multi-agent research bot:
- **[Research Bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot)** – Assists in gathering and summarizing information.

### Voice
Explore voice-related agent implementations:
- **[Voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice)** – General voice processing examples.
- **[Static Voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice/static)** – Handling pre-recorded voice data.
- **[Streamed Voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice/streamed)** – Processing real-time voice input.

### Financial Research Agent
A specialized example focusing on financial research:
- **[Financial Research Agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent)** – Assists in financial data analysis and research.

For a comprehensive understanding and to see these examples in action, visit the [examples directory](https://github.com/openai/openai-agents-python/tree/main/examples) in the repository.

