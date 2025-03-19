# Examples

Explore a variety of sample implementations of the SDK in the [examples](https://github.com/openai/openai-agents-python/tree/main/examples) section of the repository. These examples are organized into several categories, each demonstrating different patterns and capabilities.

## Categories

- **agent_patterns:**
  Examples in this category illustrate common agent design patterns, such as:
  - **[Agents as Tools](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/agents_as_tools.py):** Demonstrates how agents can utilize other agents as tools to accomplish complex tasks.
  - **[Deterministic Workflows](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/deterministic.py):** Showcases how to create predictable and repeatable agent behaviors.
  - **[Forcing Tool Use](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/forcing_tool_use.py):** Illustrates methods to ensure agents utilize specific tools during their operations.
  - **[Input Guardrails](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/input_guardrails.py):** Explores techniques to validate and constrain agent inputs to maintain system integrity.
  - **[LLM as a Judge](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/llm_as_a_judge.py):** Demonstrates using language models to evaluate and provide feedback on agent outputs.
  - **[Output Guardrails](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/output_guardrails.py):** Focuses on methods to validate and constrain agent outputs to ensure desired behavior.
  - **[Parallel Agent Execution](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/parallelization.py):** Showcases how to run multiple agents concurrently to improve efficiency.
  - **[Routing](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/routing.py):** Demonstrates how to direct tasks to appropriate agents based on specific criteria.

- **basic:**
  These examples showcase foundational capabilities of the SDK, including:
  - **[Agent Lifecycle Events](https://github.com/openai/openai-agents-python/blob/main/examples/basic/agent_lifecycle_example.py):** Demonstrates how to handle different stages of an agent's lifecycle, such as initialization and termination.
  - **[Dynamic System Prompts](https://github.com/openai/openai-agents-python/blob/main/examples/basic/dynamic_system_prompt.py):** Shows how to modify system prompts dynamically based on the context or user input.
  - **[Hello World Examples](https://github.com/openai/openai-agents-python/blob/main/examples/basic/hello_world.py):** Provides simple implementations to get started with creating agents.
  - **[Streaming Outputs](https://github.com/openai/openai-agents-python/blob/main/examples/basic/stream_text.py):** Illustrates how to handle streaming responses from agents.
  - **[Tool Integration](https://github.com/openai/openai-agents-python/blob/main/examples/basic/tools.py):** Demonstrates how to integrate tools into agents to extend their functionality.

- **tools:**
  Learn how to implement and integrate tools into your agents, including both OpenAI-hosted tools such as web search and file search, and tools from Model Context Protocol (MCP) servers. The integration with MCP servers allows agents to leverage external tools seamlessly. For example:
  - **[Web Search Tool](https://github.com/openai/openai-agents-python/blob/main/examples/tools/web_search.py):** Enables agents to perform live web searches to retrieve up-to-date information.
  - **[File Search Tool](https://github.com/openai/openai-agents-python/blob/main/examples/tools/file_search.py):** Allows agents to search within specified files or documents to extract relevant information.
  - **[Computer Use Tool](https://github.com/openai/openai-agents-python/blob/main/examples/tools/computer_use.py):** Facilitates agents in performing tasks that simulate user interactions with a computer, such as browsing or form submissions.

- **model_providers:**
  Explore how to use non-OpenAI models with the SDK. This includes configuring agents to interact with models from different providers, enabling flexibility in your workflows. For example:
  - **[Custom Example Agent](https://github.com/openai/openai-agents-python/blob/main/examples/model_providers/custom_example_agent.py):** Demonstrates how to set up a custom agent using a non-OpenAI model provider.
  - **[Custom Example Global](https://github.com/openai/openai-agents-python/blob/main/examples/model_providers/custom_example_global.py):** Shows how to configure a global model provider for all agents.
  - **[Custom Example Provider](https://github.com/openai/openai-agents-python/blob/main/examples/model_providers/custom_example_provider.py):** Illustrates how to implement a custom model provider for specific use cases.

- **handoffs:**
  See practical examples of agent handoffs, where control is transferred between agents based on the context or specific criteria. This includes scenarios like message filtering and streaming responses. For instance:
  - **[Message Filter](https://github.com/openai/openai-agents-python/blob/main/examples/handoffs/message_filter.py):** Demonstrates how an agent can hand off a task to another agent based on the content of the message.
  - **[Message Filter with Streaming](https://github.com/openai/openai-agents-python/blob/main/examples/handoffs/message_filter_streaming.py):** Shows how to handle message filtering with streaming responses between agents.

- **customer_service:**
  A more developed example that illustrates a real-world application:
  - **[Customer Service System](https://github.com/openai/openai-agents-python/blob/main/examples/customer_service/main.py):** An example customer service system for an airline, demonstrating how agents can handle tasks such as FAQ responses and seat bookings.

- **research_bot:**
  A more developed example that illustrates a real-world application:
  - **[Research Bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):** A simple multi-agent research bot that assists users in gathering and summarizing information on a given topic.

For a comprehensive understanding and to see these examples in action, visit the [examples directory](https://github.com/openai/openai-agents-python/tree/main/examples) in the repository. 
