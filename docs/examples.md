# Examples

Check out a variety of sample implementations of the SDK in the examples section of the [repo](https://github.com/openai/openai-agents-python/tree/main/examples). The examples are organized into several categories that demonstrate different patterns and capabilities.

## Categories

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    Examples in this category illustrate common agent design patterns, such as

    -   Deterministic workflows
    -   Agents as tools
    -   Agents as tools with streaming events (`examples/agent_patterns/agents_as_tools_streaming.py`)
    -   Agents as tools with structured input parameters (`examples/agent_patterns/agents_as_tools_structured.py`)
    -   Parallel agent execution
    -   Conditional tool usage
    -   Forcing tool use with different behaviors (`examples/agent_patterns/forcing_tool_use.py`)
    -   Input/output guardrails
    -   LLM as a judge
    -   Routing
    -   Streaming guardrails
    -   Human-in-the-loop with tool approval and state serialization (`examples/agent_patterns/human_in_the_loop.py`)
    -   Human-in-the-loop with streaming (`examples/agent_patterns/human_in_the_loop_stream.py`)
    -   Custom rejection messages for approval flows (`examples/agent_patterns/human_in_the_loop_custom_rejection.py`)

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    These examples showcase foundational capabilities of the SDK, such as

    -   Hello world examples (Default model, GPT-5, open-weight model)
    -   Agent lifecycle management
    -   Run hooks and agent hooks lifecycle example (`examples/basic/lifecycle_example.py`)
    -   Dynamic system prompts
    -   Basic tool usage (`examples/basic/tools.py`)
    -   Tool input/output guardrails (`examples/basic/tool_guardrails.py`)
    -   Image tool output (`examples/basic/image_tool_output.py`)
    -   Streaming outputs (text, items, function call args)
    -   Responses websocket transport with a shared session helper across turns (`examples/basic/stream_ws.py`)
    -   Prompt templates
    -   File handling (local and remote, images and PDFs)
    -   Usage tracking
    -   Runner-managed retry settings (`examples/basic/retry.py`)
    -   Runner-managed retries through a third-party adapter (`examples/basic/retry_litellm.py`)
    -   Non-strict output types
    -   Previous response ID usage

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    Example customer service system for an airline.

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    A financial research agent that demonstrates structured research workflows with agents and tools for financial data analysis.

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    Practical examples of agent handoffs with message filtering, including:

    -   Message filter example (`examples/handoffs/message_filter.py`)
    -   Message filter with streaming (`examples/handoffs/message_filter_streaming.py`)

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    Examples demonstrating how to use hosted MCP (Model Context Protocol) with the OpenAI Responses API, including:

    -   Simple hosted MCP without approval (`examples/hosted_mcp/simple.py`)
    -   MCP connectors such as Google Calendar (`examples/hosted_mcp/connectors.py`)
    -   Human-in-the-loop with interruption-based approvals (`examples/hosted_mcp/human_in_the_loop.py`)
    -   On-approval callback for MCP tool calls (`examples/hosted_mcp/on_approval.py`)

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    Learn how to build agents with MCP (Model Context Protocol), including:

    -   Filesystem examples
    -   Git examples
    -   MCP prompt server examples
    -   SSE (Server-Sent Events) examples
    -   SSE remote server connection (`examples/mcp/sse_remote_example`)
    -   Streamable HTTP examples
    -   Streamable HTTP remote connection (`examples/mcp/streamable_http_remote_example`)
    -   Custom HTTP client factory for Streamable HTTP (`examples/mcp/streamablehttp_custom_client_example`)
    -   Prefetching all MCP tools with `MCPUtil.get_all_function_tools` (`examples/mcp/get_all_mcp_tools_example`)
    -   MCPServerManager with FastAPI (`examples/mcp/manager_example`)
    -   MCP tool filtering (`examples/mcp/tool_filter_example`)

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    Examples of different memory implementations for agents, including:

    -   SQLite session storage
    -   Advanced SQLite session storage
    -   Redis session storage
    -   SQLAlchemy session storage
    -   Dapr state store session storage
    -   Encrypted session storage
    -   OpenAI Conversations session storage
    -   Responses compaction session storage
    -   Stateless Responses compaction with `ModelSettings(store=False)` (`examples/memory/compaction_session_stateless_example.py`)
    -   File-backed session storage (`examples/memory/file_session.py`)
    -   File-backed session with human-in-the-loop (`examples/memory/file_hitl_example.py`)
    -   SQLite in-memory session with human-in-the-loop (`examples/memory/memory_session_hitl_example.py`)
    -   OpenAI Conversations session with human-in-the-loop (`examples/memory/openai_session_hitl_example.py`)
    -   HITL approval/rejection scenario across sessions (`examples/memory/hitl_session_scenario.py`)

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    Explore how to use non-OpenAI models with the SDK, including custom providers and third-party adapters.

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    Examples showing how to build real-time experiences using the SDK, including:

    -   Web application patterns with structured text and image messages
    -   Command-line audio loops and playback handling
    -   Twilio Media Streams integration over WebSocket
    -   Twilio SIP integration using Realtime Calls API attach flows

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    Examples demonstrating how to work with reasoning content, including:

    -   Reasoning content with the Runner API, streaming and non-streaming (`examples/reasoning_content/runner_example.py`)
    -   Reasoning content with OSS models via OpenRouter (`examples/reasoning_content/gpt_oss_stream.py`)
    -   Basic reasoning content example (`examples/reasoning_content/main.py`)

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    Simple deep research clone that demonstrates complex multi-agent research workflows.

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    Learn how to implement OAI hosted tools and experimental Codex tooling such as:

    -   Web search and web search with filters
    -   File search
    -   Code interpreter
    -   Apply patch tool with file editing and approval (`examples/tools/apply_patch.py`)
    -   Shell tool execution with approval callbacks (`examples/tools/shell.py`)
    -   Shell tool with human-in-the-loop interruption-based approvals (`examples/tools/shell_human_in_the_loop.py`)
    -   Hosted container shell with inline skills (`examples/tools/container_shell_inline_skill.py`)
    -   Hosted container shell with skill references (`examples/tools/container_shell_skill_reference.py`)
    -   Local shell with local skills (`examples/tools/local_shell_skill.py`)
    -   Tool search with namespaces and deferred tools (`examples/tools/tool_search.py`)
    -   Computer use
    -   Image generation
    -   Experimental Codex tool workflows (`examples/tools/codex.py`)
    -   Experimental Codex same-thread workflows (`examples/tools/codex_same_thread.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    See examples of voice agents, using our TTS and STT models, including streamed voice examples.
